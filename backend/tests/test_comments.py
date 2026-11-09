"""Comments: threads on components, decisions and projects, replies, resolving, @mentions and notifications."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402

from archflow.main import create_app  # noqa: E402
from archflow.store import Store  # noqa: E402

from authhelp import authed_client, member_client  # noqa: E402


@pytest.fixture()
def app():
    return create_app(Store(":memory:"))


@pytest.fixture()
def team(app):
    owner = authed_client(app)
    dev = member_client(app, owner, "developer", "dev@example.com", "Dev")
    reviewer = member_client(app, owner, "reviewer", "rev@example.com", "Rev")
    viewer = member_client(app, owner, "viewer", "view@example.com", "View")
    pid = owner.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    ids = {m["name"]: m["user_id"] for m in owner.get("/api/orgs/org-default/members").json()}
    return {"owner": owner, "dev": dev, "reviewer": reviewer, "viewer": viewer, "pid": pid, "ids": ids}


def node_id(client, pid):
    return next(iter(client.get(f"/api/projects/{pid}").json()["state"]["nodes"]))


def test_threads_replies_and_mentions(team):
    owner, dev, pid, ids = team["owner"], team["dev"], team["pid"], team["ids"]
    nid = node_id(owner, pid)
    r = owner.post(f"/api/projects/{pid}/comments", json={
        "body": "@Dev should this be a queue?", "target_type": "node", "target_id": nid,
        "mentions": [ids["Dev"], "u-not-a-member"]})
    assert r.status_code == 201, r.text
    root = r.json()
    assert root["mentions"] == [ids["Dev"]] and root["author_name"] == "Owner" and not root["resolved"]

    # Dev was mentioned
    n = dev.get("/api/notifications").json()
    assert n["unread"] == 1 and n["items"][0]["kind"] == "mention" and n["items"][0]["thread_id"] == root["id"]
    assert n["items"][0]["project_name"] == "Shop"
    assert owner.get("/api/notifications").json()["unread"] == 0  # not for your own comment

    # Dev replies: the thread's author is notified of the reply; the reply inherits the thread's target
    reply = dev.post(f"/api/projects/{pid}/comments", json={"body": "Yes, SQS", "parent_id": root["id"]}).json()
    assert reply["target_type"] == "node" and reply["target_id"] == nid and reply["parent_id"] == root["id"]
    on = owner.get("/api/notifications").json()
    assert on["unread"] == 1 and on["items"][0]["kind"] == "reply" and on["items"][0]["preview"] == "Yes, SQS"
    # No replies to replies
    assert dev.post(f"/api/projects/{pid}/comments", json={"body": "x", "parent_id": reply["id"]}).status_code == 422

    # Listing: both comments, oldest first
    listed = owner.get(f"/api/projects/{pid}/comments").json()
    assert [c["id"] for c in listed] == [root["id"], reply["id"]]

    # Mark read
    assert dev.post("/api/notifications/read", json={}).json()["unread"] == 0
    assert not any(not x["read"] for x in dev.get("/api/notifications").json()["items"])


def test_removed_members_neither_see_nor_count_notifications_from_that_org(team):
    """The unread count used to include notifications the list hid, leaving a removed member with a badge they could
    never open or clear; the list was also cut to the limit before hiding rows, so it could come back short."""
    owner, dev, pid, ids = team["owner"], team["dev"], team["pid"], team["ids"]
    for i in range(3):
        owner.post(f"/api/projects/{pid}/comments", json={"body": f"@Dev ping {i}", "target_type": "project",
                                                          "mentions": [ids["Dev"]]})
    assert dev.get("/api/notifications").json()["unread"] == 3
    assert owner.delete(f"/api/orgs/org-default/members/{ids['Dev']}").status_code == 204
    after = dev.get("/api/notifications").json()
    assert after == {"items": [], "unread": 0}


def test_edit_resolve_delete_permissions(team):
    owner, dev, reviewer, viewer, pid = team["owner"], team["dev"], team["reviewer"], team["viewer"], team["pid"]
    c = reviewer.post(f"/api/projects/{pid}/comments", json={"body": "General question"}).json()
    assert c["target_type"] == "project" and c["target_id"] == ""
    # Viewers read, but cannot comment
    assert len(viewer.get(f"/api/projects/{pid}/comments").json()) == 1
    assert viewer.post(f"/api/projects/{pid}/comments", json={"body": "hi"}).status_code == 403

    # Only the author edits the text
    assert dev.patch(f"/api/projects/{pid}/comments/{c['id']}", json={"body": "changed"}).status_code == 403
    edited = reviewer.patch(f"/api/projects/{pid}/comments/{c['id']}", json={"body": "General question (edited)"}).json()
    assert edited["body"] == "General question (edited)" and edited["edited_at"]

    # The author or an editor resolves; others cannot
    other = owner.post(f"/api/projects/{pid}/comments", json={"body": "Owner's thread"}).json()
    assert reviewer.patch(f"/api/projects/{pid}/comments/{other['id']}", json={"resolved": True}).status_code == 403
    done = dev.patch(f"/api/projects/{pid}/comments/{other['id']}", json={"resolved": True}).json()
    assert done["resolved"] and done["resolved_by"] == "Dev"
    assert not owner.patch(f"/api/projects/{pid}/comments/{other['id']}", json={"resolved": False}).json()["resolved"]

    # Delete: author or admin; deleting a thread removes its replies
    owner.post(f"/api/projects/{pid}/comments", json={"body": "reply", "parent_id": c["id"]})
    assert dev.delete(f"/api/projects/{pid}/comments/{c['id']}").status_code == 403
    assert owner.delete(f"/api/projects/{pid}/comments/{c['id']}").status_code == 204
    assert [x["id"] for x in owner.get(f"/api/projects/{pid}/comments").json()] == [other["id"]]


def test_targets_and_branches(team):
    owner, pid = team["owner"], team["pid"]
    assert owner.post(f"/api/projects/{pid}/comments", json={"body": "x", "target_type": "node"}).status_code == 422
    assert owner.post(f"/api/projects/{pid}/comments",
                      json={"body": "x", "target_type": "decision", "target_id": "ADR-999"}).status_code == 404
    adr = owner.post(f"/api/projects/{pid}/decisions", json={"title": "Use Postgres"}).json()
    owner.post(f"/api/projects/{pid}/comments", json={"body": "on main"})
    owner.post(f"/api/projects/{pid}/comments?branch=draft", json={"body": "on the draft"})
    owner.post(f"/api/projects/{pid}/comments?branch=draft",
               json={"body": "why?", "target_type": "decision", "target_id": adr["id"]})
    main = [c["body"] for c in owner.get(f"/api/projects/{pid}/comments?branch=main").json()]
    assert main == ["on main", "why?"]  # decisions are project-wide


def test_outsiders_see_nothing(app, team):
    outsider = authed_client(app, "out@example.com", "Out")
    pid = team["pid"]
    team["owner"].post(f"/api/projects/{pid}/comments", json={"body": "secret"})
    assert outsider.get(f"/api/projects/{pid}/comments").status_code == 404
    assert outsider.post(f"/api/projects/{pid}/comments", json={"body": "hi"}).status_code == 404


def test_notifications_arrive_live_over_the_socket(app, team):
    """The bell used to learn about mentions only by polling every 20 s; now the API pushes a message to the
    mentioned user's open tabs the moment the notification is stored."""
    from starlette.websockets import WebSocketDisconnect

    from authhelp import new_client, ws_headers

    owner, dev, pid, ids = team["owner"], team["dev"], team["pid"], team["ids"]
    with dev:  # runs the app's lifespan, which wires the store's notify hook to the live channel
        with dev.websocket_connect("/ws/notifications", headers=ws_headers(dev)) as ws:
            owner.post(f"/api/projects/{pid}/comments", json={"body": "@Dev look", "target_type": "project",
                                                              "mentions": [ids["Dev"]]})
            assert ws.receive_json() == {"type": "notifications"}
    with pytest.raises(WebSocketDisconnect) as refused:  # signed-out sockets are refused
        with new_client(app).websocket_connect("/ws/notifications", headers={"Origin": "http://localhost:5173"}) as ws:
            ws.receive_json()
    assert refused.value.code == 4401


def test_comment_on_a_requirement_or_a_connection(team):
    owner, pid = team["owner"], team["pid"]
    state = owner.get(f"/api/projects/{pid}").json()["state"]
    rid, eid = next(iter(state["requirements"])), next(iter(state["edges"]))
    for target_type, target_id in (("requirement", rid), ("edge", eid)):
        r = owner.post(f"/api/projects/{pid}/comments", json={"body": "Is this right?", "target_type": target_type,
                                                              "target_id": target_id})
        assert r.status_code == 201 and r.json()["target_type"] == target_type and r.json()["target_id"] == target_id
    # Targets must exist on the branch (components were not checked before either)
    for target_type in ("requirement", "edge", "node"):
        r = owner.post(f"/api/projects/{pid}/comments", json={"body": "x", "target_type": target_type, "target_id": "nope"})
        assert r.status_code == 404, (target_type, r.text)


def test_search_across_projects_finds_components_requirements_and_comments(app, team):
    owner, viewer, pid = team["owner"], team["viewer"], team["pid"]
    owner.post(f"/api/projects/{pid}/comments", json={"body": "Should the zebrafish cache be warmed?"})
    assert owner.get("/api/search", params={"q": "z"}).status_code == 422  # too short
    hits = viewer.get("/api/search", params={"q": "zebrafish"}).json()
    assert [(h["kind"], h["project_id"]) for h in hits["results"]] == [("comment", pid)]
    kinds = {h["kind"] for h in viewer.get("/api/search", params={"q": "order"}).json()["results"]}
    assert {"component", "requirement"} <= kinds
