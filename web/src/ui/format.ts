/**
 * Turning chain values into text. Formatting only: nothing here decides what
 * a buyer may do.
 */

const MICRO_PER_UNIT = 1_000_000n

/**
 * USDC micro-units as a decimal string, without ever passing through a float.
 *
 * `Number(micro) / 1e6` reads correctly for an ordinary price and silently
 * drops digits past 2^53 micro-units -- which is where a garbled record or a
 * decoding bug would land. The one screen that shows this number is the one a
 * buyer checks before signing, so it has to show exactly what the chain holds,
 * including when what the chain holds is absurd.
 *
 * At least two decimals, so a price reads as money ("5.00"), and up to all
 * six, so no micro-unit is hidden ("0.000001").
 */
export function formatUsdc(micro: bigint): string {
  const whole = micro / MICRO_PER_UNIT
  let fraction = (micro % MICRO_PER_UNIT).toString().padStart(6, '0')
  while (fraction.length > 2 && fraction.endsWith('0')) fraction = fraction.slice(0, -1)
  return `${whole}.${fraction} USDC`
}

/** A Unix-seconds instant in the buyer's own time zone. */
export function formatInstant(seconds: bigint | number): string {
  return new Date(Number(seconds) * 1000).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

/**
 * A calendar date written `YYYY-MM-DD`, in the buyer's own words for it.
 *
 * Read as a day, not an instant: formatted in UTC, so no time zone moves a
 * trip's departure to the day before.
 */
export function formatDay(date: string): string {
  return new Date(`${date}T00:00:00Z`).toLocaleDateString(undefined, {
    dateStyle: 'medium',
    timeZone: 'UTC',
  })
}

/**
 * Roughly how long is left, or `null` once nothing is.
 *
 * Deliberately coarse. Both inputs are chain time from the last read, which is
 * up to one refresh old and trails the wall clock besides, so minutes are the
 * finest unit that means anything -- and a seconds counter would look precise
 * about a number that is not.
 */
export function formatRemaining(deadline: bigint, now: bigint): string | null {
  if (now >= deadline) return null
  const seconds = deadline - now
  const days = seconds / 86_400n
  const hours = (seconds % 86_400n) / 3_600n
  const minutes = (seconds % 3_600n) / 60n
  const unit = (count: bigint, name: string) => `${count} ${name}${count === 1n ? '' : 's'}`
  if (days > 0n) return `${unit(days, 'day')} ${unit(hours, 'hour')}`
  if (hours > 0n) return `${unit(hours, 'hour')} ${unit(minutes, 'minute')}`
  if (minutes > 0n) return unit(minutes, 'minute')
  return 'less than a minute'
}
