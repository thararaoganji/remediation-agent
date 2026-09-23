import { useAuth } from '../AuthContext.jsx'
import { usePageTitle } from '../usePageTitle.js'

export default function HelpPage() {
  usePageTitle('Help')
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'

  return (
    <div className="help-page">
      <h2>Help</h2>
      <p className="section-note">
        What this dashboard does, and how to get a run going end to end.
      </p>

      <section>
        <h3>What this is</h3>
        <p>
          The dashboard triggers and watches three remediation agents against your SonarQube
          findings: <strong>Tech-Debt</strong> (Security / Reliability / Maintainability fixes),{' '}
          <strong>Coverage</strong> (generates JUnit tests for uncovered code), and{' '}
          <strong>Duplication</strong> (extracts shared logic out of duplicated blocks). Each run
          clones the target repo, fixes what it can, verifies the build still passes, and pushes a
          new branch with the changes.
        </p>
      </section>

      <section>
        <h3>1. Set up your connections first</h3>
        <p>Before starting a run, you need at least two things configured on the Connections page:</p>
        <ul>
          <li>
            <strong>A SonarQube Server</strong> — name, base URL, whether it's Community Edition,
            and an access token. This is where the agent reads open issues from.
          </li>
          <li>
            <strong>A GitHub Credential</strong> (optional, but required to actually push a fix
            branch) — a token with write access to the repo, plus an API base URL if you're on
            GitHub Enterprise Server rather than github.com.
          </li>
        </ul>
        <p>
          Add either from the Connections page's "+" buttons; edit or rotate a token any time from
          the pencil icon on its row, and delete with the trash icon (with a confirmation).{' '}
          {isAdmin ? (
            <>
              As an admin, you see and can manage every user's Sonar servers and GitHub
              credentials, not just your own.
            </>
          ) : (
            <>You only see and manage your own — other users' connections aren't visible to you.</>
          )}
        </p>
        {isAdmin && (
          <p>
            <strong>You'll also need an LLM API Key configured</strong> — this is admin-only, at
            the top of the Connections page. Add at least one vendor (Google, OpenAI, or
            Anthropic) with its model name and API key; the first one you add becomes "active"
            automatically. Only one config is active at a time — that's the one every new run
            actually uses. Use the checkmark icon to switch which one is active, and note you
            can't delete the currently active config (activate a different one first). If no
            config is active, starting a run will fail immediately with a clear error rather than
            running with no working credentials.
          </p>
        )}
        {!isAdmin && (
          <p>
            The LLM API key that powers the actual fixes is managed by an admin — if runs are
            failing with an LLM-related error, that's the first thing to check with them.
          </p>
        )}
      </section>

      <section>
        <h3>2. Start a run</h3>
        <p>
          Click the play button next to "Runs" to open the New Run form. Pick the agent, the
          GitHub repo (owner/repo) and optionally a branch (defaults to the repo's default
          branch), the language, and which Sonar server and GitHub credential to use. Submitting
          takes you straight to that run's detail page.
        </p>
      </section>

      <section>
        <h3>3. Watch it run</h3>
        <p>On a run's detail page you'll find:</p>
        <ul>
          <li>
            A <strong>live transcript</strong> — every step the agent takes as it happens: which
            file it's working on, its reasoning (collapsed under "🤔 Thinking"), tool calls and
            their results, and state changes. It updates live via a streaming connection and
            closes automatically once the run finishes.
          </li>
          <li>
            An <strong>Export JSON</strong> button on the transcript to download the full event
            history for later review.
          </li>
          <li>
            Once finished, a <strong>final report</strong>: files changed, issues fixed, any files
            flagged for manual review (with the reason — commonly an LLM call failure, which
            usually means the active LLM API key is invalid or missing), final
            Reliability/Maintainability/Security ratings, whether the fix branch was pushed, and
            how long it took.
          </li>
        </ul>
        <p>
          The Runs list shows every run's status at a glance and refreshes automatically while
          anything is in progress.
          {isAdmin
            ? " As an admin, you see everyone's runs (with an Owner column); "
            : ' You only see your own runs; '}
          {isAdmin ? 'a regular user only sees their own.' : 'an admin can see all of them.'}
        </p>
      </section>

      {isAdmin && (
        <section>
          <h3>4. Managing users (admin only)</h3>
          <p>
            There's no self-signup — accounts are created from the Users page. Give someone an
            email, a password, and a role (<code>user</code> or <code>admin</code>); they can sign
            in immediately. You can delete any account except your own.
          </p>
        </section>
      )}

      <section>
        <h3>Roles at a glance</h3>
        <ul>
          <li>
            <strong>admin</strong> — exclusive access to the LLM API Keys section; sees and
            manages every user's runs, Sonar servers, and GitHub credentials; manages user
            accounts.
          </li>
          <li>
            <strong>user</strong> — sees and manages only their own runs, Sonar servers, and
            GitHub credentials.
          </li>
        </ul>
      </section>

      <section>
        <h3>Common issues</h3>
        <ul>
          <li>
            <strong>"No active LLM API key configured"</strong> when starting a run — an admin
            needs to add and activate one on the Connections page.
          </li>
          <li>
            <strong>Files "flagged for manual review" with an LLM error</strong> in the final
            report — almost always means the active LLM API key was invalid, expired, or
            restricted at the time the run executed. Fixing the key doesn't retroactively fix a
            finished run; start a new one after correcting it.
          </li>
          <li>
            <strong>A Sonar server or GitHub credential you expect to see is missing</strong> —
            connections are private to whoever created them unless you're an admin.
          </li>
        </ul>
      </section>
    </div>
  )
}
