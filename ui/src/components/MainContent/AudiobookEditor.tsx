import { useEffect, useMemo, useRef, useState } from 'react'
import { BookAudio, Play, Loader2, AlertTriangle, X, Headphones, RefreshCw, Wand2, Scissors, Volume2 } from 'lucide-react'
import { useStore, PASSAGE_PREVIEW_KEY } from '../../stores/useStore'
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
  const setAbChapter = useStore(s => s.setAbChapter)
  const planChapter = useStore(s => s.planAbChapter)
  const renderChapter = useStore(s => s.renderAbChapter)
  const assisting = useStore(s => s.abAssisting)
  const splitProposal = useStore(s => s.abSplitProposal)
  const castProposal = useStore(s => s.abCastProposal)
  const suggestSplit = useStore(s => s.suggestAbSplit)
  const applySplit = useStore(s => s.applyAbSplit)
  const suggestCast = useStore(s => s.suggestAbCast)
  const applyCast = useStore(s => s.applyAbCast)
  const clearProposals = useStore(s => s.clearAbProposals)
  const previewPassage = useStore(s => s.previewAbPassage)
  const previewBusy = useStore(s => s.voicePreviewBusy)
  const passageUrl = useStore(s => s.voicePreviewUrls[PASSAGE_PREVIEW_KEY])
  const passageWarnings = useStore(s => s.voicePreviewWarnings[PASSAGE_PREVIEW_KEY])

  const passageBusy = previewBusy === PASSAGE_PREVIEW_KEY

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

  /** Speak just the marked text with the voice that run already carries, so a
   *  voice or emotion can be judged without rendering the chapter. Falls back
   *  to the project default when the run has no voice of its own — the same
   *  rule the renderer applies. */
  const previewSelection = () => {
    if (!popover || !chapter) return
    const run = chapter.blocks
      .find(b => b.id === popover.blockId)?.runs
      ?.find(r => r.id === popover.runId)
    const text = (run?.text ?? '').slice(popover.from, popover.to)
    if (!text.trim()) return
    previewPassage(text, {
      profileId: run?.profile_id ?? project?.default_profile_id ?? null,
      emotion: run?.overrides?.emotion ?? null,
    })
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
              onClick={() => suggestCast()}
              disabled={!chapter || assisting !== null}
              className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary disabled:opacity-40"
              title="Let the LLM propose speakers, emotions and sound effects"
            >
              {assisting === 'cast'
                ? <Loader2 size={12} className="mr-1 inline animate-spin" />
                : <Wand2 size={12} className="mr-1 inline" />}
              Cast
            </button>
            <button
              onClick={() => suggestSplit()}
              disabled={!chapter || assisting !== null}
              className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary disabled:opacity-40"
              title="Let the LLM propose where this chapter should break"
            >
              {assisting === 'split'
                ? <Loader2 size={12} className="mr-1 inline animate-spin" />
                : <Scissors size={12} className="mr-1 inline" />}
              Split
            </button>
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

        {/* Passage preview — separate from the chapter render above so it
            never disturbs the karaoke player. `key` remounts the element so
            autoplay fires again for each new take. */}
        {passageBusy && (
          <p className="mt-2 flex items-center gap-1.5 text-[11px] text-text-secondary">
            <Loader2 size={12} className="animate-spin" />
            Speaking the selected passage — the first use of a model downloads it.
          </p>
        )}
        {passageUrl && !passageBusy && (
          <div className="mt-2">
            <div className="mb-0.5 flex items-center gap-1 text-[10px] text-text-muted">
              <Volume2 size={10} /> Passage preview
            </div>
            <audio key={passageUrl} src={passageUrl} controls autoPlay className="h-8 w-full" />
          </div>
        )}
        {!passageBusy && passageWarnings && passageWarnings.length > 0 && (
          <p className="mt-1 text-[10px] text-indicator-warning">{passageWarnings.join(' · ')}</p>
        )}
      </div>

      <div className="flex min-h-0 flex-1">
        {/* Chapter navigation — the book is read here, not in the sidebar. */}
        {project.chapters.length > 0 && (
          <nav className="w-52 shrink-0 overflow-y-auto border-r border-border p-2">
            {project.chapters.map((c, i) => (
              <button
                key={c.id}
                onClick={() => setAbChapter(c.id)}
                className={`mb-0.5 block w-full rounded-md px-2 py-1.5 text-left ${
                  c.id === chapterId ? 'bg-bg-active' : 'hover:bg-bg-hover'
                }`}
              >
                <div className="truncate text-[11px] text-text-primary">
                  {i + 1}. {c.title || 'Untitled'}
                </div>
                <div className="text-[9px] text-text-muted">
                  {c.blocks.length} blocks
                  {c.audio_path
                    ? ` · ${Math.round(c.audio_duration ?? 0)}s rendered`
                    : ' · not rendered'}
                </div>
              </button>
            ))}
          </nav>
        )}

        {/* Reading pane */}
        <div className="min-w-0 flex-1 overflow-y-auto">
        {!chapter ? (
          <div className="flex h-full items-center justify-center">
            <p className="text-xs text-text-secondary">
              {project.chapters.length === 0
                ? 'Import a text in the sidebar — its chapters land here.'
                : 'Pick a chapter on the left.'}
            </p>
          </div>
        ) : chapter.blocks.length === 0 ? (
          <div className="flex h-full items-center justify-center p-8">
            <p className="max-w-sm text-center text-xs text-text-secondary">
              This chapter is empty. Import a document in the sidebar.
            </p>
          </div>
        ) : (
          <article className="mx-auto max-w-[72ch] px-6 py-6 space-y-3">
            <h2 className="mb-4 text-base font-semibold text-text-primary">
              {(project.chapters.findIndex(c => c.id === chapter.id) + 1)}. {chapter.title || 'Untitled'}
            </h2>
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
          </article>
        )}
        </div>
      </div>

      {/* Selection popover */}
      {popover && (
        <div
          className="glass-panel fixed z-50 w-56 rounded-xl p-2 shadow-2xl"
          style={{ left: Math.min(popover.x, window.innerWidth - 240), top: popover.y + 8 }}
          onMouseDown={e => e.stopPropagation()}
        >
          <button
            onClick={previewSelection}
            disabled={previewBusy !== null}
            className="mb-2 flex w-full items-center gap-1.5 rounded-lg border border-border px-1.5 py-1 text-[11px] text-text-primary transition-colors hover:border-border-light hover:bg-bg-hover disabled:opacity-40"
            title="Speak just this selection with the voice it carries"
          >
            {passageBusy
              ? <Loader2 size={11} className="animate-spin" />
              : <Volume2 size={11} />}
            Preview selection
          </button>

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

          {project.sfx.length === 0 && (
            <p className="mt-2 text-[10px] text-text-muted">
              No effects yet — generate one or reuse an existing file under
              Effects → From library in the sidebar.
            </p>
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

      {/* Split review */}
      {splitProposal && (
        <ReviewDialog
          title="Suggested chapter breaks"
          dropped={splitProposal.dropped}
          onClose={clearProposals}
          empty={splitProposal.splits.length === 0}
          emptyText="The model found no good break points in this chapter."
          items={splitProposal.splits.map(s => ({
            key: s.after_block_id,
            primary: s.new_title,
            secondary: s.reason || 'break after this paragraph',
          }))}
          applyLabel="Split chapter"
          onApply={keys => applySplit(splitProposal.splits.filter(s => keys.has(s.after_block_id)))}
        />
      )}

      {/* Cast review */}
      {castProposal && (
        <ReviewDialog
          title="Suggested cast"
          dropped={castProposal.dropped}
          onClose={clearProposals}
          empty={castProposal.assignments.length === 0}
          emptyText="The model returned nothing usable for this chapter."
          groups={[
            {
              label: `Characters (${castProposal.characters.length})`,
              items: castProposal.characters.map(c => ({
                key: `char:${c.name}`,
                primary: c.name,
                secondary: [c.gender, c.description].filter(Boolean).join(' · ') || 'new voice profile',
              })),
            },
            {
              label: `Lines (${castProposal.assignments.length})`,
              items: castProposal.assignments.map(a => ({
                key: `run:${a.run_id}`,
                primary: a.speaker,
                secondary: a.emotion ? `emotion: ${a.emotion}` : 'no emotion',
              })),
            },
            {
              label: `Effects (${castProposal.effects.length})`,
              items: castProposal.effects.map(e => ({
                key: `eff:${e.block_id}`,
                primary: e.label,
                secondary: `${e.prompt} · ${e.playback_mode}`,
              })),
            },
          ]}
          applyLabel="Apply selected"
          onApply={keys => applyCast({
            characters: castProposal.characters.filter(c => keys.has(`char:${c.name}`)),
            assignments: castProposal.assignments.filter(a => keys.has(`run:${a.run_id}`)),
            effects: castProposal.effects.filter(e => keys.has(`eff:${e.block_id}`)),
          })}
        />
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


interface ReviewItem { key: string; primary: string; secondary: string }

/**
 * Checkbox review for LLM proposals — nothing is applied without the user
 * ticking it. Everything starts selected, because the common case is
 * accepting most of it and dropping a few, not the other way round.
 */
function ReviewDialog({ title, items, groups, dropped, empty, emptyText, applyLabel, onApply, onClose }: {
  title: string
  items?: ReviewItem[]
  groups?: { label: string; items: ReviewItem[] }[]
  dropped: number
  empty: boolean
  emptyText: string
  applyLabel: string
  onApply: (keys: Set<string>) => void
  onClose: () => void
}) {
  const all = groups ? groups.flatMap(g => g.items) : (items ?? [])
  const [picked, setPicked] = useState<Set<string>>(() => new Set(all.map(i => i.key)))
  const toggle = (key: string) => setPicked(prev => {
    const next = new Set(prev)
    if (next.has(key)) next.delete(key); else next.add(key)
    return next
  })

  const renderItems = (list: ReviewItem[]) => list.map(item => (
    <label
      key={item.key}
      className="flex cursor-pointer items-start gap-2 rounded-md px-1.5 py-1 hover:bg-bg-hover"
    >
      <input
        type="checkbox"
        checked={picked.has(item.key)}
        onChange={() => toggle(item.key)}
        className="mt-0.5 h-3.5 w-3.5 shrink-0 rounded border-border bg-bg-tertiary accent-accent-blue"
      />
      <span className="min-w-0">
        <span className="block truncate text-[11px] text-text-primary">{item.primary}</span>
        <span className="block text-[10px] text-text-muted">{item.secondary}</span>
      </span>
    </label>
  ))

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      <div className="glass-panel relative flex max-h-[85vh] w-full flex-col rounded-2xl shadow-2xl md:w-[560px]">
        <div className="flex shrink-0 items-center justify-between border-b border-border px-5 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          <div className="flex items-center gap-2">
            {all.length > 0 && (
              <button
                onClick={() => setPicked(p => (p.size === all.length ? new Set() : new Set(all.map(i => i.key))))}
                className="text-[10px] text-accent-blue hover:text-accent-blue-hover"
              >
                {picked.size === all.length ? 'None' : 'All'}
              </button>
            )}
            <button onClick={onClose} aria-label="Close" className="rounded-lg p-1.5 text-text-secondary hover:bg-bg-hover hover:text-text-primary">
              <X size={16} />
            </button>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-3">
          {empty ? (
            <p className="text-xs text-text-secondary">{emptyText}</p>
          ) : groups ? (
            <div className="space-y-3">
              {groups.filter(g => g.items.length > 0).map(g => (
                <div key={g.label}>
                  <div className="mb-1 text-[10px] uppercase tracking-wider text-text-muted">{g.label}</div>
                  {renderItems(g.items)}
                </div>
              ))}
            </div>
          ) : renderItems(items ?? [])}

          {dropped > 0 && (
            <p className="mt-3 text-[10px] text-indicator-warning">
              {dropped} suggestion{dropped === 1 ? '' : 's'} referenced text that
              does not exist and {dropped === 1 ? 'was' : 'were'} discarded.
            </p>
          )}
        </div>

        {!empty && (
          <div className="flex shrink-0 justify-end gap-2 border-t border-border px-5 py-3">
            <button onClick={onClose} className="rounded-lg border border-border px-3 py-1.5 text-xs text-text-secondary hover:text-text-primary">
              Cancel
            </button>
            <button
              onClick={() => onApply(picked)}
              disabled={picked.size === 0}
              className="rounded-lg bg-cta px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
            >
              {applyLabel} ({picked.size})
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
