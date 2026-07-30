import { useCallback, useEffect, useState } from 'react'
import { Check, Download, Loader2, Eye, ChevronDown, ChevronRight } from 'lucide-react'

/**
 * Text (LLM) models in Settings → System.
 *
 * Deliberately not the same shape as the generation-model list above it:
 * a text model has no per-mode visibility to toggle — one model is active
 * at a time, chosen per chat thread or Storywriter pass. What is actionable
 * here is pulling a model to disk before the first use, so a story doesn't
 * stall for minutes on a multi-gigabyte download.
 */

interface CatalogEntry {
  id: string
  label: string
  size_hint: string
  weights_gb: number
  mmproj_gb: number
  has_vision: boolean
  use_cases: string[]
  is_downloaded: boolean
  curated: boolean
}

const USE_CASE_LABELS: Record<string, string> = {
  chat: 'Chat',
  story_outline: 'Outline',
  story_prose: 'Prose',
}

export function TextModelsSection() {
  const [entries, setEntries] = useState<CatalogEntry[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [downloading, setDownloading] = useState<Record<string, string>>({})
  const [open, setOpen] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/v1/llm/catalog')
      if (!res.ok) throw new Error('Could not load the text-model catalog')
      const data = await res.json()
      setEntries(data.models ?? [])
      setActiveId(data.active_model_id ?? null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load the text-model catalog')
    }
  }, [])

  useEffect(() => { if (open) load() }, [open, load])

  // While anything downloads, poll the shared model-download feed — the
  // same one the download banner reads, so the two never disagree.
  useEffect(() => {
    if (Object.keys(downloading).length === 0) return
    let stop = false
    const tick = async () => {
      if (stop) return
      try {
        const res = await fetch('/api/v1/models/downloads/status')
        const data = await res.json()
        const next: Record<string, string> = {}
        let finished = false
        for (const [id, rec] of Object.entries<{ status: string }>(data.downloads ?? {})) {
          if (!(id in downloading)) continue
          if (rec.status === 'downloading') next[id] = 'downloading'
          else finished = true
        }
        setDownloading(next)
        if (finished) load()
      } catch { /* transient */ }
      if (!stop) setTimeout(tick, 2000)
    }
    tick()
    return () => { stop = true }
  }, [downloading, load])

  const startDownload = async (id: string) => {
    setDownloading(d => ({ ...d, [id]: 'downloading' }))
    try {
      const res = await fetch('/api/v1/llm/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_id: id }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Download failed' }))
        throw new Error(err.detail || 'Download failed')
      }
      const data = await res.json()
      if (data.status === 'completed') {
        setDownloading(d => { const n = { ...d }; delete n[id]; return n })
        load()
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Download failed')
      setDownloading(d => { const n = { ...d }; delete n[id]; return n })
    }
  }

  const visible = showAll ? entries : entries.filter(e => e.curated)
  const downloadedCount = entries.filter(e => e.is_downloaded).length

  return (
    <div>
      <button onClick={() => setOpen(v => !v)} className="mb-1.5 flex w-full items-center gap-1.5 text-left">
        {open ? <ChevronDown size={11} className="text-text-muted" /> : <ChevronRight size={11} className="text-text-muted" />}
        <span className="text-[11px] uppercase tracking-wider text-text-muted">Text models</span>
        <span className="ml-auto text-[10px] text-text-muted">
          {downloadedCount}/{entries.length || '—'} on disk
        </span>
      </button>

      {open && (
        <div className="space-y-1.5">
          <p className="text-[10px] text-text-muted">
            Used by Chat and the Storywriter. Downloading here is optional — a
            model is fetched automatically on first use, which can take several
            minutes.
          </p>
          {error && <p className="text-[10px] text-red-400">{error}</p>}

          <div className="overflow-hidden rounded-lg border border-border">
            {visible.map((m, i) => {
              const busy = m.id in downloading
              const totalGb = (m.weights_gb || 0) + (m.mmproj_gb || 0)
              return (
                <div
                  key={m.id}
                  className={`flex items-center gap-2 px-2.5 py-1.5 ${i % 2 ? '' : 'bg-bg-tertiary/40'}`}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate text-[11px] text-text-primary">{m.label}</span>
                      {m.id === activeId && (
                        <span className="shrink-0 rounded-full border border-accent-blue px-1 text-[9px] text-accent-blue">
                          active
                        </span>
                      )}
                      {m.has_vision && (
                        <span title="Understands images" className="shrink-0">
                          <Eye size={10} className="text-text-muted" />
                        </span>
                      )}
                    </div>
                    <div className="mt-0.5 flex items-center gap-1.5 text-[9px] text-text-muted">
                      <span>{totalGb > 0 ? `${totalGb.toFixed(1)} GB` : m.size_hint}</span>
                      {m.use_cases.map(uc => (
                        <span key={uc} className="rounded-full border border-border px-1">
                          {USE_CASE_LABELS[uc] ?? uc}
                        </span>
                      ))}
                    </div>
                  </div>
                  {m.is_downloaded ? (
                    <span title="On disk" className="shrink-0">
                      <Check size={12} className="text-indicator-success" />
                    </span>
                  ) : busy ? (
                    <Loader2 size={12} className="shrink-0 animate-spin text-accent-blue" />
                  ) : (
                    <button
                      onClick={() => startDownload(m.id)}
                      title={`Download ${m.label}`}
                      aria-label={`Download ${m.label}`}
                      className="shrink-0 rounded p-1 text-text-muted transition-colors hover:bg-bg-hover hover:text-text-primary"
                    >
                      <Download size={12} />
                    </button>
                  )}
                </div>
              )
            })}
          </div>

          {entries.length > visible.length && (
            <button
              onClick={() => setShowAll(true)}
              className="text-[10px] text-accent-blue hover:text-accent-blue-hover"
            >
              Show {entries.length - visible.length} more (loadable by id, not offered in the pickers)
            </button>
          )}
          {showAll && (
            <button onClick={() => setShowAll(false)} className="text-[10px] text-text-muted hover:text-text-secondary">
              Show curated only
            </button>
          )}
        </div>
      )}
    </div>
  )
}
