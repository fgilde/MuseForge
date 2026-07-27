import { useEffect, useState } from 'react'
import { MessageSquareText, Plus, Pencil, Trash2, Check, X, ChevronDown, ChevronRight, Sparkles } from 'lucide-react'
import { useStore } from '../../stores/useStore'
import { TextSubModeToggle } from './TextSubModeToggle'

/**
 * Sidebar for the Text mode: sub-mode toggle, the thread list, which LLM
 * answers, the active thread's system prompt and sampling shortcuts.
 *
 * The conversation itself lives in the main area (`ChatView`) — this panel
 * is only the control surface, mirroring how ToolsPanel replaces the
 * generation controls for Tools mode.
 */
export function TextPanel() {
  const textSubMode = useStore(s => s.textSubMode)
  const threads = useStore(s => s.chatThreads)
  const activeChatId = useStore(s => s.activeChatId)
  const activeThread = useStore(s => s.activeChatThread)
  const streamingId = useStore(s => s.chatStreamingId)
  const streaming = streamingId !== null
  const loadChatThreads = useStore(s => s.loadChatThreads)
  const createChatThread = useStore(s => s.createChatThread)
  const selectChatThread = useStore(s => s.selectChatThread)
  const deleteChatThread = useStore(s => s.deleteChatThread)
  const renameChatThread = useStore(s => s.renameChatThread)
  const patchChatThread = useStore(s => s.patchChatThread)

  const [editing, setEditing] = useState<{ id: string; title: string } | null>(null)

  // Load the thread list when Text mode opens, and land on the most recent
  // conversation so the main area isn't empty on arrival.
  useEffect(() => {
    let cancelled = false
    loadChatThreads().then(() => {
      if (cancelled) return
      const s = useStore.getState()
      if (!s.activeChatId && s.chatThreads.length > 0) s.selectChatThread(s.chatThreads[0].id)
    })
    return () => { cancelled = true }
  }, [loadChatThreads])

  if (textSubMode === 'story') {
    return (
      <div className="flex flex-col gap-4">
        <TextSubModeToggle />
        <div className="rounded-lg border border-border bg-bg-tertiary/50 p-4 text-center">
          <Sparkles size={18} className="mx-auto mb-2 text-accent-blue" />
          <p className="text-xs text-text-secondary">Story writing is coming soon.</p>
          <p className="text-[10px] text-text-muted mt-1">
            Long-form drafting with chapters and characters. Use Chat in the meantime.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <TextSubModeToggle />

      {/* Conversations */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-1.5 text-[11px] text-text-muted uppercase tracking-wider">
            <MessageSquareText size={12} /> Conversations
          </div>
          <button
            onClick={() => createChatThread()}
            disabled={streaming}
            aria-label="New chat"
            title="New chat"
            className="flex items-center gap-1 px-2 py-1 rounded-lg bg-bg-tertiary border border-border text-[10px] text-text-secondary hover:text-accent-blue hover:border-border-light transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Plus size={11} /> New
          </button>
        </div>

        {threads.length === 0 ? (
          <p className="text-[10px] text-text-muted px-1">
            No conversations yet. Hit New, or just type a message.
          </p>
        ) : (
          <div className="space-y-1 max-h-56 overflow-y-auto">
            {threads.map(t => {
              const active = t.id === activeChatId
              const isEditing = editing?.id === t.id
              return (
                <div
                  key={t.id}
                  className={`group rounded-lg border px-2 py-1.5 transition-colors ${
                    active
                      ? 'border-accent-blue/40 bg-accent-blue/10'
                      : 'border-border bg-bg-tertiary/40 hover:border-border-light'
                  }`}
                >
                  {isEditing ? (
                    <div className="flex items-center gap-1">
                      <input
                        autoFocus
                        value={editing.title}
                        onChange={e => setEditing({ id: t.id, title: e.target.value })}
                        onKeyDown={e => {
                          if (e.key === 'Enter') {
                            if (editing.title.trim()) renameChatThread(t.id, editing.title.trim())
                            setEditing(null)
                          }
                          if (e.key === 'Escape') setEditing(null)
                        }}
                        aria-label="Chat title"
                        className="flex-1 min-w-0 bg-bg-secondary border border-border rounded px-1.5 py-0.5 text-xs text-text-primary focus:outline-none focus:border-accent-blue"
                      />
                      <button
                        onClick={() => {
                          if (editing.title.trim()) renameChatThread(t.id, editing.title.trim())
                          setEditing(null)
                        }}
                        aria-label="Save title"
                        className="p-1 rounded hover:bg-bg-hover text-indicator-success"
                      >
                        <Check size={11} />
                      </button>
                      <button
                        onClick={() => setEditing(null)}
                        aria-label="Cancel rename"
                        className="p-1 rounded hover:bg-bg-hover text-text-muted"
                      >
                        <X size={11} />
                      </button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => selectChatThread(t.id)}
                        className="flex-1 min-w-0 text-left"
                      >
                        <div className={`text-xs truncate ${active ? 'text-text-primary' : 'text-text-secondary'}`}>
                          {t.title}
                        </div>
                        <div className="text-[10px] text-text-muted truncate">
                          {t.message_count} {t.message_count === 1 ? 'message' : 'messages'}
                          {t.preview ? ` · ${t.preview}` : ''}
                        </div>
                      </button>
                      <button
                        onClick={() => setEditing({ id: t.id, title: t.title })}
                        aria-label={`Rename ${t.title}`}
                        title="Rename"
                        className="p-1 rounded hover:bg-bg-hover text-text-muted hover:text-text-primary opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity shrink-0"
                      >
                        <Pencil size={11} />
                      </button>
                      <button
                        onClick={() => deleteChatThread(t.id)}
                        disabled={streamingId === t.id}
                        aria-label={`Delete ${t.title}`}
                        title="Delete"
                        className="p-1 rounded hover:bg-bg-hover text-text-muted hover:text-red-400 opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity shrink-0 disabled:opacity-20"
                      >
                        <Trash2 size={11} />
                      </button>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      <ChatModelSelect />

      {/* System prompt — collapsed by default, saved to the active thread */}
      {activeThread && (
        <SystemPromptField
          key={activeThread.id}
          value={activeThread.system_prompt}
          onSave={v => patchChatThread(activeThread.id, { system_prompt: v })}
        />
      )}

      <ChatSamplingOptions />
    </div>
  )
}

/** Which LLM answers. This writes the SERVICE setting (`llm_model_id`) —
 *  the same field Settings → Services exposes — because that's what the
 *  chat endpoint actually loads. The per-thread `model_id` the API accepts
 *  is only recorded, never used for generation, so exposing it here would
 *  be a control that silently does nothing. */
function ChatModelSelect() {
  const servicesConfig = useStore(s => s.servicesConfig)
  const updateConfig = useStore(s => s.updateServicesConfig)
  const llmModels = useStore(s => s.llmModels)
  const llmStatus = useStore(s => s.llmStatus)
  const streaming = useStore(s => s.chatStreamingId !== null)

  if (!servicesConfig) return null
  const provider = servicesConfig.llm_provider || 'local'
  const models = llmModels.filter(m => {
    const mp = (m as { provider?: string }).provider || 'local'
    return provider === 'local' ? mp === 'local' : mp === 'local' || mp === provider
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <label htmlFor="chat-llm-model" className="text-[11px] text-text-muted uppercase tracking-wider">
          Model
        </label>
        <span className="flex items-center gap-1 text-[10px] text-text-muted">
          <span className={`w-1.5 h-1.5 rounded-full ${llmStatus?.loaded ? 'bg-indicator-success' : 'bg-text-muted/30'}`} />
          {llmStatus?.loaded ? 'Loaded' : 'Standby'}
        </span>
      </div>
      <select
        id="chat-llm-model"
        value={servicesConfig.llm_model_id}
        onChange={e => updateConfig({ llm_model_id: e.target.value })}
        disabled={streaming}
        className="w-full bg-bg-tertiary border border-border rounded-lg px-3 py-2 text-sm text-text-primary focus:outline-none focus:border-accent-blue disabled:opacity-50"
      >
        {models.map(m => (
          <option key={m.id} value={m.id}>{m.label} ({m.size_hint})</option>
        ))}
      </select>
      <p className="text-[10px] text-text-muted mt-1">
        Loads on the first message — the very first use downloads the weights.
      </p>
    </div>
  )
}

function SystemPromptField({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const [open, setOpen] = useState(!!value)
  const [draft, setDraft] = useState(value)

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex items-center gap-1 text-[11px] text-text-muted uppercase tracking-wider hover:text-text-secondary transition-colors"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        System Prompt
        {!open && value && <span className="normal-case tracking-normal text-accent-blue">· set</span>}
      </button>
      {open && (
        <>
          <textarea
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onBlur={() => { if (draft !== value) onSave(draft) }}
            rows={3}
            placeholder="Optional. e.g. You are a concise technical writer."
            aria-label="System prompt"
            className="mt-1.5 w-full bg-bg-tertiary border border-border rounded-lg px-3 py-2 text-xs text-text-primary placeholder:text-text-muted resize-y focus:outline-none focus:border-accent-blue transition-colors"
          />
          <p className="text-[10px] text-text-muted">Applies to this conversation. Saved when you click away.</p>
        </>
      )}
    </div>
  )
}

function ChatSamplingOptions() {
  const temperature = useStore(s => s.chatTemperature)
  const maxTokens = useStore(s => s.chatMaxTokens)
  const setChatSampling = useStore(s => s.setChatSampling)

  return (
    <div className="space-y-2">
      <div>
        <div className="flex items-center justify-between mb-1">
          <label htmlFor="chat-temperature" className="text-[11px] text-text-muted uppercase tracking-wider">
            Temperature
          </label>
          <span className="text-[10px] text-text-primary">{temperature.toFixed(2)}</span>
        </div>
        <input
          id="chat-temperature"
          type="range"
          min={0}
          max={1.5}
          step={0.05}
          value={temperature}
          onChange={e => setChatSampling({ temperature: Number(e.target.value) })}
          className="w-full h-1 accent-accent-blue"
        />
      </div>
      <div>
        <div className="flex items-center justify-between mb-1">
          <label htmlFor="chat-max-tokens" className="text-[11px] text-text-muted uppercase tracking-wider">
            Max Reply Tokens
          </label>
          <span className="text-[10px] text-text-primary">{maxTokens}</span>
        </div>
        <input
          id="chat-max-tokens"
          type="range"
          min={256}
          max={8192}
          step={256}
          value={maxTokens}
          onChange={e => setChatSampling({ maxTokens: Number(e.target.value) })}
          className="w-full h-1 accent-accent-blue"
        />
      </div>
    </div>
  )
}
