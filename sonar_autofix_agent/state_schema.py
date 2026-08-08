"""
Central registry of session.state keys.

Using string constants (not a dataclass) because ADK's prompt templating
injects state via {key} placeholders in instruction strings, so the keys
need to exist as plain strings anyway. Keeping them in one place avoids
typo drift between tools and agent instructions.

Prefix convention (matches ADK's own session-state conventions):
  no prefix   -> persists for this run, scoped to this session
  "temp:"     -> scratch, cleared between invocations, never persisted
"""

# --- Set once in Phase I, read everywhere ---
SONAR_PROJECT_KEY = "sonar_project_key"
BRANCH_NAME = "branch_name"
SOURCE_TYPE = "source_type"          # "local" | "github"
WORKING_DIR = "working_dir"
LANGUAGE = "language"                # drives which LanguageAdapter tools use

# --- Outer loop (5.5) ---
OUTER_ITERATION = "outer_iteration"
MAX_OUTER_ITERATIONS = "max_outer_iterations"

# --- File queue (Phase II output, consumed by per-file loop) ---
ORDERED_FILES_REMAINING = "ordered_files_remaining"   # list[dict], pops front-to-back
FILES_COMPLETED = "files_completed"
FILES_FLAGGED = "files_flagged_for_manual_review"
ISSUES_FIXED = "issues_fixed"
ISSUES_NO_SAFE_FIX = "issues_no_safe_fix"
# Files whose fix broke the full build or introduced new Sonar issues at a
# checkpoint, at any point in this run -- excluded from FetchPrioritizeStep's
# re-fetch permanently (not just for the iteration it happened in), so an
# out-of-scope fix that keeps failing the same way doesn't get re-attempted
# identically every outer_loop iteration. See RunFullVerifyStep/
# TriggerAndReconcileScanStep in agents.py.
FILES_REVERTED_AT_CHECKPOINT = "files_reverted_at_checkpoint"

# --- Human-review lane (Minor/Low Security & Reliability candidates) ---
WONT_FIX_REVIEW_QUEUE = "wont_fix_review_queue"        # never auto-resolved

# --- Maintainability debt-ratio expansion (post main-pass top-up) ---
MAINTAINABILITY_EXPANSION_ITERATION = "maintainability_expansion_iteration"
MAINTAINABILITY_EXPANSION_BATCH_SIZE = "maintainability_expansion_batch_size"

# --- Per-file working state (temp: cleared each file iteration) ---
CURRENT_FILE_GROUP = "temp:current_file_group"        # {file, issues, clusters}
CURRENT_FILE_CONTENT = "temp:current_file_content"
PROPOSED_DIFF = "temp:proposed_diff"

# --- Checkpoint bookkeeping (5.4) ---
FILES_SINCE_CHECKPOINT = "files_since_checkpoint"
CHECKPOINT_BATCH_SIZE = "checkpoint_batch_size"        # e.g. 8-10
CHECKPOINTS = "checkpoints"
LAST_CHECKPOINT_TIME = "temp:last_checkpoint_time"

# --- Loop exit signaling ---
OUTER_LOOP_DONE = "temp:outer_loop_done"
FILE_LOOP_DONE = "temp:file_loop_done"

# --- Run metrics (Section 9 report) ---
RUN_START_TIME = "temp:run_start_time"          # time.time() set right before pipeline_agent runs
TOKEN_USAGE = "temp:token_usage"                # {"prompt_tokens", "candidates_tokens", "total_tokens"}
