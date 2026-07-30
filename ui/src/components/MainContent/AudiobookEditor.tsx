import { useEffect, useMemo, useRef, useState } from 'react'
import { BookAudio, Play, Loader2, AlertTriangle, X, Headphones, RefreshCw } from 'lucide-react'
import { useStore } from '../../stores/useStore'
import type { AudiobookBlock, AudiobookRun } from '../../api/client'

const EMOTIONS = ['neutral', 'happy', 'sad', 'angry', 'fearful', 'whispering', 'excited', 'tender', 'cheerful']

/** Split a run at [from, to) so a selection can carry its own voice, then
 *  merge neighbours that ended up identical. Runs — not character offsets —
 *  are the unit of assignment, which is why an edit elsewhere in the
 *  paragraph can never shift a voice binding. */
function assignToSelection(
  runs: AudiobookRun[],
  runId: string,
  from: number,
  to: number,
  patch: Partial<AudiobookRun>,
): AudiobookRun[] {
  const out: AudiobookRun[] = []
  for (const run of runs) {
    if (run.id !== runId) { out.push(run); continue }
    const start = Math.max(0, Math.min(from, run.text.length))
    const end = Math.max(start, Math.min(to, run.text.length))
    const before = run.text.slice(0, start)
    const middle = run.text.slice(start, end)
    const after = run.text.slice(end)
    if (!middle) { out.push(run); continue }
    if (before) out.push({ ...run, id: `${run.id}a${out.length}`, text: before })
    out.push({ ...run, ...patch, id: `${run.id}b${out.length}`, text: middle })
    if (after) out.push({ ...run, id: `${run.id}c${out.length}`, text: after })
  }
  return mergeRuns(out)
}

function mergeRuns(runs: AudiobookRun[]): AudiobookRun[] {
  const out: AudiobookRun[] = []
  for (const run of runs) {
    const prev = out[out.length - 1]
    const same = prev
      && (prev.profile_id ?? null) === (run.profile_id ?? null)
      && JSON.stringify(prev.overrides ?? null) === JSON.stringify(run.overrides ?? null)
    if (same) prev.text += run.text
    else out.push({ ...run })
  }
  return out.filter(r => r.text.length > 0)
}

export function AudiobookEditor() {
  const project = useStore(s => s.activeAudiobook)
  const chapterId = useStore(s => s.activeAbChapterId)
  const plan = useStore(s => s.abPlan)
  const renderJob = useStore(s => s.abRenderJobId)
  const renderMessage = useStore(s => s.abRenderMessage)
  const audioUrl = useStore(s => s.abAudioUrl)
  const timeline = useStore(s => s.abTimeline)
  const error = useStore(s => s.abError)
  const patchAudiobook = useStore(s => s.patchAudiobook)
  const planChapter = useStore(s => s.planAbChapter)
  const renderChapter = useStore(s => s.renderAbChapter)

  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [popover, setPopover] = useState<{ runId: string; blockId: string; from: number; to: number; x: number; y: number } | null>(null)
  const [showPlan, setShowPlan] = useState(false)

  const chapter = useMemo(
    () => project?.chapters.find(c => c.id === chapterId) ?? null,
    [project, chapterId],
  )
  const voices = project?.voice_profiles ?? []
  const voiceOf = (id?: string | null) => voices.find(v => v.id === id) ?? null

  // Karaoke: map playback position onto the speech entry that owns it.
  useEffect(() => {
    const el = audioRef.current
    if (!el || timeline.length === 0) return
    const onTime = () => {
      const t = el.currentTime
      const hit = timeline.find(e => e.kind === 'speech' && t >= e.start && t < e.end)
      setActiveRunId(hit?.run_id ?? null)
    }
    el.addEventListener('timeupdate', onTime)
    return () => el.removeEventListener('timeupdate', onTime)
  }, [timeline, audioUrl])

  const saveBlocks = (blocks: AudiobookBlock[]) => {
    if (!project || !chapter) return
    patchAudiobook({
      chapters: project.chapters.map(c => (c.id === chapter.id ? { ...c, blocks } : c)),
    })
  }

  /** Couple an effect or a music bed to the paragraph the selection is in.
   *  Ambience runs alongside the speech; the render ducks it automatically. */
  const attachToBlock = (kind: 'sfx' | 'music', assetId: string | null) => {
    if (!popover || !chapter) return
    saveBlocks(chapter.blocks.map(b => {
      if (b.id !== popover.blockId) return b
      if (kind === 'sfx') {
        return { ...b, attached_sfx: assetId ? { sfx_id: assetId, loop: true, volume: 0.35 } : null }
      }
      return { ...b, attached_music: assetId ? { music_id: assetId, loop: true, volume: 0.25 } : null }
    }))
    setPopover(null)
  }

  const applyToSelection = (patch: Partial<AudiobookRun>) => {
    if (!popover || !chapter) return
    saveBlocks(chapter.blocks.map(b =>
      b.type === 'paragraph' && b.runs
        ? { ...b, runs: assignToSelection(b.runs, popover.runId, popover.from, popover.to, patch) }
        : b,
    ))
    setPopover(null)
  }

  if (!project) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="max-w-md text-center">
          <BookAudio size={28} className="mx-auto mb-3 text-text-muted" />
          <h2 className="text-sm font-semibold text-text-primary">No audiobook open</h2>
          <p className="mt-1 text-xs text-text-secondary">
            Create a project in the sidebar, import a text, then assign voices to
            paragraphs. Rendering mixes speech, effects and music on the server,
            so it keeps running if you close the tab.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col overflow-hidden" onMouseDown={() => setPopover(null)}>
      {/* Header */}
      <div className="shrink-0 border-b border-border px-5 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold text-text-primary">{project.title}</h1>
            <p className="mt-0.5 text-[11px] text-text-muted">
              {chapter ? `${chapter.title || 'Untitled'} · ${chapter.blocks.length} blocks` : 'No chapter selected'}
              {' · '}{voices.length} voices
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <button
              onClick={async () => { await planChapter(); setShowPlan(true) }}
              disabled={!chapter}
              className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary disabled:opacity-40"
              title="Check whether this chapter can be rendered"
            >
              Check
            </button>
            <button
              onClick={() => renderChapter({ format: 'mp3' })}
              disabled={!chapter || !!renderJob}
              className="rounded-lg bg-cta px-2.5 py-1 text-[11px] font-medium text-white shadow-accent-glow disabled:opacity-40"
            >
              {renderJob ? <Loader2 size={12} className="mr-1 inline animate-spin" /> : <Play size={12} className="mr-1 inline" />}
              {renderJob ? 'Rendering…' : 'Render chapter'}
            </button>
            <button
              onClick={() => renderChapter({ book: true, format: 'm4b' })}
              disabled={!!renderJob || project.chapters.length === 0}
              className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary disabled:opacity-40"
              title="Render every chapter and join them into an M4B with chapter markers"
            >
              <Headphones size={12} className="mr-1 inline" /> Book
            </button>
          </div>
        </div>

        {renderJob && (
          <p className="mt-2 text-[11px] text-text-secondary">{renderMessage || 'Working…'}</p>
        )}
        {error && <p className="mt-2 text-[11px] text-red-400">{error}</p>}

        {chapter && project.music.length > 0 && (
          <div className="mt-2 flex items-center gap-2">
            <label className="text-[10px] text-text-muted">Chapter music</label>
            <select
              value={chapter.music_id ?? ''}
              onChange={e => patchAudiobook({
                chapters: project.chapters.map(c =>
                  c.id === chapter.id ? { ...c, music_id: e.target.value || null } : c),
              })}
              className="rounded border border-border bg-bg-tertiary px-1.5 py-0.5 text-[10px] text-text-primary"
            >
              <option value="">None</option>
              {project.music.map(m => (
                <option key={m.id} value={m.id} disabled={!m.audio_path}>
                  {m.title}{m.audio_path ? '' : ' (generating…)'}
                </option>
              ))}
            </select>
            <span className="text-[9px] text-text-muted">ducks automatically under speech</span>
          </div>
        )}

        {audioUrl && (
          <audio ref={audioRef} src={audioUrl} controls className="mt-2 h-8 w-full" />
        )}
      </div>

      {/* Blocks */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {!chapter ? (
          <div className="flex h-full items-center justify-center">
            <p className="text-xs text-text-secondary">Pick a chapter in the sidebar.</p>
          </div>
        ) : chapter.blocks.length === 0 ? (
          <div className="flex h-full items-center justify-center p-8">
            <p className="max-w-sm text-center text-xs text-text-secondary">
              This chapter is empty. Import a document in the sidebar.
            </p>
          </div>
        ) : (
          <div className="mx-auto max-w-[72ch] px-6 py-6 space-y-3">
            {chapter.blocks.map(block => {
              if (block.type === 'sfx') {
                const asset = project.sfx.find(s => s.id === block.sfx_id)
                return (
                  <div key={block.id} className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-[11px] text-text-secondary">
                    Sound effect: {asset?.label || block.sfx_id}
                  </div>
                )
              }
              const ambience = block.attached_sfx
                ? project.sfx.find(s => s.id === block.attached_sfx?.sfx_id)
                : null
              const blockMusic = block.attached_music
                ? project.music.find(m => m.id === block.attached_music?.music_id)
                : null
              return (
                <div key={block.id}>
                  {(ambience || blockMusic) && (
                    <div className="mb-1 flex flex-wrap gap-1">
                      {ambience && (
                        <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[9px] text-text-secondary">
                          ambience: {ambience.label}
                        </span>
                      )}
                      {blockMusic && (
                        <span className="rounded-full border border-accent-warm/40 bg-accent-warm/10 px-1.5 py-0.5 text-[9px] text-text-secondary">
                          music: {blockMusic.title}
                        </span>
                      )}
                    </div>
                  )}
                <p className="text-[15px] leading-[1.8] text-text-primary">
                  {(block.runs ?? []).map(run => {
                    const voice = voiceOf(run.profile_id)
                    const isActive = activeRunId === run.id
                    return (
                      <span
                        key={run.id}
                        onMouseUp={e => {
                          const sel = window.getSelection()
                          if (!sel || sel.isCollapsed) return
                          const text = sel.toString()
                          const offset = run.text.indexOf(text)
                          if (offset < 0) return
                          e.stopPropagation()
                          setPopover({
                            runId: run.id,
                            blockId: block.id,
                            from: offset,
                            to: offset + text.length,
                            x: e.clientX,
                            y: e.clientY,
                          })
                        }}
                        title={voice ? `${voice.name}${run.overrides?.emotion ? ` · ${run.overrides.emotion}` : ''}` : 'No voice assigned'}
                        className={isActive ? 'rounded bg-accent-blue/25' : undefined}
                        style={voice ? {
                          backgroundColor: isActive ? undefined : `${voice.color}22`,
                          boxShadow: `inset 0 -2px 0 ${voice.color}`,
                        } : undefined}
                      >
                        {run.text}
                        {run.overrides?.emotion && (
                          <sup
                            onClick={e => {
                              e.stopPropagation()
                              saveBlocks(chapter.blocks.map(b =>
                                b.id === block.id && b.runs
                                  ? { ...b, runs: mergeRuns(b.runs.map(r => (r.id === run.id ? { ...r, overrides: null } : r))) }
                                  : b,
                              ))
                            }}
                            className="ml-0.5 cursor-pointer text-[9px] text-accent-warm"
                            title="Remove emotion"
                          >
                            [{run.overrides.emotion}]
                          </sup>
                        )}
                      </span>
                    )
                  })}
                </p>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Selection popover */}
      {popover && (
        <div
          className="glass-panel fixed z-50 w-56 rounded-xl p-2 shadow-2xl"
          style={{ left: Math.min(popover.x, window.innerWidth - 240), top: popover.y + 8 }}
          onMouseDown={e => e.stopPropagation()}
        >
          <div className="mb-1 text-[10px] uppercase tracking-wider text-text-muted">Voice</div>
          {voices.length === 0 ? (
            <p className="text-[10px] text-text-muted">Add a voice in the sidebar first.</p>
          ) : (
            <div className="space-y-0.5">
              {voices.map(v => (
                <button
                  key={v.id}
                  onClick={() => applyToSelection({ profile_id: v.id })}
                  className="flex w-full items-center gap-1.5 rounded px-1.5 py-1 text-left text-[11px] text-text-primary hover:bg-bg-hover"
                >
                  <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: v.color }} />
                  {v.name}
                </button>
              ))}
            </div>
          )}

          {project.sfx.length > 0 && (
            <>
              <div className="mt-2 mb-1 text-[10px] uppercase tracking-wider text-text-muted">Ambience</div>
              <div className="space-y-0.5">
                {project.sfx.map(a => (
                  <button
                    key={a.id}
                    onClick={() => attachToBlock('sfx', a.id)}
                    disabled={!a.audio_path}
                    className="block w-full truncate rounded px-1.5 py-1 text-left text-[10px] text-text-primary hover:bg-bg-hover disabled:opacity-40"
                    title={a.audio_path ? a.prompt : 'Still generating'}
                  >
                    {a.label}
                  </button>
                ))}
                <button
                  onClick={() => attachToBlock('sfx', null)}
                  className="block w-full rounded px-1.5 py-1 text-left text-[10px] text-text-muted hover:bg-bg-hover"
                >
                  Remove ambience
                </button>
              </div>
            </>
          )}

          {project.music.length > 0 && (
            <>
              <div className="mt-2 mb-1 text-[10px] uppercase tracking-wider text-text-muted">Music here</div>
              <div className="space-y-0.5">
                {project.music.map(a => (
                  <button
                    key={a.id}
                    onClick={() => attachToBlock('music', a.id)}
                    disabled={!a.audio_path}
                    className="block w-full truncate rounded px-1.5 py-1 text-left text-[10px] text-text-primary hover:bg-bg-hover disabled:opacity-40"
                  >
                    {a.title}
                  </button>
                ))}
                <button
                  onClick={() => attachToBlock('music', null)}
                  className="block w-full rounded px-1.5 py-1 text-left text-[10px] text-text-muted hover:bg-bg-hover"
                >
                  Back to chapter music
                </button>
              </div>
            </>
          )}

          <div className="mt-2 mb-1 text-[10px] uppercase tracking-wider text-text-muted">Emotion</div>
          <div className="flex flex-wrap gap-1">
            {EMOTIONS.map(em => (
              <button
                key={em}
                onClick={() => {
                  // The backend drops overrides on runs with no profile_id,
                  // so stamp the default voice alongside the emotion or it
                  // would silently vanish on save.
                  const fallback = project.default_profile_id ?? voices[0]?.id ?? null
                  applyToSelection({
                    profile_id: fallback,
                    overrides: { emotion: em === 'neutral' ? undefined : em },
                  })
                }}
                className="rounded-full border border-border px-1.5 py-0.5 text-[10px] text-text-secondary hover:border-border-light hover:text-text-primary"
              >
                {em}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Plan dialog */}
      {showPlan && plan && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={() => setShowPlan(false)} />
          <div className="glass-panel relative flex max-h-[85vh] w-full flex-col rounded-2xl shadow-2xl md:w-[520px]">
            <div className="flex shrink-0 items-center justify-between border-b border-border px-5 py-3">
              <h2 className="text-sm font-semibold">
                {plan.ready ? 'Ready to render' : 'Not ready yet'}
              </h2>
              <button onClick={() => setShowPlan(false)} aria-label="Close" className="rounded-lg p-1.5 text-text-secondary hover:bg-bg-hover hover:text-text-primary">
                <X size={16} />
              </button>
            </div>
            <div className="min-h-0 space-y-3 overflow-y-auto px-5 py-4 text-xs">
              {plan.errors.length > 0 && (
                <div>
                  <div className="mb-1 flex items-center gap-1.5 font-medium text-red-400">
                    <AlertTriangle size={13} /> Blocking problems
                  </div>
                  <ul className="list-inside list-disc space-y-0.5 text-text-secondary">
                    {plan.errors.map((e, i) => <li key={i}>{e}</li>)}
                  </ul>
                </div>
              )}
              <p className="text-text-secondary">
                {plan.runs.length} speech runs ·{' '}
                {Math.round(plan.runs.reduce((n, r) => n + (r.estimated_seconds ?? 0), 0))}s estimated
              </p>
              {plan.runs.some(r => r.warnings.length > 0) && (
                <div>
                  <div className="mb-1 font-medium text-indicator-warning">Warnings</div>
                  <ul className="list-inside list-disc space-y-0.5 text-text-secondary">
                    {[...new Set(plan.runs.flatMap(r => r.warnings))].map((w, i) => <li key={i}>{w}</li>)}
                  </ul>
                </div>
              )}
              {plan.ready && (
                <button
                  onClick={() => { setShowPlan(false); renderChapter({ format: 'mp3', force: true }) }}
                  className="flex items-center gap-1.5 rounded-lg bg-cta px-3 py-1.5 text-xs font-medium text-white"
                >
                  <RefreshCw size={12} /> Render now
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
