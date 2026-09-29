import { describe, expect, it } from 'vitest'
import {
  type Event,
  type State,
  initialState,
  reduce,
} from '../src/machine'

const quote = {
  agreementId: 42n,
  amount: 10000n,
  payTo: 'IUHCGQZG5KF2T5BMWXZFCH64T6ZAEU5NHTEWRHMG2644WK55PZ7Z34FAKM',
  feePayer: 'KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M',
  expiresAt: 1_756_000_120,
}

const hashed = { sha256: 'a'.repeat(64), size: 1024 }

/** Drive the machine through a list of events, returning the final state. */
function run(events: Event[], from: State = initialState()): State {
  return events.reduce((state, event) => reduce(state, event).state, from)
}

function effectsOf(state: State, event: Event) {
  return reduce(state, event).effects.map((e) => e.type)
}

describe('the resting states', () => {
  it('starts with no wallet connected', () => {
    expect(initialState().status).toBe('CONNECT')
  })

  it('hashes on file selection even with no wallet connected', () => {
    // Hash early, quote late. Hashing is local and free; the unpaid pass
    // commits an agreement and starts the funding window, so it must not be
    // spent on a wallet handshake.
    const state = initialState()
    expect(effectsOf(state, { type: 'FILE_SELECTED', name: 'f', size: 1024 })).toContain(
      'HASH_FILE',
    )
  })

  it('holds at CONNECT with the digest ready', () => {
    const state = run([
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
    ])
    expect(state.status).toBe('CONNECT')
    expect(state.file?.sha256).toBe(hashed.sha256)
  })

  it('moves to CHOOSE when a wallet connects with no file', () => {
    expect(run([{ type: 'WALLET_CONNECTED', address: 'A' }]).status).toBe('CHOOSE')
  })

  it('quotes only once both a wallet and a digest are in hand', () => {
    const ready = run([
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'WALLET_CONNECTED', address: 'A' },
    ])
    expect(ready.status).toBe('READY')
    expect(effectsOf(ready, { type: 'QUOTE_REQUESTED' })).toContain('REQUEST_QUOTE')
  })
})

describe('the money-losing assertion', () => {
  // Named explicitly because a bug here costs a deposit every time it fires.
  it('never re-quotes while the agreement is still open', () => {
    const confirming = run([
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
    ])
    expect(confirming.status).toBe('CONFIRM')
    expect(effectsOf(confirming, { type: 'QUOTE_REQUESTED' })).not.toContain(
      'REQUEST_QUOTE',
    )
  })

  it('drops an open quote when a different file is chosen', () => {
    // Without this, choosing a second file at CONFIRM left the machine at
    // READY holding the new digest beside the OLD agreement -- and quoting
    // from there parked a second deposit against bytes the first agreement's
    // commit_hash can never match.
    const swapped = run([
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'FILE_SELECTED', name: 'first', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
      { type: 'FILE_SELECTED', name: 'second', size: 2048 },
      { type: 'FILE_HASHED', sha256: 'b'.repeat(64), size: 2048 },
    ])
    expect(swapped.status).toBe('READY')
    expect(swapped.file?.sha256).toBe('b'.repeat(64))
    expect(swapped.quote).toBeUndefined()
  })

  it('carries nothing from a finished job into the next purchase', () => {
    const afterDone = run([
      { type: 'PAGE_OPENED_WITH_JOB', jobId: 'j1' },
      {
        type: 'STATUS_POLLED',
        state: 'RELEASED',
        cid: 'bafy',
        sha256: hashed.sha256,
        size: 1024,
      },
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'FILE_SELECTED', name: 'next', size: 2048 },
      { type: 'FILE_HASHED', sha256: 'c'.repeat(64), size: 2048 },
    ])
    expect(afterDone.jobId).toBeUndefined()
    expect(afterDone.cid).toBeUndefined()
    expect(afterDone.jobState).toBeUndefined()
  })

  it('ignores a file chosen while an outcome is in flight or money is committed', () => {
    // QUOTING and RESOLVING have a result coming that describes the previous
    // file; UPLOADING and WORKING hold a funded agreement these bytes would
    // not match.
    const inFlight: State[] = [
      { status: 'QUOTING', wallet: 'A', file: { ...hashed, haveBytes: true } },
      { status: 'RESOLVING', wallet: 'A', file: { ...hashed, haveBytes: true } },
      { status: 'UPLOADING', wallet: 'A', jobId: 'j1', file: { ...hashed, haveBytes: true } },
      { status: 'WORKING', wallet: 'A', jobId: 'j1', file: { ...hashed, haveBytes: true } },
    ]
    for (const state of inFlight) {
      const { state: next, effects } = reduce(state, {
        type: 'FILE_SELECTED',
        name: 'other',
        size: 2048,
      })
      expect(effects).toEqual([])
      expect(next).toEqual(state)
    }
  })

  it('never re-quotes while a funded job is still owed its bytes', () => {
    // The buyer has already paid and the escrow is running against a
    // deadline. Quoting again here buys a second pin of the same bytes and
    // parks a second deposit while the first delivery is still outstanding.
    const owed = run([
      { type: 'PAGE_OPENED_WITH_JOB', jobId: 'j1' },
      { type: 'STATUS_POLLED', state: 'FUNDED', sha256: hashed.sha256, size: 1024 },
      { type: 'WALLET_CONNECTED', address: 'A' },
    ])
    expect(owed.status).toBe('NEED_FILE')
    expect(effectsOf(owed, { type: 'REQUOTE' })).not.toContain('REQUEST_QUOTE')
  })

  it('retries the payment against the open agreement rather than re-quoting', () => {
    // QUOTED from the hash query means the seat is still ours: reusing the
    // agreement costs nothing, re-quoting parks a second deposit.
    const deciding = run([
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
      { type: 'SETTLE_FAILED', error: 'wallet rejected' },
      { type: 'HASH_RESOLVED', result: { state: 'QUOTED', agreementId: 42n } },
    ])
    expect(deciding.status).toBe('DECIDE')
    expect(deciding.decision).toBe('RETRY_PAYMENT')
    expect(effectsOf(deciding, { type: 'RETRY_PAYMENT' })).toEqual(['BUILD_AND_SIGN'])
  })

  it('issues no quote on mount, on connect, or on hash', () => {
    // "No speculative unpaid passes": each one commits an agreement on chain.
    const events: Event[] = [
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
    ]
    let state = initialState()
    for (const event of events) {
      const next = reduce(state, event)
      expect(next.effects.map((e) => e.type)).not.toContain('REQUEST_QUOTE')
      state = next.state
    }
  })
})

describe('settlement and delivery', () => {
  // Through SETTLE_REQUESTED, which is the event that records which agreement
  // the settlement belongs to. Still CONFIRM: it starts a settlement, it does
  // not complete one.
  const confirming = () =>
    run([
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
      { type: 'SETTLE_REQUESTED' },
    ])

  it('uploads the bytes once settlement returns a jobId', () => {
    const settled = reduce(confirming(), {
      type: 'SETTLED',
      jobId: 'j1',
      agreementId: 42n,
      deadline: 1_756_000_600,
    })
    expect(settled.state.status).toBe('UPLOADING')
    expect(settled.effects.map((e) => e.type)).toEqual(['NAVIGATE', 'UPLOAD_BYTES'])
  })

  it('asks for the file again when it has a job but no bytes', () => {
    // The reload case: the job survives in the URL, the File does not.
    const reloaded = run([
      { type: 'PAGE_OPENED_WITH_JOB', jobId: 'j1' },
      { type: 'STATUS_POLLED', state: 'FUNDED' },
    ])
    expect(reloaded.status).toBe('NEED_FILE')
  })

  it('accepts the same file back and resumes the upload', () => {
    const needing = run([
      { type: 'PAGE_OPENED_WITH_JOB', jobId: 'j1' },
      { type: 'STATUS_POLLED', state: 'FUNDED', sha256: hashed.sha256, size: 1024 },
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
    ])
    expect(needing.status).toBe('UPLOADING')
  })

  it('refuses a different file against an existing job', () => {
    const wrong = run([
      { type: 'PAGE_OPENED_WITH_JOB', jobId: 'j1' },
      { type: 'STATUS_POLLED', state: 'FUNDED', sha256: hashed.sha256, size: 1024 },
      { type: 'FILE_SELECTED', name: 'other', size: 2048 },
      { type: 'FILE_HASHED', sha256: 'b'.repeat(64), size: 2048 },
    ])
    expect(wrong.status).toBe('NEED_FILE')
    expect(wrong.error).toMatch(/different file/i)
  })

  it('polls while the job is working', () => {
    for (const jobState of ['FUNDED', 'RECEIVED', 'PINNED'] as const) {
      const working = run([
        { type: 'PAGE_OPENED_WITH_JOB', jobId: 'j1' },
        { type: 'STATUS_POLLED', state: jobState, sha256: hashed.sha256, size: 1024 },
      ])
      expect(['WORKING', 'NEED_FILE']).toContain(working.status)
    }
  })

  it('ends at DONE with the CID the server returned', () => {
    const done = run([
      { type: 'PAGE_OPENED_WITH_JOB', jobId: 'j1' },
      {
        type: 'STATUS_POLLED',
        state: 'RELEASED',
        cid: 'bafy...',
        sha256: hashed.sha256,
        size: 1024,
      },
    ])
    expect(done.status).toBe('DONE')
    expect(done.cid).toBe('bafy...')
  })

  for (const terminal of ['ABANDONED', 'REFUNDED'] as const) {
    it(`ends at ENDED on ${terminal}`, () => {
      const ended = run([
        { type: 'PAGE_OPENED_WITH_JOB', jobId: 'j1' },
        { type: 'STATUS_POLLED', state: terminal, sha256: hashed.sha256, size: 1024 },
      ])
      expect(ended.status).toBe('ENDED')
    })
  }
})

describe('the paths a code review found', () => {
  const settled = () =>
    run([
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
      { type: 'SETTLE_REQUESTED' },
      { type: 'SETTLED', jobId: 'j1', agreementId: 42n, deadline: 9 },
    ])

  it('does not quote a fabricated size after a reload', () => {
    // STATUS_POLLED once invented `size: 0` for a page restored from its URL.
    // ENDED offers a re-quote, so that zero travelled into an unpaid pass:
    // a deposit parked against a key the server's (sha256, size) idempotency
    // could never match, priced against no bytes, by a buyer holding none.
    const ended = run([
      { type: 'PAGE_OPENED_WITH_JOB', jobId: 'j1' },
      { type: 'STATUS_POLLED', state: 'REFUNDED', sha256: hashed.sha256, size: 1024 },
      { type: 'WALLET_CONNECTED', address: 'A' },
    ])
    expect(ended.status).toBe('ENDED')
    expect(ended.file?.size).toBe(1024)
    expect(ended.file?.haveBytes).toBe(false)
    // No bytes in hand, so no quote until the file is chosen again.
    expect(effectsOf(ended, { type: 'REQUOTE' })).not.toContain('REQUEST_QUOTE')
  })

  it('ignores a funding-window timer that outlives the settlement', () => {
    // The countdown belongs to CONFIRM and is owned by the runner. Left
    // running, it fired while the job was UPLOADING; the hash query answered
    // FUNDED -- correctly, we funded it -- and the fork read our own buyer as
    // a stranger holding the seat, offering to sell them a second copy.
    const owned = settled()
    expect(owned.status).toBe('UPLOADING')
    expect(reduce(owned, { type: 'WINDOW_ELAPSED' }).state.status).toBe('UPLOADING')
    expect(reduce(owned, { type: 'SETTLE_FAILED', error: 'x' }).state.status).toBe(
      'UPLOADING',
    )
  })

  it('ignores a settlement that lands after the file changed under it', () => {
    const swapped = run(
      [
        { type: 'FILE_SELECTED', name: 'other', size: 2048 },
        { type: 'FILE_HASHED', sha256: 'b'.repeat(64), size: 2048 },
      ],
      run([
        { type: 'WALLET_CONNECTED', address: 'A' },
        { type: 'FILE_SELECTED', name: 'f', size: 1024 },
        { type: 'FILE_HASHED', ...hashed },
        { type: 'QUOTE_REQUESTED' },
        { type: 'QUOTE_RECEIVED', ...quote },
      ]),
    )
    const late = reduce(swapped, {
      type: 'SETTLED',
      jobId: 'j9',
      agreementId: 42n,
      deadline: 9,
    })
    expect(late.state.status).not.toBe('UPLOADING')
    expect(late.effects).toEqual([])
  })

  it('does not let RETRY_PAYMENT fire twice or outlive its screen', () => {
    const deciding = run([
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
      { type: 'SETTLE_FAILED', error: 'rejected' },
      { type: 'HASH_RESOLVED', result: { state: 'QUOTED', agreementId: 42n } },
    ])
    expect(effectsOf(deciding, { type: 'RETRY_PAYMENT' })).toEqual(['BUILD_AND_SIGN'])
    // The second click has nothing left to act on.
    const retried = reduce(deciding, { type: 'RETRY_PAYMENT' }).state
    expect(effectsOf(retried, { type: 'RETRY_PAYMENT' })).toEqual([])
    // And it cannot reach across a completed settlement.
    expect(effectsOf(settled(), { type: 'RETRY_PAYMENT' })).toEqual([])
  })

  it('keeps no quote once the wallet disconnects', () => {
    const reconnected = run([
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
      { type: 'WALLET_DISCONNECTED' },
      { type: 'WALLET_CONNECTED', address: 'A' },
    ])
    expect(reconnected.quote).toBeUndefined()
  })

  it('refuses a file at NEED_FILE when it has no digest to check it against', () => {
    const blind = reduce(
      { status: 'NEED_FILE', wallet: 'A', jobId: 'j1' },
      { type: 'FILE_HASHED', ...hashed },
    )
    expect(blind.state.status).toBe('NEED_FILE')
    expect(blind.effects).toEqual([])
  })
})

describe('the resolve fork', () => {
  const confirming = (): State =>
    run([
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
      { type: 'SETTLE_REQUESTED' },
    ])

  const failed = (): State =>
    reduce(confirming(), { type: 'SETTLE_FAILED', error: 'network' }).state

  it('always asks the hash query first, and never shows a raw error', () => {
    // The client does not classify the failure; it asks what happened.
    const { state, effects } = reduce(confirming(), {
      type: 'SETTLE_FAILED',
      error: 'TypeError: Failed to fetch',
    })
    expect(state.status).toBe('RESOLVING')
    expect(effects.map((e) => e.type)).toContain('QUERY_HASH')
  })

  const forks: Array<[string, unknown, string]> = [
    ['QUOTED', { state: 'QUOTED', agreementId: 42n }, 'RETRY_PAYMENT'],
    ['FUNDED', { state: 'FUNDED', agreementId: 42n }, 'BUY_YOUR_OWN'],
    ['RECEIVED', { state: 'RECEIVED', agreementId: 42n }, 'BUY_YOUR_OWN'],
    ['PINNED', { state: 'PINNED', agreementId: 42n }, 'BUY_YOUR_OWN'],
    ['RELEASED', { state: 'RELEASED', agreementId: 42n, cid: 'bafy' }, 'SHOW_CID'],
    ['ABANDONED', { state: 'ABANDONED', agreementId: 42n }, 'REQUOTE'],
    ['REFUNDED', { state: 'REFUNDED', agreementId: 42n }, 'REQUOTE'],
    ['not found', null, 'REQUOTE'],
  ]

  for (const [name, result, decision] of forks) {
    it(`offers ${decision} when the hash query says ${name}`, () => {
      const state = reduce(failed(), {
        type: 'HASH_RESOLVED',
        result: result as never,
      }).state
      expect(state.status).toBe('DECIDE')
      expect(state.decision).toBe(decision)
    })
  }

  it('falls back to re-quoting when the hash query itself fails', () => {
    // Bounded by the server's recent-release window, so a 404 and an outage
    // both land here. Re-quoting is correct in either case.
    const state = reduce(failed(), { type: 'HASH_QUERY_FAILED' }).state
    expect(state.status).toBe('DECIDE')
    expect(state.decision).toBe('REQUOTE')
  })
})

describe('the funding window', () => {
  it('counts down from the quote', () => {
    const confirming = run([
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
    ])
    expect(confirming.quote?.expiresAt).toBe(quote.expiresAt)
  })

  it('routes a lapsed window through the hash query like any other failure', () => {
    const confirming = run([
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
    ])
    const lapsed = reduce(confirming, { type: 'WINDOW_ELAPSED' })
    expect(lapsed.state.status).toBe('RESOLVING')
    expect(lapsed.effects.map((e) => e.type)).toContain('QUERY_HASH')
  })
})

describe('exhaustively', () => {
  const everyEvent: Event[] = [
    { type: 'WALLET_CONNECTED', address: 'A' },
    { type: 'WALLET_DISCONNECTED' },
    { type: 'FILE_SELECTED', name: 'f', size: 1024 },
    { type: 'FILE_HASHED', ...hashed },
    { type: 'FILE_REJECTED', reason: 'too large' },
    { type: 'QUOTE_REQUESTED' },
    { type: 'QUOTE_RECEIVED', ...quote },
    { type: 'QUOTE_FAILED', error: 'boom' },
    { type: 'SETTLE_REQUESTED' },
    { type: 'SETTLED', jobId: 'j', agreementId: 42n, deadline: 1 },
    { type: 'SETTLE_FAILED', error: 'boom' },
    { type: 'HASH_RESOLVED', result: { state: 'QUOTED', agreementId: 42n } },
    { type: 'HASH_QUERY_FAILED' },
    { type: 'UPLOADED', cid: 'bafy', gatewayUrl: 'https://g/bafy' },
    { type: 'UPLOAD_FAILED', error: 'boom' },
    { type: 'STATUS_POLLED', state: 'FUNDED', sha256: hashed.sha256, size: 1024 },
    { type: 'WINDOW_ELAPSED' },
    { type: 'PAGE_OPENED_WITH_JOB', jobId: 'j' },
    { type: 'RETRY_PAYMENT' },
    { type: 'REQUOTE' },
  ]

  /**
   * Every reachable state, discovered by walking the machine rather than
   * listed by hand -- a hand-written list quietly stops covering new states.
   *
   * States are keyed by what actually gates a transition: the status, and
   * which fields are present rather than what they contain. Keying on the
   * whole state does not terminate -- the walk becomes combinatorial in the
   * payload strings (error text, CIDs, job ids, deadlines), and the worker
   * runs out of memory instead of failing. Presence is what every branch in
   * the reducer actually reads.
   */
  function walk(): State[] {
    const key = (s: State) =>
      [
        s.status,
        s.wallet ? 'w' : '-',
        s.file ? (s.file.haveBytes ? 'f+' : 'f-') : '-',
        s.quote ? 'q' : '-',
        s.jobId ? 'j' : '-',
        s.jobState ?? '-',
        s.decision ?? '-',
        s.cid ? 'c' : '-',
      ].join('|')

    const seen = new Map<string, State>()
    const queue: State[] = [initialState()]
    while (queue.length) {
      const state = queue.shift()!
      const k = key(state)
      if (seen.has(k)) continue
      seen.set(k, state)
      for (const event of everyEvent) queue.push(reduce(state, event).state)
    }
    return [...seen.values()]
  }

  // Computed once: four tests iterate it, and the walk is the expensive part.
  const reachableStates = walk()
  const reachable = () => reachableStates

  it('never throws, for any event in any reachable state', () => {
    for (const state of reachable()) {
      for (const event of everyEvent) {
        expect(() => reduce(state, event)).not.toThrow()
      }
    }
  })

  it('always returns a state with a known status', () => {
    const known = new Set([
      'CONNECT', 'CHOOSE', 'READY', 'QUOTING', 'CONFIRM', 'UPLOADING',
      'WORKING', 'DONE', 'ENDED', 'RESOLVING', 'DECIDE', 'NEED_FILE',
    ])
    for (const state of reachable()) {
      for (const event of everyEvent) {
        expect(known).toContain(reduce(state, event).state.status)
      }
    }
  })

  it('emits REQUEST_QUOTE only from a quotable state, and only on request', () => {
    for (const state of reachable()) {
      for (const event of everyEvent) {
        const { effects } = reduce(state, event)
        if (effects.some((e) => e.type === 'REQUEST_QUOTE')) {
          expect(event.type === 'QUOTE_REQUESTED' || event.type === 'REQUOTE').toBe(true)
          expect(['READY', 'DECIDE', 'ENDED', 'DONE']).toContain(state.status)
        }
      }
    }
  })

  it('never quotes from a state that already holds an agreement', () => {
    // The money rule, stated as a property rather than as a list of states.
    // An unpaid pass commits an agreement on chain and parks a deposit, so it
    // is correct only when no agreement is currently open -- and only with
    // the bytes in hand, since a page restored from its URL knows the digest
    // but has nothing to deliver.
    //
    // A *terminal* job may still be in state: quoting from DONE or ENDED is
    // an ordinary second purchase, and the transition drops every job field
    // on the way into QUOTING. What must never be open is an unfunded quote
    // or a job still running.
    const TERMINAL = ['RELEASED', 'ABANDONED', 'REFUNDED']

    for (const state of reachable()) {
      for (const event of everyEvent) {
        const { state: next, effects } = reduce(state, event)
        const quoting = effects.find((e) => e.type === 'REQUEST_QUOTE')
        if (!quoting || quoting.type !== 'REQUEST_QUOTE') continue

        expect(state.quote, 'quoted while holding an open quote').toBeUndefined()
        if (state.jobId !== undefined) {
          expect(state.jobState, `quoted over live job ${state.jobId}`).toBeDefined()
          expect(TERMINAL, `quoted over live job ${state.jobId}`).toContain(
            state.jobState,
          )
        }
        expect(state.file?.haveBytes, 'quoted without the bytes').toBe(true)
        expect(quoting.sha256).toBe(state.file?.sha256)
        expect(quoting.size).toBe(state.file?.size)
        expect(quoting.size, 'quoted a fabricated size').toBeGreaterThan(0)

        // And the state it lands in carries nothing from the old purchase.
        expect(next.jobId, 'carried a job id into a new quote').toBeUndefined()
        expect(next.quote).toBeUndefined()
        expect(next.decision).toBeUndefined()
        expect(next.cid).toBeUndefined()
        // A settlement identity carried into a new purchase would let the
        // *old* agreement's 200 be accepted against the new one.
        expect(
          next.settlingAgreementId,
          'carried a settlement into a new quote',
        ).toBeUndefined()
      }
    }
  })

  it('reaches every state the spec names as resting', () => {
    const reached = new Set(reachable().map((s) => s.status))
    for (const status of ['CONNECT', 'CHOOSE', 'READY', 'CONFIRM', 'DECIDE', 'NEED_FILE']) {
      expect(reached).toContain(status)
    }
  })

  it('never rests at READY without a digest', () => {
    // READY means "connected, hashed, nothing requested yet", and it is the
    // only state from which a quote may be issued. A READY with no `file` is
    // a state claiming a digest it does not have -- reachable by rejecting an
    // oversized file after a valid one was already hashed, if a branch
    // computes its next status from the state it is replacing.
    for (const state of reachable()) {
      for (const event of everyEvent) {
        const next = reduce(state, event).state
        if (next.status === 'READY') expect(next.file).toBeDefined()
      }
    }
  })

  it('rejecting a file after a valid one drops back to CHOOSE', () => {
    const rejected = run([
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'FILE_SELECTED', name: 'good', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'FILE_REJECTED', reason: 'that file is too large' },
    ])
    expect(rejected.status).toBe('CHOOSE')
    expect(rejected.file).toBeUndefined()
    expect(rejected.error).toMatch(/too large/)
  })
})

describe('a settlement that lands after the machine gave up waiting', () => {
  // The funding-window countdown and the settlement race each other, and the
  // window is short: a wallet prompt, the facilitator and a round of chain
  // confirmation all fit inside it. When the timer wins, a status-guarded
  // SETTLED is dropped -- and the 200 it dropped was the proof of payment.
  const settling = (): State =>
    run([
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
      { type: 'SETTLE_REQUESTED' },
    ])

  const landing: Event = {
    type: 'SETTLED',
    jobId: 'j1',
    agreementId: 42n,
    deadline: 1_756_000_600,
  }

  it('takes the 200 even though the window elapsed first', () => {
    const lapsed = reduce(settling(), { type: 'WINDOW_ELAPSED' }).state
    expect(lapsed.status).toBe('RESOLVING')

    const { state, effects } = reduce(lapsed, landing)
    expect(state.status).toBe('UPLOADING')
    expect(state.jobId).toBe('j1')
    expect(effects.map((e) => e.type)).toEqual(['NAVIGATE', 'UPLOAD_BYTES'])
  })

  it('takes the 200 even after the fork offered to sell them a second copy', () => {
    // The whole failure, end to end: without this the buyer has funded an
    // escrow that will refund at its deadline, while the screen offers to buy
    // the pin they already own.
    const deciding = run(
      [
        { type: 'WINDOW_ELAPSED' },
        { type: 'HASH_RESOLVED', result: { state: 'FUNDED', agreementId: 42n } },
      ],
      settling(),
    )
    expect(deciding.status).toBe('DECIDE')
    expect(deciding.decision).toBe('BUY_YOUR_OWN')

    const { state } = reduce(deciding, landing)
    expect(state.status).toBe('UPLOADING')
    expect(state.jobId).toBe('j1')
    expect(state.decision).toBeUndefined()
  })

  it('asks for the file again when the bytes are gone', () => {
    const noBytes: State = {
      ...settling(),
      file: { ...hashed, haveBytes: false },
    }
    const { state, effects } = reduce(noBytes, landing)
    expect(state.status).toBe('NEED_FILE')
    expect(state.jobId).toBe('j1')
    // Navigated, but not asked to upload bytes it does not hold.
    expect(effects.map((e) => e.type)).toEqual(['NAVIGATE'])
  })

  it('still ignores a settlement for an agreement it walked away from', () => {
    // What the old status guard was protecting: a file swapped at CONFIRM
    // abandons the agreement, and the settlement that funds it must not
    // become an upload of bytes whose digest can never match its commit_hash.
    const swapped = run(
      [
        { type: 'FILE_SELECTED', name: 'other', size: 2048 },
        { type: 'FILE_HASHED', sha256: 'b'.repeat(64), size: 2048 },
      ],
      settling(),
    )
    expect(swapped.settlingAgreementId).toBeUndefined()
    expect(reduce(swapped, landing).state.status).not.toBe('UPLOADING')
  })

  it('ignores a settlement for an agreement it never started', () => {
    const { state } = reduce(settling(), { ...landing, agreementId: 43n })
    expect(state.status).toBe('CONFIRM')
    expect(state.jobId).toBeUndefined()
  })

  it('takes one 200 and not its duplicate', () => {
    const once = reduce(settling(), landing).state
    expect(once.settlingAgreementId).toBeUndefined()
    const twice = reduce(once, { ...landing, jobId: 'j2' }).state
    expect(twice.jobId).toBe('j1')
  })
})

describe('results that outlive the state that asked for them', () => {
  it('ignores an upload failure outside UPLOADING', () => {
    // Unguarded this reached NEED_FILE holding no job, and re-selecting the
    // file there asked for an upload against an undefined jobId.
    const ready = run([
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
    ])
    expect(ready.status).toBe('READY')
    expect(reduce(ready, { type: 'UPLOAD_FAILED', error: 'boom' }).state.status).toBe(
      'READY',
    )
  })

  it('refuses to upload against a job it cannot name', () => {
    const orphan: State = {
      status: 'NEED_FILE',
      wallet: 'A',
      file: { ...hashed, haveBytes: false },
    }
    const { state, effects } = reduce(orphan, { type: 'FILE_HASHED', ...hashed })
    expect(effects).toEqual([])
    expect(state.status).toBe('NEED_FILE')
    expect(state.error).toMatch(/reading this job back/)
  })

  it('ignores a poll result outside the states that hold a job', () => {
    // CONFIRM is the expensive one: it holds an unfunded quote and is in
    // MAY_QUOTE, so a RELEASED poll landing there reported DONE over a live
    // quote and the next purchase started still holding it.
    const confirming = run([
      { type: 'WALLET_CONNECTED', address: 'A' },
      { type: 'FILE_SELECTED', name: 'f', size: 1024 },
      { type: 'FILE_HASHED', ...hashed },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', ...quote },
    ])
    const polled = reduce(confirming, { type: 'STATUS_POLLED', state: 'RELEASED' }).state
    expect(polled.status).toBe('CONFIRM')
    expect(polled.quote).toBeDefined()
  })

  it('still takes a poll result while the job is running', () => {
    const working = run([
      { type: 'PAGE_OPENED_WITH_JOB', jobId: 'j1' },
      { type: 'STATUS_POLLED', state: 'RELEASED', cid: 'bafy' },
    ])
    expect(working.status).toBe('DONE')
    expect(working.cid).toBe('bafy')
  })
})
