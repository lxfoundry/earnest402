// @vitest-environment jsdom
//
// The hook that runs the pool machine: its refresh timer, its wallet wiring,
// and what it does with results that arrive after it is gone.
//
// The reducer is the real one -- it is what decides whether a pool is worth
// polling, and faking it would test a timer against a predicate nobody ships.
// What is faked is the effect executor, `run`: the real one reaches algod,
// the indexer and the wallet, and jsdom's cross-realm TextEncoder would break
// algosdk on the first box name it encoded. Each fake result is an event the
// real runner can produce, so the machine is driven exactly as it would be.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StrictMode } from 'react'
import { act, cleanup, render } from '@testing-library/react'
import type { Agreement } from '../src/chain/decode'
import {
  PROMPT_REFRESH_INTERVAL_MS,
  REFRESH_INTERVAL_MS,
  shouldPoll,
  type Effect,
  type Event,
} from '../src/pool/machine'
import type { Runtime } from '../src/pool/runner'
import type { Circumstances, SeatQuote } from '../src/pool/view'

const runMock = vi.hoisted(() => vi.fn())
const useWalletMock = vi.hoisted(() => vi.fn())

vi.mock('../src/pool/runner', () => ({ run: runMock }))
vi.mock('@txnlab/use-wallet-react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@txnlab/use-wallet-react')>()
  return { ...actual, useWallet: useWalletMock }
})

const { usePoolMachine, useRefresh } = await import('../src/pool/usePoolMachine')
const { resetConfigForTests } = await import('../src/config')

type Machine = ReturnType<typeof usePoolMachine>

const WALLET = 'IUHCGQZG5KF2T5BMWXZFCH64T6ZAEU5NHTEWRHMG2644WK55PZ7Z34FAKM'
const POOL = 25n
const DEADLINE = 1789651281n

const record: Agreement = {
  condition: 0,
  state: 'OPEN',
  deadline: DEADLINE,
  sharePrice: 5_000_000n,
  minSeats: 5,
  maxSeats: 5,
  seats: 2,
  refundCursor: 0,
  unclaimedSeats: 0,
  totalHeld: 10_000_000n,
  commitSha256: 'fa'.repeat(32),
  beneficiary: 'B'.repeat(58),
  verifier: 'V'.repeat(58),
  creator: 'C'.repeat(58),
}

const OPEN: Circumstances = { record, seat: null, chainNow: DEADLINE - 60n, canPayFee: true }
// Closed with no history: terminal, so nothing further is worth reading.
const GONE: Circumstances = { record: null, seat: null, chainNow: DEADLINE, canPayFee: true }

/** What the fake chain currently says about every pool. */
let chain: Circumstances = OPEN

/**
 * A fake runner that answers reads from `chain` and everything else with
 * nothing. Each answer is tagged with the wallet its effect asked about, as the
 * real runner's is.
 */
async function answerReads(effect: Effect): Promise<Event[]> {
  if (effect.type !== 'READ_POOL') return []
  return [
    {
      type: 'READ_RECEIVED',
      agreementId: effect.agreementId,
      address: effect.address,
      circumstances: chain,
    },
  ]
}

/**
 * Let the fake runner's promises settle and their events dispatch. A
 * `setTimeout` rather than a microtask, because a result is dispatched two
 * promise hops after its effect starts; `setTimeout` itself is left unfaked.
 */
const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0))

let machine: Machine | undefined

function Harness() {
  machine = usePoolMachine()
  return null
}

const current = (): Machine => {
  if (!machine) throw new Error('the harness has not rendered')
  return machine
}

async function dispatch(event: Event) {
  await act(async () => {
    current().dispatch(event)
    await settle()
  })
}

async function tick(ms = REFRESH_INTERVAL_MS) {
  await act(async () => {
    vi.advanceTimersByTime(ms)
    await settle()
  })
}

function readsOf(agreementId: bigint) {
  return runMock.mock.calls.filter(
    ([effect]) =>
      (effect as Effect).type === 'READ_POOL' &&
      (effect as Extract<Effect, { type: 'READ_POOL' }>).agreementId === agreementId,
  )
}

beforeEach(() => {
  vi.stubEnv('VITE_NETWORK', 'algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=')
  vi.stubEnv('VITE_ASSET_ID', '10458941')
  vi.stubEnv('VITE_APP_ID', '741000001')
  vi.stubEnv('VITE_FACILITATOR_URL', 'https://facilitator.example')
  vi.stubEnv('VITE_RESOURCE_HOST', 'https://earnest.example')
  vi.stubEnv('VITE_MAX_FILE_BYTES', '16777216')
  vi.stubEnv('VITE_ALGOD_URL', 'https://algod.example')
  vi.stubEnv('VITE_INDEXER_URL', 'https://indexer.example')
  vi.stubEnv('VITE_IPFS_GATEWAY', 'https://ipfs.example')
  resetConfigForTests()
  // Only the interval is faked, so the count below is intervals and nothing
  // else, and React's own scheduling runs on real timers.
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  chain = OPEN
  machine = undefined
  runMock.mockReset()
  runMock.mockImplementation(answerReads)
  useWalletMock.mockReset()
  useWalletMock.mockReturnValue({ activeAddress: null, signTransactions: vi.fn() })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllEnvs()
  resetConfigForTests()
})

describe('the refresh runs while there is something to learn, and stops when there is not', () => {
  it('keeps one interval while shouldPoll holds and clears it when it turns false', async () => {
    render(<Harness />)
    // The entry screen reads no pool.
    expect(shouldPoll(current().state)).toBe(false)
    expect(vi.getTimerCount()).toBe(0)

    await dispatch({ type: 'OPENED_POOL', agreementId: POOL })
    expect(current().state.circumstances).toBe(OPEN)
    expect(shouldPoll(current().state)).toBe(true)
    expect(vi.getTimerCount()).toBe(1)

    // Each tick is a read of the pool on screen, and a read that changes
    // nothing terminal leaves the same single interval running.
    runMock.mockClear()
    await tick()
    expect(readsOf(POOL)).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(1)

    // The pool closes. The read that says so is the last one worth making.
    chain = GONE
    await tick()
    expect(shouldPoll(current().state)).toBe(false)
    expect(vi.getTimerCount()).toBe(0)

    runMock.mockClear()
    await tick()
    expect(runMock).not.toHaveBeenCalled()
  })

  it('re-reads within seconds while a paid seat has not appeared, and relaxes once it has', async () => {
    render(<Harness />)
    await dispatch({ type: 'OPENED_POOL', agreementId: POOL })
    // Settled into the pool on screen, and the node has not caught up: the
    // fake chain still shows no seat.
    await dispatch({
      type: 'SETTLED',
      agreementId: POOL,
      seatsTotal: 5,
      deadline: Number(DEADLINE),
      commitSha256: 'fa'.repeat(32),
    })
    expect(current().state.awaitingSeat).toBe(POOL)

    runMock.mockClear()
    await tick(PROMPT_REFRESH_INTERVAL_MS)
    expect(readsOf(POOL)).toHaveLength(1)
    expect(vi.getTimerCount()).toBe(1)

    // The seat appears, and with it the reason to hurry goes.
    chain = { ...OPEN, seat: { index: 2, payer: WALLET, amount: 5_000_000n, status: 'owed' } }
    await tick(PROMPT_REFRESH_INTERVAL_MS)
    expect(current().state.awaitingSeat).toBeUndefined()
    expect(vi.getTimerCount()).toBe(1)

    runMock.mockClear()
    await tick(PROMPT_REFRESH_INTERVAL_MS)
    expect(readsOf(POOL)).toHaveLength(0)
    await tick(REFRESH_INTERVAL_MS - PROMPT_REFRESH_INTERVAL_MS)
    expect(readsOf(POOL)).toHaveLength(1)
  })

  it('leaves exactly one interval under StrictMode', async () => {
    render(
      <StrictMode>
        <Harness />
      </StrictMode>,
    )
    await dispatch({ type: 'OPENED_POOL', agreementId: POOL })
    expect(shouldPoll(current().state)).toBe(true)
    expect(vi.getTimerCount()).toBe(1)
  })
})

describe('the timer survives a rehearsal mount without doubling', () => {
  function Refresh({
    interval,
    onTick,
  }: {
    interval: number | null
    onTick: (event: Event) => void
  }) {
    useRefresh(interval, onTick)
    return null
  }

  it('leaves one interval after StrictMode mounts it polling, and ticks once per period', () => {
    // A pool machine always mounts with nothing on screen, so this is the one
    // way to watch StrictMode's mount-unmount-mount happen with polling on.
    const onTick = vi.fn()
    const { unmount } = render(
      <StrictMode>
        <Refresh interval={REFRESH_INTERVAL_MS} onTick={onTick} />
      </StrictMode>,
    )
    expect(vi.getTimerCount()).toBe(1)
    vi.advanceTimersByTime(REFRESH_INTERVAL_MS)
    expect(onTick).toHaveBeenCalledTimes(1)
    expect(onTick).toHaveBeenCalledWith({ type: 'READ_REQUESTED' })
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('reads when the tab becomes visible, and stops listening once polling stops', () => {
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    const onTick = vi.fn()
    const { rerender } = render(<Refresh interval={REFRESH_INTERVAL_MS} onTick={onTick} />)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(onTick).toHaveBeenCalledTimes(1)

    rerender(<Refresh interval={null} onTick={onTick} />)
    expect(vi.getTimerCount()).toBe(0)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(onTick).toHaveBeenCalledTimes(1)
  })
})

describe('a wallet change reaches the machine and the runtime together', () => {
  it('reads the pool again for a newly connected wallet, and signs with it', async () => {
    const { rerender } = render(<Harness />)
    await dispatch({ type: 'OPENED_POOL', agreementId: POOL })

    runMock.mockClear()
    useWalletMock.mockReturnValue({ activeAddress: WALLET, signTransactions: vi.fn() })
    await act(async () => {
      rerender(<Harness />)
      await settle()
    })

    expect(current().state.wallet).toBe(WALLET)
    const reads = readsOf(POOL)
    expect(reads).toHaveLength(1)
    // The reading is asked for the wallet now connected, and its answer is
    // heard: a reading for any other wallet would be dropped on arrival.
    expect((reads[0]![0] as Extract<Effect, { type: 'READ_POOL' }>).address).toBe(WALLET)
    expect(current().state.circumstances).toBe(OPEN)
    // And the runtime the effect ran with already holds it. Reads no longer
    // depend on that, but a purchase or a refund call signs with it, and a
    // runtime captured before the wallet effect would sign for nobody.
    expect((reads[0]![1] as Runtime).address).toBe(WALLET)
  })
})

describe('a result that lands after the machine is gone changes nothing', () => {
  it('neither sets state nor starts the effects a late event would lead to', async () => {
    let answer: (events: Event[]) => void = () => {}
    runMock.mockImplementation((effect: Effect) =>
      effect.type === 'REQUEST_QUOTE'
        ? new Promise<Event[]>((resolve) => {
            answer = resolve
          })
        : answerReads(effect),
    )
    const { unmount } = render(<Harness />)
    await dispatch({ type: 'QUOTE_REQUESTED' })
    expect(runMock).toHaveBeenCalledTimes(1)
    unmount()

    const quote: SeatQuote = {
      agreementId: POOL,
      amount: 5_000_000n,
      seatsTotal: 5,
      seatsLeft: 3,
      deadline: Number(DEADLINE),
      commitSha256: 'fa'.repeat(32),
    }
    await act(async () => {
      answer([{ type: 'QUOTE_RECEIVED', quote }])
      await settle()
    })
    // Reduced, this quote would navigate to its pool and read it: two more
    // effects. Dropped, it produces none.
    expect(runMock).toHaveBeenCalledTimes(1)
  })
})
