"""Version approvals: the review state machine, who may approve, the self-approval rule, the org policy (required
approvals, approval before adopting a draft), activity entries, notifications and the MCP status tool."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402

from archflow.approvals import derive  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.store import Store  # noqa: E402

from authhelp import authed_client, member_client  # noqa: E402


@pytest.fixture()
def app():
    return create_app(Store(":memory:"))


@pytest.fixture()
def team(app):
    owner = authed_client(app)
    arch = member_client(app, owner, "architect", "arch@example.com", "Arch")
    dev = member_client(app, owner, "developer", "dev@example.com", "Dev")
    rev = member_client(app, owner, "reviewer", "rev@example.com", "Rev")
    rev2 = member_client(app, owner, "reviewer", "rev2@example.com", "Rev2")
    viewer = member_client(app, owner, "viewer", "view@example.com", "View")
    pid = owner.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    return {"owner": owner, "arch": arch, "dev": dev, "rev": rev, "rev2": rev2, "viewer": viewer, "pid": pid}


def save(client, pid, message="Snapshot", branch="main") -> int:
    r = client.post(f"/api/projects/{pid}/commit", params={"branch": branch}, json={"message": message})
    assert r.status_code == 201, r.text
    return r.json()["number"]


def act(client, pid, number, action, comment=""):
    return client.post(f"/api/projects/{pid}/versions/{number}/{action}", json={"comment": comment})


def review_of(client, pid, number):
    return next(v["review"] for v in client.get(f"/api/projects/{pid}/versions").json() if v["number"] == number)


def test_derive_rounds():
    row = lambda action, uid="u1": {"action": action, "actor_id": uid, "actor_name": uid, "created_at": "t"}  # noqa: E731
    assert derive([], 1)["status"] == "draft"
    assert derive([row("request")], 1)["status"] == "in_review"
    assert derive([row("request"), row("approve", "u2")], 1)["status"] == "approved"
    assert derive([row("request"), row("approve", "u2")], 2)["status"] == "in_review"
    changed = derive([row("request"), row("approve", "u2"), row("request_changes", "u3")], 1)
    assert changed["status"] == "changes_requested" and changed["approvals"] == []
    again = derive([row("request"), row("request_changes", "u3"), row("request"), row("approve", "u2")], 1)
    assert again["status"] == "approved" and again["requested_by"] == {"id": "u1", "name": "u1"}


def test_state_machine_and_listing(team):
    owner, dev, rev, pid = team["owner"], team["dev"], team["rev"], team["pid"]
    n = save(dev, pid, "Added caching")
    assert review_of(owner, pid, n) == {"status": "draft", "approvals": 0, "required": 1}
    # Approving or sending back a draft is refused; it has to be in review first
    assert act(rev, pid, n, "approve").status_code == 409
    assert act(rev, pid, n, "request-changes", "x").status_code == 409

    r = act(dev, pid, n, "request-review", "Please check the cache")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "in_review" and r.json()["requested_by"]["name"] == "Dev"
    assert act(dev, pid, n, "request-review").status_code == 409  # already in review
    assert act(rev, pid, n, "request-changes").status_code == 422  # a comment is required

    r = act(rev, pid, n, "request-changes", "Cache needs a TTL")
    assert r.json()["status"] == "changes_requested"
    assert review_of(owner, pid, n)["status"] == "changes_requested"
    assert act(rev, pid, n, "approve").status_code == 409

    assert act(dev, pid, n, "request-review").json()["status"] == "in_review"
    r = act(rev, pid, n, "approve", "LGTM")
    assert r.json()["status"] == "approved" and [x["name"] for x in r.json()["approvals"]] == ["Rev"]
    assert act(dev, pid, n, "request-review").status_code == 409  # already approved
    assert review_of(owner, pid, n) == {"status": "approved", "approvals": 1, "required": 1}

    # The full history: every action with actor, time, version and comment
    h = owner.get(f"/api/projects/{pid}/versions/{n}/review").json()["history"]
    assert [(x["action"], x["actor_name"], x["comment"]) for x in h] == [
        ("request", "Dev", "Please check the cache"), ("request_changes", "Rev", "Cache needs a TTL"),
        ("request", "Dev", ""), ("approve", "Rev", "LGTM")]
    assert all(x["version"] == n and x["created_at"] for x in h)
    assert owner.get(f"/api/projects/{pid}/versions/999/review").status_code == 404


def test_roles(team):
    owner, arch, dev, rev, viewer, pid = (team[k] for k in ("owner", "arch", "dev", "rev", "viewer", "pid"))
    n = save(owner, pid)
    # Viewers read; they cannot request, approve or send back
    assert viewer.get(f"/api/projects/{pid}/versions/{n}/review").status_code == 200
    assert act(viewer, pid, n, "request-review").status_code == 403
    # Reviewers cannot request a review (they cannot edit) but can approve
    assert act(rev, pid, n, "request-review").status_code == 403
    assert act(dev, pid, n, "request-review").status_code == 200
    # Developers cannot approve or request changes
    r = act(dev, pid, n, "approve")
    assert r.status_code == 403 and "reviewers, architects, admins and owners" in r.json()["detail"]
    assert act(dev, pid, n, "request-changes", "no").status_code == 403
    assert act(arch, pid, n, "approve").json()["status"] == "approved"


def test_required_approvals_and_self_approval(team):
    owner, arch, rev, rev2, dev, pid = (team[k] for k in ("owner", "arch", "rev", "rev2", "dev", "pid"))
    assert owner.get("/api/orgs/org-default/approval-policy").json()["required_approvals"] == 1
    assert dev.put("/api/orgs/org-default/approval-policy", json={"required_approvals": 2}).status_code == 403
    assert owner.put("/api/orgs/org-default/approval-policy", json={"required_approvals": 0}).status_code == 422
    p = owner.put("/api/orgs/org-default/approval-policy", json={"required_approvals": 2}).json()
    assert p["required_approvals"] == 2 and p["require_for_adopt"] is False and p["updated_by"] == "Owner"

    n = save(arch, pid, "Architect's version")
    act(arch, pid, n, "request-review")
    r = act(arch, pid, n, "approve")  # the author cannot approve their own version
    assert r.status_code == 403 and "someone else" in r.json()["detail"]
    r = act(rev, pid, n, "approve")
    assert r.json()["status"] == "in_review" and len(r.json()["approvals"]) == 1
    assert act(rev, pid, n, "approve").status_code == 409  # one approval per person
    assert act(rev2, pid, n, "approve").json()["status"] == "approved"

    # Lowering the requirement applies to versions already in review
    m = save(dev, pid)
    act(dev, pid, m, "request-review")
    act(rev, pid, m, "approve")
    assert review_of(owner, pid, m)["status"] == "in_review"
    owner.put("/api/orgs/org-default/approval-policy", json={"required_approvals": 1})
    assert review_of(owner, pid, m)["status"] == "approved"


def test_only_owner_may_self_approve(app, team):
    owner, rev, pid = team["owner"], team["rev"], team["pid"]
    n = save(owner, pid)
    act(owner, pid, n, "request-review")
    assert act(owner, pid, n, "approve").json()["status"] == "approved"  # the org's only owner

    member_client(app, owner, "owner", "owner2@example.com", "Owner Two")
    m = save(owner, pid)
    act(owner, pid, m, "request-review")
    assert act(owner, pid, m, "approve").status_code == 403  # no longer the only owner
    assert act(rev, pid, m, "approve").status_code == 200


def test_activity_and_notifications(team):
    owner, dev, rev, rev2, arch, pid = (team[k] for k in ("owner", "dev", "rev", "rev2", "arch", "pid"))
    n = save(dev, pid, "Split payments")
    act(dev, pid, n, "request-review", "Ready for a look")
    # Everyone who can approve hears about the request; developers and the requester do not
    for c in (owner, rev, rev2, arch):
        items = c.get("/api/notifications").json()["items"]
        assert items[0]["kind"] == "review_requested" and items[0]["version"] == n
        assert items[0]["comment_id"] is None and items[0]["preview"] == "Ready for a look"
    assert dev.get("/api/notifications").json()["unread"] == 0

    act(rev, pid, n, "request-changes", "Add an idempotency key")
    act(dev, pid, n, "request-review")
    act(rev2, pid, n, "approve")
    kinds = [(x["kind"], x["actor_name"], x["version"]) for x in dev.get("/api/notifications").json()["items"]]
    assert kinds == [("approved", "Rev2", n), ("changes_requested", "Rev", n)]
    assert dev.post("/api/notifications/read", json={}).json()["unread"] == 0

    acts = [(a["actor"], a["summary"][0]) for a in owner.get(f"/api/projects/{pid}/activity").json()]
    assert acts[:4] == [("Rev2", f"approved v{n}"), ("Dev", f"requested review of v{n}"),
                        ("Rev", f"requested changes to v{n}: Add an idempotency key"),
                        ("Dev", f"requested review of v{n}: Ready for a look")]


def test_adopt_blocked_until_approved(team):
    owner, dev, rev, pid = team["owner"], team["dev"], team["rev"], team["pid"]
    for name in ("draft/a", "draft/b"):
        assert dev.post(f"/api/projects/{pid}/branches", json={"name": name}).status_code == 201
    ops = [{"op": "add_node", "node": {"name": "Cache", "type": "cache", "technology": "Redis"}}]
    assert dev.post(f"/api/projects/{pid}/ops", params={"branch": "draft/b"}, json={"ops": ops}).status_code == 200
    d = dev.post(f"/api/projects/{pid}/decisions", json={
        "title": "Add a cache", "chosen": "draft/b",
        "alternatives": [{"name": "A", "branch": "draft/a"}, {"name": "B", "branch": "draft/b"}]}).json()
    adopt = lambda: dev.post(f"/api/projects/{pid}/decisions/{d['id']}/adopt", json={"target_branch": "main"})  # noqa: E731

    owner.put("/api/orgs/org-default/approval-policy", json={"required_approvals": 1, "require_for_adopt": True})
    r = adopt()
    assert r.status_code == 409 and "save a version of 'draft/b'" in r.json()["detail"]

    n = save(dev, pid, "Cache draft", branch="draft/b")
    r = adopt()
    assert r.status_code == 409 and "is a draft" in r.json()["detail"]
    act(dev, pid, n, "request-review")
    r = adopt()
    assert r.status_code == 409 and "in review (0/1 approvals)" in r.json()["detail"]
    status = owner.get(f"/api/projects/{pid}/review", params={"branch": "draft/b"}).json()
    assert status["version"] == n and status["status"] == "in_review" and status["unsaved_changes"] is False

    act(rev, pid, n, "approve")
    # Changing the draft after the approval needs a new approval
    more = [{"op": "add_node", "node": {"name": "Queue", "type": "queue"}}]
    dev.post(f"/api/projects/{pid}/ops", params={"branch": "draft/b"}, json={"ops": more})
    r = adopt()
    assert r.status_code == 409 and "has changed since" in r.json()["detail"]

    m = save(dev, pid, "Cache + queue", branch="draft/b")
    act(dev, pid, m, "request-review")
    act(rev, pid, m, "approve")
    r = adopt()
    assert r.status_code == 200, r.text
    assert r.json()["source_branch"] == "draft/b"

    # With the policy off, adopting needs no approval
    owner.put("/api/orgs/org-default/approval-policy", json={"required_approvals": 1, "require_for_adopt": False})
    assert dev.post(f"/api/projects/{pid}/decisions", json={
        "title": "Back to A", "chosen": "draft/a", "alternatives": [{"name": "A", "branch": "draft/a"}]}).status_code == 201
    assert dev.post(f"/api/projects/{pid}/decisions/ADR-002/adopt", json={"target_branch": "main"}).status_code == 200


def test_mcp_approval_status():
    from authhelp import new_client, signup
    from test_mcp_remote import BASE, oauth_login, tool

    with new_client(create_app(Store(":memory:")), base_url=BASE) as client:
        signup(client)
        rev = member_client(client.app, client, "reviewer", "rev@example.com", "Rev")
        pid = client.post("/api/projects", json={"name": "Shop", "template": "blank"}).json()["id"]
        n = save(client, pid)
        act(client, pid, n, "request-review", "Have a look")
        act(rev, pid, n, "approve")
        _, tokens = oauth_login(client, access="read", scope="archflow:read")  # a read-only token is enough
        at = tokens["access_token"]
        err, out, text = tool(client, at, "get_approval_status", project_id=pid, version=n)
        assert not err, text
        assert out["status"] == "approved" and [h["action"] for h in out["history"]] == ["request", "approve"]
        err, out, _ = tool(client, at, "get_approval_status", project_id=pid)
        assert out["version"] == n and out["status"] == "approved" and out["unsaved_changes"] is False
        err, out, _ = tool(client, at, "list_versions", project_id=pid)
        assert out["result"][0]["review"]["status"] == "approved"
        err, _, _ = tool(client, at, "get_approval_status", project_id=pid, version=99)
        assert err


def test_self_approval_is_by_user_not_display_name(app, team):
    """The rule compared display names: a namesake was blocked, and a version saved over MCP ("<name> via MCP") could
    be approved by the person who saved it."""
    owner, arch, pid = team["owner"], team["arch"], team["pid"]
    twin = member_client(app, owner, "reviewer", "arch-twin@example.com", "Arch")  # same display name, another person
    n = save(arch, pid)
    act(arch, pid, n, "request-review")
    assert act(twin, pid, n, "approve").status_code == 200


def test_a_version_saved_over_mcp_cannot_be_approved_by_its_author():
    from authhelp import new_client, signup
    from test_mcp_remote import BASE, oauth_login, tool

    with new_client(create_app(Store(":memory:")), base_url=BASE) as client:
        signup(client)
        member_client(client.app, client, "owner", "owner2@example.com", "Owner Two")  # not the only owner any more
        arch = member_client(client.app, client, "architect", "arch@example.com", "Arch")
        pid = client.post("/api/projects", json={"name": "Shop", "template": "blank"}).json()["id"]
        _, tokens = oauth_login(arch)
        err, out, text = tool(arch, tokens["access_token"], "commit_version", project_id=pid, message="From my agent")
        assert not err, text
        n = arch.get(f"/api/projects/{pid}/versions").json()[0]["number"]
        act(arch, pid, n, "request-review")
        r = act(arch, pid, n, "approve")
        assert r.status_code == 403 and "someone else" in r.json()["detail"]
