import { useEffect, useState } from 'react'
import { AlertTriangle, X } from 'lucide-react'
import { fetchPreflight, type PreflightCheck } from '../api/client'

/**
 * PreflightBanner — one-time environment sanity check shown at the top
 * of the app on first load. Surfaces the three failures that otherwise
 * only appear as a cryptic traceback mid-generation: ffmpeg missing, no
 * CUDA GPU, and low disk on the output drive.
 *
 * Renders nothing when everything is fine. Dismissible; stays dismissed
 * for the session (sessionStorage) so it doesn't nag after the user has
 * acknowledged it, but returns next launch if the problem persists.
 */
export function PreflightBanner() {
  const [checks, setChecks] = useState<PreflightCheck[]>([])
  const [dismissed, setDismissed] = useState(
    () => sessionStorage.getItem('museforge_preflight_dismissed') === '1'
  )

  useEffect(() => {
    let cancelled = false
    fetchPreflight()
      .then(r => { if (!cancelled) setChecks(r.checks || []) })
      .catch(() => { /* older backend / transient — say nothing */ })
    return () => { cancelled = true }
  }, [])

  if (dismissed || checks.length === 0) return null

  const hasError = checks.some(c => c.level === 'error')

  return (
    <div className="fixed top-3 inset-x-0 z-50 px-4 flex justify-center pointer-events-none">
      <div className={`glass-panel banner-in pointer-events-auto max-w-2xl w-full rounded-2xl shadow-2xl border-l-4 px-4 py-3 flex items-start gap-2.5 ${
        hasError ? 'border-l-red-500/60' : 'border-l-amber-500/60'
      }`}>
        <AlertTriangle
          size={16}
          className={`shrink-0 mt-0.5 ${hasError ? 'text-red-400' : 'text-indicator-warning'}`}
        />
        <div className="flex-1 min-w-0 space-y-1">
          <div className="text-sm font-semibold text-text-primary">
            {hasError ? 'Environment check failed' : 'Environment warning'}
          </div>
          {checks.map(c => (
            <div key={c.id} className="text-xs leading-snug text-text-secondary">
              {c.message}
            </div>
          ))}
        </div>
        <button
          onClick={() => {
            sessionStorage.setItem('museforge_preflight_dismissed', '1')
            setDismissed(true)
          }}
          className="shrink-0 p-0.5 rounded text-text-muted hover:text-text-primary transition-colors"
          aria-label="Dismiss"
        >
          <X size={16} />
        </button>
      </div>
    </div>
  )
}
