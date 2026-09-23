import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../AuthContext.jsx'
import { usePageTitle } from '../usePageTitle.js'

export default function ResetPasswordPage() {
  usePageTitle('Reset Password')
  const { user, changePassword } = useAuth()
  const navigate = useNavigate()
  const forced = Boolean(user?.must_reset_password)

  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    if (newPassword !== confirmPassword) {
      setError("New password and confirmation don't match.")
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await changePassword(currentPassword, newPassword)
      navigate('/runs', { replace: true })
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="form-page">
      <h2>{forced ? 'Set a new password' : 'Change password'}</h2>
      {forced && (
        <p className="warning">
          Your account was just created with a temporary password — choose a new one before
          continuing.
        </p>
      )}
      <form className="new-run-form" onSubmit={handleSubmit}>
        <label>
          Current password
          <input
            type="password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            required
            autoFocus
          />
        </label>
        <label>
          New password
          <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} required />
        </label>
        <label>
          Confirm new password
          <input
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
          />
        </label>
        <button type="submit" disabled={submitting}>
          {submitting ? 'Saving…' : 'Save new password'}
        </button>
        {error && <p className="error">{error}</p>}
      </form>
    </div>
  )
}
