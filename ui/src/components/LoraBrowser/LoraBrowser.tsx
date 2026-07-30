import { useState, useEffect, useRef, useCallback } from 'react'
import { X, Search, Loader2, BookOpen, HardDrive, Tag, Link2, ArrowUpCircle, RefreshCw, KeyRound, ExternalLink, Boxes, Trash2 } from 'lucide-react'
import { useStore } from '../../stores/useStore'
import { fetchCivitAIModelFilters, startLoraScan, fetchLoraScanStatus, fetchInstalledLoras, importHuggingFaceLora, checkLoraUpdates, deleteLoraFile, fetchLoraDirectoryModels, relocateLora } from '../../api/client'
import { formatBytes } from '../../lib/format'
import type { CivitAIModelFilter, InstalledLora } from '../../api/client'
import { ModelCard } from './ModelCard'
import { ModelDetail } from './ModelDetail'
import { DownloadBar } from './DownloadBar'
import { InstalledCheckpoints } from './InstalledCheckpoints'

const SORT_OPTIONS = [
  { value: 'Highest Rated', label: 'Highest Rated' },
  { value: 'Most Downloaded', label: 'Most Downloaded' },
  { value: 'Newest', label: 'Newest' },
  { value: 'Most Liked', label: 'Most Liked' },
]

const PERIOD_OPTIONS = [
  { value: 'AllTime', label: 'All Time' },
  { value: 'Year', label: 'Year' },
  { value: 'Month', label: 'Month' },
  { value: 'Week', label: 'Week' },
  { value: 'Day', label: 'Day' },
]

export function LoraBrowser() {
  const open = useStore(s => s.loraBrowserOpen)
  const setOpen = useStore(s => s.setLoraBrowserOpen)
  const currentModel = useStore(s => s.params.model_type)
  const activatedLoras = useStore(s => s.params.activated_loras)
  // Not named useSomething: the lint rule reads that as a hook call.
  const activateLora = useStore(s => s.activateInstalledLora)
  const activateWithModel = useStore(s => s.useLoraWithModel)
  const allModels = useStore(s => s.models)
  // The LoRA whose model still has to be chosen. Not a boolean: the dialog
  // needs to know which file it is picking a model for.
  const [pickModelFor, setPickModelFor] = useState<InstalledLora | null>(null)
  const results = useStore(s => s.civitSearchResults)
  const cursor = useStore(s => s.civitSearchCursor)
  const loading = useStore(s => s.civitSearchLoading)
  const selectedModel = useStore(s => s.civitSelectedModel)
  const search = useStore(s => s.searchCivitAI)
  const selectModel = useStore(s => s.selectCivitAIModel)
  const clearSelection = useStore(s => s.clearCivitSelection)
  const setDefaultDir = useStore(s => s.setLoraBrowserDefaultDir)
  const searchError = useStore(s => s.civitSearchError)
  const pollDownloads = useStore(s => s.pollCivitAIDownloads)
  // API-key onboarding state. Without a CivitAI key, downloads of any
  // restricted/mature model produce error-page payloads — we now reject
  // those at download time, but a passive banner here saves the user a
  // round trip into the docs to figure out why.
  const civitaiKeySet = !!useStore(s => s.servicesConfig?.civitai_api_key_set)
  const setSettingsOpen = useStore(s => s.setSettingsOpen)
  const setSettingsTab = useStore(s => s.setSettingsTab)
  const [apiKeyBannerDismissed, setApiKeyBannerDismissed] = useState(false)
  const goToCivitaiKeySettings = useCallback(() => {
    setOpen(false)
    setSettingsTab('integrations')
    setSettingsOpen(true)
  }, [setOpen, setSettingsTab, setSettingsOpen])

  const [query, setQuery] = useState('')
  const [sort, setSort] = useState('Highest Rated')
  const [period, setPeriod] = useState('AllTime')
  const [selectedFilter, setSelectedFilter] = useState('')
  // LoRA (adapter) vs Checkpoint (full model) browse mode. Checkpoint mode
  // searches CivitAI Checkpoints and imports them as finetune model variants.
  const [browseKind, setBrowseKind] = useState<'lora' | 'checkpoint'>('lora')
  // Sticky across sessions (localStorage) — the master nsfw_mode gate below
  // still applies, so a persisted "on" is inert until Mature Mode is enabled.
  const [nsfw, setNsfw] = useState(() => {
    try { return localStorage.getItem('museforge_civitai_nsfw') === '1' } catch { return false }
  })
  const setNsfwSticky = (v: boolean) => {
    setNsfw(v)
    try { localStorage.setItem('museforge_civitai_nsfw', v ? '1' : '0') } catch { /* private mode */ }
  }
  // Master gate from Settings → Services. NSFW filter UI + data flow
  // is only honored when the user has enabled NSFW mode (which itself
  // requires the disclaimer acknowledgement). Without this gate, the
  // checkbox would tease users who haven't opted in, and the CivitAI
  // API would return NSFW results to a user who shouldn't see them.
  const nsfwEnabled = !!useStore(s => s.servicesConfig?.nsfw_mode)
  // Effective NSFW value: only true when both the master gate AND the
  // local checkbox are on. Used everywhere instead of raw `nsfw` so
  // disabling NSFW mode in services immediately suppresses NSFW
  // content even if the local checkbox happens to still be checked.
  const effectiveNsfw = nsfwEnabled && nsfw
  const [modelFilters, setModelFilters] = useState<CivitAIModelFilter[]>([])
  const [scanning, setScanning] = useState(false)
  const [scanProgress, setScanProgress] = useState('')
  const [showInstalled, setShowInstalled] = useState(false)
  // Checkpoint-mode equivalent of "My LoRAs" — shows imported checkpoints.
  const [showCkptInstalled, setShowCkptInstalled] = useState(false)
  const [installedLoras, setInstalledLoras] = useState<InstalledLora[]>([])
  // directory -> model types that load from it. Answers "then what CAN use
  // this?", which is the actual question when a download landed elsewhere.
  const [dirModels, setDirModels] = useState<Record<string, string[]>>({})
  const [dirBases, setDirBases] = useState<Record<string, string[]>>({})
  const [baseHome, setBaseHome] = useState<Record<string, string>>({})
  const [moving, setMoving] = useState<string | null>(null)
  const [used, setUsed] = useState<string | null>(null)
  const [installedLoading, setInstalledLoading] = useState(false)
  const [showUrlImport, setShowUrlImport] = useState(false)
  const [importUrl, setImportUrl] = useState('')
  const [importing, setImporting] = useState(false)
  const [importStatus, setImportStatus] = useState('')
  // Filter for the My LoRAs view: when true, hide everything except
  // LoRAs whose update_status is 'available'. Disabled when nothing is
  // updatable so the checkbox doesn't dangle uselessly.
  const [updatableOnly, setUpdatableOnly] = useState(false)
  // True while a manual /check-updates call is in flight. Used to
  // gate double-clicks and swap the icon for a spinner.
  const [checkingUpdates, setCheckingUpdates] = useState(false)

  // Force a CivitAI version check then refetch the installed-LoRAs
  // list so each card's update_status reflects the new manifest.
  const handleCheckUpdates = useCallback(async () => {
    if (checkingUpdates) return
    setCheckingUpdates(true)
    try {
      await checkLoraUpdates(true) // force=true: bypass 24h staleness window
      const r = await fetchInstalledLoras()
      setInstalledLoras(r.loras)
      fetchLoraDirectoryModels()
        .then(map => {
          setDirModels(map.models); setDirBases(map.bases); setBaseHome(map.baseHome)
        })
        .catch(() => { setDirModels({}); setDirBases({}) })
    } catch (e) {
      console.error('LoRA update check failed:', e)
    } finally {
      setCheckingUpdates(false)
    }
  }, [checkingUpdates])

  // Count of LoRAs with an update available — surfaced as a badge on
  // both the My LoRAs button (visible from any view) and the Check
  // button (in case the user is already on My LoRAs view).
  const updatableCount = installedLoras.filter(l => l.update_status === 'available').length

  // Installed-view sort. "released" falls back to downloaded date so
  // files whose sidecars predate publishedAt capture still order sanely.
  const [installedSort, setInstalledSort] = useState<'name' | 'downloaded' | 'released' | 'largest'>('name')

  // Per-card delete with two-step confirm, keyed by directory/filename.
  const [confirmDeleteKey, setConfirmDeleteKey] = useState<string | null>(null)
  const [deletingKey, setDeletingKey] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const handleDeleteLora = useCallback(async (directory: string, filename: string, e: React.MouseEvent) => {
    e.stopPropagation()
    const key = `${directory}/${filename}`
    if (confirmDeleteKey !== key) {
      setConfirmDeleteKey(key)
      setTimeout(() => setConfirmDeleteKey(c => (c === key ? null : c)), 4000)
      return
    }
    setConfirmDeleteKey(null)
    setDeletingKey(key)
    setDeleteError(null)
    try {
      await deleteLoraFile(directory, filename)
      // The endpoint response confirms exactly what was removed — drop the
      // row locally instead of re-running the server's full multi-root
      // sidecar rescan for every single delete.
      setInstalledLoras(prev => prev.filter(l => !(l.directory === directory && l.filename === filename)))
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : String(err))
      setTimeout(() => setDeleteError(null), 8000)
    } finally {
      setDeletingKey(null)
    }
  }, [confirmDeleteKey])

  const sentinelRef = useRef<HTMLDivElement>(null)
  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout>>(undefined)

  // Get the active filter object
  const activeFilter = modelFilters.find(f => f.label === selectedFilter)

  /** Can the selected model actually load this file? The directory says which
   *  models look there; the LoRA's own base_model says whether it belongs in
   *  that directory at all. Both have to hold. */
  const loraVerdict = (lora: InstalledLora) => {
    if (lora.directory === '.' || !lora.directory) {
      return {
        usable: false,
        reason: 'Sits directly in loras/ rather than an architecture folder, '
                + 'so no model looks for it. Move it into the folder for its base model.',
      }
    }
    const owners = dirModels[lora.directory]
    const expected = dirBases[lora.directory]
    const wrongBase = Boolean(
      lora.base_model && expected?.length && !expected.includes(lora.base_model),
    )
    if (wrongBase) {
      const home = baseHome[lora.base_model as string]
      if (home) {
        // Movable: the base HAS a folder here, the file is just in the wrong
        // one — which is what a download does when another model was selected.
        return {
          usable: false,
          moveTo: home,
          offer: dirModels[home] || [],
          reason: `Built for ${lora.base_model} — belongs in loras/${home}, not `
                  + `loras/${lora.directory}. Moving it there makes it usable.`,
        }
      }
      return {
        usable: false,
        noHome: true,
        reason: `Built for ${lora.base_model}, and no model here uses that base. `
                + 'This file cannot be used in MuseForge — it is safe to delete.',
      }
    }
    if (owners && !owners.includes(currentModel)) {
      return {
        usable: false,
        // Offerable: the selected model cannot load it, but others can — so
        // this is a model choice, not a dead end.
        offer: owners,
        reason: owners.length
          ? `Needs one of the ${lora.directory} models — pick one`
          : `In loras/${lora.directory}, which no installed model loads from.`,
      }
    }
    return { usable: true, reason: 'Activate it for the selected model and close' }
  }

  /** CivitAI model id -> the local copy, for marking search results. */
  const ownedByCivitId = new Map<number, InstalledLora>()
  for (const lora of installedLoras) {
    if (lora.civitai_model_id) ownedByCivitId.set(lora.civitai_model_id, lora)
  }

  // Not named useSomething: the rules-of-hooks lint reads that as a hook call.
  const applyOwnedLora = async (lora: InstalledLora, key: string) => {
    if (await activateLora(lora.filename, lora.trained_words)) {
      setUsed(key)
      setTimeout(() => setOpen(false), 450)
    }
  }

  // Load model filters from backend
  useEffect(() => {
    if (open && modelFilters.length === 0) {
      fetchCivitAIModelFilters().then(r => setModelFilters(r.filters)).catch(() => {})
    }
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  // Adopt any server-side download, including imports started before this
  // browser session. The store action is singleton-safe across all callers.
  useEffect(() => {
    if (open) pollDownloads()
  }, [open, pollDownloads])

  // Load what is installed as soon as the browser opens, not only when its
  // own tab is picked: the search results mark the ones already downloaded.
  // And open ON that view when there is something there — "Use now" lives on
  // those cards, and starting in a CivitAI search hid it behind a toggle.
  useEffect(() => {
    if (!open) return
    fetchInstalledLoras().then(r => {
      setInstalledLoras(r.loras)
      if (r.loras.length > 0 && browseKind === 'lora') setShowInstalled(true)
    }).catch(() => {})
    fetchLoraDirectoryModels()
      .then(map => { setDirModels(map.models); setDirBases(map.bases) })
      .catch(() => {})
  }, [open])

  // Run initial search when opened
  useEffect(() => {
    if (open && results.length === 0) {
      doSearch()
    }
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  const doSearch = useCallback((append = false) => {
    const params: Record<string, unknown> = {
      sort, period,
      // Send the effective NSFW value (false when master gate is off,
      // even if local checkbox is checked from a prior session).
      nsfw: effectiveNsfw,
      types: browseKind === 'checkpoint' ? 'Checkpoint' : 'LORA',
      limit: 20,
    }
    // Build query: user search + filter's search_query combined
    const parts: string[] = []
    if (query.trim()) parts.push(query.trim())
    if (activeFilter?.search_query) parts.push(activeFilter.search_query)
    if (parts.length > 0) params.query = parts.join(' ')
    // Set CivitAI base model filter (skip when search_query is set — query is specific enough)
    if (activeFilter?.civitai_base && !activeFilter?.search_query) params.baseModels = activeFilter.civitai_base
    if (append && cursor) params.cursor = cursor
    search(params, append)
  }, [query, sort, period, activeFilter, effectiveNsfw, cursor, search, browseKind])

  // Debounced search on filter changes — also re-runs when the master
  // NSFW gate toggles (nsfwEnabled), so disabling NSFW in services
  // refreshes the result set immediately instead of leaving stale
  // NSFW thumbnails on screen.
  useEffect(() => {
    if (!open) return
    clearTimeout(searchTimeoutRef.current)
    searchTimeoutRef.current = setTimeout(() => doSearch(), 300)
    return () => clearTimeout(searchTimeoutRef.current)
  }, [query, sort, period, selectedFilter, effectiveNsfw, nsfwEnabled, browseKind]) // eslint-disable-line react-hooks/exhaustive-deps

  // Infinite scroll sentinel
  useEffect(() => {
    if (!open || !cursor || loading) return
    const el = sentinelRef.current
    if (!el) return
    const observer = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) doSearch(true)
    }, { threshold: 0.1 })
    observer.observe(el)
    return () => observer.disconnect()
  }, [open, cursor, loading, doSearch])

  const handleImport = useCallback(async () => {
    if (!importUrl.trim() || importing) return
    setImporting(true)
    const trimmed = importUrl.trim()
    const isCivitai = /civitai\.com/i.test(trimmed)
    setImportStatus(isCivitai ? 'Resolving CivitAI model…' : 'Parsing HuggingFace repo…')
    try {
      // Backend endpoint dispatches by URL type — both HuggingFace and
      // CivitAI URLs route through the same /huggingface/import-lora
      // endpoint (historical name; now handles both).
      const result = await importHuggingFaceLora(trimmed)
      setImportStatus(`Downloading ${result.filename} → ${result.target_dir}/ (base: ${result.base_model || 'auto-detected'})`)
      setImportUrl('')
      pollDownloads()
      // The download runs in background on the server — it'll appear in the download bar
      setTimeout(() => setImportStatus(''), 10000)
    } catch (e) {
      setImportStatus(`Error: ${e instanceof Error ? e.message : 'Import failed'}`)
      setTimeout(() => setImportStatus(''), 8000)
    } finally {
      setImporting(false)
    }
  }, [importUrl, importing, pollDownloads])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-[60] flex flex-col bg-bg-primary">
      {/* Top bar */}
      <div className="px-4 py-3 border-b border-border flex items-center gap-3 shrink-0">
        <h1 className="text-sm font-semibold text-text-primary shrink-0">Model Browser</h1>

        {/* LoRA (adapter) vs Checkpoint (full model) mode */}
        <div className="flex items-center rounded-lg border border-border overflow-hidden shrink-0 text-xs">
          {(['lora', 'checkpoint'] as const).map(k => (
            <button
              key={k}
              onClick={() => {
                if (k === browseKind) return
                setBrowseKind(k)
                clearSelection()
                setShowInstalled(false)
                setShowCkptInstalled(false)
              }}
              className={`px-2.5 py-1.5 transition-colors ${
                browseKind === k
                  ? 'bg-accent-blue text-white'
                  : 'bg-bg-tertiary text-text-secondary hover:text-text-primary'
              }`}
            >
              {k === 'lora' ? 'LoRAs' : 'Checkpoints'}
            </button>
          ))}
        </div>

        {/* Scan & Generate Guides buttons (LoRA-only) */}
        {!selectedModel && !scanning && browseKind === 'lora' && (
          <div className="flex items-center gap-1 shrink-0">
            <button
              onClick={async () => {
                setScanning(true)
                setScanProgress('Scanning new LoRAs...')
                try {
                  const { scan_id, total } = await startLoraScan()
                  if (total === 0) {
                    setScanProgress('All LoRAs already have guides')
                    setTimeout(() => { setScanning(false); setScanProgress('') }, 3000)
                    return
                  }
                  const poll = async () => {
                    const status = await fetchLoraScanStatus(scan_id)
                    setScanProgress(`${status.message} (${status.current}/${status.total})`)
                    if (status.status === 'running') setTimeout(poll, 2000)
                    else { setScanProgress(`Done: ${status.results.length} processed`); setTimeout(() => { setScanning(false); setScanProgress('') }, 5000) }
                  }
                  poll()
                } catch (e) {
                  setScanProgress(`Error: ${e instanceof Error ? e.message : 'failed'}`)
                  setTimeout(() => { setScanning(false); setScanProgress('') }, 5000)
                }
              }}
              className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs bg-bg-tertiary border border-border rounded-lg hover:border-accent-blue text-text-secondary hover:text-accent-blue transition-colors shrink-0"
              title="Scan new LoRAs without guides"
            >
              <BookOpen size={12} />
              <span className="hidden sm:inline">Generate Guides</span>
            </button>
            <button
              onClick={async () => {
                setScanning(true)
                setScanProgress('Regenerating all guides...')
                try {
                  const { scan_id, total } = await startLoraScan({ force: true })
                  if (total === 0) {
                    setScanProgress('No LoRAs found')
                    setTimeout(() => { setScanning(false); setScanProgress('') }, 3000)
                    return
                  }
                  const poll = async () => {
                    const status = await fetchLoraScanStatus(scan_id)
                    setScanProgress(`${status.message} (${status.current}/${status.total})`)
                    if (status.status === 'running') setTimeout(poll, 2000)
                    else { setScanProgress(`Done: ${status.results.length} regenerated`); setTimeout(() => { setScanning(false); setScanProgress('') }, 5000) }
                  }
                  poll()
                } catch (e) {
                  setScanProgress(`Error: ${e instanceof Error ? e.message : 'failed'}`)
                  setTimeout(() => { setScanning(false); setScanProgress('') }, 5000)
                }
              }}
              className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs bg-bg-tertiary border border-border rounded-lg hover:border-indicator-warning text-text-secondary hover:text-indicator-warning transition-colors shrink-0"
              title="Regenerate ALL guides (overwrites existing)"
            >
              <span className="hidden sm:inline">Regenerate All</span>
              <span className="sm:hidden">Regen</span>
            </button>
            <button
              onClick={() => setShowUrlImport(!showUrlImport)}
              className={`flex items-center gap-1.5 px-2.5 py-1.5 text-xs bg-bg-tertiary border rounded-lg transition-colors shrink-0 ${
                showUrlImport ? 'border-accent-blue text-accent-blue' : 'border-border text-text-secondary hover:border-accent-blue hover:text-accent-blue'
              }`}
              title="Import LoRA from HuggingFace or CivitAI URL"
            >
              <Link2 size={12} />
              <span className="hidden sm:inline">Import URL</span>
            </button>
          </div>
        )}
        {scanning && (
          <div className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-accent-blue shrink-0">
            <Loader2 size={12} className="animate-spin" />
            <span>{scanProgress}</span>
          </div>
        )}

        {/* URL Import panel */}
        {showUrlImport && (
          <div className="w-full bg-bg-tertiary border border-border rounded-lg p-3 space-y-2">
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={importUrl}
                onChange={e => setImportUrl(e.target.value)}
                placeholder="HuggingFace repo or CivitAI model URL"
                className="flex-1 bg-bg-secondary border border-border rounded-lg px-3 py-1.5 text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent-blue"
                onKeyDown={e => {
                  if (e.key === 'Enter' && importUrl.trim() && !importing) {
                    e.preventDefault()
                    handleImport()
                  }
                }}
              />
              <button
                onClick={handleImport}
                disabled={!importUrl.trim() || importing}
                className="px-3 py-1.5 text-xs bg-accent-blue text-white rounded-lg hover:bg-accent-blue/80 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
              >
                {importing ? <Loader2 size={12} className="animate-spin" /> : 'Import'}
              </button>
            </div>
            <p className="text-[9px] text-text-muted">
              Paste a HuggingFace model URL. Downloads the LoRA, saves metadata, example media, and generates a usage guide.
            </p>
            {importStatus && (
              <div className={`text-[10px] ${importStatus.startsWith('Error') ? 'text-red-400' : 'text-accent-blue'}`}>
                {importStatus}
              </div>
            )}
          </div>
        )}

        {/* Search input */}
        {!selectedModel && (
          <div className="flex-1 flex items-center gap-2 max-w-2xl">
            <div className="flex-1 relative">
              <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-muted" />
              <input
                type="text"
                value={query}
                onChange={e => setQuery(e.target.value)}
                placeholder="Search CivitAI..."
                className="w-full bg-bg-tertiary border border-border rounded-lg pl-8 pr-3 py-1.5 text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent-blue"
                autoFocus
              />
            </div>
          </div>
        )}

        <div className="ml-auto shrink-0">
          <button
            onClick={() => setOpen(false)}
            className="p-1.5 rounded-lg hover:bg-bg-hover text-text-secondary hover:text-text-primary transition-colors"
          >
            <X size={18} />
          </button>
        </div>
      </div>

      {/* CivitAI API key onboarding banner. Renders when no key is
          configured AND the user hasn't dismissed it for this session.
          Most NSFW / restricted / "early access" models on CivitAI
          require an API key; without one, the CDN returns auth-error
          pages that we now catch at download time, but it's better to
          warn upfront so the user doesn't have to discover it through
          a failed download. */}
      {!civitaiKeySet && !apiKeyBannerDismissed && (
        <div className="px-4 py-2.5 bg-amber-500/10 border-b border-amber-500/30 flex items-start gap-2.5 shrink-0">
          <KeyRound size={14} className="text-indicator-warning shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0 text-xs text-text-primary leading-relaxed">
            <span className="font-semibold">No CivitAI API key configured.</span>{' '}
            Most NSFW, restricted, and early-access LoRAs require a key to
            download — without one, those downloads will fail with an error.
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
              <a
                href="https://civitai.com/user/account"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-indicator-warning underline decoration-indicator-warning/40 hover:decoration-indicator-warning hover:text-indicator-warning/80 transition-colors"
                title="Opens CivitAI's account page in a new tab — scroll to 'API Keys'"
              >
                <ExternalLink size={10} />
                Get a key from civitai.com
              </a>
              <span className="text-text-secondary">→</span>
              <button
                onClick={goToCivitaiKeySettings}
                className="inline-flex items-center gap-1 text-indicator-warning underline decoration-indicator-warning/40 hover:decoration-indicator-warning hover:text-indicator-warning/80 transition-colors"
                title="Opens Settings → Services where you can paste your key"
              >
                <KeyRound size={10} />
                Paste it in Settings → Services
              </button>
            </div>
          </div>
          <button
            onClick={() => setApiKeyBannerDismissed(true)}
            className="p-1 rounded hover:bg-amber-500/20 text-indicator-warning hover:text-indicator-warning/80 shrink-0 transition-colors"
            title="Dismiss for this session"
            aria-label="Dismiss"
          >
            <X size={12} />
          </button>
        </div>
      )}

      {/* Filter row (only in grid view) */}
      {!selectedModel && (
        <div className="px-4 py-2 border-b border-border flex items-center gap-2 overflow-x-auto shrink-0">
          <select
            value={sort}
            onChange={e => setSort(e.target.value)}
            className="bg-bg-tertiary border border-border rounded-lg px-2 py-1 text-xs text-text-primary focus:outline-none focus:border-accent-blue"
          >
            {SORT_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>

          <select
            value={period}
            onChange={e => setPeriod(e.target.value)}
            className="bg-bg-tertiary border border-border rounded-lg px-2 py-1 text-xs text-text-primary focus:outline-none focus:border-accent-blue"
          >
            {PERIOD_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>

          <select
            value={selectedFilter}
            onChange={e => {
              setSelectedFilter(e.target.value)
              const f = modelFilters.find(mf => mf.label === e.target.value)
              setDefaultDir(f?.default_dir || null)
            }}
            className="bg-bg-tertiary border border-border rounded-lg px-2 py-1 text-xs text-text-primary focus:outline-none focus:border-accent-blue"
          >
            <option value="">All Models</option>
            {modelFilters.map(f => <option key={f.label} value={f.label}>{f.label}</option>)}
          </select>

          {/* NSFW filter checkbox is only rendered when the user has
              enabled NSFW mode in Settings → Services. Without that
              opt-in, the entire affordance is hidden — both for the
              CivitAI search and the installed-LoRA view. */}
          {nsfwEnabled && (
          <label className="flex items-center gap-1.5 text-xs text-text-secondary cursor-pointer shrink-0">
            <input
              type="checkbox"
              checked={nsfw}
              onChange={e => setNsfwSticky(e.target.checked)}
              className="w-3.5 h-3.5 rounded accent-red-500"
            />
            NSFW
          </label>
          )}

          {/* Updatable-only filter — only meaningful in the My LoRAs
              view, so we render it conditionally to avoid clutter while
              the user is browsing CivitAI. Same UX shape as the
              corresponding toggle in the Settings LoraSelector. */}
          {showInstalled && (
            <label
              className="flex items-center gap-1.5 text-xs cursor-pointer shrink-0 select-none"
              title={updatableCount > 0
                ? `${updatableCount} LoRA${updatableCount === 1 ? ' has' : 's have'} updates available — check to filter`
                : 'No updates available — click Check to refresh from CivitAI'}
            >
              <input
                type="checkbox"
                checked={updatableOnly}
                onChange={e => setUpdatableOnly(e.target.checked)}
                disabled={updatableCount === 0}
                className="w-3.5 h-3.5 rounded accent-amber-500 disabled:opacity-40"
              />
              <span className={`flex items-center gap-0.5 ${
                updatableOnly ? 'text-indicator-warning' : updatableCount > 0 ? 'text-text-secondary' : 'text-text-muted'
              }`}>
                <ArrowUpCircle size={11} />
                Updates
              </span>
            </label>
          )}

          {/* Sort control for the installed view — the storage story:
              newest-download and newest-release make it obvious which of
              a creator's renamed variants is current and what's stale. */}
          {browseKind === 'lora' && showInstalled && (
            <select
              value={installedSort}
              onChange={e => setInstalledSort(e.target.value as typeof installedSort)}
              className="px-2 py-1 text-xs rounded-lg border border-border bg-bg-tertiary text-text-secondary focus:outline-none focus:border-accent-blue shrink-0"
              title="Sort installed LoRAs"
            >
              <option value="name">Name</option>
              <option value="downloaded">Newest download</option>
              <option value="released">Newest release</option>
              <option value="largest">Largest file</option>
            </select>
          )}

          {/* Check-for-updates button. Always visible so the user can
              trigger a refresh from any view. The amber count badge
              surfaces outdated LoRAs even before they open the My
              LoRAs view. */}
          {/* Update-check + My LoRAs are LoRA-library features; hidden in
              Checkpoint mode (checkpoints have their own model list). */}
          {browseKind === 'lora' && (
            <button
              onClick={handleCheckUpdates}
              disabled={checkingUpdates}
              className="flex items-center gap-1 px-2 py-1 text-xs rounded-lg border border-border text-text-secondary hover:text-text-primary hover:border-border-light transition-colors shrink-0 disabled:opacity-50 disabled:cursor-not-allowed"
              title={checkingUpdates
                ? 'Checking CivitAI for newer LoRA versions…'
                : 'Check CivitAI for newer LoRA versions'}
            >
              {checkingUpdates
                ? <Loader2 size={11} className="animate-spin" />
                : <RefreshCw size={11} />}
              Check
              {updatableCount > 0 && !checkingUpdates && (
                <span className="ml-0.5 px-1 rounded bg-amber-500/20 text-indicator-warning text-[10px] font-medium">
                  {updatableCount}
                </span>
              )}
            </button>
          )}

          {browseKind === 'lora' && (
            <button
              onClick={() => {
                const next = !showInstalled
                setShowInstalled(next)
                if (next && installedLoras.length === 0) {
                  setInstalledLoading(true)
                  fetchInstalledLoras().then(r => { setInstalledLoras(r.loras); setInstalledLoading(false) }).catch(() => setInstalledLoading(false))
                }
              }}
              className={`flex items-center gap-1 px-2 py-1 text-xs rounded-lg border transition-colors shrink-0 ${
                showInstalled
                  ? 'bg-accent-blue/10 border-accent-blue text-accent-blue'
                  : 'border-border text-text-secondary hover:text-text-primary hover:border-border-light'
              }`}
            >
              <HardDrive size={11} />
              My LoRAs
              {/* Count badge on the My LoRAs button itself — visible from
                  the CivitAI search view too, so the user knows there
                  are updates worth investigating without clicking through. */}
              {updatableCount > 0 && (
                <span className="ml-0.5 px-1 rounded bg-amber-500/20 text-indicator-warning text-[10px] font-medium">
                  {updatableCount}
                </span>
              )}
            </button>
          )}

          {/* Checkpoint-mode "My Models" toggle (imported checkpoints). */}
          {browseKind === 'checkpoint' && (
            <button
              onClick={() => setShowCkptInstalled(v => !v)}
              className={`flex items-center gap-1 px-2 py-1 text-xs rounded-lg border transition-colors shrink-0 ${
                showCkptInstalled
                  ? 'bg-accent-blue/10 border-accent-blue text-accent-blue'
                  : 'border-border text-text-secondary hover:text-text-primary hover:border-border-light'
              }`}
            >
              <Boxes size={11} />
              My Models
            </button>
          )}

          {loading && <Loader2 size={14} className="animate-spin text-accent-blue ml-auto shrink-0" />}
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 overflow-hidden">
        {selectedModel ? (
          <ModelDetail model={selectedModel} onBack={clearSelection} kind={browseKind} />
        ) : (browseKind === 'checkpoint' && showCkptInstalled) ? (
          <InstalledCheckpoints onSelectModel={selectModel} />
        ) : showInstalled ? (
          /* Installed LoRAs view */
          <div className="h-full overflow-y-auto p-4">
            {installedLoading ? (
              <div className="flex items-center justify-center py-20">
                <Loader2 size={24} className="animate-spin text-accent-blue" />
              </div>
            ) : installedLoras.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-20 text-text-muted">
                <HardDrive size={32} className="mb-3 opacity-50" />
                <p className="text-sm">No LoRAs installed</p>
              </div>
            ) : (() => {
              // Filter installed LoRAs by active model filter and search query
              const filtered = installedLoras.filter(lora => {
                // Filter by NSFW — when the master gate is off OR the
                // local checkbox is unchecked, hide NSFW LoRAs.
                // Uses effectiveNsfw so disabling NSFW in services
                // immediately suppresses NSFW items in this view too.
                if (!effectiveNsfw && lora.nsfw) return false
                // Filter by update availability — when "Updates" is on,
                // only LoRAs with update_status === 'available' pass
                if (updatableOnly && lora.update_status !== 'available') return false
                // Filter by model/directory
                if (activeFilter) {
                  const dirMatch = activeFilter.default_dir && lora.directory === activeFilter.default_dir
                  const baseMatch = activeFilter.civitai_base && lora.base_model === activeFilter.civitai_base
                  if (!dirMatch && !baseMatch) return false
                }
                // Filter by search query
                if (query.trim()) {
                  const q = query.toLowerCase()
                  const name = (lora.name || lora.filename).toLowerCase()
                  const words = lora.trained_words.join(' ').toLowerCase()
                  if (!name.includes(q) && !words.includes(q) && !lora.directory.toLowerCase().includes(q)) return false
                }
                return true
              })
              if (installedSort !== 'name') {
                filtered.sort((a, b) => {
                  if (installedSort === 'largest') return (b.size_bytes ?? 0) - (a.size_bytes ?? 0)
                  const dateOf = (l: typeof a) => (installedSort === 'released' ? (l.released_at || l.downloaded_at) : l.downloaded_at) || ''
                  return dateOf(b).localeCompare(dateOf(a))
                })
              }
              return filtered.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-20 text-text-muted">
                  <Search size={32} className="mb-3 opacity-50" />
                  <p className="text-sm">No matching LoRAs</p>
                  <p className="text-xs mt-1">{installedLoras.length} installed, try different filters</p>
                </div>
              ) : (
              <>
              {deleteError && (
                <div className="mb-3 px-3 py-2 text-[11px] text-red-400 bg-red-500/10 border border-red-500/30 rounded-lg leading-snug">{deleteError}</div>
              )}
              <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8 gap-3">
                {filtered.map(lora => {
                  const cardKey = `${lora.directory}/${lora.filename}`
                  const clickable = Boolean(lora.civitai_model_id || (lora as any).hf_repo_id)
                  const openCard = () => {
                    if (lora.civitai_model_id) {
                      selectModel(lora.civitai_model_id)
                    } else if ((lora as any).hf_repo_id) {
                      window.open(`https://huggingface.co/${(lora as any).hf_repo_id}`, '_blank')
                    }
                  }
                  return (
                  <div
                    key={cardKey}
                    onClick={openCard}
                    role={clickable ? 'button' : undefined}
                    tabIndex={clickable ? 0 : undefined}
                    onKeyDown={e => { if (clickable && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openCard() } }}
                    className={`relative rounded-lg border overflow-hidden bg-bg-tertiary text-left transition-all group ${
                      clickable
                        ? 'border-border hover:border-accent-blue cursor-pointer'
                        : 'border-border/50 opacity-75'
                    }`}
                  >
                    <div className="relative aspect-[3/4] bg-bg-active overflow-hidden">
                      {lora.preview_url ? (
                        lora.preview_url.endsWith('.mp4') || lora.preview_url.endsWith('.webm') ? (
                          <video src={lora.preview_url} className={`w-full h-full object-cover transition-transform duration-300 ${clickable ? 'group-hover:scale-105' : ''}`} muted loop autoPlay playsInline />
                        ) : (
                          <img src={lora.preview_url} alt={lora.name || lora.filename} className={`w-full h-full object-cover transition-transform duration-300 ${clickable ? 'group-hover:scale-105' : ''}`} loading="lazy" referrerPolicy="no-referrer" />
                        )
                      ) : (
                        <div className="w-full h-full flex flex-col items-center justify-center text-text-muted gap-1">
                          <HardDrive size={20} className="opacity-30" />
                          <span className="text-[9px] opacity-40">No metadata</span>
                        </div>
                      )}
                    <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/80 via-black/50 to-transparent p-2 pt-6">
                      <div className="text-xs font-medium text-white truncate">{lora.name || lora.filename.replace(/\.(safetensors|sft)$/i, '')}</div>
                      <div className="flex items-center gap-1.5 mt-0.5">
                        <span className="text-[9px] px-1.5 py-0.5 rounded bg-black/60 text-white/80">{lora.directory}</span>
                        {lora.linked && (
                          <span
                            className="text-[9px] px-1.5 py-0.5 rounded bg-accent-blue/70 text-white"
                            title="From a linked install's loras folder (read-only) — guides and metadata are stored in MuseForge"
                          >
                            Linked
                          </span>
                        )}
                        {lora.has_guide && <BookOpen size={9} className="text-accent-green" />}
                        {lora.base_model && <span className="text-[9px] text-white/50">{lora.base_model}</span>}
                        {typeof lora.size_bytes === 'number' && (
                          <span className="text-[9px] text-white/50 ml-auto shrink-0">{formatBytes(lora.size_bytes)}</span>
                        )}
                      </div>
                      {(lora.downloaded_at || lora.released_at) && (
                        <div
                          className="text-[9px] text-white/40 mt-0.5 truncate"
                          title={`Downloaded ${lora.downloaded_at ? new Date(lora.downloaded_at).toLocaleDateString() : 'unknown'}${lora.released_at ? ` — released ${new Date(lora.released_at).toLocaleDateString()}` : ''}`}
                        >
                          {installedSort === 'released' && lora.released_at
                            ? `released ${new Date(lora.released_at).toLocaleDateString()}`
                            : lora.downloaded_at
                              ? `added ${new Date(lora.downloaded_at).toLocaleDateString()}`
                              : `released ${new Date(lora.released_at as string).toLocaleDateString()}`}
                        </div>
                      )}
                      {lora.trained_words.length > 0 && (
                        <div className="flex items-center gap-0.5 mt-1 overflow-hidden">
                          <Tag size={8} className="text-white/50 shrink-0" />
                          <span className="text-[9px] text-white/60 truncate">{lora.trained_words.join(', ')}</span>
                        </div>
                      )}
                    </div>
                    </div>

                    {/* The action, in its own row UNDER the thumbnail. It used
                        to be 9px text inside the dark gradient over the image,
                        which is why it could not be found. */}
                    {(() => {
                      const verdict = loraVerdict(lora)
                      const on = activatedLoras?.includes(lora.filename)
                      const done = used === cardKey
                      return (
                        <button
                          onClick={async e => {
                            e.stopPropagation()
                            if (verdict.usable) {
                              await applyOwnedLora(lora, cardKey)
                            } else if (verdict.moveTo) {
                              // Move it where it belongs, then pick a model
                              // from the folder it landed in.
                              setMoving(cardKey)
                              try {
                                const moved = await relocateLora({
                                  filename: lora.filename,
                                  directory: lora.directory,
                                  target: verdict.moveTo,
                                })
                                const fresh = await fetchInstalledLoras()
                                setInstalledLoras(fresh.loras)
                                const now = fresh.loras.find(
                                  x => x.filename === lora.filename
                                    && x.directory === moved.directory)
                                if (now) setPickModelFor(now)
                              } catch (e) {
                                setDeleteError(e instanceof Error ? e.message : 'Move failed')
                              } finally {
                                setMoving(null)
                              }
                            } else if (verdict.offer?.length) {
                              setPickModelFor(lora)
                            }
                          }}
                          disabled={!verdict.usable && !verdict.offer?.length}
                          title={verdict.usable && on ? 'Already active for this model' : verdict.reason}
                          className={`w-full border-t border-border/60 px-2 py-1.5 text-[11px] font-medium transition-colors ${
                            !verdict.usable
                              ? verdict.offer?.length
                                ? 'bg-bg-tertiary text-accent-blue hover:bg-bg-hover'
                                : 'bg-bg-active text-text-muted cursor-not-allowed'
                              : done || on
                                ? 'bg-indicator-success/20 text-indicator-success'
                                : 'bg-cta text-white hover:opacity-90'
                          }`}
                        >
                          {moving === cardKey
                            ? 'Moving…'
                            : verdict.usable
                              ? (done ? 'Added' : on ? 'Active' : 'Use now')
                              : verdict.offer?.length
                                ? (verdict.moveTo
                                    ? `Move to ${verdict.moveTo} & use`
                                    : 'Use now — pick a model')
                                : verdict.noHome
                                  ? `No model for ${lora.base_model}`
                                  : lora.directory === '.'
                                    ? 'Wrong folder — no model looks here'
                                    : 'No model loads from this folder'}
                        </button>
                      )
                    })()}
                    {!lora.civitai_model_id && (
                      <div className="absolute top-1.5 right-1.5">
                        <span className={`text-[8px] px-1 py-0.5 rounded bg-black/60 ${(lora as any).hf_repo_id ? 'text-amber-300/80' : 'text-white/50'}`}>
                          {(lora as any).hf_repo_id ? 'HuggingFace' : 'Local only'}
                        </span>
                      </div>
                    )}
                    {/* Update-available pill in the top-LEFT corner so it
                        doesn't overlap the HuggingFace/Local-only badge
                        on the top-right. The pill style (vs a bare icon)
                        gives it the visual weight needed against busy
                        preview imagery — a tiny icon would disappear. */}
                    {lora.update_status === 'available' && (
                      <div className="absolute top-1.5 left-1.5">
                        <span
                          className="flex items-center gap-0.5 text-[8px] px-1 py-0.5 rounded bg-amber-500/90 text-white font-medium shadow-sm"
                          title={lora.latest_published_at
                            ? `Update available — published ${new Date(lora.latest_published_at).toLocaleDateString()}`
                            : 'Update available on CivitAI'}
                        >
                          <ArrowUpCircle size={9} />
                          UPDATE
                        </span>
                      </div>
                    )}
                    {/* Delete — primary-root files only; linked copies are
                        read-only (deleting them would break the other install). */}
                    {!lora.linked && (
                      <button
                        onClick={e => handleDeleteLora(lora.directory, lora.filename, e)}
                        disabled={deletingKey === cardKey}
                        className={`absolute bottom-1.5 right-1.5 p-1 rounded transition-all ${
                          confirmDeleteKey === cardKey
                            ? 'bg-red-500/90 text-white opacity-100'
                            : deletingKey === cardKey
                              ? 'bg-black/60 text-white/70 opacity-100 cursor-wait'
                              : 'bg-black/60 text-white/70 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:text-red-400'
                        }`}
                        title={confirmDeleteKey === cardKey
                          ? 'Click again to permanently delete this LoRA (weights + metadata + guide)'
                          : 'Delete this LoRA from disk'}
                      >
                        {deletingKey === cardKey
                          ? <Loader2 size={11} className="animate-spin" />
                          : <Trash2 size={11} />}
                      </button>
                    )}
                  </div>
                  )
                })}
              </div>
              </>
              )
            })()}
          </div>
        ) : (
          <div className="h-full overflow-y-auto p-4">
            {/* CivitAI Search Grid */}
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8 gap-3">
              {results.map(model => (
                <ModelCard
                  key={model.id}
                  model={model}
                  onClick={() => selectModel(model.id)}
                  owned={(() => {
                    const local = ownedByCivitId.get(model.id)
                    if (!local) return undefined
                    const v = loraVerdict(local)
                    return { label: local.filename, usable: v.usable, reason: v.reason }
                  })()}
                  onUseNow={() => {
                    const local = ownedByCivitId.get(model.id)
                    if (local) applyOwnedLora(local, `civit/${model.id}`)
                  }}
                  unsupportedBase={(() => {
                    // Only claim it when the map is loaded, or every card would
                    // read "unsupported" for a moment on open.
                    const base = model.modelVersions?.[0]?.baseModel
                    if (!base || !Object.keys(baseHome).length) return undefined
                    return baseHome[base] ? undefined : base
                  })()}
                />
              ))}
            </div>

            {/* Loading / empty states */}
            {loading && results.length === 0 && (
              <div className="flex items-center justify-center py-20">
                <Loader2 size={24} className="animate-spin text-accent-blue" />
              </div>
            )}

            {!loading && results.length === 0 && searchError && (
              <div className="flex flex-col items-center justify-center py-16 text-text-muted">
                <div className="max-w-md w-full px-4 py-3 rounded-lg bg-amber-500/10 border border-amber-500/30 text-text-primary text-sm leading-relaxed">
                  <div className="font-semibold mb-1">CivitAI is unavailable</div>
                  <div className="text-[12px] text-text-secondary">{searchError}</div>
                </div>
              </div>
            )}

            {!loading && results.length === 0 && !searchError && (
              <div className="flex flex-col items-center justify-center py-20 text-text-muted">
                <Search size={32} className="mb-3 opacity-50" />
                <p className="text-sm">No results found</p>
                <p className="text-xs mt-1">Try different search terms or filters</p>
              </div>
            )}

            {/* Infinite scroll sentinel */}
            {cursor && <div ref={sentinelRef} className="h-10" />}

            {/* Loading more indicator */}
            {loading && results.length > 0 && (
              <div className="flex items-center justify-center py-6">
                <Loader2 size={16} className="animate-spin text-accent-blue" />
              </div>
            )}
          </div>
        )}
      </div>

      {/* Download progress bar */}
      <DownloadBar />

      {/* Model picker — for a LoRA the selected model cannot load. The folder
          says which models look there, so this is a choice, not a dead end.
          Downloaded models first: an undownloaded one means a long wait before
          anything happens, and that should be a deliberate pick. */}
      {pickModelFor && (() => {
        const owners = dirModels[pickModelFor.directory] || []
        const rows = owners
          .map(mt => allModels.find(m => m.model_type === mt) || null)
          .map((m, i) => ({
            model_type: m?.model_type || owners[i],
            name: m?.name || owners[i],
            downloaded: !!m?.is_downloaded,
          }))
          .sort((a, b) => Number(b.downloaded) - Number(a.downloaded)
            || a.name.localeCompare(b.name))
        return (
          <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
            <div className="absolute inset-0 bg-black/60" onClick={() => setPickModelFor(null)} />
            <div className="glass-panel relative flex max-h-[70vh] w-full max-w-lg flex-col rounded-2xl p-4 shadow-2xl">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="text-sm font-semibold text-text-primary">Pick a model for this LoRA</h2>
                  <p className="mt-0.5 truncate text-[11px] text-text-muted">
                    {pickModelFor.name || pickModelFor.filename} — stored in loras/{pickModelFor.directory}
                  </p>
                </div>
                <button
                  onClick={() => setPickModelFor(null)}
                  className="shrink-0 rounded-lg p-1 text-text-muted hover:text-text-primary"
                  aria-label="Close"
                >
                  <X size={14} />
                </button>
              </div>
              <p className="mt-2 text-[11px] text-text-secondary">
                LoRAs are stored per architecture, so only these {rows.length} models
                load from that folder. Choosing one switches the model and activates
                the LoRA with its trigger words.
              </p>
              <div className="mt-3 min-h-0 flex-1 overflow-y-auto">
                {rows.map(row => (
                  <button
                    key={row.model_type}
                    onClick={async () => {
                      const ok = await activateWithModel(
                        pickModelFor.filename, row.model_type, pickModelFor.trained_words,
                      )
                      setPickModelFor(null)
                      if (ok) setOpen(false)
                    }}
                    className="flex w-full items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-bg-hover"
                  >
                    <span className="min-w-0 truncate text-[12px] text-text-primary">{row.name}</span>
                    <span className={`shrink-0 text-[9px] uppercase tracking-wide ${
                      row.downloaded ? 'text-indicator-success' : 'text-text-muted'
                    }`}>
                      {row.downloaded ? 'ready' : 'downloads first'}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          </div>
        )
      })()}
    </div>
  )
}
