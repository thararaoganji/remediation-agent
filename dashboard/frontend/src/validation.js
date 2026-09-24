// Mirrors the backend's own rules (dashboard/backend/app/validation.py) so
// a mistake gets caught before a round trip to the API, not just after --
// the backend stays the actual source of truth, this is purely UX.

export function isNonEmpty(value) {
  return typeof value === 'string' && value.trim().length > 0
}

export function isHttpUrl(value) {
  return /^https?:\/\/.+/i.test(value.trim())
}

export function isValidEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim())
}

export const MIN_PASSWORD_LENGTH = 8
