"""AudioBook render worker — the only module in the package that *does* things.

PLAN §3.5.  Everything else in ``services/audiobook`` is pure planning; this is
where a plan becomes audio:

    plan_chapter()  →  N child generations  →  ffmpeg mix  →  chapter file
    N chapter files →  concat + FFMETADATA  →  M4B with chapter markers

Wiring follows ``services/director_pipeline.py`` exactly rather than inventing a
second mechanism: module globals set by ``init()`` from ``launch.py``, a child
generation submitted as a normal job dict plus a thread running
``_run_generation``, and the parent job polled to a terminal status.  Nothing
here imports ``launch``, so the whole file is importable — and self-checkable —
without the generation engine.

Three caches, on purpose:

  * **Chapter cache** — ``model.chapter_is_cached``.  A replay of an unchanged
    chapter is a file existence check, not a render.  This is why hitting play
    twice is instant.
  * **Run cache** — one file per ``(run_id, hash(plan.params))`` in
    ``{out_dir}/_audiobook_runs_{pid}/``.  Edit one paragraph and only that
    paragraph is re-voiced; the other 27 runs are reused.  The key is the whole
    generation request, so a changed text, seed, voice clip or temperature is a
    miss while a changed chapter title is not.
    The cache is the *set of files itself* — deliberately not a table in the
    project JSON: ``chapter_content_hash`` hashes ``project.render_settings``,
    so a run cache stored there would invalidate every chapter's audio on every
    render.  File names are derived, not remembered.
  * **Book cache** — falls out of the chapter cache; a book export re-mixes
    only the chapters that changed.

Cancellation never leaves a half-finished mix: it is checked between runs and
while ffmpeg runs, in-flight child jobs are cancelled, and a partially written
output file is deleted instead of published.

Self-check: ``python -m services.audiobook.render`` from ``app/`` — stubs the
child generation, ffprobe and ffmpeg, so it needs neither a GPU nor ffmpeg.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from typing import Any, Optional

from services.audiobook import mix, model, store, tts
from services.job_lifecycle import (
    TERMINAL_STATUSES,
    finish_job,
    is_cancel_requested,
    record_job_outputs,
    request_cancel,
    snapshot_job,
    try_start,
    update_job,
)

# ── Dependency injection (same shape as director_pipeline.init) ─────────────

_jobs: dict = None              # reference to launch._jobs
_run_generation = None          # reference to launch._run_generation
_workspace_dir = None           # reference to launch._workspace_dir
_active_gen_states = None       # reference to launch._active_gen_states


def init(jobs_dict, run_generation_fn, workspace_dir_fn, active_gen_states=None):
    """Called by launch.py to wire up shared references.

    ``active_gen_states`` is optional but strongly recommended: without it a
    cancellation marks a running child job cancelled without interrupting the
    model, so the GPU keeps working on audio nobody will use.
    """
    global _jobs, _run_generation, _workspace_dir, _active_gen_states
    _jobs = jobs_dict
    _run_generation = run_generation_fn
    _workspace_dir = workspace_dir_fn
    _active_gen_states = active_gen_states


# How long to let a cancelled child settle before giving up on its thread.
_SETTLE_GRACE_S = 10.0
_CHILD_POLL_S = 0.2
_FFMPEG_POLL_S = 0.5

_AUDIO_EXTENSIONS = frozenset({
    ".wav", ".mp3", ".flac", ".m4a", ".m4b", ".aac", ".ogg", ".opus",
})

# Voicing owns the first 90 % of a chapter render, the mix the rest.  For a
# book, chapters own 85 % and the concat/encode pass the rest.
_VOICING_SPAN = 90
_CHAPTER_SPAN = 85


class RenderError(RuntimeError):
    """The render cannot continue (bad params, failed child job, ffmpeg error)."""


class RenderCancelled(RuntimeError):
    """The parent job was cancelled; nothing may be published."""


# ── ffmpeg / ffprobe ───────────────────────────────────────────────────────


_BINARY_ENV = {"ffmpeg": "FFMPEG_BINARY", "ffprobe": "FFPROBE_BINARY"}


def _binary(name: str) -> str:
    """Resolve ffmpeg/ffprobe the way the rest of the app does.

    ``shared/ffmpeg_setup.py`` exports FFMPEG_BINARY/FFPROBE_BINARY for the
    bundled builds; PATH is the fallback.  Four lines instead of importing
    ``shared.utils.video_decode``, which pulls in torch.
    """
    configured = os.environ.get(_BINARY_ENV.get(name, ""), "")
    if configured and os.path.isfile(configured):
        return configured
    return shutil.which(name) or name


def probe_duration(path: str) -> Optional[float]:
    """Exact duration of an audio file in seconds, or ``None``.

    ``None`` on every failure mode — missing file, missing ffprobe, non-zero
    exit, unparseable or non-positive output.  The mix positions every element
    by absolute offset, so a *guessed* duration would silently desynchronise
    the whole chapter; the caller must treat ``None`` as an error, not a zero.
    """
    if not path or not os.path.isfile(path):
        return None
    command = [
        _binary("ffprobe"), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1",
        path,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            encoding="utf-8", errors="ignore", check=False, timeout=60,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        seconds = float((result.stdout or "").strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


# ffmpeg's per-frame status lines are noise; the diagnosis is in the lines
# around them.
_FFMPEG_NOISE = ("frame=", "size=", "time=", "video:", "audio:", "Press [q]")


def ffmpeg_error_summary(stderr: str, *, max_lines: int = 5, max_chars: int = 600) -> str:
    """The last few *useful* stderr lines — never the whole log.

    A failing filtergraph produces hundreds of banner/progress lines and one
    real message at the end.  Putting the whole thing in ``job["error"]`` makes
    it unreadable in the UI and unloggable in the state file.
    """
    lines = [one.strip() for one in (stderr or "").splitlines() if one.strip()]
    useful = [one for one in lines if not one.startswith(_FFMPEG_NOISE)]
    tail = (useful or lines)[-max_lines:]
    return " | ".join(tail)[-max_chars:] or "ffmpeg failed without any output"


def _run_ffmpeg(args: list, *, job: Optional[dict] = None) -> tuple[int, str]:
    """Run an ffmpeg argv, draining stderr, abortable by job cancellation.

    stderr is read by a helper thread: a filtergraph this size fills the OS
    pipe buffer, and a plain ``wait()`` on a full pipe deadlocks.
    """
    command = [_binary("ffmpeg")] + [str(one) for one in list(args)[1:]]
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore",
        )
    except OSError as exc:
        return 1, f"could not start ffmpeg: {exc}"

    collected: list[str] = []

    def _drain() -> None:
        try:
            collected.append(process.stderr.read() or "")
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    killed = False
    while process.poll() is None:
        if job is not None and is_cancel_requested(job) and not killed:
            killed = True
            try:
                process.terminate()
            except OSError:
                pass
        time.sleep(_FFMPEG_POLL_S)
    reader.join(timeout=5.0)
    return process.returncode, "".join(collected)


# ── Names ──────────────────────────────────────────────────────────────────


# Everything Windows forbids in a filename, plus control characters.
_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_SPACE_RE = re.compile(r"\s+")


def safe_filename(*parts: str, fallback: str = "audiobook", max_length: int = 90) -> str:
    """Filesystem-safe stem from title fragments, joined with ``-``.

    Non-ASCII is kept (a German chapter title should stay readable); only the
    characters that actually break a path are replaced.  Trailing dots and
    spaces are stripped because Windows silently drops them, which would make
    the file the UI looks for and the file on disk two different names.
    """
    cleaned: list[str] = []
    for part in parts:
        text = _SPACE_RE.sub(" ", _UNSAFE_RE.sub(" ", str(part or ""))).strip()
        text = text.replace(" ", "_").strip("._-")
        if text:
            cleaned.append(text)
    stem = "-".join(cleaned)[:max_length].strip("._- ")
    return stem or fallback


def chapter_stem(project: model.Project, chapter: model.Chapter) -> str:
    """``{project}-{chapter}`` stem, numbered when titles would collide."""
    index = next(
        (number for number, one in enumerate(project.chapters, 1) if one.id == chapter.id),
        1,
    )
    title = chapter.title or f"Chapter {index}"
    same_title = sum(
        1 for one in project.chapters if (one.title or "") == (chapter.title or "")
    )
    if same_title > 1:
        title = f"{index:02d} {title}"
    return safe_filename(project.title, title, fallback=f"audiobook-{index:02d}")


# ── Run cache ──────────────────────────────────────────────────────────────


def runs_dir_for(out_dir: str, project_id: str) -> str:
    """Per-project directory for the cached run fragments.

    Leading underscore: ``_list_workspaces`` skips ``_``-prefixed directories,
    so the fragments never show up as a phantom workspace, and being a
    subdirectory keeps them out of the gallery.
    """
    return os.path.join(out_dir, f"_audiobook_runs_{project_id}")


def run_cache_key(plan: tts.TtsPlan) -> str:
    """Cache key for one planned run: a hash of the whole generation request.

    Hashing ``plan.params`` rather than ``(text, seed)`` means every input that
    changes the audio invalidates the cache — reference clip, temperature,
    emotion tag, model — and nothing else does.  ``workspace`` is excluded: it
    decides *where* the file lives, not how it sounds.
    """
    payload = {key: value for key, value in plan.params.items() if key != "workspace"}
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def find_cached_run(runs_dir: str, run_id: str, key: str) -> Optional[str]:
    """Existing audio for this exact run+request, or ``None``.

    The extension is whatever the TTS handler produced, hence the glob.
    """
    pattern = os.path.join(glob.escape(runs_dir), f"{run_id}-{key}.*")
    for path in sorted(glob.glob(pattern)):
        if os.path.splitext(path)[1].lower() in _AUDIO_EXTENSIONS and os.path.isfile(path):
            return path
    return None


# ── Progress ───────────────────────────────────────────────────────────────


def scaled_progress(done: int, total: int, *, span: int = _VOICING_SPAN, base: int = 0) -> int:
    """``done`` of ``total`` mapped onto ``base .. base+span`` percent.

    Clamped at both ends and safe for ``total <= 0`` (nothing to do is done).
    """
    if total <= 0:
        return base + span
    fraction = max(0, min(int(done), int(total))) / float(total)
    return base + int(round(span * fraction))


# ── Child generation ───────────────────────────────────────────────────────


def _cancel_child(child_id: str, child: dict) -> None:
    """Cancel one in-flight child generation, interrupting the model if wired."""
    request_cancel(child, job_id=child_id, active_states=_active_gen_states or {})


def _child_audio_path(child: dict, out_dir: str) -> Optional[str]:
    """The audio file a finished child produced.

    ``output_files`` holds bare filenames relative to the job's ``out_dir``
    (see ``job_lifecycle.collect_job_outputs``); absolute values are tolerated.
    """
    for name in snapshot_job(child).get("output_files") or []:
        if not isinstance(name, str) or not name:
            continue
        path = name if os.path.isabs(name) else os.path.join(out_dir, name)
        if os.path.splitext(path)[1].lower() in _AUDIO_EXTENSIONS and os.path.isfile(path):
            return path
    return None


def _wait_for_child(
    parent: dict, child_id: str, child: dict, thread: threading.Thread,
    *, timeout: Optional[float],
) -> str:
    """Block until the child reaches a terminal status.  Returns that status.

    Raises ``RenderCancelled`` if the parent was cancelled while waiting (the
    child is cancelled first), ``RenderError`` on timeout.
    """
    deadline = (time.time() + float(timeout)) if timeout else None
    while True:
        status = snapshot_job(child).get("status")
        if status in TERMINAL_STATUSES:
            thread.join(timeout=_SETTLE_GRACE_S)
            return str(status)
        if is_cancel_requested(parent):
            _cancel_child(child_id, child)
            thread.join(timeout=_SETTLE_GRACE_S)
            raise RenderCancelled("Cancelled while voicing.")
        if deadline is not None and time.time() > deadline:
            _cancel_child(child_id, child)
            thread.join(timeout=_SETTLE_GRACE_S)
            raise RenderError(f"Voicing timed out after {timeout:.0f}s.")
        time.sleep(_CHILD_POLL_S)


def voice_run(
    parent: dict, plan: tts.TtsPlan, runs_dir: str, workspace: Optional[str],
    *, force: bool = False, timeout: Optional[float] = None,
) -> tuple[str, float, bool]:
    """Produce audio for one run.  Returns ``(path, duration, from_cache)``.

    A cached file whose duration cannot be probed is treated as a miss and
    deleted — a truncated leftover from a killed process must not silently
    become part of a mix.
    """
    key = run_cache_key(plan)
    if not force:
        cached = find_cached_run(runs_dir, plan.run_id, key)
        if cached:
            duration = probe_duration(cached)
            if duration:
                return cached, duration, True
            try:
                os.remove(cached)
            except OSError:
                pass

    if _jobs is None or _run_generation is None:
        raise RenderError(
            "Render worker is not initialised; launch.py must call "
            "services.audiobook.render.init() first."
        )

    os.makedirs(runs_dir, exist_ok=True)
    params = dict(plan.params)
    params["workspace"] = workspace
    child_id = f"ab{uuid.uuid4().hex[:8]}"
    child = {
        "id": child_id,
        "status": "queued",
        "progress": 0,
        "step": 0,
        "total_steps": 0,
        "phase": "",
        "message": "Queued (audiobook voicing)",
        "created_at": time.time(),
        "params": params,
        "output_files": [],
        "error": None,
        "workspace": workspace,
        # Generate straight into the run cache directory so no fragment ever
        # lands in the gallery.
        "out_dir": runs_dir,
    }
    _jobs[child_id] = child
    # Non-daemon, like every other generation: it must survive a disconnect.
    thread = threading.Thread(target=_run_generation, args=(child_id,), daemon=False)
    thread.start()

    status = _wait_for_child(parent, child_id, child, thread, timeout=timeout)
    if status == "cancelled":
        raise RenderCancelled("Voicing was cancelled.")
    if status != "completed":
        raise RenderError(
            snapshot_job(child).get("error")
            or f"Voicing run {plan.run_id} failed."
        )

    produced = _child_audio_path(child, runs_dir)
    if not produced:
        raise RenderError(
            f"Voicing run {plan.run_id} produced no audio file."
        )
    target = os.path.join(
        runs_dir, f"{plan.run_id}-{key}{os.path.splitext(produced)[1].lower()}",
    )
    if os.path.normcase(os.path.abspath(produced)) != os.path.normcase(os.path.abspath(target)):
        try:
            os.replace(produced, target)
        except OSError as exc:
            raise RenderError(f"Could not store run audio: {exc}") from exc
    duration = probe_duration(target)
    if not duration:
        raise RenderError(
            f"Could not determine the duration of {os.path.basename(target)} "
            "(is ffprobe available?)."
        )
    return target, duration, False


# ── Chapter render ─────────────────────────────────────────────────────────


def _timeline_document(
    project: model.Project, chapter: model.Chapter, plan: mix.MixPlan, audio_path: str,
) -> dict:
    """Karaoke map: run offsets plus their text, next to the audio file.

    The mix already positions every run absolutely, so highlighting a run is a
    lookup; the text comes along so the UI can subdivide it into words without
    fetching the project.
    """
    texts = {
        run.id: run.text for _block, run in model.iter_speech_runs(chapter)
    }
    return {
        "version": 1,
        "project_id": project.id,
        "project_title": project.title,
        "chapter_id": chapter.id,
        "chapter_title": chapter.title,
        "audio_file": os.path.basename(audio_path),
        "duration": round(plan.duration, 4),
        "speech_intervals": [
            [round(start, 4), round(end, 4)] for start, end in plan.speech_intervals
        ],
        "runs": [
            {
                "run_id": one.run_id,
                "block_id": one.block_id,
                "start": round(one.start, 4),
                "end": round(one.end, 4),
                "text": texts.get(one.run_id, ""),
            }
            for one in plan.timeline if one.kind == "speech"
        ],
        "timeline": [one.to_dict() for one in plan.timeline],
    }


def _render_chapter(
    parent: dict,
    *,
    out_dir: str,
    workspace: Optional[str],
    project_id: str,
    chapter_id: str,
    fmt: str,
    force: bool,
    run_timeout: Optional[float],
    progress_base: int = 0,
    progress_span: int = _VOICING_SPAN,
    label: str = "",
) -> dict:
    """Render one chapter and record it on the project.  Shared by both workers.

    Loads the project itself so a book render always sees the chapter audio a
    previous iteration recorded.  Raises ``RenderError`` / ``RenderCancelled``.
    """
    project = store.load_project(out_dir, project_id)
    if project is None:
        raise RenderError(f"AudioBook project '{project_id}' not found.")
    chapter = project.chapter(chapter_id)
    if chapter is None:
        raise RenderError(f"Chapter '{chapter_id}' not found in this project.")

    prefix = f"{label} " if label else ""
    content_hash = model.chapter_content_hash(project, chapter)
    stem = chapter_stem(project, chapter)
    output_path = os.path.join(out_dir, f"{stem}.{fmt}")
    timeline_path = os.path.join(out_dir, f"{stem}.timeline.json")

    # 1. Chapter cache — the reason replaying an unchanged chapter is instant.
    if not force and model.chapter_is_cached(project, chapter):
        cached = chapter.audio_path or ""
        if not os.path.isabs(cached):
            cached = os.path.join(out_dir, cached)
        if os.path.isfile(cached) and cached.lower().endswith(f".{fmt}"):
            duration = chapter.audio_duration or probe_duration(cached) or 0.0
            existing_timeline = os.path.splitext(cached)[0] + ".timeline.json"
            update_job(
                parent,
                message=f"{prefix}Using the cached render",
                progress=progress_base + progress_span,
            )
            return {
                "chapter_id": chapter.id,
                "chapter_title": chapter.title,
                "audio_path": cached,
                "duration": float(duration),
                "timeline_path": existing_timeline if os.path.isfile(existing_timeline) else None,
                "cached": True,
                "warnings": [],
                "runs": 0,
                "reused_runs": 0,
            }

    # 2. Plan every run.  A run without a usable voice is a hard stop: half a
    #    chapter of narration with a silent paragraph is worse than an error
    #    that names the paragraph.
    plans, errors = tts.plan_chapter(project, chapter, workspace=workspace)
    if errors:
        raise RenderError(
            f"{len(errors)} run(s) cannot be voiced — " + "; ".join(errors[:10])
        )

    total_steps = len(plans) + 1
    update_job(
        parent,
        phase="Voicing",
        step=0,
        total_steps=total_steps,
        progress=progress_base,
        message=f"{prefix}Voicing {len(plans)} run(s)",
    )

    # 3. Voice each run (cache first).
    runs_dir = runs_dir_for(out_dir, project.id)
    rendered: dict[str, dict] = {}
    warnings: list[str] = []
    reused = 0
    voicing_span = max(1, int(progress_span * 0.9))
    for number, plan in enumerate(plans, start=1):
        if is_cancel_requested(parent):
            raise RenderCancelled("Cancelled before voicing finished.")
        update_job(
            parent,
            phase="Voicing",
            step=number - 1,
            total_steps=total_steps,
            progress=scaled_progress(
                number - 1, len(plans), span=voicing_span, base=progress_base,
            ),
            message=f"{prefix}Voicing run {number}/{len(plans)}",
        )
        path, duration, from_cache = voice_run(
            parent, plan, runs_dir, workspace, force=force, timeout=run_timeout,
        )
        rendered[plan.run_id] = {"path": path, "duration": duration}
        reused += 1 if from_cache else 0
        warnings.extend(plan.warnings)

    if is_cancel_requested(parent):
        raise RenderCancelled("Cancelled before the mix started.")

    # 4. Mix.
    update_job(
        parent,
        phase="Mixing",
        step=len(plans),
        total_steps=total_steps,
        progress=progress_base + voicing_span,
        message=f"{prefix}Mixing the chapter",
    )
    options = mix.MixOptions.from_dict({**(project.render_settings or {}), "fmt": fmt})
    try:
        plan = mix.plan_chapter_mix(project, chapter, rendered, output_path, options)
    except mix.MixPlanError as exc:
        raise RenderError(str(exc)) from exc
    warnings.extend(plan.warnings)

    os.makedirs(out_dir, exist_ok=True)
    code, stderr = _run_ffmpeg(plan.args, job=parent)
    if is_cancel_requested(parent):
        _discard(output_path)
        raise RenderCancelled("Cancelled during the mix.")
    if code != 0 or not os.path.isfile(output_path):
        _discard(output_path)
        raise RenderError(f"ffmpeg failed ({code}): {ffmpeg_error_summary(stderr)}")

    duration = probe_duration(output_path) or plan.duration

    # 5. Karaoke sidecar next to the audio.
    try:
        with open(timeline_path, "w", encoding="utf-8") as handle:
            json.dump(
                _timeline_document(project, chapter, plan, output_path),
                handle, ensure_ascii=False, indent=2, default=str,
            )
    except OSError as exc:
        warnings.append(f"Could not write the karaoke timeline: {exc}")
        timeline_path = None

    # 6. Record on the project — the only allowed mutation path.
    def _record(current: model.Project) -> None:
        target = current.chapter(chapter_id)
        if target is None:
            return
        target.audio_path = output_path
        target.audio_hash = content_hash
        target.audio_duration = float(duration)

    store.update_project(out_dir, project_id, _record)

    return {
        "chapter_id": chapter.id,
        "chapter_title": chapter.title,
        "audio_path": output_path,
        "duration": float(duration),
        "timeline_path": timeline_path,
        "cached": False,
        "warnings": warnings,
        "runs": len(plans),
        "reused_runs": reused,
    }


def _discard(path: Optional[str]) -> None:
    """Remove a partial output.  A half-written mix must never be published."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


# ── Job params ─────────────────────────────────────────────────────────────


def _job_context(job: dict) -> tuple[dict, str, Optional[str]]:
    """``(params, out_dir, workspace)`` from a job dict, params winning."""
    params = dict(job.get("params") or {})
    workspace = params.get("workspace") or job.get("workspace")
    out_dir = params.get("out_dir") or job.get("out_dir")
    if not out_dir:
        out_dir = _workspace_dir(workspace) if _workspace_dir else "outputs"
    return params, os.path.abspath(out_dir), workspace


def _validate_format(value: Any, default: str) -> str:
    fmt = str(value or default).lower().lstrip(".")
    if fmt not in mix.EXPORT_FORMATS:
        raise RenderError(
            f"Unsupported format '{fmt}'. Supported: {', '.join(mix.EXPORT_FORMATS)}"
        )
    return fmt


def _timeout(value: Any) -> Optional[float]:
    """``run_timeout`` in seconds; absent/0 means wait (cancellation is the exit).

    No default deadline on purpose: a run can sit behind a two-hour video job
    in the GPU queue, and a timeout that fires there would discard good work.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


# ── Workers ────────────────────────────────────────────────────────────────


def render_chapter_job(job_id: str) -> bool:
    """Background worker: render one chapter to a single audio file.

    Job params: ``project_id``, ``chapter_id``, ``format`` (default ``wav``),
    ``force``, ``out_dir``/``workspace``, ``run_timeout``.

    Deliberately *not* holding the generation lock: the child generations take
    it one at a time, and a parent holding it would deadlock against its own
    children.
    """
    job = _jobs[job_id]
    started = time.time()
    if not try_start(job, message="Preparing render...", phase="Preparing"):
        return False
    try:
        params, out_dir, workspace = _job_context(job)
        project_id = params.get("project_id") or params.get("pid")
        chapter_id = params.get("chapter_id")
        if not project_id or not chapter_id:
            raise RenderError("project_id and chapter_id are required.")
        fmt = _validate_format(params.get("format"), "wav")

        result = _render_chapter(
            job,
            out_dir=out_dir,
            workspace=workspace,
            project_id=str(project_id),
            chapter_id=str(chapter_id),
            fmt=fmt,
            force=bool(params.get("force")),
            run_timeout=_timeout(params.get("run_timeout")),
        )
        record_job_outputs(job, [os.path.basename(result["audio_path"])])
        completed = finish_job(
            job, "completed", progress=100, phase="", message="Done",
            result={
                "kind": "chapter",
                "project_id": project_id,
                "chapter_id": result["chapter_id"],
                "chapter_title": result["chapter_title"],
                "audio_path": result["audio_path"],
                "audio_file": os.path.basename(result["audio_path"]),
                "timeline_path": result["timeline_path"],
                "timeline_file": (
                    os.path.basename(result["timeline_path"])
                    if result["timeline_path"] else None
                ),
                "duration": result["duration"],
                "cached": result["cached"],
                "runs": result["runs"],
                "reused_runs": result["reused_runs"],
                "warnings": result["warnings"],
                "elapsed": round(time.time() - started, 2),
            },
        )
        print(
            f"[AudioBook/render] {result['chapter_title'] or result['chapter_id']} -> "
            f"{os.path.basename(result['audio_path'])} "
            f"({result['duration']:.1f}s, {result['reused_runs']}/{result['runs']} runs cached"
            f"{', chapter cache hit' if result['cached'] else ''})"
        )
        return completed
    except RenderCancelled:
        request_cancel(job)
        return False
    except RenderError as exc:
        finish_job(job, "failed", error=str(exc), message=f"Error: {exc}")
        return False
    except Exception as exc:                      # noqa: BLE001 - worker boundary
        traceback.print_exc()
        finish_job(job, "failed", error=str(exc), message=f"Error: {exc}")
        return False


def render_book_job(job_id: str) -> bool:
    """Background worker: render every chapter, then concatenate the book.

    Job params: ``project_id``, ``format`` (default ``m4b`` — the only target
    with chapter markers), ``chapter_format`` (default ``wav``, the lossless
    intermediate), ``force``, ``out_dir``/``workspace``, ``run_timeout``,
    ``author``.

    Chapters go through the exact same path as a single-chapter render, so an
    already-rendered chapter costs a file existence check.
    """
    job = _jobs[job_id]
    started = time.time()
    if not try_start(job, message="Preparing book render...", phase="Preparing"):
        return False
    concat_path = metadata_path = None
    try:
        params, out_dir, workspace = _job_context(job)
        project_id = params.get("project_id") or params.get("pid")
        if not project_id:
            raise RenderError("project_id is required.")
        fmt = _validate_format(params.get("format"), "m4b")
        chapter_fmt = _validate_format(params.get("chapter_format"), "wav")
        force = bool(params.get("force"))
        run_timeout = _timeout(params.get("run_timeout"))

        project = store.load_project(out_dir, str(project_id))
        if project is None:
            raise RenderError(f"AudioBook project '{project_id}' not found.")

        # Chapters with neither speech nor a sound effect have nothing to mix;
        # skipping them beats failing the whole book on an empty placeholder.
        renderable = [
            one for one in project.chapters
            if list(model.iter_speech_runs(one))
            or any(block.type == model.BLOCK_SFX for block in one.blocks)
        ]
        skipped = [one.title or one.id for one in project.chapters if one not in renderable]
        if not renderable:
            raise RenderError("This book has no chapter with any content to render.")

        update_job(
            job, phase="Chapters", step=0, total_steps=len(renderable) + 1,
            progress=0, message=f"Rendering {len(renderable)} chapter(s)",
        )

        chapter_files: list[tuple[str, str, float]] = []
        warnings: list[str] = [f"Chapter '{one}' is empty and was skipped." for one in skipped]
        for number, chapter in enumerate(renderable, start=1):
            if is_cancel_requested(job):
                raise RenderCancelled("Cancelled between chapters.")
            base = scaled_progress(
                number - 1, len(renderable), span=_CHAPTER_SPAN, base=0,
            )
            span = max(
                1,
                scaled_progress(number, len(renderable), span=_CHAPTER_SPAN, base=0) - base,
            )
            result = _render_chapter(
                job,
                out_dir=out_dir,
                workspace=workspace,
                project_id=str(project_id),
                chapter_id=chapter.id,
                fmt=chapter_fmt,
                force=force,
                run_timeout=run_timeout,
                progress_base=base,
                progress_span=span,
                label=f"Chapter {number}/{len(renderable)}:",
            )
            chapter_files.append(
                (result["audio_path"], chapter.title or f"Chapter {number}", result["duration"])
            )
            warnings.extend(result["warnings"])

        if is_cancel_requested(job):
            raise RenderCancelled("Cancelled before the book was assembled.")

        # Assemble: concat list + FFMETADATA are ours to write, the plan is pure.
        stem = safe_filename(project.title, fallback=f"audiobook-{project.id}")
        output_path = os.path.join(out_dir, f"{stem}.{fmt}")
        concat_path = os.path.join(out_dir, f"_audiobook_concat_{project.id}.txt")
        metadata_path = os.path.join(out_dir, f"_audiobook_meta_{project.id}.txt")
        options = mix.MixOptions.from_dict({**(project.render_settings or {}), "fmt": fmt})
        try:
            book = mix.build_book_plan(
                chapter_files, output_path, concat_path, metadata_path, options,
                title=project.title,
                author=str(
                    params.get("author")
                    or project.params_snapshot.get("author")
                    or ""
                ),
            )
        except mix.MixPlanError as exc:
            raise RenderError(str(exc)) from exc

        update_job(
            job, phase="Assembling", step=len(renderable),
            total_steps=len(renderable) + 1, progress=_CHAPTER_SPAN,
            message=f"Assembling {len(chapter_files)} chapter(s) into one file",
        )
        with open(concat_path, "w", encoding="utf-8") as handle:
            handle.write(book.concat_list)
        with open(metadata_path, "w", encoding="utf-8") as handle:
            handle.write(book.metadata)

        code, stderr = _run_ffmpeg(book.args, job=job)
        if is_cancel_requested(job):
            _discard(output_path)
            raise RenderCancelled("Cancelled while assembling the book.")
        if code != 0 or not os.path.isfile(output_path):
            _discard(output_path)
            raise RenderError(f"ffmpeg failed ({code}): {ffmpeg_error_summary(stderr)}")

        duration = probe_duration(output_path) or book.duration
        record_job_outputs(job, [os.path.basename(output_path)])
        completed = finish_job(
            job, "completed", progress=100, phase="", message="Done",
            result={
                "kind": "book",
                "project_id": project.id,
                "audio_path": output_path,
                "audio_file": os.path.basename(output_path),
                "duration": float(duration),
                "format": fmt,
                "chapters": [
                    {
                        "title": title,
                        "audio_path": path,
                        "duration": chapter_duration,
                        "start": start,
                    }
                    for (path, title, chapter_duration), start
                    in zip(chapter_files, book.chapter_starts)
                ],
                "warnings": warnings,
                "elapsed": round(time.time() - started, 2),
            },
        )
        print(
            f"[AudioBook/render] book '{project.title}' -> "
            f"{os.path.basename(output_path)} "
            f"({duration:.1f}s, {len(chapter_files)} chapters)"
        )
        return completed
    except RenderCancelled:
        request_cancel(job)
        return False
    except RenderError as exc:
        finish_job(job, "failed", error=str(exc), message=f"Error: {exc}")
        return False
    except Exception as exc:                      # noqa: BLE001 - worker boundary
        traceback.print_exc()
        finish_job(job, "failed", error=str(exc), message=f"Error: {exc}")
        return False
    finally:
        for path in (concat_path, metadata_path):
            _discard(path)


if __name__ == "__main__":
    # Self-check: naming, cache keys/hits, progress arithmetic, ffprobe and
    # ffmpeg failure handling, and the two workers end to end with the child
    # generation, ffprobe and ffmpeg stubbed out.
    # `python -m services.audiobook.render` from app/.
    import tempfile

    from services.audiobook.model import (
        Block, Chapter, MusicAsset, Project, Run, VoiceProfile,
    )

    # ── 1. Filename normalisation ---------------------------------------
    assert safe_filename("My Book", "Chapter 1") == "My_Book-Chapter_1"
    assert safe_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert safe_filename("  spaced   out  ") == "spaced_out"
    assert safe_filename("Kapitel Ärger", "Größe") == "Kapitel_Ärger-Größe"
    assert safe_filename("", None, fallback="fb") == "fb"
    assert safe_filename("...", fallback="fb") == "fb"
    assert safe_filename("trailing dots...") == "trailing_dots"
    assert safe_filename("x" * 200).__len__() == 90
    assert safe_filename("a\x00b\nc") == "a_b_c"
    # Duplicate chapter titles get numbered so they cannot overwrite each other.
    dup_project = Project(
        id="dp", title="Book",
        chapters=[Chapter(id="k1", title="Teil"), Chapter(id="k2", title="Teil"),
                  Chapter(id="k3", title="")],
    )
    assert chapter_stem(dup_project, dup_project.chapters[0]) == "Book-01_Teil"
    assert chapter_stem(dup_project, dup_project.chapters[1]) == "Book-02_Teil"
    assert chapter_stem(dup_project, dup_project.chapters[2]) == "Book-Chapter_3"

    # ── 2. Run cache key ------------------------------------------------
    def _plan(run_id="r1", text="Hallo", seed=42, **extra):
        params = {"model_type": "index_tts2", "prompt": text, "seed": seed}
        params.update(extra)
        return tts.TtsPlan(run_id=run_id, model_type="index_tts2", params=params, seed=seed)

    base_key = run_cache_key(_plan())
    assert base_key == run_cache_key(_plan()), "key must be deterministic"
    assert len(base_key) == 16
    # Same seed but different text is a MISS — the seed does not depend on text.
    assert run_cache_key(_plan(text="Hallo!")) != base_key
    assert run_cache_key(_plan(seed=43)) != base_key
    assert run_cache_key(_plan(temperature=0.9)) != base_key
    # The workspace decides where, not how it sounds.
    assert run_cache_key(_plan(workspace="ws1")) == base_key
    assert run_cache_key(_plan(workspace="ws2")) == base_key

    # ── 3. Run cache hit / miss on disk ---------------------------------
    cache_root = tempfile.mkdtemp(prefix="audiobook_render_cache_")
    hit_path = os.path.join(cache_root, f"r1-{base_key}.wav")
    assert find_cached_run(cache_root, "r1", base_key) is None, "empty dir = miss"
    with open(hit_path, "wb") as handle:
        handle.write(b"RIFF")
    assert find_cached_run(cache_root, "r1", base_key) == hit_path, "same key = hit"
    assert find_cached_run(cache_root, "r1", run_cache_key(_plan(seed=43))) is None, \
        "changed seed = miss"
    assert find_cached_run(cache_root, "r2", base_key) is None, "other run = miss"
    os.remove(hit_path)
    assert find_cached_run(cache_root, "r1", base_key) is None, "deleted file = miss"
    # A non-audio sidecar with the same stem is not mistaken for the audio.
    with open(os.path.join(cache_root, f"r1-{base_key}.meta.json"), "w") as handle:
        handle.write("{}")
    assert find_cached_run(cache_root, "r1", base_key) is None
    assert runs_dir_for("/out/ws", "pid1").endswith("_audiobook_runs_pid1"), \
        "run dir must be _-prefixed so it is not listed as a workspace"

    # ── 4. Progress arithmetic ------------------------------------------
    assert scaled_progress(0, 28) == 0
    assert scaled_progress(14, 28) == 45
    assert scaled_progress(28, 28) == 90
    assert scaled_progress(3, 0) == 90, "nothing to do is done"
    assert scaled_progress(99, 3) == 90, "clamped high"
    assert scaled_progress(-5, 3) == 0, "clamped low"
    assert scaled_progress(1, 2, span=10, base=90) == 95
    assert scaled_progress(2, 2, span=10, base=90) == 100
    assert scaled_progress(0, 4, span=_CHAPTER_SPAN) == 0
    assert scaled_progress(4, 4, span=_CHAPTER_SPAN) == 85

    # ── 5. ffmpeg error summary -----------------------------------------
    log = (
        "ffmpeg version 6.0\n  built with gcc\nframe= 100 size= 2kB time=00:00:01\n"
        "size=      12kB time=00:00:02\n"
        "[Parsed_volume_0] Invalid expression\nError initializing filter 'volume'\n"
    )
    summary = ffmpeg_error_summary(log)
    assert "Invalid expression" in summary and "Error initializing filter" in summary
    assert "frame=" not in summary and "size=" not in summary, summary
    assert len(ffmpeg_error_summary("x\n" * 500)) <= 600
    assert ffmpeg_error_summary("") == "ffmpeg failed without any output"
    assert ffmpeg_error_summary("   \n\n") == "ffmpeg failed without any output"
    # Nothing but progress noise still yields something to show the user.
    assert ffmpeg_error_summary("frame= 1 size= 2kB\n") == "frame= 1 size= 2kB"

    # ── 6. probe_duration failure modes ---------------------------------
    assert probe_duration("") is None
    assert probe_duration(os.path.join(cache_root, "does-not-exist.wav")) is None
    probe_target = os.path.join(cache_root, "probe.wav")
    with open(probe_target, "wb") as handle:
        handle.write(b"RIFF")

    class _Result:
        def __init__(self, returncode=0, stdout=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, ""

    _real_run = subprocess.run
    try:
        subprocess.run = lambda *a, **k: _Result(0, "12.5\n")
        assert probe_duration(probe_target) == 12.5
        subprocess.run = lambda *a, **k: _Result(1, "12.5\n")
        assert probe_duration(probe_target) is None, "non-zero exit = unknown"
        subprocess.run = lambda *a, **k: _Result(0, "N/A\n")
        assert probe_duration(probe_target) is None, "unparseable = unknown"
        subprocess.run = lambda *a, **k: _Result(0, "0\n")
        assert probe_duration(probe_target) is None, "zero length = unknown"
        subprocess.run = lambda *a, **k: _Result(0, "")
        assert probe_duration(probe_target) is None

        def _boom(*a, **k):
            raise OSError("ffprobe not found")

        subprocess.run = _boom
        assert probe_duration(probe_target) is None, "missing ffprobe = unknown"
    finally:
        subprocess.run = _real_run
    shutil.rmtree(cache_root, ignore_errors=True)

    # ── 7. Workers end to end, with the engine stubbed ------------------
    root = tempfile.mkdtemp(prefix="audiobook_render_selfcheck_")
    fake_jobs: dict = {}
    generated: list[str] = []
    ffmpeg_calls: list[list] = []
    captured_metadata: list[str] = []
    cancel_after: list[int] = [0]        # 0 = never

    def fake_run_generation(child_id: str) -> None:
        """Stand-in for launch._run_generation: writes one wav, completes."""
        child = fake_jobs[child_id]
        generated.append(child_id)
        assert child["params"].get("workspace") == "default", child["params"]
        assert child["params"].get("prompt"), "a run must carry its text"
        if not try_start(child, message="Generating"):
            return
        os.makedirs(child["out_dir"], exist_ok=True)
        name = f"tts_{len(generated)}.wav"
        with open(os.path.join(child["out_dir"], name), "wb") as handle:
            handle.write(b"RIFF")
        record_job_outputs(child, [name])
        finish_job(child, "completed", progress=100, message="Done")
        if cancel_after[0] and len(generated) >= cancel_after[0]:
            request_cancel(fake_jobs["parent"])

    def fake_ffmpeg(args, *, job=None):
        ffmpeg_calls.append(list(args))
        argv = [str(one) for one in args]
        if "-map_chapters" in argv:
            with open(argv[argv.index("-i", argv.index("-i") + 1) + 1], encoding="utf-8") as h:
                captured_metadata.append(h.read())
        if job is not None and is_cancel_requested(job):
            return 0, ""
        with open(argv[-1], "wb") as handle:
            handle.write(b"\0" * 16)
        return 0, ""

    init(fake_jobs, fake_run_generation, lambda ws=None: root, active_gen_states={})
    _real_ffmpeg, _real_probe = _run_ffmpeg, probe_duration
    _run_ffmpeg = fake_ffmpeg                                       # noqa: F811
    probe_duration = lambda path: 1.5 if os.path.isfile(path) else None  # noqa: E731,F811

    def new_parent(**params) -> str:
        job_id = f"parent{len(fake_jobs)}"
        fake_jobs[job_id] = fake_jobs["parent"] = {
            "id": job_id, "status": "queued", "progress": 0, "step": 0,
            "total_steps": 0, "phase": "", "message": "Queued",
            "created_at": time.time(), "params": params, "output_files": [],
            "error": None, "workspace": "default", "out_dir": root,
        }
        return job_id

    try:
        narrator = VoiceProfile(
            id="v1", name="Narrator", model_type="index_tts2",
            voice_ref_path=os.path.join(root, "narrator.wav"),
        )
        project = store.create_project(
            root, "Mein Buch", language="de", voice_profiles=[narrator],
            chapters=[
                Chapter(id="c1", title="Kapitel Eins", blocks=[
                    Block(id="b1", runs=[Run(id="r1", text="Erster Satz.", profile_id="v1")]),
                    Block(id="b2", runs=[Run(id="r2", text="Zweiter Satz.", profile_id="v1")]),
                ]),
                Chapter(id="c2", title="Kapitel Zwei", blocks=[
                    Block(id="b3", runs=[Run(id="r3", text="Dritter Satz.", profile_id="v1")]),
                ]),
                Chapter(id="c3", title="Leer"),
            ],
        )

        # 7a. First render: both runs voiced, mix run, sidecar written.
        job_id = new_parent(project_id=project.id, chapter_id="c1", format="wav")
        assert render_chapter_job(job_id) is True, fake_jobs[job_id]
        job = fake_jobs[job_id]
        assert job["status"] == "completed" and job["progress"] == 100
        assert len(generated) == 2, generated
        result = job["result"]
        assert os.path.isfile(result["audio_path"]), result
        assert result["audio_file"] == "Mein_Buch-Kapitel_Eins.wav", result["audio_file"]
        assert job["output_files"] == ["Mein_Buch-Kapitel_Eins.wav"], job["output_files"]
        assert result["runs"] == 2 and result["reused_runs"] == 0
        assert result["cached"] is False
        assert len(ffmpeg_calls) == 1

        # 7b. The karaoke sidecar carries run offsets AND their text.
        with open(result["timeline_path"], encoding="utf-8") as handle:
            timeline = json.load(handle)
        assert result["timeline_path"].endswith(".timeline.json")
        assert [one["run_id"] for one in timeline["runs"]] == ["r1", "r2"], timeline["runs"]
        assert timeline["runs"][0]["text"] == "Erster Satz."
        assert timeline["runs"][1]["start"] > timeline["runs"][0]["end"] - 1e-9
        assert timeline["audio_file"] == result["audio_file"]
        assert timeline["chapter_id"] == "c1"

        # 7c. The chapter is now cached: a repeat render voices nothing and
        #     runs no ffmpeg — this is why replaying is instant.
        stored = store.load_project(root, project.id)
        assert model.chapter_is_cached(stored, stored.chapter("c1"))
        assert stored.chapter("c1").audio_duration == 1.5
        job_id = new_parent(project_id=project.id, chapter_id="c1", format="wav")
        assert render_chapter_job(job_id) is True
        assert len(generated) == 2, "chapter cache must not re-voice"
        assert len(ffmpeg_calls) == 1, "chapter cache must not re-mix"
        assert fake_jobs[job_id]["result"]["cached"] is True

        # 7d. Adding a paragraph invalidates the chapter but hits the RUN
        #     cache: exactly one new voicing.
        store.update_project(root, project.id, lambda p: p.chapter("c1").blocks.append(
            Block(id="b1b", runs=[Run(id="r4", text="Neuer Satz.", profile_id="v1")])
        ))
        job_id = new_parent(project_id=project.id, chapter_id="c1", format="wav")
        assert render_chapter_job(job_id) is True
        assert len(generated) == 3, generated
        assert fake_jobs[job_id]["result"]["reused_runs"] == 2
        assert len(ffmpeg_calls) == 2

        # 7e. A missing run file is a miss even though the key is unchanged.
        runs_dir = runs_dir_for(root, project.id)
        fragments = sorted(glob.glob(os.path.join(runs_dir, "r1-*.wav")))
        assert len(fragments) == 1, fragments
        os.remove(fragments[0])
        job_id = new_parent(project_id=project.id, chapter_id="c1", format="wav", force=True)
        assert render_chapter_job(job_id) is True
        assert len(generated) == 6, "force re-voices every run"
        assert fake_jobs[job_id]["result"]["reused_runs"] == 0

        # 7f. Cancellation between runs: no mix, no new chapter audio.
        store.update_project(root, project.id, lambda p: p.chapter("c1").blocks.append(
            Block(id="b1c", runs=[Run(id="r5", text="Noch einer.", profile_id="v1")])
        ))
        before_hash = store.load_project(root, project.id).chapter("c1").audio_hash
        mix_calls_before = len(ffmpeg_calls)
        cancel_after[0] = len(generated) + 1        # cancel after the next voicing
        job_id = new_parent(project_id=project.id, chapter_id="c1", format="wav", force=True)
        assert render_chapter_job(job_id) is False
        assert fake_jobs[job_id]["status"] == "cancelled", fake_jobs[job_id]
        assert len(ffmpeg_calls) == mix_calls_before, "a cancel must not mix"
        assert store.load_project(root, project.id).chapter("c1").audio_hash == before_hash
        cancel_after[0] = 0

        # 7g. Cancelling a running child marks it cancelled.
        running_child = {"id": "kid", "status": "running", "cancel_requested": False}
        _cancel_child("kid", running_child)
        assert running_child["status"] == "cancelled"
        assert running_child["cancel_requested"] is True

        # 7h. A run without a usable voice fails the job and names the run —
        #     no half-rendered chapter.
        store.update_project(
            root, project.id,
            lambda p: setattr(p.voice_profiles[0], "voice_ref_path", None),
        )
        voiced_before, mix_before = len(generated), len(ffmpeg_calls)
        job_id = new_parent(project_id=project.id, chapter_id="c2", format="wav")
        assert render_chapter_job(job_id) is False
        assert fake_jobs[job_id]["status"] == "failed"
        assert "reference voice" in (fake_jobs[job_id]["error"] or ""), fake_jobs[job_id]
        assert len(generated) == voiced_before and len(ffmpeg_calls) == mix_before
        store.update_project(
            root, project.id,
            lambda p: setattr(
                p.voice_profiles[0], "voice_ref_path", os.path.join(root, "narrator.wav"),
            ),
        )

        # 7i. Bad params fail cleanly.
        job_id = new_parent(project_id=project.id, chapter_id="c1", format="ogg")
        assert render_chapter_job(job_id) is False
        assert "Unsupported format" in (fake_jobs[job_id]["error"] or "")
        job_id = new_parent(project_id=project.id)
        assert render_chapter_job(job_id) is False
        assert "chapter_id" in (fake_jobs[job_id]["error"] or "")
        job_id = new_parent(project_id="nope", chapter_id="c1")
        assert render_chapter_job(job_id) is False
        assert "not found" in (fake_jobs[job_id]["error"] or "")

        # ── 8. Book render ---------------------------------------------
        voiced_before = len(generated)
        job_id = new_parent(project_id=project.id, format="m4b", chapter_format="wav",
                            author="Autorin")
        assert render_book_job(job_id) is True, fake_jobs[job_id]
        book = fake_jobs[job_id]["result"]
        assert book["kind"] == "book"
        assert book["audio_file"] == "Mein_Buch.m4b", book["audio_file"]
        assert os.path.isfile(book["audio_path"])
        assert [one["title"] for one in book["chapters"]] == ["Kapitel Eins", "Kapitel Zwei"]
        assert book["chapters"][0]["start"] == 0.0
        assert book["chapters"][1]["start"] == book["chapters"][0]["duration"]
        assert any("Leer" in one for one in book["warnings"]), book["warnings"]
        # Chapter 1 was cached; only chapter 2's single run had to be voiced.
        assert len(generated) == voiced_before + 1, generated
        # Chapter markers really went into the container.
        assert "-map_chapters" in [str(one) for one in ffmpeg_calls[-1]]
        assert captured_metadata and captured_metadata[-1].startswith(";FFMETADATA1")
        assert captured_metadata[-1].count("[CHAPTER]") == 2
        assert "title=Kapitel Eins" in captured_metadata[-1]
        assert "artist=Autorin" in captured_metadata[-1]
        # The sidecars are cleaned up, not left in the gallery.
        assert not glob.glob(os.path.join(root, "_audiobook_concat_*"))
        assert not glob.glob(os.path.join(root, "_audiobook_meta_*"))
        assert fake_jobs[job_id]["output_files"] == ["Mein_Buch.m4b"]

        # 8a. WAV has no chapter markers — mix.py refuses, we report why.
        job_id = new_parent(project_id=project.id, format="wav")
        assert render_book_job(job_id) is False
        assert "WAV" in (fake_jobs[job_id]["error"] or ""), fake_jobs[job_id]

        # ── 9. An ffmpeg failure reports a summary, not the whole log, and
        #       publishes nothing.
        def failing_ffmpeg(args, *, job=None):
            ffmpeg_calls.append(list(args))
            return 1, "ffmpeg version 6\nframe= 1 size= 2kB\n[amix] Invalid argument\n"

        _run_ffmpeg = failing_ffmpeg                                # noqa: F811
        store.update_project(root, project.id, lambda p: p.chapter("c1").blocks.append(
            Block(id="b1d", runs=[Run(id="r6", text="Wieder neu.", profile_id="v1")])
        ))
        target = os.path.join(root, "Mein_Buch-Kapitel_Eins.wav")
        job_id = new_parent(project_id=project.id, chapter_id="c1", format="wav")
        assert render_chapter_job(job_id) is False
        assert fake_jobs[job_id]["status"] == "failed"
        error = fake_jobs[job_id]["error"] or ""
        assert "Invalid argument" in error and "ffmpeg version" not in error, error
        assert not os.path.isfile(target), "a failed mix must not leave a file behind"
    finally:
        _run_ffmpeg, probe_duration = _real_ffmpeg, _real_probe     # noqa: F811
        init(None, None, None)
        shutil.rmtree(root, ignore_errors=True)

    print("audiobook.render self-check OK")
