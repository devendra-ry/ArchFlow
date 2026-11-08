"""Generated infrastructure as code, tests and documents: offline output per target on FoodRush, well-formedness
checks, the AI refinement (faked model), endpoint authz, the zip and the MCP tool."""

import ast
import io
import os
import zipfile

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import foodrush  # noqa: E402
import pytest  # noqa: E402
import yaml  # noqa: E402
from authhelp import member_client, new_client, signup  # noqa: E402

from archflow import chat, generation, llm  # noqa: E402
from archflow.agents import tracing  # noqa: E402
from archflow.generation import File  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState  # noqa: E402
from archflow.ops import apply_ops  # noqa: E402
from archflow.store import Store  # noqa: E402

from test_mcp_remote import BASE, oauth_login, tool  # noqa: E402


@pytest.fixture(scope="module")
def food():
    state, _ = apply_ops(ProjectState(), chat.architecture_ops(ProjectState(), foodrush.flawed_option(),
                                                                foodrush.requirements()))
    return generation.Design(name="FoodRush", branch="main", state=state, description=foodrush.IDEA)


def files_of(res):
    return {f.path: f.content for f in res.files}


@pytest.mark.parametrize("kind,target", [(k, t) for k, ts in generation.TARGETS.items() for t in ts])
def test_every_target_is_well_formed_and_deterministic(food, kind, target):
    res = generation.offline(food, kind, target)
    assert res.files and res.engine == "offline"
    assert all(c["ok"] for c in res.checks), [c for c in res.checks if not c["ok"]]
    assert files_of(res) == files_of(generation.offline(food, kind, target))


def test_terraform_targets_the_designs_cloud(food):
    res = generation.offline(food, "iac", "terraform")
    assert res.cloud == "aws"  # every managed component is on AWS
    f = files_of(res)
    assert set(f) >= {"terraform/versions.tf", "terraform/providers.tf", "terraform/main.tf",
                      "terraform/variables.tf", "terraform/outputs.tf"}
    main = f["terraform/main.tf"]
    assert 'resource "aws_eks_cluster" "main"' in main  # services say "on EKS"
    assert 'resource "aws_rds_cluster" "orders_delivery_database"' in main and "aurora-postgresql" in main
    assert 'resource "aws_db_instance" "restaurant_menu_database"' in main and "multi_az                  = true" in main
    assert "aws_msk_serverless_cluster" in main and "aws_elasticache_replication_group" in main
    assert 'resource "aws_secretsmanager_secret" "payment_gateway_api_key"' in main  # Stripe: credentials only
    assert "Courier App: client app" in main
    assert '"eu-central-1"' in f["terraform/variables.tf"]  # GDPR / EU requirement
    assert 'variable "db_password"' in f["terraform/variables.tf"] and "sensitive   = true" in f["terraform/variables.tf"]
    assert "db_password" not in f["terraform/terraform.tfvars.example"]
    for cloud, marker in (("azure", "azurerm_resource_group"), ("gcp", "google_container_cluster")):
        other = generation.offline(food, "iac", "terraform", cloud)
        assert other.cloud == cloud and marker in files_of(other)["terraform/main.tf"]
        assert all(c["ok"] for c in other.checks)
    with pytest.raises(ValueError):
        generation.offline(food, "iac", "terraform", "ibm")


def test_hcl_checks_catch_broken_files():
    good = File("terraform/main.tf", 'resource "aws_s3_bucket" "a" {\n  bucket = "x-${var.project}"\n}\n')
    variables = File("terraform/variables.tf", 'variable "project" {\n  type = string\n}\n')
    assert all(c["ok"] for c in generation.check_hcl([good, variables]))
    unbalanced = File("terraform/main.tf", 'resource "aws_s3_bucket" "a" {\n  bucket = "x"\n')
    assert "unclosed" in generation.check_hcl([unbalanced, variables])[0]["detail"]
    dup = File("terraform/main.tf", good.content + good.content)
    assert "duplicate resource" in generation.check_hcl([dup, variables])[0]["detail"]
    undeclared = File("terraform/main.tf", 'resource "aws_s3_bucket" "a" {\n  bucket = var.nope\n}\n')
    assert any("undeclared variables: nope" in c["detail"] for c in generation.check_hcl([undeclared, variables]))
    dangling = File("terraform/main.tf", 'resource "aws_s3_bucket" "a" {\n  bucket = aws_s3_bucket.b.id\n}\n')
    assert any("undefined resources" in c["detail"] for c in generation.check_hcl([dangling, variables]))
    unterminated = File("terraform/main.tf", 'resource "aws_s3_bucket" "a" {\n  bucket = "x\n}\n')
    assert not generation.check_hcl([unterminated, variables])[0]["ok"]


def test_compose_and_k8s(food):
    f = files_of(generation.offline(food, "iac", "compose"))
    doc = yaml.safe_load(f["docker-compose.yml"])
    svc = doc["services"]
    assert svc["orders-delivery-database"]["image"].startswith("postgres")  # Aurora runs as PostgreSQL locally
    assert svc["restaurant-menu-database"]["image"].startswith("postgis/")
    assert svc["event-bus"]["image"].startswith("apache/kafka") and svc["api-gateway"]["image"].startswith("kong")
    order = svc["order-management-service"]
    assert order["build"] == "./services/order-management-service"
    assert order["environment"]["PAYMENT_GATEWAY_API_KEY"] == "${PAYMENT_GATEWAY_API_KEY:-}"
    assert order["environment"]["ORDERS_DELIVERY_DATABASE_URL"].startswith("postgresql://")
    assert order["depends_on"]["orders-delivery-database"] == {"condition": "service_healthy"}
    assert "courier-app" not in svc and "PAYMENT_GATEWAY_API_KEY=" in f[".env.example"]
    assert "gateway/kong.yml" in f

    res = generation.offline(food, "iac", "k8s")
    f = files_of(res)
    docs = list(yaml.safe_load_all(f["k8s/order-management-service.yaml"]))
    kinds = [d["kind"] for d in docs]
    assert kinds == ["Deployment", "Service", "HorizontalPodAutoscaler", "PodDisruptionBudget"]
    assert docs[0]["spec"]["replicas"] == 3 and "&id" not in f["k8s/order-management-service.yaml"]
    assert "postgresql://app@orders-delivery-database:5432/app" in f["k8s/config.yaml"]  # no password in config
    assert "DB_PASSWORD" in f["k8s/secrets.example.yaml"]
    kust = yaml.safe_load(f["k8s/kustomization.yaml"])
    assert "order-management-service.yaml" in kust["resources"] and kust["namespace"] == "foodrush"
    assert any("Data stores" in n for n in res.notes)

    bad = [File("k8s/x.yaml", "apiVersion: v1\nkind: Service\nmetadata: {name: Bad_Name}\nspec: {selector: {a: b}}\n")]
    problems = generation.check_yaml(bad, "k8s")
    assert not problems[0]["ok"] and any("selects no Deployment" in p["detail"] for p in problems)
    assert not generation.check_yaml([File("docker-compose.yml", "services: [")], "compose")[0]["ok"]


def test_tests_targets(food):
    f = files_of(generation.offline(food, "tests", "gherkin"))
    feature = f["features/req-002-order-and-payment-processing.feature"]
    assert feature.startswith("@REQ-002") and "Scenario: Payment is securely processed via Stripe" in feature
    assert "Given FoodRush is running\n    And a customer is signed in" in feature
    assert "Then payment is securely processed via Stripe" in feature
    nfr = f["features/req-005-eu-gdpr-compliance.feature"]
    assert "@nfr" in nfr and "@compliance" in nfr
    checklist = f["tests/contracts/CHECKLIST.md"]
    assert "## Order Management Service → Payment Gateway (HTTPS, third-party API)" in checklist
    assert "Payments are idempotent" in checklist and "(WSS, WebSocket)" in checklist and "buf breaking" in checklist

    f = files_of(generation.offline(food, "tests", "pytest"))
    src = f["tests/acceptance/test_req_001_geospatial_restaurant_search.py"]
    ast.parse(src)
    assert "def test_results_return_within_300ms_using_postgis_indexi(base_url):" in src
    f = files_of(generation.offline(food, "tests", "playwright"))
    assert "test.fixme(\"Couriers transmit location every 1-3 seconds\"" in f["e2e/req-004-high-frequency-live-tracking.spec.ts"]

    # criteria already written as Given/When/Then are split, not wrapped
    state, _ = apply_ops(ProjectState(), [{"op": "add_requirement", "requirement": {
        "title": "Lookup", "acceptance_criteria": ["Given a signed-in agent, when they search an order id, then the order shows"]}}])
    d = generation.Design(name="S", branch="main", state=state)
    text = "".join(files_of(generation.offline(d, "tests", "gherkin")).values())
    assert "Given a signed-in agent\n    When they search an order id\n    Then the order shows" in text
    bad = File("features/x.feature", "Feature: X\n  Scenario: s\n    Then a\n    Given b\n")
    assert not generation.check_tests([bad], "gherkin")[0]["ok"]


def test_documents(food):
    srs = files_of(generation.offline(food, "docs", "srs"))["docs/SRS.md"]
    for h in ("## 1. Introduction", "### 1.1 Purpose", "### 1.2 Scope", "### 3.2 Functional requirements",
              "### 3.3 Non-functional requirements", "#### 3.1.3 Communications interfaces", "## Appendix A. Traceability"):
        assert h in srs
    assert "#### REQ-001: Geospatial Restaurant Search" in srs and "| Order Management Service | Payment Gateway | HTTPS |" in srs
    brd = files_of(generation.offline(food, "docs", "brd"))["docs/BRD.md"]
    assert "## 7. Success metrics" in brd and "50,000 orders per day" in brd and "## 9. Risks" in brd
    f = files_of(generation.offline(food, "docs", "runbook"))
    assert "docs/runbooks/README.md" in f and "docs/runbooks/payment-gateway.md" not in f  # externals: no runbook
    rb = f["docs/runbooks/restaurant-search-service.md"]
    assert "p95 latency above 300 ms" in rb and "99.9% availability" in rb
    db = f["docs/runbooks/orders-delivery-database.md"]
    assert "## Backups and recovery" in db and "point-in-time recovery" in db


def fake_files(content_for):
    def complete_json(system, messages, schema, **kw):
        return content_for(system, messages)
    return complete_json


def test_ai_refinement_keeps_only_well_formed_grounded_files(food, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    base = files_of(generation.offline(food, "docs", "srs"))["docs/SRS.md"]
    seen = {}

    def answer(system, messages):
        seen["prompt"] = system + messages[-1]["content"]
        return {"files": [{"path": "docs/SRS.md", "content": base.replace("This document specifies", "This SRS specifies")},
                          {"path": "../etc/passwd", "content": "x"}], "notes": ["Decide the RTO."]}

    monkeypatch.setattr(llm, "complete_json", fake_files(answer))
    with tracing.listen(lambda t: None) as traces:
        res = generation.generate(food, "docs", "srs", use_ai=True)
    assert res.engine == "ai" and traces[-1]["prompt"] == "generate.docs@v1"
    assert "This SRS specifies" in files_of(res)["docs/SRS.md"] and "Decide the RTO." in res.notes
    assert "Payment Gateway" in seen["prompt"] and "REQ-005" in seen["prompt"]

    # a rewrite that drops sections or cites unknown ids is refused; nothing usable → the built-in version
    monkeypatch.setattr(llm, "complete_json", fake_files(lambda s, m: {"files": [
        {"path": "docs/SRS.md", "content": "# SRS\n\n## 1. Introduction\n"},
        {"path": "docs/SRS.md", "content": base + "\nSee REQ-999.\n"}]}))
    res = generation.generate(food, "docs", "srs", use_ai=True)
    assert res.engine == "offline" and files_of(res)["docs/SRS.md"] == base
    assert "could not refine" in res.notes[-1]

    tf = files_of(generation.offline(food, "iac", "terraform"))["terraform/main.tf"]
    monkeypatch.setattr(llm, "complete_json", fake_files(lambda s, m: {"files": [
        {"path": "terraform/main.tf", "content": tf + 'resource "aws_s3_bucket" "x" {\n'},
        {"path": "terraform/backups.tf", "content": 'resource "aws_backup_vault" "main" {\n  name = var.project\n}\n'}]}))
    res = generation.generate(food, "iac", "terraform", use_ai=True)
    f = files_of(res)
    assert res.engine == "ai" and f["terraform/main.tf"] == tf and "terraform/backups.tf" in f
    assert any("not well-formed" in n for n in res.notes) and all(c["ok"] for c in res.checks)


def test_ai_tests_become_scenarios(food, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", fake_files(lambda s, m: {
        "features": [{"requirement_id": "REQ-002", "scenarios": [
            {"name": "Declined card", "given": ["a customer has a basket"], "when": ["they pay with a declined card"],
             "then": ["no order is created", "the error says the card was declined"]},
            {"name": "Missing steps", "given": [], "when": ["x"], "then": ["y"]}]},
            {"requirement_id": "REQ-999", "scenarios": [{"name": "n", "given": ["a"], "when": ["b"], "then": ["c"]}]}],
        "contracts": [{"from": "Order Management Service", "to": "Payment Gateway",
                       "checks": ["Stripe webhooks are verified with the signing secret"]}]}))
    res = generation.generate(food, "tests", "gherkin", use_ai=True)
    f = files_of(res)
    feature = f["features/req-002-order-and-payment-processing.feature"]
    assert res.engine == "ai" and "Scenario: Declined card" in feature and "Missing steps" not in feature
    assert "    Then no order is created\n    And the error says the card was declined" in feature
    assert "REQ-999" not in "".join(f.values())
    assert "Stripe webhooks are verified" in f["tests/contracts/CHECKLIST.md"]
    assert all(c["ok"] for c in res.checks)


def test_endpoints_authz_and_zip(monkeypatch):
    with new_client(create_app(Store(":memory:")), base_url=BASE) as owner:
        signup(owner)
        pid = owner.post("/api/projects", json={"name": "Support Bot", "template": "ai-customer-support"}).json()["id"]
        viewer = member_client(owner.app, owner, "viewer", "viewer@example.com")
        r = viewer.post(f"/api/projects/{pid}/generate", json={"kind": "iac", "target": "terraform"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["engine"] == "offline" and data["valid"] and data["branch"] == "main"
        assert any(f["path"] == "terraform/main.tf" for f in data["files"])
        r = viewer.post(f"/api/projects/{pid}/generate", json={"kind": "docs", "target": "srs", "ai": True})
        assert r.status_code == 403 and "editors" in r.json()["detail"]
        assert viewer.post(f"/api/projects/{pid}/generate", json={"kind": "iac", "target": "srs"}).status_code == 422
        assert viewer.post(f"/api/projects/{pid}/generate", json={"kind": "tests", "target": "gherkin",
                                                                  "branch": "nope"}).status_code == 404
        # owner with AI on but no model: built-in version with a note
        r = owner.post(f"/api/projects/{pid}/generate", json={"kind": "docs", "target": "runbook", "ai": True})
        assert r.status_code == 200 and r.json()["engine"] == "offline" and "No AI model" in r.json()["notes"][-1]

        r = viewer.post(f"/api/projects/{pid}/generate/zip", json={"kind": "tests", "target": "pytest"})
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert 'filename="Support-Bot-main-tests-pytest.zip"' in r.headers["content-disposition"]
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert "Support-Bot-main-tests-pytest/tests/contracts/CHECKLIST.md" in names
        r = viewer.post(f"/api/projects/{pid}/generate/zip", json={"kind": "docs", "target": "srs",
                                                                   "files": [{"path": "docs/SRS.md", "content": "# Edited"}]})
        z = zipfile.ZipFile(io.BytesIO(r.content))
        assert z.read("Support-Bot-main-docs-srs/docs/SRS.md") == b"# Edited"
        for bad in ("../x.md", "/etc/passwd"):
            r = viewer.post(f"/api/projects/{pid}/generate/zip", json={"kind": "docs", "target": "srs",
                                                                       "files": [{"path": bad, "content": "x"}]})
            assert r.status_code == 422

        outsider = new_client(owner.app, base_url=BASE)
        signup(outsider, "out@example.com", "Out")
        assert outsider.post(f"/api/projects/{pid}/generate",
                             json={"kind": "iac", "target": "k8s"}).status_code in (403, 404)


def test_generate_artifacts_mcp_tool():
    store = Store(":memory:")
    with new_client(create_app(store), base_url=BASE) as client:
        signup(client)
        pid = client.post("/api/projects", json={"name": "Support Bot", "template": "ai-customer-support"}).json()["id"]
        _, tokens = oauth_login(client, access="read", scope="archflow:read")
        at = tokens["access_token"]
        err, data, text = tool(client, at, "generate_artifacts", project_id=pid, kind="iac", target="k8s")
        assert not err, text
        assert "k8s/kustomization.yaml" in text and '"engine":"offline"' in text.replace(" ", "")
        err, _, text = tool(client, at, "generate_artifacts", project_id=pid, kind="docs", target="srs", use_ai=True)
        assert err and "read-only" in text
        err, _, text = tool(client, at, "generate_artifacts", project_id=pid, kind="iac", target="pdf")
        assert err and "target" in text
