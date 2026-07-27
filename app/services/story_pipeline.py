"""Server-side Storywriter pipeline (docs/PLAN-text-audiobook.md §2.3).

Long-form prose generation in a background thread, structured as three LLM
passes per story:

  1. outline    — premise + target length -> structured plan (json_schema)
  2. chapter i  — prose, one call per chapter, high temperature
  3. continuity — re-reads the chapter that was ACTUALLY written and
                  refreshes the running synopsis + character state

The rolling context (outline + running synopsis + character state + the
tail of the previous prose) is the whole point: the full story text is
never sent back to the model, so a 40-chapter book costs the same context
as a 3-chapter one.

Structure deliberately mirrors ``services/director_pipeline.py`` — same
atomic state file, same file-lock-outside-memory-lock save order, same
"cancellation is an absorbing terminal state" update wrapper, same
non-daemon worker so an overnight run survives a browser disconnect, same
``{current,total,message,step,total_steps}`` progress shape so the
existing PipelinePlaceholder renders it unchanged.

Wiring (done by launch.py, not here — this module imports no launch code):
  start_story(params, out_dir, ensure_model=_ensure_llm_loaded)
``ensure_model`` and ``out_dir`` are injected rather than imported so the
worker never reaches back into launch.py.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import uuid
from functools import wraps
from typing import Callable, Optional

from services.guide_loader import load_guide

# Optional: the Director's hard safety scan. Applied to the premise and to
# every chapter text exactly as the Director applies it to screenplays.
try:
    from services.director.safety_scan import (
        SafetyViolationError,
        assert_no_minor_content,
    )
    _HAVE_SAFETY_SCAN = True
except Exception:  # pragma: no cover - only when the director package moves
    _HAVE_SAFETY_SCAN = False

    class SafetyViolationError(RuntimeError):  # type: ignore[no-redef]
        pass

    def assert_no_minor_content(text: str, source: str) -> None:  # type: ignore[misc]
        return None

# Optional: json_repair is already an (optional) dependency of the Director
# planners — reused here, not added.
try:
    import json_repair  # type: ignore
except Exception:  # pragma: no cover
    json_repair = None  # type: ignore


# ── Module state ───────────────────────────────────────────────────────────

_stories: dict = {}
_story_lock = threading.Lock()
_story_file_lock = threading.RLock()
_story_threads: dict[str, threading.Thread] = {}
_story_starting: set[str] = set()
_story_operations: set[str] = set()
_story_deleting: set[str] = set()

STORY_STATE_VERSION = 1
_STORY_FILE_PREFIX = "_story_"

STATUSES = ("queued", "planning", "writing", "paused", "completed", "failed", "cancelled")
_ACTIVE_STORY_STATUSES = ("queued", "planning", "writing", "paused")

# After a user Stop, only artifacts a late-returning pass legitimately
# produced may still be written. Status, progress, error and completion are
# frozen — otherwise a chapter finishing 30 s after Stop flips the story
# back to "writing" or "completed" and the Stop is lost.
_CANCELLED_ARTIFACT_FIELDS = {
    "chapters",
    "output_files",
    "llm_passes",
    "synopsis_running",
    "character_state",
    "outline",
    "title",
}

# Fields written to _story_{pid}.json. Everything else in the in-memory dict
# (out_dir, the ensure_model callback, the active stream id) is runtime-only.
_STORY_PERSISTED_FIELDS = (
    "version",
    "story_id",
    "created_at",
    "completed_at",
    "status",
    "phase",
    "title",
    "premise",
    "params",
    "outline",
    "chapters",
    "synopsis_running",
    "synopsis_stale",
    "character_state",
    "llm_passes",
    "output_files",
    "progress",
    "error",
    "total_time_sec",
    "_params_snapshot",
)

# ── Length maths ───────────────────────────────────────────────────────────
# The UI slider is in pages; everything downstream is in words. 275
# words/page is the plan's figure (§2.3) and matches a mass-market paperback.
WORDS_PER_PAGE = 275
# Auto chapter count aims for this; long enough to be a real chapter, short
# enough that one LLM call can hold the whole thing.
AUTO_WORDS_PER_CHAPTER = 1500
MIN_CHAPTER_WORDS = 500
MAX_CHAPTER_WORDS = 4000
# Prose token budget = words * 2 (a conservative English words->tokens
# factor) plus slack for the model's opening throat-clearing. Capped so a
# runaway target can't ask llama-server for a 100k-token completion.
MAX_CHAPTER_TOKENS = 12000
OUTLINE_MAX_TOKENS = 6144
CONTINUITY_MAX_TOKENS = 2048
# The tail of already-written prose handed to the next chapter. Big enough
# to continue a scene seamlessly, small enough that context stays flat.
CONTEXT_TAIL_WORDS = 1500

DEFAULT_PROSE_TEMPERATURE = 0.9
OUTLINE_TEMPERATURE = 0.25
CONTINUITY_TEMPERATURE = 0.2
# Long writing sessions must not unload the model between chapters —
# reloading 16 GB of weights costs minutes per chapter.
STORY_IDLE_TIMEOUT_S = 1800


class StoryBusyError(RuntimeError):
    """Raised when a mutation conflicts with active story work."""


# ── JSON schemas ───────────────────────────────────────────────────────────
# These are grammar-enforced by llama-server for provider "local"
# (llm_service.generate_streaming(json_schema=...)): the sampler masks every
# token that would break the schema, so the model physically cannot emit
# prose, markdown fences or a repeat loop. On a remote provider the schema
# degrades to a hint, which is why the parser below is still forgiving.
#
# additionalProperties=False matters: a closed object emits each key at most
# once, which makes field-level repetition loops unrepresentable.

# One planned chapter. Referenced by both the outline pass and the
# extend pass so the two can never drift apart.
_CHAPTER_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        # Ordered, concrete, dramatisable events. minItems keeps the model
        # from emitting a single mood line the prose pass can't use.
        "beats": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 8,
        },
    },
    "required": ["title", "beats"],
    "additionalProperties": False,
}

OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "logline": {"type": "string"},
        "setting": {"type": "string"},
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    # Renderable on the page: voice, body, want, secret.
                    "description": {"type": "string"},
                },
                "required": ["name", "description"],
                "additionalProperties": False,
            },
            "minItems": 1,
        },
        "chapters": {
            "type": "array",
            "items": _CHAPTER_PLAN_SCHEMA,
            "minItems": 1,
        },
    },
    "required": ["title", "logline", "characters", "chapters"],
    "additionalProperties": False,
}

# extend_story(): only new chapters, appended after the existing ones.
EXTEND_OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "items": _CHAPTER_PLAN_SCHEMA,
            "minItems": 1,
        },
    },
    "required": ["chapters"],
    "additionalProperties": False,
}

CONTINUITY_SCHEMA = {
    "type": "object",
    "properties": {
        # Full replacement synopsis covering the whole story so far — the
        # next chapter sees this and the prose tail, nothing else.
        "synopsis": {"type": "string"},
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "state": {"type": "string"},
                },
                "required": ["name", "state"],
                "additionalProperties": False,
            },
        },
        "open_threads": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["synopsis", "characters"],
    "additionalProperties": False,
}


# ── Guides ─────────────────────────────────────────────────────────────────

def _guide(name: str) -> str:
    """Load a story guide (Markdown under llm_guides/story/)."""
    return load_guide("story", name)


def _content_guidance(nsfw: bool, explicit_guide: str) -> str:
    """Pick the mature or the tame content block for a pass.

    Selection is by the master flag, not by a prompt suffix bolted on at
    call time (§2.5). The tame block reuses the Director's version-
    controlled safety guardrails so both features stay in sync.
    """
    if nsfw:
        return _guide(explicit_guide)
    try:
        from services.director.nsfw_guidance import get_safe_content_guidance
        return get_safe_content_guidance()
    except Exception:
        return "Keep all content PG-13."


def _is_nsfw(params: dict) -> bool:
    """Explicitness is only in play when the master NSFW switch is on."""
    return bool(params.get("nsfw"))


def _system_prompt(base_guide: str, explicit_guide: str, params: dict) -> str:
    blocks = [_guide(base_guide), _content_guidance(_is_nsfw(params), explicit_guide)]
    return "\n\n".join(b for b in blocks if b).strip()


# ── Length / context helpers (pure — covered by the self-check) ────────────

def total_target_words(min_pages) -> int:
    try:
        pages = max(1, int(min_pages or 1))
    except (TypeError, ValueError):
        pages = 1
    return pages * WORDS_PER_PAGE


def auto_chapter_count(min_pages) -> int:
    """Chapter count when the user let the model decide."""
    return max(3, round(total_target_words(min_pages) / AUTO_WORDS_PER_CHAPTER))


def chapter_target_words(min_pages, chapter_count) -> int:
    """Per-chapter word target, clamped to what one LLM call can hold."""
    try:
        count = max(1, int(chapter_count or 1))
    except (TypeError, ValueError):
        count = 1
    words = total_target_words(min_pages) / count
    return int(max(MIN_CHAPTER_WORDS, min(MAX_CHAPTER_WORDS, round(words))))


def chapter_token_budget(target_words: int) -> int:
    """max_new_tokens for a prose pass — generous, but bounded."""
    return int(min(MAX_CHAPTER_TOKENS, target_words * 2 + 512))


def _word_count(text: str) -> int:
    return len((text or "").split())


def _tail_words(text: str, limit: int = CONTEXT_TAIL_WORDS) -> str:
    words = (text or "").split()
    if len(words) <= limit:
        return (text or "").strip()
    return " ".join(words[-limit:]).strip()


def _outline_block(outline: dict) -> str:
    """The story bible, flattened for the prompt."""
    lines = [
        f"Title: {outline.get('title', '')}",
        f"Logline: {outline.get('logline', '')}",
    ]
    if outline.get("setting"):
        lines.append(f"Setting: {outline['setting']}")
    characters = outline.get("characters") or []
    if characters:
        lines.append("Characters:")
        for char in characters:
            role = f" ({char.get('role')})" if char.get("role") else ""
            lines.append(f"- {char.get('name', '?')}{role}: {char.get('description', '')}")
    chapters = outline.get("chapters") or []
    if chapters:
        lines.append("Chapter plan:")
        for i, chapter in enumerate(chapters):
            lines.append(f"  {i + 1}. {chapter.get('title', '')}")
    return "\n".join(lines)


def _character_state_block(character_state: dict) -> str:
    if not character_state:
        return ""
    lines = []
    for name, state in (character_state.get("characters") or {}).items():
        lines.append(f"- {name}: {state}")
    threads = character_state.get("open_threads") or []
    if threads:
        lines.append("Open threads:")
        lines.extend(f"- {t}" for t in threads)
    return "\n".join(lines)


def build_chapter_context(state: dict, index: int, instruction: Optional[str] = None) -> str:
    """The user message for one chapter pass.

    Contains the outline, the running synopsis, the character state and the
    TAIL of the prose written so far — never the whole story. Everything
    that makes long stories survivable lives in this function.
    """
    params = state.get("params") or {}
    outline = state.get("outline") or {}
    chapters = state.get("chapters") or []
    chapter = chapters[index] if 0 <= index < len(chapters) else {}
    total = len(chapters)

    previous_text = "\n\n".join(
        (chapters[i].get("text") or "") for i in range(index)
    )
    tail = _tail_words(previous_text)

    target = chapter_target_words(params.get("min_pages"), total)

    parts = [
        "=== STORY BIBLE ===",
        _outline_block(outline),
    ]
    synopsis = (state.get("synopsis_running") or "").strip()
    if synopsis:
        parts += ["", "=== WHAT HAS HAPPENED SO FAR (running synopsis) ===", synopsis]
    char_block = _character_state_block(state.get("character_state") or {})
    if char_block:
        parts += ["", "=== CURRENT CHARACTER STATE ===", char_block]
    if tail:
        parts += [
            "",
            "=== END OF THE PREVIOUS CHAPTER (verbatim — continue seamlessly, "
            "do not repeat it) ===",
            tail,
        ]

    beats = chapter.get("beats") or []
    parts += [
        "",
        "=== YOUR TASK ===",
        f'Write chapter {index + 1} of {total}: "{chapter.get("title", "")}"',
    ]
    if beats:
        parts.append("Cover these beats, in this order, as dramatised scenes:")
        parts.extend(f"- {b}" for b in beats)
    else:
        parts.append(
            "No beats were planned for this chapter — continue the story from "
            "the synopsis and drive it toward the outline's ending."
        )
    style = [
        f"Genre: {params.get('genre') or 'unspecified'}",
        f"Tone: {params.get('tone') or 'unspecified'}",
        f"Point of view: {params.get('pov') or 'third person limited'}",
        f"Tense: {params.get('tense') or 'past'}",
        f"Target audience: {params.get('audience') or 'adult general readership'}",
    ]
    parts += ["", *style]
    parts += [
        "",
        f"Target length: about {target} words. Write the full chapter — do not "
        "stop short and do not summarise scenes to save space.",
    ]
    extra = (instruction or chapter.get("instruction") or "").strip()
    if extra:
        parts += ["", f"Additional instruction for this chapter: {extra}"]
    parts += [
        "",
        "Output ONLY the chapter's prose. No title, no heading, no chapter "
        "number, no commentary, no summary at the end.",
    ]
    return "\n".join(parts)


def _continuity_context(state: dict, index: int) -> str:
    chapters = state.get("chapters") or []
    chapter = chapters[index] if 0 <= index < len(chapters) else {}
    parts = []
    synopsis = (state.get("synopsis_running") or "").strip()
    if synopsis:
        parts += ["=== PREVIOUS SYNOPSIS (revise and extend it) ===", synopsis, ""]
    char_block = _character_state_block(state.get("character_state") or {})
    if char_block:
        parts += ["=== PREVIOUS CHARACTER STATE ===", char_block, ""]
    parts += [
        f"=== CHAPTER {index + 1} AS WRITTEN "
        f"({chapter.get('title', '')}) ===",
        chapter.get("text") or "",
        "",
        "Update the record from what this chapter actually says.",
    ]
    return "\n".join(parts)


# ── Export formatting (pure) ───────────────────────────────────────────────

def format_story(state: dict, fmt: str = "md") -> str:
    """Render a story state as Markdown or plain text."""
    title = state.get("title") or (state.get("outline") or {}).get("title") or "Untitled"
    outline = state.get("outline") or {}
    chapters = [c for c in (state.get("chapters") or []) if (c.get("text") or "").strip()]
    if fmt == "txt":
        blocks = [title.upper(), ""]
        if outline.get("logline"):
            blocks += [outline["logline"], ""]
        for chapter in chapters:
            heading = f"CHAPTER {chapter.get('index', 0) + 1}"
            if chapter.get("title"):
                heading += f": {chapter['title'].upper()}"
            blocks += [heading, "", (chapter.get("text") or "").strip(), ""]
        return "\n".join(blocks).rstrip() + "\n"
    blocks = [f"# {title}", ""]
    if outline.get("logline"):
        blocks += [f"*{outline['logline']}*", ""]
    for chapter in chapters:
        number = chapter.get("index", 0) + 1
        blocks += [f"## {number}. {chapter.get('title') or ''}".rstrip(), ""]
        blocks += [(chapter.get("text") or "").strip(), ""]
    return "\n".join(blocks).rstrip() + "\n"


def _safe_slug(title: str) -> str:
    slug = re.sub(r"[^\w\-]+", "_", (title or "").strip()).strip("_")
    return (slug[:48] or "story").lower()


# ── Persistence ────────────────────────────────────────────────────────────

def _write_story_json_unlocked(filepath: str, state: dict) -> None:
    """Atomically replace one story JSON file while its file lock is held."""
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


def _persisted_snapshot(state: dict) -> dict:
    """Copy the serializable fields out of a live state.

    Lists of dicts are copied one level deep so json.dump can't trip over
    the worker appending a chapter mid-serialization.
    """
    snapshot = {k: state[k] for k in _STORY_PERSISTED_FIELDS if k in state}
    snapshot["chapters"] = [dict(c) for c in (state.get("chapters") or [])]
    snapshot["llm_passes"] = list(state.get("llm_passes") or [])
    snapshot["output_files"] = list(state.get("output_files") or [])
    created = state.get("created_at")
    snapshot["total_time_sec"] = (
        (state.get("completed_at") or time.time()) - created if created else None
    )
    return snapshot


def _save_story_state(pid: str) -> bool:
    """Serialize one live story without racing other writers."""
    with _story_file_lock:
        return _save_story_state_locked(pid)


def _save_story_state_locked(pid: str) -> bool:
    # File lock outside, memory lock inside — and the snapshot is built
    # while the memory lock is held, then written outside it (the Director's
    # ordering; the reverse deadlocks against list/load).
    with _story_lock:
        state = _stories.get(pid)
        if not state:
            return False
        out_dir = state.get("out_dir") or "outputs"
        snapshot = _persisted_snapshot(state)
    try:
        os.makedirs(out_dir, exist_ok=True)
        _write_story_json_unlocked(
            os.path.join(out_dir, f"{_STORY_FILE_PREFIX}{pid}.json"), snapshot,
        )
        return True
    except Exception as exc:
        print(f"[Story] Failed to save state for {pid}: {exc}")
        return False


def _scan_dirs(out_dir: str) -> list[str]:
    """out_dir plus its workspace subdirectories."""
    dirs = [out_dir]
    try:
        for name in os.listdir(out_dir):
            sub = os.path.join(out_dir, name)
            if os.path.isdir(sub):
                dirs.append(sub)
    except OSError:
        pass
    return dirs


def list_stories(out_dir: str) -> list[dict]:
    """Summaries of every saved story under out_dir, newest first."""
    results: list[dict] = []
    if not os.path.isdir(out_dir):
        return results
    for scan_dir in _scan_dirs(out_dir):
        try:
            names = os.listdir(scan_dir)
        except OSError:
            continue
        for fname in names:
            if not (fname.startswith(_STORY_FILE_PREFIX) and fname.endswith(".json")):
                continue
            filepath = os.path.join(scan_dir, fname)
            try:
                with _story_file_lock:
                    with open(filepath, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                    pid = data.get("story_id", "")
                    status = data.get("status", "unknown")
                    # A story whose status says active but which has no live
                    # worker died with the process. Same normalization the
                    # Director does for "running" pipelines.
                    with _story_lock:
                        live = pid in _stories
                    if status in _ACTIVE_STORY_STATUSES and not live:
                        data["status"] = status = "crashed"
                        _write_story_json_unlocked(filepath, data)
                chapters = data.get("chapters") or []
                results.append({
                    "id": pid,
                    "status": status,
                    "title": data.get("title") or "",
                    "premise": (data.get("premise") or "")[:200],
                    "created_at": data.get("created_at"),
                    "chapter_count": len(chapters),
                    "chapters_done": sum(
                        1 for c in chapters if c.get("status") == "done"
                    ),
                    "word_count": sum(int(c.get("word_count") or 0) for c in chapters),
                    "output_count": len(data.get("output_files") or []),
                    "workspace": (
                        os.path.basename(scan_dir) if scan_dir != out_dir else "default"
                    ),
                    "_filepath": filepath,
                })
            except Exception:
                pass
    results.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return results


def _find_story_file(out_dir: str, pid: str) -> Optional[str]:
    target = f"{_STORY_FILE_PREFIX}{pid}.json"
    for scan_dir in _scan_dirs(out_dir):
        filepath = os.path.join(scan_dir, target)
        if os.path.isfile(filepath):
            return filepath
    return None


def load_story(out_dir: str, pid: str) -> Optional[dict]:
    """Read a saved story state from disk (out_dir or a workspace subdir)."""
    with _story_file_lock:
        filepath = _find_story_file(out_dir, pid)
        if not filepath:
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            print(f"[Story] Failed to load {pid}: {exc}")
            return None


def _update_saved_story(out_dir: str, pid: str, updater) -> Optional[dict]:
    """Load / mutate / atomically write back one saved story."""
    with _story_file_lock:
        filepath = _find_story_file(out_dir, pid)
        if not filepath:
            return None
        with open(filepath, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        updater(state)
        _write_story_json_unlocked(filepath, state)
        return state


# ── Exclusive-operation claims ─────────────────────────────────────────────

def _claim_story_operation(pid: str) -> bool:
    """Reserve a terminal story for one mutation (edit / export / regen)."""
    with _story_lock:
        if (
            pid in _story_threads
            or pid in _story_starting
            or pid in _story_operations
            or pid in _story_deleting
            or _stories.get(pid, {}).get("status") in _ACTIVE_STORY_STATUSES
        ):
            return False
        _story_operations.add(pid)
        return True


def _release_story_operation(pid: str) -> None:
    with _story_lock:
        _story_operations.discard(pid)


def _exclusive_story_operation(function):
    """Keep a saved-state mutation away from any other story work."""
    @wraps(function)
    def wrapped(out_dir: str, pid: str, *args, **kwargs):
        if not _claim_story_operation(pid):
            raise StoryBusyError("Story is still active; try again shortly.")
        try:
            return function(out_dir, pid, *args, **kwargs)
        finally:
            _release_story_operation(pid)
    return wrapped


def any_story_active() -> bool:
    """True while any story holds a worker, a claim, or an active status."""
    with _story_lock:
        return bool(
            _story_threads or _story_starting or _story_operations or _story_deleting
        ) or any(
            s.get("status") in _ACTIVE_STORY_STATUSES for s in _stories.values()
        )


# ── State updates ──────────────────────────────────────────────────────────

def _update_story(pid: str, **kwargs) -> bool:
    """Thread-safe update; cancellation is an absorbing terminal state.

    Copied semantics from director_pipeline._update_pipeline: once a story
    is cancelled, only artifact fields (the chapter a pass was already
    writing, output files) may still land. No later phase, completion or
    failure may replace the user's Stop.
    """
    with _story_lock:
        state = _stories.get(pid)
        if not state:
            return False
        if state.get("status") == "cancelled":
            if set(kwargs) - _CANCELLED_ARTIFACT_FIELDS:
                return False
        state.update(kwargs)
        return True


def _is_cancelled(pid: str) -> bool:
    with _story_lock:
        return _stories.get(pid, {}).get("status") == "cancelled"


def _snapshot(pid: str) -> Optional[dict]:
    with _story_lock:
        state = _stories.get(pid)
        return dict(state) if state else None


def _progress(current: int, total: int, message: str, step: int = 0,
              total_steps: int = 0) -> dict:
    """EXACTLY the Director's progress shape — PipelinePlaceholder reads it."""
    return {
        "current": current,
        "total": total,
        "message": message,
        "step": step,
        "total_steps": total_steps,
    }


def _writing_progress(chapters: list, message: str, sub: int = 0) -> dict:
    """Chapters in current/total, overall pass count in step/total_steps."""
    total = len(chapters)
    done = sum(1 for c in chapters if c.get("status") == "done")
    return _progress(done, total, message, step=1 + 2 * done + sub,
                     total_steps=1 + 2 * total)


# ── LLM plumbing ───────────────────────────────────────────────────────────

def _model_downloaded(model_id: str) -> bool:
    """Is this registry model's GGUF already on disk?

    llm_service exposes no is_downloaded() helper, so this mirrors the
    cache-path derivation in llm_service.load_model (cache dir +
    cache_dir_override or the repo basename without "-GGUF"). Wrong only in
    the harmless direction: a miss just means the first candidate wins.
    """
    try:
        from services import llm_service
        entry = llm_service.MODEL_REGISTRY.get(model_id)
        if not entry:
            return False
        basename = model_id.split("/")[-1]
        subdir = entry.get("cache_dir_override") or basename.replace("-GGUF", "")
        return os.path.isfile(
            os.path.join(llm_service.get_model_dir(), subdir, entry["gguf_file"]),
        )
    except Exception:
        return False


def _pick_model(use_case: str, override: Optional[str] = None) -> Optional[str]:
    """params override, else the first downloaded candidate, else the first."""
    if override:
        return override
    try:
        from services import llm_service
        candidates = llm_service.models_for_use_case(use_case)
    except Exception:
        return None
    if not candidates:
        return None
    for model_id in candidates:
        if _model_downloaded(model_id):
            return model_id
    return candidates[0]


def _capture_story_pass(pid: str, pass_name: str, model_id: Optional[str]) -> None:
    """Append one pass to the story's LLM log (same fields as the Director).

    Reads llm_service's last-call globals exactly like
    director_pipeline._capture_llm_pass, so the reader can show what the
    model was asked, per pass.
    """
    try:
        from services import llm_service
        entry = {
            "pass": pass_name,
            "model_id": model_id,
            "system_prompt": getattr(llm_service, "_last_system_prompt", "") or "",
            "user_prompt": getattr(llm_service, "_last_user_prompt", "") or "",
            "response_text": getattr(llm_service, "_stream_buffer", "") or "",
            "thinking_text": getattr(llm_service, "_last_thinking_text", None),
            "at": time.time(),
        }
        with _story_lock:
            state = _stories.get(pid)
            if state is not None:
                state.setdefault("llm_passes", []).append(entry)
    except Exception:
        pass


def _ensure_model(pid: str, model_id: Optional[str]) -> None:
    """Invoke the injected loader callback (launch._ensure_llm_loaded)."""
    if not model_id:
        return
    with _story_lock:
        callback: Optional[Callable] = (_stories.get(pid) or {}).get("_ensure_model")
    if callback is None:
        print(f"[Story {pid}] No ensure_model callback — assuming {model_id} is loaded")
        return
    callback(model_id)


def _run_llm(pid: str, pass_name: str, *, model_id: Optional[str],
             system_prompt: str, user_prompt: str, stream_id: str,
             max_new_tokens: int, temperature: float,
             json_schema: Optional[dict] = None) -> str:
    """One LLM pass: load the model, stream, log, return the text."""
    from services import llm_service

    _ensure_model(pid, model_id)
    _update_story(pid, _active_stream_id=stream_id)
    params = (_snapshot(pid) or {}).get("params") or {}
    kwargs = {
        "prompt": user_prompt,
        "system_prompt": system_prompt,
        "max_new_tokens": max_new_tokens,
        "temperature": temperature,
        "top_p": float(params.get("top_p") or 0.95),
        "stream_id": stream_id,
        # Thinking off for every pass: grammar-constrained JSON requires it,
        # and a chapter must not be prefixed with the model's reasoning.
        "thinking_budget": 0,
        "enable_thinking": False,
    }
    if json_schema is not None:
        kwargs["json_schema"] = json_schema
    try:
        text = llm_service.generate_streaming(**kwargs)
    except Exception as exc:
        if json_schema is None:
            raise
        # Never let the grammar make a pass WORSE than it would be without
        # it — a rejecting server (old binary, remote provider) drops the
        # constraint and the forgiving parser takes over.
        print(f"[Story {pid}] Grammar-constrained {pass_name} failed ({exc}); retrying free")
        kwargs.pop("json_schema", None)
        text = llm_service.generate_streaming(**kwargs)
    _capture_story_pass(pid, pass_name, model_id)
    return text or ""


_THINK_TAG_RE = re.compile(
    r"<(think|thinking|seed:think|reasoning|reflection)>.*?(</\1>|$)",
    re.DOTALL | re.IGNORECASE,
)


def _strip_model_noise(text: str) -> str:
    """Remove thinking tags and markdown fences from a raw completion."""
    text = _THINK_TAG_RE.sub("", text or "")
    text = re.sub(r"<\|channel>thought\n.*?(<channel\|>|$)", "", text, flags=re.DOTALL)
    text = re.sub(r"```[a-zA-Z]*\s*", "", text)
    return text.strip()


def _parse_json_object(text: str) -> Optional[dict]:
    """Best-effort dict out of a completion (grammar makes this the fast path)."""
    cleaned = _strip_model_noise(text)
    if not cleaned:
        return None
    candidates = [cleaned]
    braces = re.search(r"\{[\s\S]*\}", cleaned)
    if braces:
        candidates.append(braces.group())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    if json_repair is not None:
        try:
            parsed = json_repair.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _clean_prose(text: str) -> str:
    """Strip the wrappers models add around a chapter despite instructions."""
    prose = _strip_model_noise(text)
    lines = prose.splitlines()
    # A leading "Chapter 4: The Harbour" / "## The Harbour" heading.
    while lines and (
        re.match(r"^\s*#{1,6}\s", lines[0])
        or re.match(r"^\s*(chapter|kapitel)\s+\w+\s*[:.\-–]?\s*$", lines[0], re.IGNORECASE)
        or re.match(r"^\s*(chapter|kapitel)\s+\w+\s*[:.\-–]\s*.{0,80}$", lines[0], re.IGNORECASE)
    ):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def _wait_for_gpu(pid: str, poll_interval: float = 2.0) -> bool:
    """Block until the generation queue is drained. False if cancelled.

    Deliberately NOT director_pipeline._wait_for_gpu: that one reports
    progress into the Director's _pipelines dict and checks the Director's
    cancel flag, so called with a story id it would publish nothing and
    would never notice a story Stop. Only the job registry is reused, and
    read-only — launch.py assigns it on startup, so a None means "no jobs
    can be running yet".
    """
    _update_story(pid, progress=_progress(
        0, 1, "Waiting for GPU (generation queue)...",
    ))
    while True:
        if _is_cancelled(pid):
            return False
        try:
            from services import director_pipeline
            jobs = director_pipeline._jobs
        except Exception:
            jobs = None
        if not jobs:
            return True
        if not [j for j in jobs.values() if j.get("status") in ("queued", "running")]:
            return True
        time.sleep(poll_interval)


# ── Passes ─────────────────────────────────────────────────────────────────

def _pass_outline(pid: str) -> dict:
    """Pass 1 — premise + target length -> structured plan (low temperature)."""
    state = _snapshot(pid) or {}
    params = state.get("params") or {}
    premise = state.get("premise") or ""
    if _HAVE_SAFETY_SCAN:
        assert_no_minor_content(premise, "story premise")

    requested = params.get("chapter_count")
    count = int(requested) if requested else auto_chapter_count(params.get("min_pages"))
    total_words = total_target_words(params.get("min_pages"))
    per_chapter = chapter_target_words(params.get("min_pages"), count)

    user_prompt = "\n".join([
        "=== PREMISE ===",
        premise,
        "",
        "=== REQUIREMENTS ===",
        f"Genre: {params.get('genre') or 'unspecified'}",
        f"Tone: {params.get('tone') or 'unspecified'}",
        f"Point of view: {params.get('pov') or 'third person limited'}",
        f"Tense: {params.get('tense') or 'past'}",
        f"Target audience: {params.get('audience') or 'adult general readership'}",
        f"Minimum length: {params.get('min_pages') or 1} pages "
        f"(~{total_words} words, ~{WORDS_PER_PAGE} words per page)",
        (
            f"Chapter count: exactly {count}"
            if requested else
            f"Chapter count: you decide — {count} is the recommended number "
            f"for this length"
        ),
        f"Each chapter will be drafted to about {per_chapter} words, so give "
        "every chapter enough beats to fill that.",
        (
            f"Explicitness: {params.get('explicitness')}"
            if _is_nsfw(params) and params.get("explicitness") else ""
        ),
        "",
        "Produce the story blueprint as JSON.",
    ])

    model_id = _pick_model("story_outline", params.get("outline_model"))
    text = _run_llm(
        pid, "outline",
        model_id=model_id,
        system_prompt=_system_prompt("outline", "outline_explicit", params),
        user_prompt=user_prompt,
        stream_id=f"story-{pid}-outline",
        max_new_tokens=OUTLINE_MAX_TOKENS,
        temperature=OUTLINE_TEMPERATURE,
        json_schema=OUTLINE_SCHEMA,
    )
    outline = _parse_json_object(text)
    if not outline or not outline.get("chapters"):
        raise RuntimeError("The outline pass did not return a usable chapter plan.")
    if _HAVE_SAFETY_SCAN:
        assert_no_minor_content(json.dumps(outline, ensure_ascii=False), "story outline")

    chapters = [_new_chapter(i, plan) for i, plan in enumerate(outline["chapters"])]
    _update_story(
        pid,
        outline=outline,
        title=outline.get("title") or state.get("title") or "Untitled",
        chapters=chapters,
    )
    return outline


def _new_chapter(index: int, plan: dict) -> dict:
    return {
        "index": index,
        "title": (plan or {}).get("title") or f"Chapter {index + 1}",
        "beats": list((plan or {}).get("beats") or []),
        "text": "",
        "text_pre_edit": None,
        "word_count": 0,
        "status": "pending",
        "generated_at": None,
        "model_id": None,
        "edited": False,
        "instruction": None,
        "synopsis_at_start": "",
    }


def _pass_extend_outline(pid: str, additional: int) -> None:
    """Plan N more chapters and append them (extend_story)."""
    state = _snapshot(pid) or {}
    params = state.get("params") or {}
    outline = dict(state.get("outline") or {})
    chapters = list(state.get("chapters") or [])

    user_prompt = "\n".join([
        "=== EXISTING STORY BIBLE ===",
        _outline_block(outline),
        "",
        "=== WHAT HAS HAPPENED SO FAR ===",
        (state.get("synopsis_running") or "").strip() or "(nothing written yet)",
        "",
        "=== CURRENT CHARACTER STATE ===",
        _character_state_block(state.get("character_state") or {}) or "(unchanged)",
        "",
        "=== YOUR TASK ===",
        f"Plan {additional} additional chapter(s) that continue this story "
        f"from chapter {len(chapters)} onward. They must follow from what has "
        "already been written, resolve the open threads, and bring the story "
        "to a satisfying end. Same beat rules as before. JSON only.",
    ])

    model_id = _pick_model("story_outline", params.get("outline_model"))
    text = _run_llm(
        pid, f"outline-extend+{additional}",
        model_id=model_id,
        system_prompt=_system_prompt("outline", "outline_explicit", params),
        user_prompt=user_prompt,
        stream_id=f"story-{pid}-outline",
        max_new_tokens=OUTLINE_MAX_TOKENS,
        temperature=OUTLINE_TEMPERATURE,
        json_schema=EXTEND_OUTLINE_SCHEMA,
    )
    parsed = _parse_json_object(text) or {}
    new_plans = (parsed.get("chapters") or [])[:max(1, additional)]
    if not new_plans:
        raise RuntimeError("The extend pass did not return any new chapters.")

    start = len(chapters)
    chapters += [_new_chapter(start + i, plan) for i, plan in enumerate(new_plans)]
    outline["chapters"] = (outline.get("chapters") or []) + list(new_plans)
    _update_story(pid, outline=outline, chapters=chapters)


def _pass_chapter(pid: str, index: int, instruction: Optional[str] = None) -> str:
    """Pass 2 — write chapter `index` as prose (high temperature, no JSON)."""
    state = _snapshot(pid) or {}
    params = state.get("params") or {}
    chapters = [dict(c) for c in (state.get("chapters") or [])]
    if not 0 <= index < len(chapters):
        raise RuntimeError(f"Chapter {index} does not exist")

    target = chapter_target_words(params.get("min_pages"), len(chapters))
    chapters[index]["status"] = "writing"
    chapters[index]["synopsis_at_start"] = state.get("synopsis_running") or ""
    if instruction:
        chapters[index]["instruction"] = instruction
    _update_story(
        pid,
        chapters=chapters,
        progress=_writing_progress(
            chapters, f"Writing chapter {index + 1}/{len(chapters)}: "
                      f"{chapters[index].get('title', '')}",
        ),
    )

    model_id = _pick_model("story_prose", params.get("prose_model"))
    temperature = float(params.get("temperature") or DEFAULT_PROSE_TEMPERATURE)
    text = _run_llm(
        pid, f"chapter-{index + 1}",
        model_id=model_id,
        system_prompt=_system_prompt("chapter", "chapter_explicit", params),
        user_prompt=build_chapter_context(state, index, instruction),
        stream_id=f"story-{pid}-ch{index}",
        max_new_tokens=chapter_token_budget(target),
        temperature=temperature,
    )
    prose = _clean_prose(text)
    if not prose:
        raise RuntimeError(f"Chapter {index + 1} came back empty")
    if _HAVE_SAFETY_SCAN:
        assert_no_minor_content(prose, f"story chapter {index + 1}")

    # Re-read the live chapter list: a manual edit or an extend may have
    # changed it while this pass ran.
    live = [dict(c) for c in ((_snapshot(pid) or {}).get("chapters") or chapters)]
    if index < len(live):
        live[index].update({
            "text": prose,
            "word_count": _word_count(prose),
            # A Stop mid-chapter keeps the partial prose but does not claim
            # the chapter is finished.
            "status": "partial" if _is_cancelled(pid) else "done",
            "generated_at": time.time(),
            "model_id": model_id,
            "edited": False,
            "text_pre_edit": None,
        })
    # chapters is an artifact field, so this still lands after a Stop.
    _update_story(pid, chapters=live)
    return prose


def _pass_continuity(pid: str, index: int) -> None:
    """Pass 3 — refresh synopsis + character state from the written text."""
    state = _snapshot(pid) or {}
    params = state.get("params") or {}
    chapters = state.get("chapters") or []
    if not 0 <= index < len(chapters) or not (chapters[index].get("text") or "").strip():
        return

    _update_story(pid, progress=_writing_progress(
        chapters, f"Continuity check after chapter {index + 1}", sub=1,
    ))
    model_id = _pick_model("story_outline", params.get("outline_model"))
    text = _run_llm(
        pid, f"continuity-{index + 1}",
        model_id=model_id,
        system_prompt=_system_prompt("continuity", "continuity", params),
        user_prompt=_continuity_context(state, index),
        stream_id=f"story-{pid}-cont{index}",
        max_new_tokens=CONTINUITY_MAX_TOKENS,
        temperature=CONTINUITY_TEMPERATURE,
        json_schema=CONTINUITY_SCHEMA,
    )
    parsed = _parse_json_object(text)
    if not parsed:
        # Drift-correction is best-effort: a failed continuity pass leaves
        # the previous record in place rather than failing the story.
        print(f"[Story {pid}] Continuity pass {index + 1} returned no usable JSON")
        return
    character_state = {
        "characters": {
            c.get("name"): c.get("state")
            for c in (parsed.get("characters") or [])
            if c.get("name")
        },
        "open_threads": list(parsed.get("open_threads") or []),
    }
    _update_story(
        pid,
        synopsis_running=(parsed.get("synopsis") or "").strip()
        or state.get("synopsis_running") or "",
        character_state=character_state,
        synopsis_stale=False,
    )


def _refresh_synopsis_if_stale(pid: str) -> None:
    """Rebuild the record from scratch after a manual edit.

    A user edit invalidates the synopsis (§2.4), and the honest rebuild is
    to re-read every written chapter in order from an empty record.
    # ponytail: O(chapters) low-temperature LLM calls, once per edit session.
    # If that ever hurts, store a per-chapter synopsis snapshot and replay
    # only from the edited chapter (synopsis_at_start already exists).
    """
    state = _snapshot(pid) or {}
    if not state.get("synopsis_stale"):
        return
    written = [
        c["index"] for c in (state.get("chapters") or [])
        if (c.get("text") or "").strip()
    ]
    if not written:
        _update_story(pid, synopsis_stale=False)
        return
    print(f"[Story {pid}] Synopsis is stale — rebuilding over {len(written)} chapters")
    _update_story(pid, synopsis_running="", character_state={})
    for index in written:
        if _is_cancelled(pid):
            return
        _pass_continuity(pid, index)
    _update_story(pid, synopsis_stale=False)


# ── Worker ─────────────────────────────────────────────────────────────────

def _pending_chapter_indices(pid: str) -> list[int]:
    chapters = (_snapshot(pid) or {}).get("chapters") or []
    return [c["index"] for c in chapters if c.get("status") not in ("done",)]


def _finish(pid: str, status: str, message: str, error: Optional[str] = None) -> None:
    chapters = (_snapshot(pid) or {}).get("chapters") or []
    _update_story(
        pid,
        status=status,
        phase=status,
        error=error,
        completed_at=time.time(),
        progress=_writing_progress(chapters, message),
    )


def _run_story(pid: str, job: dict) -> None:
    """The story worker. One per story, non-daemon (survives a disconnect)."""
    llm_service = None
    try:
        from services import llm_service as _llm
        llm_service = _llm
        # Keep the model warm between chapters; restored in the finally.
        llm_service.set_idle_timeout(STORY_IDLE_TIMEOUT_S)
    except Exception as exc:
        print(f"[Story {pid}] Could not set the LLM idle timeout: {exc}")

    try:
        if not _wait_for_gpu(pid):
            return
        if _is_cancelled(pid):
            return

        kind = job.get("kind", "new")
        # Before anything reads the record: a manual edit since the last run
        # invalidated it. No-op on a fresh story (nothing written yet).
        _update_story(pid, status="writing", phase="writing")
        _refresh_synopsis_if_stale(pid)
        if _is_cancelled(pid):
            return

        if kind == "extend":
            _update_story(pid, status="planning", phase="planning",
                          progress=_progress(0, 1, "Planning new chapters..."))
            _pass_extend_outline(pid, int(job.get("count") or 1))
            _save_story_state(pid)
        elif not ((_snapshot(pid) or {}).get("outline") or {}).get("chapters"):
            _update_story(pid, status="planning", phase="planning",
                          progress=_progress(0, 1, "Planning the story..."))
            _pass_outline(pid)
            _save_story_state(pid)

        if _is_cancelled(pid):
            return
        _update_story(pid, status="writing", phase="writing")

        indices = list(job.get("indices") or _pending_chapter_indices(pid))
        for index in indices:
            if _is_cancelled(pid):
                return
            _pass_chapter(pid, index, job.get("instruction"))
            if _is_cancelled(pid):
                return
            _pass_continuity(pid, index)
            _save_story_state(pid)

        # A regenerated middle chapter invalidates the record built from its
        # old text; replay continuity over the chapters that follow it.
        if kind == "regenerate" and indices:
            for index in range(max(indices) + 1, len(
                (_snapshot(pid) or {}).get("chapters") or [],
            )):
                if _is_cancelled(pid):
                    return
                _pass_continuity(pid, index)
            _save_story_state(pid)

        if _is_cancelled(pid):
            return
        state = _snapshot(pid) or {}
        words = sum(int(c.get("word_count") or 0) for c in state.get("chapters") or [])
        _finish(pid, "completed", f"Story complete — {words} words")

    except SafetyViolationError as exc:
        print(f"[Story {pid}] Safety violation: {exc}")
        _finish(pid, "failed", "Blocked by the content safety scan", error=str(exc))
    except Exception as exc:
        print(f"[Story {pid}] Failed: {exc}")
        traceback.print_exc()
        _finish(pid, "failed", "Story generation failed", error=str(exc))
    finally:
        if llm_service is not None:
            try:
                llm_service.set_idle_timeout(None)
            except Exception:
                pass
        _update_story(pid, _active_stream_id=None)
        _save_story_state(pid)
        with _story_lock:
            if _story_threads.get(pid) is threading.current_thread():
                _story_threads.pop(pid, None)


def _start_worker(pid: str, job: dict) -> None:
    """Start and track a story worker until its ``finally`` completes."""
    thread = threading.Thread(
        target=_run_story, args=(pid, job), daemon=False,
    )
    with _story_lock:
        if pid in _story_threads:
            raise RuntimeError(f"Story {pid} already has a worker")
        _story_threads[pid] = thread
    try:
        thread.start()
    except BaseException as exc:
        with _story_lock:
            if _story_threads.get(pid) is thread:
                _story_threads.pop(pid, None)
            state = _stories.get(pid)
            if state and state.get("status") not in ("completed", "failed", "cancelled"):
                state.update({
                    "status": "failed",
                    "phase": "failed",
                    "error": f"Could not start story worker: {exc}",
                    "completed_at": time.time(),
                    "progress": _progress(0, 0, "Could not start story worker"),
                })
        _save_story_state(pid)
        raise


# ── Params ─────────────────────────────────────────────────────────────────

def _normalized_params(raw: dict) -> dict:
    """The knobs the passes read, defaulted. `_params_snapshot` keeps the rest."""
    chapter_count = raw.get("chapter_count")
    if isinstance(chapter_count, str):
        chapter_count = None if chapter_count.strip().lower() in ("", "auto") \
            else int(chapter_count)
    return {
        "genre": raw.get("genre") or "",
        "tone": raw.get("tone") or "",
        "pov": raw.get("pov") or "third person limited",
        "tense": raw.get("tense") or "past",
        "audience": raw.get("audience") or "adult general readership",
        "min_pages": int(raw.get("min_pages") or 20),
        # None means "let the model decide" — auto_chapter_count() answers.
        "chapter_count": int(chapter_count) if chapter_count else None,
        "explicitness": raw.get("explicitness") or "none",
        "nsfw": bool(raw.get("nsfw")),
        "temperature": float(raw.get("temperature") or DEFAULT_PROSE_TEMPERATURE),
        "top_p": float(raw.get("top_p") or 0.95),
        "outline_model": raw.get("outline_model") or None,
        "prose_model": raw.get("prose_model") or None,
    }


# ── Public API ─────────────────────────────────────────────────────────────

def start_story(params: dict, out_dir: str,
                ensure_model: Optional[Callable[[str], None]] = None) -> str:
    """Start a new story run. Returns the story id.

    Args:
        params: the request dict — premise, min_pages, genre, tone, pov,
            tense, audience, chapter_count|None, explicitness, nsfw,
            temperature, top_p, outline_model, prose_model. Kept verbatim
            in `_params_snapshot` so "new story with these settings" and
            "show me the exact prompt used" are reads, not new plumbing.
        out_dir: workspace output dir; the state file and exports land here.
        ensure_model: callback that loads a model id (launch._ensure_llm_loaded).
            Injected so the worker never imports launch.py.
    """
    pid = uuid.uuid4().hex[:8]
    normalized = _normalized_params(params)
    state = {
        "version": STORY_STATE_VERSION,
        "story_id": pid,
        "created_at": time.time(),
        "completed_at": None,
        "status": "queued",
        "phase": "queued",
        "title": params.get("title") or "",
        "premise": params.get("premise") or "",
        "params": normalized,
        "outline": {},
        "chapters": [],
        "synopsis_running": "",
        "synopsis_stale": False,
        "character_state": {},
        "llm_passes": [],
        "output_files": [],
        "progress": _progress(0, 0, "Queued..."),
        "error": None,
        "total_time_sec": None,
        "_params_snapshot": dict(params),
        # Runtime-only (never persisted).
        "out_dir": out_dir,
        "_ensure_model": ensure_model,
        "_active_stream_id": None,
    }
    with _story_lock:
        _stories[pid] = state
    _save_story_state(pid)
    _start_worker(pid, {"kind": "new"})
    return pid


def get_story(pid: str) -> Optional[dict]:
    """Live in-memory state — same shape as the JSON on disk.

    Runtime-only keys (out_dir, the ensure_model callback, the active stream
    id) are dropped by going through the persisted-field projection, so the
    endpoint can return this verbatim.
    """
    with _story_lock:
        state = _stories.get(pid)
        return _persisted_snapshot(state) if state else None


def stop_story(pid: str) -> bool:
    """User Stop. Absorbing: nothing may flip the story back afterwards."""
    with _story_lock:
        state = _stories.get(pid)
        if not state or state.get("status") in ("completed", "failed", "cancelled"):
            return False
        state.update({
            "status": "cancelled",
            "phase": "cancelled",
            "completed_at": time.time(),
            "progress": _progress(0, 0, "Cancelled"),
        })
        stream_id = state.get("_active_stream_id")
    # Stop the in-flight pass within a token instead of at the next chapter.
    if stream_id:
        try:
            from services import llm_service
            llm_service.cancel_stream(stream_id)
        except Exception:
            pass
    _save_story_state(pid)
    return True


def delete_story(out_dir: str, pid: str) -> dict:
    """Delete a saved story and the exports it produced."""
    with _story_lock:
        if (
            pid in _story_threads
            or pid in _story_starting
            or pid in _story_operations
            or pid in _story_deleting
            or _stories.get(pid, {}).get("status") in _ACTIVE_STORY_STATUSES
        ):
            return {"ok": False, "error": "running"}
        _story_deleting.add(pid)
    try:
        with _story_file_lock:
            filepath = _find_story_file(out_dir, pid)
            if not filepath:
                return {"ok": False, "error": "not_found"}
            story_dir = os.path.dirname(filepath)
            removed = []
            try:
                with open(filepath, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
            except Exception:
                state = {}
            for name in state.get("output_files") or []:
                # Basenames only — never follow a path out of the workspace.
                if not name or os.path.basename(name) != name:
                    continue
                target = os.path.join(story_dir, name)
                try:
                    if os.path.isfile(target):
                        os.remove(target)
                        removed.append(name)
                except OSError:
                    pass
            os.remove(filepath)
        with _story_lock:
            _stories.pop(pid, None)
        return {"ok": True, "deleted_files": removed}
    finally:
        with _story_lock:
            _story_deleting.discard(pid)


def _rehydrate(pid: str, out_dir: str,
               ensure_model: Optional[Callable[[str], None]]) -> tuple[bool, str]:
    """Put a saved story back in memory so a worker can continue it."""
    saved = load_story(out_dir, pid)
    if not saved:
        return False, "No saved state found for this story."
    filepath = _find_story_file(out_dir, pid)
    with _story_lock:
        existing = _stories.get(pid) or {}
        state = {k: saved.get(k) for k in _STORY_PERSISTED_FIELDS}
        state["story_id"] = pid
        state["version"] = saved.get("version") or STORY_STATE_VERSION
        state["created_at"] = saved.get("created_at") or time.time()
        state["chapters"] = [dict(c) for c in (saved.get("chapters") or [])]
        state["llm_passes"] = list(saved.get("llm_passes") or [])
        state["output_files"] = list(saved.get("output_files") or [])
        state["params"] = saved.get("params") or {}
        state["out_dir"] = os.path.dirname(filepath) if filepath else out_dir
        state["_ensure_model"] = ensure_model or existing.get("_ensure_model")
        state["_active_stream_id"] = None
        state["error"] = None
        state["completed_at"] = None
        _stories[pid] = state
    return True, "ok"


def _start_continuation(pid: str, job: dict, out_dir: Optional[str],
                        ensure_model: Optional[Callable[[str], None]],
                        message: str) -> tuple[bool, str]:
    """Shared reservation + rehydrate + worker start for regen / extend."""
    with _story_lock:
        state = _stories.get(pid)
        if (
            pid in _story_threads
            or pid in _story_starting
            or pid in _story_operations
            or pid in _story_deleting
            or (state and state.get("status") in _ACTIVE_STORY_STATUSES)
        ):
            return False, "Story is still active; try again shortly."
        resolved_out_dir = out_dir or (state or {}).get("out_dir")
        _story_starting.add(pid)
    try:
        if not resolved_out_dir:
            return False, "No output directory for this story."
        ok, why = _rehydrate(pid, resolved_out_dir, ensure_model)
        if not ok:
            return False, why
        _update_story(pid, status="queued", phase="queued", error=None,
                      completed_at=None, progress=_progress(0, 1, message))
        _save_story_state(pid)
        _start_worker(pid, job)
        return True, "started"
    finally:
        with _story_lock:
            _story_starting.discard(pid)


def regenerate_chapter(pid: str, index: int, instruction: Optional[str] = None,
                       out_dir: Optional[str] = None,
                       ensure_model: Optional[Callable[[str], None]] = None,
                       ) -> tuple[bool, str]:
    """Rewrite one chapter, optionally with an instruction ("darker").

    The continuity record is replayed over the chapters that follow, so a
    regenerated middle chapter doesn't leave the synopsis describing the
    version it replaced.
    """
    index = int(index)
    # Validate against the saved state BEFORE a worker exists, so a bad
    # index is a plain error instead of a story that starts and stops.
    known = get_story(pid) or load_story(out_dir or "", pid) or {}
    if known and not 0 <= index < len(known.get("chapters") or []):
        return False, f"Chapter {index + 1} does not exist."
    return _start_continuation(
        pid,
        {"kind": "regenerate", "indices": [index], "instruction": instruction},
        out_dir, ensure_model, f"Rewriting chapter {index + 1}...",
    )


def extend_story(pid: str, additional_chapters: int,
                 out_dir: Optional[str] = None,
                 ensure_model: Optional[Callable[[str], None]] = None,
                 ) -> tuple[bool, str]:
    """Plan and write N more chapters, continuing from the current synopsis."""
    count = max(1, int(additional_chapters or 1))
    return _start_continuation(
        pid, {"kind": "extend", "count": count}, out_dir, ensure_model,
        f"Extending the story by {count} chapter(s)...",
    )


@_exclusive_story_operation
def update_chapter_text(out_dir: str, pid: str, index: int, text: str) -> bool:
    """Save a manual edit and mark the synopsis stale.

    It's the user's text, so it replaces the generated prose — the model's
    version is kept in `text_pre_edit` (first edit only). The stale flag
    makes the next run rebuild the running synopsis from what is actually
    on the page (`_refresh_synopsis_if_stale`).
    """
    def updater(state: dict) -> None:
        chapters = state.get("chapters") or []
        if not 0 <= index < len(chapters):
            raise IndexError(f"Chapter {index} does not exist")
        chapter = chapters[index]
        if chapter.get("text_pre_edit") is None:
            chapter["text_pre_edit"] = chapter.get("text") or ""
        chapter["text"] = text or ""
        chapter["word_count"] = _word_count(text)
        chapter["edited"] = True
        chapter["status"] = "done" if (text or "").strip() else "pending"
        state["synopsis_stale"] = True

    saved = _update_saved_story(out_dir, pid, updater)
    if saved is None:
        return False
    with _story_lock:
        live = _stories.get(pid)
        if live is not None:
            live["chapters"] = [dict(c) for c in (saved.get("chapters") or [])]
            live["synopsis_stale"] = True
    return True


def export_story(out_dir: str, pid: str, fmt: str = "md") -> str:
    """Write the story as .md or .txt into the workspace, return the path.

    Writing it into out_dir is what makes it show up as a text output
    (§1.3) and what "Create audiobook" later reads.
    """
    fmt = (fmt or "md").lower().lstrip(".")
    if fmt not in ("md", "txt"):
        raise ValueError(f"Unsupported export format: {fmt}")
    state = load_story(out_dir, pid) or get_story(pid)
    if not state:
        raise FileNotFoundError(f"No story {pid}")
    content = format_story(state, fmt)
    filename = f"story_{_safe_slug(state.get('title') or '')}_{pid}.{fmt}"
    filepath = _find_story_file(out_dir, pid)
    story_dir = os.path.dirname(filepath) if filepath else out_dir
    os.makedirs(story_dir, exist_ok=True)
    target = os.path.join(story_dir, filename)
    with _story_file_lock:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)

        def updater(saved: dict) -> None:
            files = saved.setdefault("output_files", [])
            if filename not in files:
                files.append(filename)

        _update_saved_story(out_dir, pid, updater)
    with _story_lock:
        live = _stories.get(pid)
        if live is not None and filename not in (live.get("output_files") or []):
            live.setdefault("output_files", []).append(filename)
    return target


# ── Self-check ─────────────────────────────────────────────────────────────
# Covers the pure logic and one full fake-LLM run: `python -m
# services.story_pipeline` from app/. No CUDA, no model, no network.

def _self_check() -> None:
    import shutil
    import sys
    import tempfile
    import types

    # 1. Length maths.
    assert total_target_words(20) == 20 * WORDS_PER_PAGE
    assert auto_chapter_count(100) == round(100 * WORDS_PER_PAGE / AUTO_WORDS_PER_CHAPTER)
    assert auto_chapter_count(1) == 3, "never fewer than 3 chapters"
    assert chapter_target_words(40, 10) == 40 * WORDS_PER_PAGE // 10 == 1100
    assert chapter_target_words(4, 20) == MIN_CHAPTER_WORDS, "clamped up"
    assert chapter_target_words(2000, 2) == MAX_CHAPTER_WORDS, "clamped down"
    assert chapter_token_budget(1100) == 1100 * 2 + 512
    assert chapter_token_budget(MAX_CHAPTER_WORDS) <= MAX_CHAPTER_TOKENS
    assert chapter_target_words(40, 0) > 0 and chapter_target_words(None, None) > 0

    # 2. Context window: bounded tail, bible + synopsis present, old prose gone.
    state = {
        # 12 pages / 3 chapters -> 1100 words per chapter.
        "params": {"min_pages": 12, "genre": "noir", "pov": "first person"},
        "outline": {
            "title": "The Salt Line",
            "logline": "A diver hunts her brother's killer.",
            "setting": "A drowned harbour town.",
            "characters": [{"name": "Mara", "role": "protagonist",
                            "description": "Diver, thirty, scar on her jaw."}],
            "chapters": [{"title": "Low Tide"}, {"title": "The Wreck"},
                         {"title": "Salt"}],
        },
        "synopsis_running": "Mara found the wreck and the missing canvas.",
        "character_state": {"characters": {"Mara": "aboard the Kestrel, armed"},
                            "open_threads": ["the canvas is still missing"]},
        "chapters": [
            {"index": 0, "title": "Low Tide", "beats": ["b"],
             "text": "ANCIENTMARKER " + "alpha " * 3000, "status": "done"},
            {"index": 1, "title": "The Wreck", "beats": ["b"],
             "text": "beta " * 3000 + "TAILMARKER", "status": "done"},
            {"index": 2, "title": "Salt", "beats": ["Mara confronts the buyer",
                                                    "She loses the canvas"],
             "text": "", "status": "pending"},
        ],
    }
    context = build_chapter_context(state, 2, instruction="colder, less dialogue")
    assert "The Salt Line" in context and "Mara" in context, "bible in context"
    assert "Mara found the wreck" in context, "running synopsis in context"
    assert "aboard the Kestrel" in context, "character state in context"
    assert "the canvas is still missing" in context, "open threads in context"
    assert "TAILMARKER" in context, "tail of the previous prose in context"
    assert "ANCIENTMARKER" not in context, "chapter 1 must NOT be in context"
    assert "colder, less dialogue" in context, "instruction in context"
    assert "about 1100 words" in context, "word target handed to the model"
    assert "Mara confronts the buyer" in context, "beats in context"
    tail_only = _tail_words("w " * 5000)
    assert _word_count(tail_only) == CONTEXT_TAIL_WORDS, "tail is bounded"
    assert _word_count(context) < 2600, f"context stays flat, got {_word_count(context)}"
    # Growing the story must not grow the context.
    grown = dict(state)
    grown["chapters"] = [dict(c) for c in state["chapters"]]
    grown["chapters"][0]["text"] = "gamma " * 40000
    assert abs(_word_count(build_chapter_context(grown, 2)) - _word_count(
        build_chapter_context(state, 2))) < 50, "context is length-independent"

    # 3. Absorbing cancel semantics.
    pid = "selftest1"
    with _story_lock:
        _stories[pid] = {"story_id": pid, "status": "writing", "chapters": [],
                         "progress": _progress(0, 1, "x")}
    assert _update_story(pid, status="planning") is True
    with _story_lock:
        _stories[pid]["status"] = "cancelled"
    assert _update_story(pid, status="completed") is False, "cancel absorbs completion"
    assert _update_story(pid, status="failed", error="boom") is False
    assert _update_story(pid, progress=_progress(1, 1, "done")) is False
    assert _stories[pid]["status"] == "cancelled"
    assert _update_story(pid, chapters=[{"index": 0}]) is True, "late artifact lands"
    assert _update_story(pid, output_files=["a.md"]) is True
    assert _stories[pid]["chapters"] == [{"index": 0}]
    with _story_lock:
        _stories.pop(pid, None)

    # 4. Export formatting.
    export_state = {
        "title": "The Salt Line",
        "outline": {"logline": "A diver hunts her brother's killer."},
        "chapters": [
            {"index": 0, "title": "Low Tide", "text": "The tide went out."},
            {"index": 1, "title": "The Wreck", "text": "She dove."},
            {"index": 2, "title": "Salt", "text": ""},
        ],
    }
    md = format_story(export_state, "md")
    assert md.startswith("# The Salt Line\n"), md[:40]
    assert "## 1. Low Tide" in md and "## 2. The Wreck" in md
    assert "Salt" not in md.split("## 2.")[1], "empty chapters are skipped"
    assert "*A diver hunts" in md and md.endswith("\n")
    txt = format_story(export_state, "txt")
    assert txt.startswith("THE SALT LINE") and "CHAPTER 1: LOW TIDE" in txt
    assert "#" not in txt, "txt carries no markdown"
    assert _safe_slug("The Salt Line!") == "the_salt_line"
    assert _safe_slug("") == "story"

    # 5. Prose cleanup + JSON parsing.
    assert _clean_prose("<think>hm</think>\n## Chapter 3: Salt\n\nShe dove.") == "She dove."
    assert _clean_prose("Chapter 3\n\nShe dove.") == "She dove."
    assert _parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json_object('noise {"a": 1} trailing') == {"a": 1}
    assert _parse_json_object("not json at all") in (None, {})

    # 6. Fake-LLM end-to-end: persistence roundtrip, passes, export, delete.
    fake = types.ModuleType("services.llm_service")
    fake.MODEL_REGISTRY = {"fake/outliner": {"gguf_file": "o.gguf"},
                           "fake/writer": {"gguf_file": "w.gguf"}}
    fake._last_system_prompt = ""
    fake._last_user_prompt = ""
    fake._stream_buffer = ""
    fake._last_thinking_text = None
    fake.calls = []

    def _models_for_use_case(use_case):
        return ["fake/outliner"] if use_case == "story_outline" else ["fake/writer"]

    def _generate_streaming(**kwargs):
        fake.calls.append(kwargs)
        fake._last_system_prompt = kwargs.get("system_prompt", "")
        fake._last_user_prompt = kwargs.get("prompt", "")
        schema = kwargs.get("json_schema") or {}
        props = (schema.get("properties") or {})
        if "logline" in props:
            out = {"title": "Fake Story", "logline": "A test.",
                   "setting": "A lab",
                   "characters": [{"name": "Ada", "description": "A tester."}],
                   "chapters": [{"title": "One", "beats": ["a", "b"]},
                                {"title": "Two", "beats": ["c", "d"]}]}
        elif "chapters" in props:
            out = {"chapters": [{"title": "Three", "beats": ["e", "f"]}]}
        elif "synopsis" in props:
            out = {"synopsis": "Ada tested things.",
                   "characters": [{"name": "Ada", "state": "in the lab"}],
                   "open_threads": ["the build is red"]}
        else:
            fake._stream_buffer = "## Chapter\n\n" + "word " * 120
            return fake._stream_buffer
        fake._stream_buffer = json.dumps(out)
        return fake._stream_buffer

    fake.models_for_use_case = _models_for_use_case
    fake.generate_streaming = _generate_streaming
    fake.set_idle_timeout = lambda seconds=None: fake.__dict__.setdefault(
        "idle", []).append(seconds)
    fake.cancel_stream = lambda stream_id: False
    fake.get_model_dir = lambda: tempfile.gettempdir()
    import services as _services_pkg
    sys.modules["services.llm_service"] = fake
    _services_pkg.llm_service = fake

    workdir = tempfile.mkdtemp(prefix="storycheck_")
    loaded_models: list[str] = []
    try:
        request = {"premise": "A tester writes a test.", "min_pages": 8,
                   "chapter_count": 2, "genre": "comedy", "nsfw": False,
                   "explicitness": "graphic", "temperature": 0.9}
        story_id = start_story(request, workdir, loaded_models.append)
        thread = _story_threads.get(story_id)
        if thread:
            thread.join(timeout=30)
        live = get_story(story_id)
        assert live["status"] == "completed", live.get("error")
        assert len(live["chapters"]) == 2
        assert all(c["status"] == "done" and c["word_count"] == 120
                   for c in live["chapters"])
        assert live["title"] == "Fake Story"
        assert live["synopsis_running"] == "Ada tested things."
        assert live["character_state"]["characters"] == {"Ada": "in the lab"}
        assert live["character_state"]["open_threads"] == ["the build is red"]
        assert "_ensure_model" not in live and "out_dir" not in live
        assert loaded_models == ["fake/outliner", "fake/writer", "fake/outliner",
                                 "fake/writer", "fake/outliner"], loaded_models
        # Idle timeout raised for the run, restored afterwards.
        assert fake.__dict__["idle"] == [STORY_IDLE_TIMEOUT_S, None]
        # Stream ids are unique per pass and namespaced away from the Director.
        ids = [c["stream_id"] for c in fake.calls]
        assert ids[0] == f"story-{story_id}-outline"
        assert f"story-{story_id}-ch0" in ids and f"story-{story_id}-ch1" in ids
        assert len(set(ids)) == len(ids), ids
        # Prose is high-temperature and unconstrained; the bookkeeping
        # passes are low-temperature and schema-constrained.
        prose_calls = [c for c in fake.calls if c.get("json_schema") is None]
        assert len(prose_calls) == 2
        assert all(c["temperature"] == 0.9 for c in prose_calls)
        assert all(c["max_new_tokens"] == chapter_token_budget(1100)
                   for c in prose_calls)
        assert fake.calls[0]["temperature"] == OUTLINE_TEMPERATURE
        # nsfw=False -> the tame guide path, no explicit block.
        assert "ADULT CONTENT" not in prose_calls[0]["system_prompt"]
        assert len(live["llm_passes"]) == 5, live["llm_passes"]
        assert live["llm_passes"][0]["pass"] == "outline"

        # Persistence roundtrip.
        saved = load_story(workdir, story_id)
        assert saved["version"] == STORY_STATE_VERSION
        assert saved["story_id"] == story_id
        assert saved["_params_snapshot"] == request, "full original request kept"
        assert saved["status"] == "completed" and saved["total_time_sec"] >= 0
        assert set(saved) <= set(_STORY_PERSISTED_FIELDS), set(saved)
        assert "out_dir" not in saved and "_ensure_model" not in saved
        assert [c["text"] for c in saved["chapters"]] == \
               [c["text"] for c in live["chapters"]]
        assert os.path.isfile(
            os.path.join(workdir, f"{_STORY_FILE_PREFIX}{story_id}.json"))
        summaries = list_stories(workdir)
        assert [s["id"] for s in summaries] == [story_id]
        assert summaries[0]["chapters_done"] == 2
        assert summaries[0]["word_count"] == 240

        # Manual edit -> stale synopsis, pre-edit text preserved.
        assert update_chapter_text(workdir, story_id, 0, "My own words.") is True
        edited = load_story(workdir, story_id)
        assert edited["synopsis_stale"] is True
        assert edited["chapters"][0]["text"] == "My own words."
        assert edited["chapters"][0]["edited"] is True
        assert edited["chapters"][0]["text_pre_edit"].startswith("word")
        assert edited["chapters"][0]["word_count"] == 3

        # Export lands in the workspace and is registered as an output.
        md_path = export_story(workdir, story_id, "md")
        assert os.path.isfile(md_path) and md_path.endswith(".md")
        with open(md_path, encoding="utf-8") as handle:
            assert "My own words." in handle.read()
        txt_path = export_story(workdir, story_id, "txt")
        assert os.path.isfile(txt_path)
        exported = load_story(workdir, story_id)
        assert sorted(exported["output_files"]) == sorted(
            [os.path.basename(md_path), os.path.basename(txt_path)])

        # Extend: plans + writes one more chapter, rebuilding the stale record.
        ok, why = extend_story(story_id, 1, out_dir=workdir,
                               ensure_model=loaded_models.append)
        assert ok, why
        thread = _story_threads.get(story_id)
        if thread:
            thread.join(timeout=30)
        extended = get_story(story_id)
        assert extended["status"] == "completed", extended.get("error")
        assert len(extended["chapters"]) == 3
        assert extended["chapters"][2]["title"] == "Three"
        assert extended["chapters"][2]["status"] == "done"
        assert extended["synopsis_stale"] is False, "stale flag cleared by rebuild"
        assert extended["chapters"][0]["text"] == "My own words.", "edit survives"

        # Regenerate one chapter with an instruction.
        ok, why = regenerate_chapter(story_id, 1, instruction="darker",
                                     out_dir=workdir,
                                     ensure_model=loaded_models.append)
        assert ok, why
        thread = _story_threads.get(story_id)
        if thread:
            thread.join(timeout=30)
        regenerated = get_story(story_id)
        assert regenerated["status"] == "completed", regenerated.get("error")
        assert regenerated["chapters"][1]["instruction"] == "darker"
        assert "darker" in fake.calls[-3]["prompt"] or any(
            "darker" in c.get("prompt", "") for c in fake.calls)
        assert not any_story_active()
        ok, why = regenerate_chapter(story_id, 99, out_dir=workdir)
        assert not ok and "does not exist" in why, why
        assert not any_story_active(), "a rejected regen leaves no worker"

        # Progress shape is exactly what PipelinePlaceholder reads.
        assert set(regenerated["progress"]) == {
            "current", "total", "message", "step", "total_steps"}

        # Delete removes the state file and the exports.
        assert delete_story(workdir, story_id)["ok"] is True
        assert load_story(workdir, story_id) is None
        assert not os.path.isfile(md_path) and not os.path.isfile(txt_path)
        assert list_stories(workdir) == []
    finally:
        with _story_lock:
            _stories.clear()
            _story_threads.clear()
        sys.modules.pop("services.llm_service", None)
        if getattr(_services_pkg, "llm_service", None) is fake:
            del _services_pkg.llm_service
        shutil.rmtree(workdir, ignore_errors=True)

    # 7. Guides exist and are real content, not placeholders.
    for name in ("outline", "chapter", "continuity", "outline_explicit",
                 "chapter_explicit"):
        text = _guide(name)
        assert len(text) > 500, f"guide {name} is missing or too short"
    assert "ADULT" in _guide("chapter_explicit")
    assert _system_prompt("chapter", "chapter_explicit", {"nsfw": True}) != \
        _system_prompt("chapter", "chapter_explicit", {"nsfw": False})
    assert "ADULT CONTENT" in _system_prompt(
        "chapter", "chapter_explicit", {"nsfw": True})

    print("story_pipeline self-check: OK")


if __name__ == "__main__":
    _self_check()
