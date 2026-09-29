import { useMemo, useSyncExternalStore } from 'react'

/**
 * Two routes on the History API, and nothing that would need a library.
 *
 * The pool id lives in the path rather than in component state because it is
 * the buyer's recovery handle: a link copied out of the address bar, a reload,
 * the back button -- each has to land on the same pool, and only the URL
 * survives all three.
 *
 * The parser is exported apart from the hook that listens so the rules below
 * can be tested without a DOM.
 */

export type Route =
  | { kind: 'entry' }
  | { kind: 'pool'; agreementId: bigint }
  /**
   * The browser-wallet spike harness. Recognised here unconditionally so the
   * parser stays a pure function of its input; whether it is *served* is
   * decided in `ui/App.tsx`, behind `import.meta.env.DEV`, which is the only
   * place that gate can remove the harness from a production bundle.
   */
  | { kind: 'spike' }
  | { kind: 'not-found' }

/**
 * The largest agreement id the contract can hold: ids are ABI `uint64`.
 *
 * A path above it is not a pool. It could never name a box, and passing it on
 * would fail later, when algosdk refuses to encode the box name, and reach the
 * buyer as a read that failed -- a pool that looks temporarily unreadable, and
 * keeps being retried, when the link is simply wrong.
 */
const MAX_UINT64 = 2n ** 64n - 1n

/**
 * Canonical decimal only: `0`, or a non-zero digit followed by digits.
 *
 * The shape is checked before `BigInt()` ever sees the text, because `BigInt`
 * is both too strict and too lenient to be the check. `BigInt('1e3')` throws,
 * which would take the screen down from a typo; `BigInt(' 7 ')`, `BigInt('0x1f')`
 * and `BigInt('007')` all succeed, which would let several spellings reach one
 * pool. Two URLs for one pool is a copied link that stops matching the one in
 * the buyer's history, and a leading zero is the spelling `poolPath` never
 * produces.
 */
const CANONICAL_ID = /^(?:0|[1-9][0-9]*)$/

const POOL_PREFIX = '/app/pool/'

/**
 * The entry screen's address, and the only spelling a link may leave in the
 * address bar.
 *
 * With the trailing slash, because the base is `/app/` (see `vite.config.ts`)
 * and a server with that base serves only paths under it: Vite answers a bare
 * `/app` with a 404 page of its own. The bar's contents are what a reload, a
 * bookmark or a copied link asks for next, so a link to `/app` works once and
 * then strands whoever reloads.
 */
export const ENTRY_PATH = '/app/'

export function parseRoute(pathname: string): Route {
  // Both spellings are the entry screen. Links only ever mint `ENTRY_PATH`,
  // but the dev and preview servers redirect a typed `/app` to it, and
  // whatever serves the bundle in production may serve the bare spelling
  // instead of redirecting it.
  if (pathname === '/app' || pathname === ENTRY_PATH) return { kind: 'entry' }
  if (pathname === '/app/spike') return { kind: 'spike' }

  if (pathname.startsWith(POOL_PREFIX)) {
    const segment = pathname.slice(POOL_PREFIX.length)
    // A trailing segment -- `/app/pool/25/extra` -- fails the shape test too,
    // because `/` is not a digit. That is deliberate: there is no sub-page of
    // a pool, and silently rendering pool 25 for it would make a mistyped
    // link look correct.
    if (!CANONICAL_ID.test(segment)) return { kind: 'not-found' }
    const agreementId = BigInt(segment)
    if (agreementId > MAX_UINT64) return { kind: 'not-found' }
    return { kind: 'pool', agreementId }
  }

  return { kind: 'not-found' }
}

/**
 * Subscribers to `navigate`.
 *
 * `pushState` fires no event of its own -- `popstate` is only for the back and
 * forward buttons -- so a navigation this client makes has to announce itself,
 * or the screen would stay on the old route while the address bar moved on.
 */
const listeners = new Set<() => void>()

/**
 * Move the address bar, and re-render whatever reads the route.
 *
 * A path equal to the current one adds nothing. The reducer asks to navigate
 * to a pool that is already on screen whenever a quote names it, and a history
 * entry that goes nowhere is a back button that appears to do nothing.
 */
export function navigate(path: string): void {
  if (window.location.pathname === path) return
  window.history.pushState(null, '', path)
  for (const listener of listeners) listener()
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange)
  window.addEventListener('popstate', onChange)
  return () => {
    listeners.delete(onChange)
    window.removeEventListener('popstate', onChange)
  }
}

/**
 * The route the address bar names, kept current across `navigate` and the
 * back button.
 *
 * The snapshot is the pathname string rather than a parsed `Route`, because
 * `useSyncExternalStore` compares snapshots by identity and a freshly parsed
 * object is never identical to the last one -- it would re-render forever.
 * Parsing happens after, memoised on the string.
 */
export function useRoute(): { pathname: string; route: Route } {
  const pathname = useSyncExternalStore(subscribe, () => window.location.pathname)
  const route = useMemo(() => parseRoute(pathname), [pathname])
  return { pathname, route }
}
