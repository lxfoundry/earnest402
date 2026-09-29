import algosdk from 'algosdk'
import { getConfig } from '../config'
import { CALL_BYTES_CEILING } from './calls'
import {
  type Agreement,
  type Seat,
  agreementBoxName,
  decodeAgreement,
  decodeRoster,
  rosterBoxName,
} from './decode'
import { SELECTORS } from './selectors'

/**
 * Reading the chain, and the one distinction everything here turns on.
 *
 * `close` deletes both boxes, so **a missing box is an answer** — the
 * agreement is closed, or never existed. Every other failure is the node not
 * answering, which is not an answer at all. Collapsing the two would report a
 * pool that is merely unreachable as one that is finished, and a buyer owed
 * money would be told there is nothing to claim.
 *
 * The clients are passed in rather than imported, so these can be tested
 * without an environment.
 */

/** algod did not answer. Distinct from "there is no such box". */
export class ChainUnreachableError extends Error {}

/** The configured client speaks for a different chain than the bundle does. */
export class NetworkMismatchError extends Error {}

export function createAlgod(): algosdk.Algodv2 {
  return new algosdk.Algodv2('', getConfig().algodUrl, '')
}

export function createIndexer(): algosdk.Indexer {
  return new algosdk.Indexer('', getConfig().indexerUrl, '')
}

function isNotFound(error: unknown): boolean {
  return (error as { status?: number } | null)?.status === 404
}

function unreachable(what: string, error: unknown): ChainUnreachableError {
  const detail = error instanceof Error ? error.message : String(error)
  return new ChainUnreachableError(`could not read ${what}: ${detail}`)
}

/** One box's bytes and the round algod read them at, or `null` when it is gone. */
async function readBox(
  algod: algosdk.Algodv2,
  appId: bigint,
  name: Uint8Array,
  what: string,
): Promise<{ value: Uint8Array; round: bigint } | null> {
  await ensureNetwork(algod)
  try {
    const box = await algod.getApplicationBoxByName(appId, name).do()
    return { value: box.value, round: box.round }
  } catch (error) {
    if (isNotFound(error)) return null
    throw unreachable(what, error)
  }
}

/**
 * The agreement, or `null` when its box is gone.
 *
 * `null` means closed or never created — both permanent, and neither an
 * error. A caller that needs to tell those two apart reads the application's
 * transaction history; the box cannot say.
 */
export async function readAgreement(
  algod: algosdk.Algodv2,
  appId: bigint,
  agreementId: bigint,
): Promise<Agreement | null> {
  const box = await readBox(algod, appId, agreementBoxName(agreementId), `agreement ${agreementId}`)
  return box === null ? null : decodeAgreement(box.value)
}

/**
 * The roster, or `[]` when its box is gone.
 *
 * `[]` carries the same discipline: it is an answer from the node, never a
 * read that failed. A refund path that treated an unreachable node as an
 * empty roster would decide nobody is owed anything.
 */
export async function readRoster(
  algod: algosdk.Algodv2,
  appId: bigint,
  agreementId: bigint,
): Promise<Seat[]> {
  const box = await readBox(
    algod,
    appId,
    rosterBoxName(agreementId),
    `the roster for agreement ${agreementId}`,
  )
  return box === null ? [] : decodeRoster(box.value)
}

/** Both of a pool's boxes, and how recent the pair of them is. */
export interface PoolBoxes {
  record: Agreement | null
  roster: Seat[]
  /**
   * The older of the two rounds the boxes were read at, or `null` when
   * neither box exists.
   *
   * The older, because the pair is only as current as its staler half. The
   * two boxes are two requests, and a public node is usually several nodes
   * behind one address, which need not agree on the latest round -- so a
   * record read at one round can arrive beside a roster read a round
   * earlier.
   */
  round: bigint | null
}

/**
 * The agreement and its roster, read at the same time.
 *
 * Concurrently rather than one after the other: they are independent
 * requests, and this is on a refresh interval against a public node.
 */
export async function readPoolBoxes(
  algod: algosdk.Algodv2,
  appId: bigint,
  agreementId: bigint,
): Promise<PoolBoxes> {
  const [agreement, roster] = await Promise.all([
    readBox(algod, appId, agreementBoxName(agreementId), `agreement ${agreementId}`),
    readBox(algod, appId, rosterBoxName(agreementId), `the roster for agreement ${agreementId}`),
  ])
  const rounds = [agreement?.round, roster?.round].filter(
    (round): round is bigint => round !== undefined,
  )
  return {
    record: agreement === null ? null : decodeAgreement(agreement.value),
    roster: roster === null ? [] : decodeRoster(roster.value),
    round: rounds.length === 0 ? null : rounds.reduce((a, b) => (a < b ? a : b)),
  }
}

/**
 * Whether `address` could pay one application call's fee right now.
 *
 * Holding USDC is not the same as being able to send a transaction. Opting
 * into the asset alone locks a minimum balance that is held, not spendable,
 * and a buyer needs no ALGO at all to *buy* a seat -- the settlement group
 * loads the whole pooled fee onto the facilitator's leg (`buildJoinGroup.ts`).
 * So a wallet can arrive here holding real money and still be unable to sign
 * anything that spends it.
 *
 * That is not the same as being stuck, and this function's result must never
 * be read as if it were. `refund_next` and `claim_refund` carry no sender
 * check and pay the *roster address* via an inner transaction, never
 * whoever signs the outer call -- so anyone can cover this fee on the
 * buyer's behalf and the refund still lands with the buyer. The property
 * this protects is not "you must be able to rescue yourself", it is "your
 * money is recoverable by anyone, and it is always paid to you." A caller
 * deciding what to show when this returns `false` should offer to wait, or
 * point at someone else who can send the call -- never tell the buyer their
 * refund is unreachable.
 *
 * `minBalance` is what `accountInformation` calls the account's own locked
 * floor (asset opt-ins, app opt-ins, and so on); spendable is the amount
 * above it. The fee is read from live suggested params rather than a
 * constant, for the same reason `buildJoinGroup.ts` never hardcodes one: the
 * network charges what it charges, and a stale constant would answer a
 * question about today with yesterday's minimum.
 *
 * And it is the fee the largest call would be charged, not the minimum. Under
 * congestion the node prices per byte, and a check against the minimum would
 * offer a call the wallet then cannot pay for; see `CALL_BYTES_CEILING`.
 */
export async function canPayFee(algod: algosdk.Algodv2, address: string): Promise<boolean> {
  await ensureNetwork(algod)
  // Concurrently: neither answer depends on the other.
  const [account, params] = await Promise.all([
    algod
      .accountInformation(address)
      .exclude('all')
      .do()
      .catch((error: unknown) => {
        throw unreachable(`${address}'s account`, error)
      }),
    suggestedParams(algod),
  ])
  const minFee = BigInt(params.minFee ?? 0)
  const rate = BigInt(params.fee ?? 0)
  const priced = params.flatFee ? rate : rate * BigInt(CALL_BYTES_CEILING)
  const fee = priced > minFee ? priced : minFee
  const spendable = account.amount - account.minBalance
  return spendable >= fee
}

/**
 * The time the contract will compare a deadline against.
 *
 * Not `Date.now()`. The contract reads `Global.latest_timestamp`, which is the
 * *previous* block's timestamp, so wall clock runs ahead of it. A countdown
 * driven by the browser's clock reaches zero first and offers an `expire` the
 * chain refuses. The refusal costs no fee, since a refused call is never
 * committed, but the buyer has signed a wallet prompt for nothing and is shown
 * an error they cannot act on.
 */
export async function chainTimestamp(algod: algosdk.Algodv2): Promise<bigint> {
  await ensureNetwork(algod)
  try {
    const status = await algod.status().do()
    // `headerOnly` because the payset is the rest of the block and this wants
    // one integer out of the header. Without it every poll downloads and
    // decodes a whole block of transactions, in the browser, against a public
    // node -- and this is on a refresh interval for as long as a pool is open.
    const block = await algod.block(status.lastRound).headerOnly(true).do()
    return block.block.header.timestamp
  } catch (error) {
    throw unreachable('the chain timestamp', error)
  }
}

/**
 * Refuse a bundle pointed at the wrong chain.
 *
 * The suggested params are fetched before every purchase anyway and carry the
 * genesis hash, so this costs no extra round trip. It catches the one
 * configuration mistake nothing else would: a build compiled for one network
 * whose algod URL names another. Without it the client would read boxes that
 * do not exist, or — far worse — boxes belonging to a different deployment
 * with the same application id.
 */
export function assertNetwork(
  params: algosdk.SuggestedParams,
  network: string = getConfig().network,
): void {
  const expected = network.slice(network.indexOf(':') + 1)
  const actual = params.genesisHash ? algosdk.bytesToBase64(params.genesisHash) : ''
  if (actual !== expected) {
    throw new NetworkMismatchError(
      `this client is built for ${network}, but the configured node reports ` +
        `genesis hash ${actual || '(none)'}. Refusing to read or sign against ` +
        'a chain the bundle was not built for.',
    )
  }
}

/** Suggested params, with the network they came from checked. */
export async function suggestedParams(
  algod: algosdk.Algodv2,
): Promise<algosdk.SuggestedParams> {
  let params
  try {
    params = await algod.getTransactionParams().do()
  } catch (error) {
    throw unreachable('the suggested parameters', error)
  }
  assertNetwork(params)
  return params
}

/**
 * The same check, run once per client, before the first read that trusts it.
 *
 * The purchase path gets this for free -- it fetches suggested params anyway --
 * but a buyer returning to a pool link never purchases, and the read path is
 * where a wrong chain does its quiet damage: the same application id resolves
 * against a different deployment's boxes, and a stranger's pool renders as
 * this buyer's. So the reads below pay for one params fetch per client, and
 * nothing after it.
 */
const CHECKED = new WeakMap<algosdk.Algodv2, Promise<void>>()

export async function ensureNetwork(algod: algosdk.Algodv2): Promise<void> {
  const already = CHECKED.get(algod)
  if (already) return already
  const check = suggestedParams(algod).then(() => undefined)
  CHECKED.set(algod, check)
  try {
    await check
  } catch (error) {
    // A node that did not answer is not a node on the wrong chain. Forget an
    // unreachable check so the next read retries it; a mismatch is a property
    // of the build and stays remembered.
    if (error instanceof ChainUnreachableError) CHECKED.delete(algod)
    throw error
  }
}

/** Test seam: drop the memoised checks so a later read re-runs them. */
export function resetNetworkChecksForTests(algod: algosdk.Algodv2): void {
  CHECKED.delete(algod)
}

/** One application call, reduced to the things the callers here want. */
interface AppCall {
  selector: string
  agreementId: bigint
  /** Empty when the transaction carries none. */
  note: Uint8Array
}

/**
 * How many pages of history to walk. Twenty pages is far more than any
 * agreement generates, and the bound is what stops a server that keeps
 * handing back a token from spinning the browser forever.
 */
const MAX_PAGES = 20

function decodeAppCall(
  txn: algosdk.indexerModels.Transaction,
  appId: bigint,
): AppCall | null {
  const call = txn.applicationTransaction
  if (!call || call.applicationId !== appId) return null
  const args = call.applicationArgs ?? []
  const selector = args[0]
  const agreementId = args[1]
  if (!selector || !agreementId) return null
  if (agreementId.length !== 8) return null
  return {
    selector: algosdk.bytesToHex(selector),
    agreementId: algosdk.decodeUint64(agreementId, 'bigint'),
    note: txn.note ?? new Uint8Array(),
  }
}

/**
 * Every application call the indexer will show us, across pages.
 *
 * ⚠️ `searchForTransactions` and not `lookupAccountTransactions`. They look
 * interchangeable and the second is the more convenient shape, but it maps to
 * `/v2/accounts/{addr}/transactions`, which **silently ignores its
 * `application-id` filter** — measured against both public providers: a
 * nonexistent application id returns the account's entire history. It appears
 * to work, because the call being looked for is in that history among
 * unrelated transactions. It breaks the first time a wallet has touched a
 * second escrow application, whose seats would then be shown as this one's.
 *
 * `searchForTransactions` maps to `/v2/transactions`, which honours the
 * filter. The application id is checked again in `decodeAppCall` regardless: a
 * query parameter is a convenience, never the guarantee.
 *
 * Paged, because both callers ask a question whose answer is wrong rather than
 * incomplete when a page is missed — a seat that goes unlisted reads as a seat
 * the buyer never had, and a closed pool's outcome reads as unknown.
 *
 * Application calls only, asked of the indexer rather than filtered here: the
 * application's history is every pool it has ever run, and a page spent on
 * anything else is a page nearer `MAX_PAGES`. `enough`, when given, stops the
 * walk at the page holding the first call that answers the caller's question,
 * rather than reading the history to its end.
 */
async function readAppCalls(
  indexer: algosdk.Indexer,
  appId: bigint,
  what: string,
  narrow: (
    search: ReturnType<algosdk.Indexer['searchForTransactions']>,
  ) => ReturnType<algosdk.Indexer['searchForTransactions']>,
  enough: (call: AppCall) => boolean = () => false,
): Promise<AppCall[]> {
  const calls: AppCall[] = []
  let nextToken: string | undefined
  for (let page = 0; page < MAX_PAGES; page += 1) {
    let response
    try {
      let search = narrow(
        indexer.searchForTransactions().applicationID(appId).txType('appl'),
      )
      if (nextToken !== undefined) search = search.nextToken(nextToken)
      response = await search.do()
    } catch (error) {
      throw unreachable(what, error)
    }
    let answered = false
    for (const txn of response.transactions ?? []) {
      const call = decodeAppCall(txn, appId)
      if (!call) continue
      calls.push(call)
      if (enough(call)) answered = true
    }
    if (answered) break
    const token = response.nextToken
    if (!token || token === nextToken) break
    nextToken = token
  }
  return calls
}

/**
 * Which agreements this wallet has joined — the fallback for a buyer who
 * arrives without their link.
 */
export async function findJoinedAgreements(
  indexer: algosdk.Indexer,
  appId: bigint,
  address: string,
): Promise<bigint[]> {
  const calls = await readAppCalls(
    indexer,
    appId,
    `this wallet's history`,
    (search) => search.address(address),
  )
  const found: bigint[] = []
  for (const call of calls) {
    if (call.selector !== SELECTORS.join) continue
    if (!found.includes(call.agreementId)) found.push(call.agreementId)
  }
  return found
}

/** What became of a pool whose boxes are gone. */
export type ClosedOutcome = 'released' | 'refunded' | 'unknown'

/**
 * Which of the two ways a closed pool ended.
 *
 * `close` deletes both boxes, so a delivered pool and a refunded one read
 * identically off algod: absent. That must not render as an error, and it must
 * not render as the same thing — one says the edition was published and the
 * other says the money went back. The application's own history separates
 * them, and needs nothing that is not already on chain.
 *
 * Only meaningful for an agreement whose box is gone. That is what makes
 * `expire` sufficient evidence of the refund path: closing is legal from
 * `REFUNDED` alone, so a vanished box that was ever expired was refunded
 * through to the end of its cursor pass.
 *
 * `unknown` is an honest answer, not a failure — an indexer that has not
 * caught up, or a history beyond `MAX_PAGES`. An unreachable indexer throws
 * instead, because "I could not ask" is not "I asked and nothing came back".
 */
export async function readClosedOutcome(
  indexer: algosdk.Indexer,
  appId: bigint,
  agreementId: bigint,
): Promise<ClosedOutcome> {
  const isRelease = (call: AppCall) =>
    call.agreementId === agreementId &&
    (call.selector === SELECTORS.releaseHash || call.selector === SELECTORS.releaseQuorum)
  const isRefund = (call: AppCall) =>
    call.agreementId === agreementId &&
    (call.selector === SELECTORS.refundNext || call.selector === SELECTORS.expire)

  // Stops at the first page that says either. The two are mutually exclusive
  // on one agreement, so the first one found is the answer wherever in the
  // history it sits, and the pages after it -- every pool that ran later --
  // have nothing to add.
  const calls = await readAppCalls(
    indexer,
    appId,
    `agreement ${agreementId}'s history`,
    (search) => search,
    (call) => isRelease(call) || isRefund(call),
  )
  const released = calls.some(isRelease)
  const refunded = calls.some(isRefund)
  // A release and a refund are mutually exclusive on one agreement -- release
  // leaves RELEASED, refunds only start from EXPIRED -- so this ordering is a
  // tie-break that should never be needed. It favours the delivered reading,
  // which is the one that does not tell a paid buyer to expect money back.
  if (released) return 'released'
  return refunded ? 'refunded' : 'unknown'
}

/** Where a released pool's edition is: what the release note recorded. */
export interface EditionLink {
  /** The edition number -- a trip's, on the travel line -- decimal, as the note spells it. */
  edition: string
  /** A locator for the bundle, never a commitment: the sha256 is that. */
  cid: string
}

/**
 * Which product's notes to read: the index's editions, or the travel line's
 * trips. Each writes its own prefix, so one kind's reader never takes the
 * other's release for its own.
 */
export type ProductKind = 'index' | 'travel'

/**
 * Per kind, the prefix every note starts with, the creating call's included,
 * and the release note in full: number, agreement id, CID.
 *
 * The prefix is bytes, not text, because of how algosdk takes it: `notePrefix`
 * treats a string as base64 already and sends it unchanged, so passing this
 * as text would search for a prefix nobody ever wrote and find nothing,
 * quietly.
 *
 * Written out rather than built from the kind's name, so each pattern can be
 * read, and searched for, exactly as the release tooling writes it.
 */
const NOTES: Record<ProductKind, { prefix: Uint8Array; release: RegExp }> = {
  index: {
    prefix: new TextEncoder().encode('earnest:index:edition-'),
    release: /^earnest:index:edition-([1-9][0-9]*):agreement:(0|[1-9][0-9]*):cid:([A-Za-z0-9]+)$/,
  },
  travel: {
    prefix: new TextEncoder().encode('earnest:travel:trip-'),
    release: /^earnest:travel:trip-([1-9][0-9]*):agreement:(0|[1-9][0-9]*):cid:([A-Za-z0-9]+)$/,
  },
}

/**
 * The edition link `call` carries for `agreementId`, or `null`.
 *
 * Three checks, and each one is load-bearing. The note prefix is not
 * reserved: anyone can send a call whose note starts with it, and the
 * contract accepts plenty of calls from anyone. What nobody but the
 * agreement's verifier can commit is `release_hash`, and only once -- so the
 * selector and the agreement argument are what make a note the release, and
 * the note naming the same agreement is what makes it this pool's.
 */
function editionLinkOf(
  call: AppCall,
  agreementId: bigint,
  kind: ProductKind,
): EditionLink | null {
  if (call.selector !== SELECTORS.releaseHash) return null
  if (call.agreementId !== agreementId) return null
  let text: string
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(call.note)
  } catch {
    return null
  }
  const match = NOTES[kind].release.exec(text)
  if (!match || match[2] !== String(agreementId)) return null
  return { edition: match[1]!, cid: match[3]! }
}

/**
 * Where a released pool's edition is, from the note on its release.
 *
 * The note on the `release_hash` call is the link's only on-chain record, and
 * history rather than state, so this works as well after `close` as before
 * it. `null` is an answer -- the history holds no release note for this pool,
 * or not yet, since an indexer trails the node -- and an unreachable indexer
 * throws instead, for the reason `readClosedOutcome` gives.
 *
 * What comes back is a locator, never a claim about the bytes behind it. The
 * CID was recorded after the operator fetched the bundle through gateways and
 * hashed it; a buyer checks what they fetch the same way, against the sha256
 * the pool committed to, and never by comparing CIDs.
 */
export async function readEditionLink(
  indexer: algosdk.Indexer,
  appId: bigint,
  agreementId: bigint,
  kind: ProductKind = 'index',
): Promise<EditionLink | null> {
  const calls = await readAppCalls(
    indexer,
    appId,
    `agreement ${agreementId}'s release note`,
    (search) => search.notePrefix(NOTES[kind].prefix),
    (call) => editionLinkOf(call, agreementId, kind) !== null,
  )
  for (const call of calls) {
    const link = editionLinkOf(call, agreementId, kind)
    if (link) return link
  }
  return null
}
