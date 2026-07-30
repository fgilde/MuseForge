import { useEffect, useState } from 'react'
import { Activity, Square, X, Loader2 } from 'lucide-react'
import { fetchActivity, stopActivityItem, stopAllActivity, type ActivityItem } from '../api/client'
import { formatDuration } from '../lib/format'

/**
 * ActivityPanel — one list of everything the backend is currently working
 * on (generation jobs, Director pipelines, Storywriter runs, audiobook
 * renders), each with the call that stops it.
 *
 * Backed by /api/v1/activity, which already hands back a ready-made
 * `cancel` path per item, so this component never has to know the
 * per-feature cancel conventions.
 *
 * The trigger is a fixed button bottom-LEFT: bottom-right belongs to
 * DownloadStatusBanner. It only appears while something is running (or
 * while the panel is pinned open) — an activity button with nothing
 * behind it is noise.
 *
 * Polling is 2s while the panel is open or something is running, and 8s
 * otherwise so the badge still shows up on its own.
 */
export function ActivityPanel() {
  const [items, setItems] = useState<ActivityItem[]>([])
  /** Clock taken with each poll — reading Date.now() during render is
   *  impure, and the poll already re-renders often enough for a runtime. */
  const [now, setNow] = useState(0)
  const [open, setOpen] = useState(false)
  const [stopping, setStopping] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Derived, not stored: no effect writes this, so no set-state-in-effect.
  const fast = open || items.length > 0

  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        const { activity } = await fetchActivity()
        if (!cancelled) { setItems(activity ?? []); setNow(Date.now() / 1000) }
      } catch {
        // Older backend or a transient failure — an empty list is the
        // honest reading, and the next tick recovers.
        if (!cancelled) setItems([])
      }
    }
    tick()
    const interval = setInterval(tick, fast ? 2000 : 8000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [fast])

  const stopOne = async (item: ActivityItem) => {
    setStopping(item.id)
    setError(null)
    try {
      await stopActivityItem(item.cancel)
      setItems(list => list.filter(i => i.id !== item.id))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not stop that')
    } finally {
      setStopping(null)
    }
  }

  const stopEverything = async () => {
    setStopping('*')
    setError(null)
    try {
      const { results } = await stopAllActivity()
      const failed = results.filter(r => !r.stopped)
      if (failed.length) setError(`${failed.length} could not be stopped`)
      else setItems([])
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not stop everything')
    } finally {
      setStopping(null)
    }
  }

  if (items.length === 0 && !open) return null

  return (
    <div className="fixed bottom-4 left-4 z-40 flex flex-col items-start gap-2">
      {open && (
        <div className="glass-panel banner-in w-[calc(100vw-2rem)] max-w-sm rounded-2xl shadow-2xl">
          <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
            <div className="flex items-center gap-2">
              <Activity size={14} className="text-accent-blue" />
              <span className="text-sm font-semibold text-text-primary">
                Activity{items.length > 0 ? ` (${items.length})` : ''}
              </span>
            </div>
            <div className="flex items-center gap-1">
              {items.length > 0 && (
                <button
                  onClick={stopEverything}
                  disabled={stopping === '*'}
                  className="rounded-lg border border-border px-2 py-1 text-[10px] text-text-secondary transition-colors hover:border-border-light hover:text-text-primary disabled:opacity-40"
                >
                  Stop all
                </button>
              )}
              <button
                onClick={() => setOpen(false)}
                aria-label="Close activity panel"
                className="rounded-lg p-1.5 text-text-secondary transition-colors hover:bg-bg-hover hover:text-text-primary"
              >
                <X size={14} />
              </button>
            </div>
          </div>

          <div className="max-h-[60vh] overflow-y-auto px-3 py-2">
            {items.length === 0 ? (
              <p className="px-1 py-3 text-[11px] text-text-muted">Nothing is running.</p>
            ) : (
              <div className="space-y-1.5">
                {items.map(item => (
                  <Row
                    key={`${item.kind}-${item.id}`}
                    item={item}
                    now={now}
                    busy={stopping === item.id}
                    onStop={() => stopOne(item)}
                  />
                ))}
              </div>
            )}
            {error && <p className="mt-2 px-1 text-[10px] text-red-400">{error}</p>}
          </div>
        </div>
      )}

      <button
        onClick={() => setOpen(v => !v)}
        title="Running actions"
        aria-label="Running actions"
        className="glass-panel relative flex items-center gap-2 rounded-full px-3 py-2 text-xs text-text-secondary shadow-2xl transition-colors hover:text-text-primary"
      >
        {items.length > 0
          ? <Loader2 size={14} className="animate-spin text-accent-blue" />
          : <Activity size={14} />}
        <span>Activity</span>
        {items.length > 0 && (
          <span className="rounded-full bg-accent-blue px-1.5 text-[10px] font-semibold text-white tabular-nums">
            {items.length}
          </span>
        )}
      </button>
    </div>
  )
}

const KIND_LABELS: Record<string, string> = {
  job: 'Generation',
  director: 'Director',
  story: 'Story',
  audiobook: 'Audiobook',
}

function Row({ item, now, busy, onStop }: {
  item: ActivityItem
  /** Epoch seconds sampled at the last poll. */
  now: number
  busy: boolean
  onStop: () => void
}) {
  // Steps beat the 0..1 progress float where the backend reports them —
  // pipelines and stories only ever fill in steps.
  const pct = item.total_steps > 0
    ? Math.min(100, (item.step / item.total_steps) * 100)
    : Math.min(100, (item.progress || 0) * 100)
  // Elapsed is recomputed on each poll; no separate ticker.
  const elapsed = item.started_at && now ? formatDuration(now - item.started_at) : ''

  return (
    <div className="rounded-xl border border-border bg-bg-tertiary/40 px-2.5 py-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-[11px] font-medium text-text-primary">{item.label}</div>
          <div className="text-[9px] uppercase tracking-wider text-text-muted">
            {KIND_LABELS[item.kind] || item.kind} · {item.status}
            {elapsed && ` · ${elapsed}`}
          </div>
        </div>
        <button
          onClick={onStop}
          disabled={busy}
          title="Stop"
          aria-label={`Stop ${item.label}`}
          className="shrink-0 rounded-md p-1 text-text-secondary transition-colors hover:bg-bg-hover hover:text-text-primary disabled:opacity-30"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Square size={12} />}
        </button>
      </div>

      {item.message && (
        <p className="mt-1 line-clamp-2 text-[10px] text-text-secondary">{item.message}</p>
      )}

      <div className="mt-1.5 flex items-center gap-2">
        <div className="h-1 flex-1 overflow-hidden rounded-full bg-bg-tertiary">
          <div
            className={`h-full bg-accent-blue ${pct > 0 ? 'transition-all' : 'w-1/3 progress-indeterminate'}`}
            style={pct > 0 ? { width: `${pct}%` } : undefined}
          />
        </div>
        {item.total_steps > 0 && (
          <span className="shrink-0 text-[9px] tabular-nums text-text-muted">
            {item.step}/{item.total_steps}
          </span>
        )}
      </div>
    </div>
  )
}
