"""Compliance frameworks: the deterministic control checks on sample designs (FoodRush, a well-built one, plain
HTTP, AI without guardrails), evidence overrides and scores, remediation (offline and a faked model), the report
files, and the API with roles (viewers read, editors set frameworks and evidence)."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import foodrush  # noqa: E402
import pytest  # noqa: E402
from authhelp import member_client, new_client, signup  # noqa: E402

from archflow import compliance, llm  # noqa: E402
from archflow.agents import tracing  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402
from archflow.templates import build_template  # noqa: E402

from test_mcp_remote import BASE  # noqa: E402

ALL = list(compliance.FRAMEWORKS)


def state(nodes=(), edges=(), reqs=()):
    """nodes: dicts (id, type, name, technology, ...); edges: (source, target, protocol); reqs: text or dict."""
    ops = [{"op": "add_requirement", "requirement": {"id": f"REQ-{i + 1:03d}", "kind": "non_functional",
                                                     **({"title": r[:60], "description": r} if isinstance(r, str) else r)}}
           for i, r in enumerate(reqs)]
    ops += [{"op": "add_node", "node": n} for n in nodes]
    ops += [{"op": "add_edge", "edge": {"source": e[0], "target": e[1], "protocol": e[2] if len(e) > 2 else ""}}
            for e in edges]
    s, _ = apply_ops(ProjectState(), ops)
    return s


def foodrush_state():
    opt = foodrush.flawed_option()
    nodes = [{"id": c["key"], "type": c["type"], "name": c["name"], "technology": c["technology"]}
             for c in opt["components"]]
    edges = [(c["from"], c["to"], c["protocol"]) for c in opt["connections"]]
    reqs = [{k: v for k, v in r.items() if k != "kind"} | {"kind": r.get("kind", "functional")}
            for r in foodrush.requirements()]
    return state(nodes, edges, reqs)


def checks(s, **kw):
    return compliance.report(s, frameworks=ALL, **kw)["checks"]


def control(rep, fw, cid):
    f = next(x for x in rep["frameworks"] if x["id"] == fw)
    return next(c for c in f["controls"] if c["id"] == cid)


# --------------------------------------------------------------------------- checks
def test_foodrush_gdpr_requirement_and_its_gaps():
    s = foodrush_state()
    rep = compliance.report(s, frameworks=ALL)
    c = rep["checks"]
    assert {"soc2", "gdpr", "pci"} <= set(rep["suggested"]) and "hipaa" not in rep["suggested"]
    # "All PII is encrypted at rest and in transit", "hosted in an EU region", "Right to be forgotten"
    assert c["encryption_in_transit"]["status"] == "pass" and c["encryption_at_rest"]["status"] == "pass"
    assert c["encryption_at_rest"]["statements"][0]["source"] == "requirement"
    assert c["data_residency"]["status"] == "pass"
    assert c["erasure"]["status"] == "needs_evidence" and c["erasure"]["components"] == ["kafka"]  # crypto-shredding
    # the gaps the AI review found: no key management, and the WebSocket gateway has no path to auth
    assert c["key_management"]["status"] == "fail"
    assert c["authentication"]["status"] == "fail" and "ws-gateway" in c["authentication"]["components"]
    assert c["audit_logging"]["status"] == "fail" and c["monitoring"]["status"] == "fail"
    assert c["cardholder_data"]["status"] == "pass" and c["cardholder_data"]["components"] == ["stripe"]
    assert c["vendor_processors"]["status"] == "needs_evidence" and "stripe" in c["vendor_processors"]["components"]
    assert c["llm_guardrails"]["status"] == "not_applicable"
    gdpr = next(f for f in rep["frameworks"] if f["id"] == "gdpr")
    art32 = control(rep, "gdpr", "Art. 32")
    assert art32["status"] == "fail" and art32["checks"] == ["encryption_in_transit", "encryption_at_rest", "key_management"]
    assert "No secrets manager" in art32["summary"] and art32["fixes"]
    assert gdpr["score"] is not None and 0 < gdpr["score"] < 100
    gdpr_req = next(r.id for r in s.requirements.values() if r.title == "EU GDPR Compliance")
    assert gdpr_req in control(rep, "gdpr", "Art. 17")["requirements"]
    # proposals only for open checks no requirement covers yet: none for erasure (covered), one for key management
    titles = {p["title"] for p in rep["proposals"]}
    assert "Manage secrets and keys centrally" in titles and "Delete a person's data on request" not in titles


WELL_BUILT = dict(
    nodes=[
        {"id": "web", "type": "client", "name": "Web App", "technology": "React"},
        {"id": "waf", "type": "security", "name": "Edge WAF", "technology": "AWS WAF", "vendor": "aws"},
        {"id": "gw", "type": "gateway", "name": "API Gateway", "technology": "Amazon API Gateway", "vendor": "aws"},
        {"id": "idp", "type": "security", "name": "Login", "technology": "Amazon Cognito", "vendor": "aws"},
        {"id": "api", "type": "service", "name": "Clinic API", "technology": "AWS Fargate", "vendor": "aws"},
        {"id": "guard", "type": "security", "name": "AI Guardrails", "technology": "Amazon Bedrock Guardrails"},
        {"id": "llm", "type": "llm", "name": "Claude", "technology": "Claude on Amazon Bedrock", "vendor": "aws"},
        {"id": "db", "type": "database", "name": "Patients DB", "technology": "Amazon Aurora PostgreSQL Multi-AZ",
         "description": "Encrypted with KMS; daily snapshots and PITR for 35 days",
         "props": {"data_classification": "phi"}},
        {"id": "kms", "type": "security", "name": "Keys", "technology": "AWS KMS", "vendor": "aws"},
        {"id": "audit", "type": "database", "name": "Audit Trail", "technology": "AWS CloudTrail Lake",
         "description": "Encrypted, append-only, replicated across regions, kept 7 years", "props": {"data_classification": "internal", "backup": "AWS Backup"}},
        {"id": "mon", "type": "service", "name": "Monitoring", "technology": "Amazon CloudWatch"},
    ],
    edges=[("web", "waf", "HTTPS"), ("waf", "gw", "HTTPS"), ("gw", "idp", "HTTPS"), ("gw", "api", "HTTPS"),
           ("api", "guard", "HTTPS"), ("guard", "llm", "HTTPS"), ("api", "db", "PostgreSQL over TLS"),
           ("api", "kms", "HTTPS"), ("api", "audit", "HTTPS"), ("api", "mon", "HTTPS")],
    reqs=["Staff roles: doctor, nurse and admin; least privilege for every service account.",
          "Administrators sign in with MFA.",
          "Patient records are kept for 6 years, then deleted (retention).",
          "Patients can request erasure of their data.",
          "All patient data stays in the EU (eu-central-1).",
          "A signed BAA and DPA are in place with every vendor."],
)


def test_well_built_design_passes_every_framework():
    rep = compliance.report(state(**WELL_BUILT), frameworks=ALL)
    bad = {k: (v["status"], v["summary"]) for k, v in rep["checks"].items() if v["status"] not in ("pass", "not_applicable")}
    assert bad == {}
    assert rep["checks"]["cardholder_data"]["status"] == "not_applicable"
    assert all(f["score"] == 100 for f in rep["frameworks"]), [(f["id"], f["score"]) for f in rep["frameworks"]]
    assert rep["proposals"] == []
    assert "hipaa" in rep["suggested"]


def test_transit_plain_http_fails_unknown_needs_evidence_and_a_policy_covers_it():
    nodes = [{"id": "web", "type": "client", "name": "Web", "technology": "React"},
             {"id": "gw", "type": "gateway", "name": "Gateway", "technology": "Kong"},
             {"id": "svc", "type": "service", "name": "Orders", "technology": "Go"},
             {"id": "db", "type": "database", "name": "Orders DB", "technology": "PostgreSQL"}]
    plain = checks(state(nodes, [("web", "gw", "HTTP"), ("gw", "svc", "HTTPS"), ("svc", "db", "SQL")]))
    assert plain["encryption_in_transit"]["status"] == "fail"
    assert set(plain["encryption_in_transit"]["components"]) == {"web", "gw"}
    edges = [("web", "gw", "HTTPS"), ("gw", "svc", "gRPC"), ("svc", "db", "SQL")]
    unknown = checks(state(nodes, edges))["encryption_in_transit"]
    assert unknown["status"] == "needs_evidence" and "svc" in unknown["components"]
    stated = checks(state(nodes, edges), memory=[{"id": "mem-1", "text": "We use mTLS between all services."}])
    assert stated["encryption_in_transit"]["status"] == "pass"
    assert stated["encryption_in_transit"]["statements"][0]["source"] == "memory"
    # clients calling a service directly and a client reading the database
    direct = checks(state(nodes, [("web", "svc", "HTTPS"), ("web", "db", "SQL")]))
    assert direct["edge_protection"]["status"] == "fail" and direct["edge_protection"]["components"] == ["svc"]
    assert direct["network_segmentation"]["status"] == "fail"


def test_ai_without_guardrails_and_planned_guardrails():
    nodes = [{"id": "app", "type": "client", "name": "Chat", "technology": "React"},
             {"id": "agent", "type": "agent", "name": "Support Agent", "technology": "Python"},
             {"id": "llm", "type": "llm", "name": "GPT", "technology": "OpenAI GPT-4o"}]
    edges = [("app", "agent", "HTTPS"), ("agent", "llm", "HTTPS")]
    assert checks(state(nodes, edges))["llm_guardrails"]["status"] == "fail"
    planned = checks(state(nodes, edges, ["Prompt injection attempts are blocked by guardrails."]))["llm_guardrails"]
    assert planned["status"] == "needs_evidence" and planned["components"] == ["llm"]
    loose = nodes + [{"id": "g", "type": "security", "name": "NeMo Guardrails", "technology": "NeMo Guardrails"}]
    assert checks(state(loose, edges))["llm_guardrails"]["status"] == "needs_evidence"  # not connected


def test_blank_project_is_not_applicable_and_defaults_to_soc2():
    rep = compliance.report(ProjectState())
    assert rep["selected"] == ["soc2"] and rep["frameworks"][0]["score"] is None
    assert all(c["status"] == "not_applicable" for c in rep["checks"].values())


def test_evidence_marks_controls_met_and_changes_the_score():
    s = build_template("ai-customer-support")
    before = compliance.report(s, frameworks=["soc2"])
    vendor = control(before, "soc2", "CC9.2")
    assert vendor["effective"] == "needs_evidence"
    ev = [{"framework": "soc2", "control_id": "CC9.2", "status": "met", "note": "DPAs signed with Anthropic and Zendesk",
           "updated_by": "Owner", "updated_at": "2026-11-04T10:00:00Z"},
          {"framework": "soc2", "control_id": "C1.2", "status": "", "note": "Policy draft in review",
           "updated_by": "Owner", "updated_at": "2026-11-04T10:00:00Z"}]
    after = compliance.report(s, frameworks=["soc2"], evidence=ev)
    assert control(after, "soc2", "CC9.2")["effective"] == "met" and control(after, "soc2", "CC9.2")["status"] == "needs_evidence"
    assert control(after, "soc2", "C1.2")["effective"] == "fail"  # a note alone keeps the computed status
    assert after["frameworks"][0]["score"] > before["frameworks"][0]["score"]
    assert after["frameworks"][0]["counts"]["met"] == 1
    assert not any("SOC 2 CC9.2" in p["controls"] for p in after["proposals"])
    md = compliance.to_markdown(after, "Support", "main")
    assert "| CC9.2 Vendor risk | Met by process |" in md and "DPAs signed with Anthropic and Zendesk" in md
    html = compliance.to_html(after, "Support", "main")
    assert html.startswith("<!doctype html>") and "Save as PDF" in html and "<table>" in html


# --------------------------------------------------------------------------- remediation
def test_offline_remediation_comes_from_the_checks():
    rep = compliance.report(build_template("ai-customer-support"), frameworks=["soc2", "gdpr"])
    out = compliance.remediate(rep, build_template("ai-customer-support"), "Support", None, use_ai=False)
    assert out["engine"] == "offline" and out["steps"] and out["requirements"] == rep["proposals"]
    first = out["steps"][0]
    assert first["controls"] and first["detail"] and first["effort"] in ("low", "medium", "high")
    guard = next(st for st in out["steps"] if st["title"] == "AI guardrails")
    assert guard["controls"] == ["SOC 2 CC6.8", "GDPR Art. 25"] and guard["components"] == ["llm"]


def test_ai_remediation_is_grounded(monkeypatch):
    s = build_template("ai-customer-support")
    rep = compliance.report(s, frameworks=["soc2"])
    existing = next(iter(s.requirements.values())).title
    seen = {}

    def fake(system, messages, schema, **kw):
        seen["system"], seen["user"] = system, messages[-1]["content"]
        return {"summary": "Several gaps.", "steps": [
            {"controls": ["SOC 2 CC6.8", "SOC 2 CC1.1"], "title": "Add Bedrock Guardrails",
             "detail": "Put guardrails before Claude.", "components": ["Claude", "Imaginary Proxy"], "effort": "low"},
            {"controls": ["HIPAA 164.312(b)"], "title": "Not an open control", "detail": "x", "components": [],
             "effort": "low"}],
            "requirements": [
                {"title": "Guard model prompts", "description": "d", "category": "Security", "priority": "high",
                 "acceptance_criteria": ["Injection blocked", ""], "controls": ["soc 2 cc6.8"]},
                {"title": existing, "description": "dup", "category": "security", "priority": "urgent",
                 "acceptance_criteria": [], "controls": []}],
            "questions": ["Which region?"]}

    monkeypatch.setattr(llm, "complete_json", fake)
    out = compliance.remediate(rep, s, "Support", None, use_ai=True)
    assert out["engine"] == "llm" and len(out["steps"]) == 1
    step = out["steps"][0]
    assert step["controls"] == ["SOC 2 CC6.8"] and step["components"] == ["llm"]
    assert [r["title"] for r in out["requirements"]] == ["Guard model prompts"]
    req = out["requirements"][0]
    assert req["controls"] == ["SOC 2 CC6.8"] and req["category"] == "security" and req["acceptance_criteria"] == ["Injection blocked"]
    assert "SOC 2 CC6.8" in seen["user"] and "Claude" in seen["user"] and "Support" in seen["system"]

    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"summary": "", "steps": [], "requirements": []})
    assert compliance.remediate(rep, s, "Support", None, use_ai=True)["engine"] == "offline"


def test_eval_suite_scores_a_plan(monkeypatch):
    from archflow.evals import runner, suites

    case = next(c for c in runner.load_suites(runner.DATASETS_DIR)["compliance"] if c["id"] == "payments-plain-http-pci")
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"summary": "Card data is at risk.", "steps": [
        {"controls": ["PCI DSS 3.3"], "title": "Tokenize cards with Stripe", "detail": "Use hosted fields; drop stored "
         "card numbers from Shop DB.", "components": ["Shop DB"], "effort": "medium"},
        {"controls": ["PCI DSS 4.2"], "title": "HTTPS only", "detail": "Serve the Storefront API over HTTPS.",
         "components": ["Shop API"], "effort": "low"},
        {"controls": ["PCI DSS 8.3"], "title": "Add sign-in", "detail": "Add Amazon Cognito.", "components": ["Shop API"],
         "effort": "medium"}],
        "requirements": [{"title": "Never store card numbers", "description": "d", "category": "compliance",
                          "priority": "critical", "acceptance_criteria": ["Only tokens"], "controls": ["PCI DSS 3.3"]},
                         {"title": "TLS everywhere", "description": "d", "category": "security", "priority": "high",
                          "acceptance_criteria": ["HTTPS only"], "controls": ["PCI DSS 4.2"]}]})
    out = suites.run_compliance(case, None)
    assert all(ok for _, ok, _ in out.checks), [c for c in out.checks if not c[1]]


# --------------------------------------------------------------------------- API and roles
@pytest.fixture()
def app():
    yield create_app(Store(":memory:"))
    tracing.set_sink(None)


@pytest.fixture()
def client(app):
    with new_client(app, base_url=BASE) as c:
        signup(c)
        yield c


def test_api_report_settings_evidence_exports_and_roles(app, client):
    pid = client.post("/api/projects", json={"name": "Support", "template": "ai-customer-support"}).json()["id"]
    r = client.get(f"/api/projects/{pid}/compliance")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["selected"] == rep["suggested"] and rep["saved"] is False and rep["branch"] == "main"
    assert {a["id"] for a in rep["available"]} == set(ALL)
    rep = client.get(f"/api/projects/{pid}/compliance", params={"frameworks": "gdpr,bogus,pci"}).json()
    assert [f["id"] for f in rep["frameworks"]] == ["gdpr", "pci"]

    assert client.put(f"/api/projects/{pid}/compliance/settings", json={"frameworks": ["nope"]}).status_code == 422
    r = client.put(f"/api/projects/{pid}/compliance/settings", json={"frameworks": ["hipaa", "soc2"]})
    assert r.status_code == 200 and r.json()["frameworks"] == ["hipaa", "soc2"]
    rep = client.get(f"/api/projects/{pid}/compliance").json()
    assert rep["selected"] == ["hipaa", "soc2"] and rep["saved"] is True

    viewer = member_client(app, client, "viewer", "viewer@example.com")
    editor = member_client(app, client, "developer", "dev@example.com")
    body = {"framework": "hipaa", "control_id": "164.308(b)(1)", "status": "met", "note": "BAAs signed"}
    assert viewer.get(f"/api/projects/{pid}/compliance").status_code == 200
    assert viewer.put(f"/api/projects/{pid}/compliance/evidence", json=body).status_code == 403
    assert viewer.put(f"/api/projects/{pid}/compliance/settings", json={"frameworks": ["gdpr"]}).status_code == 403
    r = editor.put(f"/api/projects/{pid}/compliance/evidence", json=body)
    assert r.status_code == 200 and r.json()["updated_by"] == "dev" and r.json()["status"] == "met"
    assert editor.put(f"/api/projects/{pid}/compliance/evidence",
                      json={**body, "control_id": "X.9"}).status_code == 404
    assert editor.put(f"/api/projects/{pid}/compliance/evidence", json={**body, "status": "done"}).status_code == 422
    c = control(viewer.get(f"/api/projects/{pid}/compliance").json(), "hipaa", "164.308(b)(1)")
    assert c["effective"] == "met" and c["evidence"]["note"] == "BAAs signed"
    r = editor.put(f"/api/projects/{pid}/compliance/evidence", json={**body, "status": "", "note": ""})
    assert r.json()["deleted"] is True
    assert control(client.get(f"/api/projects/{pid}/compliance").json(), "hipaa", "164.308(b)(1)")["evidence"] is None

    r = viewer.get(f"/api/projects/{pid}/compliance.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "# Support: compliance report" in r.text and "## HIPAA" in r.text
    r = viewer.get(f"/api/projects/{pid}/compliance.json", params={"frameworks": "pci"})
    doc = r.json()
    assert doc["schema"] == "archflow.compliance" and [f["id"] for f in doc["frameworks"]] == ["pci"]
    r = viewer.get(f"/api/projects/{pid}/compliance.html", params={"inline": "true"})
    assert r.headers["content-disposition"].startswith("inline") and "Save as PDF" in r.text

    r = viewer.post(f"/api/projects/{pid}/compliance/review", json={"frameworks": ["soc2"], "ai": True})
    assert r.status_code == 200 and r.json()["engine"] == "offline" and r.json()["steps"]
    assert client.get(f"/api/projects/{pid}/compliance", params={"branch": "nope"}).status_code == 404
