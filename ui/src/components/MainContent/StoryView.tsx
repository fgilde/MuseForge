import { useEffect, useMemo, useRef, useState } from 'react'
import {
  BookText, Copy, Check, Pencil, RefreshCw, Plus, Download, FileText,
  ChevronDown, ChevronUp, X, Loader2,
} from 'lucide-react'
import { useStore } from '../../stores/useStore'
import { WORDS_PER_PAGE } from '../../lib/storyEstimate'

const ACTIVE = new Set(['queued', 'planning', 'writing'])

/** Split a raw stream buffer into its thinking block and the visible text.
 *  Same shape the chat view uses — models emit <think>…</think> and we must
 *  not present that as prose. */
function splitThinking(raw: string): { thinking: string; body: string } {
  const m = raw.match(/^\s*<think(?:ing)?>([\s\S]*?)(?:<\/think(?:ing)?>|$)/)
  if (!m) return { thinking: '', body: raw }
  return { thinking: m[1], body: raw.slice(m[0].length) }
}

export function StoryView() {
  const story = useStore(s => s.activeStory)
  const streamText = useStore(s => s.storyStreamText)
  const error = useStore(s => s.storyError)
  const regenerateChapter = useStore(s => s.regenerateChapter)
  const saveChapterText = useStore(s => s.saveChapterText)
  const extendActiveStory = useStore(s => s.extendActiveStory)
  const exportActiveStory = useStore(s => s.exportActiveStory)
  const setStoryDraft = useStore(s => s.setStoryDraft)

  /** The chapter the user explicitly opened. While null the view follows
   *  the newest written chapter, so a running story reads as it grows
   *  without a state-syncing effect. */
  const [pinned, setPinned] = useState<number | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [regenFor, setRegenFor] = useState<number | null>(null)
  const [instruction, setInstruction] = useState('')
  const [showPrompt, setShowPrompt] = useState(false)
  const [showThinking, setShowThinking] = useState(false)
  const [exported, setExported] = useState<string | null>(null)
  const streamRef = useRef<HTMLDivElement | null>(null)

  const running = !!story && ACTIVE.has(story.status)
  const chapters = useMemo(() => story?.chapters ?? [], [story?.chapters])
  const lastWritten = useMemo(() => {
    for (let i = chapters.length - 1; i >= 0; i--) if (chapters[i].text) return i
    return 0
  }, [chapters])
  // Derived, not stored: the newest written chapter unless the user pinned one.
  const selected = Math.min(pinned ?? lastWritten, Math.max(0, chapters.length - 1))
  const anyWritten = chapters.some(c => c.text)

  useEffect(() => {
    if (streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight
  }, [streamText])

  const totalWords = chapters.reduce((n, c) => n + (c.word_count ?? 0), 0)
  const chapter = chapters[selected]
  const { thinking, body } = splitThinking(streamText)

  if (!story) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="max-w-md text-center">
          <BookText size={28} className="mx-auto mb-3 text-text-muted" />
          <h2 className="text-sm font-semibold text-text-primary">No story open</h2>
          <p className="mt-1 text-xs text-text-secondary">
            Describe a premise in the sidebar and press Forge story. An outline
            is planned first, then chapters are written one at a time — you can
            read along as they land.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="shrink-0 border-b border-border px-5 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold text-text-primary">
              {story.title || 'Untitled story'}
            </h1>
            <p className="mt-0.5 text-[11px] text-text-muted">
              {chapters.length} chapters · {totalWords.toLocaleString()} words ·
              ~{Math.max(1, Math.round(totalWords / WORDS_PER_PAGE))} pages
              {story.status !== 'completed' && ` · ${story.status}`}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <button
              onClick={() => setShowPrompt(true)}
              className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:text-text-primary hover:border-border-light transition-colors"
              title="Show the settings and prompts behind this story"
            >
              <FileText size={12} className="mr-1 inline" /> Prompt
            </button>
            <button
              onClick={async () => {
                const p = await exportActiveStory('md')
                if (p) { setExported(p); setTimeout(() => setExported(null), 4000) }
              }}
              disabled={!anyWritten}
              className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:text-text-primary hover:border-border-light transition-colors disabled:opacity-40"
              title="Write a .md file into the workspace"
            >
              <Download size={12} className="mr-1 inline" /> Export
            </button>
            {!running && story.status === 'completed' && (
              <button
                onClick={() => extendActiveStory(3)}
                className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:text-text-primary hover:border-border-light transition-colors"
                title="Write three more chapters, continuing the story"
              >
                <Plus size={12} className="mr-1 inline" /> Extend
              </button>
            )}
          </div>
        </div>

        {exported && (
          <p className="mt-2 text-[11px] text-indicator-success">Exported to {exported}</p>
        )}
        {error && (
          <p className="mt-2 text-[11px] text-red-400">{error}</p>
        )}
        {story.error && (
          <p className="mt-2 text-[11px] text-red-400">{story.error}</p>
        )}

        {/* Progress while writing */}
        {running && story.progress && (
          <div className="mt-2">
            <div className="flex items-center justify-between text-[11px] text-text-secondary">
              <span>{story.progress.message || 'Working…'}</span>
              <span className="tabular-nums">
                {story.progress.total_steps > 0
                  ? `${story.progress.step}/${story.progress.total_steps} passes`
                  : `${story.progress.current}/${story.progress.total}`}
              </span>
            </div>
            <div className="mt-1 h-1 overflow-hidden rounded-full bg-bg-tertiary">
              <div
                className="h-full bg-accent-blue transition-all"
                style={{
                  width: `${Math.min(100, story.progress.total_steps > 0
                    ? (story.progress.step / Math.max(1, story.progress.total_steps)) * 100
                    : (story.progress.current / Math.max(1, story.progress.total)) * 100)}%`,
                }}
              />
            </div>
          </div>
        )}
      </div>

      <div className="flex min-h-0 flex-1">
        {/* Chapter navigation */}
        {chapters.length > 0 && (
          <nav className="w-52 shrink-0 overflow-y-auto border-r border-border p-2">
            {chapters.map((c, i) => (
              <button
                key={c.index}
                onClick={() => setPinned(i)}
                className={`mb-0.5 block w-full rounded-md px-2 py-1.5 text-left ${
                  i === selected ? 'bg-bg-active' : 'hover:bg-bg-hover'
                }`}
              >
                <div className="truncate text-[11px] text-text-primary">
                  {c.index + 1}. {c.title || 'Untitled'}
                </div>
                <div className="text-[9px] text-text-muted">
                  {c.status === 'writing' ? 'writing…'
                    : c.text ? `${(c.word_count ?? 0).toLocaleString()} words${c.edited ? ' · edited' : ''}`
                    : c.status}
                </div>
              </button>
            ))}
          </nav>
        )}

        {/* Reading pane */}
        <div className="min-w-0 flex-1 overflow-y-auto">
          {/* Live pass */}
          {running && (body || thinking) && (
            <div className="border-b border-border bg-bg-tertiary/30 px-6 py-4">
              {thinking && (
                <div className="mb-2">
                  <button
                    onClick={() => setShowThinking(v => !v)}
                    className="flex items-center gap-1 text-[10px] text-text-muted hover:text-text-secondary"
                  >
                    {showThinking ? <ChevronUp size={10} /> : <ChevronDown size={10} />}
                    Thinking
                  </button>
                  {showThinking && (
                    <pre className="mt-1 whitespace-pre-wrap text-[11px] leading-relaxed text-text-muted">
                      {thinking}
                    </pre>
                  )}
                </div>
              )}
              <div ref={streamRef} className="max-h-64 overflow-y-auto">
                <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-text-secondary">
                  {body}
                </p>
              </div>
            </div>
          )}
          {running && !body && !thinking && (
            <div className="flex items-center gap-2 border-b border-border px-6 py-4 text-xs text-text-secondary">
              <Loader2 size={13} className="animate-spin" />
              Loading the model — the first use downloads it, which can take several minutes.
            </div>
          )}

          {chapter?.text ? (
            <article className="mx-auto max-w-[68ch] px-6 py-6">
              <div className="mb-3 flex items-start justify-between gap-3">
                <h2 className="text-base font-semibold text-text-primary">
                  {chapter.index + 1}. {chapter.title || 'Untitled'}
                </h2>
                <div className="flex shrink-0 items-center gap-1">
                  <IconButton
                    label="Copy chapter"
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(chapter.text)
                        setCopied(true); setTimeout(() => setCopied(false), 1500)
                      } catch { /* clipboard blocked */ }
                    }}
                  >
                    {copied ? <Check size={13} className="text-indicator-success" /> : <Copy size={13} />}
                  </IconButton>
                  <IconButton
                    label="Edit chapter"
                    disabled={running}
                    onClick={() => setEditing(chapter.text)}
                  >
                    <Pencil size={13} />
                  </IconButton>
                  <IconButton
                    label="Regenerate chapter"
                    disabled={running}
                    onClick={() => { setRegenFor(chapter.index); setInstruction('') }}
                  >
                    <RefreshCw size={13} />
                  </IconButton>
                </div>
              </div>

              {editing !== null ? (
                <div>
                  <textarea
                    value={editing}
                    onChange={e => setEditing(e.target.value)}
                    rows={24}
                    className="w-full resize-y rounded-lg border border-border bg-bg-tertiary px-3 py-2 text-[14px] leading-relaxed text-text-primary"
                  />
                  <div className="mt-2 flex items-center gap-2">
                    <button
                      onClick={async () => {
                        await saveChapterText(chapter.index, editing)
                        setEditing(null)
                      }}
                      className="rounded-lg bg-cta px-3 py-1.5 text-xs font-medium text-white"
                    >
                      Save
                    </button>
                    <button
                      onClick={() => setEditing(null)}
                      className="rounded-lg border border-border px-3 py-1.5 text-xs text-text-secondary hover:text-text-primary"
                    >
                      Cancel
                    </button>
                    <span className="text-[10px] text-text-muted">
                      Saving marks the synopsis stale — the next pass rebuilds it from your text.
                    </span>
                  </div>
                </div>
              ) : (
                <div className="whitespace-pre-wrap text-[15px] leading-[1.75] text-text-primary">
                  {chapter.text}
                </div>
              )}
            </article>
          ) : !running && (
            <div className="flex h-full items-center justify-center p-8">
              <p className="max-w-sm text-center text-xs text-text-secondary">
                {chapters.length === 0
                  ? 'The outline is being planned. Chapters appear here as they are written.'
                  : 'This chapter has not been written yet.'}
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Regenerate dialog */}
      <Dialog open={regenFor !== null} onClose={() => setRegenFor(null)} title="Regenerate chapter">
        <p className="text-xs text-text-secondary">
          The chapter is rewritten from the outline and the story so far.
          Continuity for later chapters is replayed afterwards.
        </p>
        <input
          value={instruction}
          onChange={e => setInstruction(e.target.value)}
          placeholder="Optional steer: darker, more dialogue, cut the flashback…"
          className="mt-3 w-full rounded-lg border border-border bg-bg-tertiary px-2.5 py-1.5 text-xs text-text-primary placeholder:text-text-muted"
        />
        <div className="mt-3 flex justify-end gap-2">
          <button
            onClick={() => setRegenFor(null)}
            className="rounded-lg border border-border px-3 py-1.5 text-xs text-text-secondary hover:text-text-primary"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              if (regenFor !== null) regenerateChapter(regenFor, instruction || undefined)
              setRegenFor(null)
            }}
            className="rounded-lg bg-cta px-3 py-1.5 text-xs font-medium text-white"
          >
            Regenerate
          </button>
        </div>
      </Dialog>

      {/* Prompt / settings dialog */}
      <Dialog open={showPrompt} onClose={() => setShowPrompt(false)} title="Story settings and prompts" wide>
        <div className="space-y-3 text-xs">
          <div>
            <div className="mb-1 font-medium text-text-primary">Premise</div>
            <p className="whitespace-pre-wrap text-text-secondary">{story.premise}</p>
          </div>
          <div>
            <div className="mb-1 font-medium text-text-primary">Settings</div>
            <pre className="overflow-x-auto rounded-lg border border-border bg-bg-tertiary p-2 text-[11px] text-text-secondary">
              {JSON.stringify(story.params ?? {}, null, 2)}
            </pre>
          </div>
          <button
            onClick={() => {
              setStoryDraft({ ...(story.params ?? {}), premise: story.premise })
              setShowPrompt(false)
            }}
            className="rounded-lg border border-border px-3 py-1.5 text-[11px] text-text-secondary hover:text-text-primary hover:border-border-light"
          >
            Reuse these settings for a new story
          </button>
          {(story.llm_passes ?? []).length > 0 && (
            <div>
              <div className="mb-1 font-medium text-text-primary">
                Passes ({(story.llm_passes ?? []).length})
              </div>
              <div className="space-y-1.5">
                {(story.llm_passes ?? []).map((p, i) => (
                  <details key={i} className="rounded-lg border border-border bg-bg-tertiary/50 p-2">
                    <summary className="cursor-pointer text-[11px] text-text-primary">
                      {p.pass} · {p.model_id}
                    </summary>
                    <pre className="mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap text-[10px] text-text-muted">
                      {p.system_prompt}
                      {'\n\n--- user ---\n'}
                      {p.user_prompt}
                    </pre>
                  </details>
                ))}
              </div>
            </div>
          )}
        </div>
      </Dialog>
    </div>
  )
}

function IconButton({ children, label, onClick, disabled }: {
  children: React.ReactNode
  label: string
  onClick: () => void
  disabled?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={label}
      aria-label={label}
      className="rounded-md p-1.5 text-text-secondary transition-colors hover:bg-bg-hover hover:text-text-primary disabled:opacity-30"
    >
      {children}
    </button>
  )
}

function Dialog({ open, onClose, title, children, wide }: {
  open: boolean
  onClose: () => void
  title: string
  children: React.ReactNode
  wide?: boolean
}) {
  return (
    <div className={`fixed inset-0 z-50 flex items-center justify-center p-4 transition-opacity duration-300 ${
      open ? 'opacity-100' : 'pointer-events-none opacity-0'
    }`}>
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      <div className={`glass-panel relative flex max-h-[85vh] w-full flex-col rounded-2xl shadow-2xl transition-all duration-300 ease-out ${
        wide ? 'md:w-[640px]' : 'md:w-[460px]'
      } ${open ? 'translate-y-0 scale-100' : 'translate-y-3 scale-95'}`}>
        <div className="flex shrink-0 items-center justify-between border-b border-border px-5 py-3">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="rounded-lg p-1.5 text-text-secondary transition-colors hover:bg-bg-hover hover:text-text-primary"
          >
            <X size={16} />
          </button>
        </div>
        <div className="min-h-0 overflow-y-auto px-5 py-4">{children}</div>
      </div>
    </div>
  )
}
