"""Generated artifacts from a branch's architecture and requirements: infrastructure as code, tests and documents.

Kinds and targets:

- ``iac``    ``terraform`` (AWS / Azure / Google Cloud, chosen from the components' catalog mappings: providers,
             variables, one resource per component, outputs), ``compose`` (docker-compose for a local / open-source
             stack: managed services become their open-source equivalent from the catalog) and ``k8s`` (Deployments,
             Services, autoscalers, config and a kustomization for the services);
- ``tests``  ``gherkin`` (.feature files from the requirements' acceptance criteria), ``pytest`` and ``playwright``
             skeletons of the same scenarios; every target adds a contract/integration checklist per connection,
             aware of its protocol (HTTP, gRPC, WebSocket, SQL, cache, messaging, third-party APIs);
- ``docs``   ``srs`` (IEEE 830-style software requirements specification), ``brd`` (business requirements
             document) and ``runbook`` (one per service and data store: health checks, scaling, failure modes,
             backups and alerts from the component types and the NFRs).

Every target has a deterministic version built from the model alone (the same inputs give the same bytes; it works
without an AI model). With AI on, an agent refines it: ``generate.iac`` and ``generate.docs`` rewrite the files (a
rewritten file is kept only when it is still well-formed and cites only real requirement ids), ``generate.tests``
writes the scenarios and checks that the deterministic renderers turn into files. Nothing is stored: the files are
returned for preview and download (zip).
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml
from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from . import agents, catalog, exports, llm
from .analysis import full_analysis
from .change_spec import slug
from .diagrams import placement
from .models import Node, ProjectState, Requirement, without_deleted

log = logging.getLogger("archflow.generation")

TARGETS: dict[str, tuple[str, ...]] = {
    "iac": ("terraform", "compose", "k8s"),
    "tests": ("gherkin", "pytest", "playwright"),
    "docs": ("srs", "brd", "runbook"),
}
PROMPTS = {"iac": "generate.iac", "tests": "generate.tests", "docs": "generate.docs"}
CLOUDS = ("aws", "azure", "gcp")
CLOUD_LABELS = {"aws": "AWS", "azure": "Azure", "gcp": "Google Cloud"}
MAX_AI_FILES = 40
MAX_FILE_CHARS = 60_000
MAX_CONTEXT_CHARS = 48_000
MAX_ZIP_FILES = 300
MAX_ZIP_BYTES = 5_000_000

WORKLOAD_TYPES = ("service", "agent", "mcp")
DATA_TYPES = ("database", "cache", "queue", "storage")
EXTERNAL_TYPES = ("external", "llm")
DEFAULT_KIND = {"service": "compute.container", "agent": "compute.container", "mcp": "compute.container",
                "database": "db.relational", "cache": "cache.kv", "queue": "msg.queue", "storage": "storage.object",
                "gateway": "edge.gateway", "security": "security.identity", "llm": "ai.llm", "external": "external"}


class _PlainDumper(yaml.SafeDumper):
    """YAML without anchors / aliases (repeated label maps are written out), as people write manifests."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _dump(data: Any, **kw: Any) -> str:
    return yaml.dump(data, Dumper=_PlainDumper, allow_unicode=True, **kw)


def _dump_all(docs: list[Any], **kw: Any) -> str:
    return yaml.dump_all(docs, Dumper=_PlainDumper, allow_unicode=True, **kw)


@dataclass
class File:
    path: str
    content: str

    def dump(self) -> dict[str, str]:
        return {"path": self.path, "content": self.content}


@dataclass
class Result:
    kind: str
    target: str
    files: list[File]
    engine: str = "offline"
    notes: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    cloud: str = ""

    def dump(self) -> dict[str, Any]:
        out = {"kind": self.kind, "target": self.target, "engine": self.engine, "notes": self.notes,
               "files": [f.dump() for f in self.files], "checks": self.checks,
               "valid": all(c["ok"] for c in self.checks)}
        if self.cloud:
            out["cloud"] = self.cloud
        return out


# --------------------------------------------------------------------------- what generation reads
@dataclass
class Design:
    name: str
    branch: str
    state: ProjectState
    description: str = ""
    decisions: list[dict[str, Any]] = field(default_factory=list)
    memory: list[dict[str, Any]] = field(default_factory=list)
    version: int | None = None

    @property
    def top(self) -> list[Node]:
        return [n for n in exports._top(self.state) if n.type != "note"]

    @property
    def functional(self) -> list[Requirement]:
        return exports._requirements(self.state, "functional")

    @property
    def nfrs(self) -> list[Requirement]:
        return exports._requirements(self.state, "non_functional")

    def text(self) -> str:
        """Everything a constraint may be written in (requirements, settled facts, decisions), lowercased."""
        parts = [self.description]
        for r in self.state.requirements.values():
            parts += [r.title, r.description, r.category, *r.acceptance_criteria]
        parts += [m.get("text", "") for m in self.memory]
        parts += [d.get("decision", "") or "" for d in self.decisions if d.get("status") == "accepted"]
        return " ".join(parts).lower()


def design_from(bundle: exports.Bundle) -> Design:
    return Design(name=bundle.name, branch=bundle.branch, state=without_deleted(bundle.state),
                  description=exports._one_line(bundle.project.get("description") or ""), decisions=bundle.decisions,
                  memory=bundle.memory, version=bundle.head_version)


def collect(store: Any, pid: str, branch: str, state: ProjectState | None = None) -> Design:
    return design_from(exports.collect(store, pid, branch, state))


# --------------------------------------------------------------------------- component facts
def kind_of(node: Node) -> str:
    """The catalog kind of a component (``db.relational``, ``compute.container``…), else a default for its type."""
    entry = catalog.match(node.model_dump(include={"catalog_id", "technology", "name"}))
    if entry and entry.get("kind"):
        return entry["kind"]
    return DEFAULT_KIND.get(node.type, "")


def is_workload(node: Node) -> bool:
    """Code the team builds and runs (services, agents, MCP servers, and gateways that run as their own app)."""
    return node.type in WORKLOAD_TYPES or (node.type == "gateway" and kind_of(node).startswith("compute."))


def runtime_of(node: Node) -> str:
    """container | kubernetes | function | vm for a workload."""
    kind = kind_of(node)
    if kind == "compute.function":
        return "function"
    if kind == "compute.container":
        return "container"
    label = placement(node)[1]
    return {"Kubernetes": "kubernetes", "Serverless": "function", "Virtual machines": "vm"}.get(label, "container")


def _tech(node: Node) -> str:
    return f"{node.name} {node.technology}".lower()


def _ha(d: Design) -> bool:
    text = d.text()
    return any(r.category.lower() in ("availability", "reliability") for r in d.nfrs) or bool(
        re.search(r"\b(uptime|high availability|highly available|failover|99\.9)", text))


def _eu(d: Design) -> bool:
    return bool(re.search(r"\b(eu|gdpr|europe|european)\b", d.text()))


def _scales(d: Design) -> bool:
    return any(r.category.lower() in ("performance", "scalability") for r in d.nfrs) or bool(
        re.search(r"\b(scale|scalab|peak|burst|per day|per second|rps|concurrent)\w*", d.text()))


def choose_cloud(d: Design, requested: str = "") -> tuple[str, list[str]]:
    """The cloud the design targets: the one most of its components map to (ties: AWS, Azure, Google Cloud)."""
    if requested:
        if requested not in CLOUDS:
            raise ValueError(f"cloud must be one of {', '.join(CLOUDS)}")
        return requested, []
    counts = {c: 0 for c in CLOUDS}
    for n in d.top:
        platform = placement(n)[0]
        if platform in counts:
            counts[platform] += 1
    best = max(CLOUDS, key=lambda c: (counts[c], -CLOUDS.index(c)))
    if counts[best]:
        return best, []
    return "aws", ["No component names a cloud, so the Terraform targets AWS. Choose another cloud to change it."]


def _env_name(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
    return s if s and not s[0].isdigit() else f"C_{s}"


def _ident(text: str, used: set[str] | None = None) -> str:
    """An HCL identifier (unique within ``used`` when given)."""
    base = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:48] or "component"
    if base[0].isdigit():
        base = f"c_{base}"
    name, k = base, 2
    while used is not None and name in used:
        name, k = f"{base}_{k}", k + 1
    if used is not None:
        used.add(name)
    return name


def _dns(text: str, used: set[str] | None = None, limit: int = 50) -> str:
    """A DNS-1123 label: lowercase letters, digits and '-', starting with a letter."""
    base = slug(text, limit)
    name, k = base, 2
    while used is not None and name in used:
        name, k = f"{base[:limit - 3]}-{k}", k + 1
    if used is not None:
        used.add(name)
    return name


def _outgoing(d: Design, node: Node) -> list[tuple[Node, str]]:
    return [(d.state.nodes[e.target], e.protocol) for e in exports._edges(d.state)
            if e.source == node.id and e.target in d.state.nodes]


def _incoming(d: Design, node: Node) -> list[tuple[Node, str]]:
    return [(d.state.nodes[e.source], e.protocol) for e in exports._edges(d.state)
            if e.target == node.id and e.source in d.state.nodes]


# =========================================================================== infrastructure as code
# --------------------------------------------------------------------------- a tiny HCL writer
class Raw(str):
    """An HCL expression written as it is (a reference, a function call)."""


class Blk(list):
    """A nested block: a list of (key, value) pairs."""


def blk(*pairs: tuple[str, Any]) -> Blk:
    return Blk(pairs)


def _hcl_value(v: Any, indent: int) -> str:
    if isinstance(v, Raw):
        return str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_hcl_value(x, indent) for x in v) + "]"
    if isinstance(v, dict):
        pad = "  " * (indent + 1)
        inner = "\n".join(f"{pad}{k} = {_hcl_value(x, indent + 1)}" for k, x in v.items())
        return "{\n" + inner + "\n" + "  " * indent + "}"
    raise TypeError(type(v))


def _hcl_body(pairs: list[tuple[str, Any]], indent: int) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    width = max((len(k) for k, v in pairs if not isinstance(v, Blk) and not k.startswith("#")), default=0)
    for k, v in pairs:
        if k.startswith("#"):
            lines.append(f"{pad}{k}")
        elif isinstance(v, Blk):
            lines.append(f"{pad}{k} {{")
            lines += _hcl_body(list(v), indent + 1)
            lines.append(f"{pad}}}")
        else:
            lines.append(f"{pad}{k.ljust(width)} = {_hcl_value(v, indent)}")
    return lines


@dataclass
class TfBlock:
    kind: str  # resource | data | module
    type: str  # resource type ("" for module)
    name: str
    body: list[tuple[str, Any]]
    comment: str = ""

    @property
    def address(self) -> str:
        return f"{self.type}.{self.name}" if self.kind == "resource" else f"{self.kind}.{self.name}"

    def render(self) -> str:
        head = f'{self.kind} "{self.type}" "{self.name}" {{' if self.type else f'{self.kind} "{self.name}" {{'
        lines = [f"# {c}" for c in self.comment.splitlines() if c] + [head] + _hcl_body(self.body, 1) + ["}"]
        return "\n".join(lines)


@dataclass
class Tf:
    """What the Terraform files are built from."""
    cloud: str
    project: str
    ha: bool
    eu: bool
    variables: dict[str, dict[str, Any]] = field(default_factory=dict)
    shared: dict[str, TfBlock] = field(default_factory=dict)
    blocks: list[TfBlock] = field(default_factory=list)
    outputs: list[tuple[str, str, str]] = field(default_factory=list)  # (name, expression, description)
    skipped: list[str] = field(default_factory=list)
    names: set[str] = field(default_factory=set)

    def var(self, name: str, type_: str = "string", description: str = "", default: Any = None,
            sensitive: bool = False) -> Raw:
        self.variables.setdefault(name, {"type": type_, "description": description, "default": default,
                                         "sensitive": sensitive})
        return Raw(f"var.{name}")

    def share(self, key: str, make: Callable[[], TfBlock]) -> TfBlock:
        if key not in self.shared:
            self.shared[key] = make()
        return self.shared[key]

    def prefix(self, short: str, sep: str = "-") -> Raw:
        return Raw(f'"${{var.project}}{sep}${{var.environment}}{sep}{short}"')


REGIONS = {"aws": ("us-east-1", "eu-central-1"), "azure": ("eastus", "westeurope"), "gcp": ("us-central1", "europe-west1")}
PROVIDERS = {
    "aws": ('aws', 'hashicorp/aws', '~> 5.0'),
    "azure": ('azurerm', 'hashicorp/azurerm', '~> 4.0'),
    "gcp": ('google', 'hashicorp/google', '~> 6.0'),
}


def _r(type_: str, name: str, *pairs: tuple[str, Any], comment: str = "") -> TfBlock:
    return TfBlock("resource", type_, name, list(pairs), comment)


def _tags(tf: Tf, node: Node) -> tuple[str, Any]:
    local = "local.common_labels" if tf.cloud == "gcp" else "local.common_tags"
    return ("labels" if tf.cloud == "gcp" else "tags",
            Raw(f"merge({local}, {{ component = {json.dumps(slug(node.name, 40))} }})"))


def _image(tf: Tf, node: Node) -> Raw:
    tf.var("image_tag", description="Container image tag to deploy", default="latest")
    if tf.cloud == "aws":
        return Raw(f'"${{aws_ecr_repository.{_ident(node.name)}.repository_url}}:${{var.image_tag}}"')
    if tf.cloud == "azure":
        return Raw(f'"${{azurerm_container_registry.main.login_server}}/{slug(node.name, 40)}:${{var.image_tag}}"')
    return Raw(f'"${{var.region}}-docker.pkg.dev/${{var.gcp_project_id}}/'
               f'${{google_artifact_registry_repository.main.repository_id}}/{slug(node.name, 40)}:${{var.image_tag}}"')


def _engine(node: Node) -> str:
    t = _tech(node)
    return "mysql" if "mysql" in t or "mariadb" in t else "postgres"


# ---- AWS
def _aws(tf: Tf, node: Node, kind: str, n: str) -> list[TfBlock]:
    tags = _tags(tf, node)

    def subnets() -> Raw:
        return tf.var("private_subnet_ids", "list(string)", "Private subnets the services and data stores run in")

    if is_workload(node):
        rt = runtime_of(node)
        repo = _r("aws_ecr_repository", n, ("name", Raw(f'"${{var.project}}/{slug(node.name, 40)}"')),
                  ("image_tag_mutability", "IMMUTABLE"), ("image_scanning_configuration", blk(("scan_on_push", True))),
                  tags, comment=f"{node.name} ({node.technology or node.type}): container image")
        if rt == "kubernetes":
            tf.share("eks", lambda: _r(
                "aws_eks_cluster", "main", ("name", tf.prefix("eks")),
                ("role_arn", tf.var("eks_cluster_role_arn", description="IAM role of the EKS control plane")),
                ("vpc_config", blk(("subnet_ids", subnets()), ("endpoint_public_access", False))),
                ("encryption_config", blk(("resources", ["secrets"]), ("provider", blk(
                    ("key_arn", tf.var("kms_key_arn", description="KMS key for secrets and data at rest"))))))
                , comment="Kubernetes cluster for the services (deploy them with the k8s manifests)"))
            tf.share("eks_nodes", lambda: _r(
                "aws_eks_node_group", "main", ("cluster_name", Raw("aws_eks_cluster.main.name")),
                ("node_group_name", "default"),
                ("node_role_arn", tf.var("eks_node_role_arn", description="IAM role of the worker nodes")),
                ("subnet_ids", subnets()), ("instance_types", ["m6i.large"]),
                ("scaling_config", blk(("desired_size", 3 if tf.ha else 2), ("min_size", 3 if tf.ha else 1),
                                       ("max_size", 10)))))
            repo.comment += "; runs on aws_eks_cluster.main (see the k8s target)"
            return [repo]
        if rt == "function":
            return [repo, _r("aws_lambda_function", n, ("function_name", tf.prefix(slug(node.name, 40))),
                             ("role", tf.var("lambda_role_arn", description="IAM role of the Lambda functions")),
                             ("package_type", "Image"), ("image_uri", _image(tf, node)), ("timeout", 30),
                             ("memory_size", 512), tags)]
        if rt == "vm":
            return [_r("aws_instance", n, ("ami", tf.var("ami_id", description="Machine image of the VMs")),
                       ("instance_type", "t3.medium"), ("subnet_id", Raw(f"{subnets()}[0]")),
                       ("metadata_options", blk(("http_tokens", "required"))), tags,
                       comment=f"{node.name} ({node.technology or 'VM'})")]
        tf.share("ecs", lambda: _r("aws_ecs_cluster", "main", ("name", tf.prefix("cluster")),
                                   ("setting", blk(("name", "containerInsights"), ("value", "enabled")))))
        port = 8080
        task = _r("aws_ecs_task_definition", n, ("family", tf.prefix(slug(node.name, 40))),
                  ("requires_compatibilities", ["FARGATE"]), ("network_mode", "awsvpc"), ("cpu", "512"),
                  ("memory", "1024"),
                  ("execution_role_arn", tf.var("ecs_execution_role_arn", description="IAM role ECS uses to pull images")),
                  ("container_definitions", Raw("jsonencode([{ name = " + json.dumps(slug(node.name, 40))
                                                + ", image = " + _image(tf, node) + ", essential = true, "
                                                + f"portMappings = [{{ containerPort = {port} }}] }}])")))
        svc = _r("aws_ecs_service", n, ("name", slug(node.name, 40)), ("cluster", Raw("aws_ecs_cluster.main.id")),
                 ("task_definition", Raw(f"aws_ecs_task_definition.{n}.arn")),
                 ("desired_count", tf.var("replicas", "number", "Tasks / replicas per service", 3 if tf.ha else 2)),
                 ("launch_type", "FARGATE"),
                 ("network_configuration", blk(("subnets", subnets()), ("assign_public_ip", False))))
        return [repo, task, svc]
    if kind == "db.relational":
        pw = tf.var("db_password", description="Master password of the relational databases", sensitive=True)
        if "aurora" in _tech(node):
            return [_r("aws_rds_cluster", n, ("cluster_identifier", tf.prefix(slug(node.name, 30))),
                       ("engine", "aurora-mysql" if _engine(node) == "mysql" else "aurora-postgresql"),
                       ("master_username", "app"), ("master_password", pw), ("storage_encrypted", True),
                       ("backup_retention_period", 7), ("deletion_protection", True),
                       ("final_snapshot_identifier", tf.prefix(slug(node.name, 30) + "-final")), tags,
                       comment=f"{node.name} ({node.technology})")]
        return [_r("aws_db_instance", n, ("identifier", tf.prefix(slug(node.name, 30))), ("engine", _engine(node)),
                   ("instance_class", tf.var("db_instance_class", description="Instance size of the databases",
                                             default="db.t4g.medium")),
                   ("allocated_storage", 20), ("db_name", _ident(node.name)[:60]), ("username", "app"),
                   ("password", pw), ("storage_encrypted", True), ("multi_az", tf.ha),
                   ("backup_retention_period", 7), ("deletion_protection", True), ("skip_final_snapshot", False),
                   ("final_snapshot_identifier", tf.prefix(slug(node.name, 30) + "-final")), tags,
                   comment=f"{node.name} ({node.technology or 'relational database'})")]
    if kind == "db.document":
        return [_r("aws_dynamodb_table", n, ("name", tf.prefix(slug(node.name, 40))), ("billing_mode", "PAY_PER_REQUEST"),
                   ("hash_key", "id"), ("attribute", blk(("name", "id"), ("type", "S"))),
                   ("point_in_time_recovery", blk(("enabled", True))), ("server_side_encryption", blk(("enabled", True))),
                   tags, comment=f"{node.name} ({node.technology or 'document database'})")]
    if kind == "db.warehouse":
        return [_r("aws_redshiftserverless_namespace", n, ("namespace_name", tf.prefix(slug(node.name, 30))), tags,
                   comment=f"{node.name} ({node.technology or 'warehouse'})")]
    if kind in ("db.search", "db.vector"):
        return [_r("aws_opensearch_domain", n, ("domain_name", slug(node.name, 28)), ("engine_version", "OpenSearch_2.13"),
                   ("cluster_config", blk(("instance_type", "r6g.large.search"), ("instance_count", 3 if tf.ha else 1),
                                          ("zone_awareness_enabled", tf.ha))),
                   ("ebs_options", blk(("ebs_enabled", True), ("volume_size", 20))),
                   ("encrypt_at_rest", blk(("enabled", True))), ("node_to_node_encryption", blk(("enabled", True))),
                   ("vpc_options", blk(("subnet_ids", Raw(f"slice({subnets()}, 0, {3 if tf.ha else 1})")))),
                   tags, comment=f"{node.name} ({node.technology or 'search'})")]
    if kind == "cache.kv":
        return [_r("aws_elasticache_replication_group", n, ("replication_group_id", slug(node.name, 40)),
                   ("description", node.name), ("engine", "redis"), ("node_type", "cache.t4g.small"),
                   ("num_cache_clusters", 2 if tf.ha else 1), ("automatic_failover_enabled", tf.ha),
                   ("at_rest_encryption_enabled", True), ("transit_encryption_enabled", True), tags,
                   comment=f"{node.name} ({node.technology or 'cache'})")]
    if kind == "msg.queue":
        return [_r("aws_sqs_queue", n, ("name", tf.prefix(slug(node.name, 40))), ("sqs_managed_sse_enabled", True),
                   ("message_retention_seconds", 345600), tags, comment=f"{node.name} ({node.technology or 'queue'})")]
    if kind == "msg.stream":
        return [_r("aws_msk_serverless_cluster", n, ("cluster_name", tf.prefix(slug(node.name, 30))),
                   ("vpc_config", blk(("subnet_ids", subnets()))),
                   ("client_authentication", blk(("sasl", blk(("iam", blk(("enabled", True))))))), tags,
                   comment=f"{node.name} ({node.technology or 'event streaming'})")]
    if kind == "msg.pubsub":
        return [_r("aws_sns_topic", n, ("name", tf.prefix(slug(node.name, 40))), ("kms_master_key_id", "alias/aws/sns"),
                   tags, comment=f"{node.name} ({node.technology or 'pub/sub'})")]
    if kind == "storage.object":
        return [_r("aws_s3_bucket", n, ("bucket", tf.prefix(slug(node.name, 30))), tags,
                   comment=f"{node.name} ({node.technology or 'object storage'})"),
                _r("aws_s3_bucket_public_access_block", n, ("bucket", Raw(f"aws_s3_bucket.{n}.id")),
                   ("block_public_acls", True), ("block_public_policy", True), ("ignore_public_acls", True),
                   ("restrict_public_buckets", True))]
    if kind == "storage.file":
        return [_r("aws_efs_file_system", n, ("creation_token", tf.prefix(slug(node.name, 30))), ("encrypted", True),
                   tags, comment=f"{node.name} ({node.technology or 'file storage'})")]
    if kind == "security.identity":
        return [_r("aws_cognito_user_pool", n, ("name", tf.prefix(slug(node.name, 40))), ("mfa_configuration", "OPTIONAL"),
                   ("software_token_mfa_configuration", blk(("enabled", True))), tags,
                   comment=f"{node.name} ({node.technology or 'identity'})")]
    if kind == "security.secrets":
        return [_r("aws_secretsmanager_secret", n, ("name", tf.prefix(slug(node.name, 40), "/")), tags,
                   comment=f"{node.name} ({node.technology or 'secrets'})")]
    if kind == "security.waf":
        return [_r("aws_wafv2_web_acl", n, ("name", tf.prefix(slug(node.name, 40))), ("scope", "REGIONAL"),
                   ("default_action", blk(("allow", blk()))),
                   ("visibility_config", blk(("cloudwatch_metrics_enabled", True), ("metric_name", _ident(node.name)),
                                             ("sampled_requests_enabled", True))), tags,
                   comment=f"{node.name} ({node.technology or 'WAF'}): add the managed rule groups you need")]
    if kind == "edge.dns":
        return [_r("aws_route53_zone", n, ("name", tf.var("domain_name", description="The public domain")), tags,
                   comment=f"{node.name} ({node.technology or 'DNS'})")]
    if kind == "edge.lb":
        return [_r("aws_lb", n, ("name", slug(node.name, 32)), ("load_balancer_type", "application"),
                   ("subnets", tf.var("public_subnet_ids", "list(string)", "Public subnets of the load balancers")),
                   ("drop_invalid_header_fields", True), tags, comment=f"{node.name} ({node.technology or 'load balancer'})")]
    if kind == "edge.gateway":
        ws = bool(re.search(r"websocket|socket\.io|\bwss?\b|mqtt", _tech(node)))
        body: list[tuple[str, Any]] = [("name", tf.prefix(slug(node.name, 40))),
                                       ("protocol_type", "WEBSOCKET" if ws else "HTTP")]
        if ws:
            body.append(("route_selection_expression", "$request.body.action"))
        return [_r("aws_apigatewayv2_api", n, *body, tags, comment=f"{node.name} ({node.technology or 'API gateway'})")]
    if kind == "edge.cdn":
        return [_r("aws_cloudfront_distribution", n, ("enabled", True), ("comment", node.name),
                   ("origin", blk(("domain_name", tf.var("origin_domain", description="Origin the CDN serves")),
                                  ("origin_id", "primary"),
                                  ("custom_origin_config", blk(("http_port", 80), ("https_port", 443),
                                                               ("origin_protocol_policy", "https-only"),
                                                               ("origin_ssl_protocols", ["TLSv1.2"]))))),
                   ("default_cache_behavior", blk(("target_origin_id", "primary"),
                                                  ("viewer_protocol_policy", "redirect-to-https"),
                                                  ("allowed_methods", ["GET", "HEAD"]),
                                                  ("cached_methods", ["GET", "HEAD"]),
                                                  ("cache_policy_id", "658327ea-f89d-4fab-a63d-7e88639e58f6"))),
                   ("restrictions", blk(("geo_restriction", blk(("restriction_type", "none"))))),
                   ("viewer_certificate", blk(("cloudfront_default_certificate", True))), tags,
                   comment=f"{node.name} ({node.technology or 'CDN'})")]
    if kind == "ops.monitoring":
        return [_r("aws_cloudwatch_log_group", n, ("name", Raw(f'"/${{var.project}}/${{var.environment}}/{slug(node.name, 40)}"')),
                   ("retention_in_days", 30), tags, comment=f"{node.name} ({node.technology or 'monitoring'})")]
    if kind == "ops.workflow":
        return [_r("aws_sfn_state_machine", n, ("name", tf.prefix(slug(node.name, 40))),
                   ("role_arn", tf.var("sfn_role_arn", description="IAM role of the state machines")),
                   ("definition", Raw('jsonencode({ StartAt = "Start", States = { Start = { Type = "Pass", End = true } } })')),
                   tags, comment=f"{node.name} ({node.technology or 'workflow'}): replace the placeholder definition")]
    if kind == "ops.email":
        return [_r("aws_sesv2_email_identity", n, ("email_identity", tf.var("domain_name", description="The public domain")),
                   tags, comment=f"{node.name} ({node.technology or 'email'})")]
    return []


# ---- Azure
def _azure(tf: Tf, node: Node, kind: str, n: str) -> list[TfBlock]:
    tags = _tags(tf, node)
    rg = ("resource_group_name", Raw("azurerm_resource_group.main.name"))
    loc = ("location", Raw("azurerm_resource_group.main.location"))
    compact = re.sub(r"[^a-z0-9]", "", slug(node.name, 40))[:14]

    def workspace() -> TfBlock:
        return tf.share("law", lambda: _r("azurerm_log_analytics_workspace", "main", ("name", tf.prefix("logs")), loc, rg,
                                          ("sku", "PerGB2018"), ("retention_in_days", 30)))

    if is_workload(node):
        tf.share("acr", lambda: _r("azurerm_container_registry", "main",
                                   ("name", Raw('"${replace(var.project, "-", "")}${var.environment}acr"')), loc, rg,
                                   ("sku", "Premium" if tf.ha else "Standard"), ("admin_enabled", False)))
        rt = runtime_of(node)
        if rt == "kubernetes":
            tf.share("aks", lambda: _r(
                "azurerm_kubernetes_cluster", "main", ("name", tf.prefix("aks")), loc, rg,
                ("dns_prefix", Raw('"${var.project}-${var.environment}"')),
                ("default_node_pool", blk(("name", "system"), ("node_count", 3 if tf.ha else 2),
                                          ("vm_size", "Standard_D4s_v5"))),
                ("identity", blk(("type", "SystemAssigned"))),
                comment="Kubernetes cluster for the services (deploy them with the k8s manifests)"))
            tf.skipped.append(f"{node.name}: runs on azurerm_kubernetes_cluster.main; deploy it with the k8s target")
            return []
        if rt == "function":
            tf.share("func_storage", lambda: _r("azurerm_storage_account", "functions",
                                                ("name", Raw('"${replace(var.project, "-", "")}${var.environment}fn"')), loc, rg,
                                                ("account_tier", "Standard"), ("account_replication_type", "LRS"),
                                                ("min_tls_version", "TLS1_2")))
            tf.share("func_plan", lambda: _r("azurerm_service_plan", "functions", ("name", tf.prefix("functions")), loc, rg,
                                             ("os_type", "Linux"), ("sku_name", "Y1")))
            return [_r("azurerm_linux_function_app", n, ("name", tf.prefix(slug(node.name, 30))), loc, rg,
                       ("service_plan_id", Raw("azurerm_service_plan.functions.id")),
                       ("storage_account_name", Raw("azurerm_storage_account.functions.name")),
                       ("storage_account_access_key", Raw("azurerm_storage_account.functions.primary_access_key")),
                       ("https_only", True), ("site_config", blk()), tags,
                       comment=f"{node.name} ({node.technology or 'function'})")]
        workspace()
        tf.share("cae", lambda: _r("azurerm_container_app_environment", "main", ("name", tf.prefix("apps")), loc, rg,
                                   ("log_analytics_workspace_id", Raw("azurerm_log_analytics_workspace.main.id"))))
        return [_r("azurerm_container_app", n, ("name", slug(node.name, 32)),
                   ("container_app_environment_id", Raw("azurerm_container_app_environment.main.id")), rg,
                   ("revision_mode", "Single"),
                   ("template", blk(("min_replicas", 2 if tf.ha else 1), ("max_replicas", 10),
                                    ("container", blk(("name", slug(node.name, 32)), ("image", _image(tf, node)),
                                                      ("cpu", 0.5), ("memory", "1Gi"))))),
                   ("ingress", blk(("external_enabled", False), ("target_port", 8080),
                                   ("traffic_weight", blk(("latest_revision", True), ("percentage", 100))))),
                   tags, comment=f"{node.name} ({node.technology or 'service'})")]
    if kind == "db.relational":
        pw = tf.var("db_password", description="Administrator password of the databases", sensitive=True)
        mysql = _engine(node) == "mysql"
        body: list[tuple[str, Any]] = [("name", tf.prefix(slug(node.name, 30))), rg, loc,
                                       ("version", "8.0.21" if mysql else "16"),
                                       ("administrator_login", "app"), ("administrator_password", pw),
                                       ("sku_name", "GP_Standard_D2ds_v4" if mysql else "GP_Standard_D2s_v3"),
                                       ("backup_retention_days", 7), ("geo_redundant_backup_enabled", tf.ha)]
        if not mysql:
            body.append(("storage_mb", 32768))
        if tf.ha:
            body.append(("high_availability", blk(("mode", "ZoneRedundant"))))
        return [_r("azurerm_mysql_flexible_server" if mysql else "azurerm_postgresql_flexible_server", n, *body, tags,
                   comment=f"{node.name} ({node.technology or 'relational database'})")]
    if kind == "db.document":
        return [_r("azurerm_cosmosdb_account", n, ("name", tf.prefix(slug(node.name, 30))), loc, rg,
                   ("offer_type", "Standard"), ("kind", "GlobalDocumentDB"),
                   ("consistency_policy", blk(("consistency_level", "Session"))),
                   ("geo_location", blk(("location", Raw("azurerm_resource_group.main.location")), ("failover_priority", 0))),
                   ("backup", blk(("type", "Continuous"))), tags,
                   comment=f"{node.name} ({node.technology or 'document database'})")]
    if kind in ("db.search", "db.vector"):
        return [_r("azurerm_search_service", n, ("name", tf.prefix(slug(node.name, 30))), rg, loc, ("sku", "standard"),
                   ("replica_count", 2 if tf.ha else 1), tags, comment=f"{node.name} ({node.technology or 'search'})")]
    if kind == "cache.kv":
        return [_r("azurerm_redis_cache", n, ("name", tf.prefix(slug(node.name, 30))), loc, rg, ("capacity", 1),
                   ("family", "P" if tf.ha else "C"), ("sku_name", "Premium" if tf.ha else "Standard"),
                   ("minimum_tls_version", "1.2"), tags, comment=f"{node.name} ({node.technology or 'cache'})")]
    if kind == "msg.queue":
        tf.share("sb", lambda: _r("azurerm_servicebus_namespace", "main", ("name", tf.prefix("bus")), loc, rg,
                                  ("sku", "Premium" if tf.ha else "Standard")))
        return [_r("azurerm_servicebus_queue", n, ("name", slug(node.name, 40)),
                   ("namespace_id", Raw("azurerm_servicebus_namespace.main.id")),
                   ("dead_lettering_on_message_expiration", True),
                   comment=f"{node.name} ({node.technology or 'queue'})")]
    if kind == "msg.stream":
        tf.share("eh", lambda: _r("azurerm_eventhub_namespace", "main", ("name", tf.prefix("events")), loc, rg,
                                  ("sku", "Standard"), ("capacity", 2 if tf.ha else 1)))
        return [_r("azurerm_eventhub", n, ("name", slug(node.name, 40)),
                   ("namespace_id", Raw("azurerm_eventhub_namespace.main.id")), ("partition_count", 4),
                   ("message_retention", 1), comment=f"{node.name} ({node.technology or 'event streaming'})")]
    if kind == "msg.pubsub":
        return [_r("azurerm_eventgrid_topic", n, ("name", tf.prefix(slug(node.name, 30))), loc, rg, tags,
                   comment=f"{node.name} ({node.technology or 'pub/sub'})")]
    if kind in ("storage.object", "storage.file"):
        return [_r("azurerm_storage_account", n, ("name", Raw(f'"${{replace(var.project, "-", "")}}{compact}"')), rg, loc,
                   ("account_tier", "Standard"), ("account_replication_type", "ZRS" if tf.ha else "LRS"),
                   ("min_tls_version", "TLS1_2"), ("allow_nested_items_to_be_public", False), tags,
                   comment=f"{node.name} ({node.technology or 'storage'})")]
    if kind == "security.secrets":
        return [_r("azurerm_key_vault", n, ("name", Raw(f'"${{var.project}}-{compact[:8]}"')), loc, rg,
                   ("tenant_id", tf.var("tenant_id", description="Microsoft Entra tenant id")), ("sku_name", "standard"),
                   ("purge_protection_enabled", True), tags, comment=f"{node.name} ({node.technology or 'secrets'})")]
    if kind == "security.waf":
        return [_r("azurerm_web_application_firewall_policy", n, ("name", _ident(node.name)), rg, loc,
                   ("managed_rules", blk(("managed_rule_set", blk(("type", "OWASP"), ("version", "3.2"))))), tags,
                   comment=f"{node.name} ({node.technology or 'WAF'})")]
    if kind == "edge.cdn":
        return [_r("azurerm_cdn_frontdoor_profile", n, ("name", tf.prefix(slug(node.name, 30))), rg,
                   ("sku_name", "Standard_AzureFrontDoor"), tags, comment=f"{node.name} ({node.technology or 'CDN'})")]
    if kind == "edge.dns":
        return [_r("azurerm_dns_zone", n, ("name", tf.var("domain_name", description="The public domain")), rg, tags,
                   comment=f"{node.name} ({node.technology or 'DNS'})")]
    if kind == "edge.lb":
        return [_r("azurerm_lb", n, ("name", tf.prefix(slug(node.name, 30))), loc, rg, ("sku", "Standard"), tags,
                   comment=f"{node.name} ({node.technology or 'load balancer'})")]
    if kind == "edge.gateway":
        return [_r("azurerm_api_management", n, ("name", tf.prefix(slug(node.name, 30))), loc, rg,
                   ("publisher_name", Raw("var.project")),
                   ("publisher_email", tf.var("publisher_email", description="Contact address of the API publisher")),
                   ("sku_name", "Premium_1" if tf.ha else "Developer_1"), tags,
                   comment=f"{node.name} ({node.technology or 'API gateway'})")]
    if kind == "ai.llm" and "openai" in _tech(node):
        return [_r("azurerm_cognitive_account", n, ("name", tf.prefix(slug(node.name, 30))), loc, rg,
                   ("kind", "OpenAI"), ("sku_name", "S0"), tags, comment=f"{node.name} ({node.technology})")]
    if kind == "ops.monitoring":
        workspace()
        return [_r("azurerm_application_insights", n, ("name", tf.prefix(slug(node.name, 30))), loc, rg,
                   ("workspace_id", Raw("azurerm_log_analytics_workspace.main.id")), ("application_type", "web"), tags,
                   comment=f"{node.name} ({node.technology or 'monitoring'})")]
    return []


# ---- Google Cloud
def _gcp(tf: Tf, node: Node, kind: str, n: str) -> list[TfBlock]:
    labels = _tags(tf, node)
    region = ("region", Raw("var.region"))
    if is_workload(node):
        tf.share("registry", lambda: _r("google_artifact_registry_repository", "main", ("repository_id", Raw("var.project")),
                                        ("location", Raw("var.region")), ("format", "DOCKER")))
        rt = runtime_of(node)
        if rt == "kubernetes":
            tf.share("gke", lambda: _r("google_container_cluster", "main", ("name", tf.prefix("gke")),
                                       ("location", Raw("var.region")), ("enable_autopilot", True),
                                       ("deletion_protection", True),
                                       comment="Kubernetes (Autopilot) cluster for the services; deploy them with the k8s manifests"))
            tf.skipped.append(f"{node.name}: runs on google_container_cluster.main; deploy it with the k8s target")
            return []
        if rt == "function":
            return [_r("google_cloudfunctions2_function", n, ("name", tf.prefix(slug(node.name, 40))),
                       ("location", Raw("var.region")),
                       ("build_config", blk(("runtime", "python312"), ("entry_point", "main"))),
                       ("service_config", blk(("max_instance_count", 10), ("ingress_settings", "ALLOW_INTERNAL_ONLY"))),
                       labels, comment=f"{node.name} ({node.technology or 'function'}): add the source bucket")]
        if rt == "vm":
            return [_r("google_compute_instance", n, ("name", tf.prefix(slug(node.name, 40))), ("machine_type", "e2-medium"),
                       ("zone", Raw('"${var.region}-b"')),
                       ("boot_disk", blk(("initialize_params", blk(("image", "debian-cloud/debian-12"))))),
                       ("network_interface", blk(("network", "default"))),
                       ("shielded_instance_config", blk(("enable_secure_boot", True))), labels,
                       comment=f"{node.name} ({node.technology or 'VM'})")]
        return [_r("google_cloud_run_v2_service", n, ("name", tf.prefix(slug(node.name, 40))),
                   ("location", Raw("var.region")), ("ingress", "INGRESS_TRAFFIC_INTERNAL_ONLY"),
                   ("deletion_protection", True),
                   ("template", blk(("scaling", blk(("min_instance_count", 2 if tf.ha else 0), ("max_instance_count", 10))),
                                    ("containers", blk(("image", _image(tf, node)),
                                                       ("ports", blk(("container_port", 8080))))))),
                   labels, comment=f"{node.name} ({node.technology or 'service'})")]
    if kind == "db.relational":
        mysql = _engine(node) == "mysql"
        return [_r("google_sql_database_instance", n, ("name", tf.prefix(slug(node.name, 30))),
                   ("database_version", "MYSQL_8_0" if mysql else "POSTGRES_16"), region, ("deletion_protection", True),
                   ("settings", blk(("tier", "db-custom-2-7680"), ("availability_type", "REGIONAL" if tf.ha else "ZONAL"),
                                    ("user_labels", Raw("local.common_labels")),
                                    ("backup_configuration", blk(("enabled", True),
                                                                 ("binary_log_enabled" if mysql else
                                                                  "point_in_time_recovery_enabled", True))),
                                    ("ip_configuration", blk(("ipv4_enabled", False),
                                                             ("private_network", tf.var("network_id", description="VPC network for private IPs")))))),
                   comment=f"{node.name} ({node.technology or 'relational database'})")]
    if kind == "db.document":
        return [_r("google_firestore_database", n, ("name", slug(node.name, 40)), ("location_id", Raw("var.region")),
                   ("type", "FIRESTORE_NATIVE"), ("point_in_time_recovery_enablement", "POINT_IN_TIME_RECOVERY_ENABLED"),
                   comment=f"{node.name} ({node.technology or 'document database'})")]
    if kind == "db.warehouse":
        return [_r("google_bigquery_dataset", n, ("dataset_id", _ident(node.name)), ("location", Raw("var.region")), labels,
                   comment=f"{node.name} ({node.technology or 'warehouse'})")]
    if kind == "cache.kv":
        return [_r("google_redis_instance", n, ("name", tf.prefix(slug(node.name, 30))),
                   ("tier", "STANDARD_HA" if tf.ha else "BASIC"), ("memory_size_gb", 1), region,
                   ("transit_encryption_mode", "SERVER_AUTHENTICATION"), labels,
                   comment=f"{node.name} ({node.technology or 'cache'})")]
    if kind in ("msg.queue", "msg.pubsub", "msg.stream"):
        return [_r("google_pubsub_topic", n, ("name", tf.prefix(slug(node.name, 40))),
                   ("message_retention_duration", "604800s"), labels,
                   comment=f"{node.name} ({node.technology or 'messaging'})"
                           + (": Pub/Sub stands in for the stream; use Managed Service for Apache Kafka if you need Kafka"
                              if kind == "msg.stream" else "")),
                _r("google_pubsub_subscription", n, ("name", tf.prefix(slug(node.name, 40) + "-sub")),
                   ("topic", Raw(f"google_pubsub_topic.{n}.id")), ("ack_deadline_seconds", 30), labels)]
    if kind == "storage.object":
        return [_r("google_storage_bucket", n, ("name", tf.prefix(slug(node.name, 30))), ("location", Raw("var.region")),
                   ("uniform_bucket_level_access", True), ("public_access_prevention", "enforced"),
                   ("versioning", blk(("enabled", True))), labels,
                   comment=f"{node.name} ({node.technology or 'object storage'})")]
    if kind == "storage.file":
        return [_r("google_filestore_instance", n, ("name", tf.prefix(slug(node.name, 30))),
                   ("location", Raw('"${var.region}-b"')), ("tier", "BASIC_HDD"),
                   ("file_shares", blk(("name", "share"), ("capacity_gb", 1024))),
                   ("networks", blk(("network", "default"), ("modes", ["MODE_IPV4"]))), labels,
                   comment=f"{node.name} ({node.technology or 'file storage'})")]
    if kind == "security.identity":
        return [_r("google_identity_platform_config", n, ("project", Raw("var.gcp_project_id")),
                   comment=f"{node.name} ({node.technology or 'identity'})")]
    if kind == "security.secrets":
        return [_r("google_secret_manager_secret", n, ("secret_id", slug(node.name, 40)),
                   ("replication", blk(("auto", blk()))), labels,
                   comment=f"{node.name} ({node.technology or 'secrets'})")]
    if kind == "security.waf":
        return [_r("google_compute_security_policy", n, ("name", tf.prefix(slug(node.name, 30))),
                   comment=f"{node.name} ({node.technology or 'WAF'}): add the preconfigured WAF rules you need")]
    if kind == "edge.dns":
        tf.var("domain_name", description="The public domain")
        return [_r("google_dns_managed_zone", n, ("name", slug(node.name, 40)),
                   ("dns_name", Raw('"${var.domain_name}."')), labels, comment=f"{node.name} ({node.technology or 'DNS'})")]
    if kind == "ai.llm" and re.search(r"vertex|gemini", _tech(node)):
        return [_r("google_project_service", n, ("service", "aiplatform.googleapis.com"), ("disable_on_destroy", False),
                   comment=f"{node.name} ({node.technology}): enable Vertex AI")]
    if kind == "ops.workflow":
        return [_r("google_workflows_workflow", n, ("name", tf.prefix(slug(node.name, 40))), region,
                   ("source_contents", "main:\n  steps:\n    - done:\n        return: ok\n"), labels,
                   comment=f"{node.name} ({node.technology or 'workflow'}): replace the placeholder steps")]
    return []


CLOUD_BUILDERS: dict[str, Callable[[Tf, Node, str, str], list[TfBlock]]] = {"aws": _aws, "azure": _azure, "gcp": _gcp}


def _secret_for(tf: Tf, node: Node, n: str) -> list[TfBlock]:
    """Credentials of a third-party service (Stripe, a model API…): a sensitive variable kept in the secret store."""
    var = tf.var(f"{n}_api_key", description=f"API key / credentials for {node.name}", sensitive=True)
    comment = f"{node.name} ({node.technology or 'external service'}): third-party; only its credentials live here"
    if tf.cloud == "aws":
        return [_r("aws_secretsmanager_secret", f"{n}_api_key", ("name", tf.prefix(slug(node.name, 40) + "-api-key", "/")),
                   comment=comment),
                _r("aws_secretsmanager_secret_version", f"{n}_api_key",
                   ("secret_id", Raw(f"aws_secretsmanager_secret.{n}_api_key.id")), ("secret_string", var))]
    if tf.cloud == "azure":
        vault = tf.share("kv", lambda: _r("azurerm_key_vault", "main", ("name", Raw('"${var.project}-${var.environment}-kv"')),
                                          ("location", Raw("azurerm_resource_group.main.location")),
                                          ("resource_group_name", Raw("azurerm_resource_group.main.name")),
                                          ("tenant_id", tf.var("tenant_id", description="Microsoft Entra tenant id")),
                                          ("sku_name", "standard"), ("purge_protection_enabled", True)))
        return [_r("azurerm_key_vault_secret", f"{n}_api_key", ("name", slug(node.name, 40) + "-api-key"),
                   ("value", var), ("key_vault_id", Raw(f"azurerm_key_vault.{vault.name}.id")), comment=comment)]
    return [_r("google_secret_manager_secret", f"{n}_api_key", ("secret_id", slug(node.name, 40) + "-api-key"),
               ("replication", blk(("auto", blk()))), comment=comment),
            _r("google_secret_manager_secret_version", f"{n}_api_key",
               ("secret", Raw(f"google_secret_manager_secret.{n}_api_key.id")), ("secret_data", var))]


def _output_attr(block: TfBlock) -> str:
    special = {"aws_ecr_repository": "repository_url", "aws_db_instance": "endpoint", "aws_rds_cluster": "endpoint",
               "aws_elasticache_replication_group": "primary_endpoint_address", "aws_sqs_queue": "url",
               "aws_s3_bucket": "bucket", "aws_apigatewayv2_api": "api_endpoint", "aws_lb": "dns_name",
               "aws_opensearch_domain": "endpoint", "aws_cloudfront_distribution": "domain_name",
               "azurerm_postgresql_flexible_server": "fqdn", "azurerm_mysql_flexible_server": "fqdn",
               "azurerm_redis_cache": "hostname", "azurerm_container_app": "latest_revision_fqdn",
               "azurerm_api_management": "gateway_url", "azurerm_cosmosdb_account": "endpoint",
               "google_cloud_run_v2_service": "uri", "google_sql_database_instance": "connection_name",
               "google_redis_instance": "host", "google_storage_bucket": "url"}
    return special.get(block.type, "id")


def terraform(d: Design, cloud: str = "") -> tuple[list[File], list[str], str]:
    cloud, notes = choose_cloud(d, cloud)
    tf = Tf(cloud=cloud, project=slug(d.name, 24), ha=_ha(d), eu=_eu(d))
    tf.var("project", description="Short name used in resource names", default=slug(d.name, 24))
    tf.var("environment", description="Environment name (dev, staging, prod)", default="prod")
    if cloud == "gcp":
        tf.var("gcp_project_id", description="Google Cloud project id")
    tf.var("region" if cloud != "azure" else "location",
           description="Region to deploy to" + (" (the design keeps data in the EU)" if tf.eu else ""),
           default=REGIONS[cloud][1 if tf.eu else 0])
    builder = CLOUD_BUILDERS[cloud]
    per_node: list[tuple[Node, list[TfBlock]]] = []
    skipped_clients: list[str] = []
    for node in d.top:
        if node.type == "client":
            skipped_clients.append(node.name)
            continue
        n = _ident(node.name, tf.names)
        kind = kind_of(node)
        if node.type in EXTERNAL_TYPES:
            blocks = builder(tf, node, kind, n) if node.type == "llm" else []
            if not blocks and node.type == "llm" and placement(node)[0] == cloud:
                tf.skipped.append(f"{node.name} ({node.technology or 'model API'}): managed model API, reached with "
                                  "IAM permissions; no resource")
                continue
            per_node.append((node, blocks or _secret_for(tf, node, n)))
            continue
        blocks = builder(tf, node, kind, n)
        if not blocks:
            if not any(s.startswith(f"{node.name}:") for s in tf.skipped):
                tf.skipped.append(f"{node.name} ({node.technology or node.type}): no {CLOUD_LABELS[cloud]} resource in "
                                  "the skeleton; add it by hand")
            continue
        per_node.append((node, blocks))
    files: list[File] = []
    provider, source, version = PROVIDERS[cloud]
    files.append(File("terraform/versions.tf", "\n".join([
        "terraform {", '  required_version = ">= 1.6"', "  required_providers {",
        f'    {provider} = {{', f'      source  = "{source}"', f'      version = "{version}"', "    }", "  }", "}", ""])))
    prov = {"aws": ['provider "aws" {', "  region = var.region", "  default_tags {", "    tags = local.common_tags", "  }", "}"],
            "azure": ['provider "azurerm" {', "  features {}", "}"],
            "gcp": ['provider "google" {', "  project = var.gcp_project_id", "  region  = var.region", "}"]}[cloud]
    tag_local = "common_labels" if cloud == "gcp" else "common_tags"
    locals_ = ["locals {", f"  {tag_local} = {{", "    project     = var.project", "    environment = var.environment",
               '    managed_by  = "terraform"', "  }", "}"]
    files.append(File("terraform/providers.tf", "\n".join(prov + [""] + locals_) + "\n"))
    main: list[str] = [f"# {d.name}: infrastructure on {CLOUD_LABELS[cloud]}, generated by ArchFlow from branch "
                       f"`{d.branch}`.", "# One resource (or a few) per component; review each one before applying.", ""]
    if cloud == "azure":
        main += [_r("azurerm_resource_group", "main", ("name", tf.prefix("rg")), ("location", Raw("var.location")),
                    ("tags", Raw("local.common_tags"))).render(), ""]
    for b in tf.shared.values():
        main += [b.render(), ""]
    for node, blocks in per_node:
        for b in blocks:
            main += [b.render(), ""]
        primary = blocks[-1] if blocks[0].type.endswith("ecr_repository") and len(blocks) > 1 else blocks[0]
        tf.outputs.append((f"{_ident(node.name)}_{_output_attr(primary)}"[:60], f"{primary.address}.{_output_attr(primary)}",
                           f"{node.name}: {_output_attr(primary).replace('_', ' ')}"))
    for key in ("eks", "aks", "gke"):
        if key in tf.shared:
            b = tf.shared[key]
            tf.outputs.append(("kubernetes_cluster_name", f"{b.address}.name", "Cluster the k8s manifests deploy to"))
    if skipped_clients or tf.skipped:
        main.append("# Not provisioned here:")
        main += [f"# - {c}: client app, released by its own pipeline" for c in skipped_clients]
        main += [f"# - {s}" for s in tf.skipped]
        main.append("")
    files.append(File("terraform/main.tf", "\n".join(main).rstrip() + "\n"))
    var_lines: list[str] = []
    for name, v in tf.variables.items():
        var_lines.append(f'variable "{name}" {{')
        var_lines.append(f"  type        = {v['type']}")
        if v["description"]:
            var_lines.append(f"  description = {json.dumps(v['description'])}")
        if v["default"] is not None:
            var_lines.append(f"  default     = {_hcl_value(v['default'], 1)}")
        if v["sensitive"]:
            var_lines.append("  sensitive   = true")
        var_lines += ["}", ""]
    files.append(File("terraform/variables.tf", "\n".join(var_lines)))
    out_lines: list[str] = []
    seen: set[str] = set()
    for name, expr, desc in tf.outputs:
        name = _ident(name, seen)
        out_lines += [f'output "{name}" {{', f"  description = {json.dumps(desc)}", f"  value       = {expr}", "}", ""]
    files.append(File("terraform/outputs.tf", "\n".join(out_lines) or "# No outputs.\n"))
    tfvars = [f"{k} = {_hcl_value(v['default'] if v['default'] is not None else ('CHANGE_ME' if v['type'] == 'string' else []), 0)}"
              for k, v in tf.variables.items() if not v["sensitive"]]
    files.append(File("terraform/terraform.tfvars.example", "\n".join(tfvars) + "\n"))
    files.append(File("terraform/README.md", "\n".join([
        f"# {d.name}: Terraform ({CLOUD_LABELS[cloud]})", "",
        f"Generated by ArchFlow from branch `{d.branch}`. It is a starting point: check every resource, then plan.", "",
        "```sh", "cp terraform.tfvars.example terraform.tfvars   # fill in the values", "terraform init",
        "terraform plan", "```", "",
        "Sensitive values (" + (", ".join(k for k, v in tf.variables.items() if v["sensitive"]) or "none") + ") are "
        "not in the example file: pass them as `TF_VAR_<name>` environment variables or from your secret store.", "",
        "High availability: " + ("on (an availability requirement asks for it)" if tf.ha else "off") + ". Region: "
        + ("an EU region, because a requirement or fact keeps data in the EU." if tf.eu else "the provider default."),
        ""])))
    if tf.skipped or skipped_clients:
        notes.append(f"{len(tf.skipped) + len(skipped_clients)} component(s) are not provisioned by the Terraform; "
                     "main.tf lists them at the end.")
    return files, notes, cloud


# --------------------------------------------------------------------------- local / open-source: docker-compose
@dataclass
class Image:
    image: str
    port: int
    env: dict[str, str] = field(default_factory=dict)
    health: list[str] | None = None
    data: str = ""  # volume mount path
    url: str = ""  # connection string template: {host} {port}
    command: list[str] | None = None


def _images() -> dict[str, Image]:
    pg = Image("postgres:16-alpine", 5432, {"POSTGRES_USER": "app", "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:-app}",
                                             "POSTGRES_DB": "app"},
               ["CMD-SHELL", "pg_isready -U app"], "/var/lib/postgresql/data",
               "postgresql://app:${POSTGRES_PASSWORD:-app}@{host}:{port}/app")
    return {
        "oss-postgresql": pg,
        "oss-pgvector": Image(**{**pg.__dict__, "image": "pgvector/pgvector:pg16"}),
        "postgis": Image(**{**pg.__dict__, "image": "postgis/postgis:16-3.4-alpine"}),
        "oss-mysql": Image("mysql:8.4", 3306, {"MYSQL_ROOT_PASSWORD": "${MYSQL_PASSWORD:-app}", "MYSQL_DATABASE": "app"},
                           ["CMD", "mysqladmin", "ping", "-h", "localhost"], "/var/lib/mysql",
                           "mysql://root:${MYSQL_PASSWORD:-app}@{host}:{port}/app"),
        "oss-mongodb": Image("mongo:7", 27017, {}, ["CMD", "mongosh", "--quiet", "--eval", "db.adminCommand('ping')"],
                             "/data/db", "mongodb://{host}:{port}/app"),
        "oss-clickhouse": Image("clickhouse/clickhouse-server:24.8", 8123, {},
                                ["CMD", "wget", "-qO-", "http://localhost:8123/ping"], "/var/lib/clickhouse",
                                "http://{host}:{port}"),
        "oss-elasticsearch": Image("opensearchproject/opensearch:2.15.0", 9200,
                                   {"discovery.type": "single-node", "DISABLE_SECURITY_PLUGIN": "true"},
                                   ["CMD-SHELL", "curl -fs http://localhost:9200 || exit 1"],
                                   "/usr/share/opensearch/data", "http://{host}:{port}"),
        "oss-qdrant": Image("qdrant/qdrant:v1.11.0", 6333, {}, None, "/qdrant/storage", "http://{host}:{port}"),
        "oss-redis": Image("redis:7.4-alpine", 6379, {}, ["CMD", "redis-cli", "ping"], "/data", "redis://{host}:{port}"),
        "oss-rabbitmq": Image("rabbitmq:3.13-management", 5672, {},
                              ["CMD", "rabbitmq-diagnostics", "-q", "ping"], "/var/lib/rabbitmq",
                              "amqp://guest:guest@{host}:{port}"),
        "oss-kafka": Image("apache/kafka:3.8.0", 9092, {
            "KAFKA_NODE_ID": "1", "KAFKA_PROCESS_ROLES": "broker,controller",
            "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
            "KAFKA_ADVERTISED_LISTENERS": "PLAINTEXT://{host}:9092",
            "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER", "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@localhost:9093",
            "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1"}, None, "/var/lib/kafka/data", "{host}:{port}"),
        "oss-nats": Image("nats:2.10-alpine", 4222, {}, None, "", "nats://{host}:{port}", ["-js"]),
        "oss-minio": Image("minio/minio:RELEASE.2024-10-13T13-34-11Z", 9000,
                           {"MINIO_ROOT_USER": "minio", "MINIO_ROOT_PASSWORD": "${MINIO_PASSWORD:-minio-secret}"},
                           ["CMD", "mc", "ready", "local"], "/data", "http://{host}:{port}", ["server", "/data"]),
        "oss-keycloak": Image("quay.io/keycloak/keycloak:25.0", 8080,
                              {"KEYCLOAK_ADMIN": "admin", "KEYCLOAK_ADMIN_PASSWORD": "${KEYCLOAK_PASSWORD:-admin}"},
                              None, "", "http://{host}:{port}", ["start-dev"]),
        "oss-vault": Image("hashicorp/vault:1.17", 8200, {"VAULT_DEV_ROOT_TOKEN_ID": "${VAULT_TOKEN:-dev-root}"},
                           None, "", "http://{host}:{port}"),
        "oss-kong": Image("kong:3.7", 8000, {"KONG_DATABASE": "off",
                                             "KONG_DECLARATIVE_CONFIG": "/kong/kong.yml"},
                          ["CMD", "kong", "health"], "", "http://{host}:{port}"),
        "oss-nginx": Image("nginx:1.27-alpine", 80, {}, None, "", "http://{host}:{port}"),
        "oss-traefik": Image("traefik:v3.1", 80, {}, None, "", "http://{host}:{port}", ["--providers.docker=true"]),
        "oss-litellm": Image("ghcr.io/berriai/litellm:main-stable", 4000, {}, None, "", "http://{host}:{port}"),
        "oss-temporal": Image("temporalio/auto-setup:1.24", 7233, {}, None, "", "{host}:{port}"),
        "oss-prometheus-grafana": Image("prom/prometheus:v2.54.0", 9090, {}, None, "/prometheus", "http://{host}:{port}"),
        "oss-mlflow": Image("ghcr.io/mlflow/mlflow:v2.16.0", 5000, {}, None, "", "http://{host}:{port}",
                            ["mlflow", "server", "--host", "0.0.0.0"]),
    }


IMAGES = _images()
KIND_IMAGE = {"db.relational": "oss-postgresql", "db.document": "oss-mongodb", "db.warehouse": "oss-clickhouse",
              "db.search": "oss-elasticsearch", "db.vector": "oss-qdrant", "cache.kv": "oss-redis",
              "msg.queue": "oss-rabbitmq", "msg.stream": "oss-kafka", "msg.pubsub": "oss-nats",
              "storage.object": "oss-minio", "storage.file": "oss-minio", "security.identity": "oss-keycloak",
              "security.secrets": "oss-vault", "edge.gateway": "oss-kong", "edge.lb": "oss-nginx",
              "ai.mcp": "", "ops.monitoring": "oss-prometheus-grafana", "ops.workflow": "oss-temporal"}


def local_image(node: Node) -> tuple[str, Image] | None:
    """The open-source image that runs a (possibly managed) component locally, from its catalog equivalent."""
    entry = catalog.match(node.model_dump(include={"catalog_id", "technology", "name"}))
    kind = (entry or {}).get("kind") or DEFAULT_KIND.get(node.type, "")
    oss = entry if entry and entry["vendor"] == "oss" else (catalog.equivalent(entry, "oss") if entry else None)
    key = (oss or {}).get("id") or KIND_IMAGE.get(kind, "")
    if kind == "db.relational" and "postgis" in _tech(node):
        key = "postgis"
    if kind == "db.relational" and "mysql" in _tech(node):
        key = "oss-mysql"
    return (key, IMAGES[key]) if key in IMAGES else None


SERVICE_PORT = 8080


def _url(host: str, img: Image) -> str:
    return img.url.replace("{host}", host).replace("{port}", str(img.port))


def compose(d: Design) -> tuple[list[File], list[str]]:
    used: set[str] = set()
    names: dict[str, str] = {}
    services: dict[str, Any] = {}
    volumes: dict[str, Any] = {}
    secrets_env: list[str] = []
    skipped: list[str] = []
    images: dict[str, Image] = {}
    taken: set[int] = set()

    def publish(port: int, inside: int) -> str:
        """host:container port mapping on the first free host port from ``port``."""
        while port in taken:
            port += 1
        taken.add(port)
        return f"{port}:{inside}"

    nodes = d.top
    for node in nodes:
        names[node.id] = _dns(node.name, used, 40)
    for node in nodes:
        host = names[node.id]
        if node.type == "client":
            if re.search(r"mobile|native|\bios\b|android|flutter", _tech(node)) and "web" not in _tech(node):
                skipped.append(f"{node.name}: mobile app, run it from its own tooling")
                continue
            services[host] = {"build": f"./apps/{host}", "ports": [publish(3000, 3000)]}
            continue
        if node.type in EXTERNAL_TYPES:
            img = local_image(node)
            if node.type == "llm" and img and img[0] == "oss-litellm":
                images[node.id] = img[1]
                services[host] = {"image": img[1].image, "ports": [publish(img[1].port, img[1].port)]}
            else:
                secrets_env.append(f"{_env_name(node.name)}_API_KEY")
            continue
        if is_workload(node):
            services[host] = {"build": f"./services/{host}", "ports": [publish(8080, SERVICE_PORT)],
                              "environment": {"PORT": str(SERVICE_PORT), "SERVICE_NAME": host},
                              "healthcheck": {"test": ["CMD", "wget", "-qO-", f"http://localhost:{SERVICE_PORT}/healthz"],
                                              "interval": "10s", "timeout": "3s", "retries": 5}}
            continue
        found = local_image(node)
        if not found:
            skipped.append(f"{node.name} ({node.technology or node.type}): no local equivalent; mock it or use the "
                           "cloud service from a dev account")
            continue
        key, img = found
        images[node.id] = img
        svc: dict[str, Any] = {"image": img.image, "ports": [publish(img.port, img.port)]}
        if img.env:
            svc["environment"] = {k: v.replace("{host}", host) for k, v in img.env.items()}
        if img.command:
            svc["command"] = img.command
        if img.health:
            svc["healthcheck"] = {"test": img.health, "interval": "10s", "timeout": "5s", "retries": 10}
        if img.data:
            volumes[f"{host}-data"] = None
            svc["volumes"] = [f"{host}-data:{img.data}"]
        if key == "oss-kong":
            svc["volumes"] = ["./gateway/kong.yml:/kong/kong.yml:ro"]
        services[host] = svc
    # connections become environment variables and start order
    for node in nodes:
        host = names[node.id]
        svc = services.get(host)
        if not svc or "build" not in svc or node.type == "client":
            continue
        env = svc.setdefault("environment", {})
        deps: dict[str, Any] = {}
        for target, protocol in _outgoing(d, node):
            th = names.get(target.id)
            if target.type in EXTERNAL_TYPES and th not in services:
                key = f"{_env_name(target.name)}_API_KEY"
                env[key] = f"${{{key}:-}}"
                continue
            if th not in services:
                continue
            img = images.get(target.id)
            env[f"{_env_name(target.name)}_URL"] = _url(th, img) if img else f"http://{th}:{SERVICE_PORT}"
            deps[th] = {"condition": "service_healthy" if "healthcheck" in services[th] else "service_started"}
        if deps:
            svc["depends_on"] = deps
    doc: dict[str, Any] = {"name": slug(d.name, 40), "services": services}
    if volumes:
        doc["volumes"] = volumes
    head = (f"# {d.name}: local stack generated by ArchFlow from branch `{d.branch}`.\n"
            "# Managed cloud services run as their open-source equivalents. Start it with: docker compose up --build\n")
    if skipped:
        head += "# Not included:\n" + "".join(f"# - {s}\n" for s in skipped)
    files = [File("docker-compose.yml", head + _dump(doc, sort_keys=False, default_flow_style=False, width=120))]
    env_lines = ["# Copy to .env and fill in. Local development only."]
    env_lines += [f"{k}=" for k in secrets_env]
    env_vars = sorted({m for s in services.values() for v in (s.get("environment") or {}).values()
                       for m in re.findall(r"\$\{([A-Z_]+):-([^}]*)\}", str(v))})
    env_lines += [f"{k}={v}" for k, v in env_vars if k not in secrets_env]
    files.append(File(".env.example", "\n".join(dict.fromkeys(env_lines)) + "\n"))
    if any((s.get("image") or "").startswith("kong") for s in services.values()):
        routes = [n for n in nodes if is_workload(n) and n.type != "gateway"]
        kong = {"_format_version": "3.0", "services": [
            {"name": names[n.id], "url": f"http://{names[n.id]}:{SERVICE_PORT}",
             "routes": [{"name": names[n.id], "paths": [f"/{names[n.id]}"]}]} for n in routes]}
        files.append(File("gateway/kong.yml", _dump(kong, sort_keys=False)))
    notes = [f"Not in the local stack: {'; '.join(skipped)}."] if skipped else []
    return files, notes


# --------------------------------------------------------------------------- Kubernetes manifests
def kubernetes(d: Design) -> tuple[list[File], list[str]]:
    ns = _dns(d.name, limit=40)
    used: set[str] = set()
    names = {n.id: _dns(n.name, used, 40) for n in d.top}
    ha, scales = _ha(d), _scales(d)
    replicas = 3 if ha else 2
    files: list[File] = [File("k8s/namespace.yaml", _dump(
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {"app.kubernetes.io/part-of": ns}}},
        sort_keys=False))]
    config: dict[str, str] = {}
    secret_keys: list[str] = []
    needs_password: set[str] = set()
    workloads = [n for n in d.top if is_workload(n)]
    for node in d.top:
        if node.type in EXTERNAL_TYPES:
            secret_keys.append(f"{_env_name(node.name)}_API_KEY")
        elif node.type in DATA_TYPES or node.type == "security" or (node.type == "gateway" and not is_workload(node)):
            img = local_image(node)
            url = _url(names[node.id], img[1]) if img else f"https://{names[node.id]}"
            if re.search(r":\$\{[A-Z_]+:-[^}]*\}@", url):
                url = re.sub(r":\$\{[A-Z_]+:-[^}]*\}@", "@", url)  # the password comes from the Secret
                needs_password.add(node.id)
            config[f"{_env_name(node.name)}_URL"] = url
    if needs_password:
        secret_keys.append("DB_PASSWORD")
    for node in workloads:
        config[f"{_env_name(node.name)}_URL"] = f"http://{names[node.id]}.{ns}.svc.cluster.local"
    resources = ["namespace.yaml"]
    if config:
        files.append(File("k8s/config.yaml", _dump({
            "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "endpoints", "namespace": ns},
            "data": dict(sorted(config.items()))}, sort_keys=False)))
        resources.append("config.yaml")
    if secret_keys:
        files.append(File("k8s/secrets.example.yaml", "# Example only: create the real Secret from your secret store.\n"
                          + _dump({"apiVersion": "v1", "kind": "Secret",
                                            "metadata": {"name": "credentials", "namespace": ns}, "type": "Opaque",
                                            "stringData": {k: "CHANGE_ME" for k in sorted(set(secret_keys))}},
                                           sort_keys=False)))
    for node in workloads:
        name = names[node.id]
        labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": ns}
        deps = [t for t, _ in _outgoing(d, node)]
        env: list[dict[str, Any]] = [{"name": "PORT", "value": str(SERVICE_PORT)}]
        for t in deps:
            if t.type in EXTERNAL_TYPES:
                key = f"{_env_name(t.name)}_API_KEY"
                env.append({"name": key, "valueFrom": {"secretKeyRef": {"name": "credentials", "key": key}}})
        if any(t.id in needs_password for t in deps):
            env.insert(0, {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "credentials", "key": "DB_PASSWORD"}}})
        env_from = [{"configMapRef": {"name": "endpoints"}}] if config else []
        container: dict[str, Any] = {
            "name": name, "image": f"registry.example.com/{ns}/{name}:latest",
            "ports": [{"name": "http", "containerPort": SERVICE_PORT}],
            "env": env, **({"envFrom": env_from} if env_from else {}),
            "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"}, "periodSeconds": 5},
            "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "initialDelaySeconds": 10,
                              "periodSeconds": 10},
            "resources": {"requests": {"cpu": "250m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}},
            "securityContext": {"runAsNonRoot": True, "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        }
        docs: list[dict[str, Any]] = [
            {"apiVersion": "apps/v1", "kind": "Deployment",
             "metadata": {"name": name, "namespace": ns, "labels": labels,
                          "annotations": {"archflow/component": node.name,
                                          **({"archflow/technology": node.technology} if node.technology else {})}},
             "spec": {"replicas": replicas, "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                      "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
                      "template": {"metadata": {"labels": labels}, "spec": {"containers": [container]}}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns, "labels": labels},
             "spec": {"selector": {"app.kubernetes.io/name": name},
                      "ports": [{"name": "http", "port": 80, "targetPort": "http"}]}},
        ]
        if scales:
            docs.append({"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
                         "metadata": {"name": name, "namespace": ns},
                         "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": name},
                                  "minReplicas": replicas, "maxReplicas": max(10, replicas * 3),
                                  "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {
                                      "type": "Utilization", "averageUtilization": 70}}}]}})
        if ha:
            docs.append({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": name, "namespace": ns},
                         "spec": {"minAvailable": replicas - 1,
                                  "selector": {"matchLabels": {"app.kubernetes.io/name": name}}}})
        files.append(File(f"k8s/{name}.yaml", f"# {node.name}" + (f" ({node.technology})" if node.technology else "")
                          + "\n" + _dump_all(docs, sort_keys=False)))
        resources.append(f"{name}.yaml")
    files.append(File("k8s/kustomization.yaml", _dump(
        {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "namespace": ns,
         "resources": resources}, sort_keys=False)))
    notes = []
    if not workloads:
        notes.append("The design has no services to run on Kubernetes yet.")
    data = [n.name for n in d.top if n.type in DATA_TYPES]
    if data:
        notes.append(f"Data stores ({', '.join(data)}) are not deployed by these manifests: use the managed services "
                     "(Terraform target) or an operator, and set their addresses in k8s/config.yaml.")
    return files, notes


# =========================================================================== tests
@dataclass
class Scenario:
    name: str
    given: list[str]
    when: list[str]
    then: list[str]


@dataclass
class Feature:
    requirement: Requirement
    scenarios: list[Scenario]
    components: list[str]


_GWT = re.compile(r"^\s*given\s+(?P<g>.+?)[,;]?\s+when\s+(?P<w>.+?)[,;]?\s+then\s+(?P<t>.+?)\s*\.?$", re.I | re.S)


def _sentence(text: str) -> str:
    text = exports._one_line(text).rstrip(".")
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        text = text[0].lower() + text[1:]
    return text


def _actor(r: Requirement) -> str:
    human = [a for a in r.actors if a.strip().lower() not in ("system", "the system")]
    return human[0].strip() if human else ""


def offline_scenarios(r: Requirement, system: str) -> list[Scenario]:
    """Given/When/Then from each acceptance criterion (one already written that way is split as it is)."""
    actor = _actor(r)
    out: list[Scenario] = []
    criteria = r.acceptance_criteria or [r.description or r.title]
    for c in criteria:
        m = _GWT.match(exports._one_line(c))
        name = exports._one_line(c)[:90].rstrip(".")
        if m:
            out.append(Scenario(name, [_sentence(m["g"])], [_sentence(m["w"])], [_sentence(m["t"])]))
            continue
        given = [f"{system} is running"]
        if actor:
            given.append(f"a {actor.lower()} is signed in")
        who = f"the {actor.lower()}" if actor else "a client"
        when = [f'{who} uses "{exports._one_line(r.title)}"']
        out.append(Scenario(name, given, when, [_sentence(c)]))
    return out


def features(d: Design) -> list[Feature]:
    impl = exports._implemented_by(d.state)
    reqs = d.functional + d.nfrs
    return [Feature(r, offline_scenarios(r, d.name), impl.get(r.id) or []) for r in reqs]


def _proto(protocol: str, target: Node) -> str:
    p = (protocol or "").lower()
    if re.search(r"grpc", p):
        return "grpc"
    if re.search(r"\bwss?\b|websocket|socket\.io|mqtt", p):
        return "websocket"
    if target.type == "queue" or re.search(r"kafka|amqp|sqs|sns|pub/?sub|event|nats|mq\b", p):
        return "messaging"
    if target.type == "cache" or re.search(r"resp|redis|memcache", p):
        return "cache"
    if target.type in ("database", "storage") or re.search(r"sql|jdbc|postgres|mongo|tcp|s3", p):
        return "data"
    if target.type in EXTERNAL_TYPES:
        return "external"
    return "http"


CONTRACT_CHECKS = {
    "http": ["Request and response bodies match the agreed schema (OpenAPI); unknown fields are ignored",
             "Status codes are agreed: 2xx success, 400/422 validation, 401/403 auth, 404, 409 conflict, 5xx",
             "Calls without a valid token are rejected (401) and calls without the right scope are refused (403)",
             "Timeouts and retries are set; retried writes carry an idempotency key",
             "A consumer-driven contract test (e.g. Pact) runs in both pipelines"],
    "grpc": ["The .proto stays backward compatible (buf breaking check in CI)",
             "Every call sets a deadline; the server honours cancellation",
             "Errors use the agreed status codes (INVALID_ARGUMENT, NOT_FOUND, UNAVAILABLE)",
             "Retries happen only on UNAVAILABLE and only for idempotent methods",
             "mTLS or a token authenticates the caller"],
    "websocket": ["The handshake requires a valid token; expired tokens close the socket",
                  "Every message type matches its schema (type, version, payload)",
                  "The client reconnects with backoff and resumes without losing or duplicating messages",
                  "Heartbeats (ping/pong) detect dead connections within the agreed time",
                  "Message order per conversation / entity is kept"],
    "messaging": ["Message schemas are versioned and checked for compatibility (schema registry)",
                  "Consumers are idempotent: a redelivered message has no extra effect",
                  "Poison messages go to a dead-letter queue / topic with an alert",
                  "The partition / ordering key keeps related messages in order",
                  "Producer and consumer agree on retention and replay"],
    "cache": ["Keys are namespaced and have a TTL",
              "A cache miss or a cache outage falls back to the source of truth",
              "Writes invalidate or update the cached value (no stale reads beyond the TTL)",
              "Large values and hot keys are bounded"],
    "data": ["Migrations apply cleanly to an empty database and to a copy of production",
             "Queries the caller runs use indexes (check the plans for the main queries)",
             "The connection pool size fits the database's connection limit across all replicas",
             "Connections use TLS and least-privilege credentials",
             "Transactions use the agreed isolation level; concurrent writes are tested"],
    "external": ["Tests run against the provider's sandbox with test credentials",
                 "Timeouts, retries with backoff and a circuit breaker protect the caller",
                 "Webhooks / callbacks verify the provider's signature",
                 "Provider errors and rate limits (429) map to clear errors for the user",
                 "Credentials come from the secret store, never from code"],
}
PROTO_LABELS = {"http": "HTTP API", "grpc": "gRPC", "websocket": "WebSocket", "messaging": "messaging",
                "cache": "cache", "data": "data store", "external": "third-party API"}


@dataclass
class Contract:
    source: str
    target: str
    protocol: str
    family: str
    checks: list[str]


def contracts(d: Design) -> list[Contract]:
    top = {n.id for n in d.top}
    out: list[Contract] = []
    for e in exports._edges(d.state):
        if e.source not in top or e.target not in top:
            continue
        s, t = d.state.nodes[e.source], d.state.nodes[e.target]
        fam = _proto(e.protocol, t)
        checks = list(CONTRACT_CHECKS[fam])
        if fam == "external" and re.search(r"stripe|pay|adyen|paypal", _tech(t)):
            checks.append("Payments are idempotent and reconciled; card data never touches our services (PCI scope)")
        if e.label:
            checks.insert(0, f"Covers the purpose of the connection: {exports._one_line(e.label)}")
        out.append(Contract(s.name, t.name, e.protocol or PROTO_LABELS[fam], fam, checks))
    return out


def contract_checklist(d: Design, items: list[Contract]) -> str:
    lines = [f"# {d.name}: contract and integration test checklist", "",
             "One section per connection in the design. Tick a box when an automated test covers it.", ""]
    if not items:
        lines.append("_The design has no connections yet._")
    for c in items:
        lines.append(f"## {c.source} → {c.target} ({c.protocol}, {PROTO_LABELS[c.family]})")
        lines.append("")
        lines += [f"- [ ] {x}" for x in c.checks]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _gherkin_text(text: str) -> str:
    return exports._one_line(text).replace('"""', "'''")


def render_gherkin(d: Design, feats: list[Feature]) -> list[File]:
    files: list[File] = []
    for f in feats:
        r = f.requirement
        tags = [f"@{r.id}", f"@{r.priority}", "@nfr" if r.kind == "non_functional" else "@functional"]
        if r.category:
            tags.append("@" + slug(r.category, 30))
        lines = [" ".join(tags), f"Feature: {_gherkin_text(r.title)}"]
        if r.description:
            lines.append(f"  {_gherkin_text(r.description)}")
        if f.components:
            lines.append(f"  Implemented by: {', '.join(f.components)}")
        lines.append("")
        for i, s in enumerate(f.scenarios, 1):
            lines.append(f"  Scenario: {_gherkin_text(s.name)}")
            for word, steps in (("Given", s.given), ("When", s.when), ("Then", s.then)):
                for j, step in enumerate(steps):
                    lines.append(f"    {word if j == 0 else 'And'} {_gherkin_text(step)}")
            lines.append("")
        files.append(File(f"features/{r.id.lower()}-{slug(r.title, 40)}.feature", "\n".join(lines).rstrip() + "\n"))
    return files


def _py_name(text: str, used: set[str]) -> str:
    return _ident(text, used)


def _doc(text: str) -> str:
    """Text safe inside a triple-quoted docstring."""
    return exports._one_line(text).replace("\\", "/").replace('"', "'")


def _py_str(text: str) -> str:
    return json.dumps(exports._one_line(text), ensure_ascii=False)


def render_pytest(d: Design, feats: list[Feature]) -> list[File]:
    files = [File("tests/acceptance/conftest.py", "\n".join([
        f'"""Shared fixtures for the {d.name} acceptance tests (generated by ArchFlow)."""', "",
        "import os", "", "import pytest", "", "",
        "@pytest.fixture(scope=\"session\")", "def base_url() -> str:",
        '    """Where the system under test runs (set BASE_URL)."""',
        '    return os.environ.get("BASE_URL", "http://localhost:8080")', ""]))]
    for f in feats:
        r = f.requirement
        used: set[str] = set()
        lines = [f'"""{r.id}: {_doc(r.title)}', ""]
        if r.description:
            lines.append(_doc(r.description))
            lines.append("")
        if f.components:
            lines.append(_doc(f"Implemented by: {', '.join(f.components)}"))
        lines += ['"""', "", "import pytest", "", f"pytestmark = pytest.mark.requirement({_py_str(r.id)})"]
        for s in f.scenarios:
            fn = "test_" + _py_name(s.name, used)
            lines += ["", "", f"def {fn}(base_url):", f'    """{_doc(s.name)}"""']
            for word, steps in (("Given", s.given), ("When", s.when), ("Then", s.then)):
                for j, step in enumerate(steps):
                    lines.append(f"    # {word if j == 0 else 'And'} {exports._one_line(step)}")
            lines.append(f"    pytest.skip({_py_str('Not implemented yet: ' + s.name)})")
        files.append(File(f"tests/acceptance/test_{_ident(r.id)}_{_ident(r.title)[:40]}.py", "\n".join(lines) + "\n"))
    files.append(File("tests/acceptance/pytest.ini", "[pytest]\nmarkers =\n    requirement(id): the requirement a test covers\n"))
    return files


def _ts_str(text: str) -> str:
    return json.dumps(exports._one_line(text), ensure_ascii=False)


def render_playwright(d: Design, feats: list[Feature]) -> list[File]:
    files = [File("e2e/playwright.config.ts", "\n".join([
        'import { defineConfig } from "@playwright/test";', "",
        f"// {d.name}: end-to-end acceptance tests generated by ArchFlow.",
        "export default defineConfig({", '  testDir: ".",',
        '  use: { baseURL: process.env.BASE_URL ?? "http://localhost:3000", trace: "on-first-retry" },',
        "  retries: process.env.CI ? 2 : 0,", "});", ""]))]
    for f in feats:
        r = f.requirement
        lines = ['import { test, expect } from "@playwright/test";', "",
                 f"// {r.id}: {exports._one_line(r.title)}" + (f" (implemented by {', '.join(f.components)})" if f.components else ""),
                 f"test.describe({_ts_str(f'{r.id} {r.title}')}, () => {{"]
        for s in f.scenarios:
            lines.append(f"  test.fixme({_ts_str(s.name)}, async ({{ page }}) => {{")
            for word, steps in (("Given", s.given), ("When", s.when), ("Then", s.then)):
                for step in steps:
                    lines.append(f"    // {word} {exports._one_line(step)}")
            lines += ['    await page.goto("/");', "    await expect(page).toHaveURL(/.*/);", "  });", ""]
        if lines[-1] == "":
            lines.pop()
        lines += ["});", ""]
        files.append(File(f"e2e/{r.id.lower()}-{slug(r.title, 40)}.spec.ts", "\n".join(lines)))
    return files


TEST_RENDERERS = {"gherkin": render_gherkin, "pytest": render_pytest, "playwright": render_playwright}


def tests_files(d: Design, target: str, feats: list[Feature], items: list[Contract]) -> list[File]:
    files = TEST_RENDERERS[target](d, feats)
    files.append(File("tests/contracts/CHECKLIST.md", contract_checklist(d, items)))
    return files


# =========================================================================== documents
def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"] + [
        "| " + " | ".join(exports._cell(c) for c in r) + " |" for r in rows]


def _by_category(reqs: list[Requirement]) -> dict[str, list[Requirement]]:
    out: dict[str, list[Requirement]] = {}
    for r in reqs:
        out.setdefault((r.category or "general").strip().title(), []).append(r)
    return out


def srs(d: Design) -> str:
    s, top, impl = d.state, d.top, exports._implemented_by(d.state)
    actors = sorted({a for r in s.requirements.values() for a in r.actors}, key=str.lower)
    clients = [n for n in top if n.type == "client"]
    externals = [n for n in top if n.type in EXTERNAL_TYPES]
    accepted = [x for x in d.decisions if x.get("status") == "accepted"]
    constraints = [m for m in d.memory if m.get("category") in ("constraint", "decision")]
    version = f"v{d.version}" if d.version else "unsaved working state"
    out: list[str] = [f"# {d.name}: Software Requirements Specification", "",
                      f"IEEE 830-style SRS generated by ArchFlow from branch `{d.branch}` ({version}).", ""]
    w = out.append
    w("## 1. Introduction\n")
    w("### 1.1 Purpose\n")
    w(f"This document specifies the software requirements of {d.name}. It is written for the team building, "
      "testing and operating the system." + (f"\n\n> {d.description}" if d.description else "") + "\n")
    w("### 1.2 Scope\n")
    w(f"{d.name} covers {len(d.functional)} functional and {len(d.nfrs)} non-functional requirements, implemented by "
      f"{len(top)} components" + (f" and used by {', '.join(actors)}" if actors else "") + ".\n")
    if d.functional:
        out += [f"- {r.title}" for r in d.functional]
        w("")
    w("### 1.3 Definitions, acronyms and abbreviations\n")
    out += ["- **FR**: functional requirement", "- **NFR**: non-functional requirement",
            "- **ADR**: architecture decision record"]
    techs = sorted({n.technology for n in top if n.technology}, key=str.lower)
    if techs:
        w(f"- **Technologies used**: {', '.join(techs)}")
    w("")
    w("### 1.4 References\n")
    w("- The ArchFlow model of this project (requirements, architecture, decisions)")
    out += [f"- {x.get('id', 'ADR')}: {exports._one_line(x.get('title', ''))}" for x in accepted]
    w("")
    w("### 1.5 Overview\n")
    w("Section 2 describes the product as a whole; section 3 lists the specific requirements, interfaces and "
      "constraints. The appendix traces requirements to components.\n")
    w("## 2. Overall description\n")
    w("### 2.1 Product perspective\n")
    w(f"The system is made of {len(top)} components:\n")
    out += _table(["Component", "Type", "Technology", "Responsibilities"],
                  [[n.name, n.type, n.technology, ", ".join(r for r in n.requirement_ids if r in s.requirements)]
                   for n in top])
    w("")
    w("### 2.2 Product functions\n")
    out += [f"- **{r.id}** {r.title}" + (f": {exports._one_line(r.description)}" if r.description else "")
            for r in d.functional] or ["_No functional requirements yet._"]
    w("")
    w("### 2.3 User classes and characteristics\n")
    out += [f"- **{a}**: " + ", ".join(r.id for r in s.requirements.values() if a in r.actors) for a in actors] \
        or ["_No actors recorded on the requirements._"]
    w("")
    w("### 2.4 Operating environment\n")
    platforms = sorted({placement(n)[0] for n in top if n.type != "client"})
    w("Platforms: " + (", ".join({"self": "self-hosted", "saas": "third-party SaaS", "devices": "user devices"}
                                  .get(p, CLOUD_LABELS.get(p, p)) for p in platforms) or "not specified") + ".\n")
    w("### 2.5 Design and implementation constraints\n")
    items = [f"- {x.get('id', '')}: {exports._one_line(x.get('decision') or x.get('title', ''))}" for x in accepted]
    items += [f"- {exports._one_line(m.get('text', ''))}" for m in constraints]
    out += items or ["_No accepted decisions or recorded constraints._"]
    w("")
    w("### 2.6 Assumptions and dependencies\n")
    out += [f"- Depends on {n.name}" + (f" ({n.technology})" if n.technology else "") for n in externals] \
        or ["_No third-party dependencies in the design._"]
    out += [f"- {exports._one_line(m.get('text', ''))}" for m in d.memory if m.get("category") == "assumption"]
    w("")
    w("## 3. Specific requirements\n")
    w("### 3.1 External interfaces\n")
    w("#### 3.1.1 User interfaces\n")
    out += [f"- {n.name}" + (f" ({n.technology})" if n.technology else "") for n in clients] or ["_None in the design._"]
    w("")
    w("#### 3.1.2 Software interfaces\n")
    out += [f"- {n.name}" + (f" ({n.technology})" if n.technology else "") + ": called by "
            + (", ".join(x.name for x, _ in _incoming(d, n)) or "nobody yet") for n in externals] or ["_None._"]
    w("")
    w("#### 3.1.3 Communications interfaces\n")
    conns = contracts(d)
    if conns:
        out += _table(["From", "To", "Protocol"], [[c.source, c.target, c.protocol] for c in conns])
    else:
        w("_No connections yet._")
    w("")
    w("### 3.2 Functional requirements\n")
    for r in d.functional:
        _req_block(out, r, impl)
    if not d.functional:
        w("_None yet._\n")
    w("### 3.3 Non-functional requirements\n")
    for cat, reqs in _by_category(d.nfrs).items():
        w(f"#### {cat}\n")
        for r in reqs:
            _req_block(out, r, impl, level=5)
    if not d.nfrs:
        w("_None yet._\n")
    w("### 3.4 Design constraints\n")
    out += [f"- {r.id} {r.title}" for r in d.nfrs if r.category.lower() in ("compliance", "security", "cost",
                                                                               "constraint", "legal")] \
        or ["_See 2.5._"]
    w("")
    w("## Appendix A. Traceability\n")
    out += _table(["Requirement", "Title", "Implemented by"],
                  [[r.id, r.title, ", ".join(impl.get(r.id) or []) or "not traced"] for r in d.functional + d.nfrs])
    return "\n".join(out).rstrip() + "\n"


def _req_block(out: list[str], r: Requirement, impl: dict[str, list[str]], level: int = 4) -> None:
    out.append(f"{'#' * level} {r.id}: {exports._one_line(r.title)}\n")
    out.append(f"- Priority: {r.priority} · Status: {r.status}" + (f" · Actors: {', '.join(r.actors)}" if r.actors else ""))
    out.append("- Implemented by: " + (", ".join(impl.get(r.id) or []) or "not traced"))
    out.append("")
    if r.description:
        out.append(exports._one_line(r.description) + "\n")
    if r.acceptance_criteria:
        out.append("Acceptance criteria:\n")
        out += [f"- {exports._one_line(c)}" for c in r.acceptance_criteria]
        out.append("")


_METRIC = re.compile(r"\d[\d,.]*\s*(%|ms|s\b|seconds?|minutes?|orders?|requests?|users?|rps|per (day|second|minute))", re.I)


def brd(d: Design) -> str:
    s = d.state
    actors = sorted({a for r in s.requirements.values() for a in r.actors}, key=str.lower)
    high = [r for r in d.functional if r.priority in ("high", "critical", "must")]
    analysis = full_analysis(s)
    risks = [i for i in analysis["architecture_issues"] + analysis["requirement_issues"]
             if i["severity"] in ("error", "warning")]
    metrics = [(r, c) for r in d.functional + d.nfrs for c in r.acceptance_criteria if _METRIC.search(c)]
    out: list[str] = [f"# {d.name}: Business Requirements Document", "",
                      f"Generated by ArchFlow from branch `{d.branch}`.", ""]
    w = out.append
    w("## 1. Executive summary\n")
    w((d.description or f"{d.name} is described by {len(d.functional)} functional requirements.")
      + f" The solution has {len(d.top)} components.\n")
    w("## 2. Business objectives\n")
    out += [f"- {r.title} ({r.id})" for r in (high or d.functional)] or ["_No objectives recorded yet._"]
    w("")
    w("## 3. Stakeholders\n")
    out += [f"- **{a}**: involved in " + ", ".join(r.id for r in s.requirements.values() if a in r.actors) for a in actors] \
        or ["_No stakeholders recorded on the requirements._"]
    w("")
    w("## 4. Scope\n")
    w("### In scope\n")
    out += [f"- {r.title}" for r in d.functional] or ["_Nothing yet._"]
    w("")
    w("### Out of scope\n")
    excluded = [r for r in s.requirements.values() if r.status in ("rejected", "deferred")]
    out += [f"- {r.title} ({r.status})" for r in excluded] or ["_Nothing recorded as out of scope._"]
    w("")
    w("## 5. Business requirements\n")
    out += _table(["ID", "Requirement", "Priority", "Status", "Actors"],
                  [[r.id, r.title, r.priority, r.status, ", ".join(r.actors)] for r in d.functional]) \
        if d.functional else ["_None yet._"]
    w("")
    w("## 6. Quality expectations\n")
    out += [f"- **{r.title}** ({r.category or 'general'}): " + "; ".join(exports._one_line(c) for c in r.acceptance_criteria)
            for r in d.nfrs] or ["_None recorded._"]
    w("")
    w("## 7. Success metrics\n")
    out += [f"- {exports._one_line(c)} ({r.id})" for r, c in metrics] or \
        ["_No measurable criteria yet: add numbers (latency, volume, uptime) to the acceptance criteria._"]
    w("")
    w("## 8. Assumptions and constraints\n")
    items = [f"- {exports._one_line(m.get('text', ''))}" for m in d.memory]
    items += [f"- {x.get('id', '')}: {exports._one_line(x.get('decision') or x.get('title', ''))}"
              for x in d.decisions if x.get("status") == "accepted"]
    out += items or ["_None recorded._"]
    w("")
    w("## 9. Risks\n")
    out += [f"- **{i['severity']}**: {exports._one_line(i['message'])}" for i in risks[:15]] or ["_No open risks found._"]
    w("")
    w("## 10. Approval\n")
    out += _table(["Role", "Name", "Date", "Decision"], [["Business owner", "", "", ""], ["Architect", "", "", ""]])
    return "\n".join(out).rstrip() + "\n"


RUNBOOK_TYPES = ("service", "agent", "mcp", "gateway", "security", "database", "cache", "queue", "storage")
_HEALTH = {
    "service": ["`GET /healthz` returns 200 (process up) and `GET /readyz` returns 200 only when its dependencies answer",
                "Error rate and p95 latency on the service dashboard"],
    "gateway": ["Synthetic request through the gateway every minute", "5xx rate and upstream latency per route"],
    "security": ["Token issuance succeeds (synthetic login every 5 minutes)", "Signing key / certificate expiry dates"],
    "database": ["Connection succeeds and `SELECT 1` answers within 100 ms", "Replication lag, connections in use, "
                 "disk free, slow queries"],
    "cache": ["`PING` answers", "Hit ratio, memory used, evictions"],
    "queue": ["Consumers are connected", "Consumer lag / queue depth and age of the oldest message, dead-letter count"],
    "storage": ["A test object can be written and read", "Error rate and request latency"],
}
_SCALING = {
    "service": ["Scale out horizontally on CPU or request rate (stateless)", "Keep at least two replicas across zones"],
    "gateway": ["Scale on connections / requests per second", "Raise rate limits only together with the upstream capacity"],
    "security": ["Use the provider's managed scaling; cache token validation (JWKS) in callers"],
    "database": ["Scale reads with replicas; scale writes vertically or by partitioning",
                 "Watch connection pools when services scale out"],
    "cache": ["Scale memory before evictions hurt the hit ratio; shard by key when one node is not enough"],
    "queue": ["Add partitions / consumers when lag grows; consumers must stay idempotent"],
    "storage": ["Managed object storage scales on its own; spread hot keys / prefixes"],
}
_FAILURES = {
    "service": ["Crash loop after a deploy: roll back to the previous version",
                "Dependency timeouts: check the dependencies below, then the circuit breakers"],
    "gateway": ["All routes fail: check certificates, DNS and the gateway's configuration change history",
                "One route fails: the upstream service is down or unhealthy"],
    "security": ["Logins fail: check the identity provider status and the signing keys",
                 "Tokens rejected everywhere: clock skew or rotated keys not yet picked up"],
    "database": ["Primary down: fail over to the replica / standby and point the services at it",
                 "Disk full or connections exhausted: stop the noisy caller, then add capacity"],
    "cache": ["Cache down: services must fall back to the source of truth (expect higher latency)",
              "Memory full: evictions climb; raise memory or cut TTLs"],
    "queue": ["Consumers stopped: lag grows; restart them and watch the dead-letter queue",
              "Broker unavailable: producers buffer or fail fast; check the broker's health"],
    "storage": ["Access denied: check the bucket policy and credentials rotation", "Provider outage: serve from the CDN cache"],
}
_BACKUPS = {
    "database": ["Automated daily backups with point-in-time recovery; keep 7-35 days",
                 "Test a restore every quarter and record how long it took"],
    "cache": ["A cache holds no source data: no backup; warm it after a restart"],
    "queue": ["Retention long enough to replay a day of messages; back up topic configuration"],
    "storage": ["Versioning on; replicate to a second region if the data must survive a regional outage"],
}


def _runbook_family(node: Node) -> str:
    if node.type in ("agent", "mcp") or is_workload(node):
        return "service"
    return node.type


def _nfr_numbers(d: Design) -> dict[str, str]:
    """Targets read from the requirements (NFRs first): latency (ms), availability (%), RPO / RTO."""
    found: dict[str, str] = {}
    for r in d.nfrs + d.functional:
        for c in [r.title, r.description, *r.acceptance_criteria]:
            if m := re.search(r"(\d+(?:\.\d+)?)\s*ms", c, re.I):
                found.setdefault("latency_ms", m.group(1))
            if m := re.search(r"(9\d(?:\.\d+)?)\s*%", c):
                found.setdefault("availability", m.group(1))
            if m := re.search(r"\bRPO\b[^0-9]*(\d+\s*\w+)", c, re.I):
                found.setdefault("rpo", m.group(1))
            if m := re.search(r"\bRTO\b[^0-9]*(\d+\s*\w+)", c, re.I):
                found.setdefault("rto", m.group(1))
    return found


def runbooks(d: Design) -> list[File]:
    nums = _nfr_numbers(d)
    used: set[str] = set()
    files: list[File] = []
    index = [f"# {d.name}: runbooks", "", f"Generated by ArchFlow from branch `{d.branch}`. One runbook per service "
             "and data store.", ""]
    for node in d.top:
        if node.type not in RUNBOOK_TYPES:
            continue
        fam = _runbook_family(node)
        name = _dns(node.name, used, 40)
        deps, callers = _outgoing(d, node), _incoming(d, node)
        reqs = [r for r in node.requirement_ids if r in d.state.requirements]
        out = [f"# Runbook: {node.name}", "",
               f"- Type: {node.type}" + (f" · Technology: {node.technology}" if node.technology else ""),
               "- Depends on: " + (", ".join(f"{t.name} ({p})" if p else t.name for t, p in deps) or "nothing"),
               "- Called by: " + (", ".join(t.name for t, _ in callers) or "nobody"),
               "- Requirements: " + (", ".join(reqs) or "none traced"), ""]
        if node.description:
            out += [exports._one_line(node.description), ""]
        out += ["## Health checks", ""] + [f"- {x}" for x in _HEALTH.get(fam, _HEALTH["service"])] + [""]
        out += ["## Scaling", ""] + [f"- {x}" for x in _SCALING.get(fam, _SCALING["service"])]
        if _scales(d):
            perf = [r.id for r in d.nfrs if r.category.lower() in ("performance", "scalability")]
            out.append("- Size for the load in " + (", ".join(perf) if perf else "the performance requirements")
                       + "; load-test before peak periods")
        out.append("")
        out += ["## Failure modes", ""] + [f"- {x}" for x in _FAILURES.get(fam, _FAILURES["service"])]
        for t, _ in deps:
            out.append(f"- {t.name} unavailable: " + {"database": "writes fail; reads may fail; return 503 and alert",
                                                      "cache": "fall back to the source of truth",
                                                      "queue": "buffer or fail fast; replay once it is back",
                                                      "external": "degrade the feature and retry later",
                                                      "llm": "fall back to a simpler path and tell the user"}
                       .get(t.type, "requests that need it fail; check its runbook"))
        out.append("")
        out += ["## Backups and recovery", ""] + [f"- {x}" for x in _BACKUPS.get(fam, [
            "Stateless: redeploy the last good image; configuration lives in version control"])]
        if fam in ("database", "storage") and ("rpo" in nums or "rto" in nums):
            out.append(f"- Targets from the requirements: RPO {nums.get('rpo', 'not set')}, RTO {nums.get('rto', 'not set')}")
        out.append("")
        alerts = []
        if "latency_ms" in nums and fam in ("service", "gateway", "database"):
            alerts.append(f"p95 latency above {nums['latency_ms']} ms for 5 minutes (from the requirements)")
        if "availability" in nums:
            alerts.append(f"Error budget for {nums['availability']}% availability burning fast (1-hour burn rate > 14)")
        alerts += {"service": ["5xx rate above 1% for 5 minutes", "Restarts / crash loops"],
                   "gateway": ["5xx rate above 1%", "Certificate expires in under 14 days"],
                   "security": ["Login failure rate spikes", "Signing key or certificate expires in under 14 days"],
                   "database": ["CPU above 80% or storage above 80%", "Replication lag above 30 s", "Backup failed"],
                   "cache": ["Hit ratio drops below 80%", "Evictions climb", "Memory above 85%"],
                   "queue": ["Consumer lag / oldest message age above the agreed limit", "Dead-letter messages appear"],
                   "storage": ["4xx/5xx error rate climbs", "Unexpected public access"]}.get(fam, [])
        out += ["## Alerts", ""] + [f"- {x}" for x in alerts] + [""]
        if fam == "service" and reqs:
            out += ["## Business impact", ""] + [f"- {r} {d.state.requirements[r].title}" for r in reqs] + [""]
        files.append(File(f"docs/runbooks/{name}.md", "\n".join(out).rstrip() + "\n"))
        index.append(f"- [{node.name}]({name}.md) ({node.type})")
    if len(index) == 4:
        index.append("_No services or data stores in the design yet._")
    return [File("docs/runbooks/README.md", "\n".join(index) + "\n")] + files


# =========================================================================== validation
def _strip_hcl(text: str) -> str:
    """HCL without comments and string contents (keeps the quotes), for structure checks."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "#" or text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if text.startswith("<<", i) and re.match(r"<<-?([A-Z_]+)\n", text[i:]):
            m = re.match(r"<<-?([A-Z_]+)\n", text[i:])
            end = re.search(rf"^\s*{m.group(1)}\s*$", text[i + m.end():], re.M)
            i = n if not end else i + m.end() + end.end()
            out.append('""')
            continue
        if c == '"':
            j, depth = i + 1, 0
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if text[j] == "}" and depth:
                    depth -= 1
                elif text[j] == '"' and not depth:
                    break
                elif text[j] == "\n" and not depth:
                    out.append('"')  # unterminated string: keep a lone quote so the check reports it
                    i = j
                    break
                j += 1
            else:
                out.append('"')
                i = n
                continue
            if i == j:
                continue
            out.append('""')
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def check_hcl(files: list[File]) -> list[dict[str, Any]]:
    results, declared_vars, used_vars, addresses = [], set(), set(), {}
    for f in files:
        if not f.path.endswith(".tf"):
            continue
        problems: list[str] = []
        body = _strip_hcl(f.content)
        stack: list[str] = []
        pairs = {")": "(", "]": "[", "}": "{"}
        for ch in body:
            if ch in "([{":
                stack.append(ch)
            elif ch in pairs:
                if not stack or stack[-1] != pairs[ch]:
                    problems.append(f"unbalanced '{ch}'")
                    break
                stack.pop()
        if stack and not problems:
            problems.append(f"unclosed '{stack[-1]}'")
        if re.search(r'(?<!")"(?!")', body.replace('""', "")):
            problems.append("unterminated string")
        for m in re.finditer(r'^\s*(resource|data)\s+"([^"]+)"\s+"([^"]+)"', f.content, re.M):
            addr = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
            if addr in addresses:
                problems.append(f"duplicate {m.group(1)} {m.group(2)}.{m.group(3)} (also in {addresses[addr]})")
            addresses.setdefault(addr, f.path)
        for m in re.finditer(r'^\s*(module|variable|output)\s+"([^"]+)"', f.content, re.M):
            addr = f"{m.group(1)}.{m.group(2)}"
            if addr in addresses:
                problems.append(f"duplicate {m.group(1)} {m.group(2)} (also in {addresses[addr]})")
            addresses.setdefault(addr, f.path)
            if m.group(1) == "variable":
                declared_vars.add(m.group(2))
        used_vars |= set(re.findall(r"\bvar\.([A-Za-z_][A-Za-z0-9_]*)", f.content))
        results.append({"path": f.path, "ok": not problems, "detail": "; ".join(problems) or "well-formed HCL"})
    missing = sorted(used_vars - declared_vars)
    if missing and results:
        results.append({"path": "terraform/variables.tf", "ok": False,
                        "detail": "undeclared variables: " + ", ".join(missing)})
    code = "\n".join(x for f in files if f.path.endswith(".tf") for x in f.content.splitlines()
                     if not x.lstrip().startswith(("#", "//")))
    referenced = set(re.findall(r"\b((?:aws|azurerm|google)_[a-z0-9_]+\.[a-z0-9_]+)\.", code))
    defined = {a.split(".", 1)[1] for a in addresses if a.startswith("resource.")}
    dangling = sorted(r for r in referenced if r not in defined)
    if dangling and results:
        results.append({"path": "terraform/main.tf", "ok": False, "detail": "references to undefined resources: "
                        + ", ".join(dangling[:8])})
    return results


_DNS = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def check_yaml(files: list[File], target: str) -> list[dict[str, Any]]:
    results = []
    deployments: set[str] = set()
    selectors: list[tuple[str, str]] = []
    paths = {f.path for f in files}
    for f in files:
        if not f.path.endswith((".yml", ".yaml")):
            continue
        problems: list[str] = []
        try:
            docs = [x for x in yaml.safe_load_all(f.content) if x is not None]
        except yaml.YAMLError as exc:
            results.append({"path": f.path, "ok": False, "detail": f"YAML does not parse: {str(exc)[:200]}"})
            continue
        if target == "compose" and f.path == "docker-compose.yml":
            doc = docs[0] if docs else {}
            services = (doc or {}).get("services") or {}
            if not isinstance(services, dict) or not services:
                problems.append("no services")
            host_ports: dict[str, str] = {}
            for name, svc in services.items() if isinstance(services, dict) else []:
                if not isinstance(svc, dict) or not (svc.get("image") or svc.get("build")):
                    problems.append(f"{name}: needs an image or a build")
                    continue
                for dep in svc.get("depends_on") or []:
                    if dep not in services:
                        problems.append(f"{name} depends on unknown service {dep}")
                for p in svc.get("ports") or []:
                    hp = str(p).split(":")[0]
                    if hp in host_ports:
                        problems.append(f"host port {hp} used by {host_ports[hp]} and {name}")
                    host_ports[hp] = name
        if target == "k8s":
            for doc in docs:
                if not isinstance(doc, dict) or not doc.get("apiVersion") or not doc.get("kind"):
                    problems.append("a document lacks apiVersion or kind")
                    continue
                name = (doc.get("metadata") or {}).get("name", "")
                if doc["kind"] != "Kustomization" and not _DNS.match(str(name)):
                    problems.append(f"{doc['kind']} name {name!r} is not a valid DNS-1123 name")
                if doc["kind"] == "Deployment":
                    spec = doc.get("spec") or {}
                    match = (spec.get("selector") or {}).get("matchLabels") or {}
                    pod = ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}
                    if not match or any(pod.get(k) != v for k, v in match.items()):
                        problems.append(f"Deployment {name}: selector does not match the pod labels")
                    deployments.add(json.dumps(match, sort_keys=True))
                if doc["kind"] == "Service":
                    selectors.append((name, json.dumps((doc.get("spec") or {}).get("selector") or {}, sort_keys=True)))
                if doc["kind"] == "Kustomization":
                    for r in doc.get("resources") or []:
                        if f"k8s/{r}" not in paths:
                            problems.append(f"kustomization lists missing file {r}")
        results.append({"path": f.path, "ok": not problems, "detail": "; ".join(problems) or "valid YAML"})
    for name, sel in selectors:
        if sel not in deployments:
            results.append({"path": f"k8s/{name}.yaml", "ok": False, "detail": f"Service {name} selects no Deployment"})
    return results


def check_tests(files: list[File], target: str) -> list[dict[str, Any]]:
    results = []
    for f in files:
        problems: list[str] = []
        if f.path.endswith(".feature"):
            lines = [x.strip() for x in f.content.splitlines() if x.strip() and not x.strip().startswith("#")]
            if not any(x.startswith("Feature:") for x in lines):
                problems.append("no Feature:")
            scenario, seen = None, ""
            for x in lines + ["Scenario: end"]:
                if x.startswith(("Scenario:", "Scenario Outline:")):
                    if scenario is not None and seen != "GWT":
                        problems.append(f"scenario '{scenario}' needs Given, When and Then in that order")
                    scenario, seen = x.split(":", 1)[1].strip(), ""
                    continue
                word = x.split(" ", 1)[0]
                if scenario is not None and word in ("Given", "When", "Then"):
                    seen += word[0] if word[0] not in seen else ""
            if "Scenario:" not in f.content:
                problems.append("no scenarios")
        elif f.path.endswith(".py"):
            try:
                ast.parse(f.content)
            except SyntaxError as exc:
                problems.append(f"Python syntax error line {exc.lineno}: {exc.msg}")
        elif f.path.endswith(".ts"):
            body = re.sub(r"//[^\n]*", "", re.sub(r'"(?:\\.|[^"\\])*"', '""', f.content))
            if body.count("{") != body.count("}") or body.count("(") != body.count(")"):
                problems.append("unbalanced brackets")
        else:
            continue
        results.append({"path": f.path, "ok": not problems, "detail": "; ".join(problems) or "well-formed"})
    return results


def check_docs(files: list[File], required: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    results = []
    for f in files:
        problems: list[str] = []
        if not f.content.lstrip().startswith("# "):
            problems.append("does not start with a title")
        heads = set(re.findall(r"^#{2,6} (.+)$", f.content, re.M))
        missing = [h for h in (required or {}).get(f.path, []) if h not in heads]
        if missing:
            problems.append("missing sections: " + ", ".join(missing[:5]))
        results.append({"path": f.path, "ok": not problems, "detail": "; ".join(problems) or "well-formed"})
    return results


def check(kind: str, target: str, files: list[File], required: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    if target == "terraform":
        return check_hcl(files)
    if kind == "iac":
        return check_yaml(files, target)
    if kind == "tests":
        return check_tests(files, target)
    return check_docs([f for f in files if f.path.endswith(".md")], required)


def _sections(files: list[File]) -> dict[str, list[str]]:
    return {f.path: re.findall(r"^#{2,6} (.+)$", f.content, re.M) for f in files if f.path.endswith(".md")}


# =========================================================================== the agents
FILES_SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "files": {"type": "array", "items": {"type": "object", "properties": {
        "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    "notes": {"type": "array", "items": {"type": "string"}}}, "required": ["files"]}
TESTS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "features": {"type": "array", "items": {"type": "object", "properties": {
        "requirement_id": {"type": "string"},
        "scenarios": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "given": {"type": "array", "items": {"type": "string"}},
            "when": {"type": "array", "items": {"type": "string"}},
            "then": {"type": "array", "items": {"type": "string"}}}, "required": ["name", "given", "when", "then"]}}},
        "required": ["requirement_id", "scenarios"]}},
    "contracts": {"type": "array", "items": {"type": "object", "properties": {
        "from": {"type": "string"}, "to": {"type": "string"}, "checks": {"type": "array", "items": {"type": "string"}}},
        "required": ["from", "to", "checks"]}},
    "notes": {"type": "array", "items": {"type": "string"}}}, "required": ["features"]}


def design_summary(d: Design) -> str:
    """The design as compact JSON for the prompts."""
    impl = exports._implemented_by(d.state)
    data = {
        "project": d.name, "description": d.description, "branch": d.branch,
        "components": [{"name": n.name, "type": n.type, "technology": n.technology, "kind": kind_of(n),
                        "platform": placement(n)[0], "description": exports._one_line(n.description)[:200],
                        "requirements": [r for r in n.requirement_ids if r in d.state.requirements]} for n in d.top],
        "connections": [{"from": c.source, "to": c.target, "protocol": c.protocol} for c in contracts(d)],
        "requirements": [{"id": r.id, "kind": r.kind, "title": r.title, "category": r.category, "priority": r.priority,
                          "actors": r.actors, "description": exports._one_line(r.description)[:300],
                          "acceptance_criteria": [exports._one_line(c) for c in r.acceptance_criteria],
                          "implemented_by": impl.get(r.id) or []} for r in d.functional + d.nfrs],
        "accepted_decisions": [{"id": x.get("id"), "title": x.get("title"), "decision": x.get("decision") or ""}
                               for x in d.decisions if x.get("status") == "accepted"],
        "facts": [m.get("text", "") for m in d.memory],
    }
    return json.dumps(data, indent=1, ensure_ascii=False)


def _files_block(files: list[File]) -> str:
    out, total = [], 0
    for f in files:
        body = f.content if len(f.content) <= 12_000 else f.content[:12_000] + "\n… (truncated)"
        if total + len(body) > MAX_CONTEXT_CHARS:
            out.append(f'<file path="{f.path}" omitted="too long; keep it as it is" />')
            continue
        total += len(body)
        out.append(f'<file path="{f.path}">\n{body}\n</file>')
    return "\n".join(out)


_SAFE_PATH = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/@-]{1,200}$")


def _known_ids(d: Design) -> set[str]:
    return set(d.state.requirements) | {str(x.get("id")) for x in d.decisions if x.get("id")}


def _bad_ids(d: Design, text: str) -> list[str]:
    known = _known_ids(d)
    prefixes = {re.sub(r"\d+$", "", k) for k in known if re.search(r"\d$", k)} or {"REQ-", "NFR-", "FR-", "ADR-"}
    found = set(re.findall(r"\b([A-Z]{2,6}-\d{1,5})\b", text))
    return sorted(i for i in found if re.sub(r"\d+$", "", i) in prefixes and i not in known)


def merge_files(d: Design, kind: str, target: str, base: list[File], raw: dict[str, Any]) -> tuple[list[File], list[str]]:
    """Apply the agent's rewritten files: each must keep a safe path under the target's folders, stay well-formed,
    keep the deterministic document's sections and cite only real requirement / decision ids."""
    by_path = {f.path: f for f in base}
    roots = {f.path.split("/")[0] + ("/" if "/" in f.path else "") for f in base}
    required = _sections(base)
    merged = dict(by_path)
    notes: list[str] = []
    accepted = 0
    for item in (raw.get("files") or [])[:MAX_AI_FILES]:
        path = str(item.get("path") or "").strip().lstrip("./")
        content = str(item.get("content") or "")
        if not path or not content.strip() or len(content) > MAX_FILE_CHARS or not _SAFE_PATH.match(path):
            continue
        if path not in by_path and not any(path.startswith(r) for r in roots if r.endswith("/")):
            notes.append(f"Ignored {path}: outside the generated folders.")
            continue
        if not content.endswith("\n"):
            content += "\n"
        bad = _bad_ids(d, content)
        if bad:
            notes.append(f"Kept the generated {path}: the AI version cited unknown ids ({', '.join(bad[:4])}).")
            continue
        candidate = {**merged, path: File(path, content)}
        files = list(candidate.values())
        before = {c["path"] for c in check(kind, target, list(merged.values()), required) if not c["ok"]}
        after = [c for c in check(kind, target, files, required) if not c["ok"] and c["path"] not in before]
        if after:
            notes.append(f"Kept the generated {path}: the AI version was not well-formed ({after[0]['detail']}).")
            continue
        merged = candidate
        accepted += 1
    if not accepted:
        raise llm.LLMInvalidOutput("the model returned no usable file")
    notes += [exports._one_line(str(x))[:300] for x in (raw.get("notes") or [])[:6] if str(x).strip()]
    return list(merged.values()), notes


def merge_tests(d: Design, feats: list[Feature], items: list[Contract], raw: dict[str, Any]) \
        -> tuple[list[Feature], list[Contract], list[str]]:
    by_id = {f.requirement.id: f for f in feats}
    changed = 0
    out_feats = {f.requirement.id: f for f in feats}
    for item in raw.get("features") or []:
        rid = str(item.get("requirement_id") or "").strip()
        if rid not in by_id:
            continue
        scenarios = []
        for s in (item.get("scenarios") or [])[:12]:
            steps = {k: [exports._one_line(str(x))[:300] for x in (s.get(k) or []) if str(x).strip()][:6]
                     for k in ("given", "when", "then")}
            name = exports._one_line(str(s.get("name") or ""))[:120]
            if name and all(steps.values()) and not _bad_ids(d, json.dumps(s)):
                scenarios.append(Scenario(name, steps["given"], steps["when"], steps["then"]))
        if scenarios:
            out_feats[rid] = Feature(by_id[rid].requirement, scenarios, by_id[rid].components)
            changed += 1
    by_edge = {(c.source.lower(), c.target.lower()): c for c in items}
    out_items = {k: c for k, c in by_edge.items()}
    for item in raw.get("contracts") or []:
        key = (str(item.get("from") or "").strip().lower(), str(item.get("to") or "").strip().lower())
        checks = [exports._one_line(str(x))[:300] for x in (item.get("checks") or []) if str(x).strip()][:10]
        if key in by_edge and checks:
            c = by_edge[key]
            out_items[key] = Contract(c.source, c.target, c.protocol, c.family, checks)
            changed += 1
    if not changed:
        raise llm.LLMInvalidOutput("the model returned no usable scenarios")
    notes = [exports._one_line(str(x))[:300] for x in (raw.get("notes") or [])[:6] if str(x).strip()]
    return [out_feats[f.requirement.id] for f in feats], list(out_items.values()), notes


# =========================================================================== entry point
def offline(d: Design, kind: str, target: str, cloud: str = "") -> Result:
    if target not in TARGETS.get(kind, ()):
        raise ValueError(f"target for {kind} must be one of {', '.join(TARGETS.get(kind, ()))}"
                         if kind in TARGETS else f"kind must be one of {', '.join(TARGETS)}")
    notes: list[str] = []
    chosen = ""
    if target == "terraform":
        files, notes, chosen = terraform(d, cloud)
    elif target == "compose":
        files, notes = compose(d)
    elif target == "k8s":
        files, notes = kubernetes(d)
    elif kind == "tests":
        files = tests_files(d, target, features(d), contracts(d))
        if not d.state.requirements:
            notes.append("The branch has no requirements yet: only the contract checklist was generated.")
    elif target == "srs":
        files = [File("docs/SRS.md", srs(d))]
    elif target == "brd":
        files = [File("docs/BRD.md", brd(d))]
    else:
        files = runbooks(d)
    return Result(kind, target, files, "offline", notes, check(kind, target, files), chosen)


def generate(d: Design, kind: str, target: str, *, use_ai: bool = False, org_id: str | None = None,
             cloud: str = "", version: str | None = None) -> Result:
    """The target's files; with ``use_ai`` (and a model) the agent refines the deterministic version."""
    res = offline(d, kind, target, cloud)
    if not use_ai or not d.state.nodes and not d.state.requirements:
        return res
    if not llm.available(org_id):
        res.notes.append("No AI model is available: these files are the built-in version.")
        return res
    base_notes = list(res.notes)
    try:
        if kind == "tests":
            feats, items = features(d), contracts(d)
            raw = agents.run_json(PROMPTS[kind], TESTS_SCHEMA, org_id=org_id, version=version,
                                  question=f"Acceptance and contract tests ({target}) for {d.name}",
                                  variables={"project": d.name, "target": target, "design": design_summary(d),
                                             "scenarios": json.dumps([{"requirement_id": f.requirement.id, "scenarios": [
                                                 s.__dict__ for s in f.scenarios]} for f in feats], indent=1),
                                             "contracts": json.dumps([{"from": c.source, "to": c.target,
                                                                       "protocol": c.protocol} for c in items])})
            feats, items, notes = merge_tests(d, feats, items, raw)
            files = tests_files(d, target, feats, items)
        else:
            raw = agents.run_json(PROMPTS[kind], FILES_SCHEMA, org_id=org_id, version=version,
                                  question=f"{kind} ({target}) for {d.name}",
                                  variables={"project": d.name, "target": target,
                                             "cloud": CLOUD_LABELS.get(res.cloud, ""), "design": design_summary(d),
                                             "files": _files_block(res.files)})
            files, notes = merge_files(d, kind, target, res.files, raw)
    except (llm.LLMUnavailable, llm.LLMRefused) as exc:
        log.info("generation %s/%s fell back to the built-in version: %s", kind, target, exc)
        res.notes.append(f"The AI could not refine the files ({exc}); these are the built-in version.")
        return res
    required = _sections(res.files)
    return Result(kind, target, files, "ai", base_notes + notes, check(kind, target, files, required), res.cloud)


# --------------------------------------------------------------------------- zip
def safe_files(items: list[dict[str, Any]]) -> list[File]:
    files, total, seen = [], 0, set()
    for it in items[:MAX_ZIP_FILES]:
        path = str(it.get("path") or "").strip().replace("\\", "/")
        content = str(it.get("content") or "")
        if not _SAFE_PATH.match(path) or path in seen:
            raise ValueError(f"invalid or duplicate file path: {path!r}")
        total += len(content.encode("utf-8"))
        if total > MAX_ZIP_BYTES:
            raise ValueError("the files are too large to zip")
        seen.add(path)
        files.append(File(path, content))
    if not files:
        raise ValueError("no files to zip")
    return files


def to_zip(files: list[File], root: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            info = zipfile.ZipInfo(f"{root}/{f.path}" if root else f.path, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.content)
    return buf.getvalue()


# --------------------------------------------------------------------------- REST
class GenerateIn(BaseModel):
    kind: str
    target: str
    branch: str | None = None
    ai: bool = False
    cloud: str = ""


class ZipIn(GenerateIn):
    files: list[dict[str, Any]] | None = Field(default=None, description="Files to zip as previewed; else regenerated")


def register_routes(app: FastAPI, store: Any, *, authz: Any, state_or_404: Callable[[str, str], ProjectState]) -> None:
    """Viewers can generate the built-in version; AI refinement needs edit rights (it spends the AI budget)."""
    read = authz.project("read")

    def run(pid: str, body: GenerateIn, branch: str, a: Any) -> Result:
        if body.ai and not a.can("edit"):
            raise HTTPException(403, "Only editors can use AI generation. Turn AI off to get the built-in version.")
        branch = body.branch or branch
        d = collect(store, pid, branch, state_or_404(pid, branch))
        try:
            return generate(d, body.kind, body.target, use_ai=body.ai, org_id=a.org_id, cloud=body.cloud)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/projects/{pid}/generate")
    async def generate_artifacts(pid: str, body: GenerateIn, branch: str = "main", a=Depends(read)):
        res = await asyncio.to_thread(run, pid, body, branch, a)
        return {**res.dump(), "branch": body.branch or branch}

    @app.post("/api/projects/{pid}/generate/zip")
    async def generate_zip(pid: str, body: ZipIn, branch: str = "main", a=Depends(read)):
        """The files as a zip: the ones given (what the preview showed), else generated again."""
        if body.target not in TARGETS.get(body.kind, ()):
            raise HTTPException(422, "unknown kind or target")
        branch = body.branch or branch
        if body.files is not None:
            state_or_404(pid, branch)
            try:
                files = safe_files(body.files)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        else:
            files = (await asyncio.to_thread(run, pid, body, branch, a)).files
        name = exports._filename(a.project.get("name", "project"), branch, body.kind, body.target)
        return Response(to_zip(files, name), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{name}.zip"', "Cache-Control": "no-store"})
