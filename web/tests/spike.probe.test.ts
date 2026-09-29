import algosdk from 'algosdk'
import { describe, expect, it } from 'vitest'
import { buildJoinGroup } from '../src/payment/buildJoinGroup'
import {
  UNSETTLEABLE_AGREEMENT_ID,
  analyse,
  classifyShape,
  ed25519Available,
  normalise,
} from '../src/spike/probe'

/**
 * The spike harness has to be trustworthy before its report means anything:
 * a probe that reports "bytes unchanged" for a mutated transaction would turn
 * a failed spike into a false verdict. So the analysis is exercised here
 * against groups signed locally, where the right answer is known.
 */

const PARAMS: algosdk.SuggestedParams = {
  fee: 0,
  minFee: 1000,
  firstValid: 1000,
  lastValid: 2000,
  genesisID: 'testnet-v1.0',
  genesisHash: algosdk.base64ToBytes('SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI='),
}

const NOTES = {
  feePayer: new TextEncoder().encode('x402-fee-payer-1'),
  payment: new TextEncoder().encode('x402-payment-1'),
}

function group(buyer: string, feePayer: string) {
  return buildJoinGroup({
    suggestedParams: PARAMS,
    buyer,
    feePayer,
    appAccount: algosdk.getApplicationAddress(771795120n).toString(),
    appId: 771795120n,
    assetId: 10458941n,
    amount: 5000000n,
    agreementId: UNSETTLEABLE_AGREEMENT_ID,
    notes: NOTES,
  })
}

function signedFixture() {
  const buyer = algosdk.generateAccount()
  const sponsor = algosdk.generateAccount()
  const { txns, indexesToSign } = group(buyer.addr.toString(), sponsor.addr.toString())
  const unsigned = txns.map((txn) => algosdk.encodeUnsignedTransaction(txn))
  const aligned: (Uint8Array | null)[] = txns.map((txn, i) =>
    indexesToSign.includes(i) ? algosdk.signTransaction(txn, buyer.sk).blob : null,
  )
  return { buyer, unsigned, aligned, indexesToSign }
}

describe('classifyShape', () => {
  it('reads one slot per transaction as aligned', () => {
    expect(classifyShape(3, 3, 2)).toBe('aligned')
  })

  it('reads one slot per requested index as compacted', () => {
    expect(classifyShape(2, 3, 2)).toBe('compacted')
  })

  it('refuses to guess at anything else', () => {
    expect(classifyShape(1, 3, 2)).toBe('unrecognised')
  })

  it('prefers aligned when a group is entirely signed by the buyer', () => {
    // Both branches match at 2-of-2. Aligned is the one that needs no
    // scattering, so it must win.
    expect(classifyShape(2, 2, 2)).toBe('aligned')
  })
})

describe('normalise', () => {
  it('scatters a compacted return onto absolute indexes', () => {
    const a = Uint8Array.from([1])
    const b = Uint8Array.from([2])
    expect(normalise([a, b], 'compacted', 3, [1, 2])).toEqual([null, a, b])
  })

  it('passes an aligned return through untouched', () => {
    const raw = [null, Uint8Array.from([1]), Uint8Array.from([2])]
    expect(normalise(raw, 'aligned', 3, [1, 2])).toBe(raw)
  })

  it('throws rather than guessing on an unrecognised shape', () => {
    expect(() => normalise([Uint8Array.from([1])], 'unrecognised', 3, [1, 2])).toThrow(
      /neither known adapter shape/,
    )
  })
})

describe('analyse', () => {
  it('passes a correctly signed aligned group, and verifies the signatures', async () => {
    const { buyer, unsigned, aligned, indexesToSign } = signedFixture()
    const canVerify = await ed25519Available()

    const report = await analyse({
      unsigned,
      raw: aligned,
      indexesToSign,
      signerAddress: buyer.addr.toString(),
      canVerify,
    })

    expect(report.shape).toBe('aligned')
    expect(report.pass).toBe(true)
    expect(report.legs.map((leg) => leg.signed)).toEqual([false, true, true])
    expect(report.legs.map((leg) => leg.bytesUnchanged)).toEqual([null, true, true])
    if (canVerify) {
      expect(report.legs.map((leg) => leg.signatureValid)).toEqual([null, true, true])
    }
  })

  it('passes a compacted return too, which is the other adapter shape', async () => {
    const { buyer, unsigned, aligned, indexesToSign } = signedFixture()
    const compacted = indexesToSign.map((i) => aligned[i] ?? null)

    const report = await analyse({
      unsigned,
      raw: compacted,
      indexesToSign,
      signerAddress: buyer.addr.toString(),
      canVerify: await ed25519Available(),
    })

    expect(report.shape).toBe('compacted')
    expect(report.pass).toBe(true)
  })

  it('catches a wallet that changed the transaction it signed', async () => {
    const { buyer, unsigned, aligned, indexesToSign } = signedFixture()
    // A different fee on the same leg: still a validly signed transaction,
    // and no longer the one we handed over. This is the failure the byte
    // comparison exists to catch, because nothing else would see it.
    const sponsor = algosdk.generateAccount()
    const tampered = group(buyer.addr.toString(), sponsor.addr.toString())
    tampered.txns[2]!.fee = 9999n
    const swapped = [...aligned]
    swapped[2] = algosdk.signTransaction(tampered.txns[2]!, buyer.sk).blob

    const report = await analyse({
      unsigned,
      raw: swapped,
      indexesToSign,
      signerAddress: buyer.addr.toString(),
      canVerify: await ed25519Available(),
    })

    expect(report.legs[2]?.bytesUnchanged).toBe(false)
    expect(report.pass).toBe(false)
  })

  it('fails when a leg the buyer owns came back unsigned', async () => {
    const { buyer, unsigned, aligned, indexesToSign } = signedFixture()
    const withHole = [...aligned]
    withHole[2] = null

    const report = await analyse({
      unsigned,
      raw: withHole,
      indexesToSign,
      signerAddress: buyer.addr.toString(),
      canVerify: await ed25519Available(),
    })

    expect(report.legs[2]?.signed).toBe(false)
    expect(report.pass).toBe(false)
  })

  it('fails, without throwing, on a shape it cannot place', async () => {
    const { buyer, unsigned, indexesToSign } = signedFixture()

    const report = await analyse({
      unsigned,
      raw: [Uint8Array.from([1])],
      indexesToSign,
      signerAddress: buyer.addr.toString(),
      canVerify: false,
    })

    expect(report.shape).toBe('unrecognised')
    expect(report.pass).toBe(false)
  })

  it('reports a signature made by some other account as invalid', async () => {
    if (!(await ed25519Available())) return
    const { buyer, unsigned, indexesToSign } = signedFixture()
    const impostor = algosdk.generateAccount()
    const { txns } = group(buyer.addr.toString(), impostor.addr.toString())
    const raw: (Uint8Array | null)[] = txns.map((txn, i) =>
      indexesToSign.includes(i) ? algosdk.signTransaction(txn, impostor.sk).blob : null,
    )

    const report = await analyse({
      unsigned,
      raw,
      indexesToSign,
      signerAddress: buyer.addr.toString(),
      canVerify: true,
    })

    expect(report.legs[1]?.signatureValid).toBe(false)
    expect(report.pass).toBe(false)
  })
})
