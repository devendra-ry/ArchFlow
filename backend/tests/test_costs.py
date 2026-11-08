"""Cost estimation: assumptions from requirements, pricing per kind and vendor, high availability, component
overrides, budgets, recommendations (rules and a faked model), CSV and the API."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from authhelp import new_client, signup  # noqa: E402

from archflow import costs, llm  # noqa: E402
from archflow.agents import tracing  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.store import Store  # noqa: E402
from archflow.templates import build_template  # noqa: E402

from test_mcp_remote import BASE  # noqa: E402


def state(*ops):
    s, _ = apply_ops(ProjectState(), list(ops))
    return s


def req(rid, text, kind="non_functional", category=""):
    return {"op": "add_requirement", "requirement": {"id": rid, "title": text[:40], "description": text, "kind": kind,
                                                     "category": category}}


def node(nid, name, type_, **kw):
    return {"op": "add_node", "node": {"id": nid, "name": name, "type": type_, **kw}}


def line(est, name):
    return next(ln for ln in est["lines"] if ln["name"] == name)


def test_assumptions_come_from_requirements_and_memory():
    s = state(req("REQ-001", "Support 5,000 concurrent users at launch."),
              req("REQ-002", "The API handles 300 requests per second at peak."),
              req("REQ-003", "Store 2 TB of scanned documents."),
              req("REQ-004", "The service is available 99.95% of the time."))
    a = costs.assumptions(s, [{"text": "We expect 80k monthly active users"}])
    assert a["peak_rps"] == {"value": 1000.0, "source": "REQ-001", "text": a["peak_rps"]["text"]}  # 5,000 × 0.2
    assert a["monthly_active_users"]["value"] == 80000 and a["monthly_active_users"]["source"] == "memory"
    assert a["storage_gb"]["value"] == 2048 and a["availability"]["value"] == 99.95
    assert a["region"] == {"value": "us", "source": "default"}
    over = costs.assumptions(s, [], {"peak_rps": 50})
    assert over["peak_rps"] == {"value": 50, "source": "you"}


def test_prices_lines_by_kind_and_vendor_with_ha_doubling():
    ops = [node("api", "Shop API", "service", vendor="aws", catalog_id="aws-ecs-fargate"),
           node("db", "Shop DB", "database", vendor="aws", catalog_id="aws-rds-postgres"),
           node("fn", "Thumbnailer", "service", vendor="aws", catalog_id="aws-lambda"),
           node("q", "Jobs", "queue", vendor="aws", catalog_id="aws-sqs"),
           node("idp", "Login", "security", vendor="aws", catalog_id="aws-cognito"),
           node("note", "A note", "note")]
    est = costs.estimate(state(*ops), [], {"monthly_active_users": 30000, "peak_rps": 100})
    db = line(est, "Shop DB")
    assert db["kind"] == "db.relational" and db["vendor"] == "aws" and db["confidence"] == "medium"
    assert db["monthly"] == pytest.approx(2 * 0.076 * 730 + 100 * 0.115, rel=1e-3)  # single instance, 100 GB
    assert line(est, "Login")["monthly"] == pytest.approx(20000 * 0.0055, rel=1e-3)  # 10k MAU free
    assert line(est, "Thumbnailer")["confidence"] == "low" and line(est, "Jobs")["monthly"] > 0
    assert "A note" not in {ln["name"] for ln in est["lines"]} and est["high_availability"] is False
    assert any(ln["kind"] == "transfer" for ln in est["lines"])  # no CDN: data transfer is its own line
    assert est["total"]["monthly"] == pytest.approx(est["production"]["monthly"] * 1.3, rel=1e-3)

    ha = costs.estimate(state(*ops, req("REQ-9", "Available 99.99% of the time.")), [],
                        {"monthly_active_users": 30000, "peak_rps": 100})
    assert ha["high_availability"] and line(ha, "Shop DB")["monthly"] == pytest.approx(2 * db["monthly"], rel=1e-3)
    assert "multi-AZ" in line(ha, "Shop DB")["basis"]


def test_component_properties_pin_numbers_and_unknown_platforms_are_noted():
    s = state(node("helpdesk", "Helpdesk", "external", technology="Zendesk", props={"monthly_cost": "$1,200"}),
              node("svc", "Worker", "service", props={"instances": "3", "vcpu": "2"}),
              node("ext", "Partner API", "external"))
    est = costs.estimate(s, [], {"include_staging": False})
    assert line(est, "Helpdesk")["monthly"] == 1200 and line(est, "Helpdesk")["confidence"] == "high"
    worker = line(est, "Worker")
    assert worker["basis"].startswith("3 × 2 vCPU") and "priced like AWS" in worker["notes"][0]
    assert line(est, "Partner API")["monthly"] == 0 and est["staging"]["monthly"] == 0


def test_llm_lines_use_the_model_price_and_dominate_the_support_template():
    est = costs.estimate(build_template("ai-customer-support"), [], {"monthly_active_users": 10000})
    claude = line(est, "Claude")
    assert claude["kind"] == "ai.llm" and "claude-opus" in claude["basis"]
    assert claude["monthly"] == pytest.approx(50000 * (2000 * 15 + 500 * 75) / 1e6, rel=1e-3)
    assert est["drivers"][0]["name"] == "Claude"
    recs = costs.offline_recommendations(est)
    assert any(r["title"] == "Route simple requests to a smaller model" for r in recs)


def test_budget_from_a_cost_requirement():
    s = state(req("REQ-001", "Hosting must cost less than $1,000 per month.", category="cost"),
              node("db", "DB", "database", vendor="aws", catalog_id="aws-rds-postgres"))
    est = costs.estimate(s)
    assert est["budget"]["monthly"] == 1000 and est["budget"]["status"] == "within"
    s = state(req("REQ-001", "Annual cloud budget of 6k USD per year.", category="cost"),
              node("db", "DB", "database", vendor="aws", catalog_id="aws-rds-postgres"))
    est = costs.estimate(s, [], {"peak_rps": 6000})
    assert est["budget"]["monthly"] == 500 and est["budget"]["status"] == "over"


def test_ai_recommendations_are_grounded_and_capped(monkeypatch):
    s = build_template("ai-customer-support")
    est = costs.estimate(s)
    monkeypatch.setattr(llm, "complete_json", lambda system, messages, schema, **kw: {"recommendations": [
        {"title": "Cache answers", "detail": "x", "saving_monthly": 999999, "components": ["Claude", "Nonexistent"],
         "tradeoff": "stale answers", "effort": "low"},
        {"title": "", "detail": "dropped", "saving_monthly": 1, "components": [], "tradeoff": "", "effort": "low"}],
        "questions": ["How many conversations a day?"]})
    recs, questions, engine = costs.recommend(est, s, "Support", None, use_ai=True)
    assert engine == "llm" and len(recs) == 1 and recs[0]["components"] == ["Claude"]
    assert recs[0]["saving_monthly"] == line(est, "Claude")["monthly"] and questions


@pytest.fixture()
def client():
    app = create_app(Store(":memory:"))
    with new_client(app, base_url=BASE) as c:
        signup(c)
        yield c
    tracing.set_sink(None)


def test_api_estimate_and_csv(client):
    pid = client.post("/api/projects", json={"name": "Support", "template": "ai-customer-support"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/costs", json={"assumptions": {"monthly_active_users": 2000, "region": "eu",
                                                                        "peak_rps": "bad", "availability": 250}})
    assert r.status_code == 200, r.text
    est = r.json()
    assert est["engine"] == "offline" and est["assumptions"]["monthly_active_users"] == {"value": 2000, "source": "you"}
    assert est["assumptions"]["availability"]["value"] == 100.0 and est["assumptions"]["peak_rps"]["source"] == "derived"
    assert est["recommendations"] and "eu region" in est["disclaimer"]
    csv = client.get(f"/api/projects/{pid}/costs.csv")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert csv.text.splitlines()[0].startswith("Component,Category") and "Total per year" in csv.text
