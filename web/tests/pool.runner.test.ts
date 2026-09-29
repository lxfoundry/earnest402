import algosdk from 'algosdk'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SELECTORS } from '../src/chain/selectors'
import { resetBrandForTests, resetConfigForTests } from '../src/config'
import type { Effect, Event } from '../src/pool/machine'
import { resetQuotedRoutesForTests, run, type Runtime } from '../src/pool/runner'
import type { Action, SeatQuote } from '../src/pool/view'
import type { Signer } from '../src/payment/schemeClient'
import golden from './golden/escrow-boxes.json'

/**
 * The runner's interesting assertions are its failure mappings, because they
 * are where a wrong answer costs money: a payment reported as failed that in
 * fact settled, a closed edition reported as a broken client, an unreachable
 * node reported as a stale pool.
 *
 * Nothing under test is mocked. algod and the indexer are hand-written fakes
 * in the shape `read.test.ts` uses, and the paid route is a stubbed `fetch`
 * in the shape `api.test.ts` uses, so every real module between the runner
 * and the wire -- the decoders, the network guard, the group builder, the
 * 402 parser -- runs for real.
 */

const TESTNET = 'algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI='
const MAINNET = 'algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8='
const APP = 771795120n
const ASSET = 10458941n
const PAY_TO = algosdk.getApplicationAddress(APP).toString()
const FEE_PAYER = algosdk.encodeAddress(new Uint8Array(32).fill(0x55))

beforeEach(() => {
  vi.stubEnv('VITE_NETWORK', TESTNET)
  vi.stubEnv('VITE_ASSET_ID', String(ASSET))
  vi.stubEnv('VITE_APP_ID', String(APP))
  vi.stubEnv('VITE_FACILITATOR_URL', 'https://facilitator.example')
  vi.stubEnv('VITE_RESOURCE_HOST', 'https://earnest.example')
  vi.stubEnv('VITE_MAX_FILE_BYTES', '16777216')
  vi.stubEnv('VITE_ALGOD_URL', 'https://algod.example')
  vi.stubEnv('VITE_INDEXER_URL', 'https://indexer.example')
  vi.stubEnv('VITE_IPFS_GATEWAY', 'https://ipfs.example')
  // The index, whatever the machine's environment says: the brand decides the
  // words the runner writes and which release notes it reads.
  vi.stubEnv('VITE_BRAND', '')
  vi.stubEnv('VITE_OFFER_B64', '')
  resetConfigForTests()
  resetBrandForTests()
  resetQuotedRoutesForTests()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  resetConfigForTests()
  resetBrandForTests()
  resetQuotedRoutesForTests()
})

const vector = (name: string) => golden.vectors.find((v) => v.name === name)!
/** Agreement 25: open, five seats at 0.10 USDC, seat 0 sold to `HOLDER`. */
const OPEN = vector('open-pool-one-seat-sold')
/** Agreement 26: refunding, seat 0 paid back, seat 1 (`SKIPPED`) still owed. */
const REFUNDING = vector('refunding-one-paid-one-skipped')
/** Agreement 28: expired, two seats owed, cursor at zero. */
const EXPIRED = vector('expired-pool')
/** Agreement 29: refunded, every seat paid, nothing held -- closable. */
const REFUNDED = vector('refunded-pool')
/** Agreement 27: released, every seat sold, nothing held. */
const RELEASED = vector('released-pool')

const HOLDER = OPEN.roster.expected[0]!.payer
const SKIPPED = REFUNDING.roster.expected[1]!.payer
const STRANGER = algosdk.encodeAddress(new Uint8Array(32).fill(0x77))

/** Every box in the vectors, addressed the way algod addresses them. */
const ALL_BOXES = new Map<string, string>(
  [OPEN, REFUNDING, EXPIRED, REFUNDED, RELEASED].flatMap((v) => [
    [v.boxNames.agreement, v.agreement.raw],
    [v.boxNames.roster, v.roster.raw],
  ]),
)

/** The shape algosdk throws on a non-200: a plain error carrying `status`. */
const httpError = (status: number) => Object.assign(new Error(`status ${status}`), { status })

const genesisHashOf = (caip2: string) =>
  algosdk.base64ToBytes(caip2.slice(caip2.indexOf(':') + 1))

/**
 * A failure as a value, so a test can throw `null`, `undefined` or a bare
 * string -- things real clients have been known to reject with -- without
 * the fake mistaking a falsy failure for no failure.
 */
interface Failure {
  with: unknown
}

interface ChainBehaviour {
  boxes?: Map<string, string>
  /** Every algod call fails this way, the network check included. */
  algodFails?: Failure
  /** The chain the node claims to be. */
  network?: string
  account?: { amount: bigint; minBalance: bigint }
  timestamp?: bigint
  sendFails?: Failure
  poolError?: string
  /**
   * What the node says about a submitted transaction on each successive
   * question, counting from 1. Overrides `poolError` and the default, which
   * is to confirm at once.
   */
  pending?: (call: number) => Record<string, unknown>
  /** Every question asked after a submission fails this way. */
  afterSendFails?: Failure
  indexerTransactions?: unknown[]
  indexerFails?: Failure
}

interface FakeChain {
  algod: algosdk.Algodv2
  indexer: algosdk.Indexer
  sent: Uint8Array[]
  asked: { accounts: number; indexer: number }
  /** The most algod requests that were ever outstanding at once. */
  inFlight: { now: number; most: number }
}

function fakeChain(behaviour: ChainBehaviour = {}): FakeChain {
  const boxes = behaviour.boxes ?? ALL_BOXES
  const sent: Uint8Array[] = []
  const asked = { accounts: 0, indexer: 0 }
  const inFlight = { now: 0, most: 0 }

  const answer = async <T>(value: () => T): Promise<T> => {
    if (behaviour.algodFails) throw behaviour.algodFails.with
    inFlight.now += 1
    inFlight.most = Math.max(inFlight.most, inFlight.now)
    try {
      // One turn of the queue before answering, as a real request takes, so a
      // request started while this one is outstanding is counted alongside it.
      await Promise.resolve()
      return value()
    } finally {
      inFlight.now -= 1
    }
  }
  /** The questions a wait asks, which a node that went away cannot answer. */
  const afterSend = async <T>(value: () => T): Promise<T> => {
    if (behaviour.afterSendFails && sent.length > 0) throw behaviour.afterSendFails.with
    return answer(value)
  }
  let pendingCalls = 0

  const algod = {
    getApplicationBoxByName: (_appId: bigint, name: Uint8Array) => ({
      do: () =>
        answer(() => {
          const raw = boxes.get(algosdk.bytesToBase64(name))
          if (raw === undefined) throw httpError(404)
          return { value: algosdk.base64ToBytes(raw), round: 100n }
        }),
    }),
    accountInformation: (_address: string) => {
      const request = {
        exclude: () => request,
        do: () =>
          answer(() => {
            asked.accounts += 1
            return behaviour.account ?? { amount: 1_000_000n, minBalance: 200_000n }
          }),
      }
      return request
    },
    status: () => ({ do: () => afterSend(() => ({ lastRound: 100n })) }),
    statusAfterBlock: (_round: bigint) => ({
      do: () => afterSend(() => ({ lastRound: 101n })),
    }),
    block: (_round: bigint) => {
      const request = {
        headerOnly: () => request,
        do: () =>
          answer(() => ({
            block: { header: { timestamp: behaviour.timestamp ?? 1789651000n } },
          })),
      }
      return request
    },
    getTransactionParams: () => ({
      do: () =>
        answer(
          () =>
            ({
              fee: 0,
              flatFee: false,
              minFee: 1000,
              firstValid: 1000n,
              lastValid: 2000n,
              genesisHash: genesisHashOf(behaviour.network ?? TESTNET),
              genesisID: 'testnet-v1.0',
            }) as algosdk.SuggestedParams,
        ),
    }),
    sendRawTransaction: (bytes: Uint8Array) => ({
      do: () =>
        answer(() => {
          if (behaviour.sendFails) throw behaviour.sendFails.with
          sent.push(bytes)
          return { txid: 'ignored' }
        }),
    }),
    pendingTransactionInformation: (_txid: string) => ({
      do: () =>
        afterSend(() => {
          pendingCalls += 1
          if (behaviour.pending) return behaviour.pending(pendingCalls)
          return behaviour.poolError
            ? { poolError: behaviour.poolError }
            : { confirmedRound: 101n }
        }),
    }),
  } as unknown as algosdk.Algodv2

  const query = {
    address: () => query,
    applicationID: () => query,
    txType: () => query,
    notePrefix: () => query,
    nextToken: () => query,
    do: async () => {
      asked.indexer += 1
      if (behaviour.indexerFails) throw behaviour.indexerFails.with
      return { transactions: behaviour.indexerTransactions ?? [] }
    },
  }
  const indexer = { searchForTransactions: () => query } as unknown as algosdk.Indexer

  return { algod, indexer, sent, asked, inFlight }
}

/** Stands in for a wallet: returns each requested slot's bytes, marked. */
function fakeSigner(): Signer & { calls: { txns: Uint8Array[]; indexes: number[] }[] } {
  const calls: { txns: Uint8Array[]; indexes: number[] }[] = []
  const signer = async (txns: Uint8Array[], indexes: number[]) => {
    calls.push({ txns, indexes })
    return txns.map((bytes, i) => (indexes.includes(i) ? Uint8Array.from([0xff, ...bytes]) : null))
  }
  return Object.assign(signer, { calls })
}

function runtime(chain: FakeChain, overrides: Partial<Runtime> = {}): Runtime {
  return {
    algod: chain.algod,
    indexer: chain.indexer,
    signer: fakeSigner(),
    address: HOLDER,
    navigate: () => {},
    ...overrides,
  }
}

const b64 = (value: unknown) =>
  algosdk.bytesToBase64(new TextEncoder().encode(JSON.stringify(value)))

const json = (body: unknown, init: ResponseInit = {}) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  })

/** The 402 the pooled route sends for agreement 25, as the server builds it. */
const ACCEPTED = {
  scheme: 'exact',
  network: TESTNET,
  amount: '100000',
  asset: String(ASSET),
  payTo: PAY_TO,
  maxTimeoutSeconds: 120,
  extra: {
    feePayer: FEE_PAYER,
    tag: 'x402-global-challenge',
    decimals: 6,
    agreementId: 25,
    seatsTaken: 1,
    seatsTotal: 5,
    seatsLeft: 4,
    deadline: 1789651281,
    commitSha256: OPEN.agreement.expected.commitSha256,
  },
}

const quote402 = (extra: Record<string, unknown> = {}) =>
  new Response('{}', {
    status: 402,
    headers: {
      'content-type': 'application/json',
      'PAYMENT-REQUIRED': b64({
        x402Version: 2,
        resource: { url: 'https://earnest.example/index' },
        accepts: [{ ...ACCEPTED, extra: { ...ACCEPTED.extra, ...extra } }],
      }),
    },
  })

const RECEIPT = {
  agreementId: 25,
  seatsTotal: 5,
  deadline: 1789651281,
  commitSha256: OPEN.agreement.expected.commitSha256,
}

interface RouteCall {
  url: string
  signature?: string
}

/**
 * The paid route. The unpaid pass and the paid retry go to the same URL, and
 * the server tells them apart by the payment header -- so this does too.
 */
function fakeRoute(route: {
  quote?: () => Response
  settle?: () => Response
  fails?: Failure
}): RouteCall[] {
  const calls: RouteCall[] = []
  vi.stubGlobal('fetch', async (url: string | URL, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>
    calls.push({ url: String(url), signature: headers['PAYMENT-SIGNATURE'] })
    if (route.fails) throw route.fails.with
    if (headers['PAYMENT-SIGNATURE']) return (route.settle ?? (() => json(RECEIPT)))()
    return (route.quote ?? (() => quote402()))()
  })
  return calls
}

const SEAT_QUOTE: SeatQuote = {
  agreementId: 25n,
  amount: 100000n,
  seatsTotal: 5,
  seatsLeft: 4,
  deadline: 1789651281,
  commitSha256: OPEN.agreement.expected.commitSha256,
}

/** Quote, so the runner is holding agreement 25's envelope, then return it. */
async function quoted(chain: FakeChain): Promise<void> {
  const events = await run({ type: 'REQUEST_QUOTE' }, runtime(chain))
  expect(events.map((e) => e.type)).toEqual(['QUOTE_RECEIVED'])
}

describe('REQUEST_QUOTE', () => {
  it('builds the seat from the 402 extra and the accepted amount', async () => {
    fakeRoute({})
    const events = await run({ type: 'REQUEST_QUOTE' }, runtime(fakeChain()))
    expect(events).toEqual([{ type: 'QUOTE_RECEIVED', quote: SEAT_QUOTE }])
  })

  it('reports a closed edition as a refusal with its reason, not as a failure', async () => {
    // The expensive confusion: `no_pool_open` is an answer. Reported as a
    // failure it tells a buyer the client is broken when the edition is shut.
    fakeRoute({ quote: () => json({ error: 'no_pool_open' }, { status: 503 }) })
    const [event] = await run({ type: 'REQUEST_QUOTE' }, runtime(fakeChain()))
    expect(event).toMatchObject({ type: 'QUOTE_REFUSED', reason: 'no_pool_open' })
  })

  it('keeps the transient refusal distinct from the closed one', async () => {
    fakeRoute({ quote: () => json({ error: 'pool_status_unavailable' }, { status: 503 }) })
    const [event] = await run({ type: 'REQUEST_QUOTE' }, runtime(fakeChain()))
    expect(event).toMatchObject({ type: 'QUOTE_REFUSED', reason: 'pool_status_unavailable' })
  })

  it('reports an unreachable route as a failure', async () => {
    fakeRoute({ fails: { with: new TypeError('Failed to fetch') } })
    const [event] = await run({ type: 'REQUEST_QUOTE' }, runtime(fakeChain()))
    expect(event).toEqual({ type: 'QUOTE_FAILED', error: 'Failed to fetch' })
  })

  it('refuses a 402 whose seat count cannot be read', async () => {
    // A missing `seatsLeft` would become NaN, disagree with every pool, and
    // refuse this buyer a seat forever with a message blaming the edition.
    fakeRoute({ quote: () => quote402({ seatsLeft: undefined }) })
    const [event] = await run({ type: 'REQUEST_QUOTE' }, runtime(fakeChain()))
    expect(event?.type).toBe('QUOTE_FAILED')
    expect(event).toMatchObject({ error: expect.stringContaining('seatsLeft') })
  })

  it('hands the reducer a lowercase commitment whatever the server spelled', async () => {
    fakeRoute({ quote: () => quote402({ commitSha256: SEAT_QUOTE.commitSha256.toUpperCase() }) })
    const [event] = await run({ type: 'REQUEST_QUOTE' }, runtime(fakeChain()))
    expect(event).toEqual({ type: 'QUOTE_RECEIVED', quote: SEAT_QUOTE })
  })

  it('refuses a 402 that does not name a legible commitment', async () => {
    fakeRoute({ quote: () => quote402({ commitSha256: 'not-a-hash' }) })
    const [event] = await run({ type: 'REQUEST_QUOTE' }, runtime(fakeChain()))
    expect(event).toMatchObject({
      type: 'QUOTE_FAILED',
      error: expect.stringContaining('commitSha256'),
    })
  })
})

describe('VERIFY_QUOTE', () => {
  it('passes a quote the pool still matches', async () => {
    expect(
      await run({ type: 'VERIFY_QUOTE', quote: SEAT_QUOTE }, runtime(fakeChain())),
    ).toEqual([{ type: 'QUOTE_VERIFIED' }])
  })

  it('refuses a quote the pool has outgrown', async () => {
    const [event] = await run(
      { type: 'VERIFY_QUOTE', quote: { ...SEAT_QUOTE, seatsLeft: 5 } },
      runtime(fakeChain()),
    )
    expect(event?.type).toBe('QUOTE_STALE')
    expect(event).toMatchObject({ disagreements: [expect.stringContaining('seats moved')] })
  })

  it('refuses a quote for a pool whose boxes are gone', async () => {
    const [event] = await run(
      { type: 'VERIFY_QUOTE', quote: SEAT_QUOTE },
      runtime(fakeChain({ boxes: new Map() })),
    )
    expect(event?.type).toBe('QUOTE_STALE')
  })

  it('does not report an unreachable node as a stale quote', async () => {
    // "The pool moved" and "nobody looked at the pool" are opposite news.
    const [event] = await run(
      { type: 'VERIFY_QUOTE', quote: SEAT_QUOTE },
      runtime(fakeChain({ algodFails: { with: httpError(503) } })),
    )
    expect(event?.type).toBe('QUOTE_UNVERIFIABLE')
  })

  it('does not report a node on the wrong chain as a stale quote', async () => {
    const [event] = await run(
      { type: 'VERIFY_QUOTE', quote: SEAT_QUOTE },
      runtime(fakeChain({ network: MAINNET })),
    )
    expect(event?.type).toBe('QUOTE_UNVERIFIABLE')
  })
})

describe('BUILD_AND_SIGN', () => {
  it('settles and reports the receipt', async () => {
    const chain = fakeChain()
    fakeRoute({})
    await quoted(chain)
    expect(
      await run({ type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE }, runtime(chain)),
    ).toEqual([
      {
        type: 'SETTLED',
        agreementId: 25n,
        seatsTotal: 5,
        deadline: 1789651281,
        commitSha256: OPEN.agreement.expected.commitSha256,
      },
    ])
  })

  it('echoes the requirements exactly as the 402 issued them', async () => {
    // The reason the whole envelope is kept outside the reducer. The
    // facilitator compares what it is sent against what it issued, so the
    // fields this client never parses -- the tag, `seatsTaken`, `decimals` --
    // have to come back untouched, or the settlement fails for a reason no
    // log points at.
    const chain = fakeChain()
    const calls = fakeRoute({})
    await quoted(chain)
    await run({ type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE }, runtime(chain))
    const paid = calls.find((call) => call.signature)!
    const envelope = JSON.parse(
      new TextDecoder().decode(algosdk.base64ToBytes(paid.signature!)),
    )
    expect(envelope.accepted).toEqual(ACCEPTED)
  })

  it('signs for the connected wallet, and asks for both of the buyer s legs at once', async () => {
    const chain = fakeChain()
    fakeRoute({})
    await quoted(chain)
    const signer = fakeSigner()
    await run({ type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE }, runtime(chain, { signer }))
    expect(signer.calls).toHaveLength(1)
    expect(signer.calls[0]!.indexes).toEqual([1, 2])
    const transfer = algosdk.decodeUnsignedTransaction(signer.calls[0]!.txns[1]!)
    expect(transfer.sender.toString()).toBe(HOLDER)
  })

  it('does not pay twice with the same envelope', async () => {
    // A duplicate BUILD_AND_SIGN -- a re-render, a retried effect -- after a
    // seat is bought must not reach the route at all.
    const chain = fakeChain()
    const calls = fakeRoute({})
    await quoted(chain)
    await run({ type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE }, runtime(chain))
    const paidBefore = calls.filter((call) => call.signature).length
    const [again] = await run({ type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE }, runtime(chain))
    expect(again?.type).toBe('SETTLE_FAILED')
    expect(calls.filter((call) => call.signature).length).toBe(paidBefore)
  })

  it('refuses to rebuild an envelope it never received', async () => {
    const calls = fakeRoute({})
    const [event] = await run(
      { type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE },
      runtime(fakeChain()),
    )
    expect(event).toMatchObject({
      type: 'SETTLE_FAILED',
      error: expect.stringContaining('new quote'),
    })
    expect(calls).toEqual([])
  })

  it('needs a wallet', async () => {
    const chain = fakeChain()
    fakeRoute({})
    await quoted(chain)
    const [event] = await run(
      { type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE },
      runtime(chain, { signer: null, address: null }),
    )
    expect(event?.type).toBe('SETTLE_FAILED')
  })

  it('reports a declined wallet prompt as a failed settlement', async () => {
    const chain = fakeChain()
    fakeRoute({})
    await quoted(chain)
    const declined: Signer = async () => {
      throw new Error('the user rejected the request')
    }
    const [event] = await run(
      { type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE },
      runtime(chain, { signer: declined }),
    )
    expect(event).toEqual({ type: 'SETTLE_FAILED', error: 'the user rejected the request' })
  })

  it('reports a wallet that returned an unsigned leg as a failed settlement', async () => {
    const chain = fakeChain()
    const calls = fakeRoute({})
    await quoted(chain)
    const partial: Signer = async (txns) => txns.map(() => null)
    const [event] = await run(
      { type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE },
      runtime(chain, { signer: partial }),
    )
    expect(event?.type).toBe('SETTLE_FAILED')
    expect(calls.filter((call) => call.signature)).toEqual([])
  })

  it('reports a refused payment as a failed settlement', async () => {
    const chain = fakeChain()
    fakeRoute({ settle: () => json({ error: 'invalid_payment' }, { status: 402 }) })
    await quoted(chain)
    const [event] = await run({ type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE }, runtime(chain))
    expect(event?.type).toBe('SETTLE_FAILED')
  })

  it('never asks the wallet when the parameters cannot be read', async () => {
    const quoteChain = fakeChain()
    fakeRoute({})
    await quoted(quoteChain)
    const signer = fakeSigner()
    const [event] = await run(
      { type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE },
      runtime(fakeChain({ algodFails: { with: httpError(503) } }), { signer }),
    )
    expect(event?.type).toBe('SETTLE_FAILED')
    expect(signer.calls).toEqual([])
  })

  it('settles when the facilitator header is garbled but the receipt is readable', async () => {
    // Past the 200 the money has moved, and the receipt says everything the
    // purchase needs. The header is secondary; failing the purchase over it
    // would tell a buyer who paid that they did not.
    const chain = fakeChain()
    fakeRoute({
      settle: () =>
        new Response(JSON.stringify(RECEIPT), {
          status: 200,
          headers: { 'content-type': 'application/json', 'PAYMENT-RESPONSE': 'not base64 json' },
        }),
    })
    await quoted(chain)
    const [event] = await run({ type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE }, runtime(chain))
    expect(event).toMatchObject({ type: 'SETTLED', agreementId: 25n })
  })

  describe('a settlement whose receipt cannot be read', () => {
    // Past the 200 the money has moved. Reporting this as a failed payment is
    // how a buyer ends up paying twice.
    const unreadable: [string, () => Response][] = [
      ['a body that is not JSON', () => new Response('not json', { status: 200 })],
      ['a receipt with no commitment', () => json({ ...RECEIPT, commitSha256: undefined })],
      ['a receipt with no agreement id', () => json({ ...RECEIPT, agreementId: 'x' })],
    ]

    for (const [what, settle] of unreadable) {
      it(`is not a failed payment: ${what}`, async () => {
        const chain = fakeChain()
        fakeRoute({ settle })
        await quoted(chain)
        const events = await run({ type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE }, runtime(chain))
        expect(events.map((e) => e.type)).not.toContain('SETTLE_FAILED')
        expect(events).toEqual([
          {
            type: 'RECEIPT_UNREADABLE',
            agreementId: 25n,
            message: expect.stringContaining('do not pay again'),
          },
        ])
      })
    }

    it('trusts the signed group over a receipt that names another pool', async () => {
      // The group joins agreement 25; that is where the money went. Believing
      // a receipt for 26 would have the buyer waiting on a seat that cannot
      // appear, in a pool they never joined.
      const chain = fakeChain()
      fakeRoute({ settle: () => json({ ...RECEIPT, agreementId: 26 }) })
      await quoted(chain)
      const events = await run({ type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE }, runtime(chain))
      expect(events).toEqual([
        {
          type: 'RECEIPT_UNREADABLE',
          agreementId: 25n,
          message: expect.stringContaining('do not pay again'),
        },
      ])
    })

    it('spends the envelope too, since the payment went through', async () => {
      const chain = fakeChain()
      const calls = fakeRoute({ settle: () => new Response('not json', { status: 200 }) })
      await quoted(chain)
      await run({ type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE }, runtime(chain))
      await run({ type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE }, runtime(chain))
      expect(calls.filter((call) => call.signature)).toHaveLength(1)
    })
  })
})

describe('READ_POOL', () => {
  it('assembles one reading from algod', async () => {
    const [event] = await run(
      { type: 'READ_POOL', agreementId: 25n, address: HOLDER },
      runtime(fakeChain({ timestamp: 1789650000n })),
    )
    expect(event?.type).toBe('READ_RECEIVED')
    if (event?.type !== 'READ_RECEIVED') return
    // Named, because the reading itself cannot say which pool it describes.
    expect(event.agreementId).toBe(25n)
    const { record, seat, chainNow, canPayFee, closedOutcome } = event.circumstances
    expect(record?.state).toBe('OPEN')
    expect(seat).toMatchObject({ index: 0, payer: HOLDER, status: 'owed' })
    expect(chainNow).toBe(1789650000n)
    expect(canPayFee).toBe(true)
    expect(closedOutcome).toBeUndefined()
  })

  it('asks algod its questions concurrently, not one after another', async () => {
    // The boxes, the chain's clock and the wallet's balance are independent,
    // and this runs on a refresh interval against a public node. In sequence
    // it was six round trips end to end.
    const chain = fakeChain()
    await run({ type: 'READ_POOL', agreementId: 25n, address: HOLDER }, runtime(chain))
    expect(chain.inFlight.most).toBeGreaterThanOrEqual(4)
  })

  it('finds no seat for a wallet that is not on the roster', async () => {
    const [event] = await run(
      { type: 'READ_POOL', agreementId: 25n, address: STRANGER },
      runtime(fakeChain(), { address: STRANGER }),
    )
    expect(event).toMatchObject({ circumstances: { seat: null } })
  })

  it('asks nothing about a wallet when none is connected', async () => {
    const chain = fakeChain()
    const [event] = await run(
      { type: 'READ_POOL', agreementId: 25n, address: null },
      runtime(chain, { address: null, signer: null }),
    )
    expect(event).toMatchObject({ circumstances: { seat: null, canPayFee: false } })
    expect(chain.asked.accounts).toBe(0)
  })

  it('reports a wallet that cannot pay a fee', async () => {
    const [event] = await run(
      { type: 'READ_POOL', agreementId: 25n, address: HOLDER },
      runtime(fakeChain({ account: { amount: 200_000n, minBalance: 200_000n } })),
    )
    expect(event).toMatchObject({ circumstances: { canPayFee: false } })
  })

  it('does not ask the indexer about a pool that is still open', async () => {
    const chain = fakeChain()
    await run({ type: 'READ_POOL', agreementId: 25n, address: HOLDER }, runtime(chain))
    expect(chain.asked.indexer).toBe(0)
  })

  describe("a released pool's edition link", () => {
    const CID = 'bafybeidahqbf2ym3bx3cbv7czf6t2ois53p6sv5pnj44fv34atm55fdr4i'
    const release = (id: bigint) => ({
      applicationTransaction: {
        applicationId: APP,
        applicationArgs: [
          algosdk.hexToBytes(SELECTORS.releaseHash),
          algosdk.encodeUint64(id),
          new Uint8Array(32),
        ],
      },
      note: new TextEncoder().encode(`earnest:index:edition-900:agreement:${id}:cid:${CID}`),
    })

    it('is read from the history while the boxes still exist', async () => {
      const [event] = await run(
        { type: 'READ_POOL', agreementId: 27n, address: HOLDER },
        runtime(fakeChain({ indexerTransactions: [release(27n)] })),
      )
      expect(event).toMatchObject({
        type: 'READ_RECEIVED',
        circumstances: { record: { state: 'RELEASED' }, edition: { edition: '900', cid: CID } },
      })
    })

    it('is read from the history once the boxes are gone', async () => {
      // The note is history, which `close` does not touch.
      const [event] = await run(
        { type: 'READ_POOL', agreementId: 99n, address: HOLDER },
        runtime(fakeChain({ indexerTransactions: [release(99n)] })),
      )
      expect(event).toMatchObject({
        circumstances: {
          record: null,
          closedOutcome: 'released',
          edition: { edition: '900', cid: CID },
        },
      })
    })

    it('is null when the history holds no release note', async () => {
      const [event] = await run(
        { type: 'READ_POOL', agreementId: 27n, address: HOLDER },
        runtime(fakeChain({ indexerTransactions: [] })),
      )
      expect(event).toMatchObject({ circumstances: { edition: null } })
    })

    it('is not asked for on a pool that was refunded', async () => {
      const refund = {
        applicationTransaction: {
          applicationId: APP,
          applicationArgs: [algosdk.hexToBytes(SELECTORS.expire), algosdk.encodeUint64(99n)],
        },
      }
      const chain = fakeChain({ indexerTransactions: [refund] })
      const [event] = await run(
        { type: 'READ_POOL', agreementId: 99n, address: HOLDER },
        runtime(chain),
      )
      expect(event).toMatchObject({ circumstances: { closedOutcome: 'refunded' } })
      if (event?.type !== 'READ_RECEIVED') return
      expect(event.circumstances.edition).toBeUndefined()
      expect(chain.asked.indexer).toBe(1)
    })

    it('does not take the algod half of the reading down when the indexer is unreachable', async () => {
      const [event] = await run(
        { type: 'READ_POOL', agreementId: 27n, address: HOLDER },
        runtime(fakeChain({ indexerFails: { with: httpError(503) } })),
      )
      expect(event?.type).toBe('READ_RECEIVED')
      if (event?.type !== 'READ_RECEIVED') return
      expect(event.circumstances.record?.state).toBe('RELEASED')
      expect('edition' in event.circumstances).toBe(true)
      expect(event.circumstances.edition).toBeUndefined()
    })
  })

  it('asks the indexer how a closed pool ended', async () => {
    const released = {
      applicationTransaction: {
        applicationId: APP,
        applicationArgs: [algosdk.hexToBytes(SELECTORS.releaseHash), algosdk.encodeUint64(99n)],
      },
    }
    const [event] = await run(
      { type: 'READ_POOL', agreementId: 99n, address: HOLDER },
      runtime(fakeChain({ indexerTransactions: [released] })),
    )
    expect(event).toMatchObject({
      type: 'READ_RECEIVED',
      circumstances: { record: null, seat: null, closedOutcome: 'released' },
    })
  })

  it('still answers from algod when the indexer is unreachable', async () => {
    // A public indexer is a third-party host. It must never take the
    // algod-backed half of a reading down with it.
    const [event] = await run(
      { type: 'READ_POOL', agreementId: 99n, address: HOLDER },
      runtime(fakeChain({ indexerFails: { with: httpError(503) } })),
    )
    expect(event?.type).toBe('READ_RECEIVED')
    if (event?.type !== 'READ_RECEIVED') return
    expect(event.circumstances.record).toBeNull()
    expect('closedOutcome' in event.circumstances).toBe(true)
    expect(event.circumstances.closedOutcome).toBeUndefined()
  })

  it('reports an unreachable algod as a failed read, never as a closed pool', async () => {
    const [event] = await run(
      { type: 'READ_POOL', agreementId: 25n, address: HOLDER },
      runtime(fakeChain({ algodFails: { with: new TypeError('Failed to fetch') } })),
    )
    expect(event).toMatchObject({
      type: 'READ_FAILED',
      agreementId: 25n,
      address: HOLDER,
      kind: 'unreachable',
    })
  })

  it('reports a node on another network as a failure retrying cannot fix', async () => {
    const [event] = await run(
      { type: 'READ_POOL', agreementId: 25n, address: HOLDER },
      runtime(fakeChain({ network: MAINNET })),
    )
    expect(event).toMatchObject({ type: 'READ_FAILED', kind: 'incompatible' })
  })

  it('reports a record it cannot decode as a failure retrying cannot fix', async () => {
    const boxes = new Map(ALL_BOXES)
    boxes.set(OPEN.boxNames.agreement, algosdk.bytesToBase64(new Uint8Array(12)))
    const [event] = await run(
      { type: 'READ_POOL', agreementId: 25n, address: HOLDER },
      runtime(fakeChain({ boxes })),
    )
    expect(event).toMatchObject({ type: 'READ_FAILED', kind: 'incompatible' })
  })

  describe('a reading asked to check a call that may still land', () => {
    // The fake reads every box at round 100.
    const CALL = { txId: 'TXCALL', lastValid: 200n }
    const readWith = async (behaviour: ChainBehaviour, call = CALL, agreementId = 28n) => {
      const [event] = await run(
        { type: 'READ_POOL', agreementId, address: HOLDER, call },
        runtime(fakeChain(behaviour)),
      )
      expect(event?.type).toBe('READ_RECEIVED')
      return event?.type === 'READ_RECEIVED' ? event.finalFor : 'not a reading'
    }

    it('is final for a call the node confirmed at or before the round the boxes were read at', async () => {
      expect(await readWith({ pending: () => ({ confirmedRound: 100n }) })).toBe('TXCALL')
    })

    it('is not final for a call confirmed after the boxes were read', async () => {
      // The boxes show the pool from before the call, and would offer it again.
      expect(await readWith({ pending: () => ({ confirmedRound: 101n }) })).toBeUndefined()
    })

    it('is not final for a call still waiting in the pool', async () => {
      expect(await readWith({ pending: () => ({ poolError: '' }) })).toBeUndefined()
    })

    it('is final for a call the node dropped', async () => {
      expect(await readWith({ poolError: 'overspend' })).toBe('TXCALL')
    })

    it('is not final while the node does not know the call and its window is open', async () => {
      // Never admitted, or forgotten. Either way the node has said nothing.
      const unknown = { pending: () => { throw httpError(404) } }
      expect(await readWith(unknown)).toBeUndefined()
    })

    it("is final once the boxes were read at the call's last valid round, whatever the node says", async () => {
      const unknown = { pending: () => { throw httpError(404) } }
      expect(await readWith(unknown, { txId: 'TXCALL', lastValid: 100n })).toBe('TXCALL')
    })

    it('is final when the boxes are gone, since nothing is left to send against', async () => {
      expect(await readWith({ pending: () => ({ poolError: '' }) }, CALL, 99n)).toBe('TXCALL')
    })

    it('says nothing about a call it was not asked about', async () => {
      const [event] = await run(
        { type: 'READ_POOL', agreementId: 28n, address: HOLDER },
        runtime(fakeChain()),
      )
      expect(event?.type).toBe('READ_RECEIVED')
      expect(event && 'finalFor' in event).toBe(false)
    })
  })

  describe('the wallet a reading is tagged with is the wallet it was read for', () => {
    // The reducer drops a reading whose tag is not the wallet connected now,
    // so a tag that disagreed with the address actually read would let a
    // stale answer through under the current wallet's name.

    it("reads the effect's wallet, not whichever wallet the runtime holds", async () => {
      // The runtime has moved on to another wallet; the reading is still for
      // the one the reducer asked about, and says so.
      const [event] = await run(
        { type: 'READ_POOL', agreementId: 25n, address: HOLDER },
        runtime(fakeChain(), { address: STRANGER }),
      )
      expect(event).toMatchObject({
        type: 'READ_RECEIVED',
        address: HOLDER,
        circumstances: { seat: { payer: HOLDER, status: 'owed' } },
      })
    })

    it('tags a reading for a wallet with no seat with that wallet', async () => {
      const [event] = await run(
        { type: 'READ_POOL', agreementId: 25n, address: STRANGER },
        runtime(fakeChain(), { address: HOLDER }),
      )
      expect(event).toMatchObject({
        type: 'READ_RECEIVED',
        address: STRANGER,
        circumstances: { seat: null },
      })
    })

    it('tags a walletless reading null, and asks nothing about the runtime wallet', async () => {
      const chain = fakeChain()
      const [event] = await run(
        { type: 'READ_POOL', agreementId: 25n, address: null },
        runtime(chain, { address: HOLDER }),
      )
      expect(event).toMatchObject({
        type: 'READ_RECEIVED',
        address: null,
        circumstances: { seat: null, canPayFee: false },
      })
      expect(chain.asked.accounts).toBe(0)
    })

    it('tags a failed reading with the wallet it was for', async () => {
      const [event] = await run(
        { type: 'READ_POOL', agreementId: 25n, address: null },
        runtime(fakeChain({ algodFails: { with: httpError(503) } }), { address: HOLDER }),
      )
      expect(event).toMatchObject({ type: 'READ_FAILED', address: null })
    })
  })
})

describe('SEND_ACTION', () => {
  /**
   * A chain whose clock has reached every vector's deadline. The call is
   * checked against the verdict before the wallet is asked, and before the
   * deadline `expire` is not a call the contract accepts.
   */
  const chainFor = (behaviour: ChainBehaviour = {}) =>
    fakeChain({ timestamp: BigInt(OPEN.agreement.expected.deadline), ...behaviour })

  /** The one call the wallet was asked to sign, decoded. */
  const signedCall = (signer: ReturnType<typeof fakeSigner>) => {
    expect(signer.calls).toHaveLength(1)
    expect(signer.calls[0]!.indexes).toEqual([0])
    return algosdk.decodeUnsignedTransaction(signer.calls[0]!.txns[0]!)
  }

  const selectorOf = (txn: algosdk.Transaction) =>
    algosdk.bytesToHex(txn.applicationCall!.appArgs[0]!)

  const uint64Arg = (txn: algosdk.Transaction, index: number) =>
    algosdk.decodeUint64(txn.applicationCall!.appArgs[index]!, 'bigint')

  const cases: [Action, bigint, string, keyof typeof SELECTORS][] = [
    ['expire', 25n, HOLDER, 'expire'],
    ['refund_next', 28n, HOLDER, 'refundNext'],
    ['claim_refund', 26n, SKIPPED, 'claimRefund'],
    ['close', 29n, HOLDER, 'close'],
  ]

  for (const [action, agreementId, address, selector] of cases) {
    it(`builds, signs, submits and confirms ${action}`, async () => {
      const chain = chainFor()
      const signer = fakeSigner()
      const events = await run(
        { type: 'SEND_ACTION', action, agreementId },
        runtime(chain, { signer, address }),
      )
      const txn = signedCall(signer)
      expect(selectorOf(txn)).toBe(SELECTORS[selector])
      expect(uint64Arg(txn, 1)).toBe(agreementId)
      expect(txn.sender.toString()).toBe(address)
      expect(chain.sent).toHaveLength(1)
      // The id is the transaction's own, computed before it was sent, so a
      // lost response still leaves something the buyer can look up.
      expect(events).toEqual([{ type: 'ACTION_SENT', action, agreementId, txId: txn.txID() }])
    })
  }

  describe('a call the pool has moved past since the page read it', () => {
    // The button was offered on an older reading. Someone else's call --
    // the operator's refund pass, most often -- can move the pool before the
    // click, and the wallet must not be asked to sign what the contract will
    // refuse.

    it('is refused before the wallet is asked', async () => {
      // Agreement 29's pass has finished, so there is nothing for refund_next.
      const chain = chainFor()
      const signer = fakeSigner()
      const [event] = await run(
        { type: 'SEND_ACTION', action: 'refund_next', agreementId: 29n },
        runtime(chain, { signer }),
      )
      expect(event).toMatchObject({
        type: 'ACTION_FAILED',
        action: 'refund_next',
        agreementId: 29n,
        error: expect.stringContaining('changed since this page last read it'),
      })
      expect(signer.calls).toEqual([])
      expect(chain.sent).toEqual([])
    })

    it('refuses expire on the chain clock read at the click, not the one on screen', async () => {
      const signer = fakeSigner()
      const [event] = await run(
        { type: 'SEND_ACTION', action: 'expire', agreementId: 25n },
        runtime(chainFor({ timestamp: BigInt(OPEN.agreement.expected.deadline) - 1n }), {
          signer,
        }),
      )
      expect(event).toMatchObject({ type: 'ACTION_FAILED', action: 'expire' })
      expect(signer.calls).toEqual([])
    })

    it('refuses a claim for a seat the pass has not reached', async () => {
      // Agreement 26's seat 2 is owed but still ahead of the cursor, and
      // claiming it early would strand a real skip.
      const signer = fakeSigner()
      const ahead = REFUNDING.roster.expected[2]!.payer
      const [event] = await run(
        { type: 'SEND_ACTION', action: 'claim_refund', agreementId: 26n },
        runtime(chainFor(), { signer, address: ahead }),
      )
      expect(event).toMatchObject({ type: 'ACTION_FAILED', action: 'claim_refund' })
      expect(signer.calls).toEqual([])
    })
  })

  it('keeps a call valid for minutes, not the network default of about an hour', async () => {
    // A call never seen to confirm holds the lock until a reading shows its
    // outcome, and when the node cannot say, only the end of this window can.
    const signer = fakeSigner()
    await run(
      { type: 'SEND_ACTION', action: 'refund_next', agreementId: 28n },
      runtime(chainFor(), { signer }),
    )
    const txn = signedCall(signer)
    expect(txn.firstValid).toBe(1000n)
    expect(txn.lastValid).toBe(1120n)
  })

  it('sizes refund_next from the record', async () => {
    // Agreement 28 sold two seats and its cursor is at zero.
    const signer = fakeSigner()
    await run(
      { type: 'SEND_ACTION', action: 'refund_next', agreementId: 28n },
      runtime(chainFor(), { signer }),
    )
    expect(uint64Arg(signedCall(signer), 2)).toBe(2n)
  })

  it('claims the buyer s own seat, not seat zero', async () => {
    // Agreement 26's seat 0 is someone else's and already paid; claiming it
    // would be refused on chain and leave this buyer's own seat unclaimed.
    const signer = fakeSigner()
    await run(
      { type: 'SEND_ACTION', action: 'claim_refund', agreementId: 26n },
      runtime(chainFor(), { signer, address: SKIPPED }),
    )
    expect(uint64Arg(signedCall(signer), 2)).toBe(1n)
  })

  it('refuses to claim for a wallet with no seat on the roster', async () => {
    const chain = chainFor()
    const signer = fakeSigner()
    const [event] = await run(
      { type: 'SEND_ACTION', action: 'claim_refund', agreementId: 26n },
      runtime(chain, { signer, address: STRANGER }),
    )
    expect(event).toMatchObject({ type: 'ACTION_FAILED', action: 'claim_refund' })
    expect(signer.calls).toEqual([])
    expect(chain.sent).toEqual([])
  })

  it('refuses to act on a pool whose boxes are gone', async () => {
    const signer = fakeSigner()
    const [event] = await run(
      { type: 'SEND_ACTION', action: 'close', agreementId: 99n },
      runtime(chainFor(), { signer }),
    )
    expect(event).toMatchObject({ type: 'ACTION_FAILED', action: 'close' })
    expect(signer.calls).toEqual([])
  })

  it('needs a wallet', async () => {
    const [event] = await run(
      { type: 'SEND_ACTION', action: 'expire', agreementId: 25n },
      runtime(chainFor(), { signer: null, address: null }),
    )
    expect(event).toMatchObject({ type: 'ACTION_FAILED', action: 'expire' })
  })

  it('submits nothing when the wallet returns no signature', async () => {
    const chain = chainFor()
    const [event] = await run(
      { type: 'SEND_ACTION', action: 'expire', agreementId: 25n },
      runtime(chain, { signer: async (txns) => txns.map(() => null) }),
    )
    expect(event).toMatchObject({ type: 'ACTION_FAILED', action: 'expire' })
    expect(chain.sent).toEqual([])
  })

  it('reports a submission the node answered with a refusal', async () => {
    // A node that answers with an error did not admit the transaction, so it
    // cannot land and the lock may go.
    const [event] = await run(
      { type: 'SEND_ACTION', action: 'expire', agreementId: 25n },
      runtime(chainFor({ sendFails: { with: httpError(400) } })),
    )
    expect(event).toMatchObject({ type: 'ACTION_FAILED', action: 'expire', agreementId: 25n })
  })

  it('reports a transaction the pool dropped after admitting it', async () => {
    const [event] = await run(
      { type: 'SEND_ACTION', action: 'expire', agreementId: 25n },
      runtime(chainFor({ poolError: 'logic eval error: assert failed' })),
    )
    expect(event).toMatchObject({
      type: 'ACTION_FAILED',
      agreementId: 25n,
      error: expect.stringContaining('assert failed'),
    })
  })

  describe('a call that may still land', () => {
    // Everything here was submitted and not seen to confirm. Reporting any of
    // it as ACTION_FAILED releases the lock while the call is still
    // travelling, and puts the same button back in front of the buyer.

    /** The id of the one call the wallet was asked to sign. */
    const signedId = (signer: ReturnType<typeof fakeSigner>) =>
      algosdk.decodeUnsignedTransaction(signer.calls[0]!.txns[0]!).txID()

    it('is unconfirmed, not failed, when the wait runs out', async () => {
      // A node that neither confirms nor drops the transaction for as long as
      // it is asked -- still pending when the rounds are spent.
      const chain = chainFor({ pending: () => ({ poolError: '' }) })
      const signer = fakeSigner()
      const events = await run(
        { type: 'SEND_ACTION', action: 'refund_next', agreementId: 28n },
        runtime(chain, { signer }),
      )
      expect(chain.sent).toHaveLength(1)
      expect(events).toEqual([
        {
          type: 'ACTION_UNCONFIRMED',
          action: 'refund_next',
          agreementId: 28n,
          txId: signedId(signer),
          // The fake's first valid round is 1000.
          lastValid: 1120n,
        },
      ])
    })

    it('is unconfirmed when the submission got no answer at all', async () => {
      // The connection dropped after the bytes left. The node may have
      // admitted the transaction first; nothing here can say it did not.
      const signer = fakeSigner()
      const events = await run(
        { type: 'SEND_ACTION', action: 'expire', agreementId: 25n },
        runtime(chainFor({ sendFails: { with: new TypeError('Failed to fetch') } }), {
          signer,
        }),
      )
      expect(events).toEqual([
        {
          type: 'ACTION_UNCONFIRMED',
          action: 'expire',
          agreementId: 25n,
          txId: signedId(signer),
          lastValid: 1120n,
        },
      ])
    })

    it('is unconfirmed when the node stops answering after the submission', async () => {
      const [event] = await run(
        { type: 'SEND_ACTION', action: 'expire', agreementId: 25n },
        runtime(chainFor({ afterSendFails: { with: httpError(503) } })),
      )
      expect(event?.type).toBe('ACTION_UNCONFIRMED')
    })

    it('is sent when it confirms just after the wait gave up', async () => {
      // The wait asks four times; the fifth question -- asked once the wait
      // has thrown -- finds it committed.
      const chain = chainFor({
        pending: (call) => (call >= 5 ? { confirmedRound: 105n } : { poolError: '' }),
      })
      const [event] = await run(
        { type: 'SEND_ACTION', action: 'expire', agreementId: 25n },
        runtime(chain),
      )
      expect(event?.type).toBe('ACTION_SENT')
    })
  })
})

describe('FIND_MY_POOLS', () => {
  it('lists the agreements this wallet joined', async () => {
    const join = {
      applicationTransaction: {
        applicationId: APP,
        applicationArgs: [algosdk.hexToBytes(SELECTORS.join), algosdk.encodeUint64(25n)],
      },
    }
    expect(
      await run(
        { type: 'FIND_MY_POOLS', address: HOLDER },
        // A runtime on another wallet: the search, and its tag, follow the
        // effect.
        runtime(fakeChain({ indexerTransactions: [join] }), { address: STRANGER }),
      ),
    ).toEqual([{ type: 'MY_POOLS_FOUND', address: HOLDER, agreementIds: [25n] }])
  })

  it('degrades to "open your link" when the indexer is unreachable', async () => {
    const [event] = await run(
      { type: 'FIND_MY_POOLS', address: HOLDER },
      runtime(fakeChain({ indexerFails: { with: httpError(503) } }), { address: STRANGER }),
    )
    expect(event).toMatchObject({ type: 'MY_POOLS_UNAVAILABLE', address: HOLDER })
  })
})

describe('NAVIGATE', () => {
  it('moves the router and reports nothing', async () => {
    const navigate = vi.fn()
    expect(
      await run({ type: 'NAVIGATE', path: '/app/pool/25' }, runtime(fakeChain(), { navigate })),
    ).toEqual([])
    expect(navigate).toHaveBeenCalledWith('/app/pool/25')
  })

  it('survives a history API that refuses', async () => {
    const navigate = () => {
      throw new Error('SecurityError')
    }
    await expect(
      run({ type: 'NAVIGATE', path: '/app/pool/25' }, runtime(fakeChain(), { navigate })),
    ).resolves.toEqual([])
  })
})

describe('never throws', () => {
  // A runner that throws puts the machine in a state no event can leave --
  // and the effects that can throw are the ones moving money. So: every
  // effect, against every way its dependencies have been seen to fail,
  // resolves to events the reducer prices.
  const failures: [string, Failure][] = [
    ['an HTTP error', { with: httpError(503) }],
    ['a network error', { with: new TypeError('Failed to fetch') }],
    ['a thrown string', { with: 'boom' }],
    ['a thrown null', { with: null }],
    ['a thrown undefined', { with: undefined }],
  ]

  const effects: Effect[] = [
    { type: 'REQUEST_QUOTE' },
    { type: 'VERIFY_QUOTE', quote: SEAT_QUOTE },
    { type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE },
    { type: 'READ_POOL', agreementId: 25n, address: HOLDER },
    { type: 'READ_POOL', agreementId: 99n, address: null },
    { type: 'SEND_ACTION', action: 'expire', agreementId: 25n },
    { type: 'SEND_ACTION', action: 'refund_next', agreementId: 28n },
    { type: 'SEND_ACTION', action: 'claim_refund', agreementId: 26n },
    { type: 'SEND_ACTION', action: 'close', agreementId: 26n },
    { type: 'FIND_MY_POOLS', address: HOLDER },
    { type: 'NAVIGATE', path: '/app/pool/25' },
  ]

  /** The events each effect may answer with. Anything else is a mis-mapping. */
  const answers: Record<Effect['type'], Event['type'][]> = {
    REQUEST_QUOTE: ['QUOTE_RECEIVED', 'QUOTE_REFUSED', 'QUOTE_FAILED'],
    VERIFY_QUOTE: ['QUOTE_VERIFIED', 'QUOTE_STALE', 'QUOTE_UNVERIFIABLE'],
    BUILD_AND_SIGN: ['SETTLED', 'RECEIPT_UNREADABLE', 'SETTLE_FAILED'],
    READ_POOL: ['READ_RECEIVED', 'READ_FAILED'],
    SEND_ACTION: ['ACTION_SENT', 'ACTION_UNCONFIRMED', 'ACTION_FAILED'],
    FIND_MY_POOLS: ['MY_POOLS_FOUND', 'MY_POOLS_UNAVAILABLE'],
    NAVIGATE: [],
  }

  for (const [what, failure] of failures) {
    it(`answers every effect with an event when everything fails with ${what}`, async () => {
      const thrower = () => {
        throw failure.with
      }
      const everythingFails: Runtime = {
        ...runtime(
          fakeChain({
            algodFails: failure,
            indexerFails: failure,
            sendFails: failure,
          }),
        ),
        signer: async () => {
          throw failure.with
        },
        navigate: thrower,
      }
      fakeRoute({ fails: failure })

      for (const effect of effects) {
        const events = await run(effect, everythingFails)
        const expected = effect.type === 'NAVIGATE' ? 0 : 1
        expect(events, effect.type).toHaveLength(expected)
        for (const event of events) {
          expect(answers[effect.type], effect.type).toContain(event.type)
        }
      }
    })

    it(`answers a quoted purchase with an event when the wallet fails with ${what}`, async () => {
      // The one path the matrix above cannot reach with a failing route: a
      // settlement needs an envelope, and an envelope needs a 402 that
      // arrived.
      const chain = fakeChain()
      fakeRoute({})
      await quoted(chain)
      const [event] = await run(
        { type: 'BUILD_AND_SIGN', quote: SEAT_QUOTE },
        runtime(chain, {
          signer: async () => {
            throw failure.with
          },
        }),
      )
      expect(event?.type).toBe('SETTLE_FAILED')
    })
  }
})

describe('on the travel line', () => {
  // The same runner, built with VITE_BRAND=travel: every message it writes
  // says "trip" where the index says "pool", and a released trip's link is
  // read from a trip note.
  beforeEach(() => {
    vi.stubEnv('VITE_BRAND', 'travel')
    resetBrandForTests()
  })

  afterEach(() => {
    resetBrandForTests()
  })

  it("words a stale quote in a trip's terms", async () => {
    const [event] = await run(
      { type: 'VERIFY_QUOTE', quote: SEAT_QUOTE },
      runtime(fakeChain({ boxes: new Map() })),
    )
    expect(event).toMatchObject({
      type: 'QUOTE_STALE',
      noun: 'trip',
      disagreements: [expect.stringMatching(/^trip 25 has no record on chain/)],
    })
  })

  it('reads a released trip link from the trip note, and never from an edition note', async () => {
    const CID = 'bafybeidahqbf2ym3bx3cbv7czf6t2ois53p6sv5pnj44fv34atm55fdr4i'
    const release = (note: string) => ({
      applicationTransaction: {
        applicationId: APP,
        applicationArgs: [
          algosdk.hexToBytes(SELECTORS.releaseHash),
          algosdk.encodeUint64(27n),
          new Uint8Array(32),
        ],
      },
      note: new TextEncoder().encode(note),
    })
    const read = (note: string) =>
      run(
        { type: 'READ_POOL', agreementId: 27n, address: HOLDER },
        runtime(fakeChain({ indexerTransactions: [release(note)] })),
      )

    const [trip] = await read(`earnest:travel:trip-901:agreement:27:cid:${CID}`)
    expect(trip).toMatchObject({ circumstances: { edition: { edition: '901', cid: CID } } })

    const [edition] = await read(`earnest:index:edition-900:agreement:27:cid:${CID}`)
    expect(edition).toMatchObject({ circumstances: { edition: null } })
  })

  it("words a refused call in a trip's terms", async () => {
    const [event] = await run(
      { type: 'SEND_ACTION', action: 'refund_next', agreementId: 29n },
      runtime(fakeChain({ timestamp: BigInt(OPEN.agreement.expected.deadline) })),
    )
    expect(event).toMatchObject({
      type: 'ACTION_FAILED',
      error: expect.stringMatching(/^Trip 29 changed since this page last read it/),
    })
  })

  it('says the index words on the index', async () => {
    vi.stubEnv('VITE_BRAND', '')
    resetBrandForTests()
    const [event] = await run(
      { type: 'VERIFY_QUOTE', quote: SEAT_QUOTE },
      runtime(fakeChain({ boxes: new Map() })),
    )
    expect(event).toMatchObject({
      type: 'QUOTE_STALE',
      noun: 'pool',
      disagreements: [expect.stringMatching(/^pool 25 has no record on chain/)],
    })
  })
})
