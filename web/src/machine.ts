/**
 * The purchase, as a pure reducer.
 *
 * `(state, event) => (state, effects)`. Nothing here touches the network, the
 * wallet or the DOM: `runner.ts` executes the effects and feeds the results
 * back as events. That split is what lets every branch below be tested
 * offline, which matters because several of them cost money when wrong.
 *
 * The state holds no `File`. It holds the digest, the size and `haveBytes`;
 * the `File` itself lives in a ref outside the reducer. So every state is
 * serialisable, and "the page has a job but not the bytes" is the ordinary
 * NEED_FILE state rather than a null check in a component.
 */

/**
 * Every state the server's job record can be in.
 *
 * A value, not just a type: `api.ts` checks the wire against it, which is
 * what lets `DECISION_FOR` below be a total map rather than a chain with a
 * fallback. The reducer owns this vocabulary because it is the reducer that
 * has to price each one; `api.ts` imports it rather than the other way
 * round, so the pure core keeps depending on nothing.
 */
export const JOB_STATES = [
  'QUOTED',
  'FUNDED',
  'RECEIVED',
  'PINNED',
  'RELEASED',
  'ABANDONED',
  'REFUNDED',
] as const

export type JobState = (typeof JOB_STATES)[number]

export type Decision = 'RETRY_PAYMENT' | 'BUY_YOUR_OWN' | 'SHOW_CID' | 'REQUOTE'

export interface FileFacts {
  sha256: string
  size: number
  /** False after a reload: the job survives in the URL, the File does not. */
  haveBytes: boolean
}

export interface Quote {
  agreementId: bigint
  amount: bigint
  payTo: string
  feePayer: string
  /** Unix seconds. The funding window is short and this is when it lapses. */
  expiresAt: number
}

export interface HashResult {
  state: JobState
  agreementId: bigint
  cid?: string
}

export type Status =
  | 'CONNECT'
  | 'CHOOSE'
  | 'READY'
  | 'QUOTING'
  | 'CONFIRM'
  | 'UPLOADING'
  | 'WORKING'
  | 'DONE'
  | 'ENDED'
  | 'RESOLVING'
  | 'DECIDE'
  | 'NEED_FILE'

export interface State {
  status: Status
  wallet?: string
  file?: FileFacts
  quote?: Quote
  /**
   * The agreement a settlement is currently in flight against.
   *
   * Set when the machine asks for a group to be built and signed, cleared
   * when the matching `SETTLED` arrives. It exists because `quote` cannot
   * answer "is this settlement mine": the resolve fork clears the quote under
   * every outcome but `RETRY_PAYMENT`, and the whole point of this field is
   * to still be here when a settlement lands *after* the machine has given up
   * waiting for it. A 200 is proof of payment whatever the machine did in the
   * meantime, so it is identity, not status, that decides whether to take it.
   */
  settlingAgreementId?: bigint
  jobId?: string
  jobState?: JobState
  deadline?: number
  cid?: string
  gatewayUrl?: string
  decision?: Decision
  error?: string
}

export type Event =
  | { type: 'WALLET_CONNECTED'; address: string }
  | { type: 'WALLET_DISCONNECTED' }
  | { type: 'FILE_SELECTED'; name: string; size: number }
  | { type: 'FILE_HASHED'; sha256: string; size: number }
  | { type: 'FILE_REJECTED'; reason: string }
  | { type: 'QUOTE_REQUESTED' }
  | {
      type: 'QUOTE_RECEIVED'
      agreementId: bigint
      amount: bigint
      payTo: string
      feePayer: string
      expiresAt: number
    }
  | { type: 'QUOTE_FAILED'; error: string }
  | { type: 'SETTLE_REQUESTED' }
  | { type: 'SETTLED'; jobId: string; agreementId: bigint; deadline: number }
  | { type: 'SETTLE_FAILED'; error: string }
  | { type: 'HASH_RESOLVED'; result: HashResult | null }
  | { type: 'HASH_QUERY_FAILED' }
  | { type: 'UPLOADED'; cid: string; gatewayUrl: string }
  | { type: 'UPLOAD_FAILED'; error: string }
  | {
      type: 'STATUS_POLLED'
      state: JobState
      sha256?: string
      size?: number
      cid?: string
    }
  | { type: 'WINDOW_ELAPSED' }
  | { type: 'PAGE_OPENED_WITH_JOB'; jobId: string }
  | { type: 'RETRY_PAYMENT' }
  | { type: 'REQUOTE' }

export type Effect =
  | { type: 'HASH_FILE' }
  | { type: 'REQUEST_QUOTE'; sha256: string; size: number }
  | { type: 'BUILD_AND_SIGN' }
  | { type: 'UPLOAD_BYTES'; jobId: string }
  | { type: 'POLL_STATUS'; jobId: string }
  | { type: 'QUERY_HASH'; sha256: string }
  | { type: 'NAVIGATE'; path: string }
  | { type: 'CLEAR_FILE' }

interface Result {
  state: State
  effects: Effect[]
}

/**
 * What the resolve fork does with each state the hash query can return.
 *
 * A total map rather than a conditional chain: `Record<JobState, Decision>`
 * will not compile with a member missing, so a new job state has to be
 * priced here deliberately instead of falling into a tail branch. That tail
 * used to be `REQUOTE`, which is the expensive direction to guess wrong in --
 * it parks a second deposit -- which is why the wire is checked in `api.ts`
 * rather than trusted and defaulted here.
 */
const DECISION_FOR: Record<JobState, Decision> = {
  // The seat is still ours and unfunded, so paying again costs nothing more.
  QUOTED: 'RETRY_PAYMENT',
  // Someone holds a funded seat for these bytes; ours would be a second one.
  FUNDED: 'BUY_YOUR_OWN',
  RECEIVED: 'BUY_YOUR_OWN',
  PINNED: 'BUY_YOUR_OWN',
  // Already delivered: the CID is the answer, and there is nothing to buy.
  RELEASED: 'SHOW_CID',
  // The agreement is over and cannot be paid into, so start a new one.
  ABANDONED: 'REQUOTE',
  REFUNDED: 'REQUOTE',
}

export function initialState(): State {
  return { status: 'CONNECT' }
}

/**
 * Whether a newly chosen file may replace what the machine is holding.
 *
 * An allowlist, like `MAY_QUOTE`, and for the same reason: every state left
 * out of it holds an agreement that the new bytes would invalidate, and
 * silently swapping the file under one of them is how a buyer ends up with
 * two deposits parked against one purchase. A status added later is in
 * neither set until someone puts it there, so it has to opt *in* to spending
 * money rather than out of it.
 *
 * CONFIRM is in this set because its agreement is *unfunded*: abandoning it
 * costs the buyer nothing and the server sweeps it at the end of the funding
 * window. NEED_FILE is in it because re-selecting is the entire point of that
 * state -- and FILE_HASHED checks those bytes against the digest the
 * agreement was actually created with before accepting them.
 */
const ACCEPTS_NEW_FILE: ReadonlySet<Status> = new Set([
  'CONNECT',
  'CHOOSE',
  'READY',
  'CONFIRM',
  'NEED_FILE',
  'DECIDE',
  'ENDED',
  'DONE',
])

/**
 * Which statuses may open a new agreement. The one rule worth stating twice:
 * never re-quote while an agreement is still open, because every unpaid pass
 * parks a deposit on chain.
 *
 * Kept next to `ACCEPTS_NEW_FILE` because the interesting fact is the
 * difference between them. CONFIRM and NEED_FILE are the two members that set
 * has and this one does not: both will take different bytes, but both are
 * already holding an agreement -- CONFIRM an unfunded quote, NEED_FILE a
 * funded escrow running against a deadline -- so neither may open a second.
 * QUOTING, UPLOADING, WORKING and RESOLVING are in neither: their outcome is
 * in flight and would arrive describing the previous file.
 */
const MAY_QUOTE: ReadonlySet<Status> = new Set([
  'CONNECT',
  'CHOOSE',
  'READY',
  'DECIDE',
  'ENDED',
  'DONE',
])

/**
 * Which statuses may take the server's word about a job.
 *
 * A third allowlist, for the same reason as the other two: `STATUS_POLLED`
 * arrives from a poll loop, and a poll result that outlives the state it was
 * started for would otherwise be applied wherever the machine now stands. The
 * expensive one is CONFIRM, which holds an unfunded quote and is in
 * `MAY_QUOTE`: a RELEASED poll landing there reports DONE over a live quote,
 * and the buyer's next purchase starts from a state still holding it.
 *
 * These three are the statuses that hold a funded job and are waiting to hear
 * what became of it. Everything else either has no job yet or has finished
 * with it.
 */
const ACCEPTS_JOB_NEWS: ReadonlySet<Status> = new Set([
  'UPLOADING',
  'WORKING',
  'NEED_FILE',
])

/** Where to sit once a wallet and a digest are both, or not both, in hand. */
function idle(state: State): Status {
  if (!state.wallet) return 'CONNECT'
  if (!state.file) return 'CHOOSE'
  return 'READY'
}

const only = (state: State): Result => ({ state, effects: [] })

/**
 * Compile-time exhaustiveness guard.
 *
 * Reachable only if an Event variant has no branch in `reduce`, in which case
 * `event` is not `never` and the call fails to compile -- which is the whole
 * point. It returns the state unchanged rather than throwing: a reducer that
 * threw on an unknown event would strand the machine instead of ignoring it.
 */
function exhaustive(_event: never, state: State): Result {
  return only(state)
}

export function reduce(state: State, event: Event): Result {
  switch (event.type) {
    case 'WALLET_CONNECTED': {
      // Connecting never quotes. The unpaid pass commits an agreement, so it
      // waits for an explicit request.
      const next = { ...state, wallet: event.address }
      if (state.status === 'CONNECT') return only({ ...next, status: idle(next) })
      return only(next)
    }

    case 'WALLET_DISCONNECTED': {
      // The quote goes with the wallet that would have paid it: nothing can
      // settle into that agreement now, and reconnecting must not come back
      // to a state still holding it. The server sweeps it at the end of the
      // funding window, as it does any unanswered 402.
      const next = { ...state, wallet: undefined, quote: undefined }
      if (state.jobId) return only(next)
      return only({ ...next, status: 'CONNECT' })
    }

    case 'FILE_SELECTED':
      // Hash early, quote late: hashing is local and free, so it happens even
      // with no wallet connected and the window is not spent on a handshake.
      //
      // But not in a state that cannot survive the file changing underneath
      // it. QUOTING and RESOLVING have an outcome in flight that will arrive
      // describing the *previous* file; UPLOADING and WORKING hold a funded
      // agreement whose commit_hash these bytes would not match.
      if (!ACCEPTS_NEW_FILE.has(state.status)) return only(state)
      return { state: { ...state, error: undefined }, effects: [{ type: 'HASH_FILE' }] }

    case 'FILE_HASHED': {
      const file: FileFacts = {
        sha256: event.sha256,
        size: event.size,
        haveBytes: true,
      }
      if (state.status === 'NEED_FILE') {
        // No digest to check against means the job's own record has not been
        // read back yet. Accepting the file here would upload unverified
        // bytes against a funded agreement, so the guard refuses rather than
        // falling through on a missing precondition.
        // The digest to check against and the job to send to. Neither is
        // optional here, and neither is asserted: a missing one means the
        // job's own record has not been read back yet, and accepting the file
        // would upload unverified bytes against a funded agreement or address
        // the upload to nothing at all.
        if (!state.file || !state.jobId) {
          return only({
            ...state,
            error:
              'Still reading this job back from the server. Try again in a ' +
              'moment -- the file has to be checked against the sha256 the ' +
              'escrow was created with before it can be sent.',
          })
        }
        if (state.file.sha256 !== event.sha256) {
          return only({
            ...state,
            error:
              'That is a different file. This job is escrowed against the ' +
              'sha256 committed when it was quoted, so only the original ' +
              'bytes can complete it.',
          })
        }
        return {
          state: { ...state, file, error: undefined, status: 'UPLOADING' },
          effects: [{ type: 'UPLOAD_BYTES', jobId: state.jobId }],
        }
      }
      // A new file starts a new purchase, so nothing about the old one is
      // carried forward. Any quote in hand was created with the previous
      // digest as its commit_hash, so paying into it now would escrow money
      // against bytes that can never match it -- and leaving it in state is
      // what would let the next QUOTE_REQUESTED layer a second unpaid pass,
      // and a second deposit, on top of a live agreement. An abandoned quote
      // costs nothing further: the server sweeps it when its funding window
      // elapses, which is the same path an unanswered 402 already takes.
      const next: State = { status: 'CHOOSE', wallet: state.wallet, file }
      return only({ ...next, status: idle(next) })
    }

    case 'FILE_REJECTED': {
      // Oversize and empty both land here, before any quote.
      //
      // `idle` is computed from the *next* state, never from this one. A
      // rejection after a valid file was already hashed clears the digest, so
      // reading the old state would return READY while `file` is undefined --
      // a state that claims a digest it does not have.
      const next = { ...state, file: undefined, error: event.reason }
      // The File itself lives in a ref outside the reducer, so clearing the
      // digest here has to be paired with telling the runner to drop it --
      // otherwise `haveBytes` and the ref disagree, which is the one seam the
      // state/ref split creates.
      return {
        state: { ...next, status: idle(next) },
        effects: [{ type: 'CLEAR_FILE' }],
      }
    }

    case 'QUOTE_REQUESTED':
    case 'REQUOTE': {
      // DECIDE is in `MAY_QUOTE`, but one of its four outcomes keeps the
      // agreement: `RETRY_PAYMENT` means the hash query found the seat still
      // ours, so paying again costs nothing and quoting again costs a second
      // deposit. That is a fact about the decision rather than the status,
      // which is why it is refined here and not in the set.
      const mayQuote =
        MAY_QUOTE.has(state.status) &&
        (state.status !== 'DECIDE' || state.decision !== 'RETRY_PAYMENT')
      if (!mayQuote) return only(state)
      // The bytes, not merely a digest. A page restored from its URL knows
      // the sha256 but holds nothing to upload, so quoting there would commit
      // an agreement, and its deposit, to a delivery it cannot make.
      if (!state.wallet || !state.file?.haveBytes) {
        return only({ ...state, status: idle(state) })
      }
      return {
        state: {
          status: 'QUOTING',
          wallet: state.wallet,
          file: state.file,
        },
        effects: [
          { type: 'REQUEST_QUOTE', sha256: state.file.sha256, size: state.file.size },
        ],
      }
    }

    case 'QUOTE_RECEIVED':
      if (state.status !== 'QUOTING') return only(state)
      return only({
        ...state,
        status: 'CONFIRM',
        quote: {
          agreementId: event.agreementId,
          amount: event.amount,
          payTo: event.payTo,
          feePayer: event.feePayer,
          expiresAt: event.expiresAt,
        },
      })

    case 'QUOTE_FAILED':
      if (state.status !== 'QUOTING') return only(state)
      // Nothing was committed, so there is nothing to resolve -- unlike a
      // settlement failure, which may or may not have moved money.
      return only({ ...state, status: idle(state), error: event.error })

    case 'SETTLE_REQUESTED':
      if (state.status !== 'CONFIRM') return only(state)
      // The quote is what names the agreement about to be funded. CONFIRM
      // always holds one, but the settlement identity is recorded from it
      // rather than assumed, so there is no path that builds a group without
      // the machine knowing which agreement it is paying into.
      if (!state.quote) return only(state)
      return {
        state: { ...state, settlingAgreementId: state.quote.agreementId },
        effects: [{ type: 'BUILD_AND_SIGN' }],
      }

    case 'SETTLED': {
      // Identity, not status. A 200 means the money has moved and the jobId
      // has been minted for whoever paid, so the only question worth asking
      // is whether this is the settlement this machine started -- never where
      // the machine happens to be standing when the answer arrives.
      //
      // Status was the wrong question because the funding-window countdown
      // and the settlement race each other, and the countdown is short. If
      // WINDOW_ELAPSED lands first the machine is already in RESOLVING, and a
      // status guard drops the 200 that follows: the bytes are never
      // uploaded, the escrow the buyer just funded runs to its deadline and
      // refunds, and the resolve fork reads their own funded agreement as a
      // stranger's and offers to sell them a second one.
      //
      // The case the old guard was defending still holds, and now for a
      // better reason. A file swapped at CONFIRM builds a fresh state through
      // FILE_HASHED, which drops this field with the rest of the purchase, so
      // a settlement for the abandoned agreement no longer matches and is
      // still ignored.
      if (
        state.settlingAgreementId === undefined ||
        event.agreementId !== state.settlingAgreementId
      ) {
        return only(state)
      }

      // The jobId is minted with the 200 and is the proof of having paid, so
      // it goes into the URL before anything else can fail.
      const next: State = {
        ...state,
        jobId: event.jobId,
        jobState: 'FUNDED',
        deadline: event.deadline,
        // Both spent: the agreement is funded, so there is no quote left to
        // pay and no settlement left in flight. Leaving either would let a
        // duplicate 200 be processed twice.
        quote: undefined,
        settlingAgreementId: undefined,
        // The fork is over however it was going, and a decision left in state
        // is one a screen would still be rendering.
        decision: undefined,
        error: undefined,
      }
      const navigate: Effect = { type: 'NAVIGATE', path: `/app/j/${event.jobId}` }

      // Funded, but the bytes are not in hand -- a settlement that landed
      // after a reload. NEED_FILE is exactly that state, and asking for the
      // upload here would send nothing.
      if (!state.file?.haveBytes) {
        return { state: { ...next, status: 'NEED_FILE' }, effects: [navigate] }
      }
      return {
        state: { ...next, status: 'UPLOADING' },
        effects: [navigate, { type: 'UPLOAD_BYTES', jobId: event.jobId }],
      }
    }

    case 'SETTLE_FAILED':
    case 'WINDOW_ELAPSED': {
      // Both belong to an open quote, so both are ignored anywhere else.
      //
      // The funding-window countdown is owned by the runner, and a timer left
      // running after settlement fires while the job is UPLOADING or WORKING.
      // Unguarded, that walks the machine into the resolve fork, where the
      // hash query answers FUNDED -- correctly, because we funded it -- and
      // the fork reads its own buyer as a stranger holding the seat. The
      // offer that follows is "buy your own pin", which settles a second real
      // payment for bytes already paid for. RETRY_PAYMENT re-enters CONFIRM,
      // so the legitimate second attempt still resolves through here.
      if (state.status !== 'CONFIRM') return only(state)

      // Every settlement failure routes through the hash query, and never to
      // a raw error. The client does not classify the failure; it asks the
      // server what actually happened.
      if (!state.file) return only({ ...state, status: idle(state) })
      return {
        state: { ...state, status: 'RESOLVING', quote: state.quote },
        effects: [{ type: 'QUERY_HASH', sha256: state.file.sha256 }],
      }
    }

    case 'HASH_RESOLVED': {
      if (state.status !== 'RESOLVING') return only(state)
      const result = event.result
      if (!result) return only({ ...state, status: 'DECIDE', decision: 'REQUOTE' })
      const decision: Decision = DECISION_FOR[result.state]
      // Only RETRY_PAYMENT still has a use for the quote: it means the hash
      // query found the seat still ours, so the payment is rebuilt against
      // that same agreement. Under every other outcome the agreement is gone
      // or belongs to someone else, and keeping it would leave a dead quote
      // in state for the next re-quote to trip over.
      return only({
        ...state,
        status: 'DECIDE',
        decision,
        quote: decision === 'RETRY_PAYMENT' ? state.quote : undefined,
        jobState: result.state,
        cid: result.cid,
      })
    }

    case 'HASH_QUERY_FAILED':
      if (state.status !== 'RESOLVING') return only(state)
      // The query is bounded by the server's recent-release window, so a 404
      // and an outage arrive the same way. Re-quoting is right for both, and
      // re-quoting has no use for the quote that failed.
      return only({
        ...state,
        status: 'DECIDE',
        decision: 'REQUOTE',
        quote: undefined,
      })

    case 'RETRY_PAYMENT':
      // Reuses the open agreement: no second unpaid pass, no second deposit.
      //
      // Guarded on the status as well as the decision, and on the quote it is
      // about to rebuild from. `decision` alone let this fire twice from one
      // screen -- two wallet prompts for one seat -- and let a stale
      // RETRY_PAYMENT survive into UPLOADING, where SETTLED has already
      // cleared the quote and the runner would be asked to build from
      // nothing.
      if (state.status !== 'DECIDE') return only(state)
      if (state.decision !== 'RETRY_PAYMENT' || !state.quote) return only(state)
      return {
        state: {
          ...state,
          status: 'CONFIRM',
          decision: undefined,
          // The second attempt pays into the same agreement, so it settles
          // under the same identity. Recorded here as well as in
          // SETTLE_REQUESTED because this branch builds and signs directly.
          settlingAgreementId: state.quote.agreementId,
        },
        effects: [{ type: 'BUILD_AND_SIGN' }],
      }

    case 'UPLOADED':
      return only({
        ...state,
        status: 'DONE',
        jobState: 'RELEASED',
        // Terminal: there is no open agreement left to hold, and a quote
        // carried into DONE would still be here when the buyer starts their
        // next purchase.
        quote: undefined,
        settlingAgreementId: undefined,
        decision: undefined,
        cid: event.cid,
        gatewayUrl: event.gatewayUrl,
      })

    case 'UPLOAD_FAILED':
      // Only the upload can fail this way. Unguarded, an upload result that
      // outlived its own state -- a late rejection, a retry from a screen the
      // buyer has already left -- lands anywhere and puts the machine in
      // NEED_FILE holding no job, which is the one state NEED_FILE is not
      // allowed to be: re-selecting the file there asks for an upload against
      // an undefined jobId.
      if (state.status !== 'UPLOADING') return only(state)
      // The bytes are still in hand and the deadline has not necessarily
      // passed, so this is retryable rather than terminal.
      return only({ ...state, status: 'NEED_FILE', error: event.error })

    case 'PAGE_OPENED_WITH_JOB':
      return {
        state: { ...state, status: 'WORKING', jobId: event.jobId },
        effects: [{ type: 'POLL_STATUS', jobId: event.jobId }],
      }

    case 'STATUS_POLLED': {
      if (!ACCEPTS_JOB_NEWS.has(state.status)) return only(state)

      // Rebuilt from the server's own view when the page arrived by URL and
      // holds nothing yet. Both fields come from the job record -- a
      // fabricated size would travel into a re-quote and price a purchase
      // against zero bytes, for a key the server's (sha256, size) idempotency
      // could never match to the original.
      const file: FileFacts | undefined =
        state.file ??
        (event.sha256 !== undefined && event.size !== undefined
          ? { sha256: event.sha256, size: event.size, haveBytes: false }
          : undefined)
      const base = { ...state, file, jobState: event.state }

      // Both terminal, so both drop whatever agreement state was in hand.
      if (event.state === 'RELEASED') {
        return only({
          ...base,
          status: 'DONE',
          quote: undefined,
          settlingAgreementId: undefined,
          decision: undefined,
          cid: event.cid,
        })
      }
      if (event.state === 'ABANDONED' || event.state === 'REFUNDED') {
        return only({
          ...base,
          status: 'ENDED',
          quote: undefined,
          settlingAgreementId: undefined,
          decision: undefined,
        })
      }
      if (event.state === 'FUNDED' && !file?.haveBytes) {
        // Funded and waiting on bytes we no longer hold: re-prompt. Persisting
        // the file across a reload is deliberately not built -- measure first.
        return only({ ...base, status: 'NEED_FILE' })
      }
      return {
        state: { ...base, status: 'WORKING' },
        effects: state.jobId ? [{ type: 'POLL_STATUS', jobId: state.jobId }] : [],
      }
    }

    default:
      return exhaustive(event, state)
  }
}
