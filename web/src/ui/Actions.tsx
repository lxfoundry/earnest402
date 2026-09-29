import { poolPath, type Sending, type State } from '../pool/machine'
import type { PoolMachine } from '../pool/usePoolMachine'
import type { Action } from '../pool/view'
import { useBrand } from './BrandContext'
import { Hex, Link } from './Link'

// Each call is named the way a buyer would say it, once as a button and once
// inside a sentence about a call that is travelling: `copy.calls`, per brand.

/**
 * The calls the verdict offers, and what became of the last one.
 *
 * **`actions` is rendered exactly: every one, in order, and nothing else.**
 * The verdict is where each call is checked against the contract's own guards
 * -- `close` is withheld on a released pool there, `claim_refund` until the
 * refund pass has gone past the seat -- and a button this component added or
 * hid would be a second copy of those rules, free to drift from the first.
 */
export function Actions({
  state,
  actions,
  dispatch,
}: {
  state: State
  actions: Action[]
  dispatch: PoolMachine['dispatch']
}) {
  // Every button, whichever pool the call in flight names. The reducer holds
  // one lock across all pools, so a button left enabled here while another
  // pool's call travels would be a click that silently does nothing.
  //
  // And while a read is out. A confirmed call releases the lock before the
  // read that reflects it lands, so for that one read the verdict on screen
  // is from before the call and still offers it. Disabling every button for
  // the length of each refresh is the cheaper of the two mistakes.
  const locked = state.sending !== undefined || state.reading
  const { copy } = useBrand()

  return (
    <section className="actions">
      {actions.length > 0 && (
        <>
          <div className="buttons">
            {actions.map((action) => (
              <button
                key={action}
                type="button"
                // Every one drawn alike. The first moves the whole pool along,
                // but a buyer whose seat the pass skipped needs the claim, and
                // a filled first button would rank the call they do not need.
                className="primary"
                disabled={locked}
                onClick={() => dispatch({ type: 'ACTION_REQUESTED', action })}
              >
                {copy.calls[action].button}
              </button>
            ))}
          </div>
          <p className="muted">{copy.callsNote}</p>
        </>
      )}
      <CallStatus state={state} />
    </section>
  )
}

function CallStatus({ state }: { state: State }) {
  const { sending } = state
  return (
    <>
      {sending !== undefined &&
        (sending.agreementId === state.agreementId ? (
          <CallHere sending={sending} />
        ) : (
          <CallElsewhere sending={sending} />
        ))}
      {state.actionError !== undefined && (
        // A refusal or a declined signature. Nothing is said about cost: a
        // transaction the chain refuses is never committed, and pays no fee.
        <p role="status">The call did not go through. ({state.actionError})</p>
      )}
      {/* Shown once. While a call is waiting for the chain its id is already
          in the line above, and a second copy under another label reads as a
          second transaction. */}
      {state.lastTxId !== undefined && state.lastTxId !== sending?.unconfirmed?.txId && (
        <p>
          Last transaction: <Hex>{state.lastTxId}</Hex>
        </p>
      )}
    </>
  )
}

function CallHere({ sending }: { sending: Sending }) {
  const { phrase } = useBrand().copy.calls[sending.action]
  if (sending.unconfirmed !== undefined) {
    // Submitted and not seen to confirm. It may still land, so this is worded
    // as waiting and never as a failure -- a buyer told it failed would send
    // it again while the first is still travelling.
    return (
      <p role="status">
        Sent, waiting for the chain: {phrase} was submitted and has not confirmed yet. It may
        still land, so no call can be sent until the chain shows whether it did -- at the
        latest a few minutes after it was signed, once it can no longer be committed.
        Transaction <Hex>{sending.unconfirmed.txId}</Hex>
      </p>
    )
  }
  return <p role="status">Sending {phrase}: approve it in your wallet, then wait for the chain.</p>
}

/**
 * Why every button here is disabled, when the call holding the lock was sent
 * from another pool's page -- with the way back to it.
 */
function CallElsewhere({ sending }: { sending: Sending }) {
  const { copy } = useBrand()
  const id = String(sending.agreementId)
  const where = <Link to={poolPath(sending.agreementId)}>{copy.callElsewhere(id)}</Link>
  return (
    <p role="status">
      Calls here wait until {copy.calls[sending.action].phrase} on {where} finishes.{' '}
      {sending.unconfirmed !== undefined
        ? 'It was sent and is waiting for the chain.'
        : 'It is still being sent.'}
    </p>
  )
}
