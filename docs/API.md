# MuseForge API & MCP

MuseForge exposes everything the UI can do over a versioned REST API, plus a
Model Context Protocol (MCP) endpoint so AI agents can drive it directly.

## REST API

Base URL: `http://localhost:7861/api/v1`. Interactive OpenAPI docs with the
full endpoint list and schemas: **`http://localhost:7861/docs`**.

The core generation workflow:

| Endpoint | Purpose |
|---|---|
| `GET /models` | Model families + model types with capability flags |
| `GET /defaults/{model_type}` | Default settings for a model (all tunable keys) |
| `POST /generate` | Submit a job — body: defaults-shaped dict with at least `model_type` and `prompt`. Returns `{job_id}` |
| `GET /status/{job_id}` | Job status, progress, `output_files`, error |
| `POST /cancel/{job_id}` | Cancel a queued/running job |
| `GET /jobs` | Recent jobs |
| `GET /outputs` | Generated files in the active workspace |
| `GET /file/{path}` | Download an output file |
| `POST /llm/enhance-prompt` | Prompt rewriting via the local LLM |
| `GET /system/preflight` | GPU/CUDA/disk readiness check |

### Text: chat and Storywriter

| Endpoint | Purpose |
|---|---|
| `GET/POST /chat/threads` | List or create conversations |
| `GET/PUT/DELETE /chat/threads/{id}` | Read, rename/retarget, delete a thread |
| `POST /chat/threads/{id}/messages` | Send a message, get the reply (runs to completion) |
| `GET /llm/stream-status?stream_id=` | Live tokens of a running generation |
| `POST /llm/cancel` | Stop a generation between tokens, keeping partial text |
| `GET /story/models` | Model catalogs per pass (`outline`, `prose`) |
| `GET /story/estimate?min_pages=` | Pages to words and chapter count |
| `GET/POST /story/stories` | List stories, start a new one |
| `GET/DELETE /story/stories/{id}` | Full state (outline, chapters, progress), delete |
| `POST /story/stories/{id}/stop` | Stop writing, keep what exists |
| `POST /story/stories/{id}/extend` | Append N chapters |
| `POST /story/stories/{id}/chapters/{i}/regenerate` | Rewrite a chapter, optional `instruction` |
| `PUT /story/stories/{id}/chapters/{i}` | Save a manual edit |
| `POST /story/stories/{id}/export` | Write `.md`/`.txt` into the workspace |

Chat and story generations stream into per-`stream_id` slots
(`chat-<tid>`, `story-<sid>-outline`, `story-<sid>-ch<i>`), so several can
run without overwriting each other's output.

### AudioBook Creator

| Endpoint | Purpose |
|---|---|
| `GET/POST /audiobook/projects` | List or create projects |
| `GET/PUT/DELETE /audiobook/projects/{id}` | Read, patch, delete. `PUT` is the only write path |
| `POST /audiobook/projects/{id}/import` | Import txt/md/docx/pdf/epub as chapters |
| `POST /audiobook/projects/{id}/plan` | Dry-run the TTS mapping; returns blocking `errors` and `ready` |
| `POST /audiobook/projects/{id}/render` | Render a chapter or the whole book. Returns `{job_id}` |

A project is `chapters[] -> blocks[] -> runs[]`, where each run carries its
voice binding and optional emotion. Runs, not character offsets, so voice
assignments cannot drift when the text is edited. Always `plan` before
`render`: it reports paragraphs with no voice and models that need a
reference clip, which would otherwise fail a render minutes in. Rendered
speech runs are cached by content and seed, so re-rendering after a small
edit only re-voices what changed.

### Everything else

The API also covers Director pipelines (`/director/*`), LoRA management and
the CivitAI browser (`/loras/*`, `/civitai/*`), blueprints (`/recipes/*`),
workspaces, upscaling/retake/inpaint tools, and system configuration — browse
`/docs` for the complete surface.

**Note:** the API has no authentication and CORS is restricted to localhost.
Don't expose the port to untrusted networks.

## MCP (Model Context Protocol)

Endpoint: **`http://localhost:7861/mcp`** (streamable HTTP transport, same
port as the UI — no extra process).

Example client config (Claude Code):

```bash
claude mcp add --transport http museforge http://localhost:7861/mcp
```

Or in an `mcp.json`-style config:

```json
{
  "mcpServers": {
    "museforge": { "type": "http", "url": "http://localhost:7861/mcp" }
  }
}
```

### Tools

**Media generation**

| Tool | Purpose |
|---|---|
| `list_models()` | Discover model types + capabilities |
| `model_defaults(model_type)` | Inspect tunable parameters |
| `generate(model_type, prompt, params?)` | Submit a job, returns `job_id` |
| `job_status(job_id)` | Poll until `completed`/`failed` |
| `list_jobs()` | Recent jobs |
| `cancel_job(job_id)` | Cancel a job |
| `list_outputs()` | Generated files |
| `get_output_url(name)` | Download URL for an output |
| `enhance_prompt(prompt, mode?)` | LLM prompt rewriting |
| `system_status()` | Readiness preflight |
| `api_request(method, path, body?)` | Call any `/api/v1/*` endpoint directly |
| `upload_image(image_base64, filename?)` | Upload an image; returned `path` → `image_start` in `generate()` params |
| `upload_audio(audio_base64, filename?)` | Upload audio (or video → audio extraction), always returns a WAV path |
| `download_model(model_type)` | Pre-download model weights in the background |
| `model_download_status()` | Status of running pre-downloads |
| `output_metadata(name)` | Prompt/seed/settings of an output file |
| `upscale(video_path, params?)` | Upscale a clip, returns `job_id` |
| `revoice(video_path, voice_ref_paths, params?)` | Voice conversion on a clip, returns `job_id` |
| `director_start(params)` | Start a Director pipeline, returns `pipeline_id` |
| `director_status(pid)` | Poll a Director pipeline |
| `director_stop(pid)` | Cancel a Director pipeline |
| `list_director_pipelines()` | Saved pipeline states |
| `list_loras(model_type)` | Installed LoRAs for a model type |
| `civitai_search(query)` | Search CivitAI for LoRAs |
| `civitai_download(params)` | Download a LoRA/checkpoint from CivitAI |

**Text and long-form writing**

| Tool | Purpose |
|---|---|
| `chat(message, thread_id?)` | Conversation with memory; omit the id to start one |
| `list_chat_threads()` | Existing conversations |
| `list_text_models()` | Model catalogs for outline vs prose |
| `story_start(premise, min_pages, params?)` | Begin a long-form story |
| `story_status(id)` | Progress, outline and every chapter's text |
| `list_stories()` | Story summaries |
| `story_stop(id)` | Stop writing, keep what exists |
| `story_extend(id, n)` | Append chapters |
| `story_regenerate_chapter(id, i, instruction?)` | Rewrite one chapter |
| `story_edit_chapter(id, i, text)` | Replace a chapter's text |
| `story_export(id, fmt)` | Write md/txt into the workspace |

**AudioBooks**

| Tool | Purpose |
|---|---|
| `audiobook_create(title, language)` | New project |
| `list_audiobooks()` / `audiobook_get(id)` | Summaries / full project |
| `audiobook_import(id, path, ...)` | Import a document as chapters |
| `audiobook_update(id, changes)` | Patch chapters, voices, sfx, music |
| `audiobook_plan(id, chapter_index)` | Verify readiness before rendering |
| `audiobook_render(id, ...)` | Render a chapter or the whole book |

**The escape hatch.** `api_request(method, path, body?)` reaches every
`/api/v1` endpoint, and `api_request("GET", "/openapi.json")` returns the
full schema, so anything without a dedicated tool is still available.

Typical agent flows:

- Media: `list_models` -> `generate` -> poll `job_status` -> `get_output_url`
- Story: `story_start` -> poll `story_status` -> `story_export`
- Audiobook: `audiobook_create` -> `audiobook_import` -> `audiobook_update`
  (voices) -> `audiobook_plan` -> `audiobook_render` -> poll `job_status`

Generation takes minutes to hours; the first use of a model also downloads
its weights (potentially many GB), so poll patiently rather than treating a
slow call as a failure.

`api_request` is the escape hatch for everything without a dedicated tool:
it accepts GET/POST/PUT/DELETE against any `/api/v1/*` path (plus
`/openapi.json` so agents can discover the full schema first).

### Authentication

By default `/mcp` is open (localhost use). Set the `MUSEFORGE_API_TOKEN`
environment variable (see docker-compose.yml) and every MCP request must
carry `Authorization: Bearer <token>` — clients that support headers:

```bash
claude mcp add --transport http museforge http://localhost:7861/mcp \
  --header "Authorization: Bearer <token>"
```

The Settings dialog (**API & MCP** tab) shows the endpoint URL, ready-made
client configs and whether a token is required on this instance.

The MCP layer is a thin wrapper over the REST API (see
`app/services/mcp_server.py`) — anything not covered by a tool can be done
against `/api/v1` directly.
