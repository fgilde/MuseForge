/** Shared byte formatting — one rounding rule everywhere a size shows
 *  (LoRA cards, CivitAI detail pane, future storage views). */
export function formatBytes(bytes: number): string {
  if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(1)} GB`
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(0)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`
  // Rounded: callers pass transfer rates (floats), not just file sizes.
  return `${Math.round(bytes)} B`
}

/** Short duration for ETAs / elapsed times: "42s", "3:07", "1:02:30".
 *  Returns '' for null/negative/non-finite so call sites can render-and-forget. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return ''
  if (!Number.isFinite(seconds) || seconds < 0) return ''
  const total = Math.round(seconds)
  if (total < 60) return `${total}s`
  const s = total % 60
  const m = Math.floor(total / 60) % 60
  const h = Math.floor(total / 3600)
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  return h > 0
    ? `${h}:${mm}:${String(s).padStart(2, '0')}`
    : `${mm}:${String(s).padStart(2, '0')}`
}

/** Compact age for dense list rows: "today", "3d", "2w", "5mo", "1y".
 *  Returns '' for null/invalid input so call sites can render-and-forget. */
export function formatAge(iso: string | null | undefined): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return ''
  const days = Math.max(0, Math.floor((Date.now() - t) / 86400000))
  if (days < 1) return 'today'
  if (days < 7) return `${days}d`
  if (days < 30) return `${Math.floor(days / 7)}w`
  // Clamp at 11mo: days 360-364 would otherwise floor to "12mo" while
  // still failing the < 365 year cutoff.
  if (days < 365) return `${Math.min(11, Math.floor(days / 30))}mo`
  return `${Math.floor(days / 365)}y`
}
