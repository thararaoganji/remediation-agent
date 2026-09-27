import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App.jsx'
import './index.css'

// No <React.StrictMode> -- its dev-mode double-invocation of effects was
// making every data-fetching page (New Run, Connections, Runs, ...) fire
// each of its GET requests twice on mount (confirmed via a real network
// trace: sonar-servers/github-credentials/llm-configs each showed up as a
// literal duplicate call). Harmless with this session's fast in-memory
// test backend, but a real, measurable source of perceived page-load lag
// once real Firestore/Secret Manager latency is in the loop. This only
// ever affected development -- React skips the double-invoke in a
// production build regardless of StrictMode, so removing it changes
// nothing about production behavior, only how many requests fire in `npm
// run dev`.
ReactDOM.createRoot(document.getElementById('root')).render(
  <BrowserRouter>
    <App />
  </BrowserRouter>,
)
