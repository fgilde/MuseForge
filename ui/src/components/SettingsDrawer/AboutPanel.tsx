import { useEffect, useRef, useState } from 'react'
import { useStore } from '../../stores/useStore'
import { resolveVariant } from '../../lib/theme'

const WIDGET_MODULE = 'https://connect.gilde.org/widgets/v1.js'
const PROJECT = 'fgilde/MuseForge'

// React 19 resolves intrinsic elements through its own JSX namespace, not the
// global one, so the Connect custom elements are declared there.
type ConnectElement = React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>
  & Record<string, unknown>

declare module 'react' {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace JSX {
    interface IntrinsicElements {
      'gilde-contact': ConnectElement
      'gilde-support': ConnectElement
    }
  }
}

/** Load the Connect module once, and report whether it actually arrived.
 *
 *  MuseForge runs locally and is expected to work without internet, so this
 *  is deliberately lazy — the script is fetched when someone opens About,
 *  never at startup — and a failure degrades to the plain links below
 *  instead of leaving two empty boxes.
 */
function useConnectWidgets(): 'loading' | 'ready' | 'unavailable' {
  const [state, setState] = useState<'loading' | 'ready' | 'unavailable'>(
    () => (customElements.get('gilde-contact') ? 'ready' : 'loading'),
  )

  useEffect(() => {
    if (customElements.get('gilde-contact')) { setState('ready'); return }

    let cancelled = false
    const done = (next: 'ready' | 'unavailable') => { if (!cancelled) setState(next) }

    let script = document.querySelector<HTMLScriptElement>(`script[src="${WIDGET_MODULE}"]`)
    if (!script) {
      script = document.createElement('script')
      script.type = 'module'
      script.src = WIDGET_MODULE
      document.head.appendChild(script)
    }
    script.addEventListener('error', () => done('unavailable'))

    // A module script's load event fires before customElements.define runs,
    // so wait for the definition itself rather than for the request.
    void Promise.race([
      customElements.whenDefined('gilde-contact').then(() => 'ready' as const),
      new Promise<'unavailable'>(resolve => window.setTimeout(() => resolve('unavailable'), 8000)),
    ]).then(done)

    return () => { cancelled = true }
  }, [])

  return state
}

/** The accent the surrounding app is currently painted in.
 *
 *  Every theme family redefines --color-accent-blue, so this is read from the
 *  document rather than hard-coded; a hard-coded blue would clash the moment
 *  someone picks Golden Hour.
 */
function useAppAccent(): string {
  const themePrefs = useStore(s => s.themePrefs)
  const [accent, setAccent] = useState('#3b82f6')

  useEffect(() => {
    const read = () => {
      const value = getComputedStyle(document.documentElement)
        .getPropertyValue('--color-accent-blue').trim()
      if (value) setAccent(value)
    }
    // The theme writes its variables during the same commit, so read after it.
    const handle = window.requestAnimationFrame(read)
    return () => window.cancelAnimationFrame(handle)
  }, [themePrefs])

  return accent
}

export function AboutPanel() {
  const appVersion = useStore(s => s.systemConfig?.app_version)
  const themePrefs = useStore(s => s.themePrefs)
  const theme = resolveVariant(themePrefs)
  const accent = useAppAccent()
  const widgets = useConnectWidgets()
  const host = useRef<HTMLDivElement>(null)

  // Attributes are set imperatively: the elements are upgraded after the
  // module lands, and re-rendering React alone would not re-apply a changed
  // accent to an element that already exists.
  useEffect(() => {
    if (widgets !== 'ready') return
    host.current?.querySelectorAll('gilde-contact, gilde-support').forEach(element => {
      element.setAttribute('theme', theme)
      element.setAttribute('accent', accent)
      // Language too: an element that upgraded before React wrote its
      // attributes falls back to the browser locale, which showed German
      // labels inside an English interface.
      element.setAttribute('language', 'en')
    })
  }, [widgets, theme, accent])

  const shared = {
    project: PROJECT,
    inline: true,
    theme,
    language: 'en',
    width: '560',
    radius: '14',
    padding: '22',
    'show-logo': 'true',
    // Inline: the widget sits inside our own panel, which already carries the
    // product name and the links, so its footer and blurb would only repeat us.
    'show-description': 'false',
    'show-footer': 'false',
    // Not the homepage, so the link out is useful here.
    'show-homepage': 'true',
    'show-preview-notice': 'false',
    'footer-brand': 'MuseForge',
    'footer-tagline': 'gilde.org',
  }

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-text-primary">MuseForge{appVersion ? ` v${appVersion}` : ''}</h3>
        <p className="mt-1 text-xs text-text-secondary leading-relaxed">
          A self-hosted AI studio for video, images, audio, long-form text and
          audiobooks. Everything runs on this machine; nothing is sent to a
          cloud service unless you configure one yourself.
        </p>
      </div>

      {widgets === 'unavailable' ? (
        <p className="rounded-lg border border-border bg-bg-tertiary px-3 py-2 text-[11px] text-text-muted">
          The contact and support forms need an internet connection. Use the
          links above, or open an issue on GitHub.
        </p>
      ) : (
        <div ref={host} className="grid gap-4 [&>*]:min-w-0">
          <gilde-contact {...shared} widget="contact" accent={accent} title="Contact MuseForge">
            Contact MuseForge
          </gilde-contact>
          <gilde-support
            {...shared}
            widget="support"
            accent={accent}
            show-support-hint="false"
            support-layout="rows"
            show-support-icons="true"
            show-support-qr="false"
          >
            Support MuseForge
          </gilde-support>
        </div>
      )}
    </div>
  )
}
