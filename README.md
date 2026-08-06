# Sonar Auto-Fix Agent (Google ADK)

Autonomous agent that fetches SonarQube findings from a local or GitHub-hosted
Java project, fixes them file-by-file, verifies the build, and re-scans to
confirm no regressions — targeting a specific rating outcome, not just "fewer
issues." Built on Google's Agent Development Kit (ADK).

**Status: architecture and orchestration are fully wired. Several I/O-layer
functions are still stubs (`NotImplementedError`) — see [Implementation
Status](#implementation-status) before assuming this runs end-to-end.**

---

## What this agent is trying to achieve

Not "fix all Sonar issues." Specifically:

- **Security & Reliability ratings → A.** These are gated by the single
  *worst* open Bug/Vulnerability, not a ratio — so scope here goes all the
  way down to Minor/Low severity (Info excluded, since it never gates the
  rating).
- **Maintainability rating → A.** This is a technical-debt *ratio* (≤5% for
  A), not a worst-issue threshold. Scope stays at Critical/High/Medium by
  default; a separate pass only pulls in more Minor/Low code smells if the
  ratio is still over target after the main pass, prioritized by
  remediation-effort (highest debt-minutes first).
- **Duplication and coverage are explicitly out of scope.** They're metrics,
  not issues, so they never entered issue-fetching — but `OUT_OF_SCOPE_METRICS`
  and `IN_SCOPE_RATING_METRICS` in `tools/sonar_tools.py` exist specifically
  so nothing downstream can accidentally pull them back into the success
  criterion.
- **Minor/Low Security & Reliability issues are never auto-resolved.**
  They're routed to a human-review queue (`won't-fix`/`false-positive`
  candidates) instead of either (a) being silently dropped or (b) having the
  agent generate a diff for every trivial one. See
  `tools/sonar_tools.resolve_issue_transition()` — it exists but is
  deliberately not called anywhere in the autonomous loop.

---

## Architecture

### Design principle: LLM only where it has to be

`fix_llm_agent` (an ADK `LlmAgent`) is the **only** LLM call in the entire
graph. Every other decision — prioritization, cluster classification, patch
application, checkpoint gating, loop exit, tool availability — is a plain
`BaseAgent` making a deterministic decision from `session.state`. This is
enforced by the object graph, not just a convention: a `BaseAgent` cannot
improvise a different orchestration path the way an `LlmAgent` could.

### Agent graph

```
root_agent (SequentialAgent)
├── SetupStep                     -- resolve source, branch, PREFLIGHT CHECK (fail fast)
├── outer_loop (LoopAgent, max 5)
│     ├── FetchPrioritizeStep     -- fetch Sonar issues, classify, build file queue
│     ├── per_file_loop (LoopAgent)
│     │     ├── FileFixerStep     -- cluster classification (5.2/6.1 resolution), builds prompt
│     │     ├── fix_llm_agent     -- ONLY LLM call: generates a unified diff for one file
│     │     ├── ApplyAndVerifyStep -- apply diff, compile check, pattern-verify, commit
│     │     └── CheckpointGate    -- fires checkpoint_pipeline every N files
│     │           └── checkpoint_pipeline (SequentialAgent)
│     │                 ├── RunFullVerifyStep          -- full build + test suite
│     │                 └── TriggerAndReconcileScanStep -- re-scan, catch regressions
│     └── OuterExitCheck          -- escalate when queue empty or max iterations hit
├── maintainability_expansion_loop (LoopAgent, max 4)
│     ├── MaintainabilityDebtCheckStep -- checks sqale_debt_ratio, tops up scope if needed
│     └── per_file_loop (reused)
├── PushStep                      -- push the fix branch to origin (skips if nothing was committed)
└── ReportStep                    -- final ratings, review queue, flagged files, push result
```

### Why some things are custom `BaseAgent`s instead of ADK's built-in primitives

ADK's `LoopAgent` repeats a fixed sub-agent list — it has no native "iterate
over a list" or "every N iterations" primitive. Both are implemented as
explicit state-driven `BaseAgent`s instead of being forced into a shape ADK
wasn't built for:

- **`per_file_loop`'s real exit condition** is `FileFixerStep` popping an
  empty `ORDERED_FILES_REMAINING` and signaling `escalate=True` — the loop's
  own `max_iterations` is just a generous ceiling, not the actual control.
- **`CheckpointGate`** manually checks `FILES_SINCE_CHECKPOINT` against
  `CHECKPOINT_BATCH_SIZE` and conditionally dispatches `checkpoint_pipeline`.

---

## The 5.2/6.1 resolution (cluster handling)

Original design tension: should overlapping/nested Sonar issues in the same
file be fixed in one LLM call (matching the batched fix-prompt skeleton) or
require a re-read-and-relocate cycle (the original per-issue-cluster
handling)? Resolved as:

1. **Classification happens before any LLM call**, deterministically, using
   `textRange` (`tools/patch_tools.classify_and_prepare_batch`).
2. **Colliding clusters (partial overlap, not nested) are excluded from the
   prompt entirely** and flagged for manual review — never sent to the LLM.
3. **Independent and nested issues stay in one batched prompt per file** —
   the model resolves nested cascades itself in a single pass, since it sees
   the full current file text, not stale offsets.
4. **The orchestrator's "re-read and relocate" step becomes verification,
   not regeneration**: `patch_tools.verify_issue_patterns_resolved()` checks
   each targeted issue's pattern is actually gone after the diff applies. A
   narrow, single-issue follow-up LLM call only fires if that check fails —
   the exception path, not the default.

Net effect: LLM calls stay at O(files), not O(issues).

---

## Scope logic (severity floors, review lane, debt ratio)

All in `tools/sonar_tools.py`, single source of truth: `classify_issue()`.

| Category | In-scope severities | Action |
|---|---|---|
| Security | Blocker/Critical/Major/Minor (legacy) or Blocker/High/Medium/Low (Clean Code) | Minor/Low → review queue; rest → autofix |
| Reliability | same as Security | same split |
| Maintainability | Blocker/Critical/Major or Blocker/High/Medium only | autofix (expansion pass below tops up if needed) |
| Hotspots | `vulnerabilityProbability` HIGH/MEDIUM only | autofix |

Info-severity is out of scope everywhere — it never gates a rating.

`partition_and_prioritize()` splits fetched issues into the autofix file
queue and the `WONT_FIX_REVIEW_QUEUE` (never auto-resolved). The
Maintainability expansion loop only fires if `sqale_debt_ratio` is still
above `MAINTAINABILITY_DEBT_RATIO_TARGET` (5.0) after the main pass, pulling
Minor/Low code smells sorted by remediation-minutes descending.

---

## Repo layout

```
sonar_autofix_agent/
├── __init__.py           -- exposes root_agent for ADK's CLI/Runner
├── agents.py              -- the ADK graph (start here)
├── prompts.py              -- shared fix-prompt skeleton + per-language addenda
├── state_schema.py          -- all session.state keys, one place
├── adapters/
│   ├── __init__.py
│   └── base.py               -- LanguageAdapter interface, Maven + Gradle impls,
│                                 build-tool auto-detection, preflight checks
└── tools/
    ├── __init__.py
    ├── sonar_tools.py         -- fetch/classify/prioritize, ratings, debt ratio
    ├── patch_tools.py          -- cluster classification, diff apply, verification
    └── git_tools.py             -- local/GitHub source resolution, branch, commit

run_local.py               -- entry point: loads .env, seeds session state, runs the graph
.env.example                -- copy to .env and fill in
requirements.txt
```

---

## Setup (macOS)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the keys below
```

| `.env` key | Purpose |
|---|---|
| `GOOGLE_API_KEY` | Gemini access for `fix_llm_agent` (AI Studio) |
| `SONAR_BASE_URL` | local Sonar instance, e.g. `http://localhost:9000` |
| `SONAR_TOKEN` | Sonar UI → My Account → Security → Generate Token |
| `CE_EDITION` | `true` → local working-tree scan (Community Edition has no branch analysis) |
| `SOURCE_TYPE` | `local` or `github` |
| `SOURCE_PATH` | used if `SOURCE_TYPE=local` |
| `GITHUB_REPO` | used if `SOURCE_TYPE=github` — `owner/repo` or full URL |
| `GITHUB_TOKEN` | fine-grained PAT, `Contents: Read & write` — only needed to push the fix branch |
| `WORKSPACE_ROOT` | where GitHub-mode clones land |
| `LANGUAGE` | `java` (auto-detects Maven vs Gradle) or explicit `java-maven`/`java-gradle` |
| `FIX_LLM_THINKING_BUDGET` | optional, default `4096` — caps `fix_llm_agent`'s Gemini thinking tokens per file. Set to `-1` for automatic/uncapped (the pre-cap default) if fix quality regresses. |

Note: `sonar.projectKey` is not an `.env` setting — it's read directly from the
checked-out repo's `build.gradle`/`build.gradle.kts` (`sonar { properties { property "sonar.projectKey", ... } } }`
or `gradle.properties`) or `pom.xml` (`<sonar.projectKey>` property, falling back to `groupId:artifactId`),
so it always matches whatever project key the Sonar plugin itself will scan under.

---

## Prerequisites (checked automatically, fail-fast)

`SetupStep` validates all of these before touching a git branch or fetching a
single issue — a failure here stops the run immediately with a specific,
actionable message instead of failing confusingly mid-pipeline (or, worse,
silently finding 0 issues). Worth self-checking before a run too:

**Tooling** (`ToolNotAvailableError`)
- `java` on PATH.
- `mvn`/`gradle` on PATH, *or* the repo ships a working wrapper (`mvnw`/`mvnw.cmd`,
  `gradlew`/`gradlew.bat` **and** the committed `gradle/wrapper/gradle-wrapper.jar`
  — a wrapper script without its jar, common when it's gitignored, fails
  opaquely deep inside a build call otherwise).

**Repo/build-file configuration** (`BuildToolNotDetectedError` / `SonarConfigNotFoundError`)
- `pom.xml` or `build.gradle[.kts]` exists at the repo root.
- It resolves to a Sonar project key — either an explicit `sonar.projectKey`
  property, or (Maven only) a `groupId`/`artifactId` to fall back to.
- Local source only: the given path is a real git repository.

**Sonar server state** (`SonarPreflightError`) — the two easiest to miss,
since a project key can look completely valid and still fail here:
- The server at `SONAR_BASE_URL` is reachable and `SONAR_TOKEN` authenticates
  (`/api/authentication/validate` always returns HTTP 200 even for a bad
  token — the actual signal is the `valid` field in the body, not the status
  code).
- **The resolved project key has at least one analysis already on that
  server.** A key that resolves cleanly from `pom.xml`/`build.gradle` can
  still be one nobody has ever scanned — or scanned under a *different*
  key — and without this check the run would proceed to create a branch and
  then silently find 0 issues, with nothing telling you why. If this fires,
  run an initial scan first, e.g.:
  ```bash
  ./mvnw sonar:sonar -Dsonar.projectKey=<key> -Dsonar.host.url=$SONAR_BASE_URL -Dsonar.token=$SONAR_TOKEN
  # or
  ./gradlew sonar -Dsonar.projectKey=<key> -Dsonar.host.url=$SONAR_BASE_URL -Dsonar.token=$SONAR_TOKEN
  ```
  then re-run — `sonar.projectKey` only needs to be set on the initial scan;
  persisting it in `pom.xml`/`build.gradle` afterward keeps every later run
  (including the ones this agent triggers itself) pointed at the same key.

Not currently pre-checked, still worth confirming yourself: GitHub source
with a private repo needs a `GITHUB_TOKEN` with read access for the clone
(currently only surfaces as a clone failure); pushing the fix branch needs
`Contents: Read & write` on that same token.

---

## Run

```bash
python run_local.py
```

Not `adk web` / `adk run` for real runs — those are built for turn-by-turn
chat, and this pipeline runs to completion from pre-seeded state rather than
responding conversationally. `run_local.py` is the actual entry point;
`adk web` is only useful for poking at `fix_llm_agent` in isolation.

---

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

Covers the deterministic logic layer — `tools/deterministic_fixes.py`,
`tools/patch_tools.py`, `tools/git_tools.py`, `tools/sonar_tools.py`
(classification/prioritization + preflight checks against mocked HTTP),
`adapters/base.py`, and `prompts.py` — via real temp git repos and
filesystem fixtures, no network or LLM calls, so it's fast and hermetic.

Not yet covered: the `BaseAgent` orchestration classes in `agents.py`
themselves (`FileFixerStep`, `ApplyAndVerifyStep`, `RunFullVerifyStep`,
etc.). Unit-testing those directly needs a real `InvocationContext`
(session, agent tree, ADK's own plumbing) rather than a plain mock, since
they're written against that contract — worth adding as a follow-up, but
scoped out here in favor of thorough coverage of the pure logic layer,
which is both the highest-value and lowest-risk-to-test surface.

---

## Implementation status

Everything in `agents.py` is fully wired and real. These are still stubs
(`raise NotImplementedError`) — the shape of the fix, but not the actual
HTTP/subprocess call:

- `tools/sonar_tools.py`: `fetch_issues_and_hotspots`, `get_rule_description`,
  `trigger_sonar_analysis`, `poll_ce_task_status`,
  `get_issues_created_after`, `get_quality_ratings`,
  `get_maintainability_debt_ratio`, `resolve_issue_transition` (intentionally
  never called autonomously — human-gated by design, not an oversight)
- `adapters/base.py`: `parse_and_validate_patch` (both Maven and Gradle
  adapters) — syntax-only diff validation, not yet implemented
- `tools/patch_tools.py`: `apply_diff`, `verify_issue_patterns_resolved`

Already real, not stubs: `git_tools.py` (full local/GitHub resolution,
branching, commits), `adapters/base.py`'s `quick_compile_check` /
`verify_build` / `preflight_check` / build-tool auto-detection for both
Maven and Gradle, and all of `agents.py`'s orchestration.

**Suggested next step if picking this up:** wire
`fetch_issues_and_hotspots` + `get_rule_description` first — everything
downstream (`classify_issue`, the file queue, the fix prompt) depends on the
shape of what those two return, so getting real data flowing through them
early surfaces any schema mismatches before more stubs get filled in on top
of a guess.
