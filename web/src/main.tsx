import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { getBrand } from './config'
import { App } from './ui/App'
import { applyBrandToDocument } from './ui/brandDocument'
// Here and nowhere else, so no module a test imports pulls in a stylesheet.
import './ui/tokens.css'
import './ui/app.css'

// The title, theme colours and icon of a brand `index.html` was not written
// for; nothing on the index. A brand that cannot be read is left to `App`,
// which renders the configuration error naming it.
try {
  applyBrandToDocument(document, getBrand(), import.meta.env.BASE_URL)
} catch {
  // Reported by `App`.
}

const root = document.getElementById('root')
if (!root) throw new Error('#root is missing from index.html')
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
