import os
import stat
import subprocess

from core.tools import git_tools


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r


# --- _force_rmtree -----------------------------------------------------------

def test_force_rmtree_deletes_read_only_files(tmp_path):
    """Regression: shutil.rmtree() alone fails on Windows (and, as this
    test itself caught, would also fail on Unix if the chmod pre-pass
    only set the write bit alone rather than a full rwx mode -- chmod
    REPLACES the mode, it doesn't add to it, so write-bit-only strips a
    directory's execute bit and makes it untraversable). git marks files
    inside .git/objects read-only; reported live as: existing workspace
    code not getting deleted before a fresh clone, crashing the run."""
    repo = tmp_path / "repo"
    objects_dir = repo / ".git" / "objects"
    objects_dir.mkdir(parents=True)
    obj_file = objects_dir / "abc123"
    obj_file.write_text("x")

    # simulate git's read-only object files and a read-only containing dir
    os.chmod(obj_file, stat.S_IREAD)
    os.chmod(objects_dir, stat.S_IREAD)
    os.chmod(repo / ".git", stat.S_IREAD)

    git_tools._force_rmtree(str(repo))
    assert not repo.exists()


def test_force_rmtree_handles_normal_writable_tree(tmp_path):
    d = tmp_path / "plain"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "file.txt").write_text("x")
    git_tools._force_rmtree(str(d))
    assert not d.exists()


# --- _sanitize_branch_component ---------------------------------------------

def test_sanitize_branch_component_strips_colon_from_maven_default_key():
    # the exact live failure this session: groupId:artifactId fallback
    assert git_tools._sanitize_branch_component("org.owasp.webgoat:webgoat") == "org.owasp.webgoat-webgoat"


def test_sanitize_branch_component_handles_all_unsafe_chars():
    result = git_tools._sanitize_branch_component("a b~c^d:e?f*g[h\\i")
    assert git_tools._BRANCH_UNSAFE_RE.search(result) is None


def test_sanitize_branch_component_collapses_dots_and_dashes():
    assert ".." not in git_tools._sanitize_branch_component("a..b")
    assert "--" not in git_tools._sanitize_branch_component("a::b")  # two colons -> two dashes -> collapsed


def test_sanitize_branch_component_strips_leading_trailing_junk():
    result = git_tools._sanitize_branch_component(".-my-key-./")
    assert not result.startswith((".", "-", "/"))
    assert not result.endswith((".", "-", "/"))


def test_sanitize_branch_component_empty_falls_back_to_project():
    assert git_tools._sanitize_branch_component(":::") == "project"


def test_sanitized_branch_name_is_actually_valid_git_ref():
    for raw_key in ["org.owasp.webgoat:webgoat", "a b~c^d:e?f*g[h\\i", ":::", "..weird..", "normal-key"]:
        branch = f"{git_tools._sanitize_branch_component(raw_key)}_agent_20260101T000000Z"
        result = subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True)
        assert result.returncode == 0, f"{branch!r} is not a valid git ref (from {raw_key!r})"


# --- _default_branch / resolve_source (local) --------------------------------

def test_default_branch_uses_origin_symbolic_ref_when_present(git_repo):
    (git_repo / "A.java").write_text("x")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))
    _git(["remote", "add", "origin", "https://example.invalid/repo.git"], str(git_repo))
    # Doesn't require the remote to actually be reachable -- this ref is
    # just local bookkeeping git itself writes after a real `git clone`.
    _git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop"], str(git_repo))
    assert git_tools._default_branch(str(git_repo)) == "develop"


def test_default_branch_falls_back_to_local_main_without_origin(git_repo):
    (git_repo / "A.java").write_text("x")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))
    assert git_tools._default_branch(str(git_repo)) == "main"


def test_default_branch_none_when_nothing_conventional_exists(git_repo):
    (git_repo / "A.java").write_text("x")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))
    _git(["branch", "-m", "main", "trunk"], str(git_repo))
    assert git_tools._default_branch(str(git_repo)) is None


def test_resolve_source_local_switches_off_a_stale_agent_branch(git_repo):
    """Regression: exact live bug (WebGoat). A local source's working
    directory persists across separate runs -- unlike the github path
    (always a fresh clone). If a prior run was interrupted mid-way, it
    leaves its own {project_key}_agent_* branch checked out with
    in-progress commits still on it. create_branch()'s `git checkout -b`
    branches from CURRENT HEAD, so without this reset, the next run
    silently branches off that stale branch instead of main, inheriting
    whatever the interrupted run had done (including anything broken that
    its own checkpoint never got a chance to catch) as if it were part of
    the original codebase. Confirmed live via `git merge-base` between two
    consecutive runs' branches -- it landed deep in the earlier run's own
    fix commits, not back at main."""
    (git_repo / "A.java").write_text("original\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    _git(["checkout", "-b", "my-project_agent_20260101_000000"], str(git_repo))
    (git_repo / "A.java").write_text("mid-run, uncommitted-checkpoint change\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "fix: sonar issues in A.java"], str(git_repo))
    # Left checked out here, as if the process had just been killed.

    working_dir = git_tools.resolve_source(str(git_repo), "local", "/unused")

    current = _git(["branch", "--show-current"], working_dir).stdout.strip()
    assert current == "main"
    assert (git_repo / "A.java").read_text() == "original\n"


# --- _checkout_source_branch / resolve_source(source_branch=...) -------------

def test_checkout_source_branch_uses_existing_local_branch(git_repo):
    (git_repo / "A.java").write_text("on main\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))
    _git(["checkout", "-b", "develop"], str(git_repo))
    (git_repo / "A.java").write_text("on develop\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "develop work"], str(git_repo))
    _git(["checkout", "main"], str(git_repo))

    git_tools._checkout_source_branch(str(git_repo), "develop")
    assert _git(["branch", "--show-current"], str(git_repo)).stdout.strip() == "develop"
    assert (git_repo / "A.java").read_text() == "on develop\n"


def test_checkout_source_branch_tracks_from_origin_when_only_remote_exists(tmp_path):
    """Mirrors what a real GitHub-clone repo looks like: a branch that
    exists as a remote-tracking ref (origin/develop) but was never
    checked out locally -- resolve_source's local path must still be able
    to reach it, not just branches that already have a local ref."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(["init", "-q", "-b", "main"], str(upstream))
    _git(["config", "user.email", "t@example.com"], str(upstream))
    _git(["config", "user.name", "T"], str(upstream))
    (upstream / "A.java").write_text("on main\n")
    _git(["add", "-A"], str(upstream))
    _git(["commit", "-m", "init"], str(upstream))
    _git(["checkout", "-b", "develop"], str(upstream))
    (upstream / "A.java").write_text("on develop\n")
    _git(["add", "-A"], str(upstream))
    _git(["commit", "-m", "develop work"], str(upstream))

    clone = tmp_path / "clone"
    _git(["clone", "-q", str(upstream), str(clone)], str(tmp_path))  # clones main only, by default

    git_tools._checkout_source_branch(str(clone), "develop")
    assert _git(["branch", "--show-current"], str(clone)).stdout.strip() == "develop"
    assert (clone / "A.java").read_text() == "on develop\n"


def test_checkout_source_branch_raises_when_branch_not_found_anywhere(git_repo):
    (git_repo / "A.java").write_text("x")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    import pytest
    with pytest.raises(RuntimeError, match="not found locally or on origin"):
        git_tools._checkout_source_branch(str(git_repo), "does-not-exist")


def test_resolve_source_local_checks_out_requested_branch_instead_of_default(git_repo):
    (git_repo / "A.java").write_text("on main\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))
    _git(["checkout", "-b", "release"], str(git_repo))
    (git_repo / "A.java").write_text("on release\n")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "release work"], str(git_repo))
    _git(["checkout", "main"], str(git_repo))

    working_dir = git_tools.resolve_source(str(git_repo), "local", "/unused", source_branch="release")

    assert _git(["branch", "--show-current"], working_dir).stdout.strip() == "release"
    assert (git_repo / "A.java").read_text() == "on release\n"


def test_resolve_source_github_clone_includes_branch_flag(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, cwd=None, env=None):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_tools, "_run", fake_run)
    git_tools.resolve_source(
        "owner/repo", "github", str(tmp_path), source_branch="release/v2",
    )
    assert "--branch" in captured["args"]
    assert captured["args"][captured["args"].index("--branch") + 1] == "release/v2"


def test_resolve_source_github_clone_omits_branch_flag_when_not_given(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, cwd=None, env=None):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_tools, "_run", fake_run)
    git_tools.resolve_source("owner/repo", "github", str(tmp_path))
    assert "--branch" not in captured["args"]


# --- create_branch -----------------------------------------------------------

def test_create_branch_creates_and_checks_out_new_branch(git_repo):
    (git_repo / "A.java").write_text("x")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    branch_name = git_tools.create_branch(str(git_repo), "my-project", "20260101_000000")
    assert branch_name == "my-project_agent_20260101_000000"
    current = _git(["branch", "--show-current"], str(git_repo)).stdout.strip()
    assert current == branch_name


def test_create_branch_ignores_an_existing_matching_branch(git_repo):
    """Regression: create_branch used to look for and resume a matching
    existing branch first. Removed by explicit request after repeatedly
    hitting friction from it in practice -- a file already correctly
    flagged as needing another look would resume into looking "done"
    forever, with no automatic way back short of deleting the branch by
    hand. Always creates fresh now, even if a same-named branch from an
    earlier run already exists on this repo -- git itself will raise if
    the exact branch name collides (extremely unlikely given the
    timestamp suffix), this isn't silently resuming into it."""
    (git_repo / "A.java").write_text("x")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))
    _git(["branch", "my-project_agent_20250101_000000"], str(git_repo))  # a stale, unrelated prior branch

    branch_name = git_tools.create_branch(str(git_repo), "my-project", "20260101_000000")
    assert branch_name == "my-project_agent_20260101_000000"  # new timestamp, not the stale branch


# --- commit() no-op semantics ------------------------------------------------

def test_commit_returns_sha_when_there_are_changes(git_repo):
    (git_repo / "A.java").write_text("x")
    sha = git_tools.commit(str(git_repo), "fix: sonar issues in A.java")
    assert sha is not None
    assert len(sha) == 40


def test_commit_returns_none_when_nothing_to_commit(git_repo):
    (git_repo / "A.java").write_text("x")
    first = git_tools.commit(str(git_repo), "init")
    assert first is not None

    second = git_tools.commit(str(git_repo), "fix: sonar issues in A.java (no-op)")
    assert second is None  # no error raised despite "nothing to commit"


# --- revert_file / revert_commit_for_file ------------------------------------

def test_revert_file_restores_committed_content(git_repo):
    (git_repo / "A.java").write_text("original")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    (git_repo / "A.java").write_text("modified")
    git_tools.revert_file(str(git_repo), "A.java")
    assert (git_repo / "A.java").read_text() == "original"


def test_revert_commit_for_file_only_touches_named_file(git_repo):
    for name in ("A.java", "B.java"):
        (git_repo / name).write_text("original")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    (git_repo / "A.java").write_text("broken fix")
    (git_repo / "B.java").write_text("unrelated later change")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "fix: sonar issues in A.java"], str(git_repo))
    sha = _git(["rev-parse", "HEAD"], str(git_repo)).stdout.strip()

    git_tools.revert_commit_for_file(str(git_repo), sha, "A.java")
    assert (git_repo / "A.java").read_text() == "original"
    assert (git_repo / "B.java").read_text() == "unrelated later change"  # untouched


# --- restore_file_from_commit -------------------------------------------------

def test_restore_file_from_commit_undoes_a_revert(git_repo):
    """The exact round trip checkpoint bisection's re-apply-and-verify pass
    relies on (agents/checkpoint.py's RunFullVerifyStep): a file reverted
    with revert_commit_for_file can be brought straight back to the fixed
    content with restore_file_from_commit(working_dir, commit_sha, file),
    no different than if the revert had never happened."""
    (git_repo / "A.java").write_text("original")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    (git_repo / "A.java").write_text("the fix")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "fix: sonar issues in A.java"], str(git_repo))
    fix_sha = _git(["rev-parse", "HEAD"], str(git_repo)).stdout.strip()

    git_tools.revert_commit_for_file(str(git_repo), fix_sha, "A.java")
    assert (git_repo / "A.java").read_text() == "original"

    git_tools.restore_file_from_commit(str(git_repo), fix_sha, "A.java")
    assert (git_repo / "A.java").read_text() == "the fix"


def test_restore_file_from_commit_leaves_change_uncommitted(git_repo):
    """Deliberately staged-but-not-committed -- see the function's
    docstring: the caller test-drives a build against this change and
    either commits it (git_tools.commit) or discards it cheaply
    (git_tools.revert_file) without leaving a throwaway commit either way."""
    (git_repo / "A.java").write_text("original")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "init"], str(git_repo))

    (git_repo / "A.java").write_text("the fix")
    _git(["add", "-A"], str(git_repo))
    _git(["commit", "-m", "fix: sonar issues in A.java"], str(git_repo))
    fix_sha = _git(["rev-parse", "HEAD"], str(git_repo)).stdout.strip()
    head_before = _git(["rev-parse", "HEAD"], str(git_repo)).stdout.strip()

    git_tools.revert_commit_for_file(str(git_repo), fix_sha, "A.java")
    head_after_revert = _git(["rev-parse", "HEAD"], str(git_repo)).stdout.strip()
    assert head_after_revert != head_before  # revert_commit_for_file does commit

    git_tools.restore_file_from_commit(str(git_repo), fix_sha, "A.java")
    head_after_restore = _git(["rev-parse", "HEAD"], str(git_repo)).stdout.strip()
    assert head_after_restore == head_after_revert  # unchanged -- nothing committed
    status = _git(["status", "--porcelain"], str(git_repo)).stdout.strip()
    assert "A.java" in status  # but the working tree/index does have a pending change


# --- Sanitization and Masking -------------------------------------------------

def test_sanitize_string_masks_authorization_and_urls():
    text = "error: AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46Z2l0aHViX3BhdF8x"
    assert git_tools._sanitize_string(text) == "error: AUTHORIZATION: basic [REDACTED]"

    text_url = "unable to access 'https://github_pat_123456789@github.com/org/repo.git'"
    assert git_tools._sanitize_string(text_url) == "unable to access 'https://[REDACTED]@github.com/org/repo.git'"

    text_url_user_pass = "unable to access 'https://user:pass@github.com/org/repo.git'"
    assert git_tools._sanitize_string(text_url_user_pass) == "unable to access 'https://[REDACTED]:[REDACTED]@github.com/org/repo.git'"


def test_sanitize_args_masks_args():
    args = [
        "git",
        "-c",
        "http.https://github.com/.extraheader=AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46Z2l0aHViX3BhdF8x",
        "clone",
        "https://github_pat_foo@github.com/org/repo.git"
    ]
    sanitized = git_tools._sanitize_args(args)
    assert sanitized == [
        "git",
        "-c",
        "http.https://github.com/.extraheader=AUTHORIZATION: basic [REDACTED]",
        "clone",
        "https://[REDACTED]@github.com/org/repo.git"
    ]


def test_sanitize_args_masks_custom_extraheader():
    args = [
        "git",
        "-c",
        "http.extraheader=secret_cookie_value"
    ]
    sanitized = git_tools._sanitize_args(args)
    assert sanitized == [
        "git",
        "-c",
        "http.extraheader=[REDACTED]"
    ]


def test_run_failure_exception_message_is_sanitized(git_repo):
    import pytest
    args = [
        "git",
        "-c",
        "http.https://github.com/.extraheader=AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46Z2l0aHViX3BhdF8x",
        "push",
        "https://foo@github.com/org/repo.git"
    ]
    with pytest.raises(RuntimeError) as exc_info:
        git_tools._run(args, cwd=str(git_repo))
    
    exc_message = str(exc_info.value)
    assert "eC1hY2Nlc3MtdG9rZW4" not in exc_message
    assert "foo" not in exc_message
    assert "AUTHORIZATION: basic [REDACTED]" in exc_message
    assert "https://[REDACTED]@github.com/org/repo.git" in exc_message

