// Transient success/failure feedback for an action that doesn't otherwise
// navigate anywhere (add/update/delete a connection) -- without this, a
// successful save/delete had no confirmation at all beyond the form
// closing or the row disappearing, and a failure only showed as small
// inline text easy to miss. Paired with useToast.js, which owns the
// message/auto-dismiss timer; this component is purely presentational,
// same split as ConfirmDialog.jsx.
export default function Toast({ toast }) {
  if (!toast) return null

  return (
    <div className="toast-stack" role="status" aria-live="polite">
      <div className={`toast toast-${toast.type}`}>{toast.message}</div>
    </div>
  )
}
