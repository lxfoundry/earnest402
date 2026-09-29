import algosdk from 'algosdk'
import { getConfig } from './config'
import { JOB_STATES, type HashResult, type JobState } from './machine'
import type { PaymentPayload, Quote402 } from './payment/schemeClient'

/**
 * The HTTP calls, for both paid routes.
 *
 * Two details are easy to get wrong and fail only against a real server, so
 * they are stated here rather than discovered again:
 *
 *   - under x402 V2 the 402's requirements ride in the `PAYMENT-REQUIRED`
 *     *header*. The body is `{}`. A client that reads `accepts` out of the
 *     body concludes the server offered nothing it can pay.
 *   - `PAYMENT-SIGNATURE` carries a whole payment payload -- the inner
 *     `{paymentGroup, paymentIndex}` wrapped with the accepted requirements,
 *     the version and the resource. Sending the inner object alone fails to
 *     decode at the facilitator.
 *
 * Same origin as the paid route in production, so no CORS and no base URL
 * beyond the configured host.
 */

const PAYMENT_REQUIRED_HEADER = 'PAYMENT-REQUIRED'
const PAYMENT_SIGNATURE_HEADER = 'PAYMENT-SIGNATURE'
const PAYMENT_RESPONSE_HEADER = 'PAYMENT-RESPONSE'

export class ApiError extends Error {}

/**
 * Built in exactly one place.
 *
 * The server tells the unpaid pass from the paid retry by the payment header
 * and looks the agreement up on the second, so the two passes must send a
 * byte-identical query string -- and two call sites building it separately is
 * precisely how they stop being identical.
 */
function pinUrl(sha256: string, size: number): string {
  return `${getConfig().resourceHost}/pin?sha256=${sha256}&size=${size}`
}

/** The pooled route takes no query: the seat it sells is whichever pool is open. */
function indexUrl(): string {
  return `${getConfig().resourceHost}/index`
}

/**
 * Base64 of UTF-8, both ways, via algosdk rather than `atob`/`btoa`.
 *
 * `btoa` throws on any code point above U+00FF, and the envelope echoed back
 * in the payment payload carries the route's own description. A client that
 * settles fine in testing and throws on a renamed route is the failure this
 * avoids.
 */
function decodeBase64Json(value: string, header: string): Record<string, unknown> {
  let text: string
  try {
    text = new TextDecoder().decode(algosdk.base64ToBytes(value))
  } catch {
    throw new ApiError(`the ${header} header is not valid base64`)
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new ApiError(`the ${header} header is not JSON`)
  }
  // `as Record<string, unknown>` would let a bare string or number through as
  // an envelope, and every field read off it would be undefined -- which reads
  // downstream as a well-formed 402 that happens to offer nothing payable.
  // A malformed header should say it is malformed.
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new ApiError(`the ${header} header is not a JSON object`)
  }
  return parsed as Record<string, unknown>
}

function encodeBase64Json(value: unknown): string {
  return algosdk.bytesToBase64(new TextEncoder().encode(JSON.stringify(value)))
}

async function readJson(response: Response): Promise<Record<string, unknown>> {
  try {
    return (await response.json()) as Record<string, unknown>
  } catch {
    throw new ApiError(`${response.status} response was not JSON`)
  }
}

/**
 * The job state, checked rather than asserted.
 *
 * `body.state as JobState` would let any string the server invents -- a
 * renamed lifecycle state, a typo -- through as a `JobState`, and the reducer
 * would then price it. The expensive direction is a state that reads as
 * "nothing is open here": it re-quotes, and a re-quote parks a second deposit
 * against bytes already paid for. So an unrecognised state is a failed
 * response, which the caller already has to handle, rather than a value.
 */
function parseJobState(raw: unknown): JobState {
  if (!JOB_STATES.includes(raw as JobState)) {
    throw new ApiError(`the server reported an unknown job state ${JSON.stringify(raw)}`)
  }
  return raw as JobState
}

/**
 * One unpaid pass, kept whole.
 *
 * `accepted` and `envelope` are the raw decoded objects, never re-serialised
 * from a parsed struct. The facilitator compares the requirements it is sent
 * against the ones it issued, so anything this client re-spells -- a dropped
 * optional field, a number that became a string -- is a settlement that fails
 * for a reason no log points at. Echoing the object back untouched is the
 * only way to be sure they match.
 */
export interface QuotedRoute {
  quote: Quote402
  accepted: Record<string, unknown>
  envelope: Record<string, unknown>
}

function parseQuote(response: Response): QuotedRoute {
  const config = getConfig()
  const header = response.headers.get(PAYMENT_REQUIRED_HEADER)
  if (!header) {
    throw new ApiError(
      `the 402 carried no ${PAYMENT_REQUIRED_HEADER} header. Under x402 V2 the ` +
        'requirements are in the header and the body is {}.',
    )
  }
  const envelope = decodeBase64Json(header, PAYMENT_REQUIRED_HEADER)
  const accepts = (envelope.accepts ?? []) as Record<string, unknown>[]
  const accepted = accepts.find(
    (entry) =>
      entry.scheme === 'exact' &&
      entry.network === config.network &&
      String(entry.asset) === String(config.assetId),
  )
  if (!accepted) {
    throw new ApiError(
      `the 402 offers nothing this client can pay on ${config.network}.`,
    )
  }
  const extra = (accepted.extra ?? {}) as Record<string, unknown>
  if (extra.agreementId === undefined || extra.feePayer === undefined) {
    throw new ApiError('the 402 is missing agreementId or feePayer')
  }
  // V2 names the field `amount`. V1's `maxAmountRequired` is read only as a
  // fallback, and never silently: a server speaking V1 would be a finding.
  const amount = accepted.amount ?? accepted.maxAmountRequired
  if (amount === undefined) {
    throw new ApiError('the 402 names no amount')
  }
  return {
    quote: {
      agreementId: BigInt(String(extra.agreementId)),
      amount: BigInt(String(amount)),
      assetId: config.assetId,
      payTo: String(accepted.payTo),
      feePayer: String(extra.feePayer),
      // No buyer: the 402 does not name one and must not, since putting the
      // address in the query string would change the resource identity and
      // the server's idempotency key. The runner adds it with `withBuyer`
      // from the connected wallet immediately before the group is built.
      appId: config.appId,
      expiresAt:
        Math.floor(Date.now() / 1000) + Number(accepted.maxTimeoutSeconds ?? 120),
    },
    accepted,
    envelope,
  }
}

/**
 * The route declined to quote, and said why.
 *
 * Separate from a bare `ApiError` because a 503 from the pooled route is
 * usually not a fault at all: `no_pool_open` means the chain was read and no
 * edition is open, which a buyer should be shown as an answer. The server
 * distinguishes that from `pool_status_unavailable`, which means it could not
 * reach the chain and the question is still open, and `reason` is what keeps
 * the two apart on this side.
 */
export class RouteUnavailableError extends ApiError {
  constructor(
    message: string,
    readonly reason: string,
  ) {
    super(message)
  }
}

async function quoteRoute(url: string): Promise<QuotedRoute> {
  const response = await fetch(url, { method: 'POST' })
  if (response.status === 503) {
    // Read strictly enough not to invent a reason: a 503 with an unreadable
    // body is still a 503, and an empty reason says "it declined and did not
    // say why" rather than naming a condition the server never reported.
    const body = await response.json().catch(() => ({}))
    const reason = String((body as Record<string, unknown>).error ?? '')
    throw new RouteUnavailableError(
      `the route is not quoting right now${reason ? `: ${reason}` : ''}`,
      reason,
    )
  }
  if (response.status !== 402) {
    throw new ApiError(`expected a 402 quote, got ${response.status}`)
  }
  return parseQuote(response)
}

/**
 * The payment payload, assembled the way the facilitator's own decoder
 * expects it. Keys whose value is absent are omitted rather than sent as
 * null, matching the server's `exclude_none` serialisation.
 */
export function paymentSignatureHeader(
  quoted: QuotedRoute,
  payload: PaymentPayload,
): string {
  const envelope: Record<string, unknown> = {
    x402Version: quoted.envelope.x402Version ?? 2,
    payload: { paymentGroup: payload.paymentGroup, paymentIndex: payload.paymentIndex },
    accepted: quoted.accepted,
  }
  if (quoted.envelope.resource !== undefined) envelope.resource = quoted.envelope.resource
  if (quoted.envelope.extensions !== undefined) {
    envelope.extensions = quoted.envelope.extensions
  }
  return encodeBase64Json(envelope)
}

/** The facilitator's decoded settlement receipt, when the server attached one. */
export function settlementOf(response: Response): Record<string, unknown> | null {
  const header = response.headers.get(PAYMENT_RESPONSE_HEADER)
  return header ? decodeBase64Json(header, PAYMENT_RESPONSE_HEADER) : null
}

async function payRoute(
  url: string,
  quoted: QuotedRoute,
  payload: PaymentPayload,
): Promise<Response> {
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      // x402 V2. X-PAYMENT is V1 legacy and is not what this server speaks.
      [PAYMENT_SIGNATURE_HEADER]: paymentSignatureHeader(quoted, payload),
      'content-type': 'application/json',
    },
  })
  if (!response.ok) {
    throw new ApiError(`settlement failed with ${response.status}`)
  }
  return response
}

export async function requestQuote(sha256: string, size: number): Promise<QuotedRoute> {
  return await quoteRoute(pinUrl(sha256, size))
}

export interface SettleResult {
  jobId: string
  agreementId: bigint
  deadline: number
}

/**
 * Settlement succeeded and its answer could not be read.
 *
 * Separate from `ApiError` because the two mean opposite things to a buyer.
 * An `ApiError` out of `settle` means the payment did not go through; this
 * means it did, and the receipt is unreadable. Collapsing them would report a
 * completed payment as a failed one, which sends the machine down the resolve
 * fork with the money already spent -- the expensive direction, since that
 * fork can end in a second payment.
 */
export class SettlementUnreadableError extends ApiError {}

const unreadable = (field: string) =>
  new SettlementUnreadableError(
    `the payment settled but the server's answer is missing ${field}, so ` +
      'this client cannot address the upload. The payment is on chain: do ' +
      'not pay again.',
  )

/**
 * Past the `response.ok` check the money has moved, so every remaining
 * failure is a receipt this client cannot read -- never a payment that did
 * not happen.
 */
async function readReceipt(response: Response): Promise<Record<string, unknown>> {
  try {
    return await readJson(response)
  } catch {
    throw new SettlementUnreadableError(
      'the payment settled but the response was not JSON, so this client ' +
        'cannot address it. The payment is on chain: do not pay again.',
    )
  }
}

/**
 * The one response whose 200 means the buyer's USDC has left their wallet.
 *
 * Read strictly, in the same spirit as `parseJobState` and for a stronger
 * reason. `String(body.jobId)` on a missing field yields the string
 * "undefined", which is a job reference the buyer would then be navigated to
 * and the upload addressed to; `BigInt(String(...))` on a missing
 * `agreementId` throws a bare `SyntaxError` from deep inside a success path.
 * Both are checked here so the failure is named at the boundary.
 */
function parseSettleResult(body: Record<string, unknown>): SettleResult {
  // The proof of having paid, minted with the 200.
  if (typeof body.jobId !== 'string' || body.jobId === '') throw unreadable('jobId')

  // Checked against the agreement the machine actually paid into, so a
  // settlement it cannot identify is one it cannot safely act on.
  let agreementId: bigint
  try {
    agreementId = BigInt(String(body.agreementId))
  } catch {
    throw unreadable('a readable agreementId')
  }

  const deadline = Number(body.deadline)
  if (!Number.isFinite(deadline)) throw unreadable('a readable deadline')

  return { jobId: body.jobId, agreementId, deadline }
}

export async function settle(
  sha256: string,
  size: number,
  quoted: QuotedRoute,
  payload: PaymentPayload,
): Promise<SettleResult> {
  const response = await payRoute(pinUrl(sha256, size), quoted, payload)
  return parseSettleResult(await readReceipt(response))
}

/* -------------------------------------------------------------------------- */
/* The pooled route                                                            */
/* -------------------------------------------------------------------------- */

/**
 * A seat in whichever edition pool is currently open.
 *
 * The 402 names the agreement; nothing about the request does.
 *
 * Throws `RouteUnavailableError` with `reason === 'no_pool_open'` when the
 * chain was read and no edition is open. That is an answer rather than a
 * fault, and a caller should show it as one — but it arrives as a throw like
 * every other refusal, because a seat was not quoted and there is nothing to
 * return. `reason === 'pool_status_unavailable'` is the transient twin: the
 * server could not reach the chain, so whether a pool is open is unknown.
 */
export async function requestSeatQuote(): Promise<QuotedRoute> {
  return await quoteRoute(indexUrl())
}

export interface SeatReceipt {
  agreementId: bigint
  seatsTotal: number
  deadline: number
  commitSha256: string
}

/**
 * Deliberately no seat *number* and no CID: the route returns neither, and a
 * client that invented either would be reporting something the chain does not
 * say.
 */
function parseSeatReceipt(body: Record<string, unknown>): SeatReceipt {
  let agreementId: bigint
  try {
    agreementId = BigInt(String(body.agreementId))
  } catch {
    throw unreadable('a readable agreementId')
  }

  const seatsTotal = Number(body.seatsTotal)
  if (!Number.isFinite(seatsTotal)) throw unreadable('a readable seatsTotal')

  const deadline = Number(body.deadline)
  if (!Number.isFinite(deadline)) throw unreadable('a readable deadline')

  // The commitment the edition will be released against. A buyer who cannot
  // read it back cannot check delivery, which is the whole product.
  if (typeof body.commitSha256 !== 'string' || body.commitSha256 === '') {
    throw unreadable('commitSha256')
  }

  return { agreementId, seatsTotal, deadline, commitSha256: body.commitSha256 }
}

/**
 * What one seat purchase produced: the server's receipt, and the
 * facilitator's own statement of the settlement it performed.
 *
 * The two are worth keeping apart. The receipt is the pool's view -- which
 * agreement, how many seats, what was committed. The settlement is the
 * facilitator naming the transaction it submitted, which is the only thing
 * either side can point at afterwards, and it is absent on a server that
 * attached no header rather than being an error.
 */
export interface SeatPurchase {
  receipt: SeatReceipt
  settlement: Record<string, unknown> | null
}

export async function settleSeat(
  quoted: QuotedRoute,
  payload: PaymentPayload,
): Promise<SeatPurchase> {
  const response = await payRoute(indexUrl(), quoted, payload)
  // Degraded to `null` rather than thrown. Past `payRoute` the money has
  // moved, and a garbled header would surface as a plain `ApiError` -- which
  // every caller reads as "the payment did not go through". The header is
  // secondary evidence: `null` already means "no statement from the
  // facilitator", and the receipt below carries what the purchase needs, so
  // throwing it away over this header would report a completed purchase as
  // failed and invite a second one.
  let settlement: Record<string, unknown> | null
  try {
    settlement = settlementOf(response)
  } catch {
    settlement = null
  }
  return { receipt: parseSeatReceipt(await readReceipt(response)), settlement }
}

/* -------------------------------------------------------------------------- */
/* The delivery escrow's remaining calls                                       */
/* -------------------------------------------------------------------------- */

export async function uploadBytes(
  jobId: string,
  file: Blob,
): Promise<{ cid: string; gatewayUrl: string }> {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch(`${getConfig().resourceHost}/pin/${jobId}/content`, {
    method: 'POST',
    body: form,
  })
  if (!response.ok) {
    // The detail if there is one, and no reason to fail over its absence:
    // the status code below is already a complete error on its own.
    const body = (await response.json().catch(() => ({}))) as Record<string, unknown>
    throw new ApiError(String(body.detail ?? `upload failed with ${response.status}`))
  }
  const body = await readJson(response)
  return { cid: String(body.cid), gatewayUrl: String(body.gatewayUrl) }
}

export interface JobView {
  state: JobState
  sha256: string
  size: number
  deadline: number
  agreementId: bigint
  cid?: string
  error?: string
}

export async function getJob(jobId: string): Promise<JobView | null> {
  const response = await fetch(`${getConfig().resourceHost}/pin/${jobId}`)
  if (response.status === 404) return null
  if (!response.ok) throw new ApiError(`job lookup failed with ${response.status}`)
  const body = await readJson(response)
  return {
    state: parseJobState(body.state),
    sha256: String(body.sha256),
    size: Number(body.size),
    deadline: Number(body.deadline),
    agreementId: BigInt(String(body.agreementId)),
    cid: body.cid ? String(body.cid) : undefined,
    error: body.error ? String(body.error) : undefined,
  }
}

/**
 * "Is there a live or recently released job for these bytes."
 *
 * A 404 is an answer, not a failure: the route is bounded by the server's
 * recent-release window, past which the correct move is to re-quote.
 */
export async function getByHash(sha256: string): Promise<HashResult | null> {
  const response = await fetch(`${getConfig().resourceHost}/pin/by-hash/${sha256}`)
  if (response.status === 404) return null
  if (!response.ok) throw new ApiError(`hash query failed with ${response.status}`)
  const body = await readJson(response)
  return {
    state: parseJobState(body.state),
    agreementId: BigInt(String(body.agreementId)),
    cid: body.cid ? String(body.cid) : undefined,
  }
}
