import { Suspense, lazy, useLayoutEffect } from 'react'
import { ConfigError, checkBrandNetwork, getBrand, getConfig, type Brand } from '../config'
import { usePoolMachine, type PoolMachine } from '../pool/usePoolMachine'
import { ENTRY_PATH, useRoute, type Route } from '../router'
import { ConnectButton, WalletProvider } from '../wallet'
import { BrandContext, useBrand } from './BrandContext'
import { EntryScreen } from './EntryScreen'
import { Link } from './Link'
import { PoolScreen } from './PoolScreen'
import { TestnetFunds } from './TestnetFunds'

/**
 * The browser-wallet spike harness, in development only.
 *
 * A dynamic import inside an `import.meta.env.DEV` branch rather than a static
 * import. `DEV` is replaced by a literal at build time, so in a production
 * build this whole expression is `false ? ... : null`, and the import -- the
 * only reference to the spike module -- is removed with the branch that held
 * it. A static import is not enough for that: the bundler drops the unused
 * component but keeps every top-level statement of the module, and the
 * spike's module scope builds an algod client, which reads the configuration
 * the moment the bundle loads. That is a configuration error thrown before
 * `App` can catch it, which is a blank page.
 */
const SpikePage = import.meta.env.DEV
  ? lazy(() => import('../spike/SpikePage').then((module) => ({ default: module.SpikePage })))
  : null

export function App() {
  // Checked before anything else renders. Every read, the wallet manager and
  // the quote all need the configuration, and each would otherwise throw on
  // first use somewhere inside the tree -- which React answers by unmounting
  // everything and leaving an empty page with the reason only in the console.
  //
  // The brand too: a build given a brand or an offer it cannot read says so
  // here, rather than showing a trip it made up -- and so does a travel build
  // made for a network its demo notice would misdescribe.
  let brand: Brand
  try {
    const config = getConfig()
    brand = getBrand()
    checkBrandNetwork(brand, config)
  } catch (error) {
    if (error instanceof ConfigError) return <ConfigurationError message={error.message} />
    throw error
  }
  return (
    <BrandContext value={brand}>
      <WalletProvider>
        <Client />
      </WalletProvider>
    </BrandContext>
  )
}

/**
 * The router and the machine, together.
 *
 * One machine for the whole client, mounted above every route rather than
 * inside the pool screen. It holds two facts that outlive any screen -- a
 * payment still awaiting its seat, and a refund call still travelling -- and a
 * machine mounted per screen would forget both the moment the buyer navigated.
 */
function Client() {
  const { route } = useRoute()
  const machine = usePoolMachine()
  const { dispatch } = machine

  // A layout effect, so the machine learns which pool is on screen before the
  // browser paints. Otherwise the first frame after the back button shows the
  // pool just left under the address of the pool just opened.
  //
  // Keyed on the parsed route, which is memoised on the pathname: the back
  // button re-dispatches only when it actually changed the address.
  useLayoutEffect(() => {
    if (route.kind === 'entry') dispatch({ type: 'OPENED_ENTRY' })
    else if (route.kind === 'pool') {
      dispatch({ type: 'OPENED_POOL', agreementId: route.agreementId })
    }
  }, [route, dispatch])

  const { copy } = useBrand()
  return (
    <div className="site">
      <header className="site-header">
        <div className="masthead">
          <Link to={ENTRY_PATH} className="brand">
            <Logo />
          </Link>
          {copy.brand === 'travel' && <span className="badge">{copy.badge}</span>}
        </div>
        <ConnectButton />
      </header>
      <main>
        <Screen route={route} machine={machine} />
      </main>
      <TestnetFunds />
      <footer className="site-footer">
        <a className="source-link" href={REPO_URL} target="_blank" rel="noopener noreferrer">
          <GitHubMark />
          {copy.shell.source}
        </a>
      </footer>
    </div>
  )
}

/**
 * The escrow contract that holds the money, and the specification it was built
 * from.
 *
 * A module constant rather than configuration: `config.ts` owns the network
 * parameters, and this URL is the same on every network.
 */
const REPO_URL = 'https://github.com/lxfoundry/earnest402'

/**
 * The GitHub mark, inline rather than a file in `public/`.
 *
 * Inline is the only way it follows `--ink` in both colour schemes: the
 * lockup next to it needs two files precisely because its colours are baked
 * into the artwork. `aria-hidden`, because the link already carries the name
 * -- naming the icon too would read it out twice.
 */
function GitHubMark() {
  return (
    <svg viewBox="0 0 16 16" width={16} height={16} fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.012 8.012 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  )
}

/**
 * The lockup, drawn for whichever scheme the OS asks for.
 *
 * Two files rather than one SVG styled by the page: the wordmark is outlines
 * with its colours baked in, so the dark file is the only way to get a light
 * ink. `BASE_URL`, because the public directory is served under the base.
 *
 * The travel line's wordmark is set as text instead, in the page's own type
 * and colours: a name, not a mark.
 */
function Logo() {
  const { copy } = useBrand()
  if (copy.brand === 'travel') {
    return (
      <span className="wordmark">
        {copy.wordmark.name} <em>{copy.wordmark.line}</em>
      </span>
    )
  }
  const base = import.meta.env.BASE_URL
  return (
    <picture>
      <source srcSet={`${base}logo-dark.svg`} media="(prefers-color-scheme: dark)" />
      <img src={`${base}logo.svg`} alt={copy.shell.home} width={260} height={56} />
    </picture>
  )
}

function Screen({ route, machine }: { route: Route; machine: PoolMachine }) {
  switch (route.kind) {
    case 'entry':
      return <EntryScreen state={machine.state} dispatch={machine.dispatch} />
    case 'pool':
      return <PoolScreen state={machine.state} dispatch={machine.dispatch} />
    case 'spike':
      if (!SpikePage) return <NotFound />
      return (
        <Suspense fallback={<p className="muted">Loading the spike harness.</p>}>
          <SpikePage />
        </Suspense>
      )
    case 'not-found':
      return <NotFound />
    default:
      return exhaustive(route)
  }
}

function exhaustive(_route: never): null {
  return null
}

function NotFound() {
  const { notFound } = useBrand().copy.shell
  return (
    <>
      <h1>{notFound.heading}</h1>
      <p>
        <Link to={ENTRY_PATH}>{notFound.link}</Link>
      </p>
    </>
  )
}

/**
 * A build that cannot run, said plainly.
 *
 * The configuration is compiled into the bundle, so nothing a buyer does can
 * fix this; the message names the key so whoever deployed the build can.
 *
 * In the same words whatever the brand, since the brand may be what could not
 * be read.
 */
export function ConfigurationError({ message }: { message: string }) {
  return (
    <main className="site">
      <h1>Configuration error</h1>
      <p>This build of the client is not configured correctly, so it cannot run.</p>
      <p>{message}</p>
    </main>
  )
}
