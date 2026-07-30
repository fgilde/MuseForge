import { useEffect, useRef, useState } from 'react'
import {
  BookAudio, Plus, Trash2, Upload, Loader2, Mic, ChevronDown, ChevronRight, Check,
  Play, Library, AlertTriangle,
} from 'lucide-react'
import { useStore } from '../../stores/useStore'
import type { AudiobookVoiceProfile, SfxLibraryEffect } from '../../api/client'

/** TTS architectures that can voice a run. Kept here rather than hardcoded
 *  in a dropdown so a new engine only needs one line. `clone` marks the
 *  models that accept a reference clip; `emotion` how they take direction. */
const TTS_MODELS: { type: string; label: string; clone: boolean; emotion: 'native' | 'partial' | 'instruction' | 'none' }[] = [
  { type: 'index_tts2', label: 'IndexTTS2 — cloning + emotion tags', clone: true, emotion: 'native' },
  { type: 'kugelaudio_0_open', label: 'KugelAudio 7B — cloning', clone: true, emotion: 'none' },
  { type: 'chatterbox', label: 'Chatterbox — multilingual', clone: true, emotion: 'partial' },
  { type: 'qwen3_tts_voicedesign', label: 'Qwen3 Voice Design — describe a voice', clone: false, emotion: 'instruction' },
  { type: 'qwen3_tts_customvoice', label: 'Qwen3 Custom Voice — speaker presets', clone: false, emotion: 'instruction' },
]

const SWATCHES = ['#22d3ee', '#a78bfa', '#f472b6', '#4ade80', '#fb923c', '#facc15', '#60a5fa', '#f87171']

/** A profile id only has to be unique inside its project, so derive it from
 *  the profiles that already exist. Deterministic on purpose — reading a
 *  clock or a random source inside a component is a purity violation. */
function nextProfileId(existing: AudiobookVoiceProfile[]): string {
  let n = existing.length + 1
  while (existing.some(v => v.id === `vp${n}`)) n++
  return `vp${n}`
}

export function AudiobookPanel() {
  const projects = useStore(s => s.audiobooks)
  const activeId = useStore(s => s.activeAudiobookId)
  const project = useStore(s => s.activeAudiobook)
  const activeChapterId = useStore(s => s.activeAbChapterId)
  const busy = useStore(s => s.abBusy)
  const error = useStore(s => s.abError)
  const loadAudiobooks = useStore(s => s.loadAudiobooks)
  const createAudiobook = useStore(s => s.createAudiobook)
  const selectAudiobook = useStore(s => s.selectAudiobook)
  const deleteAudiobook = useStore(s => s.deleteAudiobook)
  const patchAudiobook = useStore(s => s.patchAudiobook)
  const setAbChapter = useStore(s => s.setAbChapter)
  const setTextSubMode = useStore(s => s.setTextSubMode)
  const setGenerationMode = useStore(s => s.setGenerationMode)
  const setStoryDraft = useStore(s => s.setStoryDraft)
  const stories = useStore(s => s.stories)
  const loadStories = useStore(s => s.loadStories)
  const importStory = useStore(s => s.importStoryAsAudiobook)
  const createAsset = useStore(s => s.createAbAsset)
  const deleteAsset = useStore(s => s.deleteAbAsset)
  const sfxLibrary = useStore(s => s.abSfxLibrary)
  const loadSfxLibrary = useStore(s => s.loadAbSfxLibrary)
  const adoptSfx = useStore(s => s.adoptAbSfx)
  const presets = useStore(s => s.abVoicePresets)
  const loadPresets = useStore(s => s.loadAbVoicePresets)
  const libraryVoices = useStore(s => s.voices)
  const loadVoices = useStore(s => s.loadVoices)
  const importLibraryVoice = useStore(s => s.importLibraryVoice)
  const previewVoice = useStore(s => s.previewAbVoice)
  const previewBusy = useStore(s => s.voicePreviewBusy)
  const previewUrls = useStore(s => s.voicePreviewUrls)
  const previewWarnings = useStore(s => s.voicePreviewWarnings)

  const fileRef = useRef<HTMLInputElement>(null)
  const importFile = useStore(s => s.importAudiobookFile)
  const [autoSplit, setAutoSplit] = useState(true)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [openSection, setOpenSection] = useState<'voices' | 'sfx' | 'music' | 'chapters' | null>('voices')
  const [addingVoice, setAddingVoice] = useState(false)

  useEffect(() => {
    loadAudiobooks(); loadStories(); loadPresets(); loadVoices(); loadSfxLibrary()
  }, [loadAudiobooks, loadStories, loadPresets, loadVoices, loadSfxLibrary])

  const finishedStories = stories.filter(s => (s.word_count ?? 0) > 0)

  const voices = project?.voice_profiles ?? []

  /** A preset is a real configuration (engine + params), not a label — that
   *  is why it is worth offering: only some engines can clone or take an
   *  emotion at all. Without one you get a plain IndexTTS2 profile. */
  const addVoice = (preset?: {
    name: string; color: string; model_type: string
    default_emotion?: string | null
    params: Record<string, number | string | boolean>
  }) => {
    const n = voices.length + 1
    const next: AudiobookVoiceProfile = {
      id: nextProfileId(voices),
      name: preset?.name ?? (n === 1 ? 'Narrator' : `Voice ${n}`),
      color: preset?.color ?? SWATCHES[(n - 1) % SWATCHES.length],
      model_type: preset?.model_type ?? 'index_tts2',
      voice_ref_path: null,
      emotion_ref_path: null,
      default_emotion: preset?.default_emotion ?? null,
      params: { ...(preset?.params ?? {}) },
    }
    setAddingVoice(false)
    patchAudiobook({
      voice_profiles: [...voices, next],
      // First profile becomes the default so imported paragraphs have a
      // voice without the user assigning every run by hand.
      ...(voices.length === 0 ? { default_profile_id: next.id } : {}),
    })
  }

  const patchVoice = (id: string, patch: Partial<AudiobookVoiceProfile>) =>
    patchAudiobook({ voice_profiles: voices.map(v => (v.id === id ? { ...v, ...patch } : v)) })

  return (
    <div className="flex flex-col gap-4">
      {error && (
        <div className="rounded-lg border border-border border-l-4 border-l-red-500/60 bg-bg-tertiary/60 px-3 py-2 text-[11px] text-text-secondary">
          {error}
        </div>
      )}

      {/* Projects */}
      <div>
        <div className="mb-1.5 flex items-center justify-between">
          <label className="text-[11px] uppercase tracking-wider text-text-muted">Audiobooks</label>
          <button
            onClick={() => createAudiobook()}
            className="flex items-center gap-0.5 text-[10px] text-accent-blue hover:text-accent-blue-hover"
            aria-label="New audiobook"
          >
            <Plus size={10} /> New
          </button>
        </div>
        {projects.length === 0 ? (
          <p className="text-[11px] text-text-muted">No projects yet.</p>
        ) : (
          <div className="max-h-36 space-y-0.5 overflow-y-auto">
            {projects.map(p => (
              <div
                key={p.id}
                onClick={() => selectAudiobook(p.id)}
                className={`group flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1 ${
                  p.id === activeId ? 'bg-bg-active' : 'hover:bg-bg-hover'
                }`}
              >
                <BookAudio size={11} className="shrink-0 text-text-muted" />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[11px] text-text-primary">{p.title}</div>
                  <div className="text-[9px] text-text-muted">
                    {p.chapter_count} ch · {p.voice_count} voices · {p.rendered_chapters} rendered
                  </div>
                </div>
                <button
                  onClick={e => {
                    e.stopPropagation()
                    if (confirmDelete === p.id) { deleteAudiobook(p.id); setConfirmDelete(null) }
                    else { setConfirmDelete(p.id); setTimeout(() => setConfirmDelete(null), 3000) }
                  }}
                  className={`rounded p-1 opacity-0 transition-opacity group-hover:opacity-100 ${
                    confirmDelete === p.id ? 'text-red-400 opacity-100' : 'text-text-muted hover:text-text-primary'
                  }`}
                  title={confirmDelete === p.id ? 'Click again to delete' : 'Delete project'}
                  aria-label="Delete project"
                >
                  <Trash2 size={11} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Import is available with no project open: picking a file creates
          one, named after the file. */}
      <>
        <div>
            <label className="text-[11px] uppercase tracking-wider text-text-muted">Import text</label>
            <input
              ref={fileRef}
              type="file"
              accept=".txt,.md,.docx,.pdf,.epub"
              className="hidden"
              onChange={async e => {
                const f = e.target.files?.[0]
                e.target.value = ''
                if (!f) return
                if (!activeId) {
                  const created = await createAudiobook(f.name.replace(/\.[^.]+$/, ''))
                  if (!created) return
                }
                importFile(f, { autoSplit })
              }}
            />
            <button
              onClick={() => fileRef.current?.click()}
              disabled={busy}
              className="mt-1.5 flex w-full items-center justify-center gap-1.5 rounded-lg border border-border py-2 text-xs text-text-secondary transition-colors hover:border-border-light hover:text-text-primary disabled:opacity-40"
            >
              {busy ? <Loader2 size={12} className="animate-spin" /> : <Upload size={12} />}
              {busy ? 'Importing…' : 'Choose a file'}
            </button>
            <p className="mt-1 text-[10px] text-text-muted">txt · md · docx · pdf · epub</p>
            <label className="mt-1.5 flex cursor-pointer items-center gap-1.5 text-[10px] text-text-muted">
              <input
                type="checkbox"
                checked={autoSplit}
                onChange={e => setAutoSplit(e.target.checked)}
                className="h-3 w-3 rounded border-border bg-bg-tertiary accent-accent-blue"
              />
              split into chapters at headings
            </label>
            <button
              onClick={() => {
                setStoryDraft({ premise: '' })
                setGenerationMode('text')
                setTextSubMode('story')
              }}
              className="mt-2 w-full rounded-lg border border-border py-1.5 text-[10px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
            >
              No text yet? Write a story first
            </button>

            {finishedStories.length > 0 && (
              <div className="mt-2">
                <label className="text-[10px] text-text-muted">Or take a finished story</label>
                <div className="mt-1 space-y-0.5">
                  {finishedStories.slice(0, 6).map(s => (
                    <button
                      key={s.id}
                      onClick={() => importStory(s.id)}
                      disabled={busy}
                      className="block w-full truncate rounded-md border border-border px-2 py-1 text-left text-[10px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary disabled:opacity-40"
                      title={`${s.chapter_count} chapters · ${(s.word_count ?? 0).toLocaleString()} words`}
                    >
                      {s.title || 'Untitled'} — {s.chapter_count} ch
                    </button>
                  ))}
                </div>
              </div>
            )}
        </div>
      </>

      {project && (
        <>
          {/* Voices */}
          <Section
            label={`Voices (${voices.length})`}
            open={openSection === 'voices'}
            onToggle={() => setOpenSection(s => (s === 'voices' ? null : 'voices'))}
            action={
              <button
                onClick={e => { e.stopPropagation(); setOpenSection('voices'); setAddingVoice(v => !v) }}
                className="flex items-center gap-0.5 text-[10px] text-accent-blue hover:text-accent-blue-hover"
              >
                <Plus size={10} /> Add voice
              </button>
            }
          >
            {addingVoice && (
              <div className="mb-2 space-y-1.5 rounded-lg border border-border bg-bg-tertiary/40 p-2">
                <div className="text-[10px] uppercase tracking-wider text-text-muted">
                  Start from a preset
                </div>
                {presets.map(p => (
                  <button
                    key={p.id}
                    onClick={() => addVoice(p)}
                    className="block w-full rounded-md border border-border px-2 py-1.5 text-left transition-colors hover:border-border-light"
                  >
                    <div className="flex items-center gap-1.5">
                      <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: p.color }} />
                      <span className="truncate text-[11px] text-text-primary">{p.name}</span>
                      {p.needs_reference && (
                        <span className="ml-auto shrink-0 text-[9px] text-indicator-warning">
                          needs a clip
                        </span>
                      )}
                    </div>
                    <div className="mt-0.5 text-[9px] leading-snug text-text-muted">{p.description}</div>
                  </button>
                ))}

                {libraryVoices.length > 0 && (
                  <>
                    <div className="mt-2 flex items-center gap-1 text-[10px] uppercase tracking-wider text-text-muted">
                      <Library size={10} /> From library
                    </div>
                    {libraryVoices.map(v => (
                      <button
                        key={v.id}
                        onClick={() => { setAddingVoice(false); importLibraryVoice(v.id) }}
                        className="flex w-full items-center gap-1.5 rounded-md border border-border px-2 py-1 text-left transition-colors hover:border-border-light"
                        title={v.description || v.model_type}
                      >
                        <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: v.color }} />
                        <span className="truncate text-[11px] text-text-primary">{v.name}</span>
                        <span className="ml-auto shrink-0 text-[9px] text-text-muted">
                          {v.ready ? 'ready' : 'no reference'}
                        </span>
                      </button>
                    ))}
                  </>
                )}

                <button
                  onClick={() => addVoice()}
                  className="w-full rounded border border-border py-1 text-[10px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
                >
                  Blank voice
                </button>
              </div>
            )}

            {voices.length === 0 ? (
              <p className="text-[11px] text-text-muted">
                Add at least one voice — a paragraph without one cannot be rendered.
              </p>
            ) : (
              <div className="space-y-2">
                {voices.map(v => (
                  <VoiceRow
                    key={v.id}
                    voice={v}
                    isDefault={project.default_profile_id === v.id}
                    onDefault={() => patchAudiobook({ default_profile_id: v.id })}
                    onPatch={patch => patchVoice(v.id, patch)}
                    onPreview={() => previewVoice(v.id)}
                    previewing={previewBusy === v.id}
                    previewBlocked={previewBusy !== null && previewBusy !== v.id}
                    previewUrl={previewUrls[v.id]}
                    warnings={previewWarnings[v.id]}
                    onDelete={() => patchAudiobook({
                      voice_profiles: voices.filter(x => x.id !== v.id),
                      ...(project.default_profile_id === v.id
                        ? { default_profile_id: voices.find(x => x.id !== v.id)?.id ?? null }
                        : {}),
                    })}
                  />
                ))}
              </div>
            )}
          </Section>

          {/* Sound effects */}
          <Section
            label={`Effects (${project.sfx.length})`}
            open={openSection === 'sfx'}
            onToggle={() => setOpenSection(s => (s === 'sfx' ? null : 'sfx'))}
          >
            <AssetList
              kind="sfx"
              items={project.sfx.map(a => ({
                id: a.id, label: a.label, audio_path: a.audio_path,
                detail: `${a.duration}s · ${a.playback_mode}${a.loop ? ' · loop' : ''}`,
              }))}
              onCreate={body => createAsset('sfx', body)}
              onDelete={id => deleteAsset('sfx', id)}
              library={sfxLibrary}
              onAdopt={adoptSfx}
              onRefreshLibrary={loadSfxLibrary}
            />
          </Section>

          {/* Background music */}
          <Section
            label={`Music (${project.music.length})`}
            open={openSection === 'music'}
            onToggle={() => setOpenSection(s => (s === 'music' ? null : 'music'))}
          >
            <AssetList
              kind="music"
              items={project.music.map(a => ({
                id: a.id, label: a.title, audio_path: a.audio_path,
                detail: `${Math.round(a.duration)}s${a.loop ? ' · loop' : ''}`,
              }))}
              onCreate={body => createAsset('music', body)}
              onDelete={id => deleteAsset('music', id)}
            />
          </Section>

          {/* Chapters */}
          <Section
            label={`Chapters (${project.chapters.length})`}
            open={openSection === 'chapters'}
            onToggle={() => setOpenSection(s => (s === 'chapters' ? null : 'chapters'))}
          >
            <div className="max-h-48 space-y-0.5 overflow-y-auto">
              {project.chapters.map((c, i) => (
                <button
                  key={c.id}
                  onClick={() => setAbChapter(c.id)}
                  className={`block w-full rounded-md px-2 py-1 text-left ${
                    c.id === activeChapterId ? 'bg-bg-active' : 'hover:bg-bg-hover'
                  }`}
                >
                  <div className="truncate text-[11px] text-text-primary">
                    {i + 1}. {c.title || 'Untitled'}
                  </div>
                  <div className="text-[9px] text-text-muted">
                    {c.blocks.length} blocks
                    {c.audio_path && ` · ${Math.round(c.audio_duration ?? 0)}s rendered`}
                  </div>
                </button>
              ))}
            </div>
          </Section>
        </>
      )}
    </div>
  )
}

function Section({ label, open, onToggle, action, children }: {
  label: string
  open: boolean
  onToggle: () => void
  action?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <button onClick={onToggle} className="flex flex-1 items-center gap-1 text-left">
          {open ? <ChevronDown size={11} className="text-text-muted" /> : <ChevronRight size={11} className="text-text-muted" />}
          <span className="text-[11px] uppercase tracking-wider text-text-muted">{label}</span>
        </button>
        {action}
      </div>
      {open && children}
    </div>
  )
}

function VoiceRow({
  voice, isDefault, onPatch, onDefault, onDelete,
  onPreview, previewing, previewBlocked, previewUrl, warnings,
}: {
  voice: AudiobookVoiceProfile
  isDefault: boolean
  onPatch: (patch: Partial<AudiobookVoiceProfile>) => void
  onDefault: () => void
  onDelete: () => void
  onPreview: () => void
  previewing: boolean
  /** Another audition holds the generation slot — one at a time. */
  previewBlocked: boolean
  previewUrl?: string
  warnings?: string[]
}) {
  const refInput = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  const caps = TTS_MODELS.find(m => m.type === voice.model_type)

  const uploadRef = async (file: File) => {
    setUploading(true)
    try {
      const { uploadAudio } = await import('../../api/client')
      const { path } = await uploadAudio(file)
      onPatch({ voice_ref_path: path })
    } catch { /* the panel shows the store-level error */ } finally {
      setUploading(false)
    }
  }

  return (
    <div className="rounded-lg border border-border bg-bg-tertiary/40 p-2">
      <div className="flex items-center gap-1.5">
        <input
          type="color"
          value={voice.color}
          onChange={e => onPatch({ color: e.target.value })}
          className="h-5 w-5 shrink-0 cursor-pointer rounded border-0 bg-transparent p-0"
          aria-label="Voice colour"
        />
        <input
          value={voice.name}
          onChange={e => onPatch({ name: e.target.value })}
          className="min-w-0 flex-1 rounded border border-border bg-bg-tertiary px-1.5 py-0.5 text-[11px] text-text-primary"
          aria-label="Voice name"
        />
        <button
          onClick={onPreview}
          disabled={previewing || previewBlocked}
          title="Speak a sample line with this voice"
          aria-label="Preview voice"
          className="rounded p-1 text-text-muted hover:text-text-primary disabled:opacity-30"
        >
          {previewing ? <Loader2 size={11} className="animate-spin" /> : <Play size={11} />}
        </button>
        <button
          onClick={onDefault}
          title={isDefault ? 'Default voice' : 'Make this the default voice'}
          aria-label="Make default voice"
          className={`rounded p-1 ${isDefault ? 'text-indicator-success' : 'text-text-muted hover:text-text-primary'}`}
        >
          <Check size={11} />
        </button>
        <button
          onClick={onDelete}
          title="Remove voice"
          aria-label="Remove voice"
          className="rounded p-1 text-text-muted hover:text-text-primary"
        >
          <Trash2 size={11} />
        </button>
      </div>

      <select
        value={voice.model_type}
        onChange={e => onPatch({ model_type: e.target.value })}
        className="mt-1.5 w-full rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary"
        aria-label="TTS model"
      >
        {TTS_MODELS.map(m => <option key={m.type} value={m.type}>{m.label}</option>)}
      </select>

      {caps?.clone ? (
        <>
          <input
            ref={refInput}
            type="file"
            accept="audio/*,video/*"
            className="hidden"
            onChange={e => {
              const f = e.target.files?.[0]
              if (f) uploadRef(f)
              e.target.value = ''
            }}
          />
          <button
            onClick={() => refInput.current?.click()}
            disabled={uploading}
            className="mt-1.5 flex w-full items-center justify-center gap-1 rounded border border-border py-1 text-[10px] text-text-secondary hover:text-text-primary disabled:opacity-40"
          >
            {uploading ? <Loader2 size={10} className="animate-spin" /> : <Mic size={10} />}
            {voice.voice_ref_path ? 'Replace reference clip' : 'Upload reference clip'}
          </button>
          {voice.voice_ref_path && (
            <p className="mt-1 truncate text-[9px] text-text-muted" title={voice.voice_ref_path}>
              {voice.voice_ref_path.split(/[\\/]/).pop()}
            </p>
          )}
          {voice.model_type === 'index_tts2' && !voice.voice_ref_path && (
            <p className="mt-1 text-[9px] text-indicator-warning">
              IndexTTS2 needs a reference clip.
            </p>
          )}
        </>
      ) : (
        <p className="mt-1.5 text-[9px] text-text-muted">
          {voice.model_type === 'qwen3_tts_customvoice'
            ? 'Uses a built-in speaker preset — set "speaker" below.'
            : 'Describe the voice instead of cloning one.'}
        </p>
      )}

      {voice.model_type === 'qwen3_tts_voicedesign' && (
        <input
          value={String(voice.params?.voice_description ?? '')}
          onChange={e => onPatch({ params: { ...voice.params, voice_description: e.target.value } })}
          placeholder="e.g. older man, gravelly, unhurried"
          className="mt-1.5 w-full rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary placeholder:text-text-muted"
        />
      )}
      {voice.model_type === 'qwen3_tts_customvoice' && (
        <input
          value={String(voice.params?.speaker ?? '')}
          onChange={e => onPatch({ params: { ...voice.params, speaker: e.target.value } })}
          placeholder="speaker preset id"
          className="mt-1.5 w-full rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary placeholder:text-text-muted"
        />
      )}

      <input
        value={voice.default_emotion ?? ''}
        onChange={e => onPatch({ default_emotion: e.target.value || null })}
        placeholder={caps?.emotion === 'none' ? 'emotion not supported by this model' : 'default emotion (optional)'}
        disabled={caps?.emotion === 'none'}
        className="mt-1.5 w-full rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary placeholder:text-text-muted disabled:opacity-50"
      />

      {previewing && (
        <p className="mt-1 text-[9px] text-text-muted">
          Rendering a sample line — the first use of a model downloads it.
        </p>
      )}
      {previewUrl && !previewing && (
        <audio src={previewUrl} controls autoPlay className="mt-1.5 h-6 w-full" />
      )}
      {(warnings ?? []).length > 0 && (
        <p className="mt-1 flex items-start gap-1 text-[9px] text-indicator-warning">
          <AlertTriangle size={9} className="mt-0.5 shrink-0" />
          <span>{(warnings ?? []).join(' · ')}</span>
        </p>
      )}
    </div>
  )
}


/**
 * Shared list + create form for sound effects and music beds.
 *
 * Generation is fire-and-forget: the asset appears at once with no audio and
 * fills in when the job finishes, so a slow model does not block the panel.
 * An asset without audio is marked pending rather than looking ready and
 * then failing the render.
 */
function AssetList({ kind, items, onCreate, onDelete, library, onAdopt, onRefreshLibrary }: {
  kind: 'sfx' | 'music'
  items: { id: string; label: string; audio_path?: string | null; detail: string }[]
  onCreate: (body: {
    label?: string; prompt?: string; duration?: number
    playback_mode?: 'parallel' | 'sequential'; loop?: boolean; volume?: number
  }) => Promise<void>
  onDelete: (id: string) => Promise<void>
  /** Effects already on disk. Adopting one costs nothing — no generation. */
  library?: SfxLibraryEffect[]
  onAdopt?: (effect: SfxLibraryEffect, opts?: {
    playback_mode?: 'parallel' | 'sequential'; loop?: boolean
  }) => Promise<void>
  onRefreshLibrary?: () => Promise<void>
}) {
  const [adding, setAdding] = useState(false)
  const [browsing, setBrowsing] = useState(false)
  const [adoptMode, setAdoptMode] = useState<'parallel' | 'sequential'>('parallel')
  const [label, setLabel] = useState('')
  const [prompt, setPrompt] = useState('')
  const [duration, setDuration] = useState(kind === 'sfx' ? 5 : 60)
  const [mode, setMode] = useState<'parallel' | 'sequential'>('parallel')
  const [loop, setLoop] = useState(kind === 'music')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (!prompt.trim()) return
    setBusy(true)
    try {
      await onCreate({
        label: label.trim() || undefined,
        prompt: prompt.trim(),
        duration,
        ...(kind === 'sfx' ? { playback_mode: mode } : {}),
        loop,
      })
      setLabel(''); setPrompt(''); setAdding(false)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-1.5">
      {items.length === 0 && !adding && (
        <p className="text-[11px] text-text-muted">
          {kind === 'sfx'
            ? 'Effects are generated from a text prompt and can play under a paragraph or between them.'
            : 'A music bed plays under a whole chapter and ducks automatically while anyone speaks.'}
        </p>
      )}

      {items.map(a => (
        <div key={a.id} className="flex items-center gap-1.5 rounded-md border border-border bg-bg-tertiary/40 px-2 py-1">
          <div className="min-w-0 flex-1">
            <div className="truncate text-[11px] text-text-primary">{a.label}</div>
            <div className="text-[9px] text-text-muted">
              {a.audio_path ? a.detail : 'generating…'}
            </div>
          </div>
          {a.audio_path && (
            <audio
              src={`/api/v1/file/${encodeURIComponent(a.audio_path.split(/[\\/]/).pop() || '')}`}
              controls
              className="h-6 w-24"
            />
          )}
          <button
            onClick={() => onDelete(a.id)}
            title="Delete and unlink everywhere"
            aria-label="Delete asset"
            className="rounded p-1 text-text-muted hover:text-text-primary"
          >
            <Trash2 size={11} />
          </button>
        </div>
      ))}

      {adding ? (
        <div className="space-y-1.5 rounded-lg border border-border bg-bg-tertiary/40 p-2">
          <input
            value={label}
            onChange={e => setLabel(e.target.value)}
            placeholder="Name (optional)"
            className="w-full rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary placeholder:text-text-muted"
          />
          <textarea
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
            rows={2}
            placeholder={kind === 'sfx'
              ? 'Describe the sound — English works best: "heavy rain on a tin roof"'
              : 'Describe the music: "sparse melancholic piano, slow"'}
            className="w-full resize-y rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary placeholder:text-text-muted"
          />
          <div className="flex items-center gap-2">
            <label className="text-[10px] text-text-muted">Seconds</label>
            <input
              type="number"
              min={1}
              max={kind === 'sfx' ? 30 : 300}
              value={duration}
              onChange={e => setDuration(Math.max(1, Number(e.target.value) || 1))}
              className="w-16 rounded border border-border bg-bg-tertiary px-1.5 py-0.5 text-[10px] text-text-primary"
            />
            <label className="flex cursor-pointer items-center gap-1 text-[10px] text-text-muted">
              <input
                type="checkbox"
                checked={loop}
                onChange={e => setLoop(e.target.checked)}
                className="h-3 w-3 rounded border-border bg-bg-tertiary accent-accent-blue"
              />
              loop
            </label>
          </div>
          {kind === 'sfx' && (
            <select
              value={mode}
              onChange={e => setMode(e.target.value as 'parallel' | 'sequential')}
              className="w-full rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary"
            >
              <option value="parallel">Parallel — plays under the speech</option>
              <option value="sequential">Sequential — speech pauses for it</option>
            </select>
          )}
          <div className="flex gap-1.5">
            <button
              onClick={submit}
              disabled={busy || !prompt.trim()}
              className="flex-1 rounded bg-cta py-1 text-[10px] font-medium text-white disabled:opacity-40"
            >
              {busy ? 'Starting…' : 'Generate'}
            </button>
            <button
              onClick={() => setAdding(false)}
              className="rounded border border-border px-2 py-1 text-[10px] text-text-secondary hover:text-text-primary"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="flex gap-1.5">
          <button
            onClick={() => setAdding(true)}
            className="flex flex-1 items-center justify-center gap-1 rounded-lg border border-border py-1.5 text-[10px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
          >
            <Plus size={10} /> {kind === 'sfx' ? 'New effect' : 'New music bed'}
          </button>
          {onAdopt && (
            <button
              onClick={() => { setBrowsing(v => !v); if (!browsing) onRefreshLibrary?.() }}
              className={`flex items-center justify-center gap-1 rounded-lg border px-2 py-1.5 text-[10px] transition-colors ${
                browsing
                  ? 'border-accent-blue text-accent-blue'
                  : 'border-border text-text-secondary hover:border-border-light hover:text-text-primary'
              }`}
              title="Reuse an effect you already generated in Audio → SFX"
            >
              <Library size={10} /> From library
            </button>
          )}
        </div>
      )}

      {/* Existing files, reusable as they are — adopting one generates nothing. */}
      {browsing && onAdopt && (
        <div className="space-y-1.5 rounded-lg border border-border bg-bg-tertiary/40 p-2">
          <select
            value={adoptMode}
            onChange={e => setAdoptMode(e.target.value as 'parallel' | 'sequential')}
            className="w-full rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary"
          >
            <option value="parallel">Parallel — plays under the speech</option>
            <option value="sequential">Sequential — speech pauses for it</option>
          </select>
          {(library ?? []).length === 0 ? (
            <p className="text-[10px] text-text-muted">
              Nothing yet. Effects made in Audio → SFX show up here.
            </p>
          ) : (
            <div className="max-h-56 space-y-1 overflow-y-auto">
              {(library ?? []).map(eff => (
                <div key={eff.path} className="rounded-md border border-border bg-bg-tertiary/60 px-2 py-1">
                  <div className="truncate text-[10px] text-text-primary" title={eff.name}>
                    {eff.name}
                  </div>
                  {eff.prompt && (
                    <div className="truncate text-[9px] text-text-muted" title={eff.prompt}>
                      {eff.prompt}
                    </div>
                  )}
                  <div className="mt-1 flex items-center gap-1.5">
                    <audio src={eff.url} controls className="h-6 min-w-0 flex-1" />
                    <button
                      onClick={() => onAdopt(eff, { playback_mode: adoptMode })}
                      className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[9px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
                    >
                      Adopt
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
