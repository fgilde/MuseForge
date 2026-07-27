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
