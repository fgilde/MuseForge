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

Beyond that, the API covers Director pipelines (`/director/*`), LoRA
management and the CivitAI browser (`/loras/*`, `/civitai/*`), recipes,
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

Typical agent flow: `list_models` → `generate` → poll `job_status` → fetch
`get_output_url`. Generation takes minutes; the first use of a model also
downloads its weights (potentially many GB).

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
