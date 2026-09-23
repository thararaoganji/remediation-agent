import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api } from './api.js'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    // GET /auth/me is one of the two paths api.js's 401 handler leaves
    // alone -- an anonymous visitor's very first request is expected to
    // 401, not a stale session to bounce out of.
    api
      .getCurrentUser()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setLoading(false))
  }, [])

  const login = useCallback(async (email, password) => {
    const loggedInUser = await api.login(email, password)
    setUser(loggedInUser)
    return loggedInUser
  }, [])

  const logout = useCallback(async () => {
    await api.logout()
    setUser(null)
  }, [])

  const changePassword = useCallback(async (currentPassword, newPassword) => {
    const updatedUser = await api.changePassword(currentPassword, newPassword)
    setUser(updatedUser)
    return updatedUser
  }, [])

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, changePassword }}>{children}</AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
