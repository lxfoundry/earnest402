// @vitest-environment jsdom
//
// What `main.tsx` does to the page before React renders: nothing on the index,
// and on the travel line the title, the browser's theme colours and the icon
// that `index.html` cannot know. Checked against the real `index.html`, so a
// meta added there later is covered too.
import { describe, expect, it } from 'vitest'
import { applyBrandToDocument } from '../src/ui/brandDocument'
import INDEX_HTML from '../index.html?raw'

const BASE = '/app/'

const fresh = () => new DOMParser().parseFromString(INDEX_HTML, 'text/html')

const themeColors = (doc: Document) =>
  [...doc.querySelectorAll('meta[name="theme-color"]')].map((meta) => ({
    media: meta.getAttribute('media'),
    content: meta.getAttribute('content'),
  }))

describe('the index', () => {
  it('leaves the title, the theme colours, the icon and the root untouched', () => {
    const doc = fresh()
    const before = doc.documentElement.outerHTML
    applyBrandToDocument(doc, { brand: 'index' }, BASE)
    expect(doc.documentElement.outerHTML).toBe(before)
    expect(doc.title).toBe('Earnest')
    expect(themeColors(doc)).toEqual([
      { media: '(prefers-color-scheme: light)', content: '#f4efe6' },
      { media: '(prefers-color-scheme: dark)', content: '#15130f' },
    ])
    expect(doc.documentElement.hasAttribute('data-brand')).toBe(false)
  })
})

describe('the travel line', () => {
  it('marks the root, so the travel tokens apply', () => {
    const doc = fresh()
    applyBrandToDocument(doc, { brand: 'travel' }, BASE)
    expect(doc.documentElement.dataset.brand).toBe('travel')
  })

  it('titles the page Earnest Travel', () => {
    const doc = fresh()
    applyBrandToDocument(doc, { brand: 'travel' }, BASE)
    expect(doc.title).toBe('Earnest Travel')
  })

  it("sets both theme colours to the travel line's paper", () => {
    const doc = fresh()
    applyBrandToDocument(doc, { brand: 'travel' }, BASE)
    expect(themeColors(doc)).toEqual([
      { media: '(prefers-color-scheme: light)', content: '#ffffff' },
      { media: '(prefers-color-scheme: dark)', content: '#121212' },
    ])
  })

  it('points the icon at the travel favicon, under the base', () => {
    const doc = fresh()
    applyBrandToDocument(doc, { brand: 'travel' }, BASE)
    const icon = doc.querySelector('link[rel="icon"]')
    expect(icon?.getAttribute('href')).toBe('/app/brand/travel/favicon.svg')
    expect(icon?.getAttribute('type')).toBe('image/svg+xml')
  })
})
