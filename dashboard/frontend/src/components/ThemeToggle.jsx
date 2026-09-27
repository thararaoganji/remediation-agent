import { useTheme } from '../ThemeContext.jsx'
import { MoonIcon, SunIcon } from './icons.jsx'

export default function ThemeToggle() {
  const { effectiveTheme, toggleTheme } = useTheme()
  const isDark = effectiveTheme === 'dark'

  return (
    <button
      type="button"
      className="icon-action theme-toggle"
      onClick={toggleTheme}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
    >
      {isDark ? <SunIcon /> : <MoonIcon />}
    </button>
  )
}
