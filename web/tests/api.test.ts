import algosdk from 'algosdk'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  RouteUnavailableError,
  SettlementUnreadableError,
  getByHash,
  getJob,
  requestQuote,
  requestSeatQuote,
  settle,
  settleSeat,
  settlementOf,
  uploadBytes,
  type QuotedRoute,
} from '../src/api'
import { resetConfigForTests } from '../src/config'

const NETWORK = 'algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI='
const PAY_TO = 'IUHCGQZG5KF2T5BMWXZFCH64T6ZAEU5NHTEWRHMG2644WK55PZ7Z34FAKM'

beforeEach(() => {
  // getConfig() reads import.meta.env on first use and memoises, so the stub
  // has to be in place and the memo cleared before each test.
  vi.stubEnv('VITE_NETWORK', NETWORK)
  vi.stubEnv('VITE_ASSET_ID', '10458941')
  vi.stubEnv('VITE_APP_ID', '741000001')
  vi.stubEnv('VITE_FACILITATOR_URL', 'https://facilitator.example')
  vi.stubEnv('VITE_RESOURCE_HOST', 'https://earnest.example')
  vi.stubEnv('VITE_MAX_FILE_BYTES', '16777216')
  vi.stubEnv('VITE_ALGOD_URL', 'https://algod.example')
  vi.stubEnv('VITE_INDEXER_URL', 'https://indexer.example')
  vi.stubEnv('VITE_IPFS_GATEWAY', 'https://ipfs.example')
  resetConfigForTests()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  resetConfigForTests()
})

const json = (body: unknown, init: ResponseInit = {}) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  })

const b64 = (value: unknown) =>
  algosdk.bytesToBase64(new TextEncoder().encode(JSON.stringify(value)))

const ACCEPTED = {
  scheme: 'exact',
  network: NETWORK,
  amount: '10000',
  asset: '10458941',
  payTo: PAY_TO,
  maxTimeoutSeconds: 120,
  extra: { feePayer: 'FEE', agreementId: 42, decimals: 6, tag: 'x402-global-challenge' },
}

/**
 * A 402 the way a real server sends one: the requirements in the
 * `PAYMENT-REQUIRED` header, and an empty body. A client that reads `accepts`
 * out of the body sees nothing at all here, which is the point.
 */
const quote402 = (
  overrides: Record<string, unknown> = {},
  envelopeOverrides: Record<string, unknown> = {},
) =>
  new Response(JSON.stringify({}), {
    status: 402,
    headers: {
      'content-type': 'application/json',
      'PAYMENT-REQUIRED': b64({
        x402Version: 2,
        error: 'payment required',
        resource: { url: 'https://earnest.example/pin' },
        extensions: { bazaar: { declared: true } },
        accepts: [{ ...ACCEPTED, ...overrides }],
        ...envelopeOverrides,
      }),
    },
  })

const PAYLOAD = { paymentGroup: ['x', 'y', 'z'], paymentIndex: 1 }

const decodeHeader = (headers: Record<string, string>) =>
  JSON.parse(new TextDecoder().decode(algosdk.base64ToBytes(headers['PAYMENT-SIGNATURE']!)))

describe('requestQuote', () => {
  it('reads the requirements out of the PAYMENT-REQUIRED header, not the body', async () => {
    const fetchMock = vi.fn(
      async (_url: string | URL, _init?: RequestInit) => quote402(),
    )
    vi.stubGlobal('fetch', fetchMock)

    const { quote } = await requestQuote('a'.repeat(64), 1024)
    expect(quote.agreementId).toBe(42n)
    expect(quote.amount).toBe(10000n)
    expect(quote.feePayer).toBe('FEE')
    expect(quote.payTo).toBe(PAY_TO)

    const init = fetchMock.mock.calls[0]![1]
    expect(JSON.stringify(init?.headers ?? {})).not.toContain('PAYMENT-SIGNATURE')
  })

  it('keeps the accepted requirements verbatim for the paid retry', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => quote402()))
    const quoted = await requestQuote('a'.repeat(64), 1024)
    // Not re-spelled from the parsed quote: the facilitator compares what it
    // is sent against what it issued.
    expect(quoted.accepted).toEqual(ACCEPTED)
    expect(quoted.envelope.x402Version).toBe(2)
  })

  it('throws when the 402 carries no header at all', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ accepts: [ACCEPTED] }, { status: 402 })),
    )
    // The body-carrying shape this client used to expect: now a named failure
    // rather than a quote that silently reads as unpayable.
    await expect(requestQuote('a'.repeat(64), 1024)).rejects.toThrow(
      /PAYMENT-REQUIRED header/,
    )
  })

  it('throws when the header is not base64 JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response('{}', {
            status: 402,
            headers: { 'PAYMENT-REQUIRED': 'not base64 at all !!!' },
          }),
      ),
    )
    await expect(requestQuote('a'.repeat(64), 1024)).rejects.toThrow(/not valid base64|not JSON/)
  })

  it('names the header it could not read, so the message points somewhere', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response('{}', {
            status: 402,
            headers: { 'PAYMENT-REQUIRED': b64('not an object at all') },
          }),
      ),
    )
    // A bare string parses as JSON, and casting it to an envelope would make
    // every field undefined -- which reads downstream as a 402 offering
    // nothing payable rather than as a malformed header.
    await expect(requestQuote('a'.repeat(64), 1024)).rejects.toThrow(
      /PAYMENT-REQUIRED header is not a JSON object/,
    )
  })

  it('refuses an envelope that is a JSON array', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response('{}', {
            status: 402,
            headers: { 'PAYMENT-REQUIRED': b64([{ scheme: 'exact' }]) },
          }),
      ),
    )
    // typeof [] === 'object', so the array case needs its own guard.
    await expect(requestQuote('a'.repeat(64), 1024)).rejects.toThrow(/not a JSON object/)
  })

  it('throws when the 402 carries no acceptable requirement', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response('{}', {
            status: 402,
            headers: { 'PAYMENT-REQUIRED': b64({ x402Version: 2, accepts: [] }) },
          }),
      ),
    )
    await expect(requestQuote('a'.repeat(64), 1024)).rejects.toThrow(/nothing this client/)
  })

  it('refuses a requirement for a different network', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => quote402({ network: 'algorand:something-else' })),
    )
    await expect(requestQuote('a'.repeat(64), 1024)).rejects.toThrow(/nothing this client/)
  })

  it('refuses a requirement for a different asset', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => quote402({ asset: '31566704' })))
    await expect(requestQuote('a'.repeat(64), 1024)).rejects.toThrow(/nothing this client/)
  })

  it('refuses a requirement missing agreementId', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => quote402({ extra: { feePayer: 'FEE' } })))
    await expect(requestQuote('a'.repeat(64), 1024)).rejects.toThrow(/agreementId/)
  })

  it('refuses a requirement that names no amount', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => quote402({ amount: undefined, maxAmountRequired: undefined })),
    )
    await expect(requestQuote('a'.repeat(64), 1024)).rejects.toThrow(/names no amount/)
  })

  it('still reads a V1 maxAmountRequired, so an older server is not silently unpayable', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => quote402({ amount: undefined, maxAmountRequired: '7777' })),
    )
    const { quote } = await requestQuote('a'.repeat(64), 1024)
    expect(quote.amount).toBe(7777n)
  })

  it('throws when the route answers anything but a 402', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({}, { status: 200 })))
    await expect(requestQuote('a'.repeat(64), 1024)).rejects.toThrow(/expected a 402/)
  })
})

describe('the query string', () => {
  it('is byte-identical on the unpaid and paid passes', async () => {
    // The server tells the passes apart by the payment header and looks the
    // agreement up on the second. A differing query string is a different job.
    const urls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        urls.push(String(url))
        return urls.length === 1
          ? quote402()
          : json({ jobId: 'j1', agreementId: 42, deadline: 1756000600 })
      }),
    )

    const sha256 = 'a'.repeat(64)
    const quoted = await requestQuote(sha256, 1024)
    await settle(sha256, 1024, quoted, PAYLOAD)
    expect(urls[0]).toBe(urls[1])
    expect(urls[0]).toBe(`https://earnest.example/pin?sha256=${sha256}&size=1024`)
  })
})

/** A quote as `requestQuote` would have returned it, without the round trip. */
const quoted = (): QuotedRoute => ({
  quote: {
    agreementId: 42n,
    amount: 10000n,
    assetId: 10458941n,
    payTo: PAY_TO,
    feePayer: 'FEE',
    appId: 741000001n,
    expiresAt: 0,
  },
  accepted: ACCEPTED,
  envelope: {
    x402Version: 2,
    resource: { url: 'https://earnest.example/pin' },
    extensions: { bazaar: { declared: true } },
    accepts: [ACCEPTED],
  },
})

describe('settle', () => {
  it('sends a whole payment payload, not the bare group', async () => {
    const fetchMock = vi.fn(async (_url: string | URL, _init?: RequestInit) =>
      json({ jobId: 'j1', agreementId: 42, deadline: 1756000600 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const result = await settle('a'.repeat(64), 1024, quoted(), PAYLOAD)
    expect(result.jobId).toBe('j1')

    const headers = fetchMock.mock.calls[0]![1]!.headers as Record<string, string>
    expect(headers).not.toHaveProperty('X-PAYMENT')
    const decoded = decodeHeader(headers)
    // The inner object alone fails to decode at the facilitator; these three
    // wrappers are what make it a PaymentPayload.
    expect(decoded.x402Version).toBe(2)
    expect(decoded.accepted).toEqual(ACCEPTED)
    expect(decoded.resource).toEqual({ url: 'https://earnest.example/pin' })
    expect(decoded.payload).toEqual({ paymentGroup: ['x', 'y', 'z'], paymentIndex: 1 })
  })

  it('omits resource and extensions when the 402 carried none', async () => {
    // The server serialises with exclude_none, so a null here is a field the
    // facilitator did not send and must not be sent back.
    const fetchMock = vi.fn(async (_url: string | URL, _init?: RequestInit) =>
      json({ jobId: 'j1', agreementId: 42, deadline: 1 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const bare = quoted()
    bare.envelope = { x402Version: 2, accepts: [ACCEPTED] }
    await settle('a'.repeat(64), 1024, bare, PAYLOAD)

    const headers = fetchMock.mock.calls[0]![1]!.headers as Record<string, string>
    const decoded = decodeHeader(headers)
    expect(decoded).not.toHaveProperty('resource')
    expect(decoded).not.toHaveProperty('extensions')
  })

  it('encodes a non-Latin1 description without throwing', async () => {
    // btoa() throws above U+00FF, and the echoed envelope carries the route's
    // own description. Encoding via UTF-8 bytes is what makes this survive.
    const fetchMock = vi.fn(async (_url: string | URL, _init?: RequestInit) =>
      json({ jobId: 'j1', agreementId: 42, deadline: 1 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const accented = quoted()
    accented.accepted = { ...ACCEPTED, description: 'édition — 1 790 routes' }
    await settle('a'.repeat(64), 1024, accented, PAYLOAD)

    const headers = fetchMock.mock.calls[0]![1]!.headers as Record<string, string>
    expect(decodeHeader(headers).accepted.description).toBe('édition — 1 790 routes')
  })

  it('throws on a non-ok settlement so the machine can resolve it', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({}, { status: 409 })))
    await expect(
      settle('a'.repeat(64), 1024, quoted(), PAYLOAD),
    ).rejects.toThrow()
  })

  // Everything below is a 200: the money has already moved. The distinction
  // that matters is that none of these is a settlement *failure*, because
  // reporting one as such sends the machine to the resolve fork holding a
  // paid-for job -- and that fork can end in a second payment.
  const settled = () => settle('a'.repeat(64), 1024, quoted(), PAYLOAD)

  it('refuses a 200 with no jobId rather than minting the string "undefined"', async () => {
    // String(body.jobId) once made "undefined" the job reference: the buyer
    // was navigated to /app/j/undefined and the upload addressed there.
    vi.stubGlobal('fetch', vi.fn(async () => json({ agreementId: 42, deadline: 1 })))
    await expect(settled()).rejects.toThrow(SettlementUnreadableError)
    await expect(settled()).rejects.toThrow(/do not pay again/)
  })

  it('refuses a 200 with an unreadable agreementId', async () => {
    // BigInt(String(undefined)) throws a bare SyntaxError from inside a
    // success path, which reads as a settlement failure to every caller.
    vi.stubGlobal('fetch', vi.fn(async () => json({ jobId: 'j1', deadline: 1 })))
    await expect(settled()).rejects.toThrow(SettlementUnreadableError)
  })

  it('refuses a 200 with an unreadable deadline', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ jobId: 'j1', agreementId: 42, deadline: 'soon' })),
    )
    await expect(settled()).rejects.toThrow(SettlementUnreadableError)
  })

  it('refuses a 200 that is not JSON', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('not json', { status: 200 })))
    await expect(settled()).rejects.toThrow(SettlementUnreadableError)
  })

  it('reads a well-formed receipt', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ jobId: 'j1', agreementId: '42', deadline: 1756000600 })),
    )
    expect(await settled()).toEqual({ jobId: 'j1', agreementId: 42n, deadline: 1756000600 })
  })

  it('is an ApiError too, so a caller that only knows ApiError still catches it', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ agreementId: 42, deadline: 1 })))
    await expect(settled()).rejects.toThrow(ApiError)
  })
})

describe('the pooled route', () => {
  it('quotes a seat with no query string at all', async () => {
    const fetchMock = vi.fn(
      async (_url: string | URL, _init?: RequestInit) => quote402(),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { quote } = await requestSeatQuote()
    expect(quote.agreementId).toBe(42n)
    // The seat sold is whichever pool is open; nothing about the request
    // names one, so the two passes cannot disagree about which.
    expect(String(fetchMock.mock.calls[0]![0])).toBe('https://earnest.example/index')
  })

  it('reads the seat receipt', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({
          agreementId: 42,
          seatsTotal: 5,
          deadline: 1758000000,
          commitSha256: 'b'.repeat(64),
        }),
      ),
    )
    const { receipt, settlement } = await settleSeat(quoted(), PAYLOAD)
    expect(receipt).toEqual({
      agreementId: 42n,
      seatsTotal: 5,
      deadline: 1758000000,
      commitSha256: 'b'.repeat(64),
    })
    // Absent is an answer: a server that attached no header is not an error.
    expect(settlement).toBeNull()
  })

  it('surfaces the facilitator settlement when one is attached', async () => {
    // The only name either side can point at afterwards, and the one piece
    // of evidence that comes from the facilitator rather than from us.
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              agreementId: 42,
              seatsTotal: 5,
              deadline: 1,
              commitSha256: 'b'.repeat(64),
            }),
            {
              status: 200,
              headers: {
                'content-type': 'application/json',
                'PAYMENT-RESPONSE': b64({ success: true, transaction: 'TXID' }),
              },
            },
          ),
      ),
    )
    const { settlement } = await settleSeat(quoted(), PAYLOAD)
    expect(settlement).toEqual({ success: true, transaction: 'TXID' })
  })

  it('reads the receipt when the facilitator header is garbled', async () => {
    // The header arrives on a 200, so the money has already moved. Throwing
    // here would surface as a plain ApiError, which reads as a payment that
    // did not go through; the header is secondary, and `null` already means
    // the facilitator made no statement.
    const receipt = { agreementId: 42, seatsTotal: 5, deadline: 1, commitSha256: 'b'.repeat(64) }
    for (const garbled of ['not base64 json', b64('a string, not an object'), b64([1, 2])]) {
      vi.stubGlobal(
        'fetch',
        vi.fn(
          async () =>
            new Response(JSON.stringify(receipt), {
              status: 200,
              headers: { 'content-type': 'application/json', 'PAYMENT-RESPONSE': garbled },
            }),
        ),
      )
      const purchase = await settleSeat(quoted(), PAYLOAD)
      expect(purchase.settlement).toBeNull()
      expect(purchase.receipt.agreementId).toBe(42n)
    }
  })

  it('refuses a seat receipt with no commitment', async () => {
    // Without it the buyer cannot check the delivery they paid for, which is
    // the entire product.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ agreementId: 42, seatsTotal: 5, deadline: 1 })),
    )
    await expect(settleSeat(quoted(), PAYLOAD)).rejects.toThrow(SettlementUnreadableError)
  })

  it('reports "no pool open" as its own reason, not as a generic failure', async () => {
    // The pool gate answers 503 before the payment middleware is reached, and
    // this one is an answer: the chain was read and no edition is open. A
    // caller has to be able to show that differently from a broken server.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ error: 'no_pool_open' }, { status: 503 })),
    )
    await expect(requestSeatQuote()).rejects.toThrow(RouteUnavailableError)
    await expect(requestSeatQuote()).rejects.toMatchObject({ reason: 'no_pool_open' })
  })

  it('keeps the transient 503 distinct from the settled one', async () => {
    // The server could not reach the chain, so whether a pool is open is
    // unknown -- the opposite of an answer, on the same status code.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ error: 'pool_status_unavailable' }, { status: 503 })),
    )
    await expect(requestSeatQuote()).rejects.toMatchObject({
      reason: 'pool_status_unavailable',
    })
  })

  it('does not invent a reason for a 503 with an unreadable body', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('not json', { status: 503 })),
    )
    await expect(requestSeatQuote()).rejects.toMatchObject({ reason: '' })
  })

  it('is an ApiError, so a caller that only knows ApiError still catches it', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ error: 'no_pool_open' }, { status: 503 })),
    )
    await expect(requestSeatQuote()).rejects.toThrow(ApiError)
  })
})

describe('settlementOf', () => {
  it('decodes the facilitator receipt when one is attached', () => {
    const response = new Response('{}', {
      headers: { 'PAYMENT-RESPONSE': b64({ success: true, payer: PAY_TO }) },
    })
    expect(settlementOf(response)).toEqual({ success: true, payer: PAY_TO })
  })

  it('returns null when the server attached none', () => {
    expect(settlementOf(new Response('{}'))).toBeNull()
  })

  it('blames PAYMENT-RESPONSE, not PAYMENT-REQUIRED, when that header is malformed', () => {
    // The decoder is shared between the two headers, so an error message
    // naming the wrong one sends a reader to the wrong half of the exchange.
    const response = new Response('{}', {
      headers: { 'PAYMENT-RESPONSE': b64('a bare string') },
    })
    expect(() => settlementOf(response)).toThrow(/PAYMENT-RESPONSE header is not a JSON object/)
  })
})

describe('getByHash', () => {
  it('returns null on a 404 rather than throwing', async () => {
    // Bounded by the server's recent-release window: a 404 is an answer.
    vi.stubGlobal('fetch', vi.fn(async () => json({}, { status: 404 })))
    expect(await getByHash('a'.repeat(64))).toBeNull()
  })

  it('reads the state and the CID', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ state: 'RELEASED', agreementId: 42, cid: 'bafy' })),
    )
    expect(await getByHash('a'.repeat(64))).toEqual({
      state: 'RELEASED',
      agreementId: 42n,
      cid: 'bafy',
    })
  })

  it('refuses a state it does not know rather than passing it on', async () => {
    // The expensive direction: an unrecognised state used to be cast through
    // and priced by the resolve fork as REQUOTE, which parks a second deposit
    // against bytes that may already be paid for. A failed response is the
    // caller's problem; a wrong decision is the buyer's money.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => json({ state: 'SETTLING', agreementId: 42 })),
    )
    await expect(getByHash('a'.repeat(64))).rejects.toThrow(ApiError)
  })
})

describe('getJob', () => {
  it('returns null on a 404', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({}, { status: 404 })))
    expect(await getJob('nope')).toBeNull()
  })

  it('reads the owner view including the deadline', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({
          state: 'FUNDED',
          sha256: 'a'.repeat(64),
          size: 1024,
          deadline: 1756000600,
          agreementId: 42,
        }),
      ),
    )
    const job = await getJob('j1')
    expect(job?.state).toBe('FUNDED')
    expect(job?.deadline).toBe(1756000600)
    expect(job?.agreementId).toBe(42n)
  })

  it('refuses a state it does not know', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({
          state: 'settling',
          sha256: 'a'.repeat(64),
          size: 1024,
          deadline: 1756000600,
          agreementId: 42,
        }),
      ),
    )
    // Case matters: the union is upper-case and a near-miss is still a miss.
    await expect(getJob('j1')).rejects.toThrow(ApiError)
  })
})

describe('uploadBytes', () => {
  it('posts the bytes to the job content route', async () => {
    const fetchMock = vi.fn(async (_url: string | URL, _init?: RequestInit) =>
      json({ cid: 'bafy', gatewayUrl: 'https://g/bafy' }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const result = await uploadBytes('j1', new Blob(['abc']))
    expect(result.cid).toBe('bafy')
    expect(String(fetchMock.mock.calls[0]![0])).toContain('/pin/j1/content')
  })

  it('surfaces the server detail on a commit_hash mismatch', async () => {
    // A mismatch is a 400 that spends nothing, and the buyer may retry until
    // the deadline -- so the reason has to reach them.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({ detail: 'those bytes do not match the commitment' }, { status: 400 }),
      ),
    )
    await expect(uploadBytes('j1', new Blob(['abc']))).rejects.toThrow(
      /do not match the commitment/,
    )
  })
})
