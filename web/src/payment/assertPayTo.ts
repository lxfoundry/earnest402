import algosdk from 'algosdk'

export class PayToMismatchError extends Error {}
export class FileTooLargeError extends Error {}

/**
 * One local derivation, no network call.
 *
 * The 402 carries `payTo`, the application *account*; the application *id* is
 * configuration, because deriving an id from an account is one-way. Checking
 * that they agree makes it structurally impossible to build a `join` call
 * against one application while paying another -- the failure mode of a
 * client left pointed at a superseded deployment, which would otherwise move
 * money into a contract that has never heard of this agreement.
 */
export function assertPayTo(payTo: string, appId: bigint): void {
  // getApplicationAddress returns an Address in algosdk 3.x. Comparing that
  // object to a string is always false, so the .toString() is load-bearing:
  // without it this assertion would reject every payment, not none.
  const derived = algosdk.getApplicationAddress(appId).toString()
  if (payTo !== derived) {
    throw new PayToMismatchError(
      `the 402 asks for payment to ${payTo}, but application ${appId} is ${derived}. ` +
        'Refusing to build a payment: this client is configured for a different deployment.',
    )
  }
}

/**
 * Refused before the unpaid pass, not after.
 *
 * The server bounds its exposure by file size, so a file over the limit
 * cannot complete. Quoting it anyway would commit an agreement on chain and
 * park its deposit against a job that is already lost.
 */
export function assertFileSize(size: number, maxFileBytes: number): void {
  if (size <= 0) {
    throw new FileTooLargeError('that file is empty, so there is nothing to pin.')
  }
  if (size > maxFileBytes) {
    const limit = Math.floor(maxFileBytes / 1024 / 1024)
    throw new FileTooLargeError(
      `that file is ${Math.ceil(size / 1024 / 1024)} MB and the limit is ${limit} MB. ` +
        'Nothing has been paid or committed.',
    )
  }
}
