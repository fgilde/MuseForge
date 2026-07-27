import { Image, Video, AudioLines, Wand2, Wrench, MessageSquareText } from 'lucide-react'
import { useStore } from '../../stores/useStore'
import type { GenerationMode } from '../../types'

const modes: { value: GenerationMode; label: string; icon: typeof Image }[] = [
  { value: 'image', label: 'Image', icon: Image },
  { value: 'video', label: 'Video', icon: Video },
  { value: 'audio', label: 'Audio', icon: AudioLines },
  { value: 'avatar', label: 'Edit', icon: Wand2 },
  { value: 'tools', label: 'Tools', icon: Wrench },
  { value: 'text', label: 'Text', icon: MessageSquareText },
]

export function GenerationModeSelector() {
  const generationMode = useStore(s => s.generationMode)
  const setGenerationMode = useStore(s => s.setGenerationMode)

  return (
    <div className="flex bg-bg-tertiary rounded-lg p-0.5 border border-border">
      {modes.map(m => {
        const Icon = m.icon
        const active = generationMode === m.value
        return (
          <button
            key={m.value}
            onClick={() => setGenerationMode(m.value)}
            className={`flex-1 min-w-0 flex items-center justify-center gap-1.5 text-xs py-2 rounded-md transition-all ${
              active
                ? 'bg-bg-active text-text-primary'
                : 'text-text-secondary hover:text-text-primary'
            }`}
          >
            <Icon size={14} className="shrink-0" />
            {/* Six modes share this row — the label truncates on narrow
                phones instead of overflowing the pill. */}
            <span className="truncate">{m.label}</span>
          </button>
        )
      })}
    </div>
  )
}
