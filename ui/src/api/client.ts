const BASE = ''  // same origin in production; Vite proxy handles /api in dev

export interface ApiModel {
  model_type: string
  name: string
  family: string
  architecture: string
  is_i2v: boolean
  is_t2v: boolean
  guidance_max_phases: number
  fps: number
  is_downloaded?: boolean
  // True when the model JSON declares `"nsfw_only": true` in its
  // model block. The UI hides it from selectors and the visibility
  // settings unless servicesConfig.nsfw_mode is enabled.
  nsfw_only?: boolean
}

export interface ApiFamily {
  id: string
  label: string
  order: number
}

export interface ApiResolution {
  label: string
  value: string
}

export interface ApiOutput {
  name: string
  type: 'video' | 'image' | 'audio'
  mode: string | null
  favorite?: boolean
  size: number
  created_at: number
  url: string
  /** Edit-mode sub-classification (retake / inpaint / outpaint / restyle /
   *  edit_anything). Field added as a recovery stub after a git
   *  filter-repo reset wiped the original Stream C/D work that
   *  introduced it. Optional so the type compiles even when the
   *  backend hasn't been updated to emit this yet. */
  edit_sub_mode?: string | null
}

export interface ApiJobStatus {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  step: number
  total_steps: number
  phase: string
  message: string
  output_files: string[]
  error: string | null
  /** Present only on failed jobs that look like CUDA OOMs.
   *  See `OomInfo` in types/index.ts. */
  oom_info?: import('../types').OomInfo | null
}

// --- Models & Families ---

export async function fetchModels(): Promise<{ families: ApiFamily[]; models: ApiModel[] }> {
  const res = await fetch(`${BASE}/api/v1/models`)
  if (!res.ok) throw new Error('Failed to fetch models')
  return res.json()
}

// Re-scan defaults/ + finetunes/ on the server so a newly-imported checkpoint
// appears in the model list without a restart. Returns model_types that appeared.
export async function reloadModels(): Promise<{ status: string; model_count: number; added: string[] }> {
  const res = await fetch(`${BASE}/api/v1/models/reload`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to reload models')
  return res.json()
}

export async function deleteModel(modelType: string): Promise<{ deleted: string[]; model_type: string }> {
  const res = await fetch(`${BASE}/api/v1/models/${encodeURIComponent(modelType)}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('Failed to delete model')
  return res.json()
}

export type ModelDownloadStatus = 'downloading' | 'completed' | 'failed'

export async function downloadModel(modelType: string): Promise<{ status: ModelDownloadStatus; model_type: string }> {
  const res = await fetch(`${BASE}/api/v1/models/${encodeURIComponent(modelType)}/download`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to start model download')
  return res.json()
}

export interface ModelDownload {
  status: ModelDownloadStatus
  error: string | null
  /** Epoch seconds when the download was started. */
  started?: number
  /** Human-readable model name for banners. */
  model_name?: string
  /** Number of files to fetch — null until the file list is resolved. */
  files_total?: number | null
  files_done?: number
  /** Basename of the file currently in flight. */
  current_file?: string | null
  /** Estimated total transfer size in bytes, null when it couldn't be
   *  probed. Covers the model/module/text-encoder files only. */
  bytes_total?: number | null
}

export async function fetchModelDownloads(): Promise<{ downloads: Record<string, ModelDownload> }> {
  const res = await fetch(`${BASE}/api/v1/models/downloads/status`)
  if (!res.ok) throw new Error('Failed to fetch model download status')
  return res.json()
}

// --- Resolutions ---

export async function fetchResolutions(): Promise<ApiResolution[]> {
  const res = await fetch(`${BASE}/api/v1/resolutions`)
  if (!res.ok) throw new Error('Failed to fetch resolutions')
  const data = await res.json()
  return data.resolutions
}

// --- Model Defaults ---

export async function fetchDefaults(modelType: string): Promise<Record<string, unknown>> {
  const res = await fetch(`${BASE}/api/v1/defaults/${encodeURIComponent(modelType)}`)
  if (!res.ok) throw new Error(`Failed to fetch defaults for ${modelType}`)
  return res.json()
}

// --- Generation ---

export async function submitGeneration(params: Record<string, unknown>): Promise<{ job_id: string }> {
  const res = await fetch(`${BASE}/api/v1/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Generation failed' }))
    throw new Error(err.detail || 'Generation failed')
  }
  return res.json()
}

export async function fetchJobStatus(jobId: string): Promise<ApiJobStatus> {
  const res = await fetch(`${BASE}/api/v1/status/${encodeURIComponent(jobId)}`)
  if (!res.ok) throw new Error('Failed to fetch job status')
  return res.json()
}

// --- Music: LLM song writer (Music mode Simple) ---

export async function writeSong(params: {
  description: string
  instrumental?: boolean
  seed?: number
  reference_image_path?: string
}): Promise<{ style: string; lyrics: string; raw: string }> {
  const res = await fetch(`${BASE}/api/v1/llm/write-song`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Song writing failed' }))
    throw new Error(err.detail || 'Song writing failed')
  }
  return res.json()
}

// Director Music Video: generate a music track (writes the song first if only
// a description is given) and return the ABSOLUTE audio path so it can flow
// straight into the existing analyze → plan-structure → pipeline chain.
export async function generateMusic(params: {
  description?: string
  style?: string
  lyrics?: string
  instrumental?: boolean
  duration_seconds?: number
  reference_image_path?: string
  model_type?: string
  seed?: number
  workspace?: string
}): Promise<{ audio_path: string; filename: string; style: string; lyrics: string }> {
  const res = await fetch(`${BASE}/api/v1/director/generate-music`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Music generation failed' }))
    throw new Error(err.detail || 'Music generation failed')
  }
  return res.json()
}

// --- Tools: standalone post-processing on an existing clip ---

export async function submitToolUpscale(params: {
  video_path: string
  method?: string
  seed?: number
  workspace?: string
}): Promise<{ job_id: string }> {
  const res = await fetch(`${BASE}/api/v1/tools/upscale`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Upscale failed' }))
    throw new Error(err.detail || 'Upscale failed')
  }
  return res.json()
}

export async function submitToolRevoice(params: {
  video_path: string
  voice_ref_paths: string[]
  mode?: 'single' | 'two'
  diffusion_steps?: number
  cfg_rate?: number
  workspace?: string
}): Promise<{ job_id: string }> {
  const res = await fetch(`${BASE}/api/v1/tools/revoice`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Revoice failed' }))
    throw new Error(err.detail || 'Revoice failed')
  }
  return res.json()
}

// --- Workspaces ---

export interface Workspace {
  name: string
  path: string
  file_count?: number
}

export async function fetchWorkspaces(): Promise<{ workspaces: Workspace[]; active: string }> {
  const res = await fetch(`${BASE}/api/v1/workspaces`)
  if (!res.ok) throw new Error('Failed to fetch workspaces')
  return res.json()
}

export async function setActiveWorkspace(name: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/workspaces/active`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (!res.ok) throw new Error('Failed to switch workspace')
}

export async function createWorkspace(name: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/workspaces`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to create workspace' }))
    throw new Error(err.detail || 'Failed to create workspace')
  }
}

export async function deleteWorkspace(name: string): Promise<{ switched_to_default: boolean; files_deleted: number }> {
  const res = await fetch(`${BASE}/api/v1/workspaces/${encodeURIComponent(name)}`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to delete workspace' }))
    throw new Error(err.detail || 'Failed to delete workspace')
  }
  return res.json()
}

// --- Job Management ---

export async function cancelJob(jobId: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/cancel/${encodeURIComponent(jobId)}`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to cancel job')
}

export async function fetchActiveJobs(): Promise<{ jobs: Array<{
  job_id: string; status: string; progress: number; step: number;
  total_steps: number; phase: string; message: string; output_files: string[];
  error: string | null; created_at: number;
}> }> {
  const res = await fetch(`${BASE}/api/v1/jobs`)
  if (!res.ok) throw new Error('Failed to fetch jobs')
  return res.json()
}

// --- Move to Workspace ---

export async function moveOutput(name: string, workspace: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/outputs/${encodeURIComponent(name)}/move`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ workspace }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Move failed' }))
    throw new Error(err.detail || 'Move failed')
  }
}

// --- Favorites ---

export async function toggleFavorite(name: string): Promise<{ name: string; favorite: boolean }> {
  const res = await fetch(`${BASE}/api/v1/favorites/${encodeURIComponent(name)}`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to toggle favorite')
  return res.json()
}

// --- Outputs ---

export async function fetchOutputs(limit = 0, offset = 0, opts?: { favoritesOnly?: boolean; multiclipOnly?: boolean; search?: string; workspace?: string }): Promise<{ outputs: ApiOutput[]; total: number }> {
  const params = new URLSearchParams()
  if (limit > 0) params.set('limit', String(limit))
  if (offset > 0) params.set('offset', String(offset))
  if (opts?.favoritesOnly) params.set('favorites_only', 'true')
  if (opts?.multiclipOnly) params.set('multiclip_only', 'true')
  if (opts?.search) params.set('search', opts.search)
  // "__uploads__" browses the uploads folder (virtual Uploads view)
  if (opts?.workspace) params.set('workspace', opts.workspace)
  const qs = params.toString()
  const res = await fetch(`${BASE}/api/v1/outputs${qs ? '?' + qs : ''}`)
  if (!res.ok) throw new Error('Failed to fetch outputs')
  const data = await res.json()
  return { outputs: data.outputs, total: data.total ?? data.outputs.length }
}

export function getFileUrl(filename: string): string {
  return `${BASE}/api/v1/file/${encodeURIComponent(filename)}`
}

export function getUploadUrl(filename: string): string {
  return `${BASE}/api/v1/uploads/${encodeURIComponent(filename)}`
}

export async function fetchOutputMetadata(name: string): Promise<import('../types').OutputMetadata> {
  // Retry with a per-attempt timeout. On a slow/high-latency link (e.g. the user
  // is remote over VPN) the request can stall long enough that a single attempt
  // hangs or is dropped by an intermediary; the old single-shot fetch then left
  // the caller with no metadata and the "Load Settings" button a silent no-op.
  const url = `${BASE}/api/v1/outputs/${encodeURIComponent(name)}/metadata`
  const ATTEMPTS = 3
  const PER_ATTEMPT_MS = 30000  // generous: the server may read embedded video metadata to recover a seed
  let lastErr: unknown = null
  for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), PER_ATTEMPT_MS)
    try {
      const res = await fetch(url, { signal: controller.signal })
      if (!res.ok) return { source: 'none', params: null }
      return await res.json()
    } catch (e) {
      lastErr = e
      // Diagnostic: AbortError = our per-attempt timeout fired (link too slow);
      // TypeError = network failure / dropped connection. Helps pinpoint a
      // "Load Settings does nothing over VPN" report.
      console.warn(`[LoadSettings] fetchOutputMetadata attempt ${attempt + 1}/${ATTEMPTS} failed:`,
                   (e as { name?: string })?.name || e)
      if (attempt < ATTEMPTS - 1) {
        await new Promise(r => setTimeout(r, 400 * (attempt + 1)))  // brief backoff before retry
      }
    } finally {
      clearTimeout(timer)
    }
  }
  throw lastErr  // all attempts failed — loadOutputMetadata's catch sets meta null
}

export async function deleteOutput(name: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/outputs/${encodeURIComponent(name)}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('Failed to delete output')
}

export async function rejoinClips(groupId: string, audioFile?: string): Promise<{ filename: string; clip_count: number }> {
  const res = await fetch(`${BASE}/api/v1/outputs/rejoin`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ group_id: groupId, audio_file: audioFile }),
  })
  if (!res.ok) throw new Error('Failed to rejoin clips')
  return res.json()
}

export async function fetchGroupClips(groupId: string): Promise<{ group_id: string; clips: Array<{ filename: string; index: number; total: number; prompt: string }> }> {
  const res = await fetch(`${BASE}/api/v1/outputs/group/${encodeURIComponent(groupId)}`)
  if (!res.ok) throw new Error('Failed to fetch group clips')
  return res.json()
}

// --- Director Pipeline ---

export interface PipelineStatus {
  id: string
  status: 'running' | 'paused' | 'completed' | 'failed' | 'cancelled'
  phase: 'planning' | 'polishing_prompts' | 'generating_images' | 'generating_video' | 'post_processing' | 'completed'
  auto_mode: boolean
  progress: { current: number; total: number; message: string; step: number; total_steps: number }
  clip_plans: Array<{ video_prompt: string; image_prompt: string }>
  clip_images: string[]
  output_files: string[]
  error: string | null
  /** Present only on failed pipelines that look like CUDA OOMs.
   *  See `OomInfo` in types/index.ts. */
  oom_info?: import('../types').OomInfo | null
  pause_reason: string | null
  llm_streaming: boolean
  /** Non-fatal warnings raised during the run — currently used for
   *  architecture-mismatch advisories when image LoRAs are dropped
   *  because they were trained for a different Flux variant than the
   *  active model (e.g. Flux 2 Dev LoRA on Klein 9B). The chat renders
   *  these inline so users see why some selected LoRAs weren't applied. */
  lora_warnings?: string[]
}

export async function startPipeline(params: Record<string, unknown>): Promise<{ pipeline_id: string }> {
  const res = await fetch(`${BASE}/api/v1/director/pipeline/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) throw new Error('Failed to start pipeline')
  return res.json()
}

export async function fetchPipelineStatus(pid: string): Promise<PipelineStatus> {
  const res = await fetch(`${BASE}/api/v1/director/pipeline/${encodeURIComponent(pid)}`)
  if (!res.ok) throw new Error('Failed to fetch pipeline status')
  return res.json()
}

export async function continuePipeline(pid: string, updates?: { clip_plans?: Array<{ video_prompt: string; image_prompt: string }> }): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/director/pipeline/${encodeURIComponent(pid)}/continue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates || {}),
  })
  if (!res.ok) throw new Error('Failed to continue pipeline')
}

export async function stopPipeline(pid: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/director/pipeline/${encodeURIComponent(pid)}/stop`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error('Failed to stop pipeline')
}

export async function resumePipeline(pid: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/director/pipeline/${encodeURIComponent(pid)}/resume`, {
    method: 'POST',
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: 'Failed to resume pipeline' }))
    throw new Error(body.detail || 'Failed to resume pipeline')
  }
}

// ── Recipes ──────────────────────────────────────────────────────────────

export interface RecipeLora {
  filename: string
  multiplier: string | number
  source_url?: string
  size_mb?: number
}

export interface RecipeCard {
  id: string
  name: string
  description: string
  mode: string
  model_type: string
  lora_count: number
  prompt_example: string
  nsfw: boolean
  source: 'bundled' | 'user'
  thumbnail_url: string | null
}

export interface Recipe extends RecipeCard {
  loras: RecipeLora[]
  params: Record<string, unknown>
}

export async function fetchRecipes(): Promise<{ recipes: RecipeCard[] }> {
  const res = await fetch(`${BASE}/api/v1/recipes`)
  if (!res.ok) throw new Error('Failed to load recipes')
  return res.json()
}

export async function fetchRecipe(id: string): Promise<Recipe> {
  const res = await fetch(`${BASE}/api/v1/recipes/${encodeURIComponent(id)}`)
  if (!res.ok) throw new Error('Recipe not found')
  return res.json()
}

export async function saveRecipeFromOutput(body: {
  output_name: string; name: string; description?: string; nsfw?: boolean
}): Promise<RecipeCard> {
  const res = await fetch(`${BASE}/api/v1/recipes/save-from-output`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Save failed' }))
    throw new Error(err.detail || 'Save failed')
  }
  return res.json()
}

export async function importRecipe(recipe: Record<string, unknown>): Promise<RecipeCard> {
  const res = await fetch(`${BASE}/api/v1/recipes/import`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(recipe),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Import failed' }))
    throw new Error(err.detail || 'Import failed')
  }
  return res.json()
}

export async function deleteRecipe(id: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/recipes/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Delete failed' }))
    throw new Error(err.detail || 'Delete failed')
  }
}

// ── System preflight ─────────────────────────────────────────────────────

export interface PreflightCheck {
  id: string
  level: 'error' | 'warn'
  message: string
}

export async function fetchPreflight(): Promise<{ ok: boolean; checks: PreflightCheck[] }> {
  const res = await fetch(`${BASE}/api/v1/system/preflight`)
  if (!res.ok) throw new Error('preflight failed')
  return res.json()
}

// ── Director Pipeline Dashboard ──────────────────────────────────────────

export async function fetchPipelineList(): Promise<{ pipelines: import('../types').PipelineListItem[] }> {
  const res = await fetch(`${BASE}/api/v1/director/pipelines`)
  if (!res.ok) throw new Error('Failed to fetch pipelines')
  return res.json()
}

export async function fetchSavedPipeline(pid: string): Promise<import('../types').SavedPipelineState> {
  const res = await fetch(`${BASE}/api/v1/director/pipelines/${encodeURIComponent(pid)}`, {
    cache: 'no-store',
  })
  if (!res.ok) throw new Error('Pipeline not found')
  return res.json()
}

export async function tagPipelineClip(pid: string, clipIndex: number, tag: string | null): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/director/pipelines/${encodeURIComponent(pid)}/clips/${clipIndex}/tag`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag }),
  })
  if (!res.ok) throw new Error('Failed to tag clip')
}

export async function startPipelineRepair(pid: string): Promise<{
  pipeline_id: string
  repair: import('../types').PipelineRepairState
}> {
  const res = await fetch(`${BASE}/api/v1/director/pipelines/${encodeURIComponent(pid)}/repair`, {
    method: 'POST',
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: 'Repair failed to start' }))
    throw new Error(err.error || err.detail || 'Repair failed to start')
  }
  return res.json()
}

export async function cancelPipelineRepair(pid: string): Promise<{
  pipeline_id: string
  repair: import('../types').PipelineRepairState
}> {
  const res = await fetch(`${BASE}/api/v1/director/pipelines/${encodeURIComponent(pid)}/repair/cancel`, {
    method: 'POST',
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: 'Repair cancel failed' }))
    throw new Error(err.error || err.detail || 'Repair cancel failed')
  }
  return res.json()
}

export async function rerunClipImage(pid: string, clipIndex: number, prompt?: string): Promise<{ filename: string; clip_index: number }> {
  const res = await fetch(`${BASE}/api/v1/director/pipelines/${encodeURIComponent(pid)}/clips/${clipIndex}/rerun-image`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt: prompt || undefined }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: 'Re-run failed' }))
    throw new Error(err.error || 'Re-run image failed')
  }
  return res.json()
}

export async function rerunClipVideo(pid: string, clipIndex: number, prompt?: string): Promise<{ filename: string; clip_index: number }> {
  const res = await fetch(`${BASE}/api/v1/director/pipelines/${encodeURIComponent(pid)}/clips/${clipIndex}/rerun-video`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt: prompt || undefined }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: 'Re-run failed' }))
    throw new Error(err.error || 'Re-run video failed')
  }
  return res.json()
}

export async function rejoinPipeline(pid: string): Promise<{ filename: string }> {
  const res = await fetch(`${BASE}/api/v1/director/pipelines/${encodeURIComponent(pid)}/rejoin`, {
    method: 'POST',
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: 'Rejoin failed' }))
    throw new Error(err.error || 'Rejoin failed')
  }
  return res.json()
}

export async function deletePipeline(pid: string): Promise<{ media_deleted: number; media_deferred: number }> {
  const res = await fetch(`${BASE}/api/v1/director/pipelines/${encodeURIComponent(pid)}`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Delete failed' }))
    throw new Error(err.detail || 'Delete failed')
  }
  return res.json()
}

// --- Director v2 ---

export interface DirectorV2PlanRequest {
  skill_type: string
  scene_description?: string
  story_description?: string
  clips?: unknown[]
  lyrics?: unknown[]
  bpm?: number
  reference_image_path?: string
  speaker_mappings?: Record<string, unknown>
  characters?: Array<{ name: string; description: string }>
  audio_path?: string
  target_duration?: number
  target_scenes?: number
  narrative_mode?: boolean
  fps?: number
  frames_steps?: number
  frames_minimum?: number
  concept?: string
  visual_style?: string
  platform?: string
  style?: string
  prompt_type?: string
  director_flags?: Record<string, boolean>
}

export interface DirectorV2PlanResponse {
  clip_plans: Array<{ video_prompt: string; image_prompt: string }>
  production_plan: Record<string, unknown>
  skill_type: string
}

export async function directorV2Plan(params: DirectorV2PlanRequest): Promise<DirectorV2PlanResponse> {
  const res = await fetch(`${BASE}/api/v1/director/v2/plan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Plan failed' }))
    throw new Error(err.detail || 'Director v2 plan failed')
  }
  return res.json()
}

// --- Presets ---

export interface GenerationPreset {
  id: string
  name: string
  mode: string
  model_type: string
  prompt: string
  activated_loras: string[]
  loras_multipliers: string
  lora_weights: Record<string, number[]>
  params: Record<string, unknown>
  created_at: number
}

export async function fetchPresets(): Promise<{ presets: GenerationPreset[] }> {
  const res = await fetch(`${BASE}/api/v1/presets`)
  if (!res.ok) throw new Error('Failed to fetch presets')
  return res.json()
}

export async function createPreset(preset: Omit<GenerationPreset, 'id' | 'created_at'>): Promise<GenerationPreset> {
  const res = await fetch(`${BASE}/api/v1/presets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(preset),
  })
  if (!res.ok) throw new Error('Failed to create preset')
  return res.json()
}

export async function deletePreset(id: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/presets/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('Failed to delete preset')
}

// --- LoRAs ---

export async function fetchLoras(modelType: string): Promise<{ loras: string[]; guidance_max_phases: number }> {
  const res = await fetch(`${BASE}/api/v1/loras/${encodeURIComponent(modelType)}`)
  if (!res.ok) throw new Error('Failed to fetch loras')
  return res.json()
}

// --- Model Options ---

export async function fetchModelOptions(modelType: string): Promise<import('../types').ModelOptions> {
  const res = await fetch(`${BASE}/api/v1/model-options/${encodeURIComponent(modelType)}`)
  if (!res.ok) throw new Error('Failed to fetch model options')
  return res.json()
}

// --- Retake ---

export async function submitRetake(params: {
  video_path: string; start_time: number; end_time: number;
  prompt: string; model_type: string;
  negative_prompt?: string; seed?: number; guidance_scale?: number;
  num_inference_steps?: number; retake_strength?: number; workspace?: string;
  retake_engine?: string; regenerate_audio?: boolean; resolution?: string;
  activated_loras?: string[]; loras_multipliers?: string;
}): Promise<{ job_id: string; status: string; retake_frames: string }> {
  const res = await fetch(`${BASE}/api/v1/retake`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Retake failed' }))
    throw new Error(err.detail || 'Retake failed')
  }
  return res.json()
}

// --- Inpaint ---

export async function segmentPreview(params: {
  video_path: string; text: string; frame_index?: number;
  start_time?: number; end_time?: number;
  full_video?: boolean; invert_mask?: boolean;
}): Promise<{ mask_preview: string; target: string; frame_index: number; masks_path?: string; prompt?: string; negative_prompt?: string }> {
  const res = await fetch(`${BASE}/api/v1/segment/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Segmentation failed' }))
    throw new Error(err.detail || 'Segmentation failed')
  }
  return res.json()
}

export async function submitInpaint(params: {
  video_path: string; description: string;
  sam_target?: string; invert_mask?: boolean;
  start_time?: number; end_time?: number;
  model_type: string; retake_strength?: number; resolution?: string;
  activated_loras?: string[]; loras_multipliers?: string;
  seed?: number; guidance_scale?: number;
  num_inference_steps?: number; negative_prompt?: string;
  mask_padding?: number; workspace?: string;
  masks_path?: string; stage2_steps?: number;
}): Promise<{ job_id: string; status: string }> {
  const res = await fetch(`${BASE}/api/v1/inpaint`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Inpaint failed' }))
    throw new Error(err.detail || 'Inpaint failed')
  }
  return res.json()
}

// --- Edit Anything ---
//
// Prompt-driven video edit using the Alissonerdx Edit Anything LoRA
// (https://huggingface.co/Alissonerdx/LTX-LoRAs). No mask required —
// the LoRA interprets Add/Remove/Replace/Style prompts directly.

export async function submitEditAnything(params: {
  video_path: string;
  prompt: string;
  model_type: string;
  start_time?: number;
  end_time?: number;
  /** LoRA strength (default 1.0, try 1.2 if edit is too weak). */
  lora_strength?: number;
  /** Retake strength — how much of the source latent structure is kept.
   *  Default 1.0 (full regen). Lower (0.5-0.8) preserves more of the
   *  original composition. */
  retake_strength?: number;
  negative_prompt?: string;
  seed?: number;
  guidance_scale?: number;
  num_inference_steps?: number;
  activated_loras?: string[];
  loras_multipliers?: string;
  workspace?: string;
}): Promise<{
  job_id: string;
  status: string;
  edit_range?: string;
  lora_filename?: string;
}> {
  const res = await fetch(`${BASE}/api/v1/edit-anything`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Edit Anything failed' }))
    throw new Error(err.detail || 'Edit Anything failed')
  }
  return res.json()
}

// --- Recast (SCAIL-2 Replace: swap a person for a reference character) ---

export async function submitRecast(params: {
  video_path: string;
  ref_image_path: string;
  /** Who to replace, as a SAM3 keyword ("woman", "man in red"). */
  target?: string;
  /** Optional scene/character description — a good one helps identity. */
  prompt?: string;
  start_time?: number;
  end_time?: number;
  model_type?: string;
  negative_prompt?: string;
  seed?: number;
  num_inference_steps?: number;
  guidance_scale?: number;
  workspace?: string;
}): Promise<{ job_id: string; status: string; frames?: number; target?: string }> {
  const res = await fetch(`${BASE}/api/v1/recast`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Recast failed' }))
    throw new Error(err.detail || 'Recast failed')
  }
  return res.json()
}

export async function recastPreview(params: {
  video_path: string;
  target?: string;
  time?: number;
  workspace?: string;
}): Promise<{ found: boolean; frame_index: number; preview: string }> {
  const res = await fetch(`${BASE}/api/v1/recast/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Preview failed' }))
    throw new Error(err.detail || 'Preview failed')
  }
  return res.json()
}

// --- Outpaint ---

export async function submitOutpaint(params: {
  video_path: string; prompt: string; model_type: string;
  pad_top?: number; pad_bottom?: number; pad_left?: number; pad_right?: number;
  resolution_preset?: 'auto' | '480p' | '540p' | '720p' | '1080p';
  source_preservation?: number;
  outpaint_lora_strength?: number;
  seed?: number;
  activated_loras?: string[]; loras_multipliers?: string;
  workspace?: string;
  // Recovery stubs — these fields were added by the Stream C/D outpaint
  // refinement work that got wiped by the git filter-repo reset. The
  // backend should already accept them (handler is server-side); these
  // signature additions just stop the TS build from complaining.
  preserve_source_audio?: boolean;
  lock_source_pixels?: boolean;
  trim_window_smear?: boolean;
  sliding_window_size?: number;
  sliding_window_overlap?: number;
  start_time?: number;
  end_time?: number;
}): Promise<{ job_id: string; status: string }> {
  const res = await fetch(`${BASE}/api/v1/outpaint`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Outpaint failed' }))
    throw new Error(err.detail || 'Outpaint failed')
  }
  return res.json()
}

// --- Blend ---

export async function submitBlend(params: {
  clip_a_path: string; clip_b_path: string;
  prompt?: string;
  model_type: string;
  blend_mode?: 'insert' | 'overlap'; overlap_sec?: number;
  seed?: number; activated_loras?: string[]; loras_multipliers?: string;
  workspace?: string;
  // Studio params inherited by the blend (progressive_pipeline,
  // num_inference_steps, guidance_scale, negative_prompt, etc.). Blend-
  // specific fields are overridden server-side.
  base_params?: Record<string, unknown>;
  // Blend-specific tuning overrides (take precedence over base_params)
  /** Seconds of A's overlap-zone start used as video_source for motion
   *  continuity (VE mode). 0 = pure SE. Default 1.0. */
  motion_prefix_sec?: number;
  /** Seconds of B's overlap-zone end used as video_end for motion continuity
   *  on the B side (via _append_suffix_entries in ltx2.py). 0 = single
   *  image_end anchor. Default 1.0. */
  motion_suffix_sec?: number;
  /** Strength of the VE anchor locks (video_source + image_end).
   *  1.0 = hard lock → averaging → crossfade. 0.5-0.8 = model invents
   *  motion between anchors. Default 1.0 server-side. */
  input_video_strength?: number;
  anchor_frames?: number;
  injection_strength?: number;
  num_inference_steps?: number;
  guidance_scale?: number;
  negative_prompt?: string;
  /** @deprecated no longer used; kept for back-compat with existing call sites */
  transition_sec?: number;
  /** @deprecated bell-curve weighting is applied automatically */
  strength_a?: number;
  /** @deprecated bell-curve weighting is applied automatically */
  strength_b?: number;
  /** @deprecated superseded by anchor_frames; kept for back-compat */
  denoise_strength?: number;
}): Promise<{ job_id: string; status: string; overlap_sec?: number; frames?: number }> {
  const res = await fetch(`${BASE}/api/v1/blend`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Blend failed' }))
    throw new Error(err.detail || 'Blend failed')
  }
  return res.json()
}

/** SAM (Inpaint) service status. Status values:
 *   ready / available — service running, model loaded or loading
 *   installed         — env installed but service not started; will
 *                        auto-start on demand
 *   not_installed     — SAM env doesn't exist; user must install
 *                        the SAM service (see the README's Inpaint
 *                        section) before Inpaint will work
 *   unavailable       — generic failure (service unhealthy, network)
 */
export async function samServiceStatus(): Promise<{
  status: string
  model_loaded: boolean
  error?: string
}> {
  const res = await fetch(`${BASE}/api/v1/sam/status`)
  if (!res.ok) return { status: 'unavailable', model_loaded: false }
  return res.json()
}

// --- Audio Mix ---

export async function mixAudio(tracks: { path: string; start_time: number; volume: number }[], workspace?: string): Promise<{ filename: string; path: string }> {
  const res = await fetch(`${BASE}/api/v1/audio/mix`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tracks, workspace }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Mix failed' }))
    throw new Error(err.detail || 'Mix failed')
  }
  return res.json()
}

// --- Upload ---

export async function uploadImage(file: File): Promise<{ filename: string; path: string; url: string; fps?: number; frame_count?: number }> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}/api/v1/upload`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    // Surface the server's reason (413 too large, unsupported audio, ...)
    // instead of a flat "Upload failed" — every caller shows this string.
    const err = await res.json().catch(() => ({ detail: 'Upload failed' }))
    throw new Error(err.detail || 'Upload failed')
  }
  return res.json()
}

/** `/api/v1/upload` is not image-only — it stores any file and hands back a
 *  server-side path. The audiobook importer needs exactly that for
 *  txt/md/docx/pdf/epub, so it reuses this endpoint under a truthful name. */
export const uploadFile = uploadImage

// --- System Config ---

export async function fetchSystemConfig(): Promise<import('../types').SystemConfig> {
  const res = await fetch(`${BASE}/api/v1/system-config`)
  if (!res.ok) throw new Error('Failed to fetch system config')
  return res.json()
}

export async function scanModelFolders(): Promise<{ candidates: import('../types').ModelFolderCandidate[] }> {
  const res = await fetch(`${BASE}/api/v1/model-folders/scan`)
  if (!res.ok) throw new Error('Failed to scan for model folders')
  return res.json()
}

export async function updateSystemConfig(
  partial: Partial<import('../types').SystemConfig>
): Promise<{ status: string; updated: Record<string, unknown> }> {
  const res = await fetch(`${BASE}/api/v1/system-config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(partial),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Update failed' }))
    throw new Error(err.detail || 'Update failed')
  }
  return res.json()
}

// --- Performance Auto-Tune ---

/** Read the user's current hardware + the auto-tune recommendation
 *  for it. Backs the AutoPerformanceCard readout. Always succeeds —
 *  on systems without CUDA, the response includes a "no GPU detected"
 *  recommendation rather than a 500. */
export async function fetchSystemDetect(): Promise<import('../types').SystemDetectResponse> {
  const res = await fetch(`${BASE}/api/v1/system-detect`)
  if (!res.ok) throw new Error('Failed to fetch hardware detection')
  return res.json()
}

/** Live CPU / RAM / GPU + loaded-model telemetry for the hardware
 *  status indicators. Cheap enough to poll every ~2s. */
export async function fetchSystemStats(): Promise<import('../types').SystemStats> {
  const res = await fetch(`${BASE}/api/v1/system-stats`)
  if (!res.ok) throw new Error('Failed to fetch system stats')
  return res.json()
}

/** Manually unload the resident generation model (and LLM) to free
 *  VRAM/RAM. Models stay loaded between generations by design; this is
 *  the explicit opt-out. 409s when a generation or Director run is
 *  active. Returns which models were released. */
export async function releaseModels(): Promise<{ released: string[] }> {
  const res = await fetch(`${BASE}/api/v1/system/release-model`, { method: 'POST' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Unload failed' }))
    throw new Error(err.detail || 'Unload failed')
  }
  return res.json()
}

/** Apply the recommended settings to wgp_config.json. Used by both
 *  the "Re-detect" button (refreshes after hardware change) and the
 *  auto-tune toggle going from off → on. Server-side this is a single
 *  call: re-runs detection, writes recommendation, sets
 *  services.auto_performance=true, applies runtime side effects. */
export async function applySystemDetect(): Promise<import('../types').SystemDetectApplyResponse> {
  const res = await fetch(`${BASE}/api/v1/system-detect/apply`, {
    method: 'POST',
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Apply failed' }))
    throw new Error(err.detail || 'Apply failed')
  }
  return res.json()
}

// --- Services Config ---

export async function fetchServicesConfig(): Promise<import('../types').ServicesConfig> {
  const res = await fetch(`${BASE}/api/v1/services-config`)
  if (!res.ok) throw new Error('Failed to fetch services config')
  return res.json()
}

export async function updateServicesConfig(
  partial: Partial<import('../types').ServicesConfig>
): Promise<{ status: string; updated: Record<string, unknown> }> {
  const res = await fetch(`${BASE}/api/v1/services-config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(partial),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Update failed' }))
    throw new Error(err.detail || 'Update failed')
  }
  return res.json()
}

// --- LLM Service ---

export async function fetchLlmStatus(): Promise<import('../types').LlmStatus> {
  const res = await fetch(`${BASE}/api/v1/llm/status`)
  if (!res.ok) throw new Error('Failed to fetch LLM status')
  return res.json()
}

export async function loadLlm(
  params?: { model_id?: string; device?: string }
): Promise<import('../types').LlmStatus & { status: string }> {
  const res = await fetch(`${BASE}/api/v1/llm/load`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params || {}),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Load failed' }))
    throw new Error(err.detail || 'Load failed')
  }
  return res.json()
}

export async function unloadLlm(): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/llm/unload`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to unload LLM')
}

export async function fetchLlmModels(): Promise<{ models: import('../types').LlmModelOption[] }> {
  const res = await fetch(`${BASE}/api/v1/llm/models`)
  if (!res.ok) throw new Error('Failed to fetch LLM models')
  return res.json()
}

export async function llmEnhancePrompt(params: {
  prompt: string
  mode?: string
  model_type?: string
  temperature?: number
  image_path?: string
  image_paths?: string[]
  duration_seconds?: number
  window_count?: number
  window_size_seconds?: number
  activated_loras?: string[]
  tts_enhance_mode?: string
  tts_voice_count?: number
  max_new_tokens?: number
}): Promise<{ original: string; enhanced: string }> {
  const res = await fetch(`${BASE}/api/v1/llm/enhance-prompt`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Enhancement failed' }))
    throw new Error(err.detail || 'Enhancement failed')
  }
  return res.json()
}

// --- Chat threads (Text mode) ---
//
// Conversations live server-side (one JSON per thread in the workspace dir),
// so a reload or a second tab sees the same state. `POST .../messages` runs
// SYNCHRONOUSLY until the reply is complete — that can take minutes when the
// LLM has to load (or download) first, so no request here gets an
// AbortController timeout. Poll `getLlmStreamStatus('chat-<tid>')` alongside
// it to render tokens as they arrive.

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  at: number
  stream_id?: string
}

export interface ChatThreadSummary {
  id: string
  title: string
  model_id: string
  created_at: number
  updated_at: number
  message_count: number
  preview: string
}

export interface ChatThread {
  version: number
  id: string
  title: string
  system_prompt: string
  model_id: string
  created_at: number
  updated_at: number
  messages: ChatMessage[]
}

export async function fetchChatThreads(): Promise<{ threads: ChatThreadSummary[] }> {
  const res = await fetch(`${BASE}/api/v1/chat/threads`)
  if (!res.ok) throw new Error('Failed to load chats')
  return res.json()
}

export async function createChatThread(body: { title?: string; system_prompt?: string; model_id?: string } = {}): Promise<ChatThread> {
  const res = await fetch(`${BASE}/api/v1/chat/threads`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error('Failed to create chat')
  return res.json()
}

/** Full thread with messages. Returns null when the thread is gone (404). */
export async function fetchChatThread(tid: string): Promise<ChatThread | null> {
  const res = await fetch(`${BASE}/api/v1/chat/threads/${tid}`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error('Failed to load chat')
  return res.json()
}

export async function updateChatThread(
  tid: string,
  patch: { title?: string; system_prompt?: string; model_id?: string },
): Promise<ChatThread> {
  const res = await fetch(`${BASE}/api/v1/chat/threads/${tid}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new Error('Failed to update chat')
  return res.json()
}

export async function deleteChatThread(tid: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/chat/threads/${tid}`, { method: 'DELETE' })
  if (!res.ok && res.status !== 404) throw new Error('Failed to delete chat')
}

export async function sendChatMessage(tid: string, body: {
  content: string
  images?: string[]
  max_new_tokens?: number
  temperature?: number
  top_p?: number
}): Promise<{ message: ChatMessage; thread_id: string; stream_id: string }> {
  const res = await fetch(`${BASE}/api/v1/chat/threads/${tid}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Generation failed' }))
    throw new Error(err.detail || 'Generation failed')
  }
  return res.json()
}

// --- Storywriter (Text mode → Story) ---
//
// Long-form generation runs as a server-side worker (same shape as the
// Director pipeline): POST returns immediately with a story_id, the UI polls
// GET .../{sid} for state and `getLlmStreamStatus('story-<sid>-<pass>')` for
// the tokens of the pass currently running.
//
// Stream ids are deterministic: `story-<sid>-outline` for the planning pass
// and `story-<sid>-ch<index>` for a chapter's prose pass.

/** Same five fields the Director's progress dict carries. */
export interface StoryProgress {
  current: number
  total: number
  message: string
  step: number
  total_steps: number
}

export type StoryStatus = 'queued' | 'planning' | 'writing' | 'paused' | 'completed' | 'failed' | 'cancelled'

export interface StoryParams {
  premise: string
  title?: string
  /** Language the story itself is written in (BCP-47-ish code, "en" default).
   *  Translations live per chapter under `translations`. */
  language?: string
  genre?: string
  tone?: string
  pov?: 'first' | 'third_limited' | 'third_omniscient'
  tense?: 'past' | 'present'
  audience?: string
  min_pages?: number
  /** null / omitted → the outline model picks a chapter count. */
  chapter_count?: number | null
  explicitness?: 'none' | 'moderate' | 'explicit'
  outline_model?: string
  prose_model?: string
  temperature?: number
}

export interface StorySummary {
  id: string
  title: string
  status: StoryStatus | string
  chapter_count: number
  word_count: number
  created_at: number
  updated_at: number
  progress?: StoryProgress | null
}

export interface StoryChapter {
  index: number
  title: string
  beats: string[]
  text: string
  word_count: number
  status: string
  generated_at: number | null
  model_id?: string | null
  /** True once the user hand-edited this chapter. */
  edited?: boolean
  /** Per-language translations of this chapter. `stale` means the original
   *  changed after the translation was made. */
  translations?: Record<string, StoryTranslation>
}

export interface StoryTranslation {
  title: string
  text: string
  translated_at?: number
  stale?: boolean
}

/** One recorded LLM call — what "Show prompt" displays. */
export interface StoryLlmPass {
  pass: string
  model_id: string | null
  system_prompt: string
  user_prompt: string
  response_text: string
  thinking_text?: string | null
  at: number
}

export interface StoryState {
  story_id: string
  title: string
  premise: string
  params: StoryParams
  status: StoryStatus | string
  outline: unknown
  chapters: StoryChapter[]
  synopsis_running?: string
  progress?: StoryProgress | null
  error?: string | null
  llm_passes?: StoryLlmPass[]
  output_files?: string[]
  /** Original language first, then every language a translation exists in.
   *  Derived server-side on every save. */
  languages?: string[]
  /** Result of the last analyze pass, persisted with the story. */
  analysis?: StoryAnalysis | null
}

export interface StoryLanguage { code: string; name: string }

export interface StoryCharacter {
  name: string
  role: string
  description: string
  first_chapter: number
  last_chapter: number
  chapters: number[]
  traits: string[]
}

export interface StoryDialogueLine {
  chapter: number
  speaker: string
  line_excerpt: string
  context: string
}

export interface StoryIssue {
  kind: string
  severity: 'high' | 'medium' | 'low' | string
  chapter: number
  description: string
  suggestion: string
}

export interface StoryTimelineEntry {
  chapter: number
  when: string
  where: string
  summary: string
}

/** All `chapter` fields are 0-based state indices. */
export interface StoryAnalysis {
  ok?: boolean
  characters: StoryCharacter[]
  dialogue_map: StoryDialogueLine[]
  /** The dialogue map hit the server-side cap. */
  truncated: boolean
  issues: StoryIssue[]
  timeline: StoryTimelineEntry[]
  summary: string
  /** Issues dropped because the model named a chapter that doesn't exist. */
  dropped_refs: number
  language?: string
  chapters_analyzed?: number
  analyzed_at?: number
}

export interface StoryRewriteProposal {
  ok: boolean
  replacement: string
  before: string
  after: string
}

export interface StoryModelLists {
  outline: import('../types').LlmModelOption[]
  prose: import('../types').LlmModelOption[]
}

/** Shared 404/error unwrap for the story endpoints, which land in stages. */
async function storyJson<T>(res: Response, what: string): Promise<T> {
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: '' }))
    throw new Error(err.detail || `${what} failed (HTTP ${res.status})`)
  }
  return res.json()
}

export async function fetchStories(): Promise<{ stories: StorySummary[] }> {
  return storyJson(await fetch(`${BASE}/api/v1/story/stories`), 'Loading stories')
}

export async function createStory(params: StoryParams): Promise<{ story_id: string; status: string }> {
  const res = await fetch(`${BASE}/api/v1/story/stories`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  return storyJson(res, 'Starting the story')
}

/** Full story state, or null when it's gone (404). */
export async function fetchStory(sid: string): Promise<StoryState | null> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}`)
  if (res.status === 404) return null
  return storyJson(res, 'Loading the story')
}

export async function stopStory(sid: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/stop`, { method: 'POST' })
  if (!res.ok && res.status !== 404) throw new Error('Failed to stop the story')
}

export async function deleteStory(sid: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}`, { method: 'DELETE' })
  if (!res.ok && res.status !== 404) throw new Error('Failed to delete the story')
}

export async function regenerateStoryChapter(
  sid: string, index: number, instruction?: string,
): Promise<{ status?: string }> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/chapters/${index}/regenerate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(instruction ? { instruction } : {}),
  })
  return storyJson(res, 'Regenerating the chapter')
}

/** Manual edit — replaces a chapter's prose verbatim. With `lang` the
 *  translation in that language is edited instead of the original. */
export async function saveStoryChapter(sid: string, index: number, text: string, lang?: string): Promise<unknown> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/chapters/${index}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(lang ? { text, lang } : { text }),
  })
  return storyJson(res, 'Saving the chapter')
}

/** Curated list of languages the models write and translate well. */
export async function fetchStoryLanguages(): Promise<{ languages: StoryLanguage[] }> {
  return storyJson(await fetch(`${BASE}/api/v1/story/languages`), 'Loading languages')
}

/** Translate every written chapter. Runs as a worker — progress arrives
 *  through the normal story polling. 409 while the story is busy. */
export async function translateStory(sid: string, language: string): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/translate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ language }),
  })
  return storyJson(res, 'Translating the story')
}

export async function retranslateStoryChapter(sid: string, index: number, language: string): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/chapters/${index}/retranslate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ language }),
  })
  return storyJson(res, 'Re-translating the chapter')
}

/** Propose a rewrite of a marked passage. Applies nothing. The selection
 *  must match exactly once — anything else is a 400 with the reason. */
export async function rewriteStoryPassage(sid: string, index: number, body: {
  selected_text: string; instruction: string; lang?: string
}): Promise<StoryRewriteProposal> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/chapters/${index}/rewrite`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return storyJson(res, 'Rewriting the passage')
}

export async function applyStoryRewrite(sid: string, index: number, body: {
  selected_text: string; replacement: string; lang?: string
}): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/chapters/${index}/apply-rewrite`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return storyJson(res, 'Applying the rewrite')
}

/** Insert a chapter at `at_index`. `write: true` has the LLM write it
 *  (optionally steered by `brief`); otherwise an empty chapter lands. */
export async function insertStoryChapter(sid: string, body: {
  at_index: number; title?: string; text?: string; brief?: string; write?: boolean
}): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/chapters`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return storyJson(res, 'Inserting the chapter')
}

export async function deleteStoryChapter(sid: string, index: number): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/chapters/${index}`, { method: 'DELETE' })
  return storyJson(res, 'Deleting the chapter')
}

/** Audit the story. One LLM pass per chapter, so this takes minutes on a
 *  long book — deliberately without a timeout. */
export async function analyzeStory(sid: string, lang?: string): Promise<StoryAnalysis> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(lang ? { lang } : {}),
  })
  return storyJson(res, 'Analysing the story')
}

/** md/txt always work; docx and pdf depend on optional server packages. */
export async function fetchStoryExportFormats(): Promise<{ formats: Record<string, boolean> }> {
  return storyJson(await fetch(`${BASE}/api/v1/story/export-formats`), 'Loading export formats')
}

/** Download links, not fetches — the server sets Content-Disposition, so
 *  these belong in an <a href> / window.open. */
export function storyDownloadUrl(sid: string, opts: { fmt: string; lang?: string; perChapter?: boolean }): string {
  const q = new URLSearchParams({ fmt: opts.fmt })
  if (opts.lang) q.set('lang', opts.lang)
  if (opts.perChapter) q.set('per_chapter', 'true')
  return `${BASE}/api/v1/story/stories/${sid}/download?${q}`
}

export function storyChapterDownloadUrl(sid: string, index: number, opts: { fmt: string; lang?: string }): string {
  const q = new URLSearchParams({ fmt: opts.fmt })
  if (opts.lang) q.set('lang', opts.lang)
  return `${BASE}/api/v1/story/stories/${sid}/chapters/${index}/download?${q}`
}

export async function extendStory(sid: string, additionalChapters: number): Promise<unknown> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/extend`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ additional_chapters: additionalChapters }),
  })
  return storyJson(res, 'Extending the story')
}

export async function exportStory(sid: string, format: 'md' | 'txt'): Promise<{ path: string }> {
  const res = await fetch(`${BASE}/api/v1/story/stories/${sid}/export`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ format }),
  })
  return storyJson(res, 'Export')
}

/** Curated model lists per pass (outline wants a reasoner, prose a writer). */
export async function fetchStoryModels(): Promise<StoryModelLists> {
  return storyJson(await fetch(`${BASE}/api/v1/story/models`), 'Loading story models')
}

/** Stop the LLM pass behind a stream id. The partial text is kept. */
export async function cancelLlmStream(streamId: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/llm/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ stream_id: streamId }),
  })
  if (!res.ok) throw new Error('Failed to cancel')
}

// --- AudioBook Creator (Audio mode → Audiobook) ---
//
// Projects are JSON in the workspace. `PUT .../{pid}` is the ONLY write path:
// every editor mutation sends the changed collection(s) whole and the server
// re-sanitises them (dropping e.g. overrides on runs with no profile_id).

export interface AudiobookRun {
  id: string
  text: string
  profile_id?: string | null
  /** Backend key is `model_type`, not `model_id` — see OVERRIDE_KEYS. */
  overrides?: { emotion?: string; stability?: number; style?: number; speed?: number; model_type?: string } | null
}

export interface AudiobookBlock {
  id: string
  type: 'paragraph' | 'sfx'
  runs?: AudiobookRun[]
  attached_sfx?: { sfx_id: string; loop?: boolean; volume?: number } | null
  attached_music?: { music_id: string; loop?: boolean; volume?: number } | null
  sfx_id?: string | null
}

export interface AudiobookChapter {
  id: string
  title: string
  blocks: AudiobookBlock[]
  music_id?: string | null
  language?: string | null
  audio_path?: string | null
  audio_hash?: string | null
  audio_duration?: number | null
}

export interface AudiobookVoiceProfile {
  id: string
  name: string
  color: string
  model_type: string
  voice_ref_path?: string | null
  emotion_ref_path?: string | null
  default_emotion?: string | null
  params: Record<string, number | string | boolean>
}

export interface AudiobookSfxAsset {
  id: string
  label: string
  prompt: string
  duration: number
  audio_path?: string | null
  playback_mode: 'parallel' | 'sequential'
  loop: boolean
  volume: number
}

export interface AudiobookMusicAsset {
  id: string
  title: string
  source: 'generated' | 'upload' | string
  prompt: string
  audio_path?: string | null
  duration: number
  volume: number
  loop: boolean
}

export interface AudiobookProject {
  version: number
  /** The GET/PUT payload calls it `project_id`; the list endpoint calls it
   *  `id`. `normalizeProject` collapses both into `project_id`. */
  project_id: string
  title: string
  language: string
  created_at: number
  updated_at: number
  default_profile_id?: string | null
  chapters: AudiobookChapter[]
  voice_profiles: AudiobookVoiceProfile[]
  sfx: AudiobookSfxAsset[]
  music: AudiobookMusicAsset[]
  render_settings: Record<string, unknown>
}

export interface AudiobookProjectSummary {
  id: string
  title: string
  language: string
  created_at: number
  updated_at: number
  chapter_count: number
  voice_count: number
  rendered_chapters: number
  workspace?: string
}

/** Everything the writer may change. Partial — send only what moved. */
export type AudiobookPatch = Partial<Pick<AudiobookProject,
  'title' | 'language' | 'chapters' | 'voice_profiles' | 'sfx' | 'music' | 'default_profile_id' | 'render_settings'
>> & { workspace?: string }

export interface AudiobookRunPlan {
  run_id: string
  model_type: string
  params: Record<string, unknown>
  seed: number
  emotion: string | null
  emotion_mode: string
  warnings: string[]
  estimated_seconds: number
}

export interface AudiobookPlan {
  chapter_id: string
  runs: AudiobookRunPlan[]
  errors: string[]
  ready: boolean
}

/** One sounding element on the rendered chapter's absolute timeline. Speech
 *  entries double as the karaoke map (`run_id` + `start`). */
export interface AudiobookTimelineEntry {
  kind: 'speech' | 'sfx' | 'ambience' | 'music' | string
  start: number
  end: number
  duration?: number
  run_id?: string | null
  block_id?: string | null
}

/** The GET payload names the id `project_id`, the list payload `id`. Fill in
 *  whichever is missing so the rest of the app only ever reads one. */
function normalizeProject(raw: AudiobookProject & { id?: string }): AudiobookProject {
  return {
    ...raw,
    project_id: raw.project_id || raw.id || '',
    chapters: raw.chapters ?? [],
    voice_profiles: raw.voice_profiles ?? [],
    sfx: raw.sfx ?? [],
    music: raw.music ?? [],
    render_settings: raw.render_settings ?? {},
  }
}

export async function fetchAudiobookProjects(workspace?: string): Promise<{ projects: AudiobookProjectSummary[] }> {
  const q = workspace ? `?workspace=${encodeURIComponent(workspace)}` : ''
  return storyJson(await fetch(`${BASE}/api/v1/audiobook/projects${q}`), 'Loading audiobook projects')
}

export async function createAudiobookProject(
  body: { title?: string; language?: string; workspace?: string } = {},
): Promise<AudiobookProject> {
  const res = await fetch(`${BASE}/api/v1/audiobook/projects`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return normalizeProject(await storyJson(res, 'Creating the audiobook'))
}

export async function fetchAudiobookProject(pid: string, workspace?: string): Promise<AudiobookProject | null> {
  const q = workspace ? `?workspace=${encodeURIComponent(workspace)}` : ''
  const res = await fetch(`${BASE}/api/v1/audiobook/projects/${pid}${q}`)
  if (res.status === 404) return null
  return normalizeProject(await storyJson(res, 'Loading the audiobook'))
}

export async function updateAudiobookProject(pid: string, patch: AudiobookPatch): Promise<AudiobookProject> {
  const res = await fetch(`${BASE}/api/v1/audiobook/projects/${pid}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  return normalizeProject(await storyJson(res, 'Saving the audiobook'))
}

export async function deleteAudiobookProject(pid: string, workspace?: string): Promise<void> {
  const q = workspace ? `?workspace=${encodeURIComponent(workspace)}` : ''
  const res = await fetch(`${BASE}/api/v1/audiobook/projects/${pid}${q}`, { method: 'DELETE' })
  if (!res.ok && res.status !== 404) throw new Error('Failed to delete the audiobook project')
}

/** `path` must already be on the server — upload with `uploadFile` first. */
export async function importAudiobookDocument(pid: string, body: {
  path: string
  auto_split?: boolean
  profile_id?: string
  replace?: boolean
  workspace?: string
}): Promise<{ project: AudiobookProject; imported_chapters: number; language: string | null }> {
  const res = await fetch(`${BASE}/api/v1/audiobook/projects/${pid}/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const out = await storyJson<{ project: AudiobookProject; imported_chapters: number; language: string | null }>(res, 'Import')
  return { ...out, project: normalizeProject(out.project) }
}

export async function planAudiobookChapter(pid: string, body: {
  chapter_id?: string
  chapter_index?: number
  workspace?: string
}): Promise<AudiobookPlan> {
  const res = await fetch(`${BASE}/api/v1/audiobook/projects/${pid}/plan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return storyJson(res, 'Planning the chapter')
}

/** Kicks off a render job; poll `fetchJobStatus(job_id)` for `output_files`. */
export async function renderAudiobook(pid: string, body: {
  chapter_id?: string
  chapter_index?: number
  format?: 'mp3' | 'wav' | 'flac' | 'm4b'
  force?: boolean
  book?: boolean
  workspace?: string
}): Promise<{ job_id: string }> {
  const res = await fetch(`${BASE}/api/v1/audiobook/projects/${pid}/render`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return storyJson(res, 'Render')
}

/** Karaoke map for a finished render. Accepts the MixPlan payload
 *  (`{timeline: [...]}`) or a bare entry array; anything else → []. */
/** Add an sfx or music asset. Without audio_path the server generates the
 *  audio and returns a job_id; the asset exists immediately with a null
 *  audio_path so the UI can show it as pending. */
export async function createAudiobookAsset(
  pid: string,
  kind: 'sfx' | 'music',
  body: {
    label?: string; title?: string; prompt?: string; duration?: number
    playback_mode?: 'parallel' | 'sequential'; loop?: boolean; volume?: number
    audio_path?: string; generate?: boolean; workspace?: string
  },
): Promise<{ project: AudiobookProject; asset_id: string; job_id: string | null }> {
  const res = await fetch(`${BASE}/api/v1/audiobook/projects/${pid}/assets/${kind}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Could not add the asset' }))
    throw new Error(err.detail || 'Could not add the asset')
  }
  const data = await res.json()
  return { ...data, project: normalizeProject(data.project) }
}

/** Generate audio for an asset that already exists (e.g. one created by
 *  apply-cast, which attaches the asset before any audio is rendered). */
export async function generateAudiobookAssetAudio(
  pid: string, kind: 'sfx' | 'music', assetId: string,
): Promise<{ job_id: string; asset_id: string }> {
  const res = await fetch(
    `${BASE}/api/v1/audiobook/projects/${pid}/assets/${kind}/${assetId}/generate`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
  )
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Could not start generation' }))
    throw new Error(err.detail || 'Could not start generation')
  }
  return res.json()
}

export async function deleteAudiobookAsset(
  pid: string, kind: 'sfx' | 'music', assetId: string,
): Promise<AudiobookProject> {
  const res = await fetch(`${BASE}/api/v1/audiobook/projects/${pid}/assets/${kind}/${assetId}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error('Could not delete the asset')
  const data = await res.json()
  return normalizeProject(data.project)
}

export interface AbSplitProposal { after_block_id: string; new_title: string; reason?: string }
export interface AbCastCharacter { name: string; gender?: string | null; description?: string }
export interface AbCastAssignment { run_id: string; speaker: string; emotion?: string | null }
export interface AbCastEffect {
  block_id: string; label: string; prompt: string
  playback_mode: 'parallel' | 'sequential'; loop: boolean; volume: number; duration: number
}

/** Chapter-split proposals. Nothing is applied until applyAudiobookSplit. */
export async function suggestAudiobookSplit(pid: string, body: {
  chapter_index?: number; chapter_id?: string; target_words?: number
}): Promise<{ chapter_id: string; splits: AbSplitProposal[]; dropped: number }> {
  return abPost(pid, 'suggest-split', body)
}

export async function applyAudiobookSplit(pid: string, body: {
  chapter_index?: number; chapter_id?: string; splits: AbSplitProposal[]
}): Promise<AudiobookProject> {
  const data = await abPost<{ project: AudiobookProject }>(pid, 'apply-split', body)
  return normalizeProject(data.project)
}

/** Speaker / emotion / effect suggestions, ids already validated server-side. */
export async function suggestAudiobookCast(pid: string, body: {
  chapter_index?: number; chapter_id?: string
}): Promise<{
  chapter_id: string
  characters: AbCastCharacter[]
  assignments: AbCastAssignment[]
  effects: AbCastEffect[]
  dropped: number
}> {
  return abPost(pid, 'suggest-cast', body)
}

export async function applyAudiobookCast(pid: string, body: {
  chapter_index?: number; chapter_id?: string
  characters?: AbCastCharacter[]; assignments?: AbCastAssignment[]; effects?: AbCastEffect[]
}): Promise<{
  project: AudiobookProject
  created_effects: { asset_id: string; prompt: string; duration: number }[]
}> {
  const data = await abPost<{
    project: AudiobookProject
    created_effects: { asset_id: string; prompt: string; duration: number }[]
  }>(pid, 'apply-cast', body)
  return { ...data, project: normalizeProject(data.project) }
}

async function abPost<T>(pid: string, path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}/api/v1/audiobook/projects/${pid}/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `${path} failed` }))
    throw new Error(err.detail || `${path} failed`)
  }
  return res.json()
}

export async function fetchAudiobookTimeline(url: string): Promise<AudiobookTimelineEntry[]> {
  const res = await fetch(url)
  if (!res.ok) return []
  const data = await res.json().catch(() => null)
  const list = Array.isArray(data) ? data : Array.isArray(data?.timeline) ? data.timeline : []
  return list.filter((e: unknown): e is AudiobookTimelineEntry =>
    !!e && typeof (e as AudiobookTimelineEntry).start === 'number')
}

// --- Audio Analysis ---

export async function uploadAudio(file: File): Promise<{ filename: string; path: string; url: string }> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}/api/v1/upload-audio`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Upload failed' }))
    throw new Error(err.detail || 'Audio upload failed')
  }
  return res.json()
}

export async function analyzeAudio(params: {
  audio_path: string
  transcribe?: boolean
  extract_vocals?: boolean
  /** Known written lyrics (generated tracks) — seeds Whisper so the
   *  transcription snaps to the real words instead of mishearing
   *  sung vocals. Omit for uploads/unknown tracks. */
  lyrics_hint?: string
}): Promise<import('../types').AudioAnalysisResult> {
  const res = await fetch(`${BASE}/api/v1/audio/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Analysis failed' }))
    throw new Error(err.detail || 'Audio analysis failed')
  }
  return res.json()
}

/** Read live progress of the in-flight audio analyze call. Backed by
 *  audio_analysis._PROGRESS — updated at each phase boundary in the
 *  synchronous analyze() call. Polled by the Director sidebar to
 *  show "Loading transcription model (first use downloads ~300MB)..."
 *  vs "Transcribing audio..." instead of a single "Analyzing audio..."
 *  message for the entire 1-5 minute first-run wait. Returns empty
 *  step/detail when no analyze is in flight. */
export async function fetchAudioAnalyzeStatus(): Promise<{ step: string; detail: string }> {
  const res = await fetch(`${BASE}/api/v1/audio/analyze/status`)
  if (!res.ok) return { step: '', detail: '' }
  return res.json()
}

export async function suggestAudioClips(params: {
  analysis: import('../types').AudioAnalysisResult
  clip_duration: number
  total_duration?: number
}): Promise<{ clips: import('../types').SuggestedClip[] }> {
  const res = await fetch(`${BASE}/api/v1/audio/suggest-clips`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Clip suggestion failed' }))
    throw new Error(err.detail || 'Clip suggestion failed')
  }
  return res.json()
}

// --- Director ---

export async function planAnglePrompts(params: {
  style_prompt: string
  num_angles?: number
}): Promise<{ prompts: string[] }> {
  const res = await fetch(`${BASE}/api/v1/director/plan-angle-prompts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Angle prompt planning failed' }))
    throw new Error(err.detail || 'Angle prompt planning failed')
  }
  return res.json()
}

export async function planClipPrompts(params: {
  clips: import('../types').SuggestedClip[]
  style_prompt: string
  lyrics?: import('../types').LyricSegment[]
  bpm: number
}): Promise<{ prompts: string[] }> {
  const res = await fetch(`${BASE}/api/v1/director/plan-prompts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Prompt planning failed' }))
    throw new Error(err.detail || 'Prompt planning failed')
  }
  return res.json()
}

export async function planClipStructure(params: {
  analysis: import('../types').AudioAnalysisResult
  energy_bias?: number
  fps?: number
  frames_steps?: number
  frames_minimum?: number
  total_duration?: number
  /** The Director's VIDEO model — the backend resolves fps/frame params
   *  from its model def. The fps/frames_* fields above reflect the
   *  Studio-selected model (possibly a music model) and are only a
   *  fallback when this is absent. */
  video_model?: string
}): Promise<{ clips: import('../types').PlannedClip[] }> {
  const res = await fetch(`${BASE}/api/v1/audio/plan-structure`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Structure planning failed' }))
    throw new Error(err.detail || 'Structure planning failed')
  }
  return res.json()
}

export async function classifySections(params: {
  analysis: import('../types').AudioAnalysisResult
}): Promise<{
  sections: import('../types').AudioSection[]
  song_structure: { label: string; display_label: string; start: number }[]
  method: 'llm' | 'heuristic'
}> {
  const res = await fetch(`${BASE}/api/v1/director/classify-sections`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Classification failed' }))
    throw new Error(err.detail || 'Section classification failed')
  }
  return res.json()
}

export async function planClipPromptsAndImages(params: {
  clips: import('../types').PlannedClip[]
  scene_description: string
  lyrics?: import('../types').LyricSegment[]
  bpm: number
  reference_image_path?: string | null
  speaker_mappings?: Record<string, { name: string; role: string }>
  prompt_type?: 'image' | 'video' | 'both'
  existing_image_prompts?: string[]
}): Promise<{ clip_plans: import('../types').ClipPlan[] }> {
  const res = await fetch(`${BASE}/api/v1/director/plan-prompts-and-images`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Prompt and image planning failed' }))
    throw new Error(err.detail || 'Prompt and image planning failed')
  }
  return res.json()
}

// --- Short Film Director ---

export async function planDialogueScenes(params: {
  analysis: import('../types').AudioAnalysisResult
  pacing_bias?: number
  fps?: number
  frames_steps?: number
  frames_minimum?: number
}): Promise<{ clips: import('../types').PlannedClip[] }> {
  const res = await fetch(`${BASE}/api/v1/director/plan-dialogue-scenes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Dialogue scene planning failed' }))
    throw new Error(err.detail || 'Dialogue scene planning failed')
  }
  return res.json()
}

export async function planShortFilmPrompts(params: {
  clips: import('../types').PlannedClip[]
  scene_description: string
  lyrics?: import('../types').LyricSegment[]
  reference_image_path?: string | null
  speaker_mappings?: Record<string, { name: string; role: string }>
  characters?: { name: string; description: string }[]
  prompt_type?: 'image' | 'video' | 'both'
  existing_image_prompts?: string[]
}): Promise<{ clip_plans: import('../types').ClipPlan[] }> {
  const res = await fetch(`${BASE}/api/v1/director/plan-short-film-prompts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Short film prompt planning failed' }))
    throw new Error(err.detail || 'Short film prompt planning failed')
  }
  return res.json()
}

/** LLM streaming buffer. Omit `streamId` for the shared slot (prompt
 *  enhancer / Director); pass `chat-<threadId>` for a chat thread so
 *  concurrent generations don't read each other's tokens. */
export async function getLlmStreamStatus(streamId?: string): Promise<{ text: string; done: boolean; stream_id?: string }> {
  const q = streamId ? `?stream_id=${encodeURIComponent(streamId)}` : ''
  const res = await fetch(`${BASE}/api/v1/llm/stream-status${q}`)
  if (!res.ok) return { text: '', done: true }
  return res.json()
}

export async function planShortFilmScript(params: {
  story_description: string
  characters?: { name: string; description: string }[]
  reference_image_path?: string | null
  target_duration?: number
  target_scenes?: number
  narrative_mode?: boolean
  fps?: number
  frames_steps?: number
  frames_minimum?: number
}): Promise<{ clips: import('../types').PlannedClip[]; clip_plans: import('../types').ClipPlan[] }> {
  const res = await fetch(`${BASE}/api/v1/director/plan-short-film-script`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Story planning failed' }))
    throw new Error(err.detail || 'Story planning failed')
  }
  return res.json()
}

// --- CivitAI Browser ---

export async function fetchLoraDirectories(): Promise<{ directories: string[] }> {
  const res = await fetch(`${BASE}/api/v1/loras/directories`)
  if (!res.ok) throw new Error('Failed to fetch LoRA directories')
  return res.json()
}

export interface CivitAIModelFilter {
  label: string
  civitai_base: string
  search_query?: string
  default_dir?: string
}

export async function fetchCivitAIModelFilters(): Promise<{ filters: CivitAIModelFilter[] }> {
  const res = await fetch(`${BASE}/api/v1/civitai/base-models`)
  if (!res.ok) throw new Error('Failed to fetch model filters')
  return res.json()
}

export interface CheckpointArchitecture {
  architecture: string
  name: string
  family: string
  template_model_type: string
}

// List the architectures a full checkpoint can be imported as (video/image
// models we already support) + a best-guess default for the given CivitAI
// baseModel so the picker can pre-select it.
export async function fetchCheckpointArchitectures(
  baseModel?: string
): Promise<{ architectures: CheckpointArchitecture[]; suggested_architecture: string | null }> {
  const qs = baseModel ? `?base_model=${encodeURIComponent(baseModel)}` : ''
  const res = await fetch(`${BASE}/api/v1/civitai/checkpoint-architectures${qs}`)
  if (!res.ok) throw new Error('Failed to fetch checkpoint architectures')
  return res.json()
}

export interface InstalledCheckpoint {
  model_type: string
  name: string
  architecture: string
  civitai_model_id: number | null
  current_version_id: number | null
  base_model: string
  filename: string
  auto_quantize: boolean
  update_status: 'current' | 'available' | 'unknown' | 'removed'
  latest_version_id: number | null
  latest_published_at: string | null
  latest_changelog: string | null
  preview_url: string | null
}

// List CivitAI-imported checkpoints (registered finetunes) with update status.
export async function fetchInstalledCheckpoints(): Promise<{ checkpoints: InstalledCheckpoint[]; manifest_last_check_at: string | null }> {
  const res = await fetch(`${BASE}/api/v1/checkpoints/installed`)
  if (!res.ok) throw new Error('Failed to fetch installed checkpoints')
  return res.json()
}

// Query CivitAI for newer versions of every imported checkpoint.
export async function checkCheckpointUpdates(force = false): Promise<{ checked: number; updates_available: number; errors: number; skipped: boolean }> {
  const res = await fetch(`${BASE}/api/v1/checkpoints/check-updates?force=${force}`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to check checkpoint updates')
  return res.json()
}

export async function searchCivitAI(params: {
  query?: string; sort?: string; period?: string
  nsfw?: boolean; types?: string; baseModels?: string
  limit?: number; cursor?: string
}): Promise<import('../types').CivitAISearchResult> {
  const qs = new URLSearchParams()
  if (params.query) qs.set('query', params.query)
  if (params.sort) qs.set('sort', params.sort)
  if (params.period) qs.set('period', params.period)
  if (params.nsfw != null) qs.set('nsfw', String(params.nsfw))
  if (params.types) qs.set('types', params.types)
  if (params.baseModels) qs.set('baseModels', params.baseModels)
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.cursor) qs.set('cursor', params.cursor)
  const res = await fetch(`${BASE}/api/v1/civitai/search?${qs}`)
  if (!res.ok) {
    // Pull the backend's `detail` if available — it carries the
    // human-readable reason (e.g. "CivitAI is currently in scheduled
    // maintenance") that the proxy synthesises for known states.
    let detail = ''
    try {
      const body = await res.json()
      detail = body?.detail || ''
    } catch { /* non-JSON body */ }
    const err = new Error(detail || `CivitAI search failed (HTTP ${res.status})`)
    ;(err as Error & { status?: number }).status = res.status
    throw err
  }
  return res.json()
}

export async function fetchCivitAIModel(modelId: number): Promise<import('../types').CivitAIModel> {
  const res = await fetch(`${BASE}/api/v1/civitai/model/${modelId}`)
  if (!res.ok) throw new Error('Failed to fetch model details')
  return res.json()
}

export async function startCivitAIDownload(params: {
  download_url: string; filename: string; target_arch: string
  model_id: number; version_id: number; trained_words: string[]
  model_name: string; images: { url: string }[]
  description?: string; version_description?: string; base_model?: string
  example_prompts?: string[]; tags?: string[]
  nsfw?: boolean; target_dir_name?: string; published_at?: string
  // Checkpoint imports: kind='checkpoint' routes the file into ckpts/ and
  // registers a finetune for target_architecture instead of saving a LoRA.
  // auto_quantize=true sets the finetune to load-time int8 (mmgp).
  kind?: string; target_architecture?: string; auto_quantize?: boolean
}): Promise<{ download_id: string }> {
  const res = await fetch(`${BASE}/api/v1/civitai/download`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Download failed' }))
    throw new Error(err.detail || 'Download failed')
  }
  return res.json()
}

export async function fetchCivitAIDownloads(): Promise<{ downloads: import('../types').CivitAIDownload[] }> {
  const res = await fetch(`${BASE}/api/v1/civitai/downloads`)
  if (!res.ok) throw new Error('Failed to fetch downloads')
  return res.json()
}

export async function generateLoraGuide(modelType: string, filename: string): Promise<{ guide: string }> {
  const res = await fetch(`${BASE}/api/v1/loras/generate-guide`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_type: modelType, filename }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Guide generation failed' }))
    throw new Error(err.detail || 'Guide generation failed')
  }
  return res.json()
}

export async function fetchLoraGuide(modelType: string, filename: string): Promise<{ guide: string | null }> {
  const res = await fetch(`${BASE}/api/v1/loras/${encodeURIComponent(modelType)}/${encodeURIComponent(filename)}/guide`)
  if (!res.ok) return { guide: null }
  return res.json()
}

export async function importHuggingFaceLora(url: string, targetDir?: string, filename?: string): Promise<{
  status: string; download_id: string; filename: string; target_dir: string; repo_id?: string; base_model: string
}> {
  const res = await fetch(`${BASE}/api/v1/huggingface/import-lora`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, target_dir: targetDir || '', filename: filename || '' }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: 'Import failed' }))
    throw new Error(err.error || 'Import failed')
  }
  return res.json()
}

export async function startLoraScan(options?: { modelType?: string; force?: boolean }): Promise<{ scan_id: string; total: number }> {
  const body: Record<string, unknown> = {}
  if (options?.modelType) body.model_type = options.modelType
  if (options?.force) body.force = true
  const res = await fetch(`${BASE}/api/v1/loras/scan-and-generate-guides`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Scan failed' }))
    throw new Error(err.detail || 'Scan failed')
  }
  return res.json()
}

export async function fetchLoraScanStatus(scanId: string): Promise<{
  status: string; current: number; total: number; message: string
  results: { filename: string; metadata?: string; guide?: string; error?: string }[]
}> {
  const res = await fetch(`${BASE}/api/v1/loras/scan-status/${scanId}`)
  if (!res.ok) throw new Error('Failed to fetch scan status')
  return res.json()
}

/** Per-LoRA update status. Mirrored from types/index.ts for use in
 *  the API layer without forcing a circular import. */
export type LoraUpdateStatus = 'current' | 'available' | 'unknown' | 'local' | 'removed'

export interface InstalledLora {
  filename: string
  directory: string
  /** File lives in a linked install's loras folder (read-only), not
   *  MuseForge's own. Sidecars/guides for it live in MuseForge's mirror. */
  linked?: boolean
  trained_words: string[]
  preview_url: string | null
  civitai_model_id: number | null
  hf_repo_id?: string | null
  has_guide: boolean
  name: string | null
  base_model: string | null
  nsfw: boolean
  /** True when the user manually overrode CivitAI's NSFW classification
   *  via /api/v1/loras/nsfw-override. */
  nsfw_overridden?: boolean
  /** Stable identifier that survives version updates. Format:
   *  `civitai:{modelId}` when the sidecar exposes a CivitAI modelId,
   *  otherwise `local:{filename}`. Used as the persistence key for
   *  per-LoRA settings (weight overrides, activations) so updating a
   *  LoRA from v1.2 → v1.5 carries those settings forward. */
  lora_id: string
  /** Update status from the cached LoRA-update manifest, populated by
   *  the backend on every /api/v1/loras/installed and
   *  /api/v1/loras/{model_type}/details call. The UI uses this to
   *  render badges. */
  update_status?: LoraUpdateStatus
  latest_version_id?: number | null
  current_version_id?: number | null
  latest_published_at?: string | null
  latest_changelog?: string | null
  /** On-disk size of the .safetensors file (null when unreadable). */
  size_bytes?: number | null
  /** When the file arrived: sidecar downloadedAt (CivitAI downloads) or
   *  the weight file's mtime (HF/hand-installed). ISO string. */
  downloaded_at?: string | null
  /** The version's CivitAI release date (publishedAt) — captured at
   *  download time, backfilled for older files by Check Updates. */
  released_at?: string | null
}

export async function fetchInstalledLoras(): Promise<{
  loras: InstalledLora[]
  /** ISO timestamp of the last full CivitAI check that populated the
   *  cached update manifest. UI shows "last checked X minutes ago". */
  manifest_last_check_at?: string | null
}> {
  const res = await fetch(`${BASE}/api/v1/loras/installed`)
  if (!res.ok) throw new Error('Failed to fetch installed LoRAs')
  return res.json()
}

// --- Storage (duplicates + usage analytics) ---

export interface StorageDuplicate {
  kind: 'checkpoint' | 'lora'
  filename: string
  rel_path: string
  primary_path: string
  size_bytes: number
  linked_path: string
  linked_size_bytes: number
  linked_install: string
}

export interface StorageUsageModel {
  model_type: string
  name: string
  size_bytes: number
  /** Bytes living in the primary (deletable) roots — what deleting frees. */
  primary_bytes: number
  /** Display name of the base model whose weights this entry aliases
   *  (finetunes with "URLs": "<base>") — deleting this row frees nothing. */
  alias_of?: string | null
  use_count: number
  last_used: number | null
}

export interface StorageUsageLora {
  filename: string
  directory: string
  linked: boolean
  size_bytes: number
  use_count: number
  last_used: number | null
}

export interface StorageUsage {
  models: StorageUsageModel[]
  /** Globally deduped — per-model sizes overlap on shared weights
   *  (base transformers, text encoders), so summing rows over-counts. */
  models_total_bytes: number
  loras: StorageUsageLora[]
  workspaces: { name: string; file_count: number; size_bytes: number }[]
  scanned_sidecars: number
}

export async function fetchStorageUsage(): Promise<StorageUsage> {
  const res = await fetch(`${BASE}/api/v1/storage/usage`)
  if (!res.ok) throw new Error('Failed to fetch storage usage')
  return res.json()
}

export async function fetchStorageDuplicates(): Promise<{ duplicates: StorageDuplicate[]; conflicts: StorageDuplicate[]; total_reclaimable_bytes: number }> {
  const res = await fetch(`${BASE}/api/v1/storage/duplicates`)
  if (!res.ok) throw new Error('Failed to scan for duplicates')
  return res.json()
}

export async function reclaimDuplicate(path: string): Promise<{ freed_bytes: number }> {
  const res = await fetch(`${BASE}/api/v1/storage/duplicates/reclaim`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Reclaim failed' }))
    throw new Error(err.detail || 'Reclaim failed')
  }
  return res.json()
}

export async function removeLinkedDuplicate(path: string): Promise<{ freed_bytes: number; recycled: boolean }> {
  const res = await fetch(`${BASE}/api/v1/storage/duplicates/remove-linked`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Remove failed' }))
    throw new Error(err.detail || 'Remove failed')
  }
  return res.json()
}

export async function deleteLoraFile(directory: string, filename: string): Promise<{ deleted: string; deferred: boolean }> {
  const params = new URLSearchParams({ directory: directory || '.', filename })
  const res = await fetch(`${BASE}/api/v1/loras/file?${params.toString()}`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to delete LoRA' }))
    throw new Error(err.detail || 'Failed to delete LoRA')
  }
  return res.json()
}

/** Single entry in the cached LoRA-update manifest (one per
 *  civitai-sourced LoRA). The manifest itself is keyed by `lora_id`
 *  (e.g. `civitai:12345`) — see LoraUpdateManifest. */
export interface LoraManifestEntry {
  model_id: number
  current_version_id: number | null
  latest_version_id: number | null
  latest_published_at: string | null
  latest_changelog: string | null
  status: 'current' | 'available' | 'removed' | 'unknown'
  last_checked_at: string
}

export interface LoraUpdateManifest {
  _version: number
  last_full_check_at: string | null
  entries: Record<string, LoraManifestEntry>
}

export interface LoraUpdateCheckResult {
  /** Number of LoRAs with a `civitai:`-style lora_id that the backend
   *  considered for refresh during this call. */
  checked: number
  /** How many of the checked LoRAs have a newer version on CivitAI. */
  updates_available: number
  /** Per-LoRA error messages (network failures, deleted models, etc.).
   *  Empty array on success. */
  errors: string[]
  /** True when the backend skipped the refresh because the cached
   *  manifest is fresh (within the 24h window) and `force` was false.
   *  In that case `checked` and `updates_available` come from cache. */
  skipped: boolean
  /** Why the refresh was skipped, when `skipped: true`. Currently the
   *  only value is "fresh" but kept open for future cases. */
  reason?: string
  /** ISO timestamp of the most recent full check (the one whose data
   *  is reflected in `checked` / `updates_available`). */
  last_full_check_at?: string | null
}

/** Trigger a fresh CivitAI version check across every installed LoRA
 *  with a sidecar `modelId`. Updates the cached manifest the backend
 *  uses to populate per-LoRA `update_status` fields on subsequent
 *  /installed and /{model_type}/details calls.
 *
 *  Honours a 24h staleness window unless `force` is true:
 *    - `checkLoraUpdates(false)` — opportunistic; if the manifest is
 *      <24h old the backend short-circuits and returns the cached
 *      summary with `skipped: true`. Cheap to call on app startup.
 *    - `checkLoraUpdates(true)`  — bypass the window. Use for explicit
 *      "Check now" buttons in the UI; pulls from CivitAI even if a
 *      check happened minutes ago.
 *
 *  Returns the summary the UI shows in a toast. Throws on network/HTTP
 *  failure (call sites typically `.catch()` to keep UI responsive). */
export async function checkLoraUpdates(force = false): Promise<LoraUpdateCheckResult> {
  const url = `${BASE}/api/v1/loras/check-updates${force ? '?force=true' : ''}`
  const res = await fetch(url, { method: 'POST' })
  if (!res.ok) throw new Error(`Failed to check LoRA updates (${res.status})`)
  return res.json()
}

/** Read the cached LoRA-update manifest WITHOUT hitting CivitAI.
 *  Use this on app startup to populate badges immediately, then
 *  optionally call checkLoraUpdates() if the cache is stale. The
 *  manifest schema is documented in launch.py near the constant
 *  LORA_MANIFEST_VERSION. */
export async function fetchLoraUpdateManifest(): Promise<LoraUpdateManifest> {
  const res = await fetch(`${BASE}/api/v1/loras/update-manifest`)
  if (!res.ok) throw new Error('Failed to fetch LoRA update manifest')
  return res.json()
}

export async function fetchLoraDetails(modelType: string): Promise<{
  loras: import('../types').LoraInfo[]
  guidance_max_phases: number
  /** ISO timestamp of the last full CivitAI check that populated the
   *  cached update manifest. UI uses this to render "last checked X
   *  minutes ago" alongside the manual "Check updates" button. */
  manifest_last_check_at?: string | null
}> {
  const res = await fetch(`${BASE}/api/v1/loras/${encodeURIComponent(modelType)}/details`)
  if (!res.ok) throw new Error('Failed to fetch LoRA details')
  return res.json()
}

// --- Active model file downloads (HuggingFace etc.) ---

export interface ActiveDownload {
  file_id: string
  filename: string
  started_at: number
  last_active_at: number
  downloaded_bytes: number
  /** null when the server sent no Content-Length — render an
   *  indeterminate bar, not a fake percentage. */
  total_bytes: number | null
  status: 'downloading' | 'stalled' | 'retrying' | 'done' | 'incomplete'
  /** Seconds since the byte counter last advanced. UI uses this to
   *  flag stalled downloads (e.g. `> 15` → show "slow / retrying"). */
  seconds_since_progress: number
  /** Transfer speed in bytes/s (tqdm's smoothed rate), null until known. */
  rate?: number | null
  /** Seconds the transfer has been running. */
  elapsed?: number | null
  /** Seconds remaining, derived from rate + remaining bytes. null when
   *  either is unknown. */
  eta_seconds?: number | null
  /** Set while this file belongs to a model pre-download — match against
   *  the keys of fetchModelDownloads() for the "file 3/7" context. */
  model_type?: string | null
}

export async function fetchActiveDownloads(): Promise<{ downloads: ActiveDownload[] }> {
  const res = await fetch(`${BASE}/api/v1/downloads/active`)
  if (!res.ok) throw new Error(`Failed to fetch active downloads (${res.status})`)
  return res.json()
}

// --- Activity (everything currently running, across features) ---

export interface ActivityItem {
  kind: 'job' | 'director' | 'story' | 'audiobook'
  id: string
  label: string
  status: string
  message: string
  /** 0..1, only meaningful for generation jobs. Prefer step/total_steps. */
  progress: number
  step: number
  total_steps: number
  started_at: number | null
  /** Ready-made endpoint path to POST to in order to stop this item —
   *  the UI never has to know the per-feature cancel conventions. */
  cancel: string
}

export async function fetchActivity(): Promise<{ activity: ActivityItem[]; count: number }> {
  const res = await fetch(`${BASE}/api/v1/activity`)
  if (!res.ok) throw new Error(`Failed to fetch activity (${res.status})`)
  return res.json()
}

/** POST an item's own `cancel` path. No body. */
export async function stopActivityItem(cancelPath: string): Promise<void> {
  const res = await fetch(`${BASE}${cancelPath}`, { method: 'POST' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Stop failed' }))
    throw new Error(err.detail || 'Stop failed')
  }
}

export async function stopAllActivity(): Promise<{
  results: { kind: string; id: string; stopped: boolean; error?: string }[]
}> {
  const res = await fetch(`${BASE}/api/v1/activity/stop-all`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to stop everything')
  return res.json()
}
