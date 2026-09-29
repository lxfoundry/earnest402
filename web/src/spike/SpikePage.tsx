import algosdk from 'algosdk'
import { useState } from 'react'
import { useWallet } from '@txnlab/use-wallet-react'
import { requestSeatQuote, settleSeat, type QuotedRoute, type SeatPurchase } from '../api'
import { getConfig } from '../config'
import { buildJoinGroup } from '../payment/buildJoinGroup'
import { createAlgod, suggestedParams as guardedParams } from '../chain/read'
import { buildPayment, withBuyer } from '../payment/schemeClient'
import { ConnectButton, useSigner } from '../wallet'
import {
  UNSETTLEABLE_AGREEMENT_ID,
  analyse,
  ed25519Available,
  type ProbeReport,
} from './probe'

/**
 * The stage-1 harness for the browser-wallet spike. Dev-only, mounted from
 * `ui/App.tsx` behind `import.meta.env.DEV`.
 *
 * It builds a real join group with the real builder, asks the connected
 * wallet to sign the buyer's two legs, and reports what came back. It submits
 * nothing and talks to no server: the only network call is one read of
 * suggested params.
 *
 * Deliberately calls `signTransactions` from `useWallet()` rather than
 * `useSigner()`. `useSigner` wraps the adapter in `toSigner`, which normalises
 * the two known return shapes -- and which shape arrived is one of the three
 * things this page exists to record.
 */

/**
 * The facilitator's fee sponsor for `algorand:SGO1…`, as `GET /supported`
 * reports it. Editable because a rotation is possible and surfaces downstream
 * as `fee_payer_not_managed_by_facilitator`; typed in rather than fetched
 * because the facilitator sets no CORS headers, so a browser cannot read
 * `/supported` directly.
 */
const DEFAULT_FEE_PAYER = 'ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA'

/**
 * The share price of the pool the spike is pointed at, not the production
 * $5.00. A wallet that simulates a group before displaying it would balk at
 * an amount the account cannot cover, and a balk is indistinguishable from a
 * wallet refusing the group's *shape* -- which is the one thing stage 1 is
 * trying to measure.
 */
const DEFAULT_AMOUNT_MICRO = '100000'

function notesFor(stamp: bigint) {
  const encoder = new TextEncoder()
  return {
    feePayer: encoder.encode(`x402-fee-payer-${stamp}`),
    payment: encoder.encode(`x402-payment-${stamp}`),
  }
}

/** One client for the page, so the network is checked once rather than per call. */
const algod = createAlgod()

async function suggestedParams(): Promise<algosdk.SuggestedParams> {
  // The decision this spike declined to take has since been taken: the client
  // reads the chain itself, so the algod URL is ordinary configuration --
  // and the params come back through `chain/read`, which checks that the node
  // speaks for the chain this bundle was built for before anything is signed.
  return await guardedParams(algod)
}

export function SpikePage() {
  const { signTransactions, activeAddress } = useWallet()
  const signer = useSigner()
  const [agreementId, setAgreementId] = useState(String(UNSETTLEABLE_AGREEMENT_ID))
  const [feePayer, setFeePayer] = useState(DEFAULT_FEE_PAYER)
  const [amount, setAmount] = useState(DEFAULT_AMOUNT_MICRO)
  const [report, setReport] = useState<ProbeReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [quoted, setQuoted] = useState<QuotedRoute | null>(null)
  const [purchase, setPurchase] = useState<SeatPurchase | null>(null)
  const [stage2Error, setStage2Error] = useState<string | null>(null)

  const settleable = agreementId !== String(UNSETTLEABLE_AGREEMENT_ID)

  async function run() {
    setBusy(true)
    setError(null)
    setReport(null)
    try {
      if (!activeAddress) throw new Error('connect a wallet first')
      const config = getConfig()
      const params = await suggestedParams()

      const { txns, indexesToSign } = buildJoinGroup({
        suggestedParams: params,
        buyer: activeAddress,
        feePayer,
        appAccount: algosdk.getApplicationAddress(config.appId).toString(),
        appId: config.appId,
        assetId: config.assetId,
        amount: BigInt(amount),
        agreementId: BigInt(agreementId),
        notes: notesFor(BigInt(Date.now()) * 1_000_000n),
      })

      const unsigned = txns.map((txn) => algosdk.encodeUnsignedTransaction(txn))
      // The raw adapter return, before any normalisation. The declared type
      // is `(Uint8Array | null)[]` for every adapter, which is exactly the
      // claim `wallet.tsx` records as false of KMD -- so the length is read
      // rather than trusted.
      const raw = await signTransactions(unsigned, indexesToSign)

      setReport(
        await analyse({
          unsigned,
          raw,
          indexesToSign,
          signerAddress: activeAddress,
          canVerify: await ed25519Available(),
        }),
      )
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  /**
   * Stage 2: the real thing. Quote a seat from the open pool, build and sign
   * through the product's own path, and settle through the real facilitator.
   *
   * Uses `useSigner()` rather than the raw adapter, because this is the path
   * a buyer would take -- `toSigner`'s normalisation included.
   */
  async function quoteSeat() {
    setBusy(true)
    setStage2Error(null)
    setPurchase(null)
    try {
      setQuoted(await requestSeatQuote())
    } catch (caught) {
      setQuoted(null)
      setStage2Error(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  async function buySeat() {
    setBusy(true)
    setStage2Error(null)
    try {
      if (!quoted) throw new Error('quote a seat first')
      if (!signer || !activeAddress) throw new Error('connect a wallet first')
      const payload = await buildPayment(
        withBuyer(quoted.quote, activeAddress),
        signer,
        await suggestedParams(),
      )
      setPurchase(await settleSeat(quoted, payload))
    } catch (caught) {
      setStage2Error(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <main>
      <h1>Browser-wallet signature spike — stage 1</h1>
      <p>
        Builds a real join group, asks the wallet to sign legs 1 and 2, and compares what comes
        back against what was sent. <strong>Submits nothing.</strong>
      </p>

      <ConnectButton />

      <fieldset>
        <legend>Group</legend>
        <label>
          Agreement id{' '}
          <input value={agreementId} onChange={(e) => setAgreementId(e.target.value)} size={24} />
        </label>
        <label>
          Amount (micro) <input value={amount} onChange={(e) => setAmount(e.target.value)} />
        </label>
        <label>
          Fee payer{' '}
          <input value={feePayer} onChange={(e) => setFeePayer(e.target.value)} size={60} />
        </label>
      </fieldset>

      {settleable && (
        <p>
          <strong>Warning:</strong> this agreement id may exist, which makes the signed group
          submittable. Use it only to tell &ldquo;the wallet refuses this shape&rdquo; apart from
          &ldquo;the wallet refuses a group it predicts will fail&rdquo;.
        </p>
      )}

      <button type="button" disabled={busy || !activeAddress} onClick={() => void run()}>
        {busy ? 'Waiting for the wallet…' : 'Build and sign'}
      </button>

      {error && (
        <section>
          <h2>Failed</h2>
          <pre>{error}</pre>
        </section>
      )}

      {report && (
        <section>
          <h2>{report.pass ? 'PASS' : 'FAIL'}</h2>
          <p>
            Shape: <strong>{report.shape}</strong> ({report.rawLength} returned for a group of{' '}
            {report.groupLength}, {report.indexesToSign.length} asked for). Ed25519 check{' '}
            {report.ed25519Available ? 'ran' : 'unavailable in this browser'}.
          </p>
          <table>
            <thead>
              <tr>
                <th>Leg</th>
                <th>Asked</th>
                <th>Signed</th>
                <th>Bytes unchanged</th>
                <th>Signature valid</th>
              </tr>
            </thead>
            <tbody>
              {report.legs.map((leg) => (
                <tr key={leg.index}>
                  <td>{leg.index}</td>
                  <td>{leg.wasAskedToSign ? 'yes' : 'no'}</td>
                  <td>{leg.signed ? 'yes' : 'no'}</td>
                  <td>{leg.bytesUnchanged === null ? '—' : String(leg.bytesUnchanged)}</td>
                  <td>{leg.signatureValid === null ? '—' : String(leg.signatureValid)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>For the record</h3>
          <pre>{JSON.stringify(report, null, 2)}</pre>
        </section>
      )}

      <hr />

      <h1>Stage 2 — buy a real seat</h1>
      <p>
        The product path, end to end: quote the open pool, sign through{' '}
        <code>useSigner</code>, settle through the real facilitator.{' '}
        <strong>This spends real TestNet USDC.</strong>
      </p>

      <button type="button" disabled={busy} onClick={() => void quoteSeat()}>
        Quote a seat
      </button>{' '}
      <button
        type="button"
        disabled={busy || !quoted || !activeAddress}
        onClick={() => void buySeat()}
      >
        {busy ? 'Working…' : 'Buy the seat'}
      </button>

      {stage2Error && (
        <section>
          <h2>Failed</h2>
          <pre>{stage2Error}</pre>
        </section>
      )}

      {quoted && (
        <section>
          <h2>Quoted</h2>
          <pre>
            {JSON.stringify(
              {
                agreementId: String(quoted.quote.agreementId),
                amount: String(quoted.quote.amount),
                payTo: quoted.quote.payTo,
                feePayer: quoted.quote.feePayer,
                extra: quoted.accepted.extra,
              },
              null,
              2,
            )}
          </pre>
        </section>
      )}

      {purchase && (
        <section>
          <h2>Settled</h2>
          <pre>
            {JSON.stringify(
              {
                receipt: {
                  ...purchase.receipt,
                  agreementId: String(purchase.receipt.agreementId),
                },
                settlement: purchase.settlement,
              },
              null,
              2,
            )}
          </pre>
        </section>
      )}
    </main>
  )
}
