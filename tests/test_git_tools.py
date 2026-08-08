import os
import stat
import subprocess

from sonar_autofix_agent.tools import git_tools


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
