"""ArchFlow MCP server — lets external agents (Claude Code, Codex, ...) read and edit architectures.

It is a thin client of the ArchFlow HTTP API, so every change an agent makes goes through the
same operation pipeline as human edits and appears live on everyone's canvas.

Run (stdio):  archflow-mcp          (or: python -m archflow.mcp_server)
Env:          ARCHFLOW_URL (default http://localhost:8000), ARCHFLOW_ACTOR (default "mcp-agent")
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

BASE = os.environ.get("ARCHFLOW_URL", "http://localhost:8000").rstrip("/")
ACTOR = os.environ.get("ARCHFLOW_ACTOR", "mcp-agent")

mcp = MCPServer("archflow", instructions="Read and edit ArchFlow architectures and requirements. Changes appear live on the shared canvas.")


def _req(method: str, path: str, **kwargs) -> Any:
    with httpx.Client(base_url=BASE, timeout=180) as client:
        r = client.request(method, path, **kwargs)
    if r.status_code == 409:
        return {"error": "conflict", "conflicts": r.json().get("conflicts")}
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        raise RuntimeError(f"ArchFlow API error {r.status_code}: {detail}")
    return r.json() if r.content else None


def _ops(project_id: str, branch: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    out = _req("POST", f"/api/projects/{project_id}/ops", params={"branch": branch}, json={"ops": ops, "actor": ACTOR})
    if isinstance(out, dict) and "summary" in out:
        return {"ok": True, "changes": out["summary"], "rev": out["state"]["rev"]}
    return out


def _compact(state: dict[str, Any]) -> dict[str, Any]:
    deleted = {rid for rid, r in state["requirements"].items() if r.get("deleted")}  # soft-deleted: hidden
    return {
        "rev": state["rev"],
        "components": [{**{k: n[k] for k in ("id", "name", "type", "technology", "description")},
                        "requirement_ids": [r for r in n["requirement_ids"] if r not in deleted]}
                       for n in state["nodes"].values()],
        "connections": [{k: e[k] for k in ("id", "source", "target", "protocol", "label")} for e in state["edges"].values()],
        "requirements": [{k: v for k, v in r.items() if k not in ("rev", "field_revs", "deleted")}
                         for rid, r in state["requirements"].items() if rid not in deleted],
    }


@mcp.tool()
def list_projects() -> list[dict[str, Any]]:
    """List ArchFlow projects (id, name, description)."""
    return _req("GET", "/api/projects")


@mcp.tool()
def create_architecture(name: str, description: str = "", template: str = "blank") -> dict[str, Any]:
    """Create a new ArchFlow project. template: 'blank' or 'ai-customer-support'."""
    return _req("POST", "/api/projects", json={"name": name, "description": description, "template": template})


@mcp.tool()
def get_architecture(project_id: str, branch: str = "main") -> dict[str, Any]:
    """Get the full structured model: components, connections and requirements."""
    data = _req("GET", f"/api/projects/{project_id}", params={"branch": branch})
    return {"project": data["project"], "branch": branch, "branches": [b["name"] for b in data["branches"]],
            **_compact(data["state"])}


@mcp.tool()
def add_component(project_id: str, name: str, type: str = "service", technology: str = "", description: str = "",
                  requirement_ids: list[str] | None = None, x: float | None = None, y: float | None = None,
                  branch: str = "main") -> dict[str, Any]:
    """Add a component. type: client, gateway, service, database, cache, queue, storage, agent, llm, mcp,
    external, security, note. Position is auto-assigned if x/y are omitted."""
    node: dict[str, Any] = {"name": name, "type": type, "technology": technology, "description": description,
                            "requirement_ids": requirement_ids or []}
    if x is not None and y is not None:
        node["position"] = {"x": x, "y": y}
    return _ops(project_id, branch, [{"op": "add_node", "node": node}])


@mcp.tool()
def update_component(project_id: str, component: str, name: str | None = None, type: str | None = None,
                     technology: str | None = None, description: str | None = None,
                     requirement_ids: list[str] | None = None, branch: str = "main") -> dict[str, Any]:
    """Update a component (by id or name). Only the provided fields change."""
    changes = {k: v for k, v in {"name": name, "type": type, "technology": technology, "description": description,
                                 "requirement_ids": requirement_ids}.items() if v is not None}
    return _ops(project_id, branch, [{"op": "update_node", "id": component, "changes": changes}])


@mcp.tool()
def remove_component(project_id: str, component: str, branch: str = "main") -> dict[str, Any]:
    """Remove a component (by id or name) and all of its connections."""
    return _ops(project_id, branch, [{"op": "remove_node", "id": component}])


@mcp.tool()
def connect_components(project_id: str, source: str, target: str, protocol: str = "", label: str = "",
                       branch: str = "main") -> dict[str, Any]:
    """Connect source → target (ids or names), e.g. protocol 'REST', 'gRPC', 'Kafka', 'MCP'."""
    return _ops(project_id, branch, [{"op": "add_edge", "edge": {"source": source, "target": target,
                                                                 "protocol": protocol, "label": label}}])


@mcp.tool()
def disconnect_components(project_id: str, source: str, target: str, branch: str = "main") -> dict[str, Any]:
    """Remove the connection source → target."""
    return _ops(project_id, branch, [{"op": "remove_edge", "source": source, "target": target}])


@mcp.tool()
def get_requirements(project_id: str, branch: str = "main") -> list[dict[str, Any]]:
    """List structured requirements."""
    data = _req("GET", f"/api/projects/{project_id}", params={"branch": branch})
    return _compact(data["state"])["requirements"]


@mcp.tool()
def create_requirement(project_id: str, title: str, description: str = "", kind: str = "functional",
                       category: str = "", priority: str = "medium", actors: list[str] | None = None,
                       acceptance_criteria: list[str] | None = None, branch: str = "main") -> dict[str, Any]:
    """Create a requirement. kind: functional | non_functional. priority: low | medium | high | critical."""
    return _ops(project_id, branch, [{"op": "add_requirement", "requirement": {
        "title": title, "description": description, "kind": kind, "category": category, "priority": priority,
        "actors": actors or [], "acceptance_criteria": acceptance_criteria or []}}])


@mcp.tool()
def update_requirement(project_id: str, requirement_id: str, title: str | None = None, description: str | None = None,
                       priority: str | None = None, status: str | None = None,
                       acceptance_criteria: list[str] | None = None, branch: str = "main") -> dict[str, Any]:
    """Update a requirement. status: draft | review | approved | implemented."""
    changes = {k: v for k, v in {"title": title, "description": description, "priority": priority, "status": status,
                                 "acceptance_criteria": acceptance_criteria}.items() if v is not None}
    return _ops(project_id, branch, [{"op": "update_requirement", "id": requirement_id, "changes": changes}])


@mcp.tool()
def validate_architecture(project_id: str, branch: str = "main") -> dict[str, Any]:
    """Run requirement linting, architecture validation and traceability checks."""
    return _req("GET", f"/api/projects/{project_id}/analysis", params={"branch": branch})


@mcp.tool()
def commit_version(project_id: str, message: str, branch: str = "main") -> dict[str, Any]:
    """Snapshot the current branch state as a new version."""
    return _req("POST", f"/api/projects/{project_id}/commit", params={"branch": branch},
                json={"message": message, "author": ACTOR})


@mcp.tool()
def get_architecture_diff(project_id: str, from_version: str, to_version: str = "working",
                          branch: str = "main") -> str:
    """Diff two versions. Refs: 'working', a version number like '3', or 'branch:<name>'."""
    return _req("GET", f"/api/projects/{project_id}/diff",
                params={"from": from_version, "to": to_version, "branch": branch})["text"]


@mcp.tool()
def architecture_command(project_id: str, instruction: str, branch: str = "main") -> dict[str, Any]:
    """Apply a natural-language change, e.g. 'Add Redis between Order Service and Orders DB'."""
    out = _req("POST", f"/api/projects/{project_id}/ai/command", params={"branch": branch},
               json={"text": instruction, "actor": ACTOR})
    out.pop("state", None)
    return out


@mcp.tool()
def generate_coding_prompt(project_id: str, component_ids: list[str] | None = None,
                           requirement_ids: list[str] | None = None, target: str = "claude-code",
                           branch: str = "main") -> str:
    """Generate an implementation brief for the selected components/requirements (all if none selected)."""
    return _req("POST", f"/api/projects/{project_id}/prompt", params={"branch": branch},
                json={"node_ids": component_ids or [], "requirement_ids": requirement_ids or [], "target": target})["prompt"]


@mcp.tool()
def jira_push_preview(project_id: str, branch: str = "main", project_key: str = "", ai: bool = True) -> dict[str, Any]:
    """Dry run of pushing a branch to Jira: what would be created, updated or left unchanged. Sends nothing."""
    return _req("POST", f"/api/projects/{project_id}/jira/plan", params={"branch": branch},
                json={"project_key": project_key or None, "ai": ai})


@mcp.tool()
def jira_push(project_id: str, confirm: bool = False, branch: str = "main", project_key: str = "",
              ai: bool = True) -> dict[str, Any]:
    """Push a branch to Jira. Call jira_push_preview first and pass confirm=true."""
    return _req("POST", f"/api/projects/{project_id}/jira/push", params={"branch": branch},
                json={"project_key": project_key or None, "ai": ai, "confirm": confirm})


@mcp.tool()
def jira_sync_status(project_id: str, branch: str = "main") -> dict[str, Any]:
    """Pull the current Jira status of every linked issue now."""
    return _req("POST", f"/api/projects/{project_id}/jira/refresh", params={"branch": branch})


@mcp.tool()
def jira_list_unlinked_issues(project_id: str, page_token: str = "", limit: int = 25, query: str = "",
                              project_key: str = "") -> dict[str, Any]:
    """Jira issues this project does not link to yet, with the requirement each would become."""
    params: dict[str, Any] = {"limit": limit, "q": query}
    if page_token:
        params["page_token"] = page_token
    if project_key:
        params["project_key"] = project_key
    return _req("GET", f"/api/projects/{project_id}/jira/issues", params=params)


@mcp.tool()
def jira_import_issues(project_id: str, keys: list[str], branch: str = "main", ai: bool = True) -> dict[str, Any]:
    """Import Jira issues (e.g. SHOP-12) as requirements; already-linked issues are skipped."""
    out = _req("POST", f"/api/projects/{project_id}/jira/import", params={"branch": branch},
               json={"keys": keys, "ai": ai})
    out.pop("links", None)
    return out


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
