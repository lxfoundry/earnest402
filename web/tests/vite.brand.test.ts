import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { TRAVEL_DOCUMENT } from '../src/ui/brandDocument'
import { TRAVEL_INDEX_HTML, brandIndexHtml } from '../vite.config'

// A travel build's page is white from its first paint, titled for the travel
// line, with the travel icon, before any script has run: the build writes it
// into index.html, rather than leaving it all to main.tsx once the bundle has
// loaded. The index's page is untouched.

const INDEX_HTML = readFileSync(new URL('../index.html', import.meta.url), 'utf8')

describe('brandIndexHtml', () => {
  it('leaves the index page as it is', () => {
    expect(brandIndexHtml(INDEX_HTML, 'index')).toBe(INDEX_HTML)
    expect(brandIndexHtml(INDEX_HTML, undefined)).toBe(INDEX_HTML)
    expect(brandIndexHtml(INDEX_HTML, '')).toBe(INDEX_HTML)
  })

  it('marks the root, retitles the page, and recolours both theme metas for travel', () => {
    const html = brandIndexHtml(INDEX_HTML, 'travel')
    expect(html).toContain('<html lang="en" data-brand="travel">')
    expect(html).toContain('<title>Earnest Travel</title>')
    expect(html).toContain(
      '<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)" />',
    )
    expect(html).toContain(
      '<meta name="theme-color" content="#121212" media="(prefers-color-scheme: dark)" />',
    )
    expect(html).not.toContain('#f4efe6')
    expect(html).not.toContain('#15130f')
  })

  it('points the icon at the travel favicon, left for the build to put under the base', () => {
    const html = brandIndexHtml(INDEX_HTML, 'travel')
    expect(html).toContain('<link rel="icon" type="image/svg+xml" href="/brand/travel/favicon.svg" />')
    expect(html).not.toContain('href="/favicon.svg"')
  })

  it('refuses a page it cannot find its tags in, rather than half-branding it', () => {
    const drifted = INDEX_HTML.replace('<title>Earnest</title>', '<title>Earnest client</title>')
    expect(() => brandIndexHtml(drifted, 'travel')).toThrow(/title/)
  })
})

describe('the two copies of the travel page values', () => {
  it('agree, so the first paint and the run-time pass set the same page', () => {
    expect(TRAVEL_INDEX_HTML).toEqual(TRAVEL_DOCUMENT)
  })
})
