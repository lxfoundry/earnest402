import { describe, expect, it, vi } from 'vitest'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { redirectBareBase } from '../vite.config'

// Vite's own base middleware redirects only `/` and `/index.html` to the base,
// and answers every other path outside it -- the bare `/app` included -- with a
// 404 page. The plugin under test puts a redirect in front of it, on both the
// dev and the preview server.

type Middleware = (req: IncomingMessage, res: ServerResponse, next: () => void) => void

/** The middleware a server hook installs, captured without starting a server. */
function installedBy(hook: 'configureServer' | 'configurePreviewServer'): Middleware {
  const installed: Middleware[] = []
  const server = { middlewares: { use: (fn: Middleware) => installed.push(fn) } }
  const plugin = redirectBareBase('/app/')
  ;(plugin[hook] as (server: unknown) => void)(server)
  expect(installed).toHaveLength(1)
  return installed[0]!
}

function request(middleware: Middleware, url: string) {
  const res = { statusCode: 200, setHeader: vi.fn(), end: vi.fn() }
  const next = vi.fn()
  middleware({ url } as IncomingMessage, res as unknown as ServerResponse, next)
  return { res, next }
}

describe.each(['configureServer', 'configurePreviewServer'] as const)('%s', (hook) => {
  it('redirects the bare base to the base, keeping the query', () => {
    for (const [url, location] of [
      ['/app', '/app/'],
      ['/app?agreementId=25', '/app/?agreementId=25'],
    ] as const) {
      const { res, next } = request(installedBy(hook), url)
      expect(res.statusCode).toBe(302)
      expect(res.setHeader).toHaveBeenCalledWith('Location', location)
      expect(res.end).toHaveBeenCalled()
      expect(next).not.toHaveBeenCalled()
    }
  })

  it.each(['/app/', '/app/pool/25', '/apps', '/application', '/index', '/'])(
    'passes %s through untouched',
    (url) => {
      const { res, next } = request(installedBy(hook), url)
      expect(next).toHaveBeenCalled()
      expect(res.end).not.toHaveBeenCalled()
    },
  )
})
