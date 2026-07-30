import { useEffect, useMemo, useRef, useState } from 'react'
import {
  BookText, Copy, Check, Pencil, RefreshCw, Plus, Download, FileText,
  ChevronDown, ChevronUp, X, Loader2, Languages, Wand2, Trash2, Search,
  AlertTriangle,
} from 'lucide-react'
import { useStore } from '../../stores/useStore'
import { WORDS_PER_PAGE } from '../../lib/storyEstimate'
import {
  storyChapterDownloadUrl, storyDownloadUrl,
  type StoryChapter, type StoryRewriteProposal,
} from '../../api/client'

const ACTIVE = new Set(['queued', 'planning', 'writing'])

/** Download formats, in the order the menu offers them. md/txt always work;
 *  docx/pdf need optional server packages (see /story/export-formats). */
const FORMATS: [string, string][] = [
  ['md', 'Markdown (.md)'],
  ['txt', 'Plain text (.txt)'],
  ['docx', 'Word (.docx)'],
  ['pdf', 'PDF (.pdf)'],
]

/** Chips for the rewrite dialog — the phrasing the model actually gets. */
const REWRITE_CHIPS: [string, string][] = [
  ['longer', 'Expand this passage — more detail, more room to breathe.'],
  ['shorter', 'Tighten this passage — same content, fewer words.'],
  ['more dialogue', 'Rewrite this passage with more dialogue and less narration.'],
  ['more tension', 'Raise the tension in this passage.'],
  ['simpler language', 'Rewrite this passage in simpler, plainer language.'],
]

const SEVERITY_ORDER = ['high', 'medium', 'low']

/** Split a raw stream buffer into its thinking block and the visible text.
 *  Same shape the chat view uses — models emit <think>…</think> and we must
 *  not present that as prose. */
function splitThinking(raw: string): { thinking: string; body: string } {
  const m = raw.match(/^\s*<think(?:ing)?>([\s\S]*?)(?:<\/think(?:ing)?>|$)/)
  if (!m) return { thinking: '', body: raw }
  return { thinking: m[1], body: raw.slice(m[0].length) }
}

/** What to render for a chapter in a given language. Falls back to the
 *  original prose (flagged `missing`) rather than showing an empty page. */
function chapterView(chapter: StoryChapter | undefined, lang: string, original: string) {
  if (!chapter) return { title: '', text: '', missing: false, stale: false }
  if (lang === original) {
    return { title: chapter.title, text: chapter.text, missing: false, stale: false }
  }
  const t = chapter.translations?.[lang]
  if (!t || !(t.text || '').trim()) {
    return { title: chapter.title, text: chapter.text, missing: true, stale: false }
  }
  return { title: t.title || chapter.title, text: t.text, missing: false, stale: !!t.stale }
}

export function StoryView() {
  const story = useStore(s => s.activeStory)
  const streamText = useStore(s => s.storyStreamText)
  const error = useStore(s => s.storyError)
  const analyzing = useStore(s => s.storyAnalyzing)
  const allLanguages = useStore(s => s.storyLanguages)
  const exportFormats = useStore(s => s.storyExportFormats)
  const regenerateChapter = useStore(s => s.regenerateChapter)
  const saveChapterText = useStore(s => s.saveChapterText)
  const extendActiveStory = useStore(s => s.extendActiveStory)
  const setStoryDraft = useStore(s => s.setStoryDraft)
  const loadStoryLanguages = useStore(s => s.loadStoryLanguages)
  const loadStoryExportFormats = useStore(s => s.loadStoryExportFormats)
  const translateActiveStory = useStore(s => s.translateActiveStory)
  const retranslateChapter = useStore(s => s.retranslateChapter)
  const insertChapter = useStore(s => s.insertChapter)
  const deleteChapterAt = useStore(s => s.deleteChapterAt)
  const analyzeActiveStory = useStore(s => s.analyzeActiveStory)
  const rewriteChapterPassage = useStore(s => s.rewriteChapterPassage)
  const applyChapterRewrite = useStore(s => s.applyChapterRewrite)

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
  /** Language the user picked, or null to follow the original. */
  const [langPick, setLangPick] = useState<string | null>(null)
  const [translateTo, setTranslateTo] = useState<string | null>(null)
  const [downloadOpen, setDownloadOpen] = useState(false)
  const [fmt, setFmt] = useState('md')
  const [perChapter, setPerChapter] = useState(false)
  const [showAnalysis, setShowAnalysis] = useState(false)
  const [insertAfter, setInsertAfter] = useState<boolean | null>(null)
  const [insertWrite, setInsertWrite] = useState(false)
  const [insertBrief, setInsertBrief] = useState('')
  const [confirmDeleteCh, setConfirmDeleteCh] = useState<number | null>(null)
  const [rewrite, setRewrite] = useState<{ selection: string } | null>(null)
  const [rewriteInstruction, setRewriteInstruction] = useState('')
  const [rewriteBusy, setRewriteBusy] = useState(false)
  const [proposal, setProposal] = useState<StoryRewriteProposal | null>(null)
  const [selectionWarning, setSelectionWarning] = useState('')
  const streamRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    loadStoryLanguages()
    loadStoryExportFormats()
  }, [loadStoryLanguages, loadStoryExportFormats])

  const running = !!story && ACTIVE.has(story.status)
  const chapters = useMemo(() => story?.chapters ?? [], [story?.chapters])
  const lastWritten = useMemo(() => {
    for (let i = chapters.length - 1; i >= 0; i--) if (chapters[i].text) return i
    return 0
  }, [chapters])
  // Derived, not stored: the newest written chapter unless the user pinned one.
  const selected = Math.min(pinned ?? lastWritten, Math.max(0, chapters.length - 1))
  const anyWritten = chapters.some(c => c.text)

  // Language, all derived — a picked language that the story no longer has
  // (translation deleted, other story opened) simply falls back.
  const original = story?.params?.language || story?.languages?.[0] || 'en'
  const languages = story?.languages?.length ? story.languages : [original]
  const lang = langPick && languages.includes(langPick) ? langPick : original
  const isTranslation = lang !== original
  /** Passed to every per-language endpoint: omitted means "the original". */
  const langArg = isTranslation ? lang : undefined

  useEffect(() => {
    if (streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight
  }, [streamText])

  const totalWords = chapters.reduce((n, c) => n + (c.word_count ?? 0), 0)
  const chapter = chapters[selected]
  const view = chapterView(chapter, lang, original)
  const { thinking, body } = splitThinking(streamText)
  const analysis = story?.analysis ?? null
  const formatAvailable = (f: string) => exportFormats ? !!exportFormats[f] : (f === 'md' || f === 'txt')

  const switchLanguage = (code: string) => {
    setLangPick(code)
    setEditing(null)
  }

  const openRewrite = () => {
    const selection = (window.getSelection?.()?.toString() || '').trim()
    if (!selection) {
      setSelectionWarning('Select the passage in the chapter text first, then press Rewrite selection.')
      setTimeout(() => setSelectionWarning(''), 5000)
      return
    }
    setSelectionWarning('')
    setProposal(null)
    setRewriteInstruction('')
    setRewrite({ selection })
  }

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

  const sid = story.story_id

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
              onClick={() => {
                setShowAnalysis(true)
                if (!analysis && !analyzing) analyzeActiveStory(langArg)
              }}
              disabled={!anyWritten || running}
              className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:text-text-primary hover:border-border-light transition-colors disabled:opacity-40"
              title="Characters, dialogue, timeline and continuity issues"
            >
              {analyzing
                ? <Loader2 size={12} className="mr-1 inline animate-spin" />
                : <Search size={12} className="mr-1 inline" />}
              Analyze
            </button>

            {/* Download menu — replaces the old workspace-only export. */}
            <div className="relative">
              <button
                onClick={() => setDownloadOpen(v => !v)}
                disabled={!anyWritten}
                className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:text-text-primary hover:border-border-light transition-colors disabled:opacity-40"
                title="Download the story as a file"
              >
                <Download size={12} className="mr-1 inline" /> Download
              </button>
              {downloadOpen && (
                <>
                  <div className="fixed inset-0 z-40" onClick={() => setDownloadOpen(false)} />
                  <div className="glass-panel absolute right-0 top-full z-50 mt-1 w-64 rounded-xl p-2 shadow-2xl">
                    <div className="mb-1.5 flex gap-1">
                      <SegButton active={!perChapter} onClick={() => setPerChapter(false)}>
                        One file
                      </SegButton>
                      <SegButton active={perChapter} onClick={() => setPerChapter(true)}>
                        Per chapter (ZIP)
                      </SegButton>
                    </div>
                    {FORMATS.map(([f, label]) => (
                      formatAvailable(f) ? (
                        <a
                          key={f}
                          href={storyDownloadUrl(sid, { fmt: f, lang: langArg, perChapter })}
                          onClick={() => { setFmt(f); setDownloadOpen(false) }}
                          className="block rounded-md px-2 py-1.5 text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
                        >
                          {label}
                        </a>
                      ) : (
                        <span
                          key={f}
                          title="Not available — this install is missing the optional package for it"
                          className="block cursor-not-allowed rounded-md px-2 py-1.5 text-[11px] text-text-muted opacity-50"
                        >
                          {label}
                        </span>
                      )
                    ))}
                    <p className="mt-1 px-2 text-[9px] text-text-muted">
                      {isTranslation ? `${lang.toUpperCase()} translation` : 'Original language'}
                      {' · chapters without a translation fall back to the original'}
                    </p>
                  </div>
                </>
              )}
            </div>

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

        {/* Language toggle — which text is read AND edited. */}
        <div className="mt-2 flex items-center justify-end gap-1.5">
          <Languages size={12} className="text-text-muted" />
          <div className="flex items-center gap-1">
            {languages.map(code => (
              <SegButton key={code} active={code === lang} onClick={() => switchLanguage(code)}>
                {code.toUpperCase()}
                {code === original && <span className="ml-1 text-text-muted">orig</span>}
              </SegButton>
            ))}
          </div>
          <button
            onClick={() => {
              const first = allLanguages.find(l => l.code !== original)
              setTranslateTo(first?.code ?? original)
            }}
            disabled={running || !anyWritten}
            className="rounded-lg border border-border px-2 py-0.5 text-[10px] text-text-secondary hover:text-text-primary hover:border-border-light transition-colors disabled:opacity-40"
            title="Translate the whole story into another language"
          >
            + Translate
          </button>
        </div>

        {error && (
          <p className="mt-2 text-[11px] text-red-400">{error}</p>
        )}
        {story.error && (
          <p className="mt-2 text-[11px] text-red-400">{story.error}</p>
        )}
        {selectionWarning && (
          <p className="mt-2 text-[11px] text-indicator-warning">{selectionWarning}</p>
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
                onClick={() => { setPinned(i); setEditing(null) }}
                className={`mb-0.5 block w-full rounded-md px-2 py-1.5 text-left ${
                  i === selected ? 'bg-bg-active' : 'hover:bg-bg-hover'
                }`}
              >
                <div className="truncate text-[11px] text-text-primary">
                  {c.index + 1}. {chapterView(c, lang, original).title || 'Untitled'}
                </div>
                <div className="text-[9px] text-text-muted">
                  {c.status === 'writing' ? 'writing…'
                    : c.text ? `${(c.word_count ?? 0).toLocaleString()} words${c.edited ? ' · edited' : ''}`
                    : c.status}
                  {isTranslation && (
                    c.translations?.[lang]?.text
                      ? c.translations[lang].stale ? ' · translation stale' : ` · ${lang}`
                      : ' · not translated'
                  )}
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

          {view.text ? (
            <article className="mx-auto max-w-[68ch] px-6 py-6">
              <div className="mb-3 flex items-start justify-between gap-3">
                <h2 className="text-base font-semibold text-text-primary">
                  {chapter.index + 1}. {view.title || 'Untitled'}
                </h2>
                <div className="flex shrink-0 items-center gap-1">
                  <IconButton
                    label="Copy chapter"
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(view.text)
                        setCopied(true); setTimeout(() => setCopied(false), 1500)
                      } catch { /* clipboard blocked */ }
                    }}
                  >
                    {copied ? <Check size={13} className="text-indicator-success" /> : <Copy size={13} />}
                  </IconButton>
                  <a
                    href={storyChapterDownloadUrl(sid, chapter.index, { fmt, lang: langArg })}
                    title={`Download this chapter (.${fmt})`}
                    aria-label="Download this chapter"
                    className="rounded-md p-1.5 text-text-secondary transition-colors hover:bg-bg-hover hover:text-text-primary"
                  >
                    <Download size={13} />
                  </a>
                  <IconButton
                    label={isTranslation ? `Edit the ${lang.toUpperCase()} text` : 'Edit chapter'}
                    disabled={running}
                    onClick={() => setEditing(view.text)}
                  >
                    <Pencil size={13} />
                  </IconButton>
                  <IconButton
                    label="Rewrite selection"
                    disabled={running}
                    onClick={openRewrite}
                  >
                    <Wand2 size={13} />
                  </IconButton>
                  <IconButton
                    label="Regenerate chapter"
                    disabled={running}
                    onClick={() => { setRegenFor(chapter.index); setInstruction('') }}
                  >
                    <RefreshCw size={13} />
                  </IconButton>
                  <IconButton
                    label="Insert a chapter here"
                    disabled={running}
                    onClick={() => {
                      setInsertAfter(true); setInsertWrite(false); setInsertBrief('')
                    }}
                  >
                    <Plus size={13} />
                  </IconButton>
                  <IconButton
                    label={confirmDeleteCh === chapter.index ? 'Click again to delete' : 'Delete chapter'}
                    disabled={running}
                    onClick={() => {
                      if (confirmDeleteCh === chapter.index) {
                        deleteChapterAt(chapter.index)
                        setConfirmDeleteCh(null)
                        setPinned(null)
                      } else {
                        setConfirmDeleteCh(chapter.index)
                        setTimeout(() => setConfirmDeleteCh(null), 3000)
                      }
                    }}
                  >
                    <Trash2 size={13} className={confirmDeleteCh === chapter.index ? 'text-red-400' : ''} />
                  </IconButton>
                </div>
              </div>

              {/* Translation state for this chapter */}
              {isTranslation && (view.missing || view.stale) && (
                <div className="mb-3 flex items-start gap-2 rounded-lg border border-border border-l-4 border-l-indicator-warning/60 bg-bg-tertiary/60 px-3 py-2">
                  <AlertTriangle size={13} className="mt-0.5 shrink-0 text-indicator-warning" />
                  <div className="text-[11px] text-text-secondary">
                    {view.missing
                      ? `No ${lang.toUpperCase()} translation for this chapter yet — showing the original.`
                      : `This ${lang.toUpperCase()} translation is out of date: the original changed after it was made.`}
                    <button
                      onClick={() => retranslateChapter(chapter.index, lang)}
                      disabled={running}
                      className="ml-2 rounded border border-border px-1.5 py-0.5 text-[10px] text-text-secondary hover:text-text-primary hover:border-border-light disabled:opacity-40"
                    >
                      Translate this chapter
                    </button>
                  </div>
                </div>
              )}

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
                        await saveChapterText(chapter.index, editing, langArg)
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
                      {isTranslation
                        ? 'You are editing the translation — the original is untouched.'
                        : 'Saving marks the synopsis stale — the next pass rebuilds it from your text.'}
                    </span>
                  </div>
                </div>
              ) : (
                <div className="whitespace-pre-wrap text-[15px] leading-[1.75] text-text-primary">
                  {view.text}
                </div>
              )}
            </article>
          ) : !running && (
            <div className="flex h-full flex-col items-center justify-center gap-3 p-8">
              <p className="max-w-sm text-center text-xs text-text-secondary">
                {chapters.length === 0
                  ? 'The outline is being planned. Chapters appear here as they are written.'
                  : 'This chapter has not been written yet.'}
              </p>
              {chapters.length > 0 && (
                <button
                  onClick={() => { setInsertAfter(true); setInsertWrite(false); setInsertBrief('') }}
                  className="rounded-lg border border-border px-3 py-1.5 text-[11px] text-text-secondary hover:text-text-primary hover:border-border-light"
                >
                  <Plus size={12} className="mr-1 inline" /> Insert a chapter
                </button>
              )}
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

      {/* Translate dialog */}
      <Dialog open={translateTo !== null} onClose={() => setTranslateTo(null)} title="Translate the story">
        <p className="text-xs text-text-secondary">
          Every written chapter is translated one by one. The original stays
          untouched — a translation is an extra view you can switch to.
        </p>
        <select
          value={translateTo ?? ''}
          onChange={e => setTranslateTo(e.target.value)}
          className="mt-3 w-full rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary"
        >
          {allLanguages.filter(l => l.code !== original).map(l => (
            <option key={l.code} value={l.code}>
              {l.name}{languages.includes(l.code) ? ' — already translated' : ''}
            </option>
          ))}
        </select>
        <div className="mt-3 flex justify-end gap-2">
          <button
            onClick={() => setTranslateTo(null)}
            className="rounded-lg border border-border px-3 py-1.5 text-xs text-text-secondary hover:text-text-primary"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              if (translateTo) {
                translateActiveStory(translateTo)
                setLangPick(translateTo)
              }
              setTranslateTo(null)
            }}
            disabled={!translateTo}
            className="rounded-lg bg-cta px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
          >
            Translate
          </button>
        </div>
      </Dialog>

      {/* Insert chapter dialog */}
      <Dialog open={insertAfter !== null} onClose={() => setInsertAfter(null)} title="Insert a chapter">
        <div className="flex gap-1">
          <SegButton active={insertAfter === false} onClick={() => setInsertAfter(false)}>
            Before chapter {selected + 1}
          </SegButton>
          <SegButton active={insertAfter === true} onClick={() => setInsertAfter(true)}>
            After chapter {selected + 1}
          </SegButton>
        </div>
        <div className="mt-3 flex gap-1">
          <SegButton active={!insertWrite} onClick={() => setInsertWrite(false)}>
            Empty chapter
          </SegButton>
          <SegButton active={insertWrite} onClick={() => setInsertWrite(true)}>
            Let the AI write it
          </SegButton>
        </div>
        {insertWrite && (
          <textarea
            value={insertBrief}
            onChange={e => setInsertBrief(e.target.value)}
            rows={3}
            placeholder="Optional brief: what should happen in this chapter?"
            className="mt-3 w-full resize-y rounded-lg border border-border bg-bg-tertiary px-2.5 py-1.5 text-xs text-text-primary placeholder:text-text-muted"
          />
        )}
        <p className="mt-2 text-[10px] text-text-muted">
          {insertWrite
            ? 'The surrounding chapters are used as the seam so it fits where it lands.'
            : 'An empty chapter is inserted and the rest are renumbered.'}
        </p>
        <div className="mt-3 flex justify-end gap-2">
          <button
            onClick={() => setInsertAfter(null)}
            className="rounded-lg border border-border px-3 py-1.5 text-xs text-text-secondary hover:text-text-primary"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              insertChapter({
                at_index: insertAfter ? selected + 1 : selected,
                write: insertWrite || undefined,
                brief: insertWrite ? insertBrief || undefined : undefined,
              })
              setInsertAfter(null)
            }}
            className="rounded-lg bg-cta px-3 py-1.5 text-xs font-medium text-white"
          >
            Insert
          </button>
        </div>
      </Dialog>

      {/* Rewrite dialog */}
      <Dialog
        open={rewrite !== null}
        onClose={() => { setRewrite(null); setProposal(null) }}
        title="Rewrite selection"
        size="wide"
      >
        <div className="max-h-32 overflow-y-auto rounded-lg border border-border bg-bg-tertiary/60 px-2.5 py-2 text-[11px] leading-relaxed text-text-secondary">
          {rewrite?.selection}
        </div>

        {!proposal && (
          <>
            <input
              value={rewriteInstruction}
              onChange={e => setRewriteInstruction(e.target.value)}
              placeholder="How should this passage change?"
              className="mt-3 w-full rounded-lg border border-border bg-bg-tertiary px-2.5 py-1.5 text-xs text-text-primary placeholder:text-text-muted"
            />
            <div className="mt-1.5 flex flex-wrap gap-1">
              {REWRITE_CHIPS.map(([label, text]) => (
                <button
                  key={label}
                  onClick={() => setRewriteInstruction(text)}
                  className={`rounded-full border px-2 py-0.5 text-[10px] transition-colors ${
                    rewriteInstruction === text
                      ? 'border-accent-blue text-accent-blue'
                      : 'border-border text-text-secondary hover:border-border-light hover:text-text-primary'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => setRewrite(null)}
                className="rounded-lg border border-border px-3 py-1.5 text-xs text-text-secondary hover:text-text-primary"
              >
                Cancel
              </button>
              <button
                onClick={async () => {
                  if (!rewrite || !rewriteInstruction.trim()) return
                  setRewriteBusy(true)
                  const result = await rewriteChapterPassage(
                    chapter.index, rewrite.selection, rewriteInstruction, langArg,
                  )
                  setRewriteBusy(false)
                  if (result?.ok) setProposal(result)
                  else setRewrite(null)  // reason is in storyError, shown in the header
                }}
                disabled={rewriteBusy || !rewriteInstruction.trim()}
                className="rounded-lg bg-cta px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
              >
                {rewriteBusy ? 'Rewriting…' : 'Rewrite'}
              </button>
            </div>
          </>
        )}

        {proposal && (
          <>
            <div className="mt-3 rounded-lg border border-border bg-bg-tertiary/40 p-2.5 text-[12px] leading-relaxed">
              <span className="text-text-muted">…{proposal.before.slice(-240)}</span>
              <span className="whitespace-pre-wrap text-text-primary"> {proposal.replacement} </span>
              <span className="text-text-muted">{proposal.after.slice(0, 240)}…</span>
            </div>
            <div className="mt-3 flex justify-end gap-2">
              <button
                onClick={() => setProposal(null)}
                className="rounded-lg border border-border px-3 py-1.5 text-xs text-text-secondary hover:text-text-primary"
              >
                Discard
              </button>
              <button
                onClick={async () => {
                  if (!rewrite) return
                  const ok = await applyChapterRewrite(
                    chapter.index, rewrite.selection, proposal.replacement, langArg,
                  )
                  if (ok) { setRewrite(null); setProposal(null) }
                }}
                className="rounded-lg bg-cta px-3 py-1.5 text-xs font-medium text-white"
              >
                Apply
              </button>
            </div>
          </>
        )}
      </Dialog>

      {/* Analysis dialog */}
      <Dialog
        open={showAnalysis}
        onClose={() => setShowAnalysis(false)}
        title="Story analysis"
        size="xl"
      >
        {analyzing && (
          <div className="flex items-center gap-2 text-xs text-text-secondary">
            <Loader2 size={13} className="animate-spin" />
            Auditing chapter by chapter — this takes a while on a long story.
          </div>
        )}
        {!analyzing && !analysis && (
          <p className="text-xs text-text-secondary">No analysis yet.</p>
        )}
        {analysis && (
          <div className="space-y-5 text-xs">
            <div className="flex items-center justify-between gap-2">
              <p className="text-[10px] text-text-muted">
                {analysis.chapters_analyzed ?? analysis.timeline.length} chapters analysed
                {analysis.language ? ` · ${analysis.language.toUpperCase()}` : ''}
                {analysis.dropped_refs
                  ? ` · ${analysis.dropped_refs} reference(s) dropped as invalid`
                  : ''}
              </p>
              <button
                onClick={() => analyzeActiveStory(langArg)}
                disabled={analyzing || running}
                className="rounded-lg border border-border px-2 py-1 text-[10px] text-text-secondary hover:border-border-light hover:text-text-primary disabled:opacity-40"
              >
                Re-analyze
              </button>
            </div>

            {analysis.summary && (
              <Section title="Summary">
                <p className="whitespace-pre-wrap text-text-secondary">{analysis.summary}</p>
              </Section>
            )}

            <Section title={`Characters (${analysis.characters.length})`}>
              <div className="space-y-1.5">
                {analysis.characters.map(c => (
                  <div key={c.name} className="rounded-lg border border-border bg-bg-tertiary/40 px-2.5 py-2">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="font-medium text-text-primary">{c.name}</span>
                      <span className="shrink-0 text-[10px] text-text-muted">
                        {c.role && `${c.role} · `}
                        ch {c.first_chapter + 1}–{c.last_chapter + 1} ({c.chapters.length})
                      </span>
                    </div>
                    {c.description && <p className="mt-0.5 text-text-secondary">{c.description}</p>}
                    {c.traits.length > 0 && (
                      <p className="mt-0.5 text-[10px] text-text-muted">{c.traits.join(' · ')}</p>
                    )}
                  </div>
                ))}
              </div>
            </Section>

            <Section title={`Issues (${analysis.issues.length})`}>
              {SEVERITY_ORDER.map(sev => {
                const group = analysis.issues.filter(i => i.severity === sev)
                if (group.length === 0) return null
                return (
                  <div key={sev} className="mb-2">
                    <div className={`mb-1 text-[10px] uppercase tracking-wider ${
                      sev === 'high' ? 'text-red-400'
                        : sev === 'medium' ? 'text-indicator-warning' : 'text-text-muted'
                    }`}>
                      {sev} ({group.length})
                    </div>
                    <div className="space-y-1">
                      {group.map((issue, i) => (
                        <div key={i} className="rounded-lg border border-border bg-bg-tertiary/40 px-2.5 py-2">
                          <div className="flex items-baseline justify-between gap-2">
                            <span className="text-[10px] uppercase tracking-wider text-text-muted">
                              {issue.kind}
                            </span>
                            <button
                              onClick={() => { setPinned(issue.chapter); setShowAnalysis(false) }}
                              className="shrink-0 text-[10px] text-accent-blue hover:underline"
                            >
                              Chapter {issue.chapter + 1}
                            </button>
                          </div>
                          <p className="text-text-secondary">{issue.description}</p>
                          {issue.suggestion && (
                            <p className="mt-0.5 text-[11px] text-text-muted">→ {issue.suggestion}</p>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })}
              {analysis.issues.length === 0 && (
                <p className="text-text-secondary">Nothing flagged.</p>
              )}
            </Section>

            <Section title={`Dialogue map (${analysis.dialogue_map.length})`}>
              {analysis.truncated && (
                <p className="mb-1 text-[10px] text-indicator-warning">
                  Truncated — the map hit the server-side cap, later chapters are not listed.
                </p>
              )}
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-[11px]">
                  <thead>
                    <tr className="text-left text-text-muted">
                      <th className="border-b border-border py-1 pr-2 font-normal">Ch</th>
                      <th className="border-b border-border py-1 pr-2 font-normal">Speaker</th>
                      <th className="border-b border-border py-1 font-normal">Line</th>
                    </tr>
                  </thead>
                  <tbody>
                    {analysis.dialogue_map.map((d, i) => (
                      <tr key={i} className="align-top">
                        <td className="border-b border-border py-1 pr-2 tabular-nums text-text-muted">
                          {d.chapter + 1}
                        </td>
                        <td className="border-b border-border py-1 pr-2 text-text-primary">{d.speaker}</td>
                        <td className="border-b border-border py-1 text-text-secondary">
                          {d.line_excerpt}
                          {d.context && <span className="text-text-muted"> — {d.context}</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>

            <Section title="Timeline">
              <div className="space-y-1">
                {analysis.timeline.map(t => (
                  <div key={t.chapter} className="rounded-lg border border-border bg-bg-tertiary/40 px-2.5 py-2">
                    <div className="flex items-baseline justify-between gap-2">
                      <button
                        onClick={() => { setPinned(t.chapter); setShowAnalysis(false) }}
                        className="text-[10px] text-accent-blue hover:underline"
                      >
                        Chapter {t.chapter + 1}
                      </button>
                      <span className="shrink-0 text-[10px] text-text-muted">
                        {t.when} · {t.where}
                      </span>
                    </div>
                    {t.summary && <p className="mt-0.5 text-text-secondary">{t.summary}</p>}
                  </div>
                ))}
              </div>
            </Section>
          </div>
        )}
      </Dialog>

      {/* Prompt / settings dialog */}
      <Dialog open={showPrompt} onClose={() => setShowPrompt(false)} title="Story settings and prompts" size="wide">
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

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-text-primary">
        {title}
      </div>
      {children}
    </div>
  )
}

function SegButton({ active, onClick, children }: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-lg border px-2 py-0.5 text-[10px] transition-colors ${
        active
          ? 'border-accent-blue bg-bg-active text-text-primary'
          : 'border-border text-text-secondary hover:border-border-light hover:text-text-primary'
      }`}
    >
      {children}
    </button>
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

function Dialog({ open, onClose, title, children, size }: {
  open: boolean
  onClose: () => void
  title: string
  children: React.ReactNode
  size?: 'wide' | 'xl'
}) {
  return (
    <div className={`fixed inset-0 z-50 flex items-center justify-center p-4 transition-opacity duration-300 ${
      open ? 'opacity-100' : 'pointer-events-none opacity-0'
    }`}>
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      <div className={`glass-panel relative flex max-h-[85vh] w-full flex-col rounded-2xl shadow-2xl transition-all duration-300 ease-out ${
        size === 'xl' ? 'md:w-[900px]' : size === 'wide' ? 'md:w-[640px]' : 'md:w-[460px]'
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
