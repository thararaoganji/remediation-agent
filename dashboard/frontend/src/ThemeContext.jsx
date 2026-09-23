import { createContext, useContext, useEffect, useState } from 'react'

const ThemeContext = createContext(null)
const STORAGE_KEY = 'theme'

function readStoredTheme() {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    return value === 'light' || value === 'dark' ? value : null
  } catch {
    return null
  }
}

function systemPrefersDark() {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches
}

// Deliberately just toggles the `color-scheme` CSS property rather than
// introducing a parallel [data-theme] selector system -- every color in
// index.css already goes through light-dark(...), which resolves purely
// off the computed color-scheme value. Setting it inline on the root
// element overrides the stylesheet's `color-scheme: light dark` (which
// otherwise just follows the OS), so this is the entire theming
// mechanism -- no other CSS needed.
function applyTheme(theme) {
  document.documentElement.style.colorScheme = theme || ''
  if (theme) {
    document.documentElement.setAttribute('data-theme', theme)
  } else {
    document.documentElement.removeAttribute('data-theme')
  }
}

export function ThemeProvider({ children }) {
  // null = "follow system" (never explicitly chosen, or explicitly reset)
  const [theme, setThemeState] = useState(readStoredTheme)

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  function setTheme(next) {
    setThemeState(next)
    try {
      if (next) localStorage.setItem(STORAGE_KEY, next)
      else localStorage.removeItem(STORAGE_KEY)
    } catch {
      // Nothing to persist to -- the choice still applies for this visit.
    }
  }

  function toggleTheme() {
    const effective = theme || (systemPrefersDark() ? 'dark' : 'light')
    setTheme(effective === 'dark' ? 'light' : 'dark')
  }

  const effectiveTheme = theme || (systemPrefersDark() ? 'dark' : 'light')

  return (
    <ThemeContext.Provider value={{ theme, effectiveTheme, toggleTheme }}>{children}</ThemeContext.Provider>
  )
}

export function useTheme() {
  return useContext(ThemeContext)
}
