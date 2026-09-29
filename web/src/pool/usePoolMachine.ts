import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createAlgod, createIndexer } from '../chain/read'
import { navigate } from '../router'
import { useSigner, useWalletAddress } from '../wallet'
import { initialState, reduce, refreshInterval, type Event, type State } from './machine'
import { run, type Runtime } from './runner'

/**
 * The one place the pure machine meets React: state, effect execution, the
 * wallet, and the refresh timer.
 *
 * It decides nothing. Every answer about what the buyer may do comes from the
 * reducer and `verdictFor`; this only carries events in and effects out.
 */

export interface PoolMachine {
  state: State
  dispatch: (event: Event) => void
}

export function usePoolMachine(): PoolMachine {
  const [state, setState] = useState(initialState)

  // The reducer runs against this rather than inside a `setState` updater.
  // React may call an updater twice -- StrictMode does so on purpose -- and an
  // updater that returned effects would send every effect twice: two quotes,
  // two wallet prompts. Reducing outside React, once per event, is what makes
  // "one event, one set of effects" true.
  const current = useRef(state)

  // Built once each. `ensureNetwork` remembers its verdict per client
  // instance, so a client built on every render would re-check the network
  // before every read, and the refresh would pay that round trip every tick.
  // `useState`'s initialiser rather than `useMemo`, because a memo is a cache
  // React is allowed to drop.
  const [algod] = useState(createAlgod)
  const [indexer] = useState(createIndexer)
  const signer = useSigner()
  const address = useWalletAddress()

  // Read when an effect starts, not when the event that caused it was
  // dispatched. A read that begins after the wallet changed must ask about the
  // wallet now connected, or it reports the previous wallet's seat under this
  // one's name.
  const runtime = useRef<Runtime>({ algod, indexer, signer, address, navigate })
  // A layout effect so it is current before any passive effect below runs --
  // in particular before the wallet effect dispatches, whose read needs the
  // address that has just changed.
  useLayoutEffect(() => {
    runtime.current = { algod, indexer, signer, address, navigate }
  }, [algod, indexer, signer, address])

  // Set in an effect, not at declaration, so StrictMode's rehearsal unmount
  // clears it and its remount sets it again. Checked on every dispatch: a
  // runner result that lands after the screen is gone must not set state on
  // an unmounted component, and must not start the effects it would have led
  // to either -- a late `QUOTE_RECEIVED` would otherwise still move the
  // address bar.
  //
  // A layout effect because every passive effect in the tree runs after every
  // layout effect. A child that dispatches from its own `useEffect` on mount
  // -- which runs before this component's passive effects -- would otherwise
  // find the flag still false and have its event silently dropped.
  const mounted = useRef(false)
  useLayoutEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const dispatch = useCallback((event: Event): void => {
    if (!mounted.current) return
    const { state: next, effects } = reduce(current.current, event)
    current.current = next
    setState(next)
    // After the state is recorded, so the events an effect produces are
    // reduced against the state this event left, never the one before it.
    for (const effect of effects) {
      // `run` never throws; every failure comes back as an event.
      void run(effect, runtime.current).then((events) => {
        for (const produced of events) dispatch(produced)
      })
    }
  }, [])

  // The wallet layer is the source of truth for which address is connected;
  // the reducer is told when that changes. Both events are inert when they
  // repeat what the reducer already knows, so StrictMode's second run and a
  // re-render that returns the same address cost nothing.
  useEffect(() => {
    if (address) dispatch({ type: 'WALLET_CONNECTED', address })
    else dispatch({ type: 'WALLET_DISCONNECTED' })
  }, [address, dispatch])

  useRefresh(refreshInterval(state), dispatch)

  return { state, dispatch }
}

/**
 * The refresh rule: re-read every `interval` milliseconds and whenever the tab
 * becomes visible, for as long as `interval` is not `null`.
 *
 * The mount read is not here. Opening a pool is what reads it, and the
 * router's `OPENED_POOL` already asks; a read here as well would be a second
 * request for the same answer on every page load.
 *
 * Exported so the timer's lifecycle can be tested with an interval set from the
 * first mount, which is the case StrictMode's rehearsal mount can double. A
 * pool machine always mounts with nothing on screen, so through
 * `usePoolMachine` alone that mount can never be observed polling.
 */
export function useRefresh(interval: number | null, dispatch: (event: Event) => void): void {
  useEffect(() => {
    // Keyed on the period alone, not on the state: a new state on every read
    // would otherwise tear the interval down and rebuild it each time,
    // restarting the period -- and a read that lands just under the period
    // would postpone the next one indefinitely. The period itself changes
    // only when the reason for it does.
    if (interval === null) return
    const refresh = () => dispatch({ type: 'READ_REQUESTED' })
    const timer = setInterval(refresh, interval)
    // A background tab's timers are throttled, sometimes to nothing, so a
    // buyer coming back to the tab is exactly when the screen is most likely
    // to be stale.
    const onVisibility = () => {
      if (document.visibilityState === 'visible') refresh()
    }
    document.addEventListener('visibilitychange', onVisibility)
    // Both go together, on unmount and whenever polling stops. StrictMode runs
    // this cleanup between its two mounts, which is what keeps that rehearsal
    // from leaving a second interval running.
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [interval, dispatch])
}
