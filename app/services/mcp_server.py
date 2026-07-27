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
        "MuseForge is a local AI video, image and audio generation studio. "
        "Typical flow: list_models() to find a model_type -> generate() to "
        "submit a job -> job_status() until status is 'completed' -> the "
        "output_files paths can be fetched via get_output_url(). Generation "
        "can take minutes and the first use of a model downloads weights "
        "(potentially many GB) — poll patiently."
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
