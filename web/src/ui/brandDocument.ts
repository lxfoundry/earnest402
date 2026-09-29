import type { Brand } from '../config'

/**
 * The page around the app, for a brand `index.html` was not written for.
 *
 * `index.html` is the index's: its title, its theme colours and its icon. A
 * travel build keeps the same file and changes those here, before React
 * renders, along with the `data-brand` that switches the design tokens. On the
 * index this does nothing at all.
 *
 * The two colours are the travel line's `--paper` in each scheme, as the
 * index's metas carry the index's -- see `tokens.css`.
 *
 * A travel build also writes the same values into `index.html` itself (see
 * `brandIndexHtml` in `vite.config.ts`, which keeps its own copy of them), so
 * its first paint is already the travel page's; this repeats them at run
 * time, where it changes nothing.
 */
export const TRAVEL_DOCUMENT = {
  title: 'Earnest Travel',
  theme: { light: '#ffffff', dark: '#121212' },
  /** Under the public directory, which is served under the base. */
  favicon: 'brand/travel/favicon.svg',
}

export function applyBrandToDocument(doc: Document, { brand }: Brand, base: string): void {
  if (brand !== 'travel') return
  doc.documentElement.dataset.brand = 'travel'
  doc.title = TRAVEL_DOCUMENT.title
  for (const meta of doc.querySelectorAll('meta[name="theme-color"]')) {
    const dark = (meta.getAttribute('media') ?? '').includes('dark')
    meta.setAttribute('content', dark ? TRAVEL_DOCUMENT.theme.dark : TRAVEL_DOCUMENT.theme.light)
  }
  doc.querySelector('link[rel="icon"]')?.setAttribute('href', `${base}${TRAVEL_DOCUMENT.favicon}`)
}
