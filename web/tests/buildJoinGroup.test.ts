import { describe, expect, it } from 'vitest'
import algosdk from 'algosdk'
import { buildJoinGroup, escrowBoxNames } from '../src/payment/buildJoinGroup'
import golden from './golden/join-group.json'

const b64 = (bytes: Uint8Array) => Buffer.from(bytes).toString('base64')

describe('buildJoinGroup against the Python builder', () => {
  // The test that makes two implementations of one wire format safe. If this
  // fails, do not "fix" it by regenerating the vectors -- read the diff, and
  // work out which of the two builders is wrong.
  for (const vector of golden.vectors) {
    it(`encodes the ${vector.name} vector byte for byte`, () => {
      const input = vector.input
      const group = buildJoinGroup({
        suggestedParams: {
          fee: 0,
          flatFee: true,
          firstValid: input.suggestedParams.firstValid,
          lastValid: input.suggestedParams.lastValid,
          genesisHash: Uint8Array.from(
            Buffer.from(input.suggestedParams.genesisHash, 'base64'),
          ),
          genesisID: input.suggestedParams.genesisId,
          minFee: input.suggestedParams.minFee,
        },
        buyer: input.buyer,
        feePayer: input.feePayer,
        appAccount: input.appAccount,
        appId: BigInt(input.appId),
        assetId: BigInt(input.assetId),
        amount: BigInt(input.amount),
        agreementId: BigInt(input.agreementId),
        notes: {
          feePayer: new TextEncoder().encode(input.notes.feePayer),
          payment: new TextEncoder().encode(input.notes.payment),
        },
      })

      const encoded = group.txns.map((txn) =>
        b64(algosdk.encodeUnsignedTransaction(txn)),
      )
      expect(encoded).toEqual(vector.expected.unsigned)
      expect(group.paymentIndex).toBe(vector.expected.paymentIndex)
      expect(group.indexesToSign).toEqual(vector.expected.indexesToSign)
    })
  }

  it('covers every committed vector', () => {
    // A vector added on the Python side and never read here would be a test
    // that silently does nothing.
    expect(golden.vectors.length).toBeGreaterThanOrEqual(3)
  })
})

describe('the group shape', () => {
  const params = () => ({
    suggestedParams: {
      fee: 0,
      flatFee: true,
      firstValid: 1000,
      lastValid: 2000,
      genesisHash: new Uint8Array(32),
      genesisID: 'testnet-v1.0',
      minFee: 1000,
    },
    buyer: 'KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M',
    feePayer: 'IUHCGQZG5KF2T5BMWXZFCH64T6ZAEU5NHTEWRHMG2644WK55PZ7Z34FAKM',
    appAccount: 'IUHCGQZG5KF2T5BMWXZFCH64T6ZAEU5NHTEWRHMG2644WK55PZ7Z34FAKM',
    appId: 741000001n,
    assetId: 10458941n,
    amount: 10000n,
    agreementId: 42n,
    notes: {
      feePayer: new TextEncoder().encode('fp'),
      payment: new TextEncoder().encode('pay'),
    },
  })

  it('orders the legs feePayer, axfer, appCall', () => {
    const { txns } = buildJoinGroup(params())
    expect(txns.map((t) => t.type)).toEqual(['pay', 'axfer', 'appl'])
  })

  it('pools three minimum fees onto the fee payer and zeroes the rest', () => {
    const { txns } = buildJoinGroup(params())
    expect(txns[0]!.fee).toBe(3000n)
    expect(txns[1]!.fee).toBe(0n)
    expect(txns[2]!.fee).toBe(0n)
  })

  it('reads the minimum fee rather than assuming it', () => {
    const raised = params()
    raised.suggestedParams.minFee = 4000
    expect(buildJoinGroup(raised).txns[0]!.fee).toBe(12000n)
  })

  it('assigns one group id to all three legs', () => {
    const { txns } = buildJoinGroup(params())
    const ids = new Set(txns.map((t) => b64(t.group!)))
    expect(ids.size).toBe(1)
    expect(txns[0]!.group).toBeDefined()
  })

  it('names both the agreement box and the roster box', () => {
    const boxes = escrowBoxNames(42n)
    expect(boxes).toHaveLength(2)
    expect(new TextDecoder().decode(boxes[0]!.name.slice(0, 1))).toBe('a')
    expect(new TextDecoder().decode(boxes[1]!.name.slice(0, 1))).toBe('r')
    expect(boxes[0]!.name.slice(1)).toEqual(
      new Uint8Array([0, 0, 0, 0, 0, 0, 0, 42]),
    )
  })

  it('signs the buyer legs and leaves the fee payer unsigned', () => {
    expect(buildJoinGroup(params()).indexesToSign).toEqual([1, 2])
  })
})
