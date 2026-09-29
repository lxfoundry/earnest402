// `defineConfig` comes from vitest/config, not vite: the `test` key below is
// Vitest's, and vite's own defineConfig does not type it.
import { defineConfig } from 'vitest/config'
import { loadEnv, type Connect, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

// Where the local resource server answers. `tests/serve_pool_spike.py` binds
// this port by default; override it for a server started some other way.
const PAID_ROUTE_TARGET = process.env.PAID_ROUTE_TARGET ?? 'http://127.0.0.1:8402'

// The application is served under /app by the Python application, so every
// asset URL must be built relative to that prefix rather than to the root.
const BASE = '/app/'

/**
 * Answer the base without its trailing slash with a redirect to the base.
 *
 * Vite's own base middleware redirects only `/` and `/index.html`; any other
 * path outside the base, the bare `/app` included, gets a 404 page suggesting
 * a URL that is itself wrong. A buyer typing the address has no reason to know
 * the slash matters. Installed directly in the server hooks rather than in a
 * returned post hook, so it runs ahead of Vite's internal middleware.
 */
export function redirectBareBase(base: string): Plugin {
  const bare = base.replace(/\/$/, '')
  const redirect: Connect.NextHandleFunction = (req, res, next) => {
    const url = req.url ?? ''
    const queryAt = url.indexOf('?')
    const pathname = queryAt === -1 ? url : url.slice(0, queryAt)
    if (pathname !== bare) return next()
    res.statusCode = 302
    res.setHeader('Location', base + (queryAt === -1 ? '' : url.slice(queryAt)))
    res.end()
  }
  return {
    name: 'earnest:redirect-bare-base',
    configureServer(server) {
      server.middlewares.use(redirect)
    },
    configurePreviewServer(server) {
      server.middlewares.use(redirect)
    },
  }
}

/**
 * The travel page's title, theme colours and icon, as `index.html` carries
 * them. The same values as `TRAVEL_DOCUMENT` in `src/ui/brandDocument.ts`,
 * which sets them again at run time; a copy rather than an import, because
 * this file imports nothing from `src/`, and `tests/vite.brand.test.ts` holds
 * the two equal.
 */
export const TRAVEL_INDEX_HTML = {
  title: 'Earnest Travel',
  theme: { light: '#ffffff', dark: '#121212' },
  favicon: 'brand/travel/favicon.svg',
}

/**
 * `index.html` as a brand's page: the index's untouched, and a travel build's
 * with the travel title, theme colours, icon and `data-brand` written in.
 *
 * At build time, because the brand is a build constant and the page is white
 * or warm paper from its first paint: left to `main.tsx`, a travel page would
 * show the index's colours, title and icon until the whole bundle had loaded.
 * Every tag is found or the build fails, so a change to `index.html` cannot
 * leave a travel page half-branded. The icon's path is left for the build to
 * put under the base, as it does the index's.
 */
export function brandIndexHtml(html: string, brand: string | undefined): string {
  if (brand !== 'travel') return html
  const { title, theme, favicon } = TRAVEL_INDEX_HTML
  const swaps: [string, RegExp, string][] = [
    ['root', /<html lang="en">/, '<html lang="en" data-brand="travel">'],
    ['title', /<title>Earnest<\/title>/, `<title>${title}</title>`],
    [
      'light theme colour',
      /(<meta name="theme-color" content=")#[0-9a-f]{6}(" media="\(prefers-color-scheme: light\)" \/>)/,
      `$1${theme.light}$2`,
    ],
    [
      'dark theme colour',
      /(<meta name="theme-color" content=")#[0-9a-f]{6}(" media="\(prefers-color-scheme: dark\)" \/>)/,
      `$1${theme.dark}$2`,
    ],
    ['icon', /(<link rel="icon" type="image\/svg\+xml" href=")\/favicon\.svg(" \/>)/, `$1/${favicon}$2`],
  ]
  return swaps.reduce((page, [what, pattern, replacement]) => {
    if (!pattern.test(page)) {
      throw new Error(`index.html: cannot find the ${what} tag to brand for travel`)
    }
    return page.replace(pattern, replacement)
  }, html)
}

function brandIndexHtmlPlugin(brand: string | undefined): Plugin {
  return {
    name: 'earnest:brand-index-html',
    transformIndexHtml: { order: 'pre', handler: (html) => brandIndexHtml(html, brand) },
  }
}

export default defineConfig(({ mode }) => ({
  plugins: [
    react(),
    redirectBareBase(BASE),
    // `.env` files as well as the process environment, as the client reads it.
    brandIndexHtmlPlugin(
      loadEnv(mode, fileURLToPath(new URL('.', import.meta.url)), 'VITE_').VITE_BRAND,
    ),
  ],
  base: BASE,
  build: { outDir: 'dist', emptyOutDir: true },
  // In production the client and the paid route share an origin, served
  // under /app by the Python application, so no CORS is involved. In dev
  // they do not, and the server sets no CORS headers at all -- a POST
  // carrying PAYMENT-SIGNATURE is not a simple request, so it preflights,
  // and the preflight is answered 405. Even a success would be unreadable,
  // because `fetch` cannot see PAYMENT-REQUIRED without expose_headers.
  //
  // So the dev server reproduces the production arrangement instead of the
  // server relaxing for it: point VITE_RESOURCE_HOST at this origin, and
  // these rules forward to the local API. `api.ts` builds absolute URLs, and
  // absolute URLs to this same origin do match a proxy rule.
  server: {
    proxy: {
      '/index': PAID_ROUTE_TARGET,
      '/pin': PAID_ROUTE_TARGET,
    },
  },
  test: {
    // Node, not jsdom, by default. The modules that carry the risk are pure
    // and need no DOM, and jsdom actively breaks them: its TextEncoder
    // returns a Uint8Array from a different realm, so algosdk's instanceof
    // checks reject it ("Not a Uint8Array"). Component tests opt in per file
    // with `// @vitest-environment jsdom`.
    environment: 'node',
    globals: true,
    include: ['tests/**/*.test.ts', 'tests/**/*.test.tsx'],
  },
}))
