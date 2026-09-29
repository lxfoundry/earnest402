import algosdk from 'algosdk'
import { REFUND_BATCH_CEILING, agreementBoxName, rosterBoxName, type Agreement, type Seat } from './decode'
import { selectorBytes } from './selectors'

/**
 * The four refund-path application calls, as pure transaction builders.
 *
 * A buyer whose pool missed its deadline gets their money back through
 * `expire`, `refund_next`, `claim_refund` and `close`, in that order. Unlike
 * `payment/buildJoinGroup.ts`, none of these needs a facilitator, a fee payer
 * or a group: a purchase has to be atomic with a payment, but a refund has
 * nothing to be atomic with, so each of these is one plain application call
 * with one signature, and the contract funds the inner transfer from its own
 * reserve. That is also why the fee is never touched here -- `buildJoinGroup`
 * pools three fees onto one leg because two of its three transactions must
 * carry zero; these calls are singletons, so the suggested params are passed
 * straight through and algosdk prices the fee from the call's own size.
 *
 * Every builder is pure: suggested params and the already-decoded record (and,
 * where the call needs it, the decoded roster) arrive as arguments, so the one
 * read a caller does to ask `pool/view.ts`'s `availableActions` what is
 * offered is the same read that builds the transaction -- there is no second
 * fetch here to fall out of step with the first.
 *
 * These builders do not repeat `availableActions`' state checks (deadline
 * passed, pool in `EXPIRED`, and so on). That logic lives in exactly one
 * place already; a second copy here would be a second place for it to drift
 * out of step with the contract. What these do check is argument validity --
 * the things that are wrong regardless of what the chain currently says --
 * because those are checks `pool/view.ts` cannot make for a caller: it never
 * sees `count` or a seat index, both of which are chosen at the call site.
 */

/** An argument a builder was given that no chain state could make valid. */
export class CallArgumentError extends Error {}

/**
 * An upper bound, in bytes, on the signed size of any call built here.
 *
 * What a sender pays is the larger of the network's minimum fee and its
 * per-byte rate times the transaction's size. The rate is zero until the
 * network is congested, so the minimum is what everyone pays almost always --
 * but a fee check that assumed the minimum would say "yes" under congestion to
 * a wallet that cannot pay what the node then asks. Priced against this bound
 * instead, the check can only err towards withholding a call a wallet could
 * have afforded, which costs the buyer nothing: the refund calls pay the
 * roster address whoever sends them.
 *
 * `web/tests/calls.test.ts` builds all four calls with every variable-width
 * field at its maximum, signs them as a rekeyed wallet would -- which adds the
 * authorising address -- and holds them under this bound. The largest,
 * `refund_next` with a full accounts array, measured 491 bytes there.
 */
export const CALL_BYTES_CEILING = 512

const UINT64 = new algosdk.ABIUintType(64)

/** Fields every one of the four calls needs, regardless of which it is. */
export interface CallParams {
  suggestedParams: algosdk.SuggestedParams
  /**
   * Whoever will sign and submit. Every one of these calls is permissionless
   * (`pool/view.ts`'s `Action` type), so this need not be the buyer -- it is
   * simply the address paying this transaction's own fee.
   */
  sender: string
  appId: bigint
  assetId: bigint
  agreementId: bigint
}

/**
 * The agreement box alone, or both boxes.
 *
 * `expire` only ever touches the agreement record -- it flips the state to
 * `EXPIRED` (or, for a stranded `FILLED` beneficiary, checks a holding) and
 * pays nobody, so it never reads or writes a seat and the roster box has
 * nothing for it to name. The other three all read or write seats -- paying
 * one out, marking it settled, or bounds-checking a claim against it -- so
 * both boxes travel together on those.
 */
function boxRefs(agreementId: bigint, includeRoster: boolean): algosdk.BoxReference[] {
  const boxes: algosdk.BoxReference[] = [{ appIndex: 0, name: agreementBoxName(agreementId) }]
  if (includeRoster) boxes.push({ appIndex: 0, name: rosterBoxName(agreementId) })
  return boxes
}

/**
 * The one shape all four calls share: a NoOp call carrying a selector, its
 * ARC-4 arguments, the accounts an inner transfer will pay, the named boxes,
 * and the configured USDC asset.
 *
 * `foreignAssets` carries the asset on all four. `refund_next` and
 * `claim_refund` pay USDC out, and `expire`'s stranded-beneficiary branch
 * reads a USDC holding through `_can_receive` before it will let a `FILLED`
 * agreement expire -- in both cases the account and the asset have to be
 * named in the same transaction for the AVM to resolve it. `close` pays no
 * USDC at all, but the array holds one id regardless of which call it is, so
 * there is nothing to gain by special-casing the one call that does not need
 * it -- and something to lose, since a builder that omits it for one call has
 * to remember to add it back the moment that call's contract logic changes.
 */
function appCall(
  selector: Uint8Array,
  params: CallParams,
  args: Uint8Array[],
  accounts: string[],
  boxes: algosdk.BoxReference[],
): algosdk.Transaction {
  return algosdk.makeApplicationNoOpTxnFromObject({
    sender: params.sender,
    appIndex: params.appId,
    appArgs: [selector, ...args],
    accounts,
    foreignAssets: [params.assetId],
    boxes,
    suggestedParams: params.suggestedParams,
  })
}

export interface ExpireParams extends CallParams {
  agreement: Agreement
}

/**
 * `expire(uint64)void` -- moves a pool that missed its deadline into
 * `EXPIRED`, unlocking the refund pass.
 *
 * The beneficiary is named in the accounts array because the contract reads
 * its holding as part of `_can_receive` before allowing the transition, even
 * though no money moves on this call. Taken from the already-decoded
 * `Agreement` rather than asked for separately, so a caller cannot pass a
 * beneficiary that does not match the record it just read.
 */
export function buildExpire(params: ExpireParams): algosdk.Transaction {
  return appCall(
    selectorBytes('expire'),
    params,
    [UINT64.encode(params.agreementId)],
    [params.agreement.beneficiary],
    boxRefs(params.agreementId, false),
  )
}

export interface RefundNextParams extends CallParams {
  agreement: Agreement
  roster: Seat[]
  count: number
}

/**
 * `refund_next(uint64,uint64)void` -- pays or skips the next `count` roster
 * entries, FIFO from the record's own `refundCursor`.
 *
 * The accounts array is built from the roster *window this call will
 * actually touch* -- `roster[refundCursor : refundCursor + count]` -- never
 * from seat zero and never the whole roster. Every account an inner transfer
 * pays into has to be available in the same transaction, so naming the wrong
 * seats fails on chain with a resource error that points nowhere near the
 * real mistake.
 *
 * `count` is checked against the ceiling here because it is wrong
 * independently of chain state: the contract's accounts array holds four
 * entries no matter what state the pool is in, so a `count` outside
 * `1..REFUND_BATCH_CEILING` can never be satisfied and is not a question
 * `availableActions` was ever going to answer. Deliberately not checked
 * against how many seats remain unrefunded -- that *is* chain state, and
 * `pool/view.ts`'s `refundBatchSize` already sizes it; a second bound here
 * would be a second copy of that sizing.
 */
export function buildRefundNext(params: RefundNextParams): algosdk.Transaction {
  const { count } = params
  if (!(count > 0 && count <= REFUND_BATCH_CEILING)) {
    throw new CallArgumentError(
      `refund_next's count must be between 1 and ${REFUND_BATCH_CEILING} -- the ` +
        'size of the accounts array a call can carry, one seat per payer -- ' +
        `got ${count}`,
    )
  }
  const cursor = params.agreement.refundCursor
  const window = params.roster.slice(cursor, cursor + count)
  return appCall(
    selectorBytes('refundNext'),
    params,
    [UINT64.encode(params.agreementId), UINT64.encode(BigInt(count))],
    window.map((seat) => seat.payer),
    boxRefs(params.agreementId, true),
  )
}

export interface ClaimRefundParams extends CallParams {
  roster: Seat[]
  seat: number
}

/**
 * `claim_refund(uint64,uint64)void` -- a payer the batch pass skipped claims
 * their own seat back.
 *
 * `seat` is bounds-checked against the roster passed in, rather than indexed
 * blindly, and for exactly the reason `api/escrow.py`'s `claim_refund` gives:
 * a missing roster box decodes to `[]` (`chain/read.ts`'s `readRoster`), which
 * is the ordinary state once `close` has run, so an unchecked `roster[seat]`
 * would throw *before* any transaction was composed and say nothing about
 * which of the two things went wrong -- a bad seat number, or a pool that is
 * already gone. Checking here turns both into one clear message instead of a
 * `TypeError` from deep inside this function.
 */
export function buildClaimRefund(params: ClaimRefundParams): algosdk.Transaction {
  const { roster, seat } = params
  if (!(seat >= 0 && seat < roster.length)) {
    throw new CallArgumentError(
      `seat ${seat} is out of range for a roster of ${roster.length} seat(s). ` +
        'A missing roster box reads as an empty roster, so this is either a ' +
        'bad seat number or a pool that no longer exists -- not something to ' +
        'find out from a rejected transaction.',
    )
  }
  return appCall(
    selectorBytes('claimRefund'),
    params,
    [UINT64.encode(params.agreementId), UINT64.encode(BigInt(seat))],
    [roster[seat]!.payer],
    boxRefs(params.agreementId, true),
  )
}

export interface CloseParams extends CallParams {
  agreement: Agreement
}

/**
 * `close(uint64)void` -- deletes both boxes and returns the unspent deposit
 * to the creator.
 *
 * The creator is named in the accounts array because it is who the inner
 * ALGO payment above pays into -- every account an inner transfer pays into
 * has to be available in the same transaction, whether that transfer moves
 * the configured asset or, as here, ALGO. Taken from the decoded `Agreement`
 * rather than asked for separately, so this can never be pointed at an
 * address other than the one the record itself names.
 */
export function buildClose(params: CloseParams): algosdk.Transaction {
  return appCall(
    selectorBytes('close'),
    params,
    [UINT64.encode(params.agreementId)],
    [params.agreement.creator],
    boxRefs(params.agreementId, true),
  )
}
