# Sonar Remediation Agents (Google ADK)

Three autonomous agents that take a Java project's SonarQube findings to a
target outcome — not just "fewer issues" — fixing them file-by-file,
verifying the build, and re-scanning to confirm no regressions. Built on
Google's Agent Development Kit (ADK).

| Package | Agent | What it drives |
|---|---|---|
| `agent_techdebt/` | **Sonar Tech-Debt Agent** | Security / Reliability / Maintainability ratings → A |
| `agent_coverage/` | **Sonar Coverage Agent** | Test coverage % up (generates JUnit tests for uncovered lines) |
| `agent_duplicate/` | **Sonar Duplication Agent** | Duplicated-lines density down (refactors shared logic into helpers) |

All three share one engine (`core/`) and one SonarQube integration layer
(`sonar/`); only the fetch / prompt / apply-and-verify steps differ.

---

## What the Tech-Debt Agent targets

Not "fix all Sonar issues." Specifically:

- **Security & Reliability ratings → A.** Gated by the single *worst* open
  Bug/Vulnerability, not a ratio — so scope goes down to Minor/Low
  severity (Info excluded, since it never gates the rating).
- **Maintainability rating → A.** A technical-debt *ratio* (≤5% for A), not
  a worst-issue threshold. Scope stays at Critical/High/Medium by default;
  a separate expansion pass pulls in more Minor/Low code smells only if the
  ratio is still over target after the main pass, prioritized by
  remediation-effort (highest debt-minutes first).
- **Minor/Low Security & Reliability issues are never auto-resolved.**
  They're routed to a human-review queue (`won't-fix`/`false-positive`
  candidates). `sonar_tools.resolve_issue_transition()` exists but is
  deliberately never called in the autonomous loop — see [Implementation
  status](#implementation-status).
- **Duplication and coverage are out of scope for this agent** — they're
  metrics, not issues, and are handled by the other two agents.
  `OUT_OF_SCOPE_METRICS` / `IN_SCOPE_RATING_METRICS` in
  `sonar/tools/sonar_tools.py` guard against anything downstream pulling
  them back into the success criterion.

---

## Architecture

### Design principle: LLM only where it has to be

`fix_llm_agent` (an ADK `LlmAgent`) is the **only** LLM call in the fix
loop, shared by all three agents. Every other decision — prioritization,
cluster classification, patch application, checkpoint gating, loop exit,
tool availability — is a plain `BaseAgent` making a deterministic decision
from `session.state`. This is enforced by the object graph, not just
convention: a `BaseAgent` cannot improvise a different orchestration path
the way an `LlmAgent` could.

The intake step also uses a small `LlmAgent` (`sonar/intake.py`), but only
to extract a repo location from a chat message — a fully pre-seeded run
(`run_local.py`) never invokes it.

### Tech-Debt Agent graph

```
root_agent (techdebt_intake_step)     -- chat front door; skipped when state is pre-seeded
└── sonar_techdebt_pipeline (SequentialAgent)
    ├── SetupStep                      -- resolve source, branch, PREFLIGHT CHECK (fail fast)
    ├── outer_loop (LoopAgent, max 5)
    │     ├── FetchPrioritizeStep      -- fetch Sonar issues, classify, build file queue
    │     ├── per_file_loop (LoopAgent)
    │     │     ├── FileFixerStep      -- cluster classification, builds the fix prompt
    │     │     ├── fix_llm_agent      -- ONLY LLM call: generates a unified diff for one file
    │     │     ├── ApplyAndVerifyStep -- apply diff, compile check, pattern-verify, commit
    │     │     └── CheckpointGate     -- every N files: full build + test, re-scan, catch regressions
    │     └── OuterExitCheck           -- escalate when queue empty or max iterations hit
    ├── maintainability_expansion_loop (LoopAgent, max 4)
    │     └── MaintainabilityDebtCheckStep + per_file_loop  -- tops up scope if debt ratio still over target
    ├── PushStep                       -- push the fix branch to origin (skips if nothing committed)
    └── ReportStep                     -- final ratings, review queue, flagged files, push result
```

The Coverage and Duplication agents follow the same shape:
`SetupStep → BaselineStep → outer_loop → quality_loop → PushStep → ReportStep`,
reusing `core/`'s per-file loop and `sonar/`'s checkpoint pipeline.

The Coverage agent adds:
- **Preflight-fails if the project has no JaCoCo** configured (`id 'jacoco'`
  in `build.gradle`, or `jacoco-maven-plugin` with a `prepare-agent`
  execution) — without it Sonar reports 0% for every file and there's
  nothing to measure. That plugin declaration is the *only* build-file
  change needed: the scan runs `test jacocoTestReport sonar` in one
  invocation (the `org.sonarqube` plugin never runs `jacocoTestReport`
  itself), an injected init-script forces the JaCoCo XML report on (off by
  default in Gradle), and `-Dsonar.coverage.jacoco.xmlReportPaths` is
  passed explicitly so nothing depends on auto-detection.
- **`CoverageFinalVerifyStep`** before push: one last full build, giving the
  model up to 3 passes to fix its own test files against the real error,
  and resetting the branch to its base commit rather than pushing red if
  that fails — then the authoritative final coverage scan.

### Why custom `BaseAgent`s instead of ADK's built-in primitives

ADK's `LoopAgent` repeats a fixed sub-agent list — no native "iterate over
a list" or "every N iterations". Both are explicit state-driven
`BaseAgent`s:

- **`per_file_loop`'s real exit condition** is `FileFixerStep` popping an
  empty `ORDERED_FILES_REMAINING` and signaling `escalate=True` — the
  loop's `max_iterations` is just a generous ceiling.
- **`CheckpointGate`** manually checks `FILES_SINCE_CHECKPOINT` against
  `CHECKPOINT_BATCH_SIZE` and conditionally dispatches the checkpoint
  pipeline.

---

## Cluster handling (overlapping issues in one file)

1. **Classification happens before any LLM call**, deterministically, using
   `textRange` (`sonar/tools/patch_tools.classify_and_prepare_batch`).
2. **Colliding clusters (partial overlap, not nested) are excluded from the
   prompt** and flagged for manual review — never sent to the LLM.
3. **Independent and nested issues stay in one batched prompt per file** —
   the model resolves nested cascades itself in a single pass, since it
   sees the full current file text, not stale offsets.
4. **"Re-read and relocate" is verification, not regeneration:**
   `patch_tools.verify_issue_patterns_resolved()` checks each targeted
   issue's pattern is actually gone after the diff applies. A narrow
   single-issue follow-up LLM call fires only if that check fails.

Net effect: LLM calls stay at O(files), not O(issues).

---

## Scope logic (severity floors, review lane, debt ratio)

All in `sonar/tools/sonar_tools.py`, single source of truth:
`classify_issue()`.

| Category | In-scope severities | Action |
|---|---|---|
| Security | Blocker/Critical/Major/Minor (legacy) or Blocker/High/Medium/Low (Clean Code) | Minor/Low → review queue; rest → autofix |
| Reliability | same as Security | same split |
| Maintainability | Blocker/Critical/Major or Blocker/High/Medium only | autofix (expansion pass tops up if needed) |
| Hotspots | `vulnerabilityProbability` HIGH/MEDIUM only | autofix |

Info-severity is out of scope everywhere. `partition_and_prioritize()`
splits fetched issues into the autofix file queue and the
`WONT_FIX_REVIEW_QUEUE` (never auto-resolved). The Maintainability
expansion loop only fires if `sqale_debt_ratio` is still above
`MAINTAINABILITY_DEBT_RATIO_TARGET` (5.0) after the main pass.

---

## Repo layout

```
core/                      -- tool-agnostic fix-loop engine, shared by every
│                             agent regardless of finding source (Sonar today;
│                             Veracode/Coverity/SBOM/Black Duck could plug into
│                             the same engine later)
├── state_schema.py          -- all session.state keys, one place
├── adapters/base.py          -- LanguageAdapter interface, Maven + Gradle impls
├── tools/
│   ├── git_tools.py            -- local/GitHub source resolution, branch, commit
│   └── patch_tools.py          -- apply_diff, JUnit failure parsing
└── agents/
    ├── fix_loop.py             -- the LLM-call gate, per-file loop, diff/NO_SAFE_FIX helpers
    ├── checkpoint.py           -- full build verify + bisect-revert
    ├── outer_loop.py           -- generic "queue empty or max iterations" exit
    └── report.py               -- push branch, duration formatting

sonar/                     -- everything specific to SonarQube as a finding
│                             source, shared across the three agents. A plain
│                             (non-agent) package: adk web/run import the
│                             selected agent as a bare top-level module with
│                             only its own parent dir on sys.path, so the
│                             agents stay at the repo root alongside sonar/
│                             and core/, not nested inside sonar/.
├── adapters.py               -- Sonar project-key resolution + scan invocation
├── setup.py                  -- SetupStep: validates the Sonar connection, creates the branch
├── checkpoint.py             -- re-scan + reconcile new Sonar findings
├── intake.py                 -- shared conversational front door + build_intake_step()
└── tools/
    ├── sonar_tools.py          -- fetch/classify/prioritize, ratings, debt ratio, metrics
    ├── patch_tools.py          -- cluster classification, verification
    └── deterministic_fixes.py  -- mechanical one-shot rule fixes

agent_techdebt/            -- Sonar Tech-Debt Agent (Security/Reliability/Maintainability)
agent_coverage/            -- Sonar Coverage Agent (JUnit tests for uncovered lines)
agent_duplicate/           -- Sonar Duplication Agent (extract duplicated blocks)

run_local.py               -- entry point: loads .env, seeds session state, runs AGENT_TYPE's agent
.env.example               -- copy to .env and fill in
```

Run one agent directly with `adk run agent_techdebt` (or `agent_coverage` /
`agent_duplicate`), or browse all three with `adk web .` from the repo root.
`core/` and `sonar/` show up in that listing too but aren't agent
directories (no `agent.py`), so selecting either just errors.

For a real run, use `run_local.py` — not `adk web` / `adk run`. Those are
built for turn-by-turn chat; this pipeline runs to completion from
pre-seeded state. `run_local.py` picks which agent to run from the
`AGENT_TYPE` env var (`techdebt` / `coverage` / `duplicate`, default
`techdebt`).

### Per-agent clone isolation

Each agent works on a **separate clone** of a GitHub source, in its own
sibling workspace dir so the three can run against the same repo without
colliding on a working tree or branch:

```
<dirname of WORKSPACE_ROOT>/
├── sonar_remediation_techdebt/<repo>/
├── sonar_remediation_coverage/<repo>/
└── sonar_remediation_duplicate/<repo>/
```

Wired via `AGENT_SLUG` in each `agent_*/__init__.py` →
`git_tools.agent_workspace_root()`. **Local source (`SOURCE_TYPE=local`) is
still edited in place** — no per-agent copy — so two agents pointed at the
same local path *will* collide; run them one at a time, or point each at
its own checkout.

---

## Setup (macOS)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the keys
python run_local.py
```

| `.env` key | Purpose |
|---|---|
| `AGENT_TYPE` | `techdebt` (default) / `coverage` / `duplicate` — which agent `run_local.py` runs |
| `GOOGLE_API_KEY` | Gemini access for `fix_llm_agent` (AI Studio) |
| `SONAR_BASE_URL` | SonarQube instance, e.g. `http://localhost:9000` |
| `SONAR_TOKEN` | Sonar UI → My Account → Security → Generate Token |
| `CE_EDITION` | `true` → local working-tree scan (leave `true` even on paid editions) |
| `SOURCE_TYPE` | `local` or `github` |
| `SOURCE_PATH` | used if `SOURCE_TYPE=local` |
| `GITHUB_REPO` | used if `SOURCE_TYPE=github` — `owner/repo` or full URL |
| `GITHUB_TOKEN` | fine-grained PAT, `Contents: Read & write` — only to push the fix branch |
| `WORKSPACE_ROOT` | base for GitHub-mode clones — each agent clones into its own sibling `sonar_remediation_<agent>/` (see below) |
| `LANGUAGE` | `java` (auto-detects Maven vs Gradle) or explicit `java-maven`/`java-gradle` |
| `SOURCE_BRANCH` | optional — a specific branch to check out and fix (must already have its own Sonar analysis) |
| `FIX_LLM_THINKING_LEVEL` | optional, default `LOW` — caps Gemini thinking effort per file (`MINIMAL`/`LOW`/`MEDIUM`/`HIGH`) |

`sonar.projectKey` is **not** an `.env` setting — it's read from the
checked-out repo's `build.gradle`/`gradle.properties` or `pom.xml` so it
always matches whatever key the Sonar plugin scans under.

**Java version is also not an `.env` setting** — `SetupStep` reads it from
the checked-out project's own build file (`maven.compiler.release`/
`source`/`target` or `java.version` in `pom.xml`; `sourceCompatibility`/
`targetCompatibility` or a `JavaLanguageVersion` toolchain in
`build.gradle[.kts]`) via `core/adapters/jdk_provisioning.py`, and runs
every compile/test/scan for that project under a matching JDK. The Docker
image only ships its own base JDK (21) — deliberately not a bundle of
every version, since Cloud Run Jobs pull the image fresh on every
execution and that size cost would land on every single run. Any other
declared version is downloaded from
[Adoptium](https://api.adoptium.net)'s own API the first time it's
actually needed and cached on disk for the rest of that container's life
(`JAVA_HOME_<N>` or a local `/opt/jdks/<N>`/package-manager install is
checked first and used as-is if present, so this never re-downloads a
version you already have). A local dev machine or air-gapped run with no
network access to Adoptium falls back to whatever's already on `PATH`
unchanged, with a note either way in the run's first log line
(`adapter.describe_java_selection()`).

---

## Prerequisites (checked automatically, fail-fast)

`SetupStep` validates all of these before touching a git branch or fetching
an issue — a failure stops the run immediately with an actionable message.

**Tooling** (`ToolNotAvailableError`)
- `java` on PATH.
- `mvn`/`gradle` on PATH, *or* the repo ships a working wrapper (`mvnw`,
  `gradlew` **and** the committed `gradle/wrapper/gradle-wrapper.jar`).

**Repo/build-file configuration** (`BuildToolNotDetectedError` / `SonarConfigNotFoundError`)
- `pom.xml` or `build.gradle[.kts]` at the repo root.
- It resolves to a Sonar project key — an explicit `sonar.projectKey`
  property, or (Maven only) a `groupId`/`artifactId` fallback.
- Local source only: the given path is a real git repository.

**Sonar server state** (`SonarPreflightError`)
- The server at `SONAR_BASE_URL` is reachable and `SONAR_TOKEN` authenticates
  (the signal is the `valid` field in the body, not the HTTP status).
- **The resolved project key has at least one analysis already on that
  server.** A key that resolves cleanly from `pom.xml`/`build.gradle` but
  has never been scanned fails here rather than silently finding 0 issues.
  Run one manual scan first:
  ```bash
  ./mvnw sonar:sonar -Dsonar.projectKey=<key> -Dsonar.host.url=$SONAR_BASE_URL -Dsonar.token=$SONAR_TOKEN
  # or
  ./gradlew sonar -Dsonar.projectKey=<key> -Dsonar.host.url=$SONAR_BASE_URL -Dsonar.token=$SONAR_TOKEN
  ```

Not pre-checked: a private GitHub repo needs a `GITHUB_TOKEN` with read
access for the clone; pushing the fix branch needs `Contents: Read & write`.

---

## Deployment

See [docs/GCP_DEPLOYMENT.md](docs/GCP_DEPLOYMENT.md): SonarQube on a Compute
Engine VM, the agent as a Cloud Run **Job** (run-to-completion, not a
service), secrets in Secret Manager, and CI/CD via
`.github/workflows/deploy-gcp.yml` (Workload Identity Federation, no
long-lived keys). GCP resources are named `sonar-remediation-*`.

---

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

Covers the deterministic logic layer — `sonar/tools/*`, `core/tools/*`,
`core/adapters/base.py`, `agent_techdebt/prompts.py` — plus the `BaseAgent`
orchestration classes (`tests/test_orchestration.py`,
`tests/test_multi_agent.py`) driven through a real `Runner` with mocked
adapters and a stubbed `fix_llm_agent`. No network or live LLM calls, so
it's fast and hermetic.

---

## Implementation status

Everything is wired and real. The **one** deliberate exception:

- `sonar/tools/sonar_tools.py::resolve_issue_transition()` raises
  `NotImplementedError`. It is human-gated by design — the autonomous loop
  routes Minor/Low Security & Reliability issues to
  `WONT_FIX_REVIEW_QUEUE` and never transitions an issue's resolution
  status itself. Wire it to `requests.post` only behind a human
  confirmation step with an explicit list of issue keys.
