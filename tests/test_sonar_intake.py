from types import SimpleNamespace

from sonar.intake import set_analysis_source


def _ctx():
    return SimpleNamespace(state={})


def test_set_analysis_source_stores_branch_when_given():
    ctx = _ctx()
    result = set_analysis_source("github", "owner/repo", ctx, source_branch="develop")
    assert result["source_branch"] == "develop"
    assert ctx.state["source_branch"] == "develop"


def test_set_analysis_source_strips_whitespace_from_branch():
    ctx = _ctx()
    set_analysis_source("github", "owner/repo", ctx, source_branch="  release/v2  ")
    assert ctx.state["source_branch"] == "release/v2"


def test_set_analysis_source_defaults_branch_to_none_when_omitted():
    ctx = _ctx()
    result = set_analysis_source("github", "owner/repo", ctx)
    assert ctx.state["source_branch"] is None
    assert result["source_branch"] == "(default branch)"


def test_set_analysis_source_treats_empty_branch_string_as_none():
    ctx = _ctx()
    set_analysis_source("github", "owner/repo", ctx, source_branch="")
    assert ctx.state["source_branch"] is None
