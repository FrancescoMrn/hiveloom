import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './app'
// Phosphor is the cloud's icon set; same package, same 'ph ph-*' class names.
import '@phosphor-icons/web/regular'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
