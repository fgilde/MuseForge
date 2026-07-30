import { useEffect, useState } from 'react'
import { Check, Copy, Plug, ShieldCheck, ShieldOff } from 'lucide-react'

/**
 * "API & MCP" settings tab — shows where the MCP endpoint lives, how to
 * connect an AI agent to it, the available tools, and whether the
 * instance requires a bearer token (MUSEFORGE_API_TOKEN).
 *
 * Static except for one status fetch: the tool table documents the
 * surface defined in app/services/mcp_server.py — keep the two in sync
 * when adding tools.
 */

const TOOLS: Array<[string, string]> = [
  ['list_models', 'Discover model types and their capabilities'],
  ['model_defaults', 'Inspect tunable parameters of a model'],
  ['generate', 'Submit a video/image/audio job, returns job_id'],
  ['job_status', 'Poll a job until completed/failed'],
  ['list_jobs', 'Recent generation jobs'],
  ['cancel_job', 'Cancel a queued or running job'],
  ['list_outputs', 'Generated files in the active workspace'],
  ['get_output_url', 'Download URL for an output file'],
  ['enhance_prompt', 'Rewrite a rough prompt via the local LLM'],
  ['system_status', 'GPU/CUDA/disk readiness check'],
  ['api_request', 'Call any REST endpoint — escape hatch for the rest of the API'],
  ['upload_image', 'Upload a base64 image, use its path as generation start image'],
  ['upload_audio', 'Upload base64 audio (or video for audio extraction)'],
  ['download_model', 'Pre-download a model\'s weights in the background'],
  ['model_download_status', 'Status of running model pre-downloads'],
  ['output_metadata', 'Prompt/seed/settings metadata of an output file'],
  ['upscale', 'Upscale an existing clip, returns job_id'],
  ['revoice', 'Replace voices in a clip via SeedVC, returns job_id'],
  ['director_start', 'Start a Director pipeline (planning → images → video)'],
  ['director_status', 'Poll a Director pipeline'],
  ['director_stop', 'Cancel a running Director pipeline'],
  ['list_director_pipelines', 'Saved pipeline states in the workspace'],
  ['list_loras', 'Installed LoRAs for a model type'],
  ['civitai_search', 'Search CivitAI for LoRAs'],
  ['civitai_download', 'Download a LoRA/checkpoint from CivitAI'],
  ['chat', 'Conversation with memory — omit thread_id to start one'],
  ['list_chat_threads', 'Existing chat conversations'],
  ['list_text_models', 'Text model catalogs for outline vs prose'],
  ['story_start', 'Begin a long-form story from a premise'],
  ['story_status', 'Progress, outline and every chapter of a story'],
  ['list_stories', 'Story summaries in the workspace'],
  ['story_stop', 'Stop writing, keeping what exists'],
  ['story_extend', 'Append chapters to a finished story'],
  ['story_regenerate_chapter', 'Rewrite one chapter, optionally steered'],
  ['story_edit_chapter', 'Replace the text of one chapter'],
  ['story_export', 'Write the story to md/txt in the workspace'],
  ['audiobook_create', 'New audiobook project'],
  ['list_audiobooks', 'Audiobook project summaries'],
  ['audiobook_get', 'Full project: chapters, voices, sfx, music'],
  ['audiobook_import', 'Import txt/md/docx/pdf/epub as chapters'],
  ['audiobook_update', 'Patch chapters, voices, sfx or music'],
  ['audiobook_plan', 'Verify a chapter is ready to render'],
  ['audiobook_add_effect', 'Add a sound effect generated from a prompt'],
  ['audiobook_add_music', 'Add a background music bed'],
  ['audiobook_suggest_cast', 'Propose speakers, emotions and effects'],
  ['audiobook_apply_cast', 'Apply a reviewed subset of cast proposals'],
  ['audiobook_suggest_split', 'Propose where a chapter should break'],
  ['audiobook_apply_split', 'Split a chapter at chosen break points'],
  ['audiobook_render', 'Render a chapter or the whole book'],
]

function CopyRow({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard blocked */ }
  }
  return (
    <div>
      <div className="text-[11px] text-text-secondary mb-1">{label}</div>
      <div className="flex items-center gap-2 bg-bg-tertiary border border-border rounded-lg px-3 py-2">
        <code className="text-[11px] text-text-primary flex-1 overflow-x-auto whitespace-pre">{value}</code>
        <button
          onClick={copy}
          className="p-1.5 rounded-md hover:bg-bg-hover text-text-secondary hover:text-text-primary transition-colors shrink-0"
          title="Copy"
        >
          {copied ? <Check size={14} className="text-indicator-success" /> : <Copy size={14} />}
        </button>
      </div>
    </div>
  )
}

export function McpPanel() {
  const [info, setInfo] = useState<{ mounted: boolean; token_required: boolean } | null>(null)

  useEffect(() => {
    fetch('/api/v1/mcp/info')
      .then(r => (r.ok ? r.json() : null))
      .then(setInfo)
      .catch(() => setInfo(null))
  }, [])

  const mcpUrl = `${window.location.origin}/mcp`
  const mounted = info?.mounted ?? false

  return (
    <div className="space-y-5">
      {/* Status */}
      <div className="flex items-center gap-2 text-xs">
        <Plug size={14} className={mounted ? 'text-indicator-success' : 'text-red-400'} />
        <span className="text-text-primary font-medium">
          MCP endpoint {mounted ? 'active' : info === null ? 'status unknown' : 'not available'}
        </span>
        {info?.token_required ? (
          <span className="flex items-center gap-1 text-indicator-success ml-auto">
            <ShieldCheck size={13} /> token required
          </span>
        ) : (
          <span className="flex items-center gap-1 text-text-muted ml-auto">
            <ShieldOff size={13} /> no token set
          </span>
        )}
      </div>

      <p className="text-xs text-text-secondary">
        MuseForge speaks the Model Context Protocol: AI agents (Claude Code,
        IDE agents, custom orchestrators) can list models, submit generation
        jobs, poll them and fetch the outputs — everything the UI can do.
      </p>

      <CopyRow label="MCP endpoint (streamable HTTP)" value={mcpUrl} />
      <CopyRow label="Connect from Claude Code" value={`claude mcp add --transport http museforge ${mcpUrl}`} />
      <CopyRow
        label="mcp.json style config"
        value={`{\n  "mcpServers": {\n    "museforge": { "type": "http", "url": "${mcpUrl}" }\n  }\n}`}
      />

      {/* Token */}
      <div className="text-xs text-text-secondary space-y-1.5">
        <div className="text-text-primary font-medium">Access token</div>
        <p>
          Set the <code className="text-[11px] bg-bg-tertiary px-1 py-0.5 rounded">MUSEFORGE_API_TOKEN</code>{' '}
          environment variable (e.g. in docker-compose.yml) to require{' '}
          <code className="text-[11px] bg-bg-tertiary px-1 py-0.5 rounded">Authorization: Bearer &lt;token&gt;</code>{' '}
          on every MCP request. Without it the endpoint is open to anyone who
          can reach this machine — fine on localhost, not on a shared network.
        </p>
      </div>

      {/* Tools */}
      <div className="space-y-1.5">
        <div className="text-xs text-text-primary font-medium">Available tools</div>
        <div className="border border-border rounded-lg overflow-hidden">
          {TOOLS.map(([name, desc], i) => (
            <div key={name} className={`flex items-baseline gap-3 px-3 py-1.5 text-[11px] ${i % 2 ? '' : 'bg-bg-tertiary/50'}`}>
              <code className="text-text-primary shrink-0 w-32">{name}</code>
              <span className="text-text-secondary">{desc}</span>
            </div>
          ))}
        </div>
      </div>

      <p className="text-xs text-text-muted">
        The full REST API behind these tools is documented at{' '}
        <a href="/docs" target="_blank" rel="noreferrer" className="text-accent-blue hover:underline">/docs</a>.
      </p>
    </div>
  )
}
