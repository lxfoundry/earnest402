/**
 * Network parameters, and the only module that reads the environment.
 *
 * Everything here is configuration rather than a constant so that one build
 * can be pointed at LocalNet, TestNet or MainNet. A value hardcoded anywhere
 * else in `src/` is a bug: it would make the bundle network-specific without
 * saying so.
 *
 * Note that these are compiled into the bundle at build time, not read at
 * runtime -- a build made for TestNet cannot be repointed at MainNet by
 * changing an environment variable on the host. It has to be rebuilt.
 */

export class ConfigError extends Error {}

export interface Config {
  network: string
  assetId: bigint
  appId: bigint
  facilitatorUrl: string
  resourceHost: string
  maxFileBytes: number
  /**
   * The chain, read directly.
   *
   * Everything a buyer needs to know after paying -- did the pool fill, was
   * the edition delivered, can I have my money back -- is state on chain, and
   * the paid route answers none of it: it returns a receipt at purchase time
   * and there is no second route. So the client reads algod itself rather
   * than a server reading it on the client's behalf.
   *
   * The indexer is the narrower of the two. Every read of a pool is algod;
   * the indexer answers three questions from the application's history --
   * which pools this wallet joined, for a buyer who arrives without their
   * link; how a closed pool ended, since `close` deletes the boxes that would
   * say; and where a released pool's edition is, since the only record of
   * that is the release transaction's note. A public indexer being
   * unreachable costs those three answers -- recovery degrades to "open your
   * link", a closed pool reads as gone, and a released pool says its link
   * cannot be read right now -- and nothing else.
   */
  algodUrl: string
  indexerUrl: string
  /**
   * The IPFS gateway a released pool's edition link is resolved against: an
   * origin, with no path.
   *
   * Never on chain. The release note records a CID, and which gateway turns
   * that into a link is this page's choice, so a gateway that goes away is a
   * rebuild rather than a record that points nowhere.
   */
  ipfsGateway: string
}

type Env = Record<string, string | undefined>

function required(env: Env, key: string): string {
  // Empty is absent: .env templates ship optional keys present-and-empty, so
  // `env[key] ?? fallback` would find the key and hand back "".
  const value = env[key]
  if (!value) throw new ConfigError(`${key} is required and was not set`)
  return value
}

function requiredBigInt(env: Env, key: string): bigint {
  const raw = required(env, key)
  if (!/^\d+$/.test(raw)) {
    throw new ConfigError(`${key} must be a whole number, got ${JSON.stringify(raw)}`)
  }
  return BigInt(raw)
}

function requiredUrl(env: Env, key: string): string {
  const raw = required(env, key)
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    throw new ConfigError(`${key} must be a URL, got ${JSON.stringify(raw)}`)
  }
  const isLoopback = url.hostname === 'localhost' || url.hostname === '127.0.0.1'
  if (url.protocol !== 'https:' && !isLoopback) {
    throw new ConfigError(`${key} must be https, got ${JSON.stringify(raw)}`)
  }
  // Trailing slashes make every joined path ambiguous later.
  return raw.replace(/\/+$/, '')
}

/**
 * A URL that is an origin and nothing more.
 *
 * For a value this page appends a path of its own to. The gateway link is
 * `<gateway>/ipfs/<cid>/...`, and a gateway copied from a resolved link, with
 * `/ipfs` still on the end, would build `/ipfs/ipfs/<cid>`: a link that
 * resolves nowhere, shown to a buyer as the thing they paid for.
 *
 * Credentials are refused rather than dropped. `origin` has no userinfo, so a
 * gateway that needs them would build links that fail to authenticate -- and
 * anything accepted here is compiled into a bundle anyone can download. Checked
 * first, and refused without repeating the value: every other refusal quotes
 * what it was given, and the message is shown on the page.
 */
function requiredOrigin(env: Env, key: string): string {
  const raw = required(env, key)
  if (URL.canParse(raw)) {
    const { username, password } = new URL(raw)
    if (username !== '' || password !== '') {
      throw new ConfigError(
        `${key} must not carry credentials (user:password@). The value is not ` +
          'repeated here, since this message is shown on the page.',
      )
    }
  }
  const url = new URL(requiredUrl(env, key))
  if (url.pathname !== '/' || url.search !== '' || url.hash !== '') {
    throw new ConfigError(
      `${key} must be an origin, with no path, query or fragment -- got ` +
        `${JSON.stringify(env[key])}. This page adds /ipfs/<cid> itself.`,
    )
  }
  return url.origin
}

/**
 * A CAIP-2 Algorand identifier: "algorand:" plus the base64 genesis hash.
 *
 * Shape-checked because nothing downstream can tell a typo from a private
 * chain. The wallet layer maps an unrecognised identifier to LocalNet -- it
 * has to, since a LocalNet reset mints a fresh genesis hash -- so a mistyped
 * MainNet value would silently connect a production build to a local node
 * instead of failing. It is also the value `requestQuote` matches the 402's
 * `network` against, where a wrong one simply finds nothing payable.
 */
function requiredCaip2(env: Env, key: string): string {
  const raw = required(env, key)
  if (!/^algorand:[A-Za-z0-9+/]{43}=$/.test(raw)) {
    throw new ConfigError(
      `${key} must be a CAIP-2 Algorand identifier -- "algorand:" plus the ` +
        `base64 genesis hash -- got ${JSON.stringify(raw)}. Note this is not ` +
        'the transaction genesisID ("testnet-v1.0"); the two are easy to ' +
        'confuse and only one of them matches a 402.',
    )
  }
  return raw
}

// CAIP-2 identifiers of the two public networks. The wallet layer maps them
// onto its library's names; a travel build is refused on every network but
// TestNet.
export const MAINNET_CAIP2 = 'algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8='
export const TESTNET_CAIP2 = 'algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI='

export function loadConfig(env: Env): Config {
  const maxFileBytes = Number(required(env, 'VITE_MAX_FILE_BYTES'))
  if (!Number.isSafeInteger(maxFileBytes) || maxFileBytes <= 0) {
    throw new ConfigError('VITE_MAX_FILE_BYTES must be a positive whole number')
  }
  return {
    network: requiredCaip2(env, 'VITE_NETWORK'),
    assetId: requiredBigInt(env, 'VITE_ASSET_ID'),
    appId: requiredBigInt(env, 'VITE_APP_ID'),
    facilitatorUrl: requiredUrl(env, 'VITE_FACILITATOR_URL'),
    resourceHost: requiredUrl(env, 'VITE_RESOURCE_HOST'),
    maxFileBytes,
    algodUrl: requiredUrl(env, 'VITE_ALGOD_URL'),
    indexerUrl: requiredUrl(env, 'VITE_INDEXER_URL'),
    ipfsGateway: requiredOrigin(env, 'VITE_IPFS_GATEWAY'),
  }
}

let cached: Config | null = null

/**
 * The process-wide configuration, validated on first use and memoised.
 *
 * Deliberately a function rather than a `const` evaluated at module load. A
 * module-level singleton runs its validation on *import*, so every test that
 * imports anything transitively reaching this file would need a populated
 * environment -- including this module's own test, which would not be able to
 * load far enough to call `loadConfig` at all. Validation still happens once,
 * and still fails loudly; it just happens on first use rather than on import.
 */
export function getConfig(): Config {
  cached ??= loadConfig(import.meta.env as unknown as Env)
  return cached
}

/** Test seam: drop the memoised value so a later call revalidates. */
export function resetConfigForTests(): void {
  cached = null
}

/**
 * Which product line this build presents: the words, the look and the kind of
 * file a released pool delivers.
 *
 * Kept out of `Config` on purpose. `getConfig()` refuses to run without the
 * whole network configuration, and most screen tests -- CI's among them, with
 * no `.env` -- render without one. A brand read through it would turn every
 * one of those renders into a configuration error. The brand needs nothing
 * from the network, so it is read on its own and defaults to the index.
 */
export type BrandName = 'index' | 'travel'

export interface Brand {
  brand: BrandName
  /** The sample trip a travel build is showing. Never set on the index. */
  offer?: Offer
}

/**
 * The sample trip a travel build shows: passed in as `VITE_OFFER_B64`, the
 * base64 of UTF-8 JSON. Its shape and limits are `docs/specs/pool-client.md`
 * §11.
 *
 * Display text only. Nothing here reaches the chain or the paid route: the
 * seat price, the seat count and the deadline a buyer pays against are the
 * quote's and the agreement's, never the offer's.
 */
export interface Offer {
  v: 1
  title: string
  city: string
  iata: string
  origin: string
  originIata: string
  /** `YYYY-MM-DD`. */
  depart: string
  /** `YYYY-MM-DD`. */
  return: string
  /** 1 to 21. */
  nights: number
  travellers: number
  fare: {
    /** A decimal string, e.g. "612.40": never a float. */
    amount: string
    /** Three letters, e.g. "USD". */
    currency: string
    basis: string
  }
  /** Where the fare came from, and how far to trust it, in one sentence. */
  source: string
  fetchedAt: string
  /**
   * The data source's name, when the page is to credit it by name.
   *
   * Optional, and the only way a name reaches the page: this repository names
   * no data source of its own. Without it the fare's caption reads "Fare data:
   * see the demo notice", and the demo notice drops the sentence that names
   * the provider.
   */
  provider?: string
}

const OFFER_KEY = 'VITE_OFFER_B64'

/** The limit on every string in the offer, counted in characters. */
const MAX_OFFER_TEXT = 120

function offerError(problem: string): ConfigError {
  return new ConfigError(
    `${OFFER_KEY} must be the base64 of a UTF-8 JSON offer, and ${problem}.`,
  )
}

function decodeOfferText(raw: string): string {
  // Line breaks are the `base64` tool's wrapping, not part of the value. Any
  // other whitespace is not standard base64, and is refused below.
  const compact = raw.replace(/[\r\n]+/g, '')
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(compact)) {
    throw offerError('this is not standard, padded base64')
  }
  const bytes = Uint8Array.from(atob(compact), (char) => char.charCodeAt(0))
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    throw offerError('these bytes are not UTF-8')
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function offerText(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw offerError(`${field} must be a non-empty string`)
  }
  if ([...value].length > MAX_OFFER_TEXT) {
    throw offerError(`${field} must be at most ${MAX_OFFER_TEXT} characters`)
  }
  return value
}

/** A calendar date as `YYYY-MM-DD`, and one that exists. */
function offerDate(value: unknown, field: string): string {
  const text = offerText(value, field)
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text)
  const [year, month, day] = match ? match.slice(1).map(Number) : []
  const date = match ? new Date(Date.UTC(year!, month! - 1, day!)) : null
  if (
    date === null ||
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month! - 1 ||
    date.getUTCDate() !== day
  ) {
    throw offerError(`${field} must be a date written YYYY-MM-DD, got ${JSON.stringify(text)}`)
  }
  return text
}

function offerWhole(value: unknown, field: string, min: number, max: number): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) {
    const range = Number.isFinite(max) ? `from ${min} to ${max}` : `of at least ${min}`
    throw offerError(`${field} must be a whole number ${range}, got ${JSON.stringify(value)}`)
  }
  return value
}

/**
 * The offer, checked against its shape, or a `ConfigError` naming the first
 * field that is off.
 *
 * Unknown keys are dropped rather than refused, so whatever produces the offer
 * can add a field before this page shows it. The value itself is never repeated whole:
 * a refusal is shown on the page.
 */
export function parseOffer(raw: string): Offer {
  let parsed: unknown
  try {
    parsed = JSON.parse(decodeOfferText(raw))
  } catch (error) {
    if (error instanceof ConfigError) throw error
    throw offerError('this is not valid JSON')
  }
  if (!isRecord(parsed)) throw offerError('this must be a JSON object')
  if (parsed.v !== 1) throw offerError(`v must be the number 1, got ${JSON.stringify(parsed.v)}`)

  const fare = parsed.fare
  if (!isRecord(fare)) throw offerError('fare must be an object')
  const amount = offerText(fare.amount, 'fare.amount')
  if (!/^\d+(?:\.\d+)?$/.test(amount)) {
    throw offerError(
      `fare.amount must be a decimal string like "612.40", got ${JSON.stringify(amount)}`,
    )
  }
  const currency = offerText(fare.currency, 'fare.currency')
  if (!/^[A-Za-z]{3}$/.test(currency)) {
    throw offerError(`fare.currency must be three letters, got ${JSON.stringify(currency)}`)
  }

  const offer: Offer = {
    v: 1,
    title: offerText(parsed.title, 'title'),
    city: offerText(parsed.city, 'city'),
    iata: offerText(parsed.iata, 'iata'),
    origin: offerText(parsed.origin, 'origin'),
    originIata: offerText(parsed.originIata, 'originIata'),
    depart: offerDate(parsed.depart, 'depart'),
    return: offerDate(parsed.return, 'return'),
    nights: offerWhole(parsed.nights, 'nights', 1, 21),
    travellers: offerWhole(parsed.travellers, 'travellers', 1, Number.POSITIVE_INFINITY),
    fare: { amount, currency, basis: offerText(fare.basis, 'fare.basis') },
    source: offerText(parsed.source, 'source'),
    fetchedAt: offerText(parsed.fetchedAt, 'fetchedAt'),
  }
  if (parsed.provider !== undefined) offer.provider = offerText(parsed.provider, 'provider')
  return offer
}

export function loadBrand(env: Env): Brand {
  const name = env.VITE_BRAND || 'index'
  if (name !== 'index' && name !== 'travel') {
    throw new ConfigError(
      `VITE_BRAND must be "index" or "travel", or unset for the index, got ${JSON.stringify(name)}`,
    )
  }
  const raw = env[OFFER_KEY]
  if (!raw) return { brand: name }
  if (name === 'index') {
    throw new ConfigError(
      `${OFFER_KEY} is set, but VITE_BRAND is the index, which never shows an offer. ` +
        'Set VITE_BRAND=travel, or leave the offer empty.',
    )
  }
  return { brand: name, offer: parseOffer(raw) }
}

let cachedBrand: Brand | null = null

/**
 * The build's brand, validated on first use and memoised, for the reasons
 * `getConfig` gives. A refusal is not memoised: it is thrown again, from the
 * environment as it is then.
 */
export function getBrand(): Brand {
  cachedBrand ??= loadBrand(import.meta.env as unknown as Env)
  return cachedBrand
}

/**
 * Refuses a travel build on any network but TestNet.
 *
 * The travel line is a demo, and says so on every page: its badge, its demo
 * notice and its funding steps all tell a buyer the seat is paid in test USDC
 * on TestNet. Built for MainNet, that page would say so while real USDC moved.
 * Checked where the brand and the network are first both at hand, since
 * `getBrand` reads no network on purpose.
 */
export function checkBrandNetwork(brand: Brand, config: Config): void {
  if (brand.brand === 'travel' && config.network !== TESTNET_CAIP2) {
    throw new ConfigError(
      `VITE_BRAND is travel, which runs on TestNet only, but VITE_NETWORK is ` +
        `${JSON.stringify(config.network)}. Build the travel line for TestNet, or unset ` +
        'VITE_BRAND for the index.',
    )
  }
}

/** Test seam: drop the memoised brand so a later call reads it again. */
export function resetBrandForTests(): void {
  cachedBrand = null
}
