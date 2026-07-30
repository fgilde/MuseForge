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
| `POST /story/stories/{id}/stop-operation` | Stop a running analysis, translation or rewrite. The story keeps its status |
| `POST /story/stories/{id}/extend` | Append N chapters |
| `POST /story/stories/{id}/chapters/{i}/regenerate` | Rewrite a chapter, optional `instruction` |
| `PUT /story/stories/{id}/chapters/{i}` | Save a manual edit |
| `POST /story/stories/{id}/export` | Write `.md`/`.txt` into the workspace |
| `GET /story/languages` · `GET /story/estimate` | Writing languages; pages to words/chapters |
| `POST /story/stories/{id}/translate` | Translate the whole story; the original stays |
| `POST /story/stories/{id}/chapters/{i}/retranslate` | Re-translate one chapter |
| `POST /story/stories/{id}/chapters/{i}/rewrite` | Propose a rewrite of an exact passage |
| `POST /story/stories/{id}/chapters/{i}/apply-rewrite` | Apply a reviewed rewrite |
| `POST /story/stories/{id}/chapters` · `DELETE …/{i}` | Insert (empty or AI-written) / delete a chapter |
| `POST /story/stories/{id}/analyze` | Characters, dialogue map, timeline, plot holes |
| `GET /story/export-formats` | Which of md/txt/docx/pdf this install can produce |
| `GET /story/stories/{id}/download` | Download as md/txt/docx/pdf, whole or per-chapter ZIP |
| `GET /story/stories/{id}/chapters/{i}/download` | Download one chapter |

Chat and story generations stream into per-`stream_id` slots
(`chat-<tid>`, `story-<sid>-outline`, `story-<sid>-ch<i>`), so several can
run without overwriting each other's output.

### AudioBook Creator

| Endpoint | Purpose |
|---|---|
| `GET/POST /audiobook/projects` | List or create projects |
| `GET/PUT/DELETE /audiobook/projects/{id}` | Read, patch, delete. `PUT` is the only write path |
| `POST /audiobook/projects/{id}/import` | Import txt/md/docx/pdf/epub as chapters |
| `POST /audiobook/from-story` | Create a project from a written story, keeping its chapters. `lang` picks a translation |
| `POST /audiobook/projects/{id}/plan` | Dry-run the TTS mapping; returns blocking `errors` and `ready` |
| `POST /audiobook/projects/{id}/assets/{sfx\|music}` | Add an effect or music bed, generating its audio |
| `POST /audiobook/projects/{id}/assets/{kind}/{aid}/generate` | Generate audio for an asset that already exists |
| `DELETE /audiobook/projects/{id}/assets/{kind}/{aid}` | Delete an asset and unlink it everywhere |
| `POST /audiobook/projects/{id}/suggest-cast` | LLM proposals: speaker, emotion and effects per run |
| `POST /audiobook/projects/{id}/apply-cast` | Apply a reviewed subset of those proposals |
| `POST /audiobook/projects/{id}/suggest-split` | LLM proposals for where a chapter should break |
| `POST /audiobook/projects/{id}/apply-split` | Split a chapter at the given break points |
| `POST /audiobook/projects/{id}/render` | Render a chapter or the whole book. Returns `{job_id}` |
| `GET /audiobook/sfx-library` | Effects already in the workspace, ready to reuse |
| `POST /audiobook/projects/{id}/assets/sfx/adopt` | Adopt an existing audio file as an effect |
| `GET /audiobook/voice-presets` | Voice starting points and audition sample lines |
| `POST /audiobook/projects/{id}/voices/{vid}/preview` | Audition a voice in the project |
| `POST /audiobook/projects/{id}/voices/import` | Copy a library voice into the project |
| `POST /audiobook/projects/{id}/preview-passage` | Speak one passage to check a voice. Pass `block_id` to hear it under that block's ambience/music |

A project is `chapters[] -> blocks[] -> runs[]`, where each run carries its
voice binding and optional emotion. Runs, not character offsets, so voice
assignments cannot drift when the text is edited. Always `plan` before
`render`: it reports paragraphs with no voice and models that need a
reference clip, which would otherwise fail a render minutes in. Rendered
speech runs are cached by content and seed, so re-rendering after a small
edit only re-voices what changed.

The two LLM passes (`suggest-cast`, `suggest-split`) return proposals and
never write: every id they mention is checked against the project first, and
invented ids are dropped and counted rather than applied to whatever happens
to match. Applying is a separate call with the subset you accept.

### Voices and activity

| Endpoint | Purpose |
|---|---|
| `GET/POST /voices` | The workspace voice library, shared by Speech and audiobooks |
| `PUT/DELETE /voices/{id}` | Patch or remove a voice |
| `POST /voices/{id}/preview` | Audition a voice; returns `{job_id}` |
| `POST /voices/{id}/freeze` | Keep the current audition as the voice's reference clip, switching to a cloning engine |
| `POST /voices/adopt` | Turn audio you already have into a cloning voice, in one call |
| `POST /voices/{id}/reroll` | New seed, audition cleared. `unfreeze` also drops the clip and returns to the described engine |
| `POST /voices/{id}/speak` | Read arbitrary text with a library voice (real output) |
| `GET /activity` | Everything running — jobs, Director pipelines, story runs and analyses, audiobook renders — each with the exact path that stops it |
| `POST /activity/stop-all` | Stop everything, reported per item |
| `GET /downloads/active` | Model file transfers in flight, with rate, ETA and stall age |
| `POST /downloads/cancel` | Stop a transfer (`file_id` as query or body; all of them without one) |

**Keeping a voice.** The engines that build a speaker from a written
description (Qwen3 Voice Design / Custom Voice, and KugelAudio without a clip)
resample the speaker on every render — measured, not assumed: three renders of
one line with one pinned seed came back as three different voices. So a
description alone cannot give you a voice you keep. The workflow is
`preview` until a take is good, then `freeze` it: that take becomes the voice's
`reference_path` and the engine switches to a cloning one, after which every
passage is spoken by the same person.

Every voice also carries a fixed `seed`, copied into an audiobook with the
voice. Its job is cache correctness — an unchanged passage sends the identical
request and is reused rather than re-voiced. It does not pin the voice.

A voice references its recording rather than copying it, so the same audio can
back several voices — and a deleted file surfaces as `reference_missing` with
`ready: false` instead of failing mid-render. Importing a voice into an
audiobook copies the configuration, so edits inside a book cannot rewrite the
shared entry.

**A voice you can keep starts from a recording.** `POST /voices/adopt` takes
any audio already on the server — a workspace output, an upload, a file in
the workspace — and binds it to a cloning engine, so the clip carries the
timbre and every passage is spoken by the same person. Describing a voice
instead is a search, not a result: audition, then `/freeze` the take you
want, or `/reroll` for another. Audition lines are a paragraph per language
(`GET /audiobook/voice-presets`) because the audition is also what gets
frozen, and cloning wants 10-30 seconds.

### Blueprints

One-click presets, in four kinds. `kind` on each card says what it sets up and
which call the payload belongs to — nothing is applied by reading one.

| Endpoint | Purpose |
|---|---|
| `GET /recipes` | Blueprint cards: `kind`, `kind_label`, name, description, thumbnail |
| `GET /recipes/{id}` | One blueprint in full, including its kind's payload |
| `GET /recipes/{id}/thumbnail` | Preview image, when the blueprint has one |
| `POST /recipes/save-from-output` | Save a finished generation as a blueprint |
| `POST /recipes/import` · `DELETE /recipes/{id}` | Import a blueprint file, delete a user one |

| `kind` | Payload | Use it with |
|---|---|---|
| `generation` | `model_type`, `params`, `prompt_example`, `loras[]` | `POST /generate` |
| `sfx` | same, with `params.MMAudio_prompt` | `POST /generate` (lands in Audio/SFX) |
| `story` | `story{premise, genre, pov, tense, min_pages, …}` | `POST /story/stories` |
| `voice` | `voice{model_type, params, language, …}` | `POST /voices` |

LoRAs are referenced by pointer (`filename`, `multiplier`, `source_url`), never
bundled, so a blueprint carries no weights. A `voice` blueprint built on a
description-only engine still has to be auditioned and frozen — see the voices
section for why.

### Everything else

| Endpoint | Purpose |
|---|---|
| `GET /outputs/{name}/prompts` | Every prompt behind an output, oldest first: the clips of a multi-clip run and the chain it was extended from |
| `GET /loras/{model_type}` | LoRAs this model can load — per architecture, so it is not the full set |
| `GET /loras/installed` | Every installed LoRA with its directory and CivitAI metadata |
| `GET /loras/directory-models` | `{directory: [model_type, …]}` — which models load from each LoRA directory. A directory mapping to `[]` is a dead end |

LoRAs are stored per architecture under `loras/<dir>`. A file downloaded
while another model was selected therefore never appears for the current
one — `POST /generate` rejects an `activated_loras` entry that is not
installed for the requested model and names what is.

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
| `audiobook_add_effect(id, prompt, ...)` | Add a sound effect, generated from a prompt |
| `audiobook_add_music(id, prompt, ...)` | Add a background music bed |
| `audiobook_suggest_cast(id, chapter)` | Propose speakers, emotions and effects |
| `audiobook_apply_cast(id, chapter, suggestions)` | Apply a reviewed subset |
| `audiobook_suggest_split(id, chapter, words)` | Propose chapter break points |
| `audiobook_apply_split(id, chapter, splits)` | Split a chapter |
| `audiobook_render(id, ...)` | Render a chapter or the whole book |
| `audiobook_sfx_library()` / `audiobook_adopt_effect(...)` | Find and reuse existing effects |
| `audiobook_import_voice(id, voice_id)` | Copy a library voice into a project |

**Voices, activity and story editing**

| Tool | Purpose |
|---|---|
| `list_voices()` / `create_voice(...)` / `preview_voice(id)` | The shared voice library |
| `speak_with_voice(id, text, ...)` | Read text aloud with a library voice |
| `audiobook_preview_passage(id, text, ...)` | Hear one passage before rendering |
| `list_activity()` / `stop_all_activity()` | See and stop everything running |
| `story_translate(id, language)` | Add a translation |
| `story_rewrite_passage(...)` / `story_apply_rewrite(...)` | Propose and apply a passage rewrite |
| `story_insert_chapter(id, at, write?)` | Insert a chapter, optionally AI-written |
| `story_analyze(id)` | Characters, dialogue map, timeline, plot holes |
| `story_download_url(id, fmt, ...)` | Download path for md/txt/docx/pdf |

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
