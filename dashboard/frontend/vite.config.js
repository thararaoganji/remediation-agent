import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev-only proxy: `npm run dev` forwards /api/* to a locally running
// FastAPI backend (`uvicorn app.main:app` in dashboard/backend, default
// port 8000). In production (Phase D) the built dist/ is served BY that
// same FastAPI app, so /api/* is same-origin there and this proxy is a
// no-op -- no CORS configuration needed on the backend at all.
//
// VITE_API_PROXY_TARGET overrides the target -- unset for the native
// `npm run dev` workflow (localhost:8000 unchanged), set to
// http://backend:8000 by docker-compose.yml's `frontend` service, since
// "localhost" from inside that container would mean itself, not the
// separate `backend` container.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': process.env.VITE_API_PROXY_TARGET || 'http://localhost:8000',
    },
  },
  build: {
    outDir: 'dist',
  },
})
