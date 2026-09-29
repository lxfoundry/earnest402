import type { MouseEvent, ReactNode } from 'react'
import { navigate } from '../router'

/**
 * An in-app link: a real `href`, so it can be copied, bookmarked and opened in
 * a new tab, and a click that moves the router instead of reloading the page.
 *
 * A reload is not merely slower. It throws away the machine, and with it the
 * two things only the machine knows: that a payment is awaiting its seat, and
 * that a refund call is still travelling. The first is what stands between a
 * buyer who just paid and "you hold no seat here".
 */
export function Link({
  to,
  className,
  children,
}: {
  to: string
  className?: string
  children: ReactNode
}) {
  const onClick = (event: MouseEvent<HTMLAnchorElement>) => {
    // Left to the browser when the buyer asked for something other than a
    // plain click -- a new tab, a new window, a download -- since a new tab is
    // a fresh page either way and hijacking the click would deny it to them.
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return
    }
    event.preventDefault()
    navigate(to)
  }
  return (
    <a href={to} className={className} onClick={onClick}>
      {children}
    </a>
  )
}

/**
 * A hash or a transaction id, kept on the page.
 *
 * Sixty-four unbroken characters overflow a phone screen, and the part that
 * scrolls off is the part a buyer compares. The `hex` class breaks it anywhere
 * (see app.css).
 */
export function Hex({ children }: { children: string }) {
  return <code className="hex">{children}</code>
}
