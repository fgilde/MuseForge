import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Check, Heart, Loader2, Trash2, FolderInput, BookMarked, Maximize2, X, Film,
} from 'lucide-react'
import { useStore } from '../../stores/useStore'
import { getFileUrl, moveOutput } from '../../api/client'
import { MediaFeedItem } from './MediaFeedItem'
import { SaveRecipeDialog } from '../Recipes/SaveRecipeDialog'
import type { OutputFile } from '../../types'

/**
 * The gallery, as a plain CSS grid of cards.
 *
 * It replaces a hand-rolled virtualizer that measured every item's height,
 * kept an offset table, tracked the active item with an IntersectionObserver
 * and scrolled in two phases to stay in step with a thumbnail strip. That
 * machinery is where the reported scroll trouble came from: an unmeasured item
 * far away only has an estimated offset, so jumping to it landed off target,
 * measuring it mid-animation moved the total height, and the observer then
 * picked a different active item — which scrolled the strip, which moved the
 * feed again.
 *
 * Uniform cards remove the whole problem instead of tuning it: equal heights
 * mean no measurement, no offsets, no sync, and the browser's own scrolling.
 * The big view moved into a dialog, which is also where it belongs — it is a
 * deliberate action, not something scrolling should trigger.
 *
 * What the grid buys beyond the fix: selecting several items at once, which is
 * the only sane way to favourite, move or delete more than one thing.
 */
export function MediaGrid() {
  const outputs = useStore(s => s.outputs)
  const outputsTotal = useStore(s => s.outputsTotal)
  const loadMore = useStore(s => s.loadMoreOutputs)
  const nsfwMode = !!useStore(s => s.servicesConfig?.nsfw_mode)
  const setSelectedOutput = useStore(s => s.setSelectedOutput)
  const selectedOutput = useStore(s => s.selectedOutput)
  const toggleFavorite = useStore(s => s.toggleFavorite)
  const deleteSelected = useStore(s => s.deleteSelectedOutput)
  const workspaces = useStore(s => s.workspaces)
  const activeWorkspace = useStore(s => s.activeWorkspace)
  const loadOutputs = useStore(s => s.loadOutputs)
  const saveRecipe = useStore(s => s.saveRecipeFromOutput)

  /** Filenames, not indices: the list reorders and shrinks under us. */
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [openIndex, setOpenIndex] = useState<number | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [moveOpen, setMoveOpen] = useState(false)
  // loadMoreOutputs has no loading flag of its own and guards on the count, so
  // the grid tracks the in-flight page itself to avoid firing twice.
  const [loadingMore, setLoadingMore] = useState(false)
  const [blueprintFor, setBlueprintFor] = useState<OutputFile | null>(null)
  const sentinel = useRef<HTMLDivElement | null>(null)

  const hasMore = outputs.length < outputsTotal

  // Infinite scroll. No offset maths — the sentinel is a real element and the
  // browser reports when it comes into view.
  useEffect(() => {
    const el = sentinel.current
    if (!el || !hasMore || loadingMore) return
    const observer = new IntersectionObserver(async entries => {
      if (!entries[0]?.isIntersecting) return
      setLoadingMore(true)
      try {
        await loadMore()
      } finally {
        setLoadingMore(false)
      }
    }, { rootMargin: '600px' })
    observer.observe(el)
    return () => observer.disconnect()
  }, [hasMore, loadingMore, loadMore])

  // Selecting nothing should not leave a stale selection bar behind when the
  // workspace changes the list out from under it.
  useEffect(() => { setPicked(new Set()) }, [activeWorkspace])

  const toggle = useCallback((name: string) => {
    setPicked(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }, [])

  const open = useCallback((index: number) => {
    setSelectedOutput(index)
    setOpenIndex(index)
  }, [setSelectedOutput])

  const pickedFiles = outputs.filter(o => picked.has(o.name))

  /** Run one action per picked file, sequentially — these hit the disk, and a
   *  burst of parallel writes on a slow volume is how half-moved sets happen. */
  const forEachPicked = async (
    label: string, fn: (file: OutputFile) => Promise<void>,
  ) => {
    setBusy(label)
    setError(null)
    let failed = 0
    for (const file of pickedFiles) {
      try {
        await fn(file)
      } catch {
        failed++
      }
    }
    setBusy(null)
    setPicked(new Set())
    await loadOutputs()
    if (failed) setError(`${failed} of ${pickedFiles.length} could not be ${label}.`)
  }

  if (outputs.length === 0) return null

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {picked.size > 0 && (
        <div className="shrink-0 flex flex-wrap items-center gap-2 border-b border-border bg-bg-secondary px-3 py-2">
          <span className="text-[11px] font-medium text-text-primary">
            {picked.size} selected
          </span>
          <button
            onClick={() => forEachPicked('favourited', f => toggleFavorite(f.name))}
            disabled={!!busy}
            className="flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:border-border-light hover:text-text-primary disabled:opacity-40"
          >
            <Heart size={11} /> Favourite
          </button>
          <div className="relative">
            <button
              onClick={() => setMoveOpen(v => !v)}
              disabled={!!busy || workspaces.length < 2}
              title={workspaces.length < 2 ? 'Only one workspace exists' : 'Move to another workspace'}
              className="flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:border-border-light hover:text-text-primary disabled:opacity-40"
            >
              <FolderInput size={11} /> Move to…
            </button>
            {moveOpen && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setMoveOpen(false)} />
                <div className="glass-panel absolute left-0 top-full z-50 mt-1 w-52 rounded-xl p-1.5 shadow-2xl">
                  {workspaces.filter(w => w.name !== activeWorkspace).map(w => (
                    <button
                      key={w.name}
                      onClick={async () => {
                        setMoveOpen(false)
                        await forEachPicked('moved',
                          f => moveOutput(f.name, w.name).then(() => undefined))
                      }}
                      className="block w-full truncate rounded-md px-2 py-1.5 text-left text-[11px] text-text-secondary hover:bg-bg-hover hover:text-text-primary"
                    >
                      {w.name}
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>
          <button
            onClick={() => setBlueprintFor(pickedFiles[0] ?? null)}
            disabled={!!busy || picked.size !== 1}
            title={picked.size === 1
              ? 'Save this one as a blueprint'
              : 'A blueprint captures one generation — select exactly one'}
            className="flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11px] text-text-secondary hover:border-border-light hover:text-text-primary disabled:opacity-40"
          >
            <BookMarked size={11} /> Save as blueprint
          </button>
          <button
            onClick={async () => {
              // deleteSelectedOutput works on the store's selection, so point
              // it at each file in turn rather than duplicating its logic.
              await forEachPicked('deleted', async file => {
                const at = useStore.getState().outputs.findIndex(o => o.name === file.name)
                if (at < 0) return
                setSelectedOutput(at)
                await deleteSelected()
              })
            }}
            disabled={!!busy}
            className="flex items-center gap-1 rounded-lg border border-red-500/40 px-2 py-1 text-[11px] text-red-400 hover:bg-red-500/10 disabled:opacity-40"
          >
            <Trash2 size={11} /> Delete
          </button>
          <button
            onClick={() => setPicked(new Set())}
            className="ml-auto rounded-lg px-2 py-1 text-[11px] text-text-muted hover:text-text-primary"
          >
            Clear
          </button>
          {busy && <Loader2 size={12} className="animate-spin text-accent-blue" />}
        </div>
      )}

      {error && (
        <div className="shrink-0 border-b border-red-500/30 bg-red-500/10 px-3 py-1.5 text-[11px] text-red-400">
          {error}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3 md:px-5">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-5">
          {outputs.map((file, index) => (
            <MediaCard
              key={file.name}
              file={file}
              index={index}
              picked={picked.has(file.name)}
              anyPicked={picked.size > 0}
              current={index === selectedOutput}
              onPick={() => toggle(file.name)}
              onOpen={() => open(index)}
              onFavourite={() => toggleFavorite(file.name)}
            />
          ))}
        </div>

        {hasMore && (
          <div ref={sentinel} className="flex items-center justify-center py-6">
            {loadingMore && <Loader2 size={16} className="animate-spin text-accent-blue" />}
          </div>
        )}
      </div>

      {openIndex !== null && outputs[openIndex] && (
        <DetailDialog
          index={openIndex}
          onClose={() => setOpenIndex(null)}
          onStep={delta => {
            const next = openIndex + delta
            if (next >= 0 && next < outputs.length) open(next)
          }}
        />
      )}

      {blueprintFor && (
        <SaveRecipeDialog
          defaultNsfw={nsfwMode}
          onCancel={() => setBlueprintFor(null)}
          onSave={async (name, description, nsfw) => {
            await saveRecipe(blueprintFor.name, name, description, nsfw)
            setBlueprintFor(null)
            setPicked(new Set())
          }}
        />
      )}
    </div>
  )
}

function MediaCard({ file, index, picked, anyPicked, current, onPick, onOpen, onFavourite }: {
  file: OutputFile
  index: number
  picked: boolean
  anyPicked: boolean
  current: boolean
  onPick: () => void
  onOpen: () => void
  onFavourite: () => void
}) {
  const url = getFileUrl(file.name)
  const isVideo = file.type === 'video'
  const isAudio = file.type === 'audio'

  return (
    <div
      className={`group relative flex flex-col overflow-hidden rounded-xl border bg-bg-secondary transition-colors ${
        picked
          ? 'border-accent-blue'
          : current
            ? 'border-border-light'
            : 'border-border hover:border-border-light'
      }`}
    >
      <button
        onClick={() => (anyPicked ? onPick() : onOpen())}
        title={anyPicked ? 'Add to selection' : 'Open'}
        className="relative block aspect-video w-full overflow-hidden bg-bg-active"
      >
        {isAudio ? (
          <div className="flex h-full w-full items-center justify-center text-text-muted">
            <Film size={22} className="opacity-30" />
          </div>
        ) : isVideo ? (
          // No autoplay: a gridful of playing videos is what makes a gallery
          // stutter. The dialog plays it.
          <video
            src={url}
            className="h-full w-full object-cover"
            muted
            playsInline
            preload="metadata"
          />
        ) : (
          <img src={url} alt="" className="h-full w-full object-cover" loading="lazy" />
        )}

        <span className="absolute inset-0 flex items-center justify-center bg-black/0 opacity-0 transition-opacity group-hover:bg-black/25 group-hover:opacity-100">
          <Maximize2 size={20} className="text-white" />
        </span>
      </button>

      {/* Checkbox stays reachable without opening anything. */}
      <button
        onClick={onPick}
        aria-label={picked ? 'Deselect' : 'Select'}
        className={`absolute left-1.5 top-1.5 flex h-5 w-5 items-center justify-center rounded border transition-opacity ${
          picked
            ? 'border-accent-blue bg-accent-blue text-white'
            : 'border-white/60 bg-black/40 text-transparent opacity-0 group-hover:opacity-100'
        }`}
      >
        <Check size={12} />
      </button>

      <button
        onClick={onFavourite}
        aria-label={file.favorite ? 'Remove favourite' : 'Favourite'}
        className={`absolute right-1.5 top-1.5 rounded p-1 transition-opacity ${
          file.favorite
            ? 'text-red-400'
            : 'text-white/70 opacity-0 group-hover:opacity-100 hover:text-red-400'
        }`}
      >
        <Heart size={13} fill={file.favorite ? 'currentColor' : 'none'} />
      </button>

      <div className="flex items-center gap-1.5 px-2 py-1.5">
        <span className="min-w-0 flex-1 truncate text-[10px] text-text-muted" title={file.name}>
          {file.name.replace(/\.[^.]+$/, '')}
        </span>
        <span className="shrink-0 text-[9px] uppercase tracking-wide text-text-muted">
          {file.type}
        </span>
      </div>
      <span className="sr-only">item {index + 1}</span>
    </div>
  )
}

/** The big view. Mounts the existing feed item, which already carries every
 *  per-item action — reroll, load settings, copy prompt, upscale, revoice,
 *  move, delete — so none of that is duplicated here. */
function DetailDialog({ index, onClose, onStep }: {
  index: number
  onClose: () => void
  onStep: (delta: number) => void
}) {
  const outputs = useStore(s => s.outputs)
  const file = outputs[index]

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      if (e.key === 'ArrowRight') onStep(1)
      if (e.key === 'ArrowLeft') onStep(-1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, onStep])

  if (!file) return null

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-3 md:p-6">
      <div className="absolute inset-0 bg-black/70" onClick={onClose} />
      <div className="glass-panel relative flex max-h-full w-full max-w-4xl flex-col overflow-hidden rounded-2xl shadow-2xl">
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-2">
          <span className="min-w-0 truncate text-[11px] text-text-secondary" title={file.name}>
            {file.name}
          </span>
          <div className="flex shrink-0 items-center gap-1">
            <span className="mr-1 text-[10px] text-text-muted">
              {index + 1} / {outputs.length}
            </span>
            <button
              onClick={() => onStep(-1)}
              disabled={index === 0}
              className="rounded px-2 py-1 text-[11px] text-text-secondary hover:text-text-primary disabled:opacity-30"
            >
              ‹
            </button>
            <button
              onClick={() => onStep(1)}
              disabled={index >= outputs.length - 1}
              className="rounded px-2 py-1 text-[11px] text-text-secondary hover:text-text-primary disabled:opacity-30"
            >
              ›
            </button>
            <button
              onClick={onClose}
              aria-label="Close"
              className="rounded p-1 text-text-muted hover:text-text-primary"
            >
              <X size={14} />
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <MediaFeedItem
            file={file}
            index={index}
            isActive
            onVisible={() => {}}
            onMeasured={() => {}}
          />
        </div>
      </div>
    </div>
  )
}
