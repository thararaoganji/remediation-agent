import subprocess

import pytest


def _git(args: list[str], cwd: str) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"


@pytest.fixture
def git_repo(tmp_path):
    """A real, minimal git repo in a temp dir — used by git_tools tests
    that need actual commit history/working-tree state, not a mock. Local
    identity is set per-repo (not global) so tests never depend on or
    mutate the machine's real git config."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], str(repo))
    _git(["config", "user.email", "test@example.com"], str(repo))
    _git(["config", "user.name", "Test"], str(repo))
    _git(["config", "commit.gpgsign", "false"], str(repo))
    return repo
