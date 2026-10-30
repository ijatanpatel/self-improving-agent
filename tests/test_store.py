from loop.contract import RuleResult
from loop.store import IssueStore

FINDING = {
    "rule_id": "required_span:input_guardrail", "title": "Email skips the guardrail",
    "severity": "critical", "summary": "s", "root_cause": "r",
    "evidence": [{"trace_id": "t1", "observation": "o"}],
    "code_locations": [{"path": "support_bot/channels.py", "line": 1, "why": "w"}],
    "proposed_fix": ["call the guardrail"], "related_rules": [],
    "evaluator_idea": {"name": "n", "criteria": "c", "evaluation_params": ["input"]},
}


def rule(failed: bool) -> RuleResult:
    return RuleResult("required_span:input_guardrail", "required_span", "", "critical",
                      checked=20, violations=["t1"] if failed else [])


def test_reruns_update_instead_of_duplicating(tmp_path):
    store = IssueStore(tmp_path / "issues.db")
    assert store.upsert_finding("app", FINDING, rule(True), "run-1") == "new"
    reworded = {**FINDING, "title": "Guardrail is bypassed for emails"}  # agents reword titles
    assert store.upsert_finding("app", reworded, rule(True), "run-2") == "updated"
    [issue] = store.list_issues()
    assert issue["times_seen"] == 2 and issue["title"] == "Guardrail is bypassed for emails"


def test_issues_resolve_when_the_rule_passes_and_reopen_if_it_breaks_again(tmp_path):
    store = IssueStore(tmp_path / "issues.db")
    store.upsert_finding("app", FINDING, rule(True), "run-1")
    resolved = store.resolve_passing([rule(False)])
    assert [i["id"] for i in resolved] == [1]
    assert store.get_issue(1)["status"] == "resolved"
    assert store.upsert_finding("app", FINDING, rule(True), "run-3") == "regressed"
    issue = store.get_issue(1)
    assert issue["status"] == "open" and issue["times_regressed"] == 1


def test_each_exit_is_recorded_once(tmp_path):
    store = IssueStore(tmp_path / "issues.db")
    store.upsert_finding("app", FINDING, rule(True), "run-1")
    store.record_exit(1, "pr", "branch-a")
    store.record_exit(1, "pr", "branch-b")
    assert [e["target"] for e in store.list_exits(1)] == ["branch-b"]
