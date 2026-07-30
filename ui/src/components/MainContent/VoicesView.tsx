import { useEffect, useRef, useState } from 'react'
import {
  Mic2, Upload, Loader2, Play, Square, Circle, AlertTriangle, Check,
  RefreshCw, Trash2, BookAudio, Dices, Snowflake,
} from 'lucide-react'
import { useStore } from '../../stores/useStore'
import { uploadAudio, type VoiceEngine, type VoiceLibraryEntry } from '../../api/client'

/** Container the browser gave us → an extension /api/v1/upload-audio accepts.
 *  webm/mp4 land in its video branch, which extracts the audio track to wav —
 *  exactly what a reference clip needs. */
function recordingExtension(mime: string): string {
  if (mime.includes('ogg')) return 'ogg'
  if (mime.includes('mp4')) return 'm4a'
  if (mime.includes('wav')) return 'wav'
  return 'webm'
}

const fileName = (path?: string | null) => path?.split(/[\\/]/).pop() ?? ''

export function VoicesView() {
  const voices = useStore(s => s.voices)
  const engines = useStore(s => s.voiceEngines)
  const error = useStore(s => s.voicesError)
  const loadVoices = useStore(s => s.loadVoices)
  const loadSampleTexts = useStore(s => s.loadAbVoicePresets)
  const adoptVoice = useStore(s => s.adoptVoiceFromFile)
  const busy = useStore(s => s.voicesBusy)
  const adoptInput = useRef<HTMLInputElement>(null)

  useEffect(() => { loadVoices(); loadSampleTexts() }, [loadVoices, loadSampleTexts])

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="shrink-0 border-b border-border px-5 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold text-text-primary">Voices</h1>
            <p className="mt-0.5 text-[11px] text-text-muted">
              {voices.length} saved · reference recordings live here once and are
              reused by every audiobook
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <input
              ref={adoptInput}
              type="file"
              accept="audio/*,video/*"
              className="hidden"
              onChange={e => {
                const f = e.target.files?.[0]
                if (f) adoptVoice(f)
                e.target.value = ''
              }}
            />
            <button
              onClick={() => adoptInput.current?.click()}
              disabled={busy}
              title="Pick a recording — it becomes a cloning voice that stays the same in every passage"
              className="rounded-lg bg-cta px-2.5 py-1 text-[11px] font-medium text-white disabled:opacity-40"
            >
              {busy
                ? <Loader2 size={12} className="mr-1 inline animate-spin" />
                : <Upload size={12} className="mr-1 inline" />}
              Voice from a recording
            </button>
            <button
              onClick={() => loadVoices()}
              className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
            >
              <RefreshCw size={12} className="mr-1 inline" /> Reload
            </button>
          </div>
        </div>
        {error && <p className="mt-2 text-[11px] text-red-400">{error}</p>}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
        {voices.length === 0 ? (
          <div className="flex h-full items-center justify-center">
            <div className="max-w-md text-center">
              <Mic2 size={28} className="mx-auto mb-3 text-text-muted" />
              <h2 className="text-sm font-semibold text-text-primary">No voices yet</h2>
              <p className="mt-1 text-xs text-text-secondary">
                Fastest route: <span className="text-text-secondary">Voice from a
                recording</span> above — any clip of the voice you want becomes a
                clone that stays the same in every passage. Describing a voice
                instead works too, but those engines invent a new speaker on
                every render until you audition one and keep the take.
              </p>
            </div>
          </div>
        ) : (
          <div className="grid gap-4 xl:grid-cols-2">
            {voices.map(v => (
              <VoiceCard key={v.id} voice={v} engine={engines[v.model_type]} engines={engines} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function VoiceCard({ voice, engine, engines }: {
  voice: VoiceLibraryEntry
  engine?: VoiceEngine
  engines: Record<string, VoiceEngine>
}) {
  const patch = useStore(s => s.patchVoiceEntry)
  const remove = useStore(s => s.deleteVoiceEntry)
  const reroll = useStore(s => s.rerollVoiceEntry)
  const freeze = useStore(s => s.freezeVoiceEntry)
  const preview = useStore(s => s.previewLibraryVoice)
  const previewBusy = useStore(s => s.voicePreviewBusy)
  const previewUrls = useStore(s => s.voicePreviewUrls)
  const previewWarnings = useStore(s => s.voicePreviewWarnings)
  const sampleTexts = useStore(s => s.abVoiceSampleTexts)

  const fileInput = useRef<HTMLInputElement>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const [uploading, setUploading] = useState(false)
  const [recording, setRecording] = useState(false)
  const [recError, setRecError] = useState('')
  const [clip, setClip] = useState<{ url: string; file: File } | null>(null)
  const [sampleText, setSampleText] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)

  const previewing = previewBusy === voice.id
  const previewUrl = previewUrls[voice.id]
    ?? (voice.sample_path ? `/api/v1/file/${encodeURIComponent(fileName(voice.sample_path))}` : undefined)
  const warnings = previewWarnings[voice.id] ?? []
  const lang = (voice.language || 'en').slice(0, 2)
  const canRecord = typeof MediaRecorder !== 'undefined' && !!navigator.mediaDevices

  const setReference = async (file: File) => {
    setUploading(true)
    try {
      const { path } = await uploadAudio(file)
      await patch(voice.id, { reference_path: path })
      setClip(prev => { if (prev) URL.revokeObjectURL(prev.url); return null })
    } catch (e) {
      setRecError(e instanceof Error ? e.message : 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  const startRecording = async () => {
    setRecError('')
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const chunks: Blob[] = []
      const rec = new MediaRecorder(stream)
      rec.ondataavailable = e => { if (e.data.size > 0) chunks.push(e.data) }
      rec.onstop = () => {
        stream.getTracks().forEach(t => t.stop())
        const mime = rec.mimeType || 'audio/webm'
        const blob = new Blob(chunks, { type: mime })
        const file = new File([blob], `voice-reference.${recordingExtension(mime)}`, { type: mime })
        setClip(prev => {
          if (prev) URL.revokeObjectURL(prev.url)
          return { url: URL.createObjectURL(blob), file }
        })
        setRecording(false)
      }
      recorder.current = rec
      rec.start()
      setRecording(true)
    } catch (e) {
      setRecError(e instanceof Error ? e.message : 'Microphone access was refused')
    }
  }

  return (
    <div className="glass-panel rounded-2xl p-4">
      {/* Identity */}
      <div className="flex items-center gap-2">
        <input
          type="color"
          value={voice.color}
          onChange={e => patch(voice.id, { color: e.target.value })}
          className="h-6 w-6 shrink-0 cursor-pointer rounded border-0 bg-transparent p-0"
          aria-label="Voice colour"
        />
        <input
          defaultValue={voice.name}
          onBlur={e => {
            if (e.target.value.trim() && e.target.value !== voice.name) {
              patch(voice.id, { name: e.target.value.trim() })
            }
          }}
          className="min-w-0 flex-1 rounded-lg border border-border bg-bg-tertiary px-2 py-1 text-sm text-text-primary"
          aria-label="Voice name"
        />
        {voice.ready ? (
          <span className="flex shrink-0 items-center gap-0.5 text-[10px] text-indicator-success">
            <Check size={11} /> ready
          </span>
        ) : (
          <span className="flex shrink-0 items-center gap-0.5 text-[10px] text-indicator-warning">
            <AlertTriangle size={11} /> not usable yet
          </span>
        )}
        <button
          onClick={() => {
            if (confirmDelete) { remove(voice.id); setConfirmDelete(false) }
            else { setConfirmDelete(true); setTimeout(() => setConfirmDelete(false), 3000) }
          }}
          title={confirmDelete ? 'Click again to delete' : 'Delete voice'}
          aria-label="Delete voice"
          className={`shrink-0 rounded p-1.5 ${
            confirmDelete ? 'text-red-400' : 'text-text-muted hover:text-text-primary'
          }`}
        >
          <Trash2 size={13} />
        </button>
      </div>

      {/* Engine + language + emotion */}
      <div className="mt-3 grid grid-cols-2 gap-2">
        <label className="col-span-2 block">
          <span className="text-[10px] uppercase tracking-wider text-text-muted">Engine</span>
          <select
            value={voice.model_type}
            onChange={e => patch(voice.id, { model_type: e.target.value })}
            className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary"
          >
            {Object.entries(engines).map(([type, eng]) => (
              <option key={type} value={type}>{eng.label}</option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-[10px] uppercase tracking-wider text-text-muted">Language</span>
          <input
            defaultValue={voice.language ?? ''}
            onBlur={e => patch(voice.id, { language: e.target.value.trim() || null })}
            placeholder="en"
            className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary placeholder:text-text-muted"
          />
        </label>
        <label className="block">
          <span className="text-[10px] uppercase tracking-wider text-text-muted">Default emotion</span>
          <input
            defaultValue={voice.default_emotion ?? ''}
            onBlur={e => patch(voice.id, { default_emotion: e.target.value.trim() || null })}
            placeholder={engine?.emotion === 'none' ? 'not supported by this engine' : 'neutral'}
            disabled={engine?.emotion === 'none'}
            className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary placeholder:text-text-muted disabled:opacity-50"
          />
        </label>
      </div>

      {/* Engine-specific parameters — only what has an effect here. */}
      {voice.model_type === 'qwen3_tts_voicedesign' && (
        <label className="mt-2 block">
          <span className="text-[10px] uppercase tracking-wider text-text-muted">Voice description</span>
          <input
            defaultValue={String(voice.params?.voice_description ?? '')}
            onBlur={e => patch(voice.id, {
              params: { ...voice.params, voice_description: e.target.value },
            })}
            placeholder="older man, gravelly, unhurried"
            className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary placeholder:text-text-muted"
          />
        </label>
      )}
      {voice.model_type === 'qwen3_tts_customvoice' && (
        <label className="mt-2 block">
          <span className="text-[10px] uppercase tracking-wider text-text-muted">Speaker preset</span>
          <input
            defaultValue={String(voice.params?.speaker ?? '')}
            onBlur={e => patch(voice.id, { params: { ...voice.params, speaker: e.target.value } })}
            placeholder="speaker preset id"
            className="mt-1 w-full rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary placeholder:text-text-muted"
          />
        </label>
      )}
      <label className="mt-2 block">
        <span className="text-[10px] uppercase tracking-wider text-text-muted">Temperature</span>
        <input
          type="number"
          step={0.05}
          min={0.1}
          max={2}
          defaultValue={Number(voice.params?.temperature ?? 0.8)}
          onBlur={e => patch(voice.id, {
            params: { ...voice.params, temperature: Number(e.target.value) || 0.8 },
          })}
          className="mt-1 w-24 rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary"
        />
      </label>

      <label className="mt-2 block">
        <span className="text-[10px] uppercase tracking-wider text-text-muted">Notes</span>
        <textarea
          defaultValue={voice.description}
          onBlur={e => patch(voice.id, { description: e.target.value })}
          rows={2}
          placeholder="What this voice is for — which character, which book."
          className="mt-1 w-full resize-y rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary placeholder:text-text-muted"
        />
      </label>

      {/* Reference clip — upload one, or record it right here. */}
      {engine?.clone ? (
        <div className="mt-3 rounded-xl border border-border bg-bg-tertiary/40 p-2.5">
          <div className="text-[10px] uppercase tracking-wider text-text-muted">Reference recording</div>
          {voice.reference_path ? (
            <div className="mt-1.5">
              <p className="truncate text-[11px] text-text-secondary" title={voice.reference_path}>
                {fileName(voice.reference_path)}
              </p>
              {voice.reference_missing ? (
                <p className="mt-0.5 text-[10px] text-red-400">
                  This file is gone from the workspace — upload or record it again.
                </p>
              ) : (
                <audio
                  src={`/api/v1/file/${encodeURIComponent(fileName(voice.reference_path))}`}
                  controls
                  className="mt-1 h-7 w-full"
                />
              )}
            </div>
          ) : (
            <p className="mt-1 text-[11px] text-text-secondary">
              {engine.needs_reference
                ? 'Required: 10–30 seconds of clean speech in the voice you want.'
                : 'Optional — without one the engine uses its own voice.'}
            </p>
          )}

          <div className="mt-2 flex flex-wrap gap-1.5">
            <input
              ref={fileInput}
              type="file"
              accept="audio/*,video/*"
              className="hidden"
              onChange={e => {
                const f = e.target.files?.[0]
                if (f) setReference(f)
                e.target.value = ''
              }}
            />
            <button
              onClick={() => fileInput.current?.click()}
              disabled={uploading}
              className="flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary disabled:opacity-40"
            >
              {uploading ? <Loader2 size={11} className="animate-spin" /> : <Upload size={11} />}
              Upload a file
            </button>
            {recording ? (
              <button
                onClick={() => recorder.current?.stop()}
                className="flex items-center gap-1 rounded-lg border border-red-500/60 px-2 py-1 text-[11px] text-red-400"
              >
                <Square size={11} /> Stop recording
              </button>
            ) : (
              <button
                onClick={startRecording}
                disabled={!canRecord || uploading}
                title={canRecord ? 'Record with your microphone' : 'This browser cannot record audio'}
                className="flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary disabled:opacity-40"
              >
                <Circle size={11} /> Record
              </button>
            )}
          </div>

          {recording && (
            <p className="mt-1.5 text-[10px] text-indicator-warning">
              Recording — read a few sentences at your normal pace, then stop.
            </p>
          )}
          {clip && !recording && (
            <div className="mt-2 rounded-lg border border-border bg-bg-tertiary/60 p-2">
              <p className="text-[10px] text-text-muted">Take not saved yet</p>
              <audio src={clip.url} controls className="mt-1 h-7 w-full" />
              <div className="mt-1.5 flex gap-1.5">
                <button
                  onClick={() => setReference(clip.file)}
                  disabled={uploading}
                  className="flex-1 rounded-lg bg-cta py-1 text-[11px] font-medium text-white disabled:opacity-40"
                >
                  {uploading ? 'Uploading…' : 'Use this recording'}
                </button>
                <button
                  onClick={() => setClip(prev => { if (prev) URL.revokeObjectURL(prev.url); return null })}
                  className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:text-text-primary"
                >
                  Discard
                </button>
              </div>
            </div>
          )}
          {recError && <p className="mt-1.5 text-[10px] text-red-400">{recError}</p>}
        </div>
      ) : (
        /* Not just a note: hiding upload/record behind an engine choice made it
           look as if your own voice could not be used at all. Say why, and
           offer the switch that makes it possible. */
        <div className="mt-3 rounded-xl border border-border bg-bg-tertiary/40 p-2.5">
          <div className="text-[10px] uppercase tracking-wider text-text-muted">
            Your own recording
          </div>
          <p className="mt-1 text-[11px] text-text-secondary">
            {engine?.label ?? 'This engine'} builds a voice from the description
            above and cannot copy a recording. To use your own voice — recorded
            here or uploaded — switch to a cloning engine.
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {Object.entries(engines)
              .filter(([, eng]) => eng.clone)
              .map(([type, eng]) => (
                <button
                  key={type}
                  onClick={() => patch(voice.id, { model_type: type })}
                  className="rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
                  title={eng.needs_reference
                    ? 'Needs a reference clip — you can record one right after switching'
                    : 'A clip is optional for this engine'}
                >
                  Switch to {eng.label}
                </button>
              ))}
          </div>
        </div>
      )}

      {/* A frozen voice is otherwise a dead end: the clip decides who speaks,
          so rerolling the seed does nothing. Offer the way back, but only when
          the written description it was built from is still there. */}
      {voice.reference_path && !voice.reference_missing
        && !!voice.params?.voice_description && (
        <div className="mt-3 rounded-xl border border-border bg-bg-tertiary/40 p-2.5">
          <div className="flex items-center justify-between gap-2">
            <p className="min-w-0 text-[11px] text-text-secondary">
              Kept as a recording — this voice now stays the same in every
              passage.
            </p>
            <button
              onClick={() => reroll(voice.id, { unfreeze: true })}
              className="shrink-0 flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
              title="Drop the recording and go back to searching from the description"
            >
              <Dices size={11} /> Try another
            </button>
          </div>
        </div>
      )}

      {/* Identity. Only meaningful without a clip: there the seed decides who
          the speaker turns out to be, so it has to be visible and stickable.
          Keyed on the clip, not on the engine — a cloning engine that does not
          require one (KugelAudio) and has none is in exactly the same boat. */}
      {(!voice.reference_path || voice.reference_missing) && (
        <div className="mt-3 rounded-xl border border-border bg-bg-tertiary/40 p-2.5">
          <div className="text-[10px] uppercase tracking-wider text-text-muted">
            Voice identity
          </div>
          <p className="mt-1 text-[11px] text-text-secondary">
            Without a reference clip this engine invents a speaker on every
            single render — the same description does not give you the same
            person twice. Audition until you hear one you like, then keep that
            take: it becomes this voice's reference clip and every passage is
            spoken by it.
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <button
              onClick={() => freeze(voice.id)}
              disabled={!previewUrl || previewing}
              title={previewUrl
                ? 'Make the audition below this voice’s reference clip'
                : 'Audition the voice first — the take you keep is what gets frozen'}
              className="flex items-center gap-1 rounded-lg bg-cta px-2.5 py-1 text-[11px] font-medium text-white disabled:opacity-40"
            >
              <Snowflake size={11} /> Keep this take
            </button>
            <button
              onClick={() => reroll(voice.id)}
              className="flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary"
              title="Drop the stored audition and start looking again"
            >
              <Dices size={11} /> Start over
            </button>
          </div>
          <p className="mt-1.5 text-[10px] text-text-muted">
            Seed <span className="font-mono">{voice.seed ?? '—'}</span> — fixed so
            an unchanged passage is not re-rendered. It does not pin the voice;
            only keeping a take does.
          </p>
        </div>
      )}

      {/* Audition */}
      <div className="mt-3 rounded-xl border border-border bg-bg-tertiary/40 p-2.5">
        <div className="text-[10px] uppercase tracking-wider text-text-muted">Audition</div>
        <div className="mt-1.5 flex gap-1.5">
          <input
            value={sampleText}
            onChange={e => setSampleText(e.target.value)}
            placeholder={sampleTexts[lang] || sampleTexts.en || 'A line to read…'}
            className="min-w-0 flex-1 rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-xs text-text-primary placeholder:text-text-muted"
          />
          <button
            onClick={() => preview(voice.id, sampleText.trim() || undefined, voice.language || undefined)}
            disabled={!voice.ready || previewing || (previewBusy !== null && !previewing)}
            title={voice.ready
              ? 'Render this line with this voice'
              : 'Add the reference clip this engine needs first'}
            className="flex shrink-0 items-center gap-1 rounded-lg bg-cta px-2.5 py-1.5 text-[11px] font-medium text-white shadow-accent-glow disabled:opacity-40"
          >
            {previewing ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
            {previewing ? 'Rendering…' : 'Speak'}
          </button>
        </div>
        {previewing && (
          <p className="mt-1.5 text-[10px] text-text-muted">
            Runs through the same TTS path a book render uses — the first use of
            a model downloads it, which can take several minutes.
          </p>
        )}
        {previewUrl && !previewing && (
          <audio src={previewUrl} controls className="mt-1.5 h-7 w-full" />
        )}
        {warnings.length > 0 && (
          <p className="mt-1.5 flex items-start gap-1 text-[10px] text-indicator-warning">
            <AlertTriangle size={10} className="mt-0.5 shrink-0" />
            <span>{warnings.join(' · ')}</span>
          </p>
        )}
      </div>

      <p className="mt-2.5 flex items-start gap-1.5 text-[10px] leading-snug text-text-muted">
        <BookAudio size={11} className="mt-0.5 shrink-0" />
        <span>
          To read a book with this voice: Audio → Book → Voices → Add voice →
          From library. The book gets its own copy, so tuning it there leaves
          this entry untouched.
        </span>
      </p>
    </div>
  )
}
