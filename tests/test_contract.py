import pytest

from loop.contract import (Condition, ContractError, HealthContract, check_contract, load_contract,
                           percentile)


def contract(**overrides) -> HealthContract:
    base = {
        "app": "test", "phoenix_project": "test", "root_span": "support_request",
        "required_spans": [{"span": "input_guardrail"},
                           {"span": "retrieve", "when": "answer.blocked == false"}],
        "invariants": [{"id": "grounded", "description": "", "when": "answer.answered == true",
                        "require": "eval.faithfulness >= 0.7", "max_violation_rate": 0.25}],
        "latency_slo": {"span": "support_request", "percentile": 95, "max_ms": 5000},
    }
    return HealthContract(**{**base, **overrides})


def by_id(results):
    return {r.rule_id: r for r in results}


def test_conditions_compare_numbers_strings_and_booleans():
    fields = {"eval.faithfulness": 0.8, "answer.confidence": "high", "answer.blocked": False}
    assert Condition.parse("eval.faithfulness >= 0.7").test(fields) is True
    assert Condition.parse("answer.confidence == 'high'").test(fields) is True
    assert Condition.parse("answer.blocked == true").test(fields) is False
    assert Condition.parse("eval.missing > 0").test(fields) is None  # no data, no verdict


def test_bad_conditions_are_rejected():
    with pytest.raises(ContractError):
        Condition.parse("eval.faithfulness is good")
    with pytest.raises(ContractError):
        Condition.parse("answer.confidence == high")  # strings must be quoted


def test_missing_required_span_is_a_violation(request_factory):
    requests = [request_factory("a"), request_factory("b", spans=("retrieve", "generate_answer"))]
    result = by_id(check_contract(contract(), requests))["required_span:input_guardrail"]
    assert result.violations == ["b"] and result.failed


def test_when_clause_limits_where_a_rule_applies(request_factory):
    blocked = request_factory("a", spans=("input_guardrail",), answer__blocked=True)
    result = by_id(check_contract(contract(), [blocked]))["required_span:retrieve"]
    assert result.checked == 0 and not result.failed


def test_error_budget(request_factory):
    good = [request_factory(str(i), evals={"faithfulness": 0.9}) for i in range(3)]
    bad = request_factory("bad", evals={"faithfulness": 0.2})
    unscored = request_factory("new")
    result = by_id(check_contract(contract(), [*good, bad, unscored]))["invariant:grounded"]
    assert result.violations == ["bad"]
    assert result.skipped == 1
    assert result.rate == 0.25 and not result.failed  # exactly at the 25% budget
    result = by_id(check_contract(contract(), [*good[:1], bad, unscored]))["invariant:grounded"]
    assert result.failed  # 50% > 25%


def test_latency_slo_uses_the_percentile(request_factory):
    fast = [request_factory(str(i), duration_ms=1000) for i in range(19)]
    slow = request_factory("slow", duration_ms=9000)
    result = by_id(check_contract(contract(), [*fast, slow]))["latency_slo:support_request"]
    assert not result.failed  # one slow request does not move p95
    result = by_id(check_contract(contract(), [*fast[:10], *[request_factory(f"s{i}", duration_ms=9000)
                                                             for i in range(5)]]))["latency_slo:support_request"]
    assert result.failed and len(result.violations) == 5


def test_percentile_nearest_rank():
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert percentile([1, 2, 3, 4], 50) == 2


def test_the_real_contract_loads():
    loaded = load_contract()
    assert loaded.root_span == "support_request"
    assert loaded.required_spans and loaded.invariants and loaded.latency_slo
