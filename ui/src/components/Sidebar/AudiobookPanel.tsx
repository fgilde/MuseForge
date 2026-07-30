import { useEffect, useRef, useState } from 'react'
import {
  BookAudio, Plus, Trash2, Upload, Loader2, Mic, ChevronDown, ChevronRight, Check,
} from 'lucide-react'
import { useStore } from '../../stores/useStore'
import type { AudiobookVoiceProfile } from '../../api/client'

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

  const fileRef = useRef<HTMLInputElement>(null)
  const importFile = useStore(s => s.importAudiobookFile)
  const [autoSplit, setAutoSplit] = useState(true)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [openSection, setOpenSection] = useState<'voices' | 'chapters' | null>('voices')

  useEffect(() => { loadAudiobooks() }, [loadAudiobooks])

  const voices = project?.voice_profiles ?? []

  const addVoice = () => {
    const n = voices.length + 1
    const next: AudiobookVoiceProfile = {
      id: `vp${Date.now().toString(36)}`,
      name: n === 1 ? 'Narrator' : `Voice ${n}`,
      color: SWATCHES[(n - 1) % SWATCHES.length],
      model_type: 'index_tts2',
      voice_ref_path: null,
      emotion_ref_path: null,
      default_emotion: null,
      params: {},
    }
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

      {project && (
        <>
          {/* Import */}
          <div>
            <label className="text-[11px] uppercase tracking-wider text-text-muted">Import text</label>
            <input
              ref={fileRef}
              type="file"
              accept=".txt,.md,.docx,.pdf,.epub"
              className="hidden"
              onChange={e => {
                const f = e.target.files?.[0]
                if (f) importFile(f, { autoSplit })
                e.target.value = ''
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
          </div>

          {/* Voices */}
          <Section
            label={`Voices (${voices.length})`}
            open={openSection === 'voices'}
            onToggle={() => setOpenSection(s => (s === 'voices' ? null : 'voices'))}
            action={
              <button
                onClick={e => { e.stopPropagation(); addVoice() }}
                className="flex items-center gap-0.5 text-[10px] text-accent-blue hover:text-accent-blue-hover"
              >
                <Plus size={10} /> Add
              </button>
            }
          >
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

function VoiceRow({ voice, isDefault, onPatch, onDefault, onDelete }: {
  voice: AudiobookVoiceProfile
  isDefault: boolean
  onPatch: (patch: Partial<AudiobookVoiceProfile>) => void
  onDefault: () => void
  onDelete: () => void
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
    </div>
  )
}
