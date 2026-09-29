import algosdk from 'algosdk'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ChainUnreachableError,
  NetworkMismatchError,
  assertNetwork,
  canPayFee,
  chainTimestamp,
  createAlgod,
  createIndexer,
  findJoinedAgreements,
  readAgreement,
  readClosedOutcome,
  readEditionLink,
  readPoolBoxes,
  readRoster,
  suggestedParams,
} from '../src/chain/read'
import { CALL_BYTES_CEILING } from '../src/chain/calls'
import { SELECTORS } from '../src/chain/selectors'
import { resetConfigForTests } from '../src/config'
import golden from './golden/escrow-boxes.json'

const TESTNET = 'algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI='
const MAINNET = 'algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8='
const APP = 771795120n

beforeEach(() => {
  vi.stubEnv('VITE_NETWORK', TESTNET)
  vi.stubEnv('VITE_ASSET_ID', '10458941')
  vi.stubEnv('VITE_APP_ID', '771795120')
  vi.stubEnv('VITE_FACILITATOR_URL', 'https://facilitator.example')
  vi.stubEnv('VITE_RESOURCE_HOST', 'https://earnest.example')
  vi.stubEnv('VITE_MAX_FILE_BYTES', '16777216')
  vi.stubEnv('VITE_ALGOD_URL', 'https://algod.example')
  vi.stubEnv('VITE_INDEXER_URL', 'https://indexer.example')
  vi.stubEnv('VITE_IPFS_GATEWAY', 'https://ipfs.example')
  resetConfigForTests()
})

afterEach(() => {
  vi.unstubAllEnvs()
  resetConfigForTests()
})

/** The shape algosdk throws on a non-200: a plain error carrying `status`. */
const httpError = (status: number) => Object.assign(new Error(`status ${status}`), { status })

const b64 = (value: string) => algosdk.base64ToBytes(value)
const vector = golden.vectors.find((v) => v.name === 'open-pool-one-seat-sold')!

const genesisHashOf = (caip2: string) =>
  algosdk.base64ToBytes(caip2.slice(caip2.indexOf(':') + 1))

/**
 * Just enough algod for the call under test.
 *
 * `params` defaults to this network rather than to nothing, because every read
 * now checks the chain before it trusts the node. `paramsFail` is separate
 * from `fail` so a test can fail the read under test without the guard in
 * front of it answering first.
 */
function fakeAlgod(behaviour: {
  box?: (name: Uint8Array) => Promise<{ value: Uint8Array; round?: bigint }>
  lastRound?: bigint
  timestamp?: bigint
  params?: Partial<algosdk.SuggestedParams>
  fail?: unknown
  paramsFail?: unknown
  onHeaderOnly?: (headerOnly: boolean) => void
  account?: { amount: bigint; minBalance: bigint }
  onExclude?: (exclude: string) => void
}) {
  return {
    getApplicationBoxByName: (_appId: bigint, name: Uint8Array) => ({
      do: async () => {
        if (behaviour.fail) throw behaviour.fail
        return behaviour.box!(name)
      },
    }),
    // `.exclude('all')` returns the same chainable request, matching the
    // real client's builder shape: `canPayFee` calls it before `.do()`, and a
    // fake that only understood `.do()` would let a typo in the exclude
    // argument's plumbing pass unnoticed.
    accountInformation: (_address: string) => {
      const request = {
        exclude: (value: string) => {
          behaviour.onExclude?.(value)
          return request
        },
        do: async () => {
          if (behaviour.fail) throw behaviour.fail
          return {
            amount: behaviour.account?.amount ?? 0n,
            minBalance: behaviour.account?.minBalance ?? 0n,
          }
        },
      }
      return request
    },
    status: () => ({
      do: async () => {
        if (behaviour.fail) throw behaviour.fail
        return { lastRound: behaviour.lastRound ?? 1n }
      },
    }),
    block: (_round: bigint) => {
      const request = {
        headerOnly: (headerOnly: boolean) => {
          behaviour.onHeaderOnly?.(headerOnly)
          return request
        },
        do: async () => {
          if (behaviour.fail) throw behaviour.fail
          return { block: { header: { timestamp: behaviour.timestamp ?? 0n } } }
        },
      }
      return request
    },
    getTransactionParams: () => ({
      do: async () => {
        if (behaviour.paramsFail) throw behaviour.paramsFail
        return (behaviour.params ?? {
          genesisHash: genesisHashOf(TESTNET),
        }) as algosdk.SuggestedParams
      },
    }),
  } as unknown as algosdk.Algodv2
}

describe('building the network clients from the right config key', () => {
  // createAlgod and createIndexer are three lines each: read a URL out of
  // getConfig() and hand it to an algosdk constructor. The risk is not in the
  // logic, it is in which key each one reads -- a client built from
  // `indexerUrl` where `algodUrl` was meant would construct without
  // complaint and only fail later, against a host that was never going to
  // answer an algod question, in a way that would not obviously implicate a
  // config swap.
  //
  // The constructed client cannot be asked its base URL from outside the
  // class: `ServiceClient.c` (the shared field both Algodv2 and Indexer
  // build on) is tagged `@ignore` in algosdk's own source, and the
  // `URLTokenBaseHTTPClient` underneath it holds the URL in a `private`
  // field. Reaching into either would pin an implementation detail this
  // module does not own. So instead this stubs the global `fetch` algosdk
  // calls internally and reads the host it actually requested -- the
  // boundary a config swap would really break at, since that is the first
  // place a wrong URL produces an observable difference.
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const okResponse = () =>
    new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })

  it('points createAlgod at algodUrl, not indexerUrl', async () => {
    let requested: string | null = null
    vi.stubGlobal('fetch', async (url: string | URL) => {
      requested = url.toString()
      return okResponse()
    })
    await createAlgod().healthCheck().do()
    expect(requested).toBe('https://algod.example/health')
  })

  it('points createIndexer at indexerUrl, not algodUrl', async () => {
    let requested: string | null = null
    vi.stubGlobal('fetch', async (url: string | URL) => {
      requested = url.toString()
      return okResponse()
    })
    await createIndexer().makeHealthCheck().do()
    expect(requested).toBe('https://indexer.example/health')
  })
})

describe('readAgreement', () => {
  it('decodes the box', async () => {
    const algod = fakeAlgod({ box: async () => ({ value: b64(vector.agreement.raw) }) })
    const record = await readAgreement(algod, APP, 25n)
    expect(record?.state).toBe('OPEN')
    expect(record?.seats).toBe(1)
    expect(record?.commitSha256).toBe(vector.agreement.expected.commitSha256)
  })

  it('asks for the box the agreement id names', async () => {
    let asked: Uint8Array | null = null
    const algod = fakeAlgod({
      box: async (name) => {
        asked = name
        return { value: b64(vector.agreement.raw) }
      },
    })
    await readAgreement(algod, APP, 25n)
    expect(algosdk.bytesToBase64(asked!)).toBe(vector.boxNames.agreement)
  })

  it('reads a missing box as null, because close deletes it', async () => {
    const algod = fakeAlgod({ fail: httpError(404) })
    expect(await readAgreement(algod, APP, 25n)).toBeNull()
  })

  it('refuses to read an unreachable node as a closed agreement', async () => {
    // The expensive confusion: a node that did not answer is not a pool that
    // is finished. A buyer owed money would be told there is nothing to claim.
    const algod = fakeAlgod({ fail: httpError(503) })
    await expect(readAgreement(algod, APP, 25n)).rejects.toThrow(ChainUnreachableError)
  })

  it('treats a network failure with no status as unreachable, not as absent', async () => {
    const algod = fakeAlgod({ fail: new TypeError('Failed to fetch') })
    await expect(readAgreement(algod, APP, 25n)).rejects.toThrow(ChainUnreachableError)
  })
})

describe('readRoster', () => {
  it('decodes the seats', async () => {
    const algod = fakeAlgod({ box: async () => ({ value: b64(vector.roster.raw) }) })
    const seats = await readRoster(algod, APP, 25n)
    expect(seats.map((s) => s.status)).toEqual([
      'owed',
      'empty',
      'empty',
      'empty',
      'empty',
    ])
  })

  it('reads a missing box as no seats', async () => {
    const algod = fakeAlgod({ fail: httpError(404) })
    expect(await readRoster(algod, APP, 25n)).toEqual([])
  })

  it('refuses to read an unreachable node as an empty roster', async () => {
    // A refund path that made this mistake would decide nobody is owed
    // anything.
    const algod = fakeAlgod({ fail: httpError(500) })
    await expect(readRoster(algod, APP, 25n)).rejects.toThrow(ChainUnreachableError)
  })
})

describe('readPoolBoxes', () => {
  const agreementName = vector.boxNames.agreement

  it('decodes both boxes', async () => {
    const algod = fakeAlgod({
      box: async (name) => ({
        value: b64(
          algosdk.bytesToBase64(name) === agreementName ? vector.agreement.raw : vector.roster.raw,
        ),
        round: 700n,
      }),
    })
    const { record, roster, round } = await readPoolBoxes(algod, APP, 25n)
    expect(record?.state).toBe('OPEN')
    expect(roster.map((s) => s.status)[0]).toBe('owed')
    expect(round).toBe(700n)
  })

  it('reports the older of the two rounds, since the pair is only as current as that', async () => {
    // Two requests can land on two nodes behind one address, a round apart.
    const algod = fakeAlgod({
      box: async (name) =>
        algosdk.bytesToBase64(name) === agreementName
          ? { value: b64(vector.agreement.raw), round: 701n }
          : { value: b64(vector.roster.raw), round: 699n },
    })
    expect((await readPoolBoxes(algod, APP, 25n)).round).toBe(699n)
  })

  it('reads a pool whose boxes are gone as no record, no seats and no round', async () => {
    const algod = fakeAlgod({ fail: httpError(404) })
    expect(await readPoolBoxes(algod, APP, 25n)).toEqual({ record: null, roster: [], round: null })
  })

  it('refuses to read an unreachable node as a closed pool', async () => {
    const algod = fakeAlgod({ fail: httpError(503) })
    await expect(readPoolBoxes(algod, APP, 25n)).rejects.toThrow(ChainUnreachableError)
  })
})

describe('canPayFee', () => {
  // A buyer can hold real USDC and still be unable to send anything: an
  // opted-in asset locks a minimum balance that is held, not spendable, and
  // buying a seat costs the buyer no ALGO at all -- the settlement group
  // loads the whole pooled fee onto the facilitator's leg. So "holds money"
  // and "can pay a fee right now" are different questions, and this reads
  // the second one off the account, never the first.
  const params = { genesisHash: genesisHashOf(TESTNET), minFee: 1000n }

  it('says yes when spendable balance covers the live minimum fee', async () => {
    const algod = fakeAlgod({
      account: { amount: 2_001_000n, minBalance: 1_000_000n },
      params,
    })
    expect(await canPayFee(algod, 'ADDR')).toBe(true)
  })

  it('says no when the whole balance is locked as minimum balance', async () => {
    const algod = fakeAlgod({
      account: { amount: 1_000_000n, minBalance: 1_000_000n },
      params,
    })
    expect(await canPayFee(algod, 'ADDR')).toBe(false)
  })

  it('says yes exactly at the boundary, spendable equal to the fee', async () => {
    // The off-by-one that matters here rejects a buyer who has precisely
    // enough, which is the common case for a wallet funded for exactly one
    // call rather than a comfortable margin.
    const algod = fakeAlgod({
      account: { amount: 1_001_000n, minBalance: 1_000_000n },
      params,
    })
    expect(await canPayFee(algod, 'ADDR')).toBe(true)
  })

  it('prices a congested network against the largest call, not the minimum', async () => {
    // At 10 µALGO a byte the largest call costs more than the 1,000 minimum.
    // A wallet holding exactly the minimum could not pay what the node would
    // ask, so it is not offered the call.
    const congested = { ...params, fee: 10n }
    const needed = 10n * BigInt(CALL_BYTES_CEILING)
    expect(needed).toBeGreaterThan(1000n)
    const short = fakeAlgod({
      account: { amount: 1_001_000n, minBalance: 1_000_000n },
      params: congested,
    })
    expect(await canPayFee(short, 'ADDR')).toBe(false)
    const enough = fakeAlgod({
      account: { amount: 1_000_000n + needed, minBalance: 1_000_000n },
      params: congested,
    })
    expect(await canPayFee(enough, 'ADDR')).toBe(true)
  })

  it('throws on an unreachable node rather than reporting it cannot pay', async () => {
    // The one wrong answer this must never give. "No answer from algod" read
    // as "false" would tell a buyer their refund is unreachable when the
    // node, not the wallet, is what failed -- and unlike every other action
    // in this app, this result gates nothing about the refund itself
    // (`refund_next` and `claim_refund` pay the roster address regardless of
    // who sends them), so a wrong `false` here only ever misleads.
    const algod = fakeAlgod({ fail: httpError(503) })
    await expect(canPayFee(algod, 'ADDR')).rejects.toThrow(ChainUnreachableError)
  })
})

describe('chainTimestamp', () => {
  it('reads the last block, not the browser clock', async () => {
    const algod = fakeAlgod({ lastRound: 67334733n, timestamp: 1789491426n })
    expect(await chainTimestamp(algod)).toBe(1789491426n)
  })

  it('reports an unreachable node rather than a time', async () => {
    const algod = fakeAlgod({ fail: httpError(502) })
    await expect(chainTimestamp(algod)).rejects.toThrow(ChainUnreachableError)
  })
})

describe('assertNetwork', () => {
  const paramsFor = (network: string) =>
    ({
      genesisHash: algosdk.base64ToBytes(network.slice(network.indexOf(':') + 1)),
    }) as algosdk.SuggestedParams

  it('accepts a node that speaks for the configured chain', () => {
    expect(() => assertNetwork(paramsFor(TESTNET), TESTNET)).not.toThrow()
  })

  it('refuses a TestNet bundle pointed at MainNet', () => {
    // The one misconfiguration nothing else catches: the application id would
    // resolve against a different deployment's boxes.
    expect(() => assertNetwork(paramsFor(MAINNET), TESTNET)).toThrow(NetworkMismatchError)
  })

  it('refuses params that carry no genesis hash at all', () => {
    expect(() => assertNetwork({} as algosdk.SuggestedParams, TESTNET)).toThrow(
      NetworkMismatchError,
    )
  })

  it('defaults to the configured network', () => {
    expect(() => assertNetwork(paramsFor(MAINNET))).toThrow(NetworkMismatchError)
    expect(() => assertNetwork(paramsFor(TESTNET))).not.toThrow()
  })
})

describe('suggestedParams', () => {
  it('checks the network before handing the params back', async () => {
    const algod = fakeAlgod({
      params: { genesisHash: genesisHashOf(MAINNET) },
    })
    await expect(suggestedParams(algod)).rejects.toThrow(NetworkMismatchError)
  })

  it('returns params from the right chain', async () => {
    const algod = fakeAlgod({
      params: {
        genesisHash: genesisHashOf(TESTNET),
        minFee: 1000n,
      } as Partial<algosdk.SuggestedParams>,
    })
    expect((await suggestedParams(algod)).minFee).toBe(1000n)
  })
})

/** An application call as the indexer hands it back. */
const appCall = (selector: string, id: bigint, appId = APP) => ({
  applicationTransaction: {
    applicationId: appId,
    applicationArgs: [
      algosdk.hexToBytes(selector),
      algosdk.encodeUint64(id),
      algosdk.encodeUint64(1n),
    ],
  },
})

const join = (id: bigint, appId = APP) => appCall('f76681b7', id, appId)

type Page = { transactions: unknown[]; nextToken?: string }

/**
 * An indexer that hands back one page per `do()`, in order.
 *
 * `asked` records the tokens it was given, so a test can assert that the
 * second page was actually requested rather than inferred from the result.
 * `types` records every transaction type the query was narrowed to.
 */
function fakeIndexer(pages: Page[] | unknown[], fail?: unknown) {
  const asPages: Page[] = Array.isArray(pages) && pages.every((p) => p && typeof p === 'object' && 'transactions' in (p as object))
    ? (pages as Page[])
    : [{ transactions: pages as unknown[] }]
  const asked: (string | undefined)[] = []
  const types: string[] = []
  const prefixes: (string | Uint8Array)[] = []
  let token: string | undefined
  let served = 0
  const query = {
    address: () => query,
    applicationID: () => query,
    txType: (value: string) => {
      types.push(value)
      return query
    },
    notePrefix: (value: string | Uint8Array) => {
      prefixes.push(value)
      return query
    },
    nextToken: (value: string) => {
      token = value
      return query
    },
    do: async () => {
      if (fail) throw fail
      asked.push(token)
      const page = asPages[Math.min(served, asPages.length - 1)] ?? { transactions: [] }
      served += 1
      return { transactions: page.transactions, nextToken: page.nextToken }
    },
  }
  const indexer = { searchForTransactions: () => query } as unknown as algosdk.Indexer
  return Object.assign(indexer, { asked, types, prefixes })
}

describe('findJoinedAgreements', () => {

  it('reads the agreement id off each join', async () => {
    const found = await findJoinedAgreements(fakeIndexer([join(25n)]), APP, 'ADDR')
    expect(found).toEqual([25n])
  })

  it('asks the indexer for application calls only', async () => {
    const indexer = fakeIndexer([join(25n)])
    await findJoinedAgreements(indexer, APP, 'ADDR')
    expect(indexer.types).toEqual(['appl'])
  })

  it('ignores a call to a different application', async () => {
    // The whole reason the filter is repeated client-side. The account
    // endpoint ignores its application-id parameter outright, and even the
    // search endpoint is a convenience rather than a guarantee -- a seat in
    // an earlier deployment must never render as a seat in this one.
    const found = await findJoinedAgreements(
      fakeIndexer([join(9n, 771376580n), join(25n)]),
      APP,
      'ADDR',
    )
    expect(found).toEqual([25n])
  })

  it('ignores a call that is not a join', async () => {
    const expire = {
      applicationTransaction: {
        applicationId: APP,
        applicationArgs: [algosdk.hexToBytes('575088fd'), algosdk.encodeUint64(25n)],
      },
    }
    expect(await findJoinedAgreements(fakeIndexer([expire]), APP, 'ADDR')).toEqual([])
  })

  it('ignores a transaction that is not an application call', async () => {
    expect(await findJoinedAgreements(fakeIndexer([{}]), APP, 'ADDR')).toEqual([])
  })

  it('ignores a join whose id argument is the wrong width', async () => {
    const malformed = {
      applicationTransaction: {
        applicationId: APP,
        applicationArgs: [algosdk.hexToBytes('f76681b7'), new Uint8Array(4)],
      },
    }
    expect(await findJoinedAgreements(fakeIndexer([malformed]), APP, 'ADDR')).toEqual([])
  })

  it('lists each agreement once', async () => {
    const found = await findJoinedAgreements(
      fakeIndexer([join(25n), join(25n), join(26n)]),
      APP,
      'ADDR',
    )
    expect(found).toEqual([25n, 26n])
  })

  it('reports an unreachable indexer rather than an empty history', async () => {
    await expect(
      findJoinedAgreements(fakeIndexer([], httpError(503)), APP, 'ADDR'),
    ).rejects.toThrow(ChainUnreachableError)
  })
})

describe('the network guard on the read path', () => {
  // A buyer returning to a pool link never purchases, so the purchase path's
  // free check never runs for them. The failure it catches is a read failure:
  // the same application id resolves against another deployment's boxes, and
  // a stranger's pool renders as this buyer's.
  it('refuses to read boxes from a node on another chain', async () => {
    const algod = fakeAlgod({
      box: async () => ({ value: b64(vector.agreement.raw) }),
      params: { genesisHash: genesisHashOf(MAINNET) },
    })
    await expect(readAgreement(algod, APP, 25n)).rejects.toThrow(NetworkMismatchError)
  })

  it('refuses to read the roster from a node on another chain', async () => {
    const algod = fakeAlgod({
      box: async () => ({ value: b64(vector.roster.raw) }),
      params: { genesisHash: genesisHashOf(MAINNET) },
    })
    await expect(readRoster(algod, APP, 25n)).rejects.toThrow(NetworkMismatchError)
  })

  it('refuses to read the clock from a node on another chain', async () => {
    const algod = fakeAlgod({ params: { genesisHash: genesisHashOf(MAINNET) } })
    await expect(chainTimestamp(algod)).rejects.toThrow(NetworkMismatchError)
  })

  it('checks once per client, not once per read', async () => {
    let fetches = 0
    const base = fakeAlgod({ box: async () => ({ value: b64(vector.agreement.raw) }) })
    const algod = {
      ...base,
      getTransactionParams: () => ({
        do: async () => {
          fetches += 1
          return { genesisHash: genesisHashOf(TESTNET) } as algosdk.SuggestedParams
        },
      }),
    } as unknown as algosdk.Algodv2
    await readAgreement(algod, APP, 25n)
    await readAgreement(algod, APP, 25n)
    await readAgreement(algod, APP, 25n)
    expect(fetches).toBe(1)
  })

  it('retries a check the node failed to answer, rather than remembering it', async () => {
    // A mismatch is a property of the build and never changes. An unreachable
    // node is a moment, and memoising it would strand the client for the rest
    // of the session on one bad response.
    let attempts = 0
    const base = fakeAlgod({ box: async () => ({ value: b64(vector.agreement.raw) }) })
    const algod = {
      ...base,
      getTransactionParams: () => ({
        do: async () => {
          attempts += 1
          if (attempts === 1) throw httpError(503)
          return { genesisHash: genesisHashOf(TESTNET) } as algosdk.SuggestedParams
        },
      }),
    } as unknown as algosdk.Algodv2
    await expect(readAgreement(algod, APP, 25n)).rejects.toThrow(ChainUnreachableError)
    expect((await readAgreement(algod, APP, 25n))?.seats).toBe(1)
    expect(attempts).toBe(2)
  })
})

describe('chainTimestamp', () => {
  it('asks for the header alone', async () => {
    // The payset is the rest of the block, and this wants one integer out of
    // the header. On a 30-second refresh, against a public node, in a browser.
    let headerOnly: boolean | null = null
    const algod = fakeAlgod({
      timestamp: 1789651281n,
      onHeaderOnly: (value) => {
        headerOnly = value
      },
    })
    await chainTimestamp(algod)
    expect(headerOnly).toBe(true)
  })

  it('reports an unreachable node rather than a time', async () => {
    const algod = fakeAlgod({ fail: httpError(503) })
    await expect(chainTimestamp(algod)).rejects.toThrow(ChainUnreachableError)
  })
})

describe('paging the indexer', () => {
  it('walks past the first page', async () => {
    // A wallet whose history runs to a second page is precisely the buyer who
    // has been around long enough to have lost their link. Stopping at page
    // one would report their older seats as seats they never held.
    const indexer = fakeIndexer([
      { transactions: [join(25n)], nextToken: 'page-2' },
      { transactions: [join(26n)] },
    ])
    expect(await findJoinedAgreements(indexer, APP, 'ADDR')).toEqual([25n, 26n])
    expect(indexer.asked).toEqual([undefined, 'page-2'])
  })

  it('stops when a server keeps handing back the same token', async () => {
    const indexer = fakeIndexer([{ transactions: [join(25n)], nextToken: 'stuck' }])
    expect(await findJoinedAgreements(indexer, APP, 'ADDR')).toEqual([25n])
    expect(indexer.asked.length).toBe(2)
  })
})

describe('readClosedOutcome', () => {
  // close deletes both boxes, so a delivered pool and a refunded one read
  // identically off algod. Telling a buyer the wrong one is the whole risk.
  it('reads a released pool out of its history', async () => {
    const indexer = fakeIndexer([appCall('81d6f306', 25n)])
    expect(await readClosedOutcome(indexer, APP, 25n)).toBe('released')
  })

  it('reads a refunded pool out of its history', async () => {
    const indexer = fakeIndexer([appCall('575088fd', 25n), appCall('9368b091', 25n)])
    expect(await readClosedOutcome(indexer, APP, 25n)).toBe('refunded')
  })

  it('answers unknown rather than guessing, when the history says nothing', async () => {
    const indexer = fakeIndexer([join(25n)])
    expect(await readClosedOutcome(indexer, APP, 25n)).toBe('unknown')
  })

  it('ignores another agreement in the same application', async () => {
    const indexer = fakeIndexer([appCall('81d6f306', 26n), appCall('9368b091', 25n)])
    expect(await readClosedOutcome(indexer, APP, 25n)).toBe('refunded')
  })

  it('ignores another application entirely', async () => {
    const indexer = fakeIndexer([appCall('81d6f306', 25n, 771376580n)])
    expect(await readClosedOutcome(indexer, APP, 25n)).toBe('unknown')
  })

  it('asks the indexer for application calls only', async () => {
    const indexer = fakeIndexer([appCall('81d6f306', 25n)])
    await readClosedOutcome(indexer, APP, 25n)
    expect(indexer.types).toEqual(['appl'])
  })

  it('stops walking the history once it has found how the pool ended', async () => {
    // The history is every pool the application has run. Walking past the
    // answer spends pages on later pools, and at MAX_PAGES a recent pool's
    // answer would be out of reach.
    const indexer = fakeIndexer([
      { transactions: [join(25n), appCall('575088fd', 25n)], nextToken: 'page-2' },
      { transactions: [appCall('9368b091', 26n)], nextToken: 'page-3' },
    ])
    expect(await readClosedOutcome(indexer, APP, 25n)).toBe('refunded')
    expect(indexer.asked).toEqual([undefined])
  })

  it('keeps walking past pages that only concern other agreements', async () => {
    const indexer = fakeIndexer([
      { transactions: [appCall('81d6f306', 26n)], nextToken: 'page-2' },
      { transactions: [appCall('81d6f306', 25n)] },
    ])
    expect(await readClosedOutcome(indexer, APP, 25n)).toBe('released')
    expect(indexer.asked).toEqual([undefined, 'page-2'])
  })

  it('reports an unreachable indexer rather than an unknown outcome', async () => {
    // "I could not ask" is not "I asked and nothing came back". One is worth
    // retrying; the other is an answer.
    await expect(
      readClosedOutcome(fakeIndexer([], httpError(503)), APP, 25n),
    ).rejects.toThrow(ChainUnreachableError)
  })
})

describe('readEditionLink', () => {
  // The note TestNet agreement 26 was released with, read from the indexer on
  // 2026-09-17.
  const CID = 'bafybeidahqbf2ym3bx3cbv7czf6t2ois53p6sv5pnj44fv34atm55fdr4i'
  const NOTE = `earnest:index:edition-900:agreement:26:cid:${CID}`
  const CREATION_NOTE = `earnest:index:edition-900:sha256:${'fa'.repeat(32)}`

  /** A committed call as the indexer hands it back, note included. */
  const noted = (
    note: string | Uint8Array,
    {
      selector = SELECTORS.releaseHash,
      id = 26n,
      appId = APP,
    }: { selector?: string; id?: bigint; appId?: bigint } = {},
  ) => ({
    applicationTransaction: {
      applicationId: appId,
      applicationArgs: [
        algosdk.hexToBytes(selector),
        algosdk.encodeUint64(id),
        new Uint8Array(32).fill(0xfa),
      ],
    },
    note: typeof note === 'string' ? new TextEncoder().encode(note) : note,
  })

  it('reads the edition and the CID off the release note', async () => {
    const indexer = fakeIndexer([noted(NOTE)])
    expect(await readEditionLink(indexer, APP, 26n)).toEqual({ edition: '900', cid: CID })
  })

  it('asks for application calls whose note starts with the edition prefix, as bytes', async () => {
    // algosdk takes a *string* prefix to be base64 already and sends it as it
    // is, so the plain text would search for a prefix nobody wrote.
    const indexer = fakeIndexer([noted(NOTE)])
    await readEditionLink(indexer, APP, 26n)
    expect(indexer.types).toEqual(['appl'])
    expect(indexer.prefixes).toHaveLength(1)
    const prefix = indexer.prefixes[0]
    expect(prefix).toBeInstanceOf(Uint8Array)
    expect(new TextDecoder().decode(prefix as Uint8Array)).toBe('earnest:index:edition-')
  })

  it('answers null when the history holds no release note for the pool', async () => {
    // Released without one: a test pool, or a release sent some other way.
    // An answer, and not a failure -- there is no link to show.
    expect(await readEditionLink(fakeIndexer([]), APP, 26n)).toBeNull()
  })

  it('ignores a note that names another agreement', async () => {
    const other = NOTE.replace(':agreement:26:', ':agreement:27:')
    expect(await readEditionLink(fakeIndexer([noted(other)]), APP, 26n)).toBeNull()
  })

  it("ignores this pool's note on the release of another agreement", async () => {
    const indexer = fakeIndexer([noted(NOTE, { id: 27n })])
    expect(await readEditionLink(indexer, APP, 26n)).toBeNull()
  })

  it('ignores the note on a call that is not release_hash', async () => {
    // Anyone can write a note with this prefix, on any call the contract
    // accepts from them. Only `release_hash` is limited to the verifier.
    for (const selector of [SELECTORS.join, SELECTORS.releaseQuorum, SELECTORS.expire]) {
      const indexer = fakeIndexer([noted(NOTE, { selector })])
      expect(await readEditionLink(indexer, APP, 26n), selector).toBeNull()
    }
  })

  it('ignores a release note on another application', async () => {
    const indexer = fakeIndexer([noted(NOTE, { appId: 771376580n })])
    expect(await readEditionLink(indexer, APP, 26n)).toBeNull()
  })

  it('ignores notes that only look like the release note', async () => {
    const lookalikes = [
      // The note the creating call carries, which shares the prefix.
      CREATION_NOTE,
      `${NOTE}\n`,
      ` ${NOTE}`,
      `x${NOTE}`,
      NOTE.replace(':agreement:26:', ':agreement:026:'),
      NOTE.replace('edition-900', 'edition-0900'),
      NOTE.replace('edition-900', 'edition-0'),
      `${NOTE}:cid:${CID}`,
    ]
    for (const note of lookalikes) {
      const indexer = fakeIndexer([noted(note)])
      expect(await readEditionLink(indexer, APP, 26n), note).toBeNull()
    }
  })

  it('ignores a malformed CID', async () => {
    const malformed = ['', `${CID}/edition-900.html`, `${CID}?`, '../../evil', 'bafy.example']
    for (const cid of malformed) {
      const indexer = fakeIndexer([noted(NOTE.replace(CID, cid))])
      expect(await readEditionLink(indexer, APP, 26n), cid).toBeNull()
    }
  })

  it('ignores a note that is not UTF-8', async () => {
    const bytes = new TextEncoder().encode(NOTE)
    bytes[bytes.length - 1] = 0xff
    expect(await readEditionLink(fakeIndexer([noted(bytes)]), APP, 26n)).toBeNull()
  })

  it('finds the release on a later page', async () => {
    const indexer = fakeIndexer([
      { transactions: [noted(CREATION_NOTE, { selector: '3ba106ea' })], nextToken: 'page-2' },
      { transactions: [noted(NOTE)] },
    ])
    expect(await readEditionLink(indexer, APP, 26n)).toEqual({ edition: '900', cid: CID })
    expect(indexer.asked).toEqual([undefined, 'page-2'])
  })

  it('stops at the page that holds the release', async () => {
    const indexer = fakeIndexer([
      { transactions: [noted(NOTE)], nextToken: 'page-2' },
      { transactions: [], nextToken: 'page-3' },
    ])
    await readEditionLink(indexer, APP, 26n)
    expect(indexer.asked).toEqual([undefined])
  })

  it('keeps the release note when a forged one comes first', async () => {
    const forged = noted(NOTE.replace(CID, 'bafyforged'), { selector: SELECTORS.join })
    const indexer = fakeIndexer([forged, noted(NOTE)])
    expect(await readEditionLink(indexer, APP, 26n)).toEqual({ edition: '900', cid: CID })
  })

  it('reports an unreachable indexer rather than a pool with no link', async () => {
    await expect(
      readEditionLink(fakeIndexer([], httpError(503)), APP, 26n),
    ).rejects.toThrow(ChainUnreachableError)
  })

  describe('for the travel kind', () => {
    const TRIP_NOTE = `earnest:travel:trip-901:agreement:26:cid:${CID}`

    it('reads the trip number and the CID off a trip release note', async () => {
      const indexer = fakeIndexer([noted(TRIP_NOTE)])
      expect(await readEditionLink(indexer, APP, 26n, 'travel')).toEqual({
        edition: '901',
        cid: CID,
      })
    })

    it('asks for the trip prefix, as bytes', async () => {
      const indexer = fakeIndexer([noted(TRIP_NOTE)])
      await readEditionLink(indexer, APP, 26n, 'travel')
      expect(indexer.prefixes).toHaveLength(1)
      const prefix = indexer.prefixes[0]
      expect(prefix).toBeInstanceOf(Uint8Array)
      expect(new TextDecoder().decode(prefix as Uint8Array)).toBe('earnest:travel:trip-')
    })

    it('reads the index by default', async () => {
      // Every caller that names no kind is the index, whose notes are unchanged.
      expect(await readEditionLink(fakeIndexer([noted(TRIP_NOTE)]), APP, 26n)).toBeNull()
    })

    it("never takes one kind's note for the other's", async () => {
      expect(await readEditionLink(fakeIndexer([noted(NOTE)]), APP, 26n, 'travel')).toBeNull()
      expect(await readEditionLink(fakeIndexer([noted(TRIP_NOTE)]), APP, 26n, 'index')).toBeNull()
    })

    it('ignores the trip note on a call that is not release_hash', async () => {
      for (const selector of [SELECTORS.join, SELECTORS.releaseQuorum, SELECTORS.expire]) {
        const indexer = fakeIndexer([noted(TRIP_NOTE, { selector })])
        expect(await readEditionLink(indexer, APP, 26n, 'travel'), selector).toBeNull()
      }
    })

    it('ignores notes that only look like the trip release note', async () => {
      const lookalikes = [
        `earnest:travel:trip-901:sha256:${'fa'.repeat(32)}`,
        `${TRIP_NOTE}\n`,
        TRIP_NOTE.replace(':agreement:26:', ':agreement:27:'),
        TRIP_NOTE.replace('trip-901', 'trip-0901'),
        TRIP_NOTE.replace('trip-901', 'trip-0'),
        TRIP_NOTE.replace('earnest:travel:trip-', 'earnest:travel:edition-'),
        TRIP_NOTE.replace('earnest:travel:', 'earnest:index:'),
        TRIP_NOTE.replace(CID, `${CID}/trip-901.html`),
      ]
      for (const note of lookalikes) {
        const indexer = fakeIndexer([noted(note)])
        expect(await readEditionLink(indexer, APP, 26n, 'travel'), note).toBeNull()
      }
    })
  })
})
