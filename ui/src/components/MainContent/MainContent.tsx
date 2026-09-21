import { useRef, useState, useEffect, useMemo } from 'react'
import { Film, Play, Square, FolderOpen, Plus, Check, Loader2, X, BookMarked, Upload, Trash2, Layers, ChevronDown, ChevronUp } from 'lucide-react'
import { TabFilter } from './TabFilter'
import { MediaGrid } from './MediaGrid'
import { ChatView } from './ChatView'
import { AudiobookEditor } from './AudiobookEditor'
import { VoicesView } from './VoicesView'
import { GlobalQueuePopover } from '../GlobalQueuePopover'
import { useStore } from '../../stores/useStore'
import { useIsMobile } from '../../lib/useIsMobile'
import { formatEstimatedClock, formatEtaDuration } from '../../lib/format'
import { PROMPT_ENHANCEMENT_ACTIVITY } from '../../lib/promptEnhancementActivity'
import type { GenerationJob } from '../../types'

function WorkspaceSelector() {
  const workspaces = useStore(s => s.workspaces)
  const activeWorkspace = useStore(s => s.activeWorkspace)
  const browsingAllFolders = useStore(s => s.browsingAllFolders)
  const browsingUploads = useStore(s => s.browsingUploads)
  const switchWorkspace = useStore(s => s.switchWorkspace)
  const createWorkspace = useStore(s => s.createWorkspace)
  const deleteWorkspace = useStore(s => s.deleteWorkspace)
  const [open, setOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)

  const handleDelete = async (name: string, e: React.MouseEvent) => {
    e.stopPropagation()
    if (confirmDelete !== name) {
      setConfirmDelete(name)
      setTimeout(() => setConfirmDelete(c => (c === name ? null : c)), 4000)
      return
    }
    setConfirmDelete(null)
    setDeleting(name)
    setDeleteError(null)
    try {
      await deleteWorkspace(name)
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : String(err))
      setTimeout(() => setDeleteError(null), 6000)
    } finally {
      setDeleting(null)
    }
  }

  // Close on outside click
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpen(false)
        setCreating(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const handleCreate = async () => {
    const name = newName.trim().replace(/\s+/g, '-')
    if (!name) return
    try {
      await createWorkspace(name)
      setNewName('')
      setCreating(false)
      setOpen(false)
    } catch {
      // error logged in store
    }
  }

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 px-2 py-1 rounded-md text-xs text-text-secondary hover:text-text-primary hover:bg-bg-hover transition-colors border border-border"
        title="Switch workspace"
      >
        <FolderOpen size={12} />
        <span className="max-w-[120px] truncate">{browsingAllFolders ? 'All folders' : browsingUploads ? 'Uploads' : activeWorkspace}</span>
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-56 bg-bg-secondary border border-border rounded-lg shadow-lg z-50 overflow-hidden">
          <div className="px-2 py-1.5 border-b border-border">
            <span className="text-[10px] text-text-muted uppercase tracking-wider">Workspaces</span>
          </div>
          <button
            onClick={() => { switchWorkspace('__all__'); setOpen(false) }}
            className={`w-full text-left px-3 py-2 text-xs flex items-center justify-between hover:bg-bg-hover ${browsingAllFolders ? 'text-accent-blue' : 'text-text-secondary'}`}
            title="Browse and search every output folder. New generations keep their current destination."
          >
            <span className="flex items-center gap-1.5"><FolderOpen size={12} /> All folders</span>
            {browsingAllFolders && <Check size={12} />}
          </button>
          <div className="max-h-[200px] overflow-y-auto">
            {workspaces.map(ws => (
              <div key={ws.name} className="flex items-center group hover:bg-bg-hover transition-colors">
                <button
                  onClick={() => { switchWorkspace(ws.name); setOpen(false) }}
                  className={`flex-1 min-w-0 text-left px-3 py-2 text-xs flex items-center justify-between ${
                    ws.name === activeWorkspace && !browsingUploads && !browsingAllFolders ? 'text-accent-blue' : 'text-text-secondary'
                  }`}
                >
                  <span className="truncate">{ws.name}</span>
                  {ws.name === activeWorkspace && !browsingUploads && !browsingAllFolders && <Check size={12} className="shrink-0" />}
                </button>
                {/* default IS the outputs folder itself — not deletable */}
                {ws.name !== 'default' && (
                  <button
                    onClick={e => handleDelete(ws.name, e)}
                    disabled={deleting === ws.name}
                    className={`px-2 py-2 shrink-0 transition-colors ${
                      confirmDelete === ws.name
                        ? 'text-red-400 bg-red-500/15'
                        : deleting === ws.name
                          ? 'text-text-muted cursor-wait'
                          : 'text-text-muted opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:text-red-400'
                    }`}
                    title={confirmDelete === ws.name
                      ? `Click again to permanently delete "${ws.name}" and its ${ws.file_count ?? 0} files`
                      : `Delete workspace (${ws.file_count ?? 0} files)`}
                  >
                    {deleting === ws.name ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
                  </button>
                )}
              </div>
            ))}
          </div>
          {deleteError && (
            <div className="px-3 py-1.5 text-[10px] text-red-400 border-t border-border leading-snug">{deleteError}</div>
          )}
          {/* Virtual Uploads view — browse user-uploaded media (read-only;
              generations keep saving to the real active workspace). */}
          <div className="border-t border-border">
            <button
              onClick={() => { switchWorkspace('__uploads__'); setOpen(false) }}
              className={`w-full text-left px-3 py-2 text-xs flex items-center justify-between hover:bg-bg-hover transition-colors ${
                browsingUploads ? 'text-accent-blue' : 'text-text-secondary'
              }`}
              title="Browse media you've uploaded — reuse as inputs"
            >
              <span className="flex items-center gap-1.5"><Upload size={12} /> Uploads</span>
              {browsingUploads && <Check size={12} />}
            </button>
          </div>
          <div className="border-t border-border p-2">
            {creating ? (
              <div className="flex gap-1.5">
                <input
                  type="text"
                  value={newName}
                  onChange={e => setNewName(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && handleCreate()}
                  placeholder="workspace-name"
                  className="flex-1 bg-bg-tertiary border border-border rounded px-2 py-1 text-xs text-text-primary focus:outline-none focus:border-accent-blue"
                  autoFocus
                />
                <button
                  onClick={handleCreate}
                  disabled={!newName.trim()}
                  className="px-2 py-1 text-xs bg-accent-blue text-white rounded hover:bg-accent-blue-hover disabled:opacity-50"
                >
                  Create
                </button>
              </div>
            ) : (
              <button
                onClick={() => setCreating(true)}
                className="w-full text-left px-1 py-1 text-xs text-accent-blue hover:text-accent-blue-hover flex items-center gap-1"
              >
                <Plus size={12} /> New Workspace
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function stripTimeSuffix(msg: string): string {
  return msg.replace(/\s*\|\s*\d+:\d+.*$/, '').trim()
}

function JobPlaceholder({ job, onStop, onDismiss }: { job: GenerationJob; onStop?: () => void; onDismiss: () => void }) {
  const hasSteps = job.totalSteps > 0
  const progressPct = hasSteps ? (job.step / job.totalSteps) * 100 : job.progress * 100
  const phase = stripTimeSuffix(job.phase || job.message)
  const isFailed = job.status === 'failed' || job.status === 'cancelled'
  const isPromptPlanning = job.kind === 'prompt_enhancement'
  const errorText = job.error || job.message || (job.status === 'cancelled' ? 'Cancelled' : 'Generation failed')
  const h3PlanSignature = job.h3WindowPlan?.signature
  const [h3PromptDisclosure, setH3PromptDisclosure] = useState({
    signature: h3PlanSignature,
    open: false,
  })
  const showH3Prompts = (
    h3PromptDisclosure.signature === h3PlanSignature && h3PromptDisclosure.open
  )
  const h3WindowMatch = (job.phase || job.message || '').match(/Sliding Window\s+(\d+)\/(\d+)/i)
  const activeH3Window = job.currentWindow ?? (h3WindowMatch ? Number(h3WindowMatch[1]) : 1)
  const totalStudioWindows = job.totalWindows ?? (h3WindowMatch ? Number(h3WindowMatch[2]) : 1)
  const isMultiWindow = totalStudioWindows > 1
  const isMultiClip = (job.totalClips ?? 1) > 1
  const windowEta = formatEtaDuration(job.windowEtaSeconds)
  const windowClock = formatEstimatedClock(job.windowCompletionAt)
  const generationEta = formatEtaDuration(job.generationEtaSeconds)
  const generationClock = formatEstimatedClock(job.generationCompletionAt)
  const activeH3PlanWindow = job.h3WindowPlan?.windows.find(
    window => window.index === activeH3Window,
  ) || job.h3WindowPlan?.windows[0]

  return (
    <div className={`rounded-xl border overflow-hidden ${
      isFailed ? 'border-red-500/30 bg-bg-tertiary' : 'border-accent-blue/30 bg-bg-tertiary'
    }`}>
      <div className="w-full aspect-video flex items-center justify-center relative">
        {/* Dismiss button (top-right, failed only) */}
        {isFailed && (
          <button
            onClick={onDismiss}
            className="absolute top-2 right-2 p-1.5 rounded-full bg-bg-active text-text-secondary hover:bg-red-600 hover:text-white transition-colors z-10"
            title="Dismiss"
          >
            <X size={14} />
          </button>
        )}
        <div className="flex flex-col items-center gap-3 text-text-muted w-full max-w-md px-4">
          <Film size={40} className={isFailed ? 'text-red-400' : 'animate-pulse'} />

          <div className="text-center w-full">
            <p className={`text-sm font-medium ${isFailed ? 'text-red-400' : 'text-text-secondary'}`}>
              {isFailed
                ? (job.status === 'cancelled' ? 'Cancelled' : 'Generation Failed')
                : isPromptPlanning
                  ? 'Planning with AI...'
                  : job.status === 'queued'
                    ? 'Queued...'
                    : 'Generating...'}
            </p>
            {!isFailed && phase && (
              <p className="text-xs mt-1 truncate">{phase}</p>
            )}
            {hasSteps && !isFailed && (
              <p className="text-[10px] text-text-muted mt-0.5">
                Step {job.step}/{job.totalSteps}
              </p>
            )}
            {!isFailed && job.status === 'running' && (
              <div className="mt-1 space-y-0.5 text-[10px] text-text-muted">
                {isMultiClip && (
                  <p>
                    Clip {job.currentClip ?? 1}/{job.totalClips}
                    {isMultiWindow ? ` · Window ${activeH3Window}/${totalStudioWindows}` : ''}
                  </p>
                )}
                {isMultiWindow && windowEta && (
                  <p>
                    Window {activeH3Window}/{totalStudioWindows} · {windowEta} remaining
                    {windowClock ? ` · around ${windowClock}` : ''}
                  </p>
                )}
                {generationEta ? (
                  <p>
                    {isMultiWindow || isMultiClip ? 'Full Studio render' : 'Estimated'} {generationEta}
                    {generationClock ? ` · around ${generationClock}` : ''}
                  </p>
                ) : job.etaConfidence === 'calibrating' ? (
                  <p>Calibrating ETA…</p>
                ) : null}
                {(job.etaHistorySamples ?? 0) > 0 && (
                  <p>
                    Learned from {job.etaHistorySamples} {job.etaHistoryMatch === 'exact' ? 'matching' : 'related'} local render{job.etaHistorySamples === 1 ? '' : 's'}
                  </p>
                )}
              </div>
            )}
            {isFailed && (
              <p className="text-[11px] text-text-secondary mt-2 max-h-24 overflow-y-auto px-2 leading-relaxed whitespace-pre-wrap break-words">
                {errorText}
              </p>
            )}
          </div>

          {/* Progress bar — hidden when failed */}
          {!isFailed && (
            <div className="w-full bg-bg-active rounded-full h-1.5 overflow-hidden">
              {progressPct > 0 ? (
                <div
                  className="h-full bg-accent-green rounded-full transition-all duration-300"
                  style={{ width: `${progressPct}%` }}
                />
              ) : (
                <div className="h-full bg-accent-green/60 rounded-full animate-pulse w-full" />
              )}
            </div>
          )}
        </div>
      </div>

      {job.h3WindowPlan && activeH3PlanWindow && (
        <div className="border-t border-border bg-bg-secondary/60 px-3 py-2">
          <div className="flex items-center justify-between gap-2 text-[10px] text-text-muted">
            <span className="font-medium text-text-secondary">
              Exact H3 prompt · Window {activeH3PlanWindow.index}/{job.h3WindowPlan.window_count}
            </span>
            <button
              type="button"
              onClick={() => setH3PromptDisclosure(current => ({
                signature: h3PlanSignature,
                open: current.signature === h3PlanSignature ? !current.open : true,
              }))}
              className="flex items-center gap-1 text-accent-blue hover:text-accent-blue/80"
            >
              {showH3Prompts ? 'Hide all' : 'View all'}
              {showH3Prompts ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
            </button>
          </div>
          <p className="mt-1 text-[10px] leading-relaxed text-text-muted line-clamp-3 whitespace-pre-wrap break-words">
            {activeH3PlanWindow.prompt}
          </p>
          {showH3Prompts && (
            <div className="mt-2 max-h-80 overflow-y-auto space-y-2 border-t border-border pt-2">
              {job.h3WindowPlan.windows.map(window => (
                <div
                  key={`${window.index}-${window.start_frame}`}
                  className={`rounded-md border p-2 ${
                    window.index === activeH3Window
                      ? 'border-accent-blue/70 bg-accent-blue/5'
                      : 'border-border bg-bg-tertiary/60'
                  }`}
                >
                  <div className="mb-1 flex items-center justify-between text-[9px] text-text-muted">
                    <span>
                      Window {window.index}: {window.title || `Beat ${window.index}`}
                      {window.index === activeH3Window ? ' · Generating now' : ''}
                    </span>
                    <span>{window.start_seconds.toFixed(1)}–{window.end_seconds.toFixed(1)}s</span>
                  </div>
                  <pre className="whitespace-pre-wrap break-words font-sans text-[10px] leading-relaxed text-text-secondary">
                    {window.prompt}
                  </pre>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Bottom bar */}
      <div className="px-3 py-2 min-h-[40px] flex items-center justify-between">
        <div className="text-[11px] text-text-muted truncate flex-1">
          {isFailed ? 'Click × to dismiss — the tile stays so you can see what failed' : phase || 'Preparing...'}
        </div>
        {!isFailed && onStop && (
          <button
            onClick={onStop}
            className="flex items-center gap-1 text-xs text-red-400 hover:text-red-300 transition-colors shrink-0 ml-2"
          >
            <Square size={11} />
            Stop
          </button>
        )}
      </div>
    </div>
  )
}

function PipelinePlaceholder() {
  const pipelineStatus = useStore(s => s.pipelineStatus)
  const pipelineId = useStore(s => s.pipelineId)
  const stopPipeline = useStore(s => s.stopPipeline)
  const resumePipeline = useStore(s => s.resumePipeline)
  const reattachDirectorPipeline = useStore(s => s.reattachDirectorPipeline)
  const [resuming, setResuming] = useState(false)

  if (!pipelineId || !pipelineStatus) return null
  if (pipelineStatus.status === 'completed') return null

  const phase = pipelineStatus.phase || 'planning'
  const progress = pipelineStatus.progress
  const message = progress?.message || phase
  const isFailed = pipelineStatus.status === 'failed' || pipelineStatus.status === 'cancelled'
  const errorText = pipelineStatus.error || message || 'Director pipeline stopped'

  const hasSteps = (progress?.total_steps ?? 0) > 0
  const progressPct = hasSteps
    ? ((progress?.step ?? 0) / progress!.total_steps) * 100
    : progress && progress.total > 0
      ? (progress.current / progress.total) * 100
      : 0
  const phaseLabel = stripTimeSuffix(message)
  const currentClip = progress?.current_clip
  const totalClips = progress?.total_clips
  const clipEta = formatEtaDuration(progress?.clip_eta_seconds)
  const clipClock = formatEstimatedClock(progress?.clip_completion_at)
  const projectEta = formatEtaDuration(progress?.project_eta_seconds)
  const projectClock = formatEstimatedClock(progress?.project_completion_at)

  return (
    <div className={`rounded-xl overflow-hidden border ${isFailed ? 'border-red-500/30' : 'border-accent-blue/30'} bg-bg-tertiary`}>
      <div className="w-full aspect-video flex items-center justify-center relative">
        {isFailed && (
          <button
            type="button"
            onClick={() => useStore.setState({ pipelineId: null, pipelineStatus: null, directorError: null })}
            className="absolute top-2 right-2 p-1.5 rounded-full bg-bg-active text-text-secondary hover:bg-red-600 hover:text-white transition-colors z-10"
            title="Dismiss"
          >
            <X size={14} />
          </button>
        )}
        <div className="flex flex-col items-center gap-3 text-text-muted w-full max-w-xs px-4">
          <Film size={40} className={isFailed ? 'text-red-400' : 'animate-pulse'} />

          <div className="text-center w-full">
            <p className={`text-sm font-medium ${isFailed ? 'text-red-400' : 'text-text-secondary'}`}>
              {isFailed
                ? (pipelineStatus.status === 'cancelled' ? 'Director Cancelled' : 'Director Failed')
                : pipelineStatus.status === 'paused' ? 'Paused — Review' : 'Director'}
            </p>
            {!isFailed && <p className="text-xs mt-1 truncate">{phaseLabel}</p>}
            {hasSteps && !isFailed && (
              <p className="text-[10px] text-text-muted mt-0.5">
                Step {progress!.step}/{progress!.total_steps}
              </p>
            )}
            {!isFailed && currentClip ? (
              <div className="mt-1 space-y-0.5 text-[10px] text-text-muted">
                <p>
                  Clip {currentClip}/{totalClips || '?'}
                  {clipEta
                    ? ` · ${clipEta} remaining${clipClock ? ` · around ${clipClock}` : ''}`
                    : ' · Calibrating ETA…'}
                </p>
                {projectEta && (
                  <p>
                    Full Director render {projectEta}
                    {projectClock ? ` · around ${projectClock}` : ''}
                  </p>
                )}
                {(progress?.eta_history_samples ?? 0) > 0 && (
                  <p>
                    Learned from {progress!.eta_history_samples} {progress!.eta_history_match === 'exact' ? 'matching' : 'related'} local render{progress!.eta_history_samples === 1 ? '' : 's'}
                  </p>
                )}
              </div>
            ) : null}
            {isFailed && (
              <>
                {progress && progress.total > 0 && (
                  <p className="mt-1 text-[10px] text-text-muted">
                    Saved progress: {progress.current}/{progress.total}
                  </p>
                )}
                <p className="text-[11px] text-text-secondary mt-2 max-h-24 overflow-y-auto px-2 leading-relaxed whitespace-pre-wrap break-words">
                  {errorText}
                </p>
              </>
            )}
          </div>

          {/* Progress bar */}
          {!isFailed && (
            <div className="w-full bg-bg-active rounded-full h-1.5 overflow-hidden">
              {progressPct > 0 ? (
                <div
                  className="h-full bg-accent-green rounded-full transition-all duration-300"
                  style={{ width: `${progressPct}%` }}
                />
              ) : (
                <div className="h-full bg-accent-green/60 rounded-full animate-pulse w-full" />
              )}
            </div>
          )}
        </div>
      </div>

      {/* Bottom bar with stop button */}
      <div className="px-3 py-2 min-h-[40px] flex items-center justify-between">
        <div className="text-[11px] text-text-muted truncate flex-1">
          {isFailed ? 'The saved Director checkpoint can be resumed' : phaseLabel || 'Preparing...'}
        </div>
        {isFailed ? (
          <div className="flex items-center gap-2 shrink-0 ml-2">
            <button
              type="button"
              onClick={() => void reattachDirectorPipeline(pipelineId, true)}
              className="text-xs text-text-secondary hover:text-text-primary transition-colors"
            >
              Open Director
            </button>
            <button
              type="button"
              disabled={resuming}
              onClick={async () => {
                setResuming(true)
                try {
                  await resumePipeline(pipelineId)
                } finally {
                  setResuming(false)
                }
              }}
              className="flex items-center gap-1 rounded-md bg-accent-blue px-2 py-1 text-xs text-white hover:bg-accent-blue-hover disabled:opacity-50"
            >
              {resuming ? <Loader2 size={11} className="animate-spin" /> : <Play size={11} />}
              Resume
            </button>
          </div>
        ) : (
          <button
            onClick={() => stopPipeline()}
            className="flex items-center gap-1 text-xs text-red-400 hover:text-red-300 transition-colors shrink-0 ml-2"
          >
            <Square size={11} />
            Stop
          </button>
        )}
      </div>
    </div>
  )
}

export function MainContent() {
  const isMobile = useIsMobile()
  const outputs = useStore(s => s.filteredOutputs())
  // Unfiltered count, to tell "no generations yet" apart from "the filter
  // hides them all" — the first-run text is wrong for the second case.
  const loadedCount = useStore(s => s.outputs.length)
  const outputsTotal = useStore(s => s.outputsTotal)
  const jobs = useStore(s => s.jobs)
  const isEnhancing = useStore(s => s.isEnhancing)
  const generationMode = useStore(s => s.generationMode)
  const audioSubMode = useStore(s => s.audioSubMode)
  const stopGeneration = useStore(s => s.stopGeneration)
  const dismissJob = useStore(s => s.dismissJob)
  // Waiting work now lives in the universal top-bar queue. Keep the gallery
  // focused on media plus useful live/error cards instead of large blank
  // placeholders for every job that has not started yet.
  const galleryJobs = useMemo(
    () => {
      const visibleJobs = jobs.filter(job => (
        job.status !== 'held'
        && job.status !== 'completed'
        && (job.status !== 'queued' || job.showInGallery === true)
      ))
      return isEnhancing ? [PROMPT_ENHANCEMENT_ACTIVITY, ...visibleJobs] : visibleJobs
    },
    [isEnhancing, jobs],
  )

  // The virtualizer that used to live here is gone: measured heights, an
  // offset table, an IntersectionObserver for the active item and a two-phase
  // scroll to keep a thumbnail strip in step. That was the reported scroll
  // trouble, and uniform grid cards make all of it unnecessary — see MediaGrid.

  // Text mode and the audiobook editor own the whole main area instead of
  // the media feed. Placed after every hook above so the hook order stays
  // stable.
  //
  // They MUST keep the same flex-1/min-w-0 wrapper the feed uses: without
  // flex-1 the view only claims its intrinsic width inside App's flex row,
  // which left the panel floating mid-screen with the rest of the window
  // empty. min-w-0 lets long unbroken text shrink instead of pushing the
  // sidebar off-screen.
  const takeoverView =
    generationMode === 'text' ? <ChatView />
    : generationMode === 'audio' && audioSubMode === 'audiobook' ? <AudiobookEditor />
    : generationMode === 'audio' && audioSubMode === 'voices' ? <VoicesView />
    : null
  if (takeoverView) {
    return (
      <main className="flex-1 min-w-0 flex flex-col h-full overflow-hidden">
        {takeoverView}
      </main>
    )
  }

  return (
    <main className="flex-1 min-w-0 flex flex-col h-full overflow-hidden">
      {/* Top bar */}
      <div className="px-2 md:px-6 py-2 md:py-3 border-b border-border flex items-center justify-between gap-2">
        <TabFilter />
        <div className="flex items-center gap-2 shrink-0">
          <div className="hidden text-xs text-text-muted xl:block">
            {outputsTotal > outputs.length
              ? `${outputs.length} / ${outputsTotal} items`
              : `${outputs.length} ${outputs.length === 1 ? 'item' : 'items'}`}
          </div>
          {/* Blueprints belong here, not only on the empty state: once a mode
              had a single output the only way in was a bare bookmark icon in
              the sidebar's bottom bar, which reads as decoration. */}
          <button
            onClick={() => useStore.getState().setRecipesOpen(true)}
            className="flex items-center gap-1.5 rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary shrink-0"
            title="Blueprints — one-click presets for a look"
          >
            <BookMarked size={13} />
            <span className="hidden md:inline">Blueprints</span>
          </button>
          {/* Same reasoning as Blueprints: the only way in was an unlabelled
              globe icon in the sidebar's bottom bar, which is also hidden in
              every mode that owns no generation model. */}
          <button
            onClick={() => useStore.getState().setLoraBrowserOpen(true, useStore.getState().params.model_type)}
            className="flex items-center gap-1.5 rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary shrink-0"
            title="LoRAs — browse CivitAI and the ones you already have"
          >
            <Layers size={13} />
            <span className="hidden md:inline">LoRAs</span>
          </button>
          <WorkspaceSelector />
          {!isMobile && <GlobalQueuePopover />}
        </div>
      </div>

      {/* Content area: queued work, then the gallery as a card grid.
          The queue placeholders stay above it so a running job is visible
          without hunting for it. */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {jobs.length > 0 ? (
          <div className="shrink-0 space-y-3 border-b border-border px-3 py-3 md:px-5">
            <PipelinePlaceholder />
            {galleryJobs.map((j, i) => (
              <JobPlaceholder
                key={j.id || `pending-${i}`}
                job={j}
                onStop={j.kind === 'prompt_enhancement' ? undefined : () => stopGeneration(j.id)}
                onDismiss={() => dismissJob(j.id)}
              />
            ))}
          </div>
        ) : (
          <PipelinePlaceholder />
        )}

        {outputs.length === 0 && jobs.length === 0 && loadedCount > 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
            <Film size={22} className="text-text-muted opacity-40" />
            <p className="text-sm text-text-secondary">Nothing matches this filter.</p>
            <p className="text-[11px] text-text-muted">
              {loadedCount} item{loadedCount === 1 ? '' : 's'} in this workspace —
              switch the type filter back to All, or clear the search.
            </p>
          </div>
        ) : outputs.length === 0 && jobs.length === 0 ? (() => {
          const noun = generationMode === 'image' ? 'images'
            : generationMode === 'audio' ? 'audio' : 'videos'
          const example = generationMode === 'image'
            ? 'a neon city street at night, cinematic'
            : generationMode === 'audio'
            ? 'a dreamy synthwave track about the ocean'
            : 'a golden retriever surfing a big wave, slow motion'
          return (
          <div className="flex flex-1 items-center justify-center px-6">
            <div className="flex max-w-sm flex-col items-center gap-4 text-center">
              <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-bg-active text-text-muted">
                <Film size={26} />
              </div>
              <p className="text-sm text-text-secondary">Your generated {noun} will appear here.</p>
              <ol className="space-y-1.5 text-left text-xs text-text-muted">
                <li><span className="font-medium text-accent-blue">1.</span> Pick a model in the sidebar (a good default is already selected).</li>
                <li><span className="font-medium text-accent-blue">2.</span> Type a prompt — e.g. <span className="italic text-text-secondary">“{example}”</span></li>
                <li><span className="font-medium text-accent-blue">3.</span> Hit Forge.</li>
              </ol>
              <p className="text-[11px] leading-snug text-text-muted">
                Heads up: the first time you use a model, its weights download
                once (often tens of GB) before generation starts — later runs
                are fast. Progress shows at the bottom-right.
              </p>
              <button
                onClick={() => useStore.getState().setRecipesOpen(true)}
                className="mt-1 flex items-center gap-1.5 rounded-lg border border-accent-blue/30 bg-accent-blue/10 px-3 py-1.5 text-xs text-accent-blue transition-colors hover:bg-accent-blue/20"
              >
                <BookMarked size={13} /> Browse blueprints
              </button>
            </div>
          </div>
          )
        })() : (
          <MediaGrid />
        )}
      </div>
    </main>
  )
}
