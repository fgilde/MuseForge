import { useEffect, useRef, useState } from 'react'
import {
  Mic2, Plus, Trash2, Play, Loader2, Upload, AlertTriangle, Check, X,
} from 'lucide-react'
import { useStore } from '../../stores/useStore'
import { uploadAudio, type VoiceDraft } from '../../api/client'

const SWATCHES = ['#22d3ee', '#a78bfa', '#f472b6', '#4ade80', '#fb923c', '#facc15', '#60a5fa', '#f87171']

const BLANK: VoiceDraft = {
  name: '',
  model_type: 'index_tts2',
  reference_path: null,
  default_emotion: null,
  language: null,
  description: '',
  params: {},
}

/**
 * The workspace voice library, in the sidebar: what exists, whether it can
 * actually speak, and a one-click audition. Full editing lives in the main
 * area (VoicesView) — this column is for picking and checking.
 */
export function VoicesPanel() {
  const voices = useStore(s => s.voices)
  const engines = useStore(s => s.voiceEngines)
  const error = useStore(s => s.voicesError)
  const busy = useStore(s => s.voicesBusy)
  const loadVoices = useStore(s => s.loadVoices)
  const createVoice = useStore(s => s.createVoiceEntry)
  const deleteVoice = useStore(s => s.deleteVoiceEntry)
  const preview = useStore(s => s.previewLibraryVoice)
  const previewBusy = useStore(s => s.voicePreviewBusy)
  const previewUrls = useStore(s => s.voicePreviewUrls)
  const previewWarnings = useStore(s => s.voicePreviewWarnings)

  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState<VoiceDraft>(BLANK)
  const [uploading, setUploading] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const refInput = useRef<HTMLInputElement>(null)

  useEffect(() => { loadVoices() }, [loadVoices])

  const engineList = Object.entries(engines)
  const caps = engines[draft.model_type ?? 'index_tts2']

  const submit = async () => {
    if (!draft.name?.trim()) return
    const id = await createVoice({
      ...draft,
      color: SWATCHES[voices.length % SWATCHES.length],
    })
    if (id) { setDraft(BLANK); setAdding(false) }
  }

  const uploadRef = async (file: File) => {
    setUploading(true)
    try {
      const { path } = await uploadAudio(file)
      setDraft(d => ({ ...d, reference_path: path }))
    } catch { /* the banner above shows the reason */ } finally {
      setUploading(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {error && (
        <div className="rounded-lg border border-border border-l-4 border-l-red-500/60 bg-bg-tertiary/60 px-3 py-2 text-[11px] text-text-secondary">
          {error}
        </div>
      )}

      <div>
        <div className="mb-1.5 flex items-center justify-between">
          <label className="text-[11px] uppercase tracking-wider text-text-muted">
            Voices ({voices.length})
          </label>
          <button
            onClick={() => setAdding(v => !v)}
            className="flex items-center gap-0.5 text-[10px] text-accent-blue hover:text-accent-blue-hover"
          >
            {adding ? <X size={10} /> : <Plus size={10} />} {adding ? 'Cancel' : 'New voice'}
          </button>
        </div>

        {adding && (
          <div className="mb-2 space-y-1.5 rounded-lg border border-border bg-bg-tertiary/40 p-2">
            <input
              value={draft.name ?? ''}
              onChange={e => setDraft(d => ({ ...d, name: e.target.value }))}
              placeholder="Name — e.g. Narrator"
              className="w-full rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[11px] text-text-primary placeholder:text-text-muted"
            />
            <select
              value={draft.model_type ?? 'index_tts2'}
              onChange={e => setDraft(d => ({ ...d, model_type: e.target.value }))}
              className="w-full rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary"
              aria-label="TTS engine"
            >
              {engineList.map(([type, eng]) => (
                <option key={type} value={type}>{eng.label}</option>
              ))}
            </select>

            {/* Only the fields this engine actually uses. */}
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
                  className="flex w-full items-center justify-center gap-1 rounded border border-border py-1 text-[10px] text-text-secondary hover:text-text-primary disabled:opacity-40"
                >
                  {uploading ? <Loader2 size={10} className="animate-spin" /> : <Upload size={10} />}
                  {draft.reference_path ? 'Replace reference clip' : 'Upload reference clip'}
                </button>
                {draft.reference_path && (
                  <p className="truncate text-[9px] text-text-muted" title={draft.reference_path}>
                    {draft.reference_path.split(/[\\/]/).pop()}
                  </p>
                )}
                {caps.needs_reference && !draft.reference_path && (
                  <p className="text-[9px] text-indicator-warning">
                    {caps.label} cannot speak without a reference clip. You can also
                    record one in the main area.
                  </p>
                )}
              </>
            ) : (
              <p className="text-[9px] text-text-muted">
                No reference clip needed for {caps?.label ?? 'this engine'}.
              </p>
            )}

            {draft.model_type === 'qwen3_tts_voicedesign' && (
              <input
                value={String(draft.params?.voice_description ?? '')}
                onChange={e => setDraft(d => ({
                  ...d, params: { ...d.params, voice_description: e.target.value },
                }))}
                placeholder="Describe the voice — older man, gravelly, unhurried"
                className="w-full rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary placeholder:text-text-muted"
              />
            )}
            {draft.model_type === 'qwen3_tts_customvoice' && (
              <input
                value={String(draft.params?.speaker ?? '')}
                onChange={e => setDraft(d => ({
                  ...d, params: { ...d.params, speaker: e.target.value },
                }))}
                placeholder="speaker preset id"
                className="w-full rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary placeholder:text-text-muted"
              />
            )}

            <div className="flex gap-1.5">
              <input
                value={draft.language ?? ''}
                onChange={e => setDraft(d => ({ ...d, language: e.target.value || null }))}
                placeholder="lang (en)"
                className="w-20 rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary placeholder:text-text-muted"
              />
              <input
                value={draft.default_emotion ?? ''}
                onChange={e => setDraft(d => ({ ...d, default_emotion: e.target.value || null }))}
                placeholder={caps?.emotion === 'none' ? 'no emotion support' : 'default emotion'}
                disabled={caps?.emotion === 'none'}
                className="min-w-0 flex-1 rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary placeholder:text-text-muted disabled:opacity-50"
              />
            </div>

            <button
              onClick={submit}
              disabled={busy || !draft.name?.trim()}
              className="w-full rounded bg-cta py-1 text-[10px] font-medium text-white disabled:opacity-40"
            >
              {busy ? 'Saving…' : 'Create voice'}
            </button>
          </div>
        )}

        {voices.length === 0 ? (
          <p className="text-[11px] text-text-muted">
            A saved voice can be auditioned once and then imported into any
            audiobook — the reference recording is stored here, not per book.
          </p>
        ) : (
          <div className="space-y-1.5">
            {voices.map(v => {
              const eng = engines[v.model_type]
              const previewing = previewBusy === v.id
              const url = previewUrls[v.id]
                ?? (v.sample_path
                  ? `/api/v1/file/${encodeURIComponent(v.sample_path.split(/[\\/]/).pop() || '')}`
                  : undefined)
              const warnings = previewWarnings[v.id] ?? []
              return (
                <div key={v.id} className="rounded-lg border border-border bg-bg-tertiary/40 p-2">
                  <div className="flex items-center gap-1.5">
                    <span
                      className="h-2.5 w-2.5 shrink-0 rounded-full"
                      style={{ backgroundColor: v.color }}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[11px] text-text-primary">{v.name}</div>
                      <div className="truncate text-[9px] text-text-muted">
                        {eng?.label ?? v.model_type}
                        {v.language ? ` · ${v.language}` : ''}
                      </div>
                    </div>
                    <button
                      onClick={() => preview(v.id)}
                      disabled={!v.ready || previewing || (previewBusy !== null && !previewing)}
                      title={v.ready ? 'Speak a sample line' : 'This voice needs a reference clip first'}
                      aria-label="Preview voice"
                      className="rounded p-1 text-text-muted hover:text-text-primary disabled:opacity-30"
                    >
                      {previewing ? <Loader2 size={11} className="animate-spin" /> : <Play size={11} />}
                    </button>
                    <button
                      onClick={() => {
                        if (confirmDelete === v.id) { deleteVoice(v.id); setConfirmDelete(null) }
                        else { setConfirmDelete(v.id); setTimeout(() => setConfirmDelete(null), 3000) }
                      }}
                      title={confirmDelete === v.id ? 'Click again to delete' : 'Delete voice'}
                      aria-label="Delete voice"
                      className={`rounded p-1 ${
                        confirmDelete === v.id ? 'text-red-400' : 'text-text-muted hover:text-text-primary'
                      }`}
                    >
                      <Trash2 size={11} />
                    </button>
                  </div>

                  {/* Status is the point of this list: a voice missing its
                      reference cannot speak, and must not look like it can. */}
                  <div className="mt-1 flex flex-wrap items-center gap-1">
                    {v.ready ? (
                      <span className="flex items-center gap-0.5 text-[9px] text-indicator-success">
                        <Check size={9} /> ready
                      </span>
                    ) : (
                      <span className="flex items-center gap-0.5 text-[9px] text-indicator-warning">
                        <AlertTriangle size={9} /> needs a reference clip
                      </span>
                    )}
                    {v.reference_missing && (
                      <span className="text-[9px] text-red-400">reference file is gone</span>
                    )}
                    {v.default_emotion && (
                      <span className="text-[9px] text-text-muted">· {v.default_emotion}</span>
                    )}
                  </div>

                  {previewing && (
                    <p className="mt-1 text-[9px] text-text-muted">
                      Rendering a sample line — the first use of a model downloads it.
                    </p>
                  )}
                  {url && !previewing && (
                    <audio src={url} controls className="mt-1.5 h-6 w-full" />
                  )}
                  {warnings.length > 0 && (
                    <p className="mt-1 text-[9px] text-indicator-warning">{warnings.join(' · ')}</p>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      <p className="flex items-start gap-1.5 text-[10px] leading-snug text-text-muted">
        <Mic2 size={11} className="mt-0.5 shrink-0" />
        <span>
          Edit, record and audition in the main area. To use a voice in a book,
          open Audio → Book and pick it under Voices → Add voice → From library.
        </span>
      </p>
    </div>
  )
}
