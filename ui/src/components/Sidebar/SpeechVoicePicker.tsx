import { useEffect } from 'react'
import { Loader2, Mic2, Volume2 } from 'lucide-react'
import { useStore, speechSlot } from '../../stores/useStore'

/**
 * Pick a saved library voice for Audio → Speech.
 *
 * The Forge button runs the selected generation model and knows nothing about
 * the library, so a picked voice would simply be ignored there. Hence the own
 * "Speak with voice" button below: it routes to /voices/{id}/speak, which
 * reads the prompt with that voice and writes a normal workspace output.
 */
export function SpeechVoicePicker() {
  const voices = useStore(s => s.voices)
  const loadVoices = useStore(s => s.loadVoices)
  const error = useStore(s => s.voicesError)
  const voiceId = useStore(s => s.speechVoiceId)
  const emotion = useStore(s => s.speechVoiceEmotion)
  const language = useStore(s => s.speechVoiceLanguage)
  const setSpeechVoice = useStore(s => s.setSpeechVoice)
  const speak = useStore(s => s.speakWithLibraryVoice)
  const setAudioSubMode = useStore(s => s.setAudioSubMode)
  const prompt = useStore(s => (s.params.prompt as string) || '')
  const previewBusy = useStore(s => s.voicePreviewBusy)
  const url = useStore(s => (voiceId ? s.voicePreviewUrls[speechSlot(voiceId)] : undefined))
  const warnings = useStore(s => (voiceId ? s.voicePreviewWarnings[speechSlot(voiceId)] : undefined))

  useEffect(() => { loadVoices() }, [loadVoices])

  const voice = voices.find(v => v.id === voiceId) ?? null
  const busy = voiceId !== null && previewBusy === speechSlot(voiceId)

  return (
    <div className="space-y-2">
      <label className="block text-[11px] uppercase tracking-wider text-text-muted">
        Library Voice
      </label>

      <select
        value={voiceId ?? ''}
        onChange={e => {
          const next = voices.find(v => v.id === e.target.value) ?? null
          setSpeechVoice({
            id: next?.id ?? null,
            // Prefill from the voice, still editable below.
            language: next?.language ?? '',
            emotion: next?.default_emotion ?? '',
          })
        }}
        className="w-full rounded-lg border border-border bg-bg-tertiary px-2 py-1.5 text-[11px] text-text-primary focus:border-accent-blue focus:outline-none"
        aria-label="Library voice"
      >
        <option value="">None — use the selected model directly</option>
        {voices.map(v => (
          <option key={v.id} value={v.id} disabled={!v.ready}>
            {v.name}{v.ready ? '' : ' — needs a reference recording'}
          </option>
        ))}
      </select>

      {voices.length === 0 && (
        <p className="text-[9px] text-text-muted">
          No saved voices yet. Create one under Audio → Voices.
        </p>
      )}

      {voice && (
        <div className="space-y-2 rounded-lg border border-border bg-bg-tertiary/40 p-2">
          <div className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: voice.color }} />
            <span className="min-w-0 flex-1 truncate text-[10px] text-text-secondary">
              {voice.model_type}
            </span>
          </div>

          <div className="flex gap-1.5">
            <input
              value={language}
              onChange={e => setSpeechVoice({ language: e.target.value })}
              placeholder="lang (en)"
              className="w-20 rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary placeholder:text-text-muted focus:border-accent-blue focus:outline-none"
              aria-label="Language"
            />
            {voice.emotion_support !== 'none' && (
              <input
                value={emotion}
                onChange={e => setSpeechVoice({ emotion: e.target.value })}
                placeholder="emotion (optional)"
                className="min-w-0 flex-1 rounded border border-border bg-bg-tertiary px-1.5 py-1 text-[10px] text-text-primary placeholder:text-text-muted focus:border-accent-blue focus:outline-none"
                aria-label="Emotion"
              />
            )}
          </div>

          <button
            onClick={() => speak(prompt)}
            disabled={busy || previewBusy !== null || !prompt.trim()}
            className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-cta py-1.5 text-[11px] font-medium text-white shadow-accent-glow disabled:opacity-40"
          >
            {busy ? <Loader2 size={12} className="animate-spin" /> : <Volume2 size={12} />}
            {busy ? 'Speaking…' : 'Speak with voice'}
          </button>

          <p className="text-[9px] leading-snug text-text-muted">
            Use this button — the Forge button below generates with the selected
            model and ignores the library voice.
          </p>

          {busy && (
            <p className="text-[9px] text-text-muted">
              The first use of a model downloads it.
            </p>
          )}
          {url && !busy && (
            <audio key={url} src={url} controls autoPlay className="h-6 w-full" />
          )}
          {warnings && warnings.length > 0 && (
            <p className="text-[9px] text-indicator-warning">{warnings.join(' · ')}</p>
          )}
        </div>
      )}

      {error && <p className="text-[9px] text-red-400">{error}</p>}

      <button
        onClick={() => setAudioSubMode('voices')}
        className="flex items-start gap-1.5 text-left text-[10px] leading-snug text-text-muted hover:text-accent-blue"
      >
        <Mic2 size={11} className="mt-0.5 shrink-0" />
        <span>Manage voices in Audio → Voices</span>
      </button>
    </div>
  )
}
