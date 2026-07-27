import { MessageSquare, BookText } from 'lucide-react'
import { useStore } from '../../stores/useStore'
import type { TextSubMode } from '../../types'

const modes: { value: TextSubMode; label: string; icon: typeof MessageSquare }[] = [
  { value: 'chat', label: 'Chat', icon: MessageSquare },
  // Story isn't built yet — selectable so the panel can explain what's
  // coming instead of showing a dead, disabled button.
  { value: 'story', label: 'Story', icon: BookText },
]

export function TextSubModeToggle() {
  const textSubMode = useStore(s => s.textSubMode)
  const setTextSubMode = useStore(s => s.setTextSubMode)

  return (
    <div className="flex bg-bg-tertiary rounded-lg p-0.5 border border-border">
      {modes.map(m => {
        const Icon = m.icon
        const active = textSubMode === m.value
        return (
          <button
            key={m.value}
            onClick={() => setTextSubMode(m.value)}
            className={`flex-1 flex items-center justify-center gap-1.5 text-xs py-1.5 rounded-md transition-all ${
              active
                ? 'bg-bg-active text-text-primary'
                : 'text-text-secondary hover:text-text-primary'
            }`}
          >
            <Icon size={13} />
            <span>{m.label}</span>
          </button>
        )
      })}
    </div>
  )
}
