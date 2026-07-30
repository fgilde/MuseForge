import { useEffect, useState } from 'react'
import { BookText, Plus, Trash2, Square, ChevronDown, ChevronRight } from 'lucide-react'
import { useStore } from '../../stores/useStore'
import { TextSubModeToggle } from './TextSubModeToggle'
import { WORDS_PER_PAGE, estimateStory } from '../../lib/storyEstimate'

const GENRE_CHIPS = ['Science fiction', 'Fantasy', 'Thriller', 'Romance', 'Horror', 'Literary', 'Mystery', 'Historical']
const TONE_CHIPS = ['Dark', 'Hopeful', 'Satirical', 'Melancholic', 'Adventurous', 'Cosy', 'Bleak']

const ACTIVE = new Set(['queued', 'planning', 'writing'])

/**
 * Sidebar for Text → Story: the story list plus the form that starts a new
 * one. The reading and editing surface lives in the main area (StoryView),
 * mirroring how the chat sub-mode splits its controls from its transcript.
 */
export function StoryPanel() {
  const stories = useStore(s => s.stories)
  const activeStoryId = useStore(s => s.activeStoryId)
  const activeStory = useStore(s => s.activeStory)
  const models = useStore(s => s.storyModels)
  const draft = useStore(s => s.storyDraft)
  const error = useStore(s => s.storyError)
  const nsfwMode = useStore(s => s.servicesConfig?.nsfw_mode ?? false)
  const setStoryDraft = useStore(s => s.setStoryDraft)
  const loadStories = useStore(s => s.loadStories)
  const loadStoryModels = useStore(s => s.loadStoryModels)
  const selectStory = useStore(s => s.selectStory)
  const startStory = useStore(s => s.startStory)
  const stopActiveStory = useStore(s => s.stopActiveStory)
  const deleteStoryById = useStore(s => s.deleteStoryById)

  const [showForm, setShowForm] = useState(true)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    loadStories()
    loadStoryModels()
  }, [loadStories, loadStoryModels])

  const running = !!activeStory && ACTIVE.has(activeStory.status)
  const estimate = estimateStory(draft.min_pages ?? 60, draft.chapter_count ?? null)

  const submit = async () => {
    setBusy(true)
    try {
      const id = await startStory()
      if (id) setShowForm(false)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <TextSubModeToggle />

      {error && (
        <div className="rounded-lg border-l-4 border-l-red-500/60 border border-border bg-bg-tertiary/60 px-3 py-2 text-[11px] text-text-secondary">
          {error}
        </div>
      )}

      {/* Story list */}
      <div>
        <div className="flex items-center justify-between mb-1.5">
          <label className="text-[11px] text-text-muted uppercase tracking-wider">Stories</label>
          <button
            onClick={() => { setShowForm(true); selectStory(null) }}
            className="text-[10px] text-accent-blue hover:text-accent-blue-hover flex items-center gap-0.5"
            aria-label="New story"
          >
            <Plus size={10} /> New
          </button>
        </div>
        {stories.length === 0 ? (
          <p className="text-[11px] text-text-muted">No stories yet.</p>
        ) : (
          <div className="space-y-0.5 max-h-44 overflow-y-auto">
            {stories.map(s => (
              <div
                key={s.id}
                className={`group flex items-center gap-1.5 rounded-md px-2 py-1 cursor-pointer ${
                  s.id === activeStoryId ? 'bg-bg-active' : 'hover:bg-bg-hover'
                }`}
                onClick={() => { selectStory(s.id); setShowForm(false) }}
              >
                <BookText size={11} className="shrink-0 text-text-muted" />
                <div className="flex-1 min-w-0">
                  <div className="truncate text-[11px] text-text-primary">{s.title || 'Untitled'}</div>
                  <div className="text-[9px] text-text-muted">
                    {s.chapter_count ?? 0} ch · {(s.word_count ?? 0).toLocaleString()} words
                    {ACTIVE.has(s.status) && ' · writing…'}
                    {s.status === 'failed' && ' · failed'}
                    {s.status === 'cancelled' && ' · stopped'}
                  </div>
                </div>
                <button
                  onClick={e => {
                    e.stopPropagation()
                    if (confirmDelete === s.id) { deleteStoryById(s.id); setConfirmDelete(null) }
                    else { setConfirmDelete(s.id); setTimeout(() => setConfirmDelete(null), 3000) }
                  }}
                  className={`p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity ${
                    confirmDelete === s.id ? 'opacity-100 text-red-400' : 'text-text-muted hover:text-text-primary'
                  }`}
                  title={confirmDelete === s.id ? 'Click again to delete' : 'Delete story'}
                  aria-label="Delete story"
                >
                  <Trash2 size={11} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {running && (
        <button
          onClick={stopActiveStory}
          className="flex items-center justify-center gap-1.5 rounded-lg border border-border py-2 text-xs text-text-secondary hover:text-text-primary hover:border-border-light transition-colors"
        >
          <Square size={12} /> Stop writing
        </button>
      )}

      {/* New-story form */}
      <div>
        <button
          onClick={() => setShowForm(v => !v)}
          className="flex items-center gap-1 w-full text-left mb-2"
        >
          {showForm ? <ChevronDown size={11} className="text-text-muted" /> : <ChevronRight size={11} className="text-text-muted" />}
          <span className="text-[11px] text-text-muted uppercase tracking-wider">New story</span>
        </button>

        {showForm && (
          <div className="space-y-3">
            <div>
              <label className="text-[10px] text-text-muted">Premise</label>
              <textarea
                value={draft.premise ?? ''}
                onChange={e => setStoryDraft({ premise: e.target.value })}
                rows={4}
                placeholder="What is the story about? A sentence or a page — the more you give, the closer the outline lands."
                className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2.5 py-2 text-xs text-text-primary placeholder:text-text-muted resize-y"
              />
            </div>

            <div>
              <label className="text-[10px] text-text-muted">Title (optional)</label>
              <input
                value={draft.title ?? ''}
                onChange={e => setStoryDraft({ title: e.target.value })}
                placeholder="The model invents one if you leave this empty"
                className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2.5 py-1.5 text-xs text-text-primary placeholder:text-text-muted"
              />
            </div>

            {/* Length */}
            <div>
              <div className="flex items-center justify-between">
                <label className="text-[10px] text-text-muted">Minimum length</label>
                <div className="flex items-center gap-1">
                  <input
                    type="number"
                    min={3}
                    max={2000}
                    value={draft.min_pages ?? 60}
                    onChange={e => setStoryDraft({ min_pages: Math.max(3, Number(e.target.value) || 3) })}
                    className="w-16 rounded border border-border bg-bg-tertiary px-1.5 py-0.5 text-[11px] text-text-primary text-right"
                    aria-label="Minimum pages"
                  />
                  <span className="text-[10px] text-text-muted">pages</span>
                </div>
              </div>
              <input
                type="range"
                min={3}
                max={600}
                step={1}
                value={Math.min(600, draft.min_pages ?? 60)}
                onChange={e => setStoryDraft({ min_pages: Number(e.target.value) })}
                className="mt-1.5 w-full"
                aria-label="Minimum length in pages"
              />
              <p className="text-[10px] text-text-muted mt-1">
                ≈ {estimate.totalWords.toLocaleString()} words · {estimate.chapters} chapters ·
                ~{estimate.wordsPerChapter.toLocaleString()} words each
                <span className="text-text-muted/70"> ({WORDS_PER_PAGE} words/page)</span>
              </p>
            </div>

            <div>
              <div className="flex items-center justify-between">
                <label className="text-[10px] text-text-muted">Chapters</label>
                <label className="flex items-center gap-1 text-[10px] text-text-muted cursor-pointer">
                  <input
                    type="checkbox"
                    checked={draft.chapter_count == null}
                    onChange={e => setStoryDraft({ chapter_count: e.target.checked ? null : estimate.chapters })}
                    className="w-3 h-3 rounded border-border bg-bg-tertiary accent-accent-blue"
                  />
                  let the model decide
                </label>
              </div>
              {draft.chapter_count != null && (
                <input
                  type="number"
                  min={1}
                  max={300}
                  value={draft.chapter_count}
                  onChange={e => setStoryDraft({ chapter_count: Math.max(1, Number(e.target.value) || 1) })}
                  className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2.5 py-1.5 text-xs text-text-primary"
                />
              )}
            </div>

            {/* Genre / tone with suggestion chips */}
            <ChipField
              label="Genre"
              value={draft.genre ?? ''}
              chips={GENRE_CHIPS}
              onChange={v => setStoryDraft({ genre: v })}
            />
            <ChipField
              label="Tone"
              value={draft.tone ?? ''}
              chips={TONE_CHIPS}
              onChange={v => setStoryDraft({ tone: v })}
            />

            <div className="grid grid-cols-2 gap-2">
              <Select
                label="Point of view"
                value={draft.pov ?? 'third_limited'}
                onChange={v => setStoryDraft({ pov: v as typeof draft.pov })}
                options={[
                  ['first', 'First person'],
                  ['third_limited', 'Third, limited'],
                  ['third_omniscient', 'Third, omniscient'],
                ]}
              />
              <Select
                label="Tense"
                value={draft.tense ?? 'past'}
                onChange={v => setStoryDraft({ tense: v as typeof draft.tense })}
                options={[['past', 'Past'], ['present', 'Present']]}
              />
            </div>

            <div>
              <label className="text-[10px] text-text-muted">Audience (optional)</label>
              <input
                value={draft.audience ?? ''}
                onChange={e => setStoryDraft({ audience: e.target.value })}
                placeholder="e.g. adult, young adult"
                className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2.5 py-1.5 text-xs text-text-primary placeholder:text-text-muted"
              />
            </div>

            {/* Mature control only exists when the master switch is on —
                hidden rather than disabled, matching how model visibility
                behaves elsewhere. */}
            {nsfwMode && (
              <Select
                label="Explicitness"
                value={draft.explicitness ?? 'none'}
                onChange={v => setStoryDraft({ explicitness: v as typeof draft.explicitness })}
                options={[
                  ['none', 'None — fade to black'],
                  ['moderate', 'Moderate — sensual, not graphic'],
                  ['explicit', 'Explicit — direct language'],
                ]}
              />
            )}

            <Select
              label="Outline model"
              value={draft.outline_model ?? ''}
              onChange={v => setStoryDraft({ outline_model: v || undefined })}
              options={[
                ['', 'Auto (first downloaded)'],
                ...(models?.outline ?? []).map(m => [m.id, m.label] as [string, string]),
              ]}
            />
            <Select
              label="Prose model"
              value={draft.prose_model ?? ''}
              onChange={v => setStoryDraft({ prose_model: v || undefined })}
              options={[
                ['', 'Auto (first downloaded)'],
                ...(models?.prose ?? []).map(m => [m.id, m.label] as [string, string]),
              ]}
            />

            <div>
              <div className="flex items-center justify-between">
                <label className="text-[10px] text-text-muted">Prose temperature</label>
                <span className="text-[10px] text-text-primary tabular-nums">
                  {(draft.temperature ?? 0.9).toFixed(2)}
                </span>
              </div>
              <input
                type="range"
                min={0.3}
                max={1.3}
                step={0.05}
                value={draft.temperature ?? 0.9}
                onChange={e => setStoryDraft({ temperature: Number(e.target.value) })}
                className="mt-1.5 w-full"
                aria-label="Prose temperature"
              />
              <p className="text-[10px] text-text-muted mt-0.5">
                Higher is more inventive, lower more controlled. The outline pass
                always runs cool regardless.
              </p>
            </div>

            <button
              onClick={submit}
              disabled={busy || running || !(draft.premise ?? '').trim()}
              className="w-full rounded-lg bg-cta py-2 text-xs font-medium text-white shadow-accent-glow disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {busy ? 'Starting…' : running ? 'A story is being written…' : 'Forge story'}
            </button>
            {running && (
              <p className="text-[10px] text-text-muted text-center">
                One story at a time — the LLM is shared.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function Select({ label, value, onChange, options }: {
  label: string
  value: string
  onChange: (v: string) => void
  options: [string, string][]
}) {
  return (
    <div>
      <label className="text-[10px] text-text-muted">{label}</label>
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary"
      >
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </div>
  )
}

function ChipField({ label, value, chips, onChange }: {
  label: string
  value: string
  chips: string[]
  onChange: (v: string) => void
}) {
  return (
    <div>
      <label className="text-[10px] text-text-muted">{label}</label>
      <input
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder="Type anything, or pick below"
        className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2.5 py-1.5 text-xs text-text-primary placeholder:text-text-muted"
      />
      <div className="mt-1.5 flex flex-wrap gap-1">
        {chips.map(c => (
          <button
            key={c}
            onClick={() => onChange(value === c ? '' : c)}
            className={`rounded-full px-2 py-0.5 text-[10px] border transition-colors ${
              value === c
                ? 'border-accent-blue text-accent-blue'
                : 'border-border text-text-secondary hover:text-text-primary hover:border-border-light'
            }`}
          >
            {c}
          </button>
        ))}
      </div>
    </div>
  )
}
