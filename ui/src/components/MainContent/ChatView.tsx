import { useEffect, useRef, useState } from 'react'
import { Send, Loader2, Copy, Check, ChevronDown, ChevronRight, MessageSquareText, AlertTriangle } from 'lucide-react'
import { useStore } from '../../stores/useStore'
import { StoryView } from './StoryView'
import type { ChatMessage } from '../../api/client'

/**
 * Text mode's main area: the conversation plus its composer.
 *
 * Rendering notes
 * - No markdown library. ``` fences become <pre> blocks, everything else is
 *   `whitespace-pre-wrap`. That covers what a chat reply actually needs.
 * - Reasoning models emit `<think>…</think>`. That block is split out and
 *   rendered collapsed and dimmed, never as part of the answer (same split
 *   as DirectorChat's LlmThinkingStream / PromptInput's enhance status).
 * - While a reply is in flight, the live buffer (`chatStreamText`) renders as
 *   a provisional assistant bubble; the real message replaces it when the
 *   synchronous POST returns.
 */

/** Split a reply into its reasoning block and the answer body. */
function splitThinking(text: string): { thinking: string; stillThinking: boolean; body: string } {
  const m = text.match(/<(think|thinking)>([\s\S]*?)(<\/\1>|$)/)
  if (!m) return { thinking: '', stillThinking: false, body: text }
  return {
    thinking: m[2].trim(),
    // No closing tag captured yet → the model is still reasoning.
    stillThinking: !m[3],
    body: text.replace(m[0], '').trim(),
  }
}

type Segment = { code: true; lang: string; text: string } | { code: false; text: string }

/** Split on ``` fences. An unterminated fence (mid-stream) still renders as
 *  a code block so the layout doesn't jump when the closing fence arrives. */
function splitFences(text: string): Segment[] {
  const segments: Segment[] = []
  const re = /```([^\n]*)\n?([\s\S]*?)(?:```|$)/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (m[0] === '') break  // zero-width match guard
    if (m.index > last) segments.push({ code: false, text: text.slice(last, m.index) })
    segments.push({ code: true, lang: m[1].trim(), text: m[2] })
    last = re.lastIndex
  }
  if (last < text.length) segments.push({ code: false, text: text.slice(last) })
  return segments
}

function MessageBody({ text }: { text: string }) {
  return (
    <>
      {splitFences(text).map((seg, i) => seg.code ? (
        <div key={i} className="my-2 rounded-lg border border-border bg-bg-primary/60 overflow-hidden">
          {seg.lang && (
            <div className="px-3 py-1 text-[10px] text-text-muted border-b border-border/60">{seg.lang}</div>
          )}
          <pre className="px-3 py-2 text-xs font-mono text-text-secondary overflow-x-auto">{seg.text}</pre>
        </div>
      ) : (
        <p key={i} className="text-sm text-text-primary whitespace-pre-wrap break-words">{seg.text}</p>
      ))}
    </>
  )
}

function ThinkingBlock({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(false)
  if (!text) return null
  return (
    <div className="mb-2">
      <button
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex items-center gap-1 text-[10px] text-text-muted hover:text-text-secondary transition-colors"
      >
        {open ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
        {live ? 'Thinking…' : 'Thought process'}
      </button>
      {open && (
        <pre className="mt-1 max-h-48 overflow-y-auto rounded bg-bg-primary/50 border border-border/30 p-2 text-[11px] font-mono text-text-muted whitespace-pre-wrap leading-relaxed">
          {text}
        </pre>
      )}
    </div>
  )
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        } catch { /* clipboard blocked (insecure origin) — nothing to do */ }
      }}
      aria-label="Copy reply"
      title="Copy reply"
      className="p-1 rounded text-text-muted hover:text-text-primary hover:bg-bg-hover transition-colors"
    >
      {copied ? <Check size={12} className="text-indicator-success" /> : <Copy size={12} />}
    </button>
  )
}

function Bubble({ message }: { message: ChatMessage }) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl rounded-br-md bg-bg-tertiary border border-border px-3.5 py-2.5">
          <p className="text-sm text-text-primary whitespace-pre-wrap break-words">{message.content}</p>
        </div>
      </div>
    )
  }
  const { thinking, body } = splitThinking(message.content)
  return (
    <div className="flex justify-start">
      <div className="group max-w-[85%] rounded-2xl rounded-bl-md bg-bg-secondary border border-border px-3.5 py-2.5">
        <ThinkingBlock text={thinking} live={false} />
        <MessageBody text={body || message.content} />
        <div className="mt-1 flex justify-end opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
          <CopyButton text={body || message.content} />
        </div>
      </div>
    </div>
  )
}

/** The in-flight reply. Empty buffer means the backend is still loading the
 *  LLM — say so rather than spinning silently for minutes. */
function StreamingBubble({ text }: { text: string }) {
  const { thinking, stillThinking, body } = splitThinking(text)
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] rounded-2xl rounded-bl-md bg-bg-secondary border border-border px-3.5 py-2.5">
        {!text ? (
          <div className="flex items-center gap-2">
            <Loader2 size={13} className="animate-spin text-accent-blue" />
            <span className="text-xs text-text-secondary">
              Loading model — the first use downloads it, which can take several minutes.
            </span>
          </div>
        ) : (
          <>
            <ThinkingBlock text={thinking} live={stillThinking} />
            {body
              ? <MessageBody text={body} />
              : <span className="text-xs text-text-muted">Reasoning…</span>}
            {!stillThinking && body && <span className="animate-pulse text-accent-blue">▍</span>}
          </>
        )}
      </div>
    </div>
  )
}

export function ChatView() {
  const textSubMode = useStore(s => s.textSubMode)
  const thread = useStore(s => s.activeChatThread)
  const activeChatId = useStore(s => s.activeChatId)
  const streamingId = useStore(s => s.chatStreamingId)
  const streamText = useStore(s => s.chatStreamText)
  const chatError = useStore(s => s.chatError)
  const sendChatMessage = useStore(s => s.sendChatMessage)

  // A reply is in flight somewhere (the LLM is serialized, so the composer
  // locks globally) vs. in flight in THIS thread (only then do its tokens
  // belong on screen here).
  const busy = streamingId !== null
  const streamingHere = streamingId === activeChatId && activeChatId !== null

  const [draft, setDraft] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const endRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to the newest content — but only when the user is already
  // near the bottom, so scrolling up to re-read isn't yanked back down on
  // every streamed chunk.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160
    if (nearBottom) endRef.current?.scrollIntoView({ block: 'end' })
  }, [thread?.messages.length, streamText, streamingHere, activeChatId])

  if (textSubMode === 'story') return <StoryView />

  const messages = thread?.messages ?? []
  const canSend = !!draft.trim() && !busy

  const submit = () => {
    if (!canSend) return
    const text = draft
    setDraft('')
    sendChatMessage(text)
  }

  return (
    <main className="flex-1 flex flex-col h-full overflow-hidden">
      {/* Top bar — thread title, mirrors the media feed's header height */}
      <div className="px-4 md:px-6 py-2 md:py-3 border-b border-border flex items-center gap-2">
        <MessageSquareText size={14} className="text-accent-blue shrink-0" />
        <span className="text-sm text-text-primary truncate">{thread?.title || 'Chat'}</span>
        {messages.length > 0 && (
          <span className="text-[10px] text-text-muted shrink-0">
            {messages.length} {messages.length === 1 ? 'message' : 'messages'}
          </span>
        )}
      </div>

      {/* Conversation */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 md:px-6 py-4 space-y-3">
        {messages.length === 0 && !streamingHere && (
          <div className="flex items-center justify-center min-h-[300px] px-6">
            <div className="flex flex-col items-center gap-3 text-center max-w-sm">
              <div className="w-16 h-16 rounded-2xl bg-bg-active flex items-center justify-center text-text-muted">
                <MessageSquareText size={24} />
              </div>
              <p className="text-sm text-text-secondary">Ask the local model anything.</p>
              <p className="text-[11px] text-text-muted leading-snug">
                Conversations are stored in your workspace, so they survive a reload.
                The model loads on your first message — the very first time it also
                downloads the weights, which can take a while.
              </p>
            </div>
          </div>
        )}

        {messages.map((m, i) => <Bubble key={`${m.at}-${i}`} message={m} />)}
        {streamingHere && <StreamingBubble text={streamText} />}

        {chatError && (
          <div className="flex items-start gap-2 text-[11px] text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg px-3 py-2">
            <AlertTriangle size={12} className="mt-0.5 shrink-0" />
            <span>{chatError}</span>
          </div>
        )}

        <div ref={endRef} />
      </div>

      {/* Composer. Send stays disabled while a reply is in flight — the
          backend has no cancel endpoint for chat, so there's no honest Stop
          button to offer here. */}
      <div className="px-4 md:px-6 py-3 border-t border-border">
        <div className="flex items-end gap-2">
          <textarea
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
            rows={2}
            placeholder={busy ? 'Waiting for the reply…' : 'Send a message — Enter to send, Shift+Enter for a new line'}
            aria-label="Message"
            className="flex-1 bg-bg-tertiary border border-border rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-muted resize-none focus:outline-none focus:border-accent-blue transition-colors"
          />
          <button
            onClick={submit}
            disabled={!canSend}
            aria-label="Send message"
            className="p-2.5 rounded-lg bg-cta text-white hover:brightness-110 shadow-accent-glow transition-all disabled:opacity-30 disabled:cursor-not-allowed shrink-0"
          >
            {busy ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
          </button>
        </div>
      </div>
    </main>
  )
}
