import { Mic, Mic2, Music, Zap, Layers, BookAudio } from 'lucide-react'
import { useStore } from '../../stores/useStore'
import type { AudioSubMode } from '../../types'

const modes: { value: AudioSubMode; label: string; icon: typeof Mic }[] = [
  { value: 'speech', label: 'Speech', icon: Mic },
  { value: 'music', label: 'Music', icon: Music },
  { value: 'sfx', label: 'SFX', icon: Zap },
  { value: 'mixer', label: 'Mixer', icon: Layers },
  { value: 'audiobook', label: 'Book', icon: BookAudio },
  { value: 'voices', label: 'Voices', icon: Mic2 },
]

export function AudioSubModeToggle() {
  const audioSubMode = useStore(s => s.audioSubMode)
  const setAudioSubMode = useStore(s => s.setAudioSubMode)

  return (
    <div className="flex bg-bg-tertiary rounded-lg p-0.5 border border-border">
      {modes.map(m => {
        const Icon = m.icon
        const active = audioSubMode === m.value
        return (
          <button
            key={m.value}
            onClick={() => setAudioSubMode(m.value)}
            title={m.label}
            className={`flex-1 min-w-0 flex items-center justify-center gap-1 text-[11px] py-1.5 rounded-md transition-all ${
              active
                ? 'bg-bg-active text-text-primary'
                : 'text-text-secondary hover:text-text-primary'
            }`}
          >
            <Icon size={13} className="shrink-0" />
            <span className="truncate">{m.label}</span>
          </button>
        )
      })}
    </div>
  )
}
