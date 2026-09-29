import algosdk from 'algosdk'

/**
 * `sha256` of the chosen file, computed locally.
 *
 * This is the commitment the escrow agreement is created with, so it is
 * computed from the bytes themselves and never from anything the server said.
 * The file does not leave the page until after settlement.
 *
 * Uses `crypto.subtle.digest` over the whole blob rather than an incremental
 * implementation. The plan allowed either; the whole-buffer form wins because
 * this is the one value the entire release condition rests on, and a
 * hand-written compression function is a great deal of surface on which to be
 * subtly wrong. The memory cost is already bounded by the same maximum file
 * size that bounds the server -- which must also materialise the whole body
 * to pay the worker -- so nothing is given up by holding the file once.
 *
 * If that maximum is ever raised far enough for a single ArrayBuffer to be a
 * problem, the replacement is a streaming digest from a vetted library, not a
 * hand-rolled one.
 */
export async function sha256Hex(file: Blob): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
  return algosdk.bytesToHex(new Uint8Array(digest))
}
