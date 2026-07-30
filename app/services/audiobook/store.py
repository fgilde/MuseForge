"""Persistence for AudioBook projects — one JSON per project.

Mirrors ``services/director_pipeline.py`` deliberately rather than inventing a
parallel mechanism (PLAN §0.1, §3.1):

  * ``_audiobook_{pid}.json`` in the workspace output dir, same naming shape as
    ``_director_pipeline_{pid}.json``.
  * ``_write_project_json_unlocked`` is the same temp-file + ``os.replace``
    atomic write as ``_write_pipeline_json_unlocked`` — a crash mid-write can
    never leave a half-written project.
  * **Two locks, kept separate**, exactly as the Director does with
    ``_pipeline_lock`` / ``_pipeline_file_lock``: ``_memory_lock`` guards the
    in-process cache, ``_file_lock`` serializes disk access.  Holding the
    memory lock while doing file IO is what turns a slow disk into a stalled
    API, so the two never nest in that order.
  * ``_params_snapshot`` records the verbatim create/import request so
    "re-import with the same settings" and "show me what produced this" are
    reads, not new plumbing.
  * Generated audio lives as real files next to the JSON — never base64 in it.

``out_dir`` is always passed in (usually ``_workspace_dir(workspace)``).  This
module never imports ``launch``, which keeps it testable without the engine.

Self-check: ``python -m services.audiobook.store`` from ``app/``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Optional

from services.audiobook.model import (
    MODEL_VERSION,
    Chapter,
    Project,
    VoiceProfile,
    new_id,
    sanitize_project,
)

AUDIOBOOK_STATE_VERSION = MODEL_VERSION
_AUDIOBOOK_FILE_PREFIX = "_audiobook_"

# Guards the in-memory cache only.  Never held across file IO.
_memory_lock = threading.RLock()
# Serializes every read/write/delete of a project file, so a listing snapshot
# cannot be overwritten by a concurrent save (the exact race
# ``list_pipeline_states`` documents).
_file_lock = threading.RLock()

# pid -> (filepath, project dict as last read/written).  A cache, not the truth:
# it is only ever populated from disk and dropped on delete.
_cache: dict[str, tuple[str, dict]] = {}


class ProjectNotFoundError(KeyError):
    """Raised when a pid has no JSON on disk."""


# ── Atomic write (copied shape from director_pipeline) ─────────────────────


def _write_project_json_unlocked(filepath: str, state: dict) -> None:
    """Atomically replace one project JSON while ``_file_lock`` is held."""
    temp_filepath = f"{filepath}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_filepath, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False, default=str)
        os.replace(temp_filepath, filepath)
    finally:
        if os.path.isfile(temp_filepath):
            try:
                os.remove(temp_filepath)
            except OSError:
                pass


def project_filename(pid: str) -> str:
    return f"{_AUDIOBOOK_FILE_PREFIX}{pid}.json"


def _scan_dirs(out_dir: str) -> list[str]:
    """``out_dir`` plus its workspace subdirectories, like the Director scan."""
    dirs = [out_dir]
    if os.path.isdir(out_dir):
        for name in sorted(os.listdir(out_dir)):
            sub = os.path.join(out_dir, name)
            if os.path.isdir(sub):
                dirs.append(sub)
    return dirs


def find_project_file(out_dir: str, pid: str) -> Optional[str]:
    """Locate a project JSON in ``out_dir`` or one workspace below it."""
    target = project_filename(pid)
    direct = os.path.join(out_dir, target)
    if os.path.isfile(direct):
        return direct
    if os.path.isdir(out_dir):
        for name in sorted(os.listdir(out_dir)):
            sub = os.path.join(out_dir, name, target)
            if os.path.isfile(sub):
                return sub
    return None


# ── CRUD ───────────────────────────────────────────────────────────────────


def create_project(
    out_dir: str,
    title: str = "Untitled audiobook",
    *,
    language: str = "en",
    chapters: Optional[list[Chapter]] = None,
    voice_profiles: Optional[list[VoiceProfile]] = None,
    params_snapshot: Optional[dict] = None,
    pid: Optional[str] = None,
) -> Project:
    """Create, persist and return a new project.

    ``params_snapshot`` should be the request body that caused the creation
    (import options, story hand-off metadata, …) — stored verbatim.
    """
    now = time.time()
    project = Project(
        id=pid or new_id(),
        title=title or "Untitled audiobook",
        language=language or "en",
        version=AUDIOBOOK_STATE_VERSION,
        created_at=now,
        updated_at=now,
        chapters=list(chapters or []),
        voice_profiles=list(voice_profiles or []),
        params_snapshot=dict(params_snapshot or {}),
    )
    if not project.chapters:
        project.chapters = [Chapter(title="Chapter 1")]
    sanitize_project(project)
    save_project(out_dir, project)
    return project


def save_project(out_dir: str, project: Project) -> str:
    """Sanitize, stamp ``updated_at`` and atomically write.  Returns the path.

    Saving into the directory the project was last read from keeps a project
    that lives in a workspace subdirectory there, instead of silently forking a
    second copy at the top level.
    """
    sanitize_project(project)
    project.updated_at = time.time()
    state = project.to_dict()
    with _file_lock:
        filepath = find_project_file(out_dir, project.id)
        if filepath is None:
            os.makedirs(out_dir, exist_ok=True)
            filepath = os.path.join(out_dir, project_filename(project.id))
        _write_project_json_unlocked(filepath, state)
    with _memory_lock:
        _cache[project.id] = (filepath, state)
    return filepath


def load_project(out_dir: str, pid: str) -> Optional[Project]:
    """Read one project from disk, self-healing it on the way in."""
    with _file_lock:
        filepath = find_project_file(out_dir, pid)
        if filepath is None:
            with _memory_lock:
                _cache.pop(pid, None)
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"[AudioBook] Failed to read project {pid}: {exc}")
            return None
    project = sanitize_project(Project.from_dict(state))
    with _memory_lock:
        _cache[pid] = (filepath, state)
    return project


def list_projects(out_dir: str) -> list[dict]:
    """Summaries for the project picker, newest first.

    A summary read must stay cheap, so it parses the JSON but does not build
    the dataclass graph.  Unreadable files are skipped, not fatal — same
    tolerance as ``list_pipeline_states``.
    """
    results: list[dict] = []
    with _file_lock:
        for scan_dir in _scan_dirs(out_dir):
            try:
                names = sorted(os.listdir(scan_dir))
            except OSError:
                continue
            for name in names:
                if not (
                    name.startswith(_AUDIOBOOK_FILE_PREFIX) and name.endswith(".json")
                ):
                    continue
                filepath = os.path.join(scan_dir, name)
                try:
                    with open(filepath, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                except (OSError, ValueError):
                    continue
                chapters = data.get("chapters") or []
                results.append({
                    "id": data.get("project_id") or name[len(_AUDIOBOOK_FILE_PREFIX):-5],
                    # Mirrors the key the full project serialises as, so a
                    # client that lists and then fetches can use one field
                    # name throughout instead of switching between the two.
                    "project_id": data.get("project_id") or name[len(_AUDIOBOOK_FILE_PREFIX):-5],
                    "title": data.get("title") or "",
                    "language": data.get("language") or "",
                    "version": data.get("version"),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "chapter_count": len(chapters),
                    "voice_count": len(data.get("voice_profiles") or []),
                    "rendered_chapters": sum(
                        1 for one in chapters
                        if isinstance(one, dict) and one.get("audio_path")
                    ),
                    "workspace": (
                        os.path.basename(scan_dir)
                        if os.path.abspath(scan_dir) != os.path.abspath(out_dir)
                        else "default"
                    ),
                    "_filepath": filepath,
                })
    results.sort(key=lambda one: one.get("updated_at") or one.get("created_at") or 0, reverse=True)
    return results


def delete_project(out_dir: str, pid: str) -> bool:
    """Remove the project JSON.  Generated audio files are left alone.

    Rendered chapter audio is a normal workspace output the gallery shows and
    the user may still want; deleting it as a side effect of removing a project
    would be a data-loss surprise.  The endpoint can offer that explicitly.
    """
    with _file_lock:
        filepath = find_project_file(out_dir, pid)
        if filepath is None:
            with _memory_lock:
                _cache.pop(pid, None)
            return False
        try:
            os.remove(filepath)
        except OSError as exc:
            print(f"[AudioBook] Failed to delete project {pid}: {exc}")
            return False
    with _memory_lock:
        _cache.pop(pid, None)
    return True


def update_project(
    out_dir: str, pid: str, updater: Callable[[Project], Any],
) -> Optional[Project]:
    """Read-modify-write a project under one continuous file lock.

    Any mutation an endpoint performs (assign a voice, add a chapter, record a
    rendered file) must go through here.  Load-outside/save-later is the race
    that loses a concurrent render's ``audio_path``.  ``updater`` may return a
    ``Project`` to replace the loaded one, or mutate it in place and return
    ``None``.
    """
    with _file_lock:
        project = load_project(out_dir, pid)
        if project is None:
            return None
        replacement = updater(project)
        if isinstance(replacement, Project):
            project = replacement
        save_project(out_dir, project)
        return project


def snapshot_params(project: Project) -> dict:
    """The stored ``_params_snapshot`` plus the current shape of the project.

    The Director's equivalent is ``_params_snapshot`` in
    ``_save_pipeline_state``: enough to faithfully recreate the request.  Here
    the useful extra is the voice cast and render settings, since "same book,
    new voices" and "same voices, new book" are both real workflows.
    """
    return {
        "request": dict(project.params_snapshot),
        "title": project.title,
        "language": project.language,
        "version": project.version,
        "voice_profiles": [one.to_dict() for one in project.voice_profiles],
        "render_settings": dict(project.render_settings),
        "chapter_titles": [one.title for one in project.chapters],
    }


if __name__ == "__main__":
    # Self-check: atomic round-trip, workspace-subdir discovery, update under
    # lock, listing order, delete.  `python -m services.audiobook.store`.
    import shutil
    import tempfile

    from services.audiobook.model import Block, Run

    root = tempfile.mkdtemp(prefix="audiobook_store_selfcheck_")
    try:
        created = create_project(
            root, "My Book", language="de",
            voice_profiles=[VoiceProfile(id="p1", name="Erzähler")],
            params_snapshot={"source": "import", "file": "book.docx"},
        )
        assert created.default_profile_id == "p1", created.default_profile_id
        path = os.path.join(root, project_filename(created.id))
        assert os.path.isfile(path), path

        # Round-trip keeps content and snapshot; only one file on disk.
        loaded = load_project(root, created.id)
        assert loaded is not None and loaded.title == "My Book"
        assert loaded.language == "de"
        assert loaded.params_snapshot["file"] == "book.docx"
        assert loaded.version == AUDIOBOOK_STATE_VERSION
        leftovers = [n for n in os.listdir(root) if n.endswith(".tmp")]
        assert not leftovers, leftovers

        # update_project mutates in place and persists.
        def _add(project: Project) -> None:
            project.chapters[0].blocks = [
                Block(runs=[Run(text="Es war einmal.", profile_id="p1")])
            ]
            project.chapters.append(Chapter(title="Kapitel 2"))

        updated = update_project(root, created.id, _add)
        assert updated is not None and len(updated.chapters) == 2
        again = load_project(root, created.id)
        assert again.chapters[0].blocks[0].runs[0].text == "Es war einmal."

        # A project living in a workspace subdirectory is found and saved back
        # THERE, not forked to the top level.
        workspace = os.path.join(root, "ws1")
        os.makedirs(workspace)
        nested = create_project(workspace, "Nested")
        found = find_project_file(root, nested.id)
        assert found is not None and os.path.dirname(found) == workspace, found
        nested_loaded = load_project(root, nested.id)
        assert nested_loaded is not None and nested_loaded.title == "Nested"
        nested_loaded.title = "Nested v2"
        saved_to = save_project(root, nested_loaded)
        assert os.path.dirname(saved_to) == workspace, saved_to
        assert not os.path.isfile(os.path.join(root, project_filename(nested.id)))

        # Listing sees both, newest-updated first, and labels the workspace.
        listing = list_projects(root)
        assert len(listing) == 2, listing
        assert listing[0]["id"] == nested.id, listing
        by_id = {one["id"]: one for one in listing}
        assert by_id[created.id]["chapter_count"] == 2
        assert by_id[created.id]["workspace"] == "default"
        assert by_id[nested.id]["workspace"] == "ws1"

        # Unknown pid: no crash, no phantom entry.
        assert load_project(root, "nope") is None
        assert update_project(root, "nope", lambda p: None) is None
        assert delete_project(root, "nope") is False

        # Reproducibility snapshot.
        snap = snapshot_params(load_project(root, created.id))
        assert snap["request"]["source"] == "import"
        assert snap["chapter_titles"] == ["Chapter 1", "Kapitel 2"], snap

        assert delete_project(root, created.id) is True
        assert load_project(root, created.id) is None
        assert len(list_projects(root)) == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("audiobook.store self-check OK")
