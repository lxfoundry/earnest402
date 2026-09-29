import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ConfigError, getBrand, loadBrand, loadConfig, resetBrandForTests } from '../src/config'

const complete = {
  VITE_NETWORK: 'algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=',
  VITE_ASSET_ID: '10458941',
  VITE_APP_ID: '741000001',
  VITE_FACILITATOR_URL: 'https://facilitator.example',
  VITE_RESOURCE_HOST: 'https://earnest.example',
  VITE_MAX_FILE_BYTES: '10485760',
  VITE_ALGOD_URL: 'https://algod.example',
  VITE_INDEXER_URL: 'https://indexer.example',
  VITE_IPFS_GATEWAY: 'https://ipfs.example',
}

describe('loadConfig', () => {
  it('reads a complete configuration', () => {
    const config = loadConfig(complete)
    expect(config.appId).toBe(741000001n)
    expect(config.assetId).toBe(10458941n)
    expect(config.maxFileBytes).toBe(10485760)
  })

  for (const key of Object.keys(complete)) {
    it(`refuses to start without ${key}`, () => {
      const partial = { ...complete, [key]: undefined }
      expect(() => loadConfig(partial)).toThrow(ConfigError)
    })
  }

  it('treats an empty string as absent', () => {
    // .env templates ship optional keys present-and-empty, so a plain
    // `env[key] ?? default` finds the key and returns "".
    expect(() => loadConfig({ ...complete, VITE_APP_ID: '' })).toThrow(ConfigError)
  })

  it('refuses a non-numeric application id', () => {
    expect(() => loadConfig({ ...complete, VITE_APP_ID: 'later' })).toThrow(ConfigError)
  })

  it('refuses the transaction genesisID in place of the CAIP-2 network', () => {
    // The confusion that shipped a broken client template: with this value
    // the 402 offers nothing the client recognises, and the wallet layer's
    // LocalNet fallback would quietly connect a production build to a local
    // node rather than failing.
    expect(() =>
      loadConfig({ ...complete, VITE_NETWORK: 'algorand:testnet-v1.0' }),
    ).toThrow(ConfigError)
  })

  it('refuses a network identifier with no chain prefix', () => {
    expect(() => loadConfig({ ...complete, VITE_NETWORK: 'testnet' })).toThrow(
      ConfigError,
    )
  })

  it('refuses a resource host that is not https', () => {
    expect(() =>
      loadConfig({ ...complete, VITE_RESOURCE_HOST: 'http://earnest.example' }),
    ).toThrow(ConfigError)
  })

  it('allows http on localhost, which is what LocalNet serves', () => {
    expect(() =>
      loadConfig({ ...complete, VITE_RESOURCE_HOST: 'http://localhost:8000' }),
    ).not.toThrow()
  })

  it('strips a trailing slash so joined paths are unambiguous', () => {
    expect(
      loadConfig({ ...complete, VITE_RESOURCE_HOST: 'https://earnest.example/' })
        .resourceHost,
    ).toBe('https://earnest.example')
  })
})

describe('the IPFS gateway', () => {
  it('is read as an origin, without a trailing slash', () => {
    expect(
      loadConfig({ ...complete, VITE_IPFS_GATEWAY: 'https://gateway.example/' }).ipfsGateway,
    ).toBe('https://gateway.example')
  })

  it('refuses a gateway that is not https', () => {
    expect(() =>
      loadConfig({ ...complete, VITE_IPFS_GATEWAY: 'http://gateway.example' }),
    ).toThrow(ConfigError)
  })

  it('refuses a gateway given with a path, which the link would repeat', () => {
    // The link is `<gateway>/ipfs/<cid>/...`. A gateway written the way a
    // browser shows a resolved link, with `/ipfs` on the end, would build
    // `/ipfs/ipfs/<cid>` -- a link that resolves nowhere, shown to a buyer as
    // what they paid for.
    expect(() =>
      loadConfig({ ...complete, VITE_IPFS_GATEWAY: 'https://gateway.example/ipfs' }),
    ).toThrow(ConfigError)
  })

  it('refuses a gateway carrying credentials, without repeating them', () => {
    // Dropping them silently would build links that fail to authenticate, and
    // anything accepted here is compiled into a bundle anyone can download.
    // The refusal is shown on the page, so it must not print them either --
    // not even when the value is wrong in some other way too.
    for (const value of [
      'https://user:hunter2@gateway.example',
      'https://user:hunter2@gateway.example/ipfs',
      'https://hunter2@gateway.example',
      'http://user:hunter2@gateway.example',
    ]) {
      let refusal: unknown
      try {
        loadConfig({ ...complete, VITE_IPFS_GATEWAY: value })
      } catch (error) {
        refusal = error
      }
      expect(refusal, value).toBeInstanceOf(ConfigError)
      expect((refusal as Error).message, value).toMatch(/credentials/i)
      expect((refusal as Error).message, value).not.toContain('hunter2')
    }
  })
})

// The synthetic offer every travel test uses. Encoded here, at run time: no
// base64 of an offer is ever committed, so what a build will show can always
// be read in plain text.
const OFFER = {
  v: 1,
  title: 'Example City beach week',
  city: 'Example City',
  iata: 'EXC',
  origin: 'Sample Town',
  originIata: 'SMP',
  depart: '2026-12-12',
  return: '2026-12-19',
  nights: 7,
  travellers: 3,
  fare: { amount: '612.40', currency: 'USD', basis: 'per traveller, round trip' },
  source: 'Example travel API, test environment. Indicative, not bookable.',
  fetchedAt: '2026-09-28T14:03:00Z',
}

const encode = (value: unknown): string =>
  Buffer.from(typeof value === 'string' ? value : JSON.stringify(value), 'utf8').toString(
    'base64',
  )

const travel = (offer: unknown) => ({ VITE_BRAND: 'travel', VITE_OFFER_B64: encode(offer) })

/** The refusal `loadBrand` throws for `env`, which must be a ConfigError. */
function refusal(env: Record<string, string | undefined>): ConfigError {
  let thrown: unknown
  try {
    loadBrand(env)
  } catch (error) {
    thrown = error
  }
  expect(thrown).toBeInstanceOf(ConfigError)
  return thrown as ConfigError
}

describe('the brand', () => {
  it('is the index when VITE_BRAND is unset or empty', () => {
    expect(loadBrand({})).toEqual({ brand: 'index' })
    expect(loadBrand({ VITE_BRAND: '' })).toEqual({ brand: 'index' })
  })

  it('reads index and travel', () => {
    expect(loadBrand({ VITE_BRAND: 'index' })).toEqual({ brand: 'index' })
    expect(loadBrand({ VITE_BRAND: 'travel' })).toEqual({ brand: 'travel' })
  })

  it('refuses any other value, naming the key', () => {
    for (const value of ['Travel', 'trip', ' travel', 'index,travel']) {
      expect(refusal({ VITE_BRAND: value }).message, value).toContain('VITE_BRAND')
    }
  })

  it('needs none of the network configuration', () => {
    // CI has no .env, and most screen tests render with no environment at all:
    // reading the brand must never pass through `loadConfig`.
    expect(() => loadBrand(travel(OFFER))).not.toThrow()
  })
})

describe('the offer', () => {
  it('decodes a well-formed offer', () => {
    expect(loadBrand(travel(OFFER))).toEqual({ brand: 'travel', offer: OFFER })
  })

  it('decodes UTF-8, not Latin-1', () => {
    const offer = { ...OFFER, city: 'Ciudad Ejémplo', title: 'Ciudad Ejémplo, playa' }
    expect(loadBrand(travel(offer)).offer?.city).toBe('Ciudad Ejémplo')
  })

  it('tolerates line breaks in the base64, as `base64` wraps it', () => {
    for (const eol of ['\n', '\r\n']) {
      const wrapped = encode(OFFER).replace(/(.{76})/g, `$1${eol}`)
      expect(wrapped).toContain(eol)
      expect(loadBrand({ VITE_BRAND: 'travel', VITE_OFFER_B64: wrapped }).offer).toEqual(OFFER)
    }
  })

  it('refuses spaces inside the base64: only line breaks are wrapping', () => {
    const spaced = encode(OFFER).replace(/(.{76})/g, '$1 ')
    expect(spaced).toContain(' ')
    const message = refusal({ VITE_BRAND: 'travel', VITE_OFFER_B64: spaced }).message
    expect(message).toContain('VITE_OFFER_B64')
    expect(message).toMatch(/padded base64/)
  })

  it('ignores keys it does not know', () => {
    const offer = { ...OFFER, cabin: 'economy', fare: { ...OFFER.fare, taxes: '80.00' } }
    expect(loadBrand(travel(offer)).offer).toEqual(OFFER)
  })

  it('keeps the name of the data source when one is given', () => {
    const offer = { ...OFFER, provider: 'Example Data Co' }
    expect(loadBrand(travel(offer)).offer?.provider).toBe('Example Data Co')
  })

  it('treats an empty VITE_OFFER_B64 as no offer', () => {
    expect(loadBrand({ VITE_BRAND: 'travel', VITE_OFFER_B64: '' })).toEqual({ brand: 'travel' })
  })

  it('refuses an offer on the index brand, which never shows one', () => {
    const message = refusal({ VITE_OFFER_B64: encode(OFFER) }).message
    expect(message).toContain('VITE_OFFER_B64')
    expect(message).toContain('VITE_BRAND')
  })

  it('refuses base64 that is not standard and padded', () => {
    // "e30" is "{}", and "eyJ2IjoxfQ" is '{"v":1}', each with its padding dropped.
    expect(encode('{"v":1}')).toBe('eyJ2IjoxfQ==')
    for (const raw of ['not base64!', 'e30', 'eyJ2IjoxfQ', 'eyJ2IjoxfQ=']) {
      const message = refusal({ VITE_BRAND: 'travel', VITE_OFFER_B64: raw }).message
      expect(message, raw).toContain('VITE_OFFER_B64')
      expect(message, raw).toMatch(/padded base64/)
    }
  })

  it('refuses bytes that are not UTF-8, rather than reading them as Latin-1', () => {
    // A well-formed offer whose one accented letter is a Latin-1 byte: only a
    // strict decoder notices.
    const latin1 = Buffer.from(
      JSON.stringify({ ...OFFER, city: 'Ciudad Ej\u00e9mplo' }),
      'latin1',
    ).toString('base64')
    expect(refusal({ VITE_BRAND: 'travel', VITE_OFFER_B64: latin1 }).message).toMatch(/not UTF-8/)
  })

  it('refuses a value that is not JSON, or not a JSON object', () => {
    expect(refusal(travel('{"v":1')).message).toMatch(/not valid JSON/)
    for (const json of ['[1]', 'null', '"offer"', '1']) {
      expect(refusal(travel(json)).message, json).toMatch(/must be a JSON object/)
    }
  })

  const offBy = (changes: Record<string, unknown>) => ({ ...OFFER, ...changes })
  const fareBy = (changes: Record<string, unknown>) =>
    offBy({ fare: { ...OFFER.fare, ...changes } })

  it.each([
    ['v is not 1', offBy({ v: 2 }), 'v'],
    ['v is the string "1"', offBy({ v: '1' }), 'v'],
    ['a string field is missing', offBy({ city: undefined }), 'city'],
    ['a string field is blank', offBy({ title: '  ' }), 'title'],
    ['a string field is not a string', offBy({ origin: 42 }), 'origin'],
    ['a string is longer than 120 characters', offBy({ title: 'x'.repeat(121) }), 'title'],
    ['the source is longer than 120 characters', offBy({ source: 's'.repeat(121) }), 'source'],
    ['a date is not YYYY-MM-DD', offBy({ depart: '12/12/2026' }), 'depart'],
    ['a date does not exist', offBy({ return: '2026-02-30' }), 'return'],
    ['nights is 0', offBy({ nights: 0 }), 'nights'],
    ['nights is 22', offBy({ nights: 22 }), 'nights'],
    ['nights is not whole', offBy({ nights: 7.5 }), 'nights'],
    ['nights is a string', offBy({ nights: '7' }), 'nights'],
    ['travellers is 0', offBy({ travellers: 0 }), 'travellers'],
    ['travellers is not whole', offBy({ travellers: 2.5 }), 'travellers'],
    ['the fare is missing', offBy({ fare: undefined }), 'fare'],
    ['the amount is a number', fareBy({ amount: 612.4 }), 'fare.amount'],
    ['the amount uses a comma', fareBy({ amount: '612,40' }), 'fare.amount'],
    ['the amount is in exponent form', fareBy({ amount: '6e2' }), 'fare.amount'],
    ['the amount is negative', fareBy({ amount: '-1.00' }), 'fare.amount'],
    ['the currency is two letters', fareBy({ currency: 'US' }), 'fare.currency'],
    ['the currency has a digit', fareBy({ currency: 'US1' }), 'fare.currency'],
    ['the basis is too long', fareBy({ basis: 'b'.repeat(121) }), 'fare.basis'],
    ['the provider is empty', offBy({ provider: '' }), 'provider'],
    ['the provider is not a string', offBy({ provider: 1 }), 'provider'],
  ])('refuses an offer where %s, naming the field', (_case, offer, field) => {
    const message = refusal(travel(offer)).message
    expect(message).toContain('VITE_OFFER_B64')
    expect(message).toContain(field)
  })

  it('counts characters, not bytes, against the 120 limit', () => {
    const title = 'é'.repeat(120)
    expect(loadBrand(travel({ ...OFFER, title })).offer?.title).toBe(title)
  })
})

describe('getBrand', () => {
  beforeEach(() => {
    // Nothing from the machine's environment: each test sets what it reads.
    vi.stubEnv('VITE_BRAND', '')
    vi.stubEnv('VITE_OFFER_B64', '')
    resetBrandForTests()
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    resetBrandForTests()
  })

  it('reads the build environment once, and again after a reset', () => {
    vi.stubEnv('VITE_BRAND', 'travel')
    vi.stubEnv('VITE_OFFER_B64', encode(OFFER))
    resetBrandForTests()
    expect(getBrand()).toEqual({ brand: 'travel', offer: OFFER })

    vi.stubEnv('VITE_BRAND', 'index')
    vi.stubEnv('VITE_OFFER_B64', '')
    expect(getBrand().brand).toBe('travel')
    resetBrandForTests()
    expect(getBrand()).toEqual({ brand: 'index' })
  })

  it('does not remember a refusal', () => {
    vi.stubEnv('VITE_BRAND', 'cruise')
    resetBrandForTests()
    expect(() => getBrand()).toThrow(ConfigError)
    vi.stubEnv('VITE_BRAND', 'travel')
    expect(getBrand().brand).toBe('travel')
  })
})
