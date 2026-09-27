import pytest
import requests

from sonar.adapters import SonarPreflightError
from sonar.tools import sonar_tools


# --- _parse_effort_minutes ---------------------------------------------------

@pytest.mark.parametrize("effort,expected", [
    (None, 0), ("", 0), ("5min", 5), ("1h30min", 90), ("2d", 960), ("1d", 480), ("10min", 10),
])
def test_parse_effort_minutes(effort, expected):
    assert sonar_tools._parse_effort_minutes(effort) == expected


# --- dashboard_url ------------------------------------------------------

def test_dashboard_url_builds_expected_link():
    url = sonar_tools.dashboard_url("http://sonar.example.com", "my:proj", "my-proj_agent_20260101_120000")
    assert url == (
        "http://sonar.example.com/dashboard?id=my%3Aproj&branch=my-proj_agent_20260101_120000"
    )


def test_dashboard_url_strips_trailing_slash_on_base():
    url = sonar_tools.dashboard_url("http://sonar.example.com/", "proj", "main")
    assert url == "http://sonar.example.com/dashboard?id=proj&branch=main"


def test_dashboard_url_encodes_special_characters_in_branch():
    url = sonar_tools.dashboard_url("http://sonar", "proj", "feature/foo bar")
    assert url == "http://sonar/dashboard?id=proj&branch=feature%2Ffoo%20bar"


# --- _component_path ---------------------------------------------------------

def test_component_path_strips_project_prefix():
    assert sonar_tools._component_path("myproj:src/Main.java", "myproj") == "src/Main.java"


def test_component_path_leaves_unprefixed_key_unchanged():
    assert sonar_tools._component_path("other:src/Main.java", "myproj") == "other:src/Main.java"


# --- classify_issue: SECURITY/RELIABILITY worst-issue gating ---------------

def _issue(category, severity=None, impact_severity=None, vuln_prob=None):
    d = {"category": category}
    if impact_severity is not None:
        d["impact_severities"] = impact_severity
    if severity is not None:
        d["severity"] = severity
    if vuln_prob is not None:
        d["vulnerability_probability"] = vuln_prob
    return d


def test_classify_legacy_security_critical_is_autofix():
    c = sonar_tools.classify_issue(_issue("SECURITY", severity="CRITICAL"))
    assert c == {"in_scope": True, "action": "autofix", "rank": (0, 0)}


def test_classify_legacy_security_minor_is_review_not_autofix():
    c = sonar_tools.classify_issue(_issue("SECURITY", severity="MINOR"))
    assert c["in_scope"] is True
    assert c["action"] == "review_wont_fix"


def test_classify_clean_code_security_low_is_review():
    c = sonar_tools.classify_issue(_issue("SECURITY", impact_severity="LOW"))
    assert c["action"] == "review_wont_fix"


def test_classify_maintainability_minor_out_of_scope():
    # MAINTAINABILITY's floor stops at MAJOR/MEDIUM -- MINOR/LOW never in scope
    c = sonar_tools.classify_issue(_issue("MAINTAINABILITY", severity="MINOR"))
    assert c == {"in_scope": False, "action": None, "rank": None}


def test_classify_maintainability_critical_is_autofix():
    c = sonar_tools.classify_issue(_issue("MAINTAINABILITY", severity="CRITICAL"))
    assert c["in_scope"] is True
    assert c["action"] == "autofix"


def test_classify_hotspot_high_probability_in_scope():
    c = sonar_tools.classify_issue(_issue("HOTSPOT", vuln_prob="HIGH"))
    assert c["in_scope"] is True
    assert c["action"] == "autofix"


def test_classify_hotspot_low_probability_out_of_scope():
    c = sonar_tools.classify_issue(_issue("HOTSPOT", vuln_prob="LOW"))
    assert c == {"in_scope": False, "action": None, "rank": None}


def test_classify_unknown_category_out_of_scope():
    c = sonar_tools.classify_issue(_issue("SOMETHING_ELSE", severity="CRITICAL"))
    assert c == {"in_scope": False, "action": None, "rank": None}


def test_classify_info_severity_out_of_scope():
    c = sonar_tools.classify_issue(_issue("SECURITY", severity="INFO"))
    assert c == {"in_scope": False, "action": None, "rank": None}


# --- partition_and_prioritize -----------------------------------------------

def test_partition_groups_autofix_by_file_and_orders_by_priority():
    issues = [
        {"category": "MAINTAINABILITY", "severity": "CRITICAL", "component_path": "Low.java", "issue_key": "m1"},
        {"category": "SECURITY", "severity": "CRITICAL", "component_path": "High.java", "issue_key": "s1"},
    ]
    file_groups, review_queue = sonar_tools.partition_and_prioritize(issues)
    assert review_queue == []
    assert [g["file"] for g in file_groups] == ["High.java", "Low.java"]  # SECURITY ranks first


def test_partition_review_lane_kept_separate_and_flat():
    issues = [
        {"category": "SECURITY", "severity": "MINOR", "component_path": "A.java", "issue_key": "a1"},
        {"category": "SECURITY", "severity": "CRITICAL", "component_path": "A.java", "issue_key": "a2"},
    ]
    file_groups, review_queue = sonar_tools.partition_and_prioritize(issues)
    assert len(review_queue) == 1
    assert review_queue[0]["issue_key"] == "a1"
    assert len(file_groups) == 1
    assert [i["issue_key"] for i in file_groups[0]["issues"]] == ["a2"]


def test_partition_drops_out_of_scope_issues_entirely():
    issues = [{"category": "MAINTAINABILITY", "severity": "MINOR", "component_path": "A.java", "issue_key": "a1"}]
    file_groups, review_queue = sonar_tools.partition_and_prioritize(issues)
    assert file_groups == []
    assert review_queue == []


def test_partition_ranks_hotspot_ahead_of_maintainability():
    """Security Hotspots block the quality gate the same way an unfixed
    vulnerability does; Maintainability is a debt ratio that tolerates
    partial progress. This ordering applies unconditionally, regardless
    of how many issues are in the batch."""
    issues = [
        {"category": "HOTSPOT", "vulnerability_probability": "HIGH", "component_path": "Spot.java", "issue_key": "h1"},
        {"category": "MAINTAINABILITY", "severity": "CRITICAL", "component_path": "Debt.java", "issue_key": "m1"},
    ]
    file_groups, _ = sonar_tools.partition_and_prioritize(issues)
    assert [g["file"] for g in file_groups] == ["Spot.java", "Debt.java"]


def test_partition_ranks_all_four_categories_in_order():
    """Security, then Reliability, then Security Hotspots, then
    Maintainability -- every file whose worst issue is in an earlier
    category sorts before every file whose worst issue is in a later one."""
    issues = [
        {"category": "MAINTAINABILITY", "severity": "CRITICAL", "component_path": "Debt.java", "issue_key": "m1"},
        {"category": "HOTSPOT", "vulnerability_probability": "HIGH", "component_path": "Spot.java", "issue_key": "h1"},
        {"category": "RELIABILITY", "severity": "CRITICAL", "component_path": "Rel.java", "issue_key": "r1"},
        {"category": "SECURITY", "severity": "CRITICAL", "component_path": "Sec.java", "issue_key": "s1"},
    ]
    file_groups, _ = sonar_tools.partition_and_prioritize(issues)
    assert [g["file"] for g in file_groups] == ["Sec.java", "Rel.java", "Spot.java", "Debt.java"]


# --- validate_connection / check_project_analyzed (mocked HTTP) ------------

class _FakeResponse:
    def __init__(self, json_data, status_code=200, headers=None):
        self._json = json_data
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"{self.status_code} error")
            err.response = self
            raise err

    def json(self):
        return self._json


def test_validate_connection_ok(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({"valid": True}))
    sonar_tools.validate_connection("http://localhost:9000", "good-token")  # no raise


def test_validate_connection_bad_token_raises(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({"valid": False}))
    with pytest.raises(SonarPreflightError, match="rejected"):
        sonar_tools.validate_connection("http://localhost:9000", "bad-token")


def test_validate_connection_unreachable_raises(monkeypatch):
    def raise_conn_error(*a, **kw):
        raise requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(requests, "get", raise_conn_error)
    with pytest.raises(SonarPreflightError, match="Could not reach"):
        sonar_tools.validate_connection("http://localhost:9000", "token")


def test_check_project_analyzed_ok(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({"analyses": [{"key": "x"}]}))
    sonar_tools.check_project_analyzed("http://localhost:9000", "my-key", "token")  # no raise


def test_check_project_analyzed_404_treated_as_never_scanned(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({}, status_code=404))
    with pytest.raises(SonarPreflightError, match="no analysis"):
        sonar_tools.check_project_analyzed("http://localhost:9000", "my-key", "token")


def test_check_project_analyzed_empty_analyses_raises(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({"analyses": []}))
    with pytest.raises(SonarPreflightError, match="no analysis"):
        sonar_tools.check_project_analyzed("http://localhost:9000", "my-key", "token")


def test_check_project_analyzed_403_raises_permission_error(monkeypatch):
    """A valid, server-authenticated token can still lack Browse permission
    on one specific project -- this must be reported as a permission
    problem, not the misleading "run an initial scan" message a 404 gets."""
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({}, status_code=403))
    with pytest.raises(SonarPreflightError, match="permission"):
        sonar_tools.check_project_analyzed("http://localhost:9000", "my-key", "token")


def test_check_project_analyzed_404_message_mentions_permission_too(monkeypatch):
    """Some Sonar versions/endpoints return 404, not 403, for a permission
    problem too (same not-found-vs-no-access ambiguity GitHub has) -- the
    message must not claim with false confidence that this is definitely
    "never analyzed" when it could just as well be a permission issue."""
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({}, status_code=404))
    with pytest.raises(SonarPreflightError, match="permission"):
        sonar_tools.check_project_analyzed("http://localhost:9000", "my-key", "token")


# --- _sonar_get: rate limiting ----------------------------------------------

def test_sonar_get_429_raises_clear_rate_limit_error(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({}, status_code=429))
    with pytest.raises(RuntimeError, match="rate-limited"):
        sonar_tools._sonar_get("http://localhost:9000", "/api/issues/search", "token", {})


def test_sonar_get_429_includes_retry_after_when_present(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda *a, **kw: _FakeResponse({}, status_code=429, headers={"Retry-After": "30"})
    )
    with pytest.raises(RuntimeError, match="Retry after 30s"):
        sonar_tools._sonar_get("http://localhost:9000", "/api/issues/search", "token", {})


# --- _normalize_issue / _normalize_hotspot: malformed responses ------------

def test_normalize_issue_missing_key_raises_clear_error():
    with pytest.raises(RuntimeError, match="missing field 'key'"):
        sonar_tools._normalize_issue({"rule": "java:S1234", "component": "proj:A.java"}, "proj")


def test_normalize_hotspot_missing_component_raises_clear_error():
    with pytest.raises(RuntimeError, match="missing field 'component'"):
        sonar_tools._normalize_hotspot({"key": "h1"}, "proj")


# --- get_maintainability_debt_ratio ------------------------------------

def test_get_maintainability_debt_ratio_branch_none_omits_branch_param(monkeypatch):
    """Regression: a freshly created agent branch with zero commits has
    never been Sonar-analyzed under its own name (that only happens once
    a checkpoint fires a re-scan, which needs at least one committed
    file first) -- querying its debt ratio crashed the whole pipeline
    with an unhandled RuntimeError on any run that found zero files to
    fix. branch=None (falling back to the project's default branch,
    which the new branch is still byte-identical to at that point) is
    the fix -- this confirms requests actually omits a None-valued query
    param rather than sending the literal string "None", which is what
    that fallback depends on."""
    captured = {}

    def fake_get(url, headers, params, timeout):
        captured["params"] = params
        return _FakeResponse({"component": {"measures": [{"metric": "sqale_debt_ratio", "value": "2.5"}]}})

    monkeypatch.setattr(requests, "get", fake_get)
    ratio = sonar_tools.get_maintainability_debt_ratio("http://localhost:9000", "my-key", "token", None)
    assert ratio == 2.5
    assert captured["params"]["branch"] is None  # requests itself drops a None param from the URL


def test_get_maintainability_debt_ratio_raises_when_metric_missing(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({"component": {"measures": []}}))
    with pytest.raises(RuntimeError, match="sqale_debt_ratio not returned"):
        sonar_tools.get_maintainability_debt_ratio("http://localhost:9000", "my-key", "token", "some-branch")


# --- branch_exists -------------------------------------------------------

def test_branch_exists_true_when_branch_is_in_the_list(monkeypatch):
    """Regression: exact live bug (be-exps-portal) -- whether the agent's
    own branch actually got created server-side depends on the resolved
    Sonar scanner plugin, not on the ce_edition flag (see this function's
    docstring). Asking the server directly via /api/project_branches/list
    is what _scanned_branch (agents/maintainability.py) now relies on
    instead of guessing."""
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({
        "branches": [{"name": "main"}, {"name": "my-project_agent_20260101_000000"}],
    }))
    assert sonar_tools.branch_exists(
        "http://localhost:9000", "my-key", "my-project_agent_20260101_000000", "token",
    ) is True


def test_branch_exists_false_when_branch_never_got_created(monkeypatch):
    """Regression: exact live bug (WebGoat, Maven-built) -- a run that
    committed files fine but whose scanner never created a distinct
    branch server-side; only 'main' shows up."""
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({"branches": [{"name": "main"}]}))
    assert sonar_tools.branch_exists(
        "http://localhost:9000", "my-key", "my-project_agent_20260101_000000", "token",
    ) is False


# --- fetch_uncovered_files / fetch_duplicated_files -------------------------
# Regression coverage for a real bug: both used to swallow every exception
# from the component_tree fetch and return [], making a genuine Sonar
# failure (network blip, permissions, schema drift) indistinguishable from
# "this project has 100% coverage / zero duplication." A real failure must
# now raise, not silently report "nothing to do."

def test_fetch_uncovered_files_empty_components_is_not_an_error(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({"components": []}))
    assert sonar_tools.fetch_uncovered_files("http://localhost:9000", "my-key", "token", None) == []


def test_fetch_uncovered_files_raises_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({}, status_code=500))
    with pytest.raises(RuntimeError, match="Failed to fetch coverage data"):
        sonar_tools.fetch_uncovered_files("http://localhost:9000", "my-key", "token", None)


def test_fetch_uncovered_files_raises_on_connection_error(monkeypatch):
    def raise_conn_error(*a, **kw):
        raise requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(requests, "get", raise_conn_error)
    with pytest.raises(RuntimeError, match="Failed to fetch coverage data"):
        sonar_tools.fetch_uncovered_files("http://localhost:9000", "my-key", "token", None)


def test_fetch_duplicated_files_empty_components_is_not_an_error(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({"components": []}))
    assert sonar_tools.fetch_duplicated_files("http://localhost:9000", "my-key", "token", None) == []


def test_fetch_duplicated_files_raises_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse({}, status_code=500))
    with pytest.raises(RuntimeError, match="Failed to fetch duplication data"):
        sonar_tools.fetch_duplicated_files("http://localhost:9000", "my-key", "token", None)
