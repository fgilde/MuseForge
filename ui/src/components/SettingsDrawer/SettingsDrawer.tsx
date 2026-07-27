import { X } from 'lucide-react'
import { useStore } from '../../stores/useStore'
import { SystemSettingsPanel } from './SystemSettingsPanel'
import { ServicesSettingsPanel } from './ServicesSettingsPanel'

/**
 * Settings dialog — global panel for hardware/perf and external-service
 * configuration, presented as a centered animated glass modal. Two tabs
 * in both Studio and Director modes:
 *
 *   Performance    — VRAM coefficient, profile, hardware tier, etc.
 *                    (mounts <SystemSettingsPanel />)
 *   Integrations   — LLM provider, API keys, NSFW master gate, etc.
 *                    (mounts <ServicesSettingsPanel />)
 *
 * The dialog stays mounted and animates via opacity/scale so opening
 * feels instant and closing doesn't unmount mid-edit; pointer-events
 * are disabled while hidden.
 */
export function SettingsDrawer() {
  const settingsOpen = useStore(s => s.settingsOpen)
  const setSettingsOpen = useStore(s => s.setSettingsOpen)
  const settingsTab = useStore(s => s.settingsTab)
  const setSettingsTab = useStore(s => s.setSettingsTab)

  const tabs = [
    { id: 'performance' as const, label: 'Performance' },
    { id: 'integrations' as const, label: 'Integrations' },
  ]

  return (
    <div className={`fixed inset-0 z-50 flex items-center justify-center p-4 transition-opacity duration-300 ${
      settingsOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'
    }`}>
      {/* Backdrop — dim + blur everything behind the dialog */}
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-sm"
        onClick={() => setSettingsOpen(false)}
      />

      {/* Dialog */}
      <div className={`relative glass-panel w-full md:w-[560px] max-h-[85vh] rounded-2xl shadow-2xl flex flex-col transform transition-all duration-300 ease-out ${
        settingsOpen ? 'scale-100 translate-y-0' : 'scale-95 translate-y-3'
      }`}>
        {/* Header */}
        <div className="px-5 py-3 border-b border-border flex items-center justify-between shrink-0">
          <h2 className="font-semibold text-sm">Settings</h2>
          <button
            onClick={() => setSettingsOpen(false)}
            className="p-1.5 rounded-lg hover:bg-bg-hover text-text-secondary hover:text-text-primary transition-colors"
          >
            <X size={16} />
          </button>
        </div>

        {/* Tab Bar */}
        <div className="px-5 pt-3 shrink-0">
          <div className="flex bg-bg-tertiary rounded-lg p-0.5 border border-border">
            {tabs.map(tab => (
              <button
                key={tab.id}
                onClick={() => setSettingsTab(tab.id)}
                className={`flex-1 text-xs py-1.5 rounded-md transition-all ${
                  settingsTab === tab.id
                    ? 'bg-bg-active text-text-primary'
                    : 'text-text-secondary hover:text-text-primary'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {/* Content */}
        <div className="overflow-y-auto px-5 py-4 space-y-5 min-h-0">
          {settingsTab === 'performance' && (
            <SystemSettingsPanel />
          )}

          {settingsTab === 'integrations' && (
            <ServicesSettingsPanel />
          )}
        </div>
      </div>
    </div>
  )
}
