"""Requirement soft delete / restore / purge, history with field diffs, and stale component links."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from authhelp import authed_client, member_client  # noqa: E402

from archflow import ai, collab_sync, evaluation, lld  # noqa: E402
from archflow.analysis import full_analysis, stale_links  # noqa: E402
from archflow.change_spec import generate as change_spec  # noqa: E402
from archflow.history import actor_kind, requirement_changes  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState, active_requirements, without_deleted  # noqa: E402
from archflow.ops import OpError, apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402
from archflow.vibe import build_prompt  # noqa: E402

ai.MODE = "offline"
H = {"x-internal-token": collab_sync.INTERNAL_TOKEN}


def base() -> ProjectState:
    s, _ = apply_ops(ProjectState(), [
        {"op": "add_requirement", "requirement": {"title": "Customers place orders", "actors": ["Customer"],
                                                  "acceptance_criteria": ["Order is stored"]}},
        {"op": "add_requirement", "requirement": {"title": "Users log in with SSO", "description": "Sign in via OAuth"}},
        {"op": "add_node", "node": {"name": "Order Service", "type": "service", "requirement_ids": ["REQ-001", "REQ-002"]}},
        {"op": "add_node", "node": {"name": "Orders DB", "type": "database", "requirement_ids": ["REQ-001"]}},
        {"op": "add_edge", "edge": {"source": "order-service", "target": "orders-db"}},
    ])
    return s


# --------------------------------------------------------------------------- soft delete, restore, purge
def test_remove_is_a_soft_delete_and_restore_undoes_it():
    s, summary = apply_ops(base(), [{"op": "remove_requirement", "id": "REQ-002"}])
    req = s.requirements["REQ-002"]
    assert req.deleted and req.field_revs["deleted"] == s.rev and summary == ["removed REQ-002 'Users log in with SSO'"]
    assert s.nodes["order-service"].requirement_ids == ["REQ-001", "REQ-002"]  # links kept
    assert list(active_requirements(s)) == ["REQ-001"]
    assert apply_ops(s, [{"op": "remove_requirement", "id": "REQ-002"}])[1] == []  # idempotent

    s, summary = apply_ops(s, [{"op": "restore_requirement", "id": "REQ-002"}])
    assert not s.requirements["REQ-002"].deleted and summary == ["restored REQ-002 'Users log in with SSO'"]
    assert s.nodes["order-service"].requirement_ids == ["REQ-001", "REQ-002"]


def test_purge_needs_a_deleted_requirement_and_drops_links():
    with pytest.raises(OpError, match="not deleted"):
        apply_ops(base(), [{"op": "purge_requirement", "id": "REQ-001"}])
    s, _ = apply_ops(base(), [{"op": "remove_requirement", "id": "REQ-001"}])
    s, summary = apply_ops(s, [{"op": "purge_requirement", "id": "REQ-001"}])
    assert "REQ-001" not in s.requirements and summary == ["permanently removed REQ-001 'Customers place orders'"]
    assert s.nodes["order-service"].requirement_ids == ["REQ-002"] and s.nodes["orders-db"].requirement_ids == []
    # the next new requirement does not reuse a soft-deleted id
    s, _ = apply_ops(s, [{"op": "remove_requirement", "id": "REQ-002"}, {"op": "add_requirement", "requirement": {"title": "X"}}])
    assert "REQ-003" in s.requirements


def test_a_purged_id_is_never_issued_again():
    """History rows and Jira/GitHub links are keyed by requirement id: reusing a purged id would hand them to an
    unrelated new requirement. Purging the HIGHEST id is the case that used to reuse it."""
    from archflow import decisions
    from archflow.merge import merge_states

    s, _ = apply_ops(base(), [{"op": "remove_requirement", "id": "REQ-002"}])
    s, _ = apply_ops(s, [{"op": "purge_requirement", "id": "REQ-002"}])
    assert "REQ-002" not in s.requirements and s.requirement_seq == 2
    after, _ = apply_ops(s, [{"op": "add_requirement", "requirement": {"title": "New"}}])
    assert "REQ-003" in after.requirements and "REQ-002" not in after.requirements

    # Every path that replaces a branch's state keeps the high-water mark.
    live, _ = collab_sync.sanitize(s.model_dump())  # the Yjs document does not carry requirement_seq
    assert live.requirement_seq == 0
    merged, _ = collab_sync.merge_projection(s, apply_ops(live, [{"op": "add_node", "node": {"name": "X"}}])[0])
    assert merged.requirement_seq == 2
    older = base()  # e.g. restoring or adopting a state from before the purge
    older.requirement_seq = 0
    del older.requirements["REQ-002"]
    assert decisions.with_bumped_revs(older, s).requirement_seq == 2
    result = merge_states(older, s, older)
    assert result.state.requirement_seq == 2


def test_a_project_can_number_requirements_with_its_own_prefix():
    from archflow.merge import merge_states, plan_ops

    b = base()
    s, summary = apply_ops(b, [{"op": "set_requirement_prefix", "prefix": "pay"}])
    assert s.requirement_prefix == "PAY" and summary == ["new requirement ids start with PAY-"]
    with pytest.raises(OpError, match="prefix"):
        apply_ops(s, [{"op": "set_requirement_prefix", "prefix": "9-bad"}])
    s, _ = apply_ops(s, [{"op": "add_requirement", "requirement": {"title": "Refunds"}}])
    assert "PAY-003" in s.requirements and "REQ-001" in s.requirements  # numbering continues, old ids kept
    # a branch that changed the prefix carries it through a merge (and the merge's ops)
    result = merge_states(b, b, s)
    assert result.state.requirement_prefix == "PAY"
    merged, _ = apply_ops(b, plan_ops(b, result.state))
    assert merged.requirement_prefix == "PAY" and "PAY-003" in merged.requirements
    # the live document does not carry the prefix: the branch keeps its own
    live, _ = collab_sync.sanitize(s.model_dump())
    merged, _ = collab_sync.merge_projection(s, apply_ops(live, [{"op": "add_node", "node": {"name": "X"}}])[0])
    assert merged.requirement_prefix == "PAY"


def test_add_edge_with_an_invalid_field_is_a_client_error_not_a_crash():
    with pytest.raises(OpError, match="invalid connection"):
        apply_ops(base(), [{"op": "add_edge", "edge": {"source": "orders-db", "target": "order-service", "protocol": 5}}])


def test_relinking_keeps_links_to_deleted_requirements():
    """Callers (the AI, MCP) only see active requirements; replacing a component's links must not drop the hidden
    link to a deleted one, so a restore brings it back."""
    s, _ = apply_ops(base(), [{"op": "remove_requirement", "id": "REQ-002"}])
    s, _ = apply_ops(s, [{"op": "update_node", "id": "order-service", "changes": {"requirement_ids": ["REQ-001"]}}])
    assert s.nodes["order-service"].requirement_ids == ["REQ-001", "REQ-002"]


def test_consumers_ignore_deleted_requirements():
    s, _ = apply_ops(base(), [{"op": "remove_requirement", "id": "REQ-002"}])
    active = without_deleted(s)
    assert list(active.requirements) == ["REQ-001"] and active.nodes["order-service"].requirement_ids == ["REQ-001"]
    assert without_deleted(base()) is not None and "REQ-002" in s.requirements  # the original is untouched

    a = full_analysis(s)
    assert "REQ-002" not in a["traceability"]["requirement_to_nodes"]
    assert a["health"]["counts"]["requirements"] == 1
    assert not any("REQ-002" in i["requirement_ids"] for i in a["requirement_issues"])
    assert not any(i["code"] == "missing_auth" for i in a["architecture_issues"])  # SSO text is gone with it

    assert "REQ-002" not in ai._state_for_prompt(s) and '"deleted"' not in ai._state_for_prompt(s)
    assert "SSO" not in build_prompt(s, "P", [], [])
    assert "SSO" not in build_prompt(s, "P", [], ["REQ-002"])
    files = change_spec(s, "P", [], [], use_llm=False).files
    assert not any("SSO" in text for text in files.values())
    assert [r["id"] for r in evaluation.requirements_summary(s)] == ["REQ-001"]
    assert list(evaluation.merged_requirements(s, {}).requirements) == ["REQ-001"]
    ops, _ = lld.build(s, "order-service")
    assert not any("REQ-002" in (o.get("node") or {}).get("requirement_ids", []) for o in ops)


# --------------------------------------------------------------------------- history
def test_requirement_changes_diff_fields_and_actions():
    old = base()
    new, _ = apply_ops(old, [
        {"op": "update_requirement", "id": "REQ-001", "changes": {"priority": "high", "acceptance_criteria": ["A", "B"]}},
        {"op": "remove_requirement", "id": "REQ-002"},
        {"op": "add_requirement", "requirement": {"title": "Audit log", "kind": "non_functional"}},
    ])
    out = {e["requirement_id"]: e for e in requirement_changes(old, new)}
    assert out["REQ-001"] == {"requirement_id": "REQ-001", "action": "updated", "changes": [
        {"field": "priority", "before": "medium", "after": "high"},
        {"field": "acceptance_criteria", "before": ["Order is stored"], "after": ["A", "B"]}]}
    assert out["REQ-002"]["action"] == "deleted" and out["REQ-002"]["changes"] == []
    assert out["REQ-003"]["action"] == "added"
    assert {"field": "title", "before": None, "after": "Audit log"} in out["REQ-003"]["changes"]
    restored, _ = apply_ops(new, [{"op": "restore_requirement", "id": "REQ-002"}])
    assert requirement_changes(new, restored)[0]["action"] == "restored"
    purged, _ = apply_ops(new, [{"op": "purge_requirement", "id": "REQ-002"}])
    assert requirement_changes(new, purged) == [{"requirement_id": "REQ-002", "action": "purged", "changes": []}]
    assert requirement_changes(new, new) == []


def test_actor_kind():
    assert actor_kind("AI (Priya)") == "ai"
    assert actor_kind("Priya (design chat)") == "ai"
    assert actor_kind("Priya via MCP") == "agent"
    assert actor_kind("Priya") == actor_kind("Narender, Priya") == "human"


def test_history_api_records_every_path(monkeypatch):
    async def fake_push(*_a, **_k):
        return True

    monkeypatch.setattr(collab_sync, "push_patch", fake_push)
    client = authed_client(create_app(Store(":memory:")))
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    ops = lambda *o: client.post(f"/api/projects/{pid}/ops", json={"ops": list(o)})  # noqa: E731
    assert ops({"op": "add_requirement", "requirement": {"title": "Checkout"}}).status_code == 200
    ops({"op": "update_requirement", "id": "REQ-001", "changes": {"title": "Checkout with cards", "priority": "high"}})

    # a canvas edit arrives as the live document's projection
    state = client.get(f"/api/projects/{pid}").json()["state"]
    state["requirements"]["REQ-001"]["description"] = "Visa and Mastercard"
    client.put(f"/api/internal/projection/{pid}:main", json={"state": state, "actors": ["Priya"]}, headers=H)
    ops({"op": "remove_requirement", "id": "REQ-001"})

    hist = client.get(f"/api/projects/{pid}/requirements/REQ-001/history").json()
    assert [h["action"] for h in hist] == ["deleted", "updated", "updated", "added"]  # newest first
    assert hist[1]["actor"] == "Priya" and hist[1]["changes"] == [
        {"field": "description", "before": "", "after": "Visa and Mastercard"}]
    assert hist[2]["actor"] == "Owner" and hist[2]["actor_kind"] == "human"
    assert {"field": "title", "before": "Checkout", "after": "Checkout with cards"} in hist[2]["changes"]
    assert all(h["rev"] and h["created_at"] for h in hist)
    assert client.get(f"/api/projects/{pid}/requirements/REQ-404/history").status_code == 404

    # deleted: hidden from analysis and tracing, still in the state for the Deleted filter
    assert client.get(f"/api/projects/{pid}/analysis").json()["health"]["counts"]["requirements"] == 0
    assert client.get(f"/api/projects/{pid}/trace/REQ-001").status_code == 404
    assert client.get(f"/api/projects/{pid}").json()["state"]["requirements"]["REQ-001"]["deleted"] is True


def test_purge_is_admin_only():
    app = create_app(Store(":memory:"))
    owner = authed_client(app)
    pid = owner.post("/api/projects", json={"name": "P"}).json()["id"]
    owner.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "add_requirement", "requirement": {"title": "A"}},
                                                         {"op": "remove_requirement", "id": "REQ-001"}]})
    dev = member_client(app, owner, "developer", "dev@example.com")
    assert dev.delete(f"/api/projects/{pid}/requirements/REQ-001").status_code == 403
    r = dev.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "purge_requirement", "id": "REQ-001"}]})
    assert r.status_code == 403
    assert owner.delete(f"/api/projects/{pid}/requirements/REQ-001").status_code == 200
    assert "REQ-001" not in owner.get(f"/api/projects/{pid}").json()["state"]["requirements"]
    hist = owner.get(f"/api/projects/{pid}/requirements/REQ-001/history").json()
    assert hist[0]["action"] == "purged"  # the history outlives the requirement


# --------------------------------------------------------------------------- stale links
def test_stale_links_computed_and_cleared():
    s = base()
    assert stale_links(s) == []  # linked in the same batch as created
    s, _ = apply_ops(s, [{"op": "update_requirement", "id": "REQ-001", "changes": {"acceptance_criteria": ["Paid"]}}])
    stale = stale_links(s)
    assert {(x["node_id"], x["requirement_id"]) for x in stale} == {("order-service", "REQ-001"), ("orders-db", "REQ-001")}
    assert stale[0]["fields"] == ["acceptance_criteria"]
    assert full_analysis(s)["stale_links"] == stale

    # status and position changes do not make or clear staleness
    s, _ = apply_ops(s, [{"op": "update_requirement", "id": "REQ-002", "changes": {"status": "approved"}},
                         {"op": "update_node", "id": "orders-db", "changes": {"position": {"x": 500, "y": 0}}}])
    assert len(stale_links(s)) == 2

    # reviewing one link clears only that link; changing the component clears its links
    s, summary = apply_ops(s, [{"op": "mark_reviewed", "id": "Order Service", "requirement_id": "REQ-001"}])
    assert summary == ["reviewed 'Order Service' against REQ-001"]
    assert [(x["node_id"], x["requirement_id"]) for x in stale_links(s)] == [("orders-db", "REQ-001")]
    s, _ = apply_ops(s, [{"op": "update_node", "id": "orders-db", "changes": {"technology": "PostgreSQL"}}])
    assert stale_links(s) == []

    # a later requirement change makes the links stale again; deleted requirements are never stale
    s, _ = apply_ops(s, [{"op": "update_requirement", "id": "REQ-002", "changes": {"title": "SSO via Okta"}}])
    assert [(x["node_id"], x["requirement_id"]) for x in stale_links(s)] == [("order-service", "REQ-002")]
    s, _ = apply_ops(s, [{"op": "remove_requirement", "id": "REQ-002"}])
    assert stale_links(s) == []
    with pytest.raises(OpError, match="not linked"):
        apply_ops(s, [{"op": "mark_reviewed", "id": "orders-db", "requirement_id": "REQ-002"}])


def test_review_bookkeeping_survives_the_collab_projection():
    s, _ = apply_ops(base(), [{"op": "update_requirement", "id": "REQ-001", "changes": {"title": "Place orders"}}])
    before = s
    s, _ = apply_ops(s, [{"op": "mark_reviewed", "id": "orders-db"}])
    assert collab_sync.state_patch(before, s) == {}  # bookkeeping only: nothing to push to the live document
    incoming, _ = collab_sync.sanitize(s.model_dump())
    incoming.nodes["order-service"].description = "Handles orders"
    merged, _ = collab_sync.merge_projection(s, incoming)
    assert stale_links(merged) == []
