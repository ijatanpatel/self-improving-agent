"""The investigation agent's findings are only saved if the evidence checks out."""
from loop.contract import check_contract, load_contract
from loop.investigate import validate_findings
from loop.settings import REPO_ROOT
from loop.skills import InvestigationSkills, Snapshot
from loop.workspace import Workspace


def snapshot(request_factory) -> Snapshot:
    requests = [request_factory(f"ok{i}") for i in range(5)]
    requests += [request_factory(f"email{i}", spans=("retrieve", "generate_answer"), app__channel="email")
                 for i in range(3)]
    contract = load_contract()
    return Snapshot(contract, requests, check_contract(contract, requests), hours=24)


def finding(**overrides) -> dict:
    issue = {
        "rule_id": "required_span:input_guardrail", "title": "t", "severity": "critical",
        "summary": "s", "root_cause": "r",
        "evidence": [{"trace_id": "email0", "observation": "no guardrail"},
                     {"trace_id": "email1", "observation": "no guardrail"}],
        "code_locations": [{"path": "support_bot/channels.py", "line": 10, "why": "w"}],
        "proposed_fix": ["run the guardrail for email too"],
        "evaluator_idea": {"name": "n", "criteria": "c", "evaluation_params": ["input"]},
        "related_rules": [],
    }
    return {"issues": [{**issue, **overrides}]}


def test_valid_findings_pass(request_factory):
    assert validate_findings(finding(), snapshot(request_factory), Workspace(REPO_ROOT)) is None


def test_made_up_or_unrelated_traces_are_rejected(request_factory):
    snap, ws = snapshot(request_factory), Workspace(REPO_ROOT)
    made_up = finding(evidence=[{"trace_id": "nope", "observation": "o"},
                                {"trace_id": "email1", "observation": "o"}])
    assert "does not exist" in validate_findings(made_up, snap, ws)
    unrelated = finding(evidence=[{"trace_id": "ok0", "observation": "o"},
                                  {"trace_id": "email1", "observation": "o"}])
    assert "did not break" in validate_findings(unrelated, snap, ws)


def test_wrong_code_lines_are_rejected(request_factory):
    bad = finding(code_locations=[{"path": "support_bot/channels.py", "line": 9999, "why": "w"}])
    assert "line 9999 is wrong" in validate_findings(bad, snapshot(request_factory), Workspace(REPO_ROOT))


def test_group_by_shows_the_pattern(request_factory):
    skills = InvestigationSkills(snapshot(request_factory), Workspace(REPO_ROOT))
    table = skills.query_traces("required_span:input_guardrail", None, "app.channel", None)
    assert "email | 3 | 3 | 100%" in table
    assert "web | 0 | 5 | 0%" in table
