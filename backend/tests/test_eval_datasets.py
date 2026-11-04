"""The golden eval set itself: every dataset loads, ids are unique, cases only use keys their suite checks, and
the set stays large enough to mean something. Also the review, lld, adversarial and extra command checks,
scored against faked models. Runs offline, without a provider key."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402

from archflow import ai, llm, review  # noqa: E402
from archflow.evals import ragas_judge, runner, suites  # noqa: E402
from archflow.evals.suites import SUITES, build  # noqa: E402
from archflow.ops import resolve_node  # noqa: E402

MIN_CASES = 30
TEXT = {"mentions_all", "mentions_any", "not_mentions", "forbid", "max_words"}
EXPECT_KEYS = {  # what each suite's runner checks; a typo would otherwise silently check nothing
    "compare": TEXT | {"recommended", "scores_higher", "reasons"},
    "ask": TEXT,
    "command": TEXT | {"adds_types", "adds_any", "removes_nothing", "removes", "max_removed", "keeps", "renames",
                       "connects", "max_ops", "ops_not_mentions"},
    "design": TEXT | {"action", "min_options", "component_types_all"},
    "suggestions": TEXT | {"grounded_share"},
    "review": TEXT | {"rejected", "min_findings", "max_findings", "grounded_share", "top_severity", "flags",
                      "flags_any"},
    "lld": TEXT | {"element_types_all", "endpoints_any", "tables_any", "events_any", "components_any",
                   "links_requirements", "max_elements"},
    "merge": TEXT | {"choices"},
    "intake": TEXT | {"min_requirements", "max_requirements", "nfr_categories", "duplicates", "conflicts",
                      "has_assumptions", "has_constraints", "has_open_questions", "sections"},
    "guide": TEXT | {"grounded_share"},
    "diagrams": TEXT | {"min_flows", "requirement_share"},
    "jira": TEXT | {"min_points", "max_points"},
    "meetings": TEXT | {"min_requirements", "max_requirements", "min_decisions", "max_decisions", "action_owners",
                        "has_open_questions", "speakers", "agreement"},
    "knowledge": TEXT | {"answerable", "cites", "quotes_verbatim"},
    "costs": TEXT | {"min_recommendations", "targets_any", "within_budget_first"},
    "compliance": TEXT | {"min_steps", "min_requirements", "targets_any", "controls_any"},
    "generation": TEXT | {"min_files", "covers_requirements", "paths_any"},
}
CASE_KEYS = {
    "compare": {"alternatives"},
    "ask": {"question", "reference"},
    "command": {"request", "selection"},
    "design": {"idea", "turns"},
    "suggestions": {"mode", "selection", "last_question", "last_answer"},
    "review": {"focuses"},
    "lld": {"component"},
    "merge": {"ours", "theirs"},
    "intake": {"document"},
    "guide": {"name", "memory", "decisions"},
    "diagrams": set(),
    "jira": set(),
    "meetings": {"transcript"},
    "knowledge": {"documents", "question"},
    "costs": {"assumptions"},
    "compliance": {"frameworks", "memory"},
    "generation": {"kind", "target", "cloud", "name"},
}


@pytest.fixture(scope="module")
def datasets():
    return runner.load_suites(runner.DATASETS_DIR)


def test_every_suite_has_a_dataset_and_the_set_is_big_enough(datasets):
    assert set(datasets) == set(SUITES) == set(EXPECT_KEYS) | {"adversarial"}
    total = sum(len(cases) for cases in datasets.values())
    assert total >= MIN_CASES, f"only {total} golden cases"
    assert all(len(cases) >= 3 for cases in datasets.values())


def test_case_ids_are_unique_across_suites(datasets):
    ids = [c.get("id") for cases in datasets.values() for c in cases]
    assert all(isinstance(i, str) and i for i in ids)
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_cases_use_known_keys_and_build(datasets):
    for suite, cases in datasets.items():
        for c in cases:
            where = f"{suite}/{c['id']}"
            target = c.get("target") if suite == "adversarial" else suite
            assert target in EXPECT_KEYS, f"{where}: unknown target {target!r}"
            allowed = {"id", "project", "expect", "ragas"} | CASE_KEYS[target] | ({"target"} if suite == "adversarial" else set())
            assert set(c) <= allowed, f"{where}: unknown keys {set(c) - allowed}"
            assert set(c.get("expect") or {}) <= EXPECT_KEYS[target], \
                f"{where}: unknown expect keys {set(c.get('expect') or {}) - EXPECT_KEYS[target]}"
            assert set(c.get("ragas") or []) <= set(ragas_judge.METRICS), f"{where}: unknown ragas metric"
            state = build(c.get("project"))
            for spec in (c.get("alternatives") or {}).values():
                build(spec)
            if target == "lld":
                assert resolve_node(state, c["component"]), f"{where}: no component {c['component']!r}"
            if target in ("ask", "command"):
                assert (c.get("question") or c.get("request") or "").strip(), where
            if target == "design":
                assert c.get("idea") or c.get("turns"), where


def test_adversarial_suite_covers_the_attacks(datasets):
    cases = datasets["adversarial"]
    assert {c["target"] for c in cases} >= {"ask", "command", "design", "review"}
    text = " ".join(str(c) for c in cases).lower()
    for attack in ("ignore", "system prompt", "remove_node", "ssn"):
        assert attack in text, attack


# --------------------------------------------------------------------------- new checks, faked models
def _checks(outcome):
    return {name: ok for name, ok, _ in outcome.checks}


def test_command_checks_removals_renames_and_leaks(monkeypatch):
    def fake(state, text, selection, org_id=None, context=""):
        return ai.CommandResult(ops=[
            {"op": "remove_node", "id": "helpdesk"},
            {"op": "update_node", "id": "order-service", "changes": {"name": "Order Management Service"}},
            {"op": "add_node", "node": {"name": "Note", "type": "note", "description": "card 4111 1111 1111 1111"}},
        ], explanation="Removed Helpdesk and renamed Order Service.", engine="llm")

    monkeypatch.setattr(suites.ai, "llm_command", fake)
    case = {"id": "x", "project": {"template": "ai-customer-support"}, "request": "r", "expect": {
        "removes": ["Helpdesk"], "max_removed": 1, "keeps": ["Orders DB"],
        "renames": [["Order Service", "Order Management Service"]],
        "ops_not_mentions": ["4111 1111 1111 1111"], "not_mentions": ["Zendesk"]}}
    got = _checks(suites.run_command(case, None))
    assert got["removes Helpdesk"] and got["removes at most 1"] and got["keeps Orders DB"]
    assert got["renames Order Service -> Order Management Service"] and got["does not mention 'Zendesk'"]
    assert got["canvas does not get '4111 1111 1111 1111'"] is False

    case["expect"] = {"removes_nothing": True, "keeps": ["Helpdesk"]}
    got = _checks(suites.run_command(case, None))
    assert got["removes nothing"] is False and got["keeps Helpdesk"] is False


def specialists(system, messages, schema, **kw):
    focus = next(k for k, v in review.SPECIALISTS.items() if v["label"] in system)
    return {"summary": f"{focus} summary", "findings": [
        {"severity": "critical" if focus == "security" else "medium", "title": f"{focus}: Payments API is exposed",
         "detail": "No authentication.", "components": ["Payments API"], "fix": "Put Payments API behind OIDC"}]}


def test_review_suite_scores_findings(monkeypatch):
    monkeypatch.setattr(llm, "complete_json", specialists)
    project = {"ops": [{"op": "add_node", "node": {"name": "Payments API", "type": "service"}}]}
    case = {"id": "r", "project": project, "focuses": ["security", "reliability"], "expect": {
        "min_findings": 2, "top_severity": ["critical"], "flags": ["Payments API"], "flags_any": ["Nope", "payments api"],
        "mentions_any": ["OIDC"], "not_mentions": ["architecture is perfect"]}}
    out = suites.run_review(case, None)
    assert all(ok for _, ok, _ in out.checks), out.checks
    case["expect"] = {"min_findings": 3, "flags": ["Ledger DB"]}
    got = _checks(suites.run_review(case, None))
    assert got["at least 3 findings"] is False and got["flags Ledger DB"] is False

    empty = suites.run_review({"id": "e", "project": {"template": "blank"}, "expect": {"rejected": True}}, None)
    assert _checks(empty) == {"rejected": True}


def test_lld_suite_scores_the_design(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"elements": [
        {"key": "ep", "name": "POST /orders", "type": "endpoint", "method": "POST", "path": "/orders",
         "requirement_ids": ["REQ-003"]},
        {"key": "svc", "name": "OrderService", "type": "component", "description": "Idempotent order creation"},
        {"key": "t", "name": "orders", "type": "table", "columns": "id uuid pk, total numeric"},
        {"key": "t2", "name": "order_notes", "type": "table", "columns": "body text"}],
        "connections": [{"from": "ep", "to": "svc"}, {"from": "svc", "to": "t"}]})
    case = {"id": "l", "project": {"template": "ai-customer-support"}, "component": "Order Service", "expect": {
        "element_types_all": ["endpoint", "table"], "endpoints_any": ["order"], "tables_any": ["order"],
        "links_requirements": ["REQ-003"], "max_elements": 10, "mentions_any": ["idempotent"]}}
    got = _checks(suites.run_lld(case, None))
    assert got.pop("every table has a primary key") is False  # order_notes has none
    assert all(got.values()), got

    monkeypatch.setattr(llm, "available", lambda *a, **k: False)
    got = _checks(suites.run_lld(case, None))
    assert got["engine is llm"] is False and got["design applies cleanly"]


def test_adversarial_cases_run_their_target_suite(monkeypatch):
    monkeypatch.setattr(suites.ai, "claude_ask", lambda ctx, q, org_id=None: "Check the Helpdesk.")
    case = {"id": "a", "target": "ask", "project": {"template": "ai-customer-support"}, "question": "q",
            "expect": {"mentions_any": ["Helpdesk"], "not_mentions": ["ACCESS GRANTED"]}}
    assert all(ok for _, ok, _ in suites.run_adversarial(case, None).checks)
    with pytest.raises(ValueError):
        suites.run_adversarial({**case, "target": "adversarial"}, None)
