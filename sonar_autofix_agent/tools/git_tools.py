"""Section 3 + Section 8, implemented via subprocess git calls.

GitHub auth note: the token is passed as a per-invocation `-c
http.extraheader`, not embedded in the remote URL — an embedded
`https://TOKEN@github.com/...` URL gets written into .git/config in
plaintext and shows up in `git remote -v`, error messages, and logs.
extraheader is scoped to the single command and never persisted."""

import base64
import os
import re
import shutil
import stat
import subprocess


def _run(args: list[str], cwd: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git command failed: {' '.join(args)}\n{result.stderr}")
    return result


def _github_auth_args(github_token: str) -> list[str]:
    """Returns -c http.extraheader=... args to inject on the clone/fetch
    command only, so the token never lands in persisted git config."""
    basic = base64.b64encode(f"x-access-token:{github_token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]


def _force_rmtree(path: str) -> None:
    """shutil.rmtree() alone fails on Windows with PermissionError
    ([WinError 5] Access is denied) on a git-cloned directory -- git
    marks files inside .git/objects (and sometimes others) read-only,
    and unlike Unix (where the containing directory's permissions govern
    deletion), Windows enforces the file's own read-only attribute for
    deletion too. Confirmed as the reported failure mode: resolve_source's
    stale-workspace cleanup left old code in place and crashed the whole
    run with an unhandled exception on Windows specifically.

    Clears the read-only bit on everything first, then deletes, rather
    than using shutil.rmtree's onerror/onexc retry-callback -- that
    parameter's name and callback signature changed between Python
    versions (onerror deprecated in 3.12, removed in 3.14 in favor of
    onexc with a different signature), so a pre-pass that doesn't depend
    on either is the version-safe choice.

    Uses S_IRWXU (owner rwx), not just the write bit alone -- os.chmod
    sets the ABSOLUTE mode, it doesn't add to the existing one, so
    chmod(path, S_IWRITE) alone strips a directory's execute bit on Unix
    and makes it untraversable (confirmed live: rmtree then failed with
    PermissionError trying to even open the directory). S_IRWXU is safe
    on both platforms -- Windows only cares whether any write bit is set
    at all (that's what clears the read-only attribute), the rest of the
    mode is irrelevant there."""
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                full = os.path.join(root, name)
                try:
                    os.chmod(full, stat.S_IRWXU)
                except OSError:
                    pass
    shutil.rmtree(path)


def resolve_source(source: str, source_type: str, workspace_root: str, github_token: str | None = None) -> str:
    """
    source_type == "local": source is a filesystem path. Verify it's a git
        repo with a clean working tree; if dirty, stash with a labeled
        entry rather than failing outright.
    source_type == "github": source is a repo URL (https://github.com/owner/repo
        or owner/repo shorthand). Clones into an isolated workspace dir and
        checks out the default branch. github_token is required for private
        repos; harmless to pass for public ones too (auth header is simply
        unused/ignored by GitHub in that case).
    Returns the resolved working_dir.
    """
    if source_type == "local":
        working_dir = os.path.abspath(os.path.expanduser(source))
        if not os.path.isdir(os.path.join(working_dir, ".git")):
            raise RuntimeError(f"{working_dir} is not a git repository")

        status = _run(["git", "status", "--porcelain"], cwd=working_dir)
        if status.stdout.strip():
            label = f"sonar-agent-autostash-{os.getpid()}"
            _run(["git", "stash", "push", "-m", label], cwd=working_dir)
        return working_dir

    if source_type == "github":
        if not source.startswith("http"):
            source = f"https://github.com/{source}.git" if not source.endswith(".git") else f"https://github.com/{source}"
        repo_name = source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        working_dir = os.path.join(workspace_root, repo_name)

        # Always a fresh clone off the default branch — never reuse/fetch a
        # prior run's workspace. A stale clone can carry over its own
        # {project_key}_agent_* branches (find_or_create_branch would then
        # "resume" into whatever state was left there) and any uncommitted
        # or committed drift from a previous, possibly-failed run. Wiping
        # it guarantees this run starts from the actual current upstream.
        if os.path.exists(working_dir):
            _force_rmtree(working_dir)

        os.makedirs(workspace_root, exist_ok=True)
        auth = _github_auth_args(github_token) if github_token else []
        # core.autocrlf=false pins this fresh clone to whatever line endings
        # are actually committed in the repo (typically LF for Java
        # sources), regardless of the host machine's global git config —
        # Windows installs commonly default core.autocrlf=true, which would
        # silently rewrite every checked-out file to CRLF. fix_llm_agent's
        # diffs are generated against LF content; a CRLF-translated working
        # tree would then mismatch on every line, turning the hunk-miscount
        # problem (already unreliable enough on its own) into a guaranteed
        # failure on Windows specifically.
        _run(["git", "-c", "core.autocrlf=false", *auth, "clone", source, working_dir])
        return working_dir

    raise ValueError(f"Unknown source_type: {source_type!r} (expected 'local' or 'github')")


_BRANCH_UNSAFE_RE = re.compile(r"[\s~^:?*\[\\]")


def _sanitize_branch_component(value: str) -> str:
    """git branch names reject a specific set of characters — space, ~, ^,
    :, ?, *, [, \\ (see `git check-ref-format --branch`) — and can't start
    with '.', end with '.'/'.lock'/'/', or contain '..'. Sonar project keys
    aren't restricted the same way: Maven's default groupId:artifactId
    fallback (when a project has no explicit sonar.projectKey) routinely
    produces a colon, e.g. 'org.owasp.webgoat:webgoat' — used verbatim as a
    branch-name prefix, that's an invalid ref and `git checkout -b` fails
    outright before anything else runs (observed live). Only for the
    branch name — callers must keep using the original, unsanitized
    project key everywhere else (Sonar API calls), since Sonar itself
    doesn't share git's restrictions."""
    sanitized = _BRANCH_UNSAFE_RE.sub("-", value)
    sanitized = re.sub(r"\.{2,}", "-", sanitized)
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-./")
    return sanitized or "project"


def create_branch(working_dir: str, sonar_project_key: str, timestamp: str) -> str:
    """Section 3 step 4: always creates a brand-new
    {project_key}_agent_{timestamp} branch from the current HEAD.

    Used to look for and resume a matching existing branch first —
    removed by explicit request after repeatedly hitting friction from
    it in practice: InMemorySessionService has no cross-invocation
    persistence, so a genuinely new session resuming a branch with real
    commits already on it had to reconstruct FILES_COMPLETED /
    FILES_REVERTED_AT_CHECKPOINT from git history, and a file already
    correctly flagged as needing another look (broke the build, or
    verification caught a genuine miss) would otherwise sit "resumed"
    into looking done forever, with no automatic way back to a real
    re-attempt short of deleting the branch by hand — which is exactly
    what ended up happening before every real test run this session. A
    GitHub source already behaved this way in practice (resolve_source()
    wipes and re-clones every time, so an existing local branch to
    resume essentially never exists) — this makes local source match
    that, rather than being the one path with different semantics."""
    prefix = f"{_sanitize_branch_component(sonar_project_key)}_agent_"
    branch_name = f"{prefix}{timestamp}"
    _run(["git", "checkout", "-b", branch_name], cwd=working_dir)
    return branch_name


def commit(working_dir: str, message: str) -> str | None:
    """Returns the new commit SHA, or None if there was nothing to commit
    (e.g. a redundant re-fix regenerated content byte-identical to what's
    already on disk — observed live from the resume bug above, but possible
    any time a fix is a genuine no-op). `git commit` would exit non-zero
    for "nothing to commit", which _run() turns into an unhandled
    RuntimeError that crashes the whole pipeline — a no-op isn't a failure.
    Callers should treat None as "nothing new for a checkpoint to revert",
    not as an error."""
    _run(["git", "add", "-A"], cwd=working_dir)
    status = _run(["git", "status", "--porcelain"], cwd=working_dir)
    if not status.stdout.strip():
        return None
    _run(["git", "commit", "-m", message], cwd=working_dir)
    return _run(["git", "rev-parse", "HEAD"], cwd=working_dir).stdout.strip()


def revert_file(working_dir: str, file_path: str) -> None:
    _run(["git", "checkout", "--", file_path], cwd=working_dir)


def revert_commit_for_file(working_dir: str, commit_sha: str, file_path: str) -> None:
    """Reverts a single file's fix-commit without touching any other file:
    restores file_path's content from that commit's parent, then commits
    the reversion. Used at checkpoint time (Section 5.4) when a full build
    fails after a batch of per-file commits — checking out just this path
    is safer than `git revert <sha>` here, since each fix-commit was staged
    with `git add -A` and a plain revert would replay the whole commit,
    not just this file, if anything else were ever incidentally dirty."""
    _run(["git", "checkout", f"{commit_sha}~1", "--", file_path], cwd=working_dir)
    _run(["git", "add", file_path], cwd=working_dir)
    _run(["git", "commit", "-m", f"revert: {file_path} broke the full build at checkpoint"], cwd=working_dir)


def push_branch(working_dir: str, branch_name: str, github_token: str | None = None) -> None:
    """Pushes branch_name to 'origin'. Uses the same per-invocation
    extraheader auth as the initial clone (never persisted to git config)
    when a github_token is available — harmless to pass even if origin
    isn't github.com, since the extraheader is scoped to the
    github.com host and simply goes unused for any other remote.
    Without a token, relies on whatever git auth is already configured in
    the environment (SSH agent, credential helper) — the path a local
    repo with its own already-authenticated origin takes.

    Raises RuntimeError (with actual git output for real push failures,
    or a clearer message when there's no 'origin' remote at all) — this is
    the run's very last step, so a caller should treat a failure here as
    'commits exist locally on the branch, just not pushed' rather than
    unwinding any of the completed fix work."""
    remotes = _run(["git", "remote"], cwd=working_dir).stdout.split()
    if "origin" not in remotes:
        raise RuntimeError("no 'origin' remote configured in this repo — nothing to push to")

    auth = _github_auth_args(github_token) if github_token else []
    _run(["git", *auth, "push", "-u", "origin", branch_name], cwd=working_dir)


def commit_checkpoint_marker(working_dir: str) -> str:
    _run(["git", "commit", "--allow-empty", "-m", "checkpoint: verified + rescanned clean"], cwd=working_dir)
    return _run(["git", "rev-parse", "HEAD"], cwd=working_dir).stdout.strip()
