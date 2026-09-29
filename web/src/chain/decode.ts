import algosdk from 'algosdk'

/**
 * The escrow application's two boxes, decoded.
 *
 * Both records are fixed-width, so every field below is read at a literal
 * offset. That is what makes the length check above the decode load-bearing
 * rather than defensive: a record of the wrong size does not fail on its own,
 * it decodes into plausible numbers at the wrong places. A pool that is
 * actually refunding would read as open, with a deadline taken from the
 * middle of an address.
 *
 * Pure, so the whole layout can be checked offline against vectors the Python
 * side produces with an independent ARC-4 encoder.
 */

/** Byte widths the contract fixes; mirrored, never inferred. */
export const AGREEMENT_BYTES = 164
export const SEAT_BYTES = 40

/**
 * The release conditions. A pool is always `HASH`: `QUORUM` releases on a
 * count of payers with no deliverable, which is the other product sharing
 * this application.
 */
export const CONDITION_HASH = 0
export const CONDITION_QUORUM = 1

/**
 * The per-call refund ceiling, mirroring the contract. It is the size of a
 * transaction's accounts array, not a chosen number: every payer an inner
 * transfer pays into has to be available in the same call, and that array
 * holds four.
 *
 * Lives here rather than beside the code that consumes it, because both
 * `pool/view.ts` (sizing a refund batch for the UI) and `chain/calls.ts`
 * (building the `refund_next` call itself) need it, and `chain/` must not
 * import from `pool/` -- `pool/view.ts` already imports from `chain/`, so the
 * reverse direction would be a cycle. `pool/view.ts` re-exports this name so
 * existing imports of it keep resolving.
 */
export const REFUND_BATCH_CEILING = 4

/**
 * Every state the contract stores, as a value so the view map below can be a
 * total `Record` rather than a chain with a fallback.
 *
 * There is no `CLOSED`: `close` deletes both boxes, so a closed agreement is
 * the *absence* of a record rather than a state within one. That absence is a
 * seventh case every reader has to handle, and it is not an error.
 */
export const POOL_STATES = [
  'OPEN',
  'FUNDED',
  'FILLED',
  'RELEASED',
  'EXPIRED',
  'REFUNDING',
  'REFUNDED',
] as const

export type PoolState = (typeof POOL_STATES)[number]

export class BoxDecodeError extends Error {}

function poolState(value: number): PoolState {
  const state = POOL_STATES[value]
  if (state === undefined) {
    throw new BoxDecodeError(
      `the agreement reports state ${value}, which this client does not know. ` +
        'Refusing to guess: the states decide whether money is owed.',
    )
  }
  return state
}

export interface Agreement {
  condition: number
  state: PoolState
  /** Unix seconds, compared by the contract against the *previous* block. */
  deadline: bigint
  sharePrice: bigint
  minSeats: number
  maxSeats: number
  /** Seats taken, not seats available. */
  seats: number
  refundCursor: number
  unclaimedSeats: number
  totalHeld: bigint
  commitSha256: string
  beneficiary: string
  verifier: string
  creator: string
}

function u16(view: DataView, offset: number): number {
  return view.getUint16(offset, false)
}

function u64(view: DataView, offset: number): bigint {
  return view.getBigUint64(offset, false)
}

function address(raw: Uint8Array, offset: number): string {
  return algosdk.encodeAddress(raw.subarray(offset, offset + 32))
}

export function decodeAgreement(raw: Uint8Array): Agreement {
  if (raw.length !== AGREEMENT_BYTES) {
    throw new BoxDecodeError(
      `the agreement box is ${raw.length} bytes, not ${AGREEMENT_BYTES}. Every ` +
        'field below is read at a hardcoded offset, so a record of another ' +
        'size would decode as plausible values at the wrong places rather ' +
        'than fail.',
    )
  }
  const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength)
  return {
    condition: raw[0]!,
    state: poolState(raw[1]!),
    deadline: u64(view, 2),
    sharePrice: u64(view, 10),
    minSeats: u16(view, 18),
    maxSeats: u16(view, 20),
    seats: u16(view, 22),
    refundCursor: u16(view, 24),
    unclaimedSeats: u16(view, 26),
    totalHeld: u64(view, 28),
    commitSha256: algosdk.bytesToHex(raw.subarray(36, 68)),
    beneficiary: address(raw, 68),
    verifier: address(raw, 100),
    creator: address(raw, 132),
  }
}

/**
 * What one roster slot says. Three states, and the distinction between the
 * last two is the one worth getting right:
 *
 *   - `empty`   -- nobody has taken this seat
 *   - `settled` -- somebody took it and has been paid back
 *   - `owed`    -- somebody took it and the money is still theirs
 *
 * `settled` and `empty` both carry a zero amount. Only the address tells them
 * apart, and reading a zeroed amount as an empty seat would report a refunded
 * buyer as never having had a seat — and hide a seat that is still owed money
 * behind a roster that looks unsold.
 */
export type SeatStatus = 'empty' | 'settled' | 'owed'

export interface Seat {
  index: number
  payer: string
  amount: bigint
  status: SeatStatus
}

/** Derived once, so nothing has to remember what an all-zero address means. */
export const ZERO_ADDRESS = algosdk.encodeAddress(new Uint8Array(32))

export function decodeRoster(raw: Uint8Array): Seat[] {
  if (raw.length % SEAT_BYTES !== 0) {
    throw new BoxDecodeError(
      `the roster box is ${raw.length} bytes, which is not a whole number of ` +
        `${SEAT_BYTES}-byte seats.`,
    )
  }
  const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength)
  const seats: Seat[] = []
  for (let offset = 0; offset < raw.length; offset += SEAT_BYTES) {
    const payer = address(raw, offset)
    const amount = u64(view, offset + 32)
    seats.push({
      index: offset / SEAT_BYTES,
      payer,
      amount,
      status: payer === ZERO_ADDRESS ? 'empty' : amount === 0n ? 'settled' : 'owed',
    })
  }
  return seats
}

/**
 * The box names, which are a prefix byte and the id big-endian.
 *
 * Big-endian is not incidental: it makes ordering by box name the same as
 * ordering by agreement id, which is what lets the operator's scan walk
 * backwards from the newest.
 */
export function agreementBoxName(agreementId: bigint): Uint8Array {
  return prefixed('a', agreementId)
}

export function rosterBoxName(agreementId: bigint): Uint8Array {
  return prefixed('r', agreementId)
}

function prefixed(prefix: string, agreementId: bigint): Uint8Array {
  return Uint8Array.from([
    ...algosdk.coerceToBytes(prefix),
    ...algosdk.encodeUint64(agreementId),
  ])
}

/**
 * The seat this wallet holds, or null.
 *
 * A linear scan rather than an index lookup, because the roster is addressed
 * by seat and the client knows an address. It is cheap — the contract caps a
 * pool at twenty seats — and the contract guarantees an address appears on a
 * roster at most once, so the first match is the only match.
 */
export function findSeat(seats: Seat[], address: string): Seat | null {
  return seats.find((seat) => seat.payer === address && seat.status !== 'empty') ?? null
}
