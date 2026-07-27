import { useEffect, useMemo, useState } from 'react'
import { Check, Loader2, AlertCircle } from 'lucide-react'
import { useStore } from '../../stores/useStore'
import { formatDuration } from '../../lib/format'
import type { CivitAIDownload } from '../../types'

const COMPLETED_VISIBLE_MS = 30_000

function timestampMs(value: unknown): number | null {
  const timestamp = Number(value)
  if (!Number.isFinite(timestamp) || timestamp <= 0) return null
  // The backend uses Unix seconds, but accepting milliseconds keeps this
  // tolerant of persisted/older records from alternate download sources.
  return timestamp < 1_000_000_000_000 ? timestamp * 1000 : timestamp
}

function displayProgress(download: CivitAIDownload): number {
  const downloaded = Number(download.bytes_downloaded)
  const total = Number(download.bytes_total)
  let progress = Number(download.progress)

  // Byte counts are the least ambiguous source. Otherwise trust the
  // backend's normalized 0..100 contract and clamp corrupt values.
  if (Number.isFinite(downloaded) && Number.isFinite(total) && total > 0) {
    progress = (Math.max(0, downloaded) / total) * 100
  }

  if (!Number.isFinite(progress)) return 0
  return Math.min(100, Math.max(0, progress))
}

/** Seconds remaining, from average speed since start. No rate field on
 *  these records, so it's derived — jumpy for the first second or two,
 *  which is why anything under 2s of history is treated as unknown. */
function etaSeconds(download: CivitAIDownload, nowMs: number): number | null {
  const startedAt = timestampMs(download.started_at)
  const downloaded = Number(download.bytes_downloaded)
  const total = Number(download.bytes_total)
  if (startedAt === null || !Number.isFinite(downloaded) || !Number.isFinite(total)) return null
  if (downloaded <= 0 || total <= downloaded) return null
  const elapsed = (nowMs - startedAt) / 1000
  if (elapsed < 2) return null
  return ((total - downloaded) / (downloaded / elapsed))
}

export function DownloadBar() {
  const downloads = useStore(s => s.civitDownloads)
  const [now, setNow] = useState<number | null>(null)

  const visible = useMemo(() => downloads.filter(download => {
    if (download.status !== 'completed') return true
    if (now === null) return false
    const completedAt = timestampMs(download.completed_at)
    return completedAt !== null && now - completedAt < COMPLETED_VISIBLE_MS
  }), [downloads, now])

  // The clock is needed for two things: expiring completed rows, and the
  // per-row ETA (which needs elapsed time since started_at).
  const hasDownloading = downloads.some(download => download.status === 'downloading')
  const needsClock = hasDownloading || downloads.some(download => download.status === 'completed')
  useEffect(() => {
    if (!needsClock) return
    // Initialize outside the effect body so react-hooks/set-state-in-effect
    // remains satisfied while the first render safely hides historical rows.
    const timer = window.setTimeout(() => setNow(Date.now()), 0)
    return () => window.clearTimeout(timer)
  }, [downloads, needsClock])

  const hasVisibleCompletion = visible.some(download => download.status === 'completed')
  useEffect(() => {
    if (!hasVisibleCompletion && !hasDownloading) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [hasVisibleCompletion, hasDownloading])

  if (visible.length === 0) return null

  return (
    <div className="border-t border-border bg-bg-tertiary px-4 py-2 space-y-1.5 shrink-0">
      {visible.map(download => {
        const progress = displayProgress(download)
        const eta = now === null ? '' : formatDuration(etaSeconds(download, now))
        return (
          <div key={download.id} className="flex items-center gap-2">
            {download.status === 'downloading' ? (
              <Loader2 size={12} className="animate-spin text-accent-blue shrink-0" />
            ) : download.status === 'completed' ? (
              <Check size={12} className="text-accent-green shrink-0" />
            ) : (
              <AlertCircle size={12} className="text-red-400 shrink-0" />
            )}
            <span className="text-[11px] text-text-secondary truncate flex-1">{download.filename}</span>
            {download.status === 'downloading' && (
              <>
                <div className="w-24 bg-bg-active rounded-full h-1 overflow-hidden shrink-0">
                  <div className="h-full bg-accent-blue rounded-full transition-all" style={{ width: `${progress}%` }} />
                </div>
                <span className="text-[10px] text-text-muted w-8 text-right shrink-0 tabular-nums">{Math.round(progress)}%</span>
                {eta && (
                  <span className="text-[10px] text-text-muted w-12 text-right shrink-0 tabular-nums">{eta} left</span>
                )}
              </>
            )}
            {download.status === 'failed' && (
              <span className="text-[10px] text-red-400 truncate">{download.error || 'Download failed'}</span>
            )}
          </div>
        )
      })}
    </div>
  )
}
