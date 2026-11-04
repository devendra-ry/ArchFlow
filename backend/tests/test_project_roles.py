"""Per-project role overrides: who may set them, and that every project route honours them."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402

from archflow.authz import effective_role  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.store import DEFAULT_ORG_ID, Store  # noqa: E402

from authhelp import authed_client, member_client  # noqa: E402

OP = {"ops": [{"op": "add_node", "node": {"name": "Cache", "type": "cache"}}]}


@pytest.fixture()
def app():
    return create_app(Store(":memory:"))


@pytest.fixture()
def owner(app):
    return authed_client(app)


def make_project(client, name="Shop"):
    r = client.post("/api/projects", json={"name": name, "template": "blank", "org_id": DEFAULT_ORG_ID})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def uid(client):
    return client.get("/api/me").json()["user"]["id"]


def test_effective_role():
    assert effective_role("developer", None) == "developer"
    assert effective_role("developer", "viewer") == "viewer"
    assert effective_role("viewer", "admin") == "admin"
    assert effective_role("owner", "viewer") == "owner"  # owners are never overridden
    assert effective_role(None, "admin") is None  # not a member: nothing
    assert effective_role("developer", "owner") == "developer"  # not a project role


def test_overrides_change_access_on_one_project_only(app, owner):
    shop, other = make_project(owner), make_project(owner, "Other")
    dev = member_client(app, owner, "developer", "dev@example.com", "Dev")
    viewer = member_client(app, owner, "viewer", "viewer@example.com", "Viewer")

    # Lower: a developer becomes a viewer on Shop.
    r = owner.put(f"/api/projects/{shop}/roles/{uid(dev)}", json={"role": "viewer"})
    assert r.status_code == 200
    row = next(m for m in r.json()["members"] if m["user_id"] == uid(dev))
    assert row["org_role"] == "developer" and row["project_role"] == "viewer" and row["effective_role"] == "viewer"
    assert dev.get(f"/api/projects/{shop}").json()["role"] == "viewer"
    assert dev.post(f"/api/projects/{shop}/ops", json=OP).status_code == 403
    assert dev.post(f"/api/projects/{shop}/commit", json={"message": "x"}).status_code == 403
    assert dev.post(f"/api/projects/{other}/ops", json=OP).status_code == 200  # other projects unchanged
    assert dev.get(f"/api/projects/{shop}/collab-ticket").json().get("read_only", True) is True

    # Higher: a viewer becomes an editor (the developer role) on Shop.
    r = owner.put(f"/api/projects/{shop}/roles/{uid(viewer)}", json={"role": "editor"})
    assert r.status_code == 200
    assert next(m for m in r.json()["members"] if m["user_id"] == uid(viewer))["project_role"] == "developer"
    assert viewer.post(f"/api/projects/{shop}/ops", json=OP).status_code == 200
    assert viewer.post(f"/api/projects/{other}/ops", json=OP).status_code == 403

    # Removing the override restores the org role.
    assert owner.delete(f"/api/projects/{shop}/roles/{uid(dev)}").status_code == 200
    assert dev.post(f"/api/projects/{shop}/ops", json=OP).status_code == 200

    # Overrides end when the member leaves the org.
    store = app.state.authz.store
    owner.delete(f"/api/orgs/{DEFAULT_ORG_ID}/members/{uid(viewer)}")
    assert store.get_project_role(shop, uid(viewer)) is None


def test_who_may_set_overrides(app, owner):
    shop = make_project(owner)
    admin = member_client(app, owner, "admin", "admin@example.com", "Admin")
    dev = member_client(app, owner, "developer", "dev@example.com", "Dev")
    rev = member_client(app, owner, "reviewer", "rev@example.com", "Rev")

    # Everyone in the org can see the list; only project admins manage.
    listing = dev.get(f"/api/projects/{shop}/roles").json()
    assert listing["can_manage"] is False and listing["roles"] == ["viewer", "reviewer", "developer", "admin"]
    assert dev.put(f"/api/projects/{shop}/roles/{uid(rev)}", json={"role": "viewer"}).status_code == 403
    assert rev.delete(f"/api/projects/{shop}/roles/{uid(dev)}").status_code == 403
    assert admin.put(f"/api/projects/{shop}/roles/{uid(rev)}", json={"role": "viewer"}).status_code == 200

    # Owners are never overridden; unknown members and roles are refused.
    assert admin.put(f"/api/projects/{shop}/roles/{uid(owner)}", json={"role": "viewer"}).status_code == 409
    assert admin.put(f"/api/projects/{shop}/roles/u-nobody", json={"role": "viewer"}).status_code == 404
    assert admin.put(f"/api/projects/{shop}/roles/{uid(rev)}", json={"role": "owner"}).status_code == 422
    assert admin.put(f"/api/projects/{shop}/roles/{uid(rev)}", json={"role": "architect"}).status_code == 422

    # A project admin (by override) manages this project, but not org admins, and not other projects.
    other = make_project(owner, "Other")
    assert owner.put(f"/api/projects/{shop}/roles/{uid(dev)}", json={"role": "admin"}).status_code == 200
    r = dev.get(f"/api/projects/{shop}/roles")
    assert r.json()["can_manage"] is True
    assert dev.put(f"/api/projects/{shop}/roles/{uid(rev)}", json={"role": "editor"}).status_code == 200
    assert dev.put(f"/api/projects/{shop}/roles/{uid(admin)}", json={"role": "viewer"}).status_code == 403
    assert dev.put(f"/api/projects/{other}/roles/{uid(rev)}", json={"role": "editor"}).status_code == 403
    # A project admin can delete the project, like an org admin.
    assert dev.delete(f"/api/projects/{shop}").status_code == 204


def test_cannot_grant_above_own_project_role(app, owner):
    shop = make_project(owner)
    admin = member_client(app, owner, "admin", "admin@example.com", "Admin")
    dev = member_client(app, owner, "developer", "dev@example.com", "Dev")
    # An org admin lowered to editor on this project cannot manage it any more.
    assert owner.put(f"/api/projects/{shop}/roles/{uid(admin)}", json={"role": "developer"}).status_code == 200
    assert admin.put(f"/api/projects/{shop}/roles/{uid(dev)}", json={"role": "viewer"}).status_code == 403
    assert admin.get(f"/api/projects/{shop}").json()["role"] == "developer"
    # Non-members still get 404 (no existence leak).
    outsider = authed_client(app, "out@example.com", "Out")
    assert outsider.get(f"/api/projects/{shop}/roles").status_code == 404
    assert outsider.put(f"/api/projects/{shop}/roles/{uid(dev)}", json={"role": "viewer"}).status_code == 404
