"""MCP server for MuseForge.

Exposes the core generation workflow as Model Context Protocol tools so
AI agents (Claude, IDE agents, custom orchestrators) can drive MuseForge:
discover models, submit generation jobs, poll them, and fetch outputs.

Transport: streamable HTTP, mounted into the main FastAPI app at /mcp by
launch.py — same port as the UI/REST API, no extra process. Point an MCP
client at:  http://<host>:7860/mcp

Implementation notes:
- Tools are thin wrappers over the local REST API (self-HTTP against
  127.0.0.1). That keeps one canonical implementation of validation and
  defaults-hydration (the /api/v1 endpoints) instead of a second code
  path into wgp internals. Tools are sync functions on purpose: FastMCP
  runs sync tools in a worker thread, so blocking `requests` calls don't
  stall the shared event loop.
- launch.py calls set_api_port() with the RESOLVED port before serving
  (the preferred port may be taken and fall forward), so self-calls
  always hit the right instance.
"""

import base64
import os

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "MuseForge",
    instructions=(
        "MuseForge is a local AI studio for video, images, audio, long-form "
        "text and audiobooks.\n\n"
        "Media: list_models() to find a model_type -> generate() -> "
        "job_status() until 'completed' -> get_output_url() to download.\n"
        "Text: chat() for conversation; story_start() -> story_status() for "
        "long-form prose, then story_export().\n"
        "Audiobooks: audiobook_create() -> audiobook_import() -> assign "
        "voices -> audiobook_plan() to verify -> audiobook_render(). From a "
        "story, audiobook_from_story() replaces the first two steps.\n\n"
        "Generation takes minutes to hours and the first use of any model "
        "downloads weights (potentially many GB), so poll patiently. "
        "Anything without a dedicated tool is reachable through "
        "api_request(); api_request('GET', '/openapi.json') returns the "
        "full API schema."
    ),
    stateless_http=True,
    # Mounted at /mcp by launch.py — serve at the mount root so the
    # endpoint is /mcp, not /mcp/mcp.
    streamable_http_path="/",
)

_api_port: int = int(os.environ.get("SERVER_PORT", "7860"))


def set_api_port(port: int) -> None:
    """Called by launch.py with the resolved bind port before serving."""
    global _api_port
    _api_port = port


def _get(path: str, **kw):
    r = requests.get(f"http://127.0.0.1:{_api_port}{path}", timeout=kw.pop("timeout", 30), **kw)
    r.raise_for_status()
    return r.json()


def _post(path: str, json=None, timeout: float = 60):
    r = requests.post(f"http://127.0.0.1:{_api_port}{path}", json=json, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _put(path: str, json=None, timeout: float = 60):
    r = requests.put(f"http://127.0.0.1:{_api_port}{path}", json=json, timeout=timeout)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def list_models() -> dict:
    """List available generation models grouped by family.

    Returns families and models; each model has model_type (the id to
    pass to generate()), name, family, capability flags (is_t2v, is_i2v,
    supports_audio, ...) and is_downloaded (False means the first
    generation will download the weights first).
    """
    return _get("/api/v1/models")


@mcp.tool()
def model_defaults(model_type: str) -> dict:
    """Get the default generation settings for a model_type.

    Useful to inspect tunable parameters (resolution, video_length,
    guidance, steps, ...) before overriding them in generate().
    """
    return _get(f"/api/v1/defaults/{model_type}")


@mcp.tool()
def generate(model_type: str, prompt: str, params: dict | None = None) -> dict:
    """Submit a generation job (video, image or audio depending on model).

    model_type: id from list_models(). prompt: the text prompt. params:
    optional overrides merged over the model's defaults — see
    model_defaults() for available keys (e.g. resolution "832x480",
    video_length in frames, seed, negative_prompt).

    Returns {"job_id": ...} immediately; poll job_status(job_id).
    """
    body = dict(params or {})
    body["model_type"] = model_type
    body["prompt"] = prompt
    return _post("/api/v1/generate", json=body)


@mcp.tool()
def job_status(job_id: str) -> dict:
    """Status of a generation job: status (queued/running/completed/
    failed), progress, step counts, message, error and output_files."""
    return _get(f"/api/v1/status/{job_id}")


@mcp.tool()
def list_jobs() -> dict:
    """List recent generation jobs with their statuses."""
    return _get("/api/v1/jobs")


@mcp.tool()
def cancel_job(job_id: str) -> dict:
    """Cancel a queued or running generation job."""
    return _post(f"/api/v1/cancel/{job_id}")


@mcp.tool()
def list_outputs() -> dict:
    """List generated output files (videos, images, audio) in the active
    workspace, newest first."""
    return _get("/api/v1/outputs")


@mcp.tool()
def get_output_url(name: str) -> str:
    """Download path for an output file returned by list_outputs() or
    job_status().output_files. Returns a path relative to this MCP
    server's origin — append it to the host:port you connected to
    (inside Docker the container port may be mapped elsewhere on the
    host, so an absolute internal URL would be wrong)."""
    return f"/api/v1/file/{name}"


@mcp.tool()
def enhance_prompt(prompt: str, mode: str = "video") -> dict:
    """Rewrite a rough prompt into a detailed generation prompt using
    MuseForge's local LLM (downloads the LLM on first use, can take a
    while). mode: "video" or "image"."""
    return _post("/api/v1/llm/enhance-prompt", json={"prompt": prompt, "mode": mode}, timeout=600)


# ── Text mode: chat and long-form writing ─────────────────────────────


@mcp.tool()
def chat(message: str, thread_id: str = "", system_prompt: str = "",
         model_id: str = "") -> dict:
    """Talk to the local LLM with conversation memory.

    Without thread_id a new conversation is created and its id returned —
    pass that id back to continue the same conversation. Threads persist on
    the server, so the UI and other clients see them too.

    model_id pins a model for a new thread (see list_text_models); it is
    ignored when continuing an existing one. The first call may take
    minutes if the model still has to download.

    Returns {reply, thread_id, stream_id}.
    """
    tid = thread_id.strip()
    if not tid:
        body = {}
        if system_prompt:
            body["system_prompt"] = system_prompt
        if model_id:
            body["model_id"] = model_id
        tid = _post("/api/v1/chat/threads", json=body)["id"]
    result = _post(f"/api/v1/chat/threads/{tid}/messages",
                   json={"content": message}, timeout=3600)
    return {"reply": (result.get("message") or {}).get("content", ""),
            "thread_id": tid,
            "stream_id": result.get("stream_id")}


@mcp.tool()
def list_chat_threads() -> dict:
    """Existing chat conversations, newest first."""
    return _get("/api/v1/chat/threads")


@mcp.tool()
def list_text_models() -> dict:
    """Text models per Storywriter pass.

    Returns {outline, prose}: the outline pass wants instruction-following,
    the prose pass wants a writer, so the catalogs differ deliberately.
    Chat can use either.
    """
    return _get("/api/v1/story/models")


@mcp.tool()
def story_start(premise: str, min_pages: int = 50, params: dict | None = None) -> dict:
    """Start writing a long-form story. Returns {story_id} immediately.

    premise: what the story is about, in prose. min_pages: target length
    (~275 words per page) — the outline pass derives a chapter count from
    it unless params sets chapter_count.

    params may also carry: title, genre, tone, pov ("first" |
    "third_limited" | "third_omniscient"), tense ("past" | "present"),
    audience, chapter_count, explicitness ("none" | "moderate" |
    "explicit", only effective when the instance has mature mode enabled),
    outline_model, prose_model, temperature.

    Writing runs for many minutes to hours. Poll story_status(story_id);
    live text streams under stream_id story-<id>-outline and
    story-<id>-ch<index> via the stream-status endpoint.
    """
    body = dict(params or {})
    body["premise"] = premise
    body["min_pages"] = min_pages
    return _post("/api/v1/story/stories", json=body)


@mcp.tool()
def story_status(story_id: str) -> dict:
    """Full story state: status, progress, outline, and every chapter with
    its text and word count. Status runs queued -> planning -> writing ->
    completed, or failed/cancelled/crashed."""
    return _get(f"/api/v1/story/stories/{story_id}", timeout=120)


@mcp.tool()
def list_stories() -> dict:
    """Story summaries in the active workspace, newest first."""
    return _get("/api/v1/story/stories")


@mcp.tool()
def story_stop(story_id: str) -> dict:
    """Stop a running story. Chapters already written are kept."""
    return _post(f"/api/v1/story/stories/{story_id}/stop")


@mcp.tool()
def story_extend(story_id: str, additional_chapters: int = 1) -> dict:
    """Append chapters, continuing from the story's current synopsis."""
    return _post(f"/api/v1/story/stories/{story_id}/extend",
                 json={"additional_chapters": additional_chapters})


@mcp.tool()
def story_regenerate_chapter(story_id: str, chapter_index: int,
                             instruction: str = "") -> dict:
    """Rewrite one chapter. instruction steers the rewrite ("darker", "more
    dialogue", "cut the flashback"). Continuity for later chapters is
    replayed afterwards."""
    body = {"instruction": instruction} if instruction else {}
    return _post(
        f"/api/v1/story/stories/{story_id}/chapters/{chapter_index}/regenerate",
        json=body)


@mcp.tool()
def story_edit_chapter(story_id: str, chapter_index: int, text: str) -> dict:
    """Replace a chapter's text. The running synopsis is marked stale so the
    next pass rebuilds it from what is actually written. Only allowed while
    the story is not running."""
    return _put(f"/api/v1/story/stories/{story_id}/chapters/{chapter_index}",
                {"text": text})


@mcp.tool()
def story_export(story_id: str, fmt: str = "md") -> dict:
    """Write the story into the workspace as "md" or "txt". The file then
    appears as a text output, downloadable via get_output_url()."""
    return _post(f"/api/v1/story/stories/{story_id}/export", json={"format": fmt})


# ── AudioBook Creator ─────────────────────────────────────────────────


@mcp.tool()
def audiobook_create(title: str = "Untitled audiobook", language: str = "en") -> dict:
    """Create an audiobook project. Returns the full project.

    Workflow: create -> audiobook_import a document -> assign voices
    (audiobook_update, or the UI) -> audiobook_plan to check readiness ->
    audiobook_render.

    The project id is under "project_id" in the returned object; pass that
    to the other audiobook tools.
    """
    return _post("/api/v1/audiobook/projects",
                 json={"title": title, "language": language})


@mcp.tool()
def audiobook_from_story(story_id: str, lang: str = "", title: str = "",
                         profile_id: str = "") -> dict:
    """Create an audiobook project from a written story in one step.

    Keeps the story's own chapters instead of re-detecting headings. lang
    picks a translation (falling back to the original per chapter); empty
    means the story's original language. profile_id pre-assigns one voice to
    every run. Returns {project, chapters, story_id, lang}.

    Use this instead of audiobook_create + audiobook_import when the source
    is a story — no file export/upload roundtrip is needed.
    """
    body = {"story_id": story_id}
    for key, value in (("lang", lang), ("title", title), ("profile_id", profile_id)):
        if value:
            body[key] = value
    return _post("/api/v1/audiobook/from-story", json=body, timeout=300)


@mcp.tool()
def list_audiobooks() -> dict:
    """Audiobook project summaries in the active workspace."""
    return _get("/api/v1/audiobook/projects")


@mcp.tool()
def audiobook_get(project_id: str) -> dict:
    """Full project: chapters, blocks, voice profiles, sfx, music."""
    return _get(f"/api/v1/audiobook/projects/{project_id}", timeout=120)


@mcp.tool()
def audiobook_import(project_id: str, path: str, auto_split: bool = True,
                     replace: bool = False) -> dict:
    """Import a document as chapters.

    path is a file already on the server — upload it first with
    upload_image/upload_audio for media, or place it in the workspace.
    Supports .txt, .md, .docx, .pdf and .epub (the last three need their
    optional parser installed; the error names the package if missing).
    auto_split detects chapter headings; replace swaps the existing
    chapters instead of appending.
    """
    return _post(f"/api/v1/audiobook/projects/{project_id}/import",
                 json={"path": path, "auto_split": auto_split, "replace": replace},
                 timeout=300)


@mcp.tool()
def audiobook_update(project_id: str, changes: dict) -> dict:
    """Patch a project. changes may carry any of: title, language,
    chapters, voice_profiles, sfx, music, default_profile_id,
    render_settings.

    A voice profile is {id, name, color, model_type, voice_ref_path,
    emotion_ref_path, default_emotion, params}; runs reference it by
    profile_id. Call audiobook_get first and send back modified structures
    — this replaces the fields you pass.
    """
    return _put(f"/api/v1/audiobook/projects/{project_id}", changes)


@mcp.tool()
def audiobook_add_effect(project_id: str, prompt: str, label: str = "",
                         duration: float = 5.0, ambience: bool = True) -> dict:
    """Add a sound effect, generating its audio from a text prompt.

    Write the prompt in English regardless of the book's language — the
    audio model is trained on English descriptions. ambience=True loops
    quietly under the speech; False makes it a one-shot the speech pauses
    for. The asset exists at once with no audio and fills in when the job
    finishes.
    """
    return _post(f"/api/v1/audiobook/projects/{project_id}/assets/sfx", json={
        "prompt": prompt, "label": label or prompt[:40], "duration": duration,
        "playback_mode": "parallel" if ambience else "sequential",
        "loop": ambience, "volume": 0.3 if ambience else 0.8,
    })


@mcp.tool()
def audiobook_add_music(project_id: str, prompt: str, title: str = "",
                        duration: float = 60.0) -> dict:
    """Add a background music bed, generating it from a description.

    Assign it to a chapter with audiobook_update (chapter.music_id); the
    render ducks it automatically while anyone speaks.
    """
    return _post(f"/api/v1/audiobook/projects/{project_id}/assets/music", json={
        "prompt": prompt, "title": title or prompt[:40], "duration": duration,
    })


@mcp.tool()
def audiobook_suggest_cast(project_id: str, chapter_index: int = 0) -> dict:
    """Ask the LLM who speaks which line, with what emotion, plus effects.

    Returns proposals only — apply a reviewed subset with
    audiobook_apply_cast. Every id is validated against the chapter first,
    and invented ones are reported in `dropped` rather than applied.
    """
    return _post(f"/api/v1/audiobook/projects/{project_id}/suggest-cast",
                 json={"chapter_index": chapter_index}, timeout=900)


@mcp.tool()
def audiobook_apply_cast(project_id: str, chapter_index: int = 0,
                         suggestions: dict | None = None) -> dict:
    """Apply cast suggestions. Pass the (possibly filtered) characters,
    assignments and effects from audiobook_suggest_cast.

    Missing voice profiles are created so no assignment can point at a
    profile that does not exist. Effects are attached and their audio
    generated in the background.
    """
    body = {"chapter_index": chapter_index, **(suggestions or {})}
    return _post(f"/api/v1/audiobook/projects/{project_id}/apply-cast",
                 json=body, timeout=300)


@mcp.tool()
def audiobook_suggest_split(project_id: str, chapter_index: int = 0,
                            target_words: int = 2500) -> dict:
    """Ask the LLM where a long chapter should break. Proposals only —
    apply them with audiobook_apply_split."""
    return _post(f"/api/v1/audiobook/projects/{project_id}/suggest-split",
                 json={"chapter_index": chapter_index, "target_words": target_words},
                 timeout=900)


@mcp.tool()
def audiobook_apply_split(project_id: str, chapter_index: int = 0,
                          splits: list | None = None) -> dict:
    """Split a chapter at the given break points.

    splits is a list of {after_block_id, new_title} from
    audiobook_suggest_split. Blocks keep their identity, so voice
    assignments and attached effects survive the split.
    """
    return _post(f"/api/v1/audiobook/projects/{project_id}/apply-split",
                 json={"chapter_index": chapter_index, "splits": splits or []})


@mcp.tool()
def audiobook_render(project_id: str, chapter_index: int = -1, book: bool = False,
                     fmt: str = "", force: bool = False) -> dict:
    """Render audiobook audio. Returns {job_id} — poll job_status().

    chapter_index renders one chapter; book=True renders the whole book and
    defaults to m4b with chapter markers. fmt overrides the format
    (mp3/wav/flac, plus m4b for a book). force ignores the cache.

    Speech runs are cached by content and seed, so re-rendering after a
    small edit only re-voices what actually changed. A full book runs for
    a long time; the job survives a disconnect.
    """
    body: dict = {"force": force}
    if book:
        body["book"] = True
    else:
        body["chapter_index"] = max(0, chapter_index)
    if fmt:
        body["format"] = fmt
    return _post(f"/api/v1/audiobook/projects/{project_id}/render", json=body)


@mcp.tool()
def audiobook_plan(project_id: str, chapter_index: int = 0) -> dict:
    """Dry-run the text-to-speech mapping for a chapter.

    Returns one plan per speech run plus `errors` and `ready`. Check this
    before rendering: it reports paragraphs with no voice assigned and
    models that need a reference clip, which would otherwise fail the
    render minutes in.
    """
    return _post(f"/api/v1/audiobook/projects/{project_id}/plan",
                 json={"chapter_index": chapter_index})


@mcp.tool()
def list_blueprints(kind: str = "") -> dict:
    """Preset cards ("blueprints"): image/video looks, stories, voices, effects.

    Each card carries `kind`, which says what it sets up and therefore how to
    use it — fetch the full blueprint with get_blueprint:

      generation  model_type + params + prompt_example  -> generate()
      sfx         same shape, params carry MMAudio_prompt -> generate()
      story       a `story` object of Storywriter fields -> story_start()
      voice       a `voice` object of library fields     -> create_voice()

    kind filters the list; empty returns everything.
    """
    cards = _get("/api/v1/recipes").get("recipes") or []
    if kind:
        cards = [c for c in cards if c.get("kind") == kind]
    return {"blueprints": cards, "count": len(cards)}


@mcp.tool()
def get_blueprint(blueprint_id: str) -> dict:
    """One blueprint in full, including the payload for its kind.

    See list_blueprints for which field to read per kind and which call to
    pass it to. Nothing is applied by fetching it.
    """
    return _get(f"/api/v1/recipes/{blueprint_id}")


@mcp.tool()
def list_activity() -> dict:
    """Everything currently running, with how to stop each one.

    Covers generation jobs, Director pipelines, Storywriter runs, story
    analysis/translation passes and audiobook renders. Each entry carries a
    `cancel` path — pass it to stop_activity_item.
    """
    return _get("/api/v1/activity")


@mcp.tool()
def stop_activity_item(cancel_path: str) -> dict:
    """Stop one running task by the `cancel` path list_activity gave for it.

    One tool for every kind of task, because the cancel route differs per
    feature and list_activity already resolved it.
    """
    path = cancel_path if cancel_path.startswith("/") else f"/{cancel_path}"
    if not path.startswith("/api/v1/"):
        return {"error": f"Not a cancel path from list_activity: {cancel_path}"}
    return _post(path, timeout=120)


@mcp.tool()
def stop_all_activity() -> dict:
    """Stop every running task. Reports per item, so one stubborn task does
    not hide that the rest went down."""
    return _post("/api/v1/activity/stop-all", timeout=120)


@mcp.tool()
def story_translate(story_id: str, language: str) -> dict:
    """Translate a story into another language (ISO code, e.g. "de").

    The original is untouched — a translation is an additional view. Runs in
    the background; poll story_status and read languages/chapters.
    """
    return _post(f"/api/v1/story/stories/{story_id}/translate",
                 json={"language": language})


@mcp.tool()
def story_rewrite_passage(story_id: str, chapter_index: int, selected_text: str,
                          instruction: str, lang: str = "") -> dict:
    """Propose a rewrite of an exact passage. Nothing is applied.

    selected_text must appear exactly once in that chapter — zero or several
    matches is an error rather than a guess, because rewriting the wrong
    paragraph is worse than refusing. Returns {replacement, before, after};
    apply it with story_apply_rewrite.
    """
    body = {"selected_text": selected_text, "instruction": instruction}
    if lang:
        body["lang"] = lang
    return _post(
        f"/api/v1/story/stories/{story_id}/chapters/{chapter_index}/rewrite",
        json=body, timeout=900)


@mcp.tool()
def story_apply_rewrite(story_id: str, chapter_index: int, selected_text: str,
                        replacement: str, lang: str = "") -> dict:
    """Replace a passage with a reviewed rewrite."""
    body = {"selected_text": selected_text, "replacement": replacement}
    if lang:
        body["lang"] = lang
    return _post(
        f"/api/v1/story/stories/{story_id}/chapters/{chapter_index}/apply-rewrite",
        json=body)


@mcp.tool()
def story_insert_chapter(story_id: str, at_index: int, write: bool = False,
                         brief: str = "", title: str = "") -> dict:
    """Insert a chapter. write=True has the LLM write it using both
    neighbours as the seam, so it fits where it lands; otherwise an empty
    chapter is inserted. at_index past the end appends."""
    return _post(f"/api/v1/story/stories/{story_id}/chapters", json={
        "at_index": at_index, "write": write, "brief": brief, "title": title,
    })


@mcp.tool()
def story_analyze(story_id: str) -> dict:
    """Audit a story: characters with their chapter spans, who speaks where,
    a timeline, and issues such as plot holes and continuity breaks.

    Runs one pass per chapter and merges, so it takes a while on a novel.
    The result is stored on the story and comes back from story_status too.
    """
    return _post(f"/api/v1/story/stories/{story_id}/analyze", timeout=3600)


@mcp.tool()
def story_download_url(story_id: str, fmt: str = "md", lang: str = "",
                       per_chapter: bool = False) -> str:
    """Download path for a story as md, txt, docx or pdf.

    per_chapter=True returns a ZIP with one file per chapter. Append the
    result to the host you connected to and fetch it.
    """
    query = f"fmt={fmt}"
    if lang:
        query += f"&lang={lang}"
    if per_chapter:
        query += "&per_chapter=true"
    return f"/api/v1/story/stories/{story_id}/download?{query}"


@mcp.tool()
def list_voices() -> dict:
    """Named voices in this workspace, shared by Speech and audiobooks.

    `ready` is false when an engine needs a reference recording and none is
    present (or the file went missing), which is what would otherwise fail
    mid-render.
    """
    return _get("/api/v1/voices")


@mcp.tool()
def create_voice(name: str, model_type: str = "index_tts2",
                 reference_path: str = "", description: str = "",
                 default_emotion: str = "", language: str = "") -> dict:
    """Add a named voice to the library.

    reference_path is a file already on the server — upload a recording with
    upload_audio first (mp3/ogg/m4a/wav or a video, whose track is
    extracted). Engines that clone need one; qwen3_tts_voicedesign instead
    takes a written description via params.voice_description.
    """
    body = {"name": name, "model_type": model_type}
    for key, value in (("reference_path", reference_path),
                       ("description", description),
                       ("default_emotion", default_emotion),
                       ("language", language)):
        if value:
            body[key] = value
    return _post("/api/v1/voices", json=body)


@mcp.tool()
def adopt_voice(path: str, name: str = "", engine: str = "index_tts2",
                language: str = "", description: str = "") -> dict:
    """Turn audio that already exists into a library voice, in one call.

    path: any audio on the server — a workspace output from list_outputs, an
    upload_audio result, or a file in the workspace. Binds it to a cloning
    engine, which is what makes the voice stable: the clip carries the timbre,
    so every passage is spoken by the same person.

    Prefer this over create_voice whenever a recording of the wanted voice
    exists. create_voice with a written description gives a speaker that
    changes on every render until freeze_voice pins one.

    Returns {voice, adopted_from, duration, warnings} — a warning says so when
    the clip is too short or too long to clone well.
    """
    body = {"path": path, "engine": engine}
    for key, value in (("name", name), ("language", language),
                       ("description", description)):
        if value:
            body[key] = value
    return _post("/api/v1/voices/adopt", json=body)


@mcp.tool()
def preview_voice(voice_id: str, text: str = "") -> dict:
    """Audition a library voice. Returns a job_id; poll job_status.

    Goes through the same planner a real render uses, so a voice that
    previews will also render.

    Repeating it does NOT give the same voice unless the voice has a reference
    clip: the description-driven engines resample a speaker every run. Audition
    until one is good, then freeze_voice to keep it.
    """
    return _post(f"/api/v1/voices/{voice_id}/preview",
                 json={"text": text} if text else {})


@mcp.tool()
def reroll_voice(voice_id: str) -> dict:
    """New seed and a cleared audition — start looking for a voice again.

    Cosmetic on its own: the seed does not decide who a description-built
    speaker turns out to be (measured: three renders with one pinned seed gave
    three voices). Use it to discard a stored audition; use freeze_voice to
    actually keep one.
    """
    return _post(f"/api/v1/voices/{voice_id}/reroll")


@mcp.tool()
def freeze_voice(voice_id: str, engine: str = "index_tts2") -> dict:
    """Keep the audition you liked: it becomes the voice's reference clip.

    THE way to make a voice reproducible. Engines that build a speaker from a
    written description sample a new one on every render, so a voice without a
    clip is a different person in every paragraph. Freezing switches the voice
    to a cloning engine with that take as its reference.

    Order: create_voice -> preview_voice (repeat until good) -> freeze_voice.
    Returns {voice, frozen_from, warnings}; a warning says so when the frozen
    take is shorter than cloning likes.
    """
    return _post(f"/api/v1/voices/{voice_id}/freeze", json={"engine": engine})


@mcp.tool()
def audiobook_import_voice(project_id: str, voice_id: str) -> dict:
    """Copy a library voice into an audiobook project. A copy on purpose:
    editing it inside the book must not rewrite the shared entry."""
    return _post(f"/api/v1/audiobook/projects/{project_id}/voices/import",
                 json={"voice_id": voice_id})


@mcp.tool()
def speak_with_voice(voice_id: str, text: str, language: str = "",
                     emotion: str = "") -> dict:
    """Read text aloud with a library voice. Returns a job_id.

    Unlike preview_voice this is real output: it lands in the workspace as a
    normal audio file. Poll job_status, then get_output_url.
    """
    body: dict = {"text": text}
    if language:
        body["language"] = language
    if emotion:
        body["emotion"] = emotion
    return _post(f"/api/v1/voices/{voice_id}/speak", json=body)


@mcp.tool()
def audiobook_preview_passage(project_id: str, text: str,
                              profile_id: str = "", emotion: str = "") -> dict:
    """Speak one passage of a book to check a voice before a full render.

    Without profile_id the project's default voice is used. Returns a
    job_id; the same planner as a real render decides whether it can be
    spoken at all.
    """
    body: dict = {"text": text}
    if profile_id:
        body["profile_id"] = profile_id
    if emotion:
        body["emotion"] = emotion
    return _post(f"/api/v1/audiobook/projects/{project_id}/preview-passage",
                 json=body)


@mcp.tool()
def audiobook_sfx_library() -> dict:
    """Sound effects already in the workspace, ready to reuse instead of
    regenerating them."""
    return _get("/api/v1/audiobook/sfx-library")


@mcp.tool()
def audiobook_adopt_effect(project_id: str, path: str, label: str = "",
                           ambience: bool = True) -> dict:
    """Add an existing audio file to a project as an effect, without
    generating anything. Use audiobook_sfx_library to find candidates."""
    return _post(f"/api/v1/audiobook/projects/{project_id}/assets/sfx/adopt", json={
        "path": path, "label": label,
        "playback_mode": "parallel" if ambience else "sequential",
        "loop": ambience,
    })


@mcp.tool()
def system_status() -> dict:
    """Environment preflight: GPU/CUDA availability, disk space, ffmpeg.
    ok=true means the system is ready to generate."""
    return _get("/api/v1/system/preflight")


@mcp.tool()
def api_request(method: str, path: str, body: dict | None = None) -> dict:
    """Call any MuseForge REST endpoint directly — the escape hatch for
    everything not covered by a dedicated tool.

    method: GET, POST, PUT or DELETE. path: must start with "/api/v1/"
    (or be exactly "/openapi.json" to discover the full API schema —
    fetch that first when unsure which endpoint or body shape to use).
    body: JSON body for POST/PUT.

    Returns the endpoint's JSON response, or {"status_code", "text"} for
    non-JSON responses.
    """
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "DELETE"):
        raise ValueError(f"Unsupported method: {method}")
    if not (path.startswith("/api/v1/") or path == "/openapi.json"):
        raise ValueError('path must start with "/api/v1/" (or be "/openapi.json")')
    r = requests.request(
        method, f"http://127.0.0.1:{_api_port}{path}", json=body, timeout=120
    )
    r.raise_for_status()
    if "application/json" in r.headers.get("content-type", ""):
        return r.json()
    return {"status_code": r.status_code, "text": r.text[:2000]}


@mcp.tool()
def upload_image(image_base64: str, filename: str = "upload.png") -> dict:
    """Upload an image (base64-encoded) to the server for use in
    generation.

    Returns {"filename", "path", "url"}. Pass the returned "path" as the
    "image_start" key in generate() params to use it as an i2v start
    image (see model_defaults() for related keys like image_end,
    image_prompt_type). The extension of `filename` determines how the
    server treats the file.
    """
    files = {"file": (filename, base64.b64decode(image_base64))}
    r = requests.post(
        f"http://127.0.0.1:{_api_port}/api/v1/upload", files=files, timeout=120
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
def upload_audio(audio_base64: str, filename: str = "upload.mp3") -> dict:
    """Upload an audio file (base64-encoded): wav, mp3, flac, ogg, m4a —
    or a video whose audio track gets extracted. Compressed formats are
    transcoded to wav server-side.

    Returns {"filename", "path", "url"} — always a WAV path. Use "path"
    e.g. as audio_path in director_start() or as an audio guide in
    generate() params (see model_defaults() for the exact key).
    """
    files = {"file": (filename, base64.b64decode(audio_base64))}
    r = requests.post(
        f"http://127.0.0.1:{_api_port}/api/v1/upload-audio", files=files, timeout=300
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
def upload_document(document_base64: str, filename: str = "book.txt") -> dict:
    """Upload a text document (base64) so audiobook_import can read it.

    Accepts .txt, .md, .docx, .pdf and .epub — the extension of `filename`
    decides which parser runs, so keep it correct. Returns
    {"filename", "path", "url"}; pass the returned "path" to
    audiobook_import.

    Full hand-off for "here is a document, make an audiobook":
      upload_document -> audiobook_create -> audiobook_import ->
      adopt_voice or create_voice (+ audiobook_import_voice) ->
      audiobook_plan until ready -> audiobook_render -> get_output_url.
    """
    files = {"file": (filename, base64.b64decode(document_base64))}
    r = requests.post(
        f"http://127.0.0.1:{_api_port}/api/v1/upload", files=files, timeout=300
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
def download_model(model_type: str) -> dict:
    """Pre-download a model's weights in the background (instead of
    stalling the first generate() for many GB). Returns
    {"status": "downloading", "model_type"}; poll model_download_status().
    """
    return _post(f"/api/v1/models/{model_type}/download")


@mcp.tool()
def model_download_status() -> dict:
    """Status of model pre-downloads started via download_model():
    {"downloads": {model_type: {"status": downloading/completed/failed,
    "error", "started", "model_name", "files_total", "files_done",
    "current_file", "bytes_total"}}}. Byte-level progress for the file
    currently in flight lives at GET /api/v1/downloads/active."""
    return _get("/api/v1/models/downloads/status")


@mcp.tool()
def output_metadata(name: str) -> dict:
    """Generation metadata (prompt, seed, model, settings) for an output
    file from list_outputs(). Reads the sidecar .meta.json first, falls
    back to metadata embedded in the media file."""
    return _get(f"/api/v1/outputs/{name}/metadata")


@mcp.tool()
def upscale(video_path: str, params: dict | None = None) -> dict:
    """Upscale an existing clip (output filename or uploaded path) with
    the spatial upsampler.

    Optional params keys: method (default "flashvsr2"), seed, workspace.
    Returns {"job_id"}; poll job_status(job_id).
    """
    body = dict(params or {})
    body["video_path"] = video_path
    return _post("/api/v1/tools/upscale", json=body)


@mcp.tool()
def revoice(video_path: str, voice_ref_paths: list[str], params: dict | None = None) -> dict:
    """Replace the voice(s) in an existing clip via SeedVC voice
    conversion.

    video_path: output filename or uploaded path. voice_ref_paths: one or
    two reference voice audio paths (e.g. from upload_audio()). Optional
    params keys: mode ("single"|"two"), diffusion_steps (default 25),
    cfg_rate (default 0.5), workspace. Returns {"job_id"}; poll
    job_status(job_id).
    """
    body = dict(params or {})
    body["video_path"] = video_path
    body["voice_ref_paths"] = voice_ref_paths
    return _post("/api/v1/tools/revoice", json=body)


@mcp.tool()
def director_start(params: dict) -> dict:
    """Start a Director pipeline: LLM planning -> start-image generation
    -> video generation, fully server-side (can run for hours).

    Key params fields: pipeline_type ("music_video" | "short_film_audio"
    | "short_film_story" | "podcast" | "viral_video"), scene_description
    (the concept/story text), audio_path (server path from upload_audio(),
    required for audio-driven modes), lyrics (or transcript text),
    video_model / image_model (model_type ids from list_models()),
    workspace. For the full field list fetch the schema via
    api_request("GET", "/openapi.json").

    Returns {"pipeline_id"}; poll director_status(pipeline_id).
    """
    return _post("/api/v1/director/pipeline/start", json=params, timeout=300)


@mcp.tool()
def director_status(pid: str) -> dict:
    """Current status of a Director pipeline: status, phase, progress,
    clip_plans, output_files, error."""
    return _get(f"/api/v1/director/pipeline/{pid}")


@mcp.tool()
def director_stop(pid: str) -> dict:
    """Cancel a running Director pipeline."""
    return _post(f"/api/v1/director/pipeline/{pid}/stop")


@mcp.tool()
def list_director_pipelines() -> dict:
    """List saved Director pipeline states for the active workspace."""
    return _get("/api/v1/director/pipelines")


@mcp.tool()
def list_loras(model_type: str) -> dict:
    """List installed LoRA filenames usable with a model_type. Activate
    them in generate() params via activated_loras (see model_defaults())."""
    return _get(f"/api/v1/loras/{model_type}")


@mcp.tool()
def civitai_search(query: str) -> dict:
    """Search CivitAI for LoRAs by text query. Returns CivitAI's model
    list (items with modelVersions containing download URLs and files).
    For filters (types, baseModels, sort, nsfw, ...) use
    api_request("GET", "/api/v1/civitai/search?query=...&types=...")."""
    return _get("/api/v1/civitai/search", params={"query": query})


@mcp.tool()
def civitai_download(params: dict) -> dict:
    """Download a LoRA (or checkpoint) file from CivitAI.

    Required: download_url (must point to civitai.com — take it from
    civitai_search() results' modelVersions files). Recommended:
    filename, target_arch (model_type the LoRA is for, routes it into
    the right loras dir). Optional metadata for the sidecar: model_id,
    version_id, trained_words, model_name, base_model. For checkpoints:
    kind="checkpoint" plus target_architecture (required). Returns
    {"download_id", "status"}; check progress via
    api_request("GET", "/api/v1/civitai/downloads").
    """
    return _post("/api/v1/civitai/download", json=params)
