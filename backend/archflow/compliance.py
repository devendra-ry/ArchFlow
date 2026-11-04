"""Compliance frameworks: SOC 2, HIPAA, GDPR, PCI DSS and ISO 27001 controls checked against the architecture.

Each framework lists a curated set of *architecture-relevant* controls (``FRAMEWORKS``): the ones a design can
show or break, such as encryption in transit and at rest, authentication in front of public services, audit
logging, backups, network segmentation, data residency, retention and AI guardrails. Process-only controls (HR
screening, security training, ...) are left out: this is a design check, not an audit.

A control is backed by one or more deterministic *checks* (``CHECKS``) that read the branch's top-level diagram:
component types, technologies, descriptions and properties, the protocols on connections, and the statements in
the requirements, the project memory and the active decisions ("all data stays in the EU", "TLS 1.2+ on every
connection", "backups every 24 h, RPO 1 h"). A check returns

* ``pass``            the design shows it,
* ``fail``            the design contradicts it or lacks it (e.g. a plain-HTTP public connection, no audit log),
* ``needs_evidence``  the design cannot show it either way (e.g. internal connections without a protocol), or it is
                      planned in a requirement but not visible in the design,
* ``not_applicable``  nothing to check (no LLM, no databases, no payments, ...),

with the components and requirements involved, what was found and how to fix it. A control's status is the
worst of its checks. A person can mark a control *met by process* or *not applicable*, with a note (stored per
project in ``compliance_evidence``); the score of a framework is the share of applicable controls that pass or are
met by process. The frameworks that apply to a project are stored in ``compliance_settings``; until someone picks
them they are suggested from the project text (payments → PCI DSS, health data → HIPAA, the EU or personal data
→ GDPR; SOC 2 always).

``compliance.review`` (an agent) writes remediation steps grounded in the failing controls and proposes
non-functional requirements to add; without a model, the checks' own fixes and requirement stubs are used.

The report downloads as Markdown, JSON and print-ready HTML (the browser's "Save as PDF", as ``exports``).

=======  ==================================================  ===============================================
method   path                                                what
=======  ==================================================  ===============================================
GET      /api/projects/{pid}/compliance?branch&frameworks    the report (deterministic)
PUT      /api/projects/{pid}/compliance/settings             {frameworks} that apply (editors)
PUT      /api/projects/{pid}/compliance/evidence             {framework, control_id, status, note} (editors)
POST     /api/projects/{pid}/compliance/review?branch        {frameworks, ai} → remediation steps + NFRs
GET      /api/projects/{pid}/compliance.md|.json|.html       the report as a file
=======  ==================================================  ===============================================
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections import Counter
from typing import Any, Callable, Iterable

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import agents, catalog, llm
from .analysis import diagram_of, flow_checks, is_auth, is_key_management
from .checklists import CAPABILITIES
from .models import Node, ProjectState, active_requirements

log = logging.getLogger("archflow.compliance")

PROMPT = "compliance.review"
STATUSES = ("pass", "fail", "needs_evidence", "not_applicable")
EVIDENCE_STATUSES = ("met", "not_applicable", "")  # "" = a note only; the computed status stands
_RANK = {"not_applicable": 0, "pass": 1, "needs_evidence": 2, "fail": 3}
MAX_LISTED = 8

# --------------------------------------------------------------------------- frameworks and controls
# control: (id, title, plain description, [check ids]). Ids are the framework's own references.
FRAMEWORKS: dict[str, dict[str, Any]] = {
    "soc2": {
        "name": "SOC 2", "version": "Trust Services Criteria 2017, revised 2022",
        "description": "Security, availability and confidentiality controls auditors test for a SOC 2 report.",
        "detect": None,  # always suggested
        "controls": [
            ("CC6.1", "Logical access", "Users and services sign in through an identity provider, access follows roles, "
             "and secrets and keys are managed.", ["authentication", "access_control", "key_management"]),
            ("CC6.6", "Boundary protection", "Public traffic enters through a gateway, and data stores are not reachable "
             "from outside.", ["edge_protection", "network_segmentation"]),
            ("CC6.7", "Encryption in transit and at rest", "Data is encrypted on every connection and in every data "
             "store.", ["encryption_in_transit", "encryption_at_rest"]),
            ("CC6.8", "Protection against malicious input", "A web application firewall filters public traffic and AI "
             "guardrails filter prompts and model output.", ["waf", "llm_guardrails"]),
            ("CC7.2", "Monitoring and audit logging", "Systems are monitored, and security-relevant actions are "
             "logged.", ["monitoring", "audit_logging"]),
            ("CC9.2", "Vendor risk", "Third parties that receive data are known and reviewed.", ["vendor_processors"]),
            ("A1.2", "Backups and recovery", "Data is backed up and databases can fail over.", ["backups", "resilience"]),
            ("C1.2", "Disposal of confidential data", "Data is kept only as long as needed, then deleted.",
             ["retention"]),
        ],
    },
    "hipaa": {
        "name": "HIPAA", "version": "Security Rule, 45 CFR Part 164",
        "description": "Safeguards for electronic protected health information (ePHI) in the United States.",
        "detect": r"hipaa|\bphi\b|\bephi\b|health|patient|medical|clinic|hospital|\behr\b|\bemr\b|telehealth",
        "controls": [
            ("164.312(a)(1)", "Access control", "Only authorized people and services reach ePHI, by role.",
             ["authentication", "access_control"]),
            ("164.312(a)(2)(iv)", "Encryption at rest", "ePHI is encrypted where it is stored, with managed keys.",
             ["encryption_at_rest", "key_management"]),
            ("164.312(b)", "Audit controls", "Activity on systems holding ePHI is recorded and reviewed.",
             ["audit_logging", "monitoring"]),
            ("164.312(d)", "Person or entity authentication", "People prove who they are, with a second factor.",
             ["mfa"]),
            ("164.312(e)(1)", "Transmission security", "ePHI is encrypted on every connection.",
             ["encryption_in_transit"]),
            ("164.308(a)(7)", "Contingency plan", "ePHI is backed up and can be restored after an outage.",
             ["backups", "resilience"]),
            ("164.308(b)(1)", "Business associate agreements", "Every vendor that handles ePHI has signed a BAA.",
             ["vendor_processors"]),
            ("164.502(b)", "Minimum necessary", "Data stores holding ePHI are known, and AI models only see what they "
             "need.", ["pii_tagging", "llm_guardrails"]),
            ("164.316(b)(2)", "Retention", "Records are kept for the required time and then deleted.", ["retention"]),
        ],
    },
    "gdpr": {
        "name": "GDPR", "version": "EU Regulation 2016/679",
        "description": "Protection of personal data of people in the EU and EEA.",
        "detect": r"gdpr|\beu\b|\beea\b|europe|personal data|\bpii\b|privacy|data protection|\buk\b",
        "controls": [
            ("Art. 5(1)(e)", "Storage limitation", "Personal data is kept no longer than needed.", ["retention"]),
            ("Art. 17", "Right to erasure", "A person's data can be found and deleted on request, everywhere it is "
             "stored.", ["erasure"]),
            ("Art. 25", "Data protection by design", "Access follows roles, and AI models are guarded against leaking "
             "personal data.", ["access_control", "llm_guardrails"]),
            ("Art. 28", "Processors", "Every third party that processes personal data has a data processing "
             "agreement.", ["vendor_processors"]),
            ("Art. 30", "Records of processing", "Data stores holding personal data are identified.", ["pii_tagging"]),
            ("Art. 32", "Security of processing", "Personal data is encrypted in transit and at rest, with managed "
             "keys.", ["encryption_in_transit", "encryption_at_rest", "key_management"]),
            ("Art. 32(1)(c)", "Restore availability", "Personal data can be restored in time after an incident.",
             ["backups"]),
            ("Art. 33", "Breach detection", "Breaches can be detected and investigated within 72 hours.",
             ["audit_logging", "monitoring"]),
            ("Art. 44", "International transfers", "Where personal data is stored and processed is stated and "
             "controlled.", ["data_residency"]),
        ],
    },
    "pci": {
        "name": "PCI DSS", "version": "v4.0",
        "description": "Protection of payment card data for anyone who stores, processes or transmits it.",
        "detect": r"\bpci\b|payment|credit card|card data|checkout|stripe|adyen|braintree|billing",
        "controls": [
            ("1.3", "Network security controls", "The cardholder data environment is separated from untrusted "
             "networks.", ["network_segmentation", "edge_protection"]),
            ("3.3", "Minimal card data", "Card numbers are not stored, or are tokenized by a PCI-compliant provider.",
             ["cardholder_data", "retention"]),
            ("3.5", "Stored account data is protected", "Any stored account data is encrypted with managed keys.",
             ["encryption_at_rest", "key_management"]),
            ("4.2", "Strong cryptography in transit", "Card data is encrypted on every connection.",
             ["encryption_in_transit"]),
            ("6.4", "Public web apps are protected", "A web application firewall sits in front of public "
             "applications.", ["waf"]),
            ("7.2", "Access by need to know", "Access to systems and data follows roles.", ["access_control"]),
            ("8.3", "Strong authentication", "Users and administrators sign in through an identity provider.",
             ["authentication"]),
            ("8.4", "Multi-factor authentication", "Access to the cardholder data environment uses MFA.", ["mfa"]),
            ("10.2", "Audit logs", "Access to systems and card data is logged and monitored.",
             ["audit_logging", "monitoring"]),
            ("12.8", "Third-party service providers", "Providers that touch card data are listed and their PCI "
             "status is checked.", ["vendor_processors"]),
        ],
    },
    "iso27001": {
        "name": "ISO 27001", "version": "ISO/IEC 27001:2022 Annex A",
        "description": "Information security controls of an ISO 27001 information security management system.",
        "detect": r"iso ?27001|\bisms\b",
        "controls": [
            ("A.5.15", "Access control", "Access follows roles, through an identity provider.",
             ["access_control", "authentication"]),
            ("A.5.19", "Supplier relationships", "Suppliers that receive data are known and assessed.",
             ["vendor_processors"]),
            ("A.5.34", "Privacy and protection of PII", "Data stores holding personal data are identified.",
             ["pii_tagging"]),
            ("A.8.5", "Secure authentication", "Sign-in uses a second factor where it matters.", ["mfa"]),
            ("A.8.10", "Information deletion", "Data is deleted when no longer needed or on request.",
             ["retention", "erasure"]),
            ("A.8.13", "Information backup", "Data stores are backed up and restores are tested.", ["backups"]),
            ("A.8.14", "Redundancy", "Data stores can fail over.", ["resilience"]),
            ("A.8.15", "Logging", "Security events are logged.", ["audit_logging"]),
            ("A.8.16", "Monitoring", "Systems are monitored for unusual behaviour.", ["monitoring"]),
            ("A.8.20", "Network security", "Public traffic enters through gateways, and networks are segmented.",
             ["edge_protection", "network_segmentation"]),
            ("A.8.24", "Use of cryptography", "Data is encrypted in transit and at rest, with managed keys.",
             ["encryption_in_transit", "encryption_at_rest", "key_management"]),
            ("A.8.26", "Application security", "Public apps sit behind a WAF and AI models behind guardrails.",
             ["waf", "llm_guardrails"]),
        ],
    },
}
DEFAULT_FRAMEWORKS = ("soc2",)

# --------------------------------------------------------------------------- what the checks look for
_SECURE = re.compile(r"https|\btls\b|\bssl\b|\bmtls\b|\bwss\b|grpcs|amqps|mqtts|rediss|sftp|ftps|\bssh\b|ipsec|\bvpn\b|"
                     r"private ?link|private endpoint|encrypt", re.I)
_PLAIN = re.compile(r"\bhttp\b|\bws\b|\bftp\b|telnet|\bsmtp\b|plain ?text|unencrypted", re.I)
_TRANSIT_POLICY = re.compile(r"\btls\b|\bmtls\b|encrypt\w*[^.]{0,40}\bin[- ]transit|in[- ]transit[^.]{0,40}encrypt|"
                             r"https (only|everywhere)|service mesh|zero[- ]trust", re.I)
_MESH = re.compile(r"istio|linkerd|consul connect|app mesh|service mesh|cilium", re.I)
_AT_REST_NOTE = re.compile(r"encrypt|\bkms\b|\bcmk\b|\bsse\b|\btde\b|customer[- ]managed key", re.I)
_AT_REST_POLICY = re.compile(r"encrypt\w*[^.]{0,60}\bat[- ]rest|at[- ]rest[^.]{0,60}encrypt", re.I)
_ENCRYPT = re.compile(r"encrypt", re.I)
_RBAC = re.compile(r"\brbac\b|\babac\b|role[- ]based|least[- ]privilege|permission|\broles?\b|authori[sz]", re.I)
_MFA = re.compile(r"\bmfa\b|\b2fa\b|multi[- ]factor|two[- ]factor|passkey|webauthn|\btotp\b|one[- ]time (code|password)",
                  re.I)
_WAF = re.compile(r"\bwaf\b|web application firewall|cloud armor|aws shield|imperva|modsecurity|coraza", re.I)
_EDGE_WAF = re.compile(r"cloudflare|front door|akamai|fastly", re.I)
_AUDIT = re.compile(r"audit|cloudtrail|activity log|\bsiem\b|splunk|sentinel|chronicle|security lake|ledger", re.I)
_AUDIT_STMT = re.compile(r"audit", re.I)
_MONITOR = re.compile(CAPABILITIES["observability"]["words"], re.I)
_BACKUP_NOTE = re.compile(r"backup|snapshot|\bpitr\b|point[- ]in[- ]time|geo[- ]redundant|cross[- ]region", re.I)
_BACKUP_NODE = re.compile(r"\bbackups?\b|velero|veeam|backup (vault|service)", re.I)
_BACKUP_STMT = re.compile(r"backup|\brpo\b|\brto\b|disaster recovery|\bdr\b|restore", re.I)
_REPLICATED = re.compile(r"multi[- ]?az|replica|replicat|cluster|\bha\b|high availability|aurora|dynamodb|cosmos|spanner|"
                         r"firestore|bigtable|cockroach|cassandra|alloydb|atlas|standby|failover|geo[- ]redundant|"
                         r"zone[- ]redundant", re.I)
_AVAIL_STMT = re.compile(r"99\.9|high availability|multi[- ]?az|failover|uptime|\bsla\b|\brto\b|\brpo\b", re.I)
_CLASS_KEYS = {"pii", "phi", "pci", "personal_data", "contains_pii", "data_classification", "classification",
               "sensitivity", "data_category"}
_CLASS_NOTE = re.compile(r"\bpii\b|personal data|\bphi\b|\bephi\b|cardholder|sensitive|confidential|anonymi[sz]ed|"
                         r"pseudonymi[sz]ed|no personal data|public data|classification", re.I)
_RESIDENCY = re.compile(r"data residency|\bresidency\b|\b(stays?|kept|stored|hosted|processed) (only )?(in|within) (the )?"
                        r"(eu|eea|europe|us|usa|uk|united states|united kingdom|germany|france|india|canada|australia|"
                        r"switzerland|[a-z]+-[a-z]+-\d)\b|"
                        r"\b(eu|europe|european|us|uk|germany|india|canada|australia|swiss)[- ](region|only|hosted|based|"
                        r"data ?cent(er|re)s?)\b|eu-(west|central|north|south)|europe-west|westeurope|northeurope|"
                        r"frankfurt|ireland|in[- ]region|sovereign|data localisation|data localization", re.I)
_EU = re.compile(r"gdpr|\beu\b|\beea\b|europe", re.I)
_RETENTION = re.compile(r"retention|\bretain|kept for|keep [^.]{0,30}\bfor \d|delete[ds]? after|purge|archiv|\bttl\b|"
                        r"time[- ]to[- ]live|lifecycle (rule|polic)", re.I)
_ERASURE = re.compile(r"right to (be forgotten|erasure)|erasure|data deletion|delete (their|personal|customer|user|"
                      r"patient|account) data|deleted on request|deletion request|account deletion", re.I)
_EVENT_LOG = re.compile(r"kafka|kinesis|event hubs|\bmsk\b|pulsar|event ?store|redpanda|event (log|stream)", re.I)
_SHRED = re.compile(r"crypto[- ]?shred|compaction|tombstone", re.I)
_PROCESSOR_STMT = re.compile(r"\bdpa\b|data processing agreement|\bbaa\b|business associate|sub-?processor|"
                             r"vendor (review|assessment|risk)|soc ?2 report|aoc\b|attestation of compliance", re.I)
_PAYMENT_CTX = re.compile(r"payment|credit card|card data|checkout|\bpci\b|billing", re.I)
_PAYMENT_PROVIDER = re.compile(r"stripe|adyen|braintree|paypal|checkout\.com|mollie|razorpay|square\b|worldpay|"
                               r"authorize\.net", re.I)
_CARD_STORED = re.compile(r"card numbers?|\bpans?\b|cardholder data|\bcvv\b|\bcvc\b", re.I)
_GUARD = re.compile(CAPABILITIES["guardrails"]["words"], re.I)
_GUARD_STMT = re.compile(r"guardrail|prompt[- ]injection|content (filter|safety)|moderation|pii (redaction|filter)",
                         re.I)
_PERSONAL_CTX = re.compile(r"personal|\bpii\b|customer|user|patient|account|email|phone|address|profile|gdpr|hipaa", re.I)


def _falsy(v: str) -> bool:
    return str(v).strip().lower() in ("", "no", "false", "none", "off", "0", "n/a")


# --------------------------------------------------------------------------- reading the project
class Context:
    """The parts of a project the checks read: the top-level diagram, statements and requirement texts."""

    def __init__(self, state: ProjectState, memory: list[dict[str, Any]] | None = None,
                 decisions: list[dict[str, Any]] | None = None, description: str = ""):
        self.state = state
        self.nodes: dict[str, Node] = {nid: n for nid, n in state.nodes.items()
                                       if not diagram_of(state, n) and n.type != "note"}
        self.edges = [e for e in state.edges.values() if e.source in self.nodes and e.target in self.nodes
                      and e.source != e.target]
        self.reqs = active_requirements(state)
        # (source, id, text): requirements, memory and the decisions still in force.
        self.statements: list[tuple[str, str, str]] = (
            [("requirement", r.id, " ".join([r.title, r.description, *r.acceptance_criteria]))
             for r in self.reqs.values()]
            + [("memory", m.get("id", ""), m.get("text", "")) for m in memory or []]
            + [("decision", d.get("id", ""), f"{d.get('title', '')}. {d.get('decision', '')}")
               for d in decisions or [] if d.get("status") in ("accepted", "proposed")])
        self.description = description
        self.vendor = self._vendor()

    def _vendor(self) -> str:
        counts = Counter(n.vendor for n in self.nodes.values() if n.vendor in ("aws", "azure", "gcp"))
        return counts.most_common(1)[0][0] if counts else "agnostic"

    def of(self, *types: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.type in types]

    def stated(self, pattern: re.Pattern) -> list[tuple[str, str, str]]:
        return [s for s in self.statements if pattern.search(s[2])]

    def text(self) -> str:
        return " ".join([self.description, *(s[2] for s in self.statements),
                         *(f"{n.name} {n.technology} {n.description}" for n in self.nodes.values())])

    def neighbours(self, nid: str) -> set[str]:
        return {e.target for e in self.edges if e.source == nid} | {e.source for e in self.edges if e.target == nid}


def _role(n: Node) -> str:
    return f"{n.name} {n.technology}"


def _full(n: Node) -> str:
    props = " ".join(f"{k} {v}" for k, v in n.props.items())
    return f"{n.name} {n.technology} {n.description} {props}"


def _kind(n: Node) -> str:
    entry = catalog.match({"technology": n.technology, "name": n.name, "catalog_id": n.catalog_id})
    return entry["kind"] if entry else ""


def _managed(n: Node) -> bool:
    entry = catalog.match({"technology": n.technology, "name": n.name, "catalog_id": n.catalog_id})
    return bool(entry and entry["vendor"] in ("aws", "azure", "gcp", "saas")) or n.vendor in ("aws", "azure", "gcp", "saas")


def _tech(ctx: Context, cap: str) -> str:
    tech = CAPABILITIES[cap]["component"]["tech"]
    return tech.get(ctx.vendor) or tech["agnostic"]


def _names(ctx: Context, ids: Iterable[str]) -> str:
    names = [ctx.nodes[i].name for i in ids if i in ctx.nodes]
    more = len(names) - MAX_LISTED
    return ", ".join(names[:MAX_LISTED]) + (f" and {more} more" if more > 0 else "")


def _cite(hits: list[tuple[str, str, str]]) -> str:
    s = hits[0]
    ref = "project memory" if s[0] == "memory" else s[1]
    text = " ".join(s[2].split())
    return f"{ref}: \"{text[:120]}{'…' if len(text) > 120 else ''}\""


def _result(status: str, summary: str, components: Iterable[str] = (), fix: str = "",
            evidence: list[tuple[str, str, str]] | None = None) -> dict[str, Any]:
    return {"status": status, "summary": summary, "components": list(dict.fromkeys(components)), "fix": fix,
            "statements": [{"source": s[0], "id": s[1], "text": " ".join(s[2].split())[:300]} for s in (evidence or [])[:3]]}


# --------------------------------------------------------------------------- checks
def _public(ctx: Context, nid: str) -> bool:
    return ctx.nodes[nid].type in ("client", "external")


def check_encryption_in_transit(ctx: Context) -> dict[str, Any]:
    if not ctx.edges:
        return _result("not_applicable", "No connections on the diagram yet.")
    policy = ctx.stated(_TRANSIT_POLICY)
    mesh = [n.id for n in ctx.nodes.values() if _MESH.search(_role(n))]
    plain_public, unknown = [], []
    for e in ctx.edges:
        text = f"{e.protocol} {e.label}"
        public = _public(ctx, e.source) or _public(ctx, e.target)
        if _SECURE.search(text):
            continue
        if _PLAIN.search(text) and public:
            plain_public.append(e)
        else:
            unknown.append(e)
    if plain_public:
        ids = [x for e in plain_public for x in (e.source, e.target)]
        pairs = ", ".join(f"{ctx.nodes[e.source].name} → {ctx.nodes[e.target].name} ({e.protocol or 'no protocol'})"
                          for e in plain_public[:MAX_LISTED])
        return _result("fail", f"Public connections without TLS: {pairs}.", ids,
                       "Use HTTPS / WSS (TLS 1.2 or later) on every connection that leaves your network, and redirect "
                       "plain HTTP to HTTPS.")
    if unknown and not (policy or mesh):
        ids = [x for e in unknown for x in (e.source, e.target)]
        return _result("needs_evidence", f"{len(unknown)} connection(s) do not say whether they use TLS, e.g. "
                       + ", ".join(f"{ctx.nodes[e.source].name} → {ctx.nodes[e.target].name}"
                                   f" ({e.protocol or 'no protocol'})" for e in unknown[:4]) + ".", ids,
                       "Set the protocol of each connection (HTTPS, gRPC over TLS, TLS to the database), or state "
                       "\"TLS on every internal connection\" in a security requirement (a service mesh with mTLS "
                       "covers service-to-service traffic).")
    if not unknown:
        return _result("pass", "Every connection uses an encrypted protocol.")
    how = f"the service mesh {_names(ctx, mesh)}" if mesh else _cite(policy)
    return _result("pass", f"Connections without a stated protocol are covered by {how}.", mesh, evidence=policy)


def _stores(ctx: Context) -> list[Node]:
    return ctx.of("database", "storage")


def check_encryption_at_rest(ctx: Context) -> dict[str, Any]:
    stores = _stores(ctx)
    if not stores:
        return _result("not_applicable", "No databases or storage on the diagram.")
    noted = [n for n in stores if _AT_REST_NOTE.search(n.description)
             or any(k.lower() in ("encryption", "encrypted", "encryption_at_rest", "kms_key") and not _falsy(v)
                    for k, v in n.props.items())]
    missing = [n.id for n in stores if n not in noted]
    policy = ctx.stated(_AT_REST_POLICY)
    if not missing:
        return _result("pass", "Every data store notes its encryption at rest.")
    if policy:
        return _result("pass", f"Encryption at rest is required for all data ({_cite(policy)}).", evidence=policy)
    general = ctx.stated(_ENCRYPT)
    kms = [n.id for n in ctx.nodes.values() if is_key_management(n)]
    fix = (f"Turn on storage encryption with managed keys ({_tech(ctx, 'key_management')}) for {_names(ctx, missing)} "
           "and note it on each component (property encryption: \"AES-256, KMS\").")
    if general or kms:
        why = _cite(general) if general else f"a key manager ({_names(ctx, kms)}) is on the diagram"
        return _result("needs_evidence", f"Encryption is planned ({why}), but encryption at rest is not noted on "
                       f"{_names(ctx, missing)}.", missing, fix, general)
    return _result("fail", f"No data store says it is encrypted at rest: {_names(ctx, missing)}.", missing, fix)


def check_key_management(ctx: Context) -> dict[str, Any]:
    backend = ctx.of("service", "agent", "database", "storage")
    if not backend:
        return _result("not_applicable", "No services or data stores yet.")
    kms = [n.id for n in ctx.nodes.values() if is_key_management(n)]
    if kms:
        return _result("pass", f"Keys and secrets are managed by {_names(ctx, kms)}.", kms)
    return _result("fail", "No secrets manager or key management service.", [n.id for n in backend[:MAX_LISTED]],
                   f"Add {_tech(ctx, 'key_management')} for encryption keys and service secrets; never keep "
                   "secrets in code or environment files.")


def _entries(ctx: Context) -> list[str]:
    clients = {n.id for n in ctx.of("client", "external")}
    return list(dict.fromkeys(e.target for e in ctx.edges if e.source in clients and not _public(ctx, e.target)))


def check_authentication(ctx: Context) -> dict[str, Any]:
    entries = _entries(ctx)
    if not entries:
        return _result("not_applicable", "No clients or callers reach the system yet.")
    auth = [n.id for n in ctx.nodes.values() if is_auth(n)]
    if not auth:
        return _result("fail", f"No identity provider: {_names(ctx, entries)} take requests without sign-in.", entries,
                       "Add an identity provider (e.g. Amazon Cognito, Microsoft Entra ID, Auth0, Keycloak) and "
                       "validate tokens at every entry point.")
    open_ = [nid for i in flow_checks(ctx.state) if i.get("code") == "unauthenticated_entry"
             for nid in i.get("node_ids") or [] if nid in ctx.nodes]
    if open_:
        return _result("fail", f"No path to the identity provider ({_names(ctx, auth)}) from {_names(ctx, open_)}.",
                       open_ + auth, f"Validate tokens from {_names(ctx, auth)} at {_names(ctx, open_)}, or put them "
                       "behind the authenticated API gateway.")
    return _result("pass", f"Entry points authenticate through {_names(ctx, auth)}.", auth)


def check_access_control(ctx: Context) -> dict[str, Any]:
    if not ctx.nodes:
        return _result("not_applicable", "The diagram is empty.")
    hits = ctx.stated(_RBAC)
    if hits:
        return _result("pass", f"Access rules are stated ({_cite(hits)}).", evidence=hits)
    auth = [n.id for n in ctx.nodes.values() if is_auth(n)]
    fix = ("Add a requirement that lists the roles (e.g. customer, support agent, admin), what each can do, and that "
           "services use least-privilege credentials.")
    if auth:
        return _result("needs_evidence", "There is an identity provider, but no requirement says who may do what.",
                       auth, fix)
    return _result("fail", "No identity provider and no access rules.", [], fix)


def check_mfa(ctx: Context) -> dict[str, Any]:
    auth = [n.id for n in ctx.nodes.values() if is_auth(n)]
    if not auth and not _entries(ctx):
        return _result("not_applicable", "No sign-in on the diagram yet.")
    hits = ctx.stated(_MFA) + [("component", n.name, _full(n)) for n in ctx.nodes.values() if _MFA.search(_full(n))]
    if hits:
        return _result("pass", f"Multi-factor sign-in is stated ({_cite(hits)}).", auth, evidence=hits)
    fix = "Require MFA (TOTP, passkeys or WebAuthn) for administrators and for anyone who reaches sensitive data."
    if auth:
        return _result("needs_evidence", f"Sign-in goes through {_names(ctx, auth)}, but nothing says MFA is required.",
                       auth, fix)
    return _result("fail", "No identity provider, so no multi-factor sign-in.", [], fix)


def check_edge_protection(ctx: Context) -> dict[str, Any]:
    clients = [n.id for n in ctx.of("client")]
    if not clients:
        return _result("not_applicable", "No clients on the diagram.")
    direct = [e for e in ctx.edges if e.source in clients
              and ctx.nodes[e.target].type in ("service", "agent", "mcp", "llm")]
    if direct:
        ids = list(dict.fromkeys(e.target for e in direct))
        return _result("fail", f"Clients call {_names(ctx, ids)} directly, without a gateway.", ids,
                       "Route client traffic through an API gateway or load balancer that terminates TLS, "
                       "authenticates and rate-limits requests.")
    gws = [n.id for n in ctx.of("gateway")]
    return _result("pass", f"Clients enter through {_names(ctx, gws) or 'gateways'}.", gws)


def check_waf(ctx: Context) -> dict[str, Any]:
    if not ctx.of("client"):
        return _result("not_applicable", "No public clients on the diagram.")
    waf = [n.id for n in ctx.nodes.values() if _WAF.search(_role(n)) or _kind(n) == "security.waf"]
    if waf:
        return _result("pass", f"Public traffic is filtered by {_names(ctx, waf)}.", waf)
    edge = [n.id for n in ctx.of("gateway") if _EDGE_WAF.search(_role(n))]
    fix = ("Put a web application firewall (AWS WAF, Azure WAF, Cloud Armor or Cloudflare WAF) in front of the public "
           "gateway, with managed OWASP rules and rate limits.")
    if edge:
        return _result("needs_evidence", f"{_names(ctx, edge)} can filter traffic: confirm the WAF rules are on.",
                       edge, fix)
    return _result("fail", "No web application firewall in front of the public entry points.",
                   [n.id for n in ctx.of("gateway")] or _entries(ctx), fix)


def check_network_segmentation(ctx: Context) -> dict[str, Any]:
    data = {n.id for n in ctx.of("database", "cache", "queue", "storage")}
    if not data:
        return _result("not_applicable", "No data stores on the diagram.")
    outside = {n.id for n in ctx.of("client", "external")}
    exposed = [e for e in ctx.edges if (e.source in outside and e.target in data) or (e.target in outside and e.source in data)]
    hard = [e for e in exposed if ctx.nodes[e.target if e.target in data else e.source].type != "storage"]
    fix = ("Keep data stores in private subnets reachable only from your services; for uploads and downloads use "
           "short-lived signed URLs.")
    if hard:
        ids = [x for e in hard for x in (e.source, e.target)]
        return _result("fail", "Data stores are reachable from outside: " + ", ".join(
            f"{ctx.nodes[e.source].name} → {ctx.nodes[e.target].name}" for e in hard[:MAX_LISTED]) + ".", ids, fix)
    if exposed:
        ids = [x for e in exposed for x in (e.source, e.target)]
        return _result("needs_evidence", "Clients reach object storage directly: confirm it uses signed URLs and no "
                       "public buckets.", ids, fix)
    return _result("pass", "No data store is reachable from clients or third parties directly.")


def check_audit_logging(ctx: Context) -> dict[str, Any]:
    if not ctx.of("service", "agent", "database", "storage", "gateway"):
        return _result("not_applicable", "No services yet.")
    audit = [n.id for n in ctx.nodes.values() if _AUDIT.search(_role(n)) and n.type != "client"]
    if audit:
        return _result("pass", f"Security-relevant actions are recorded in {_names(ctx, audit)}.", audit)
    fix = (f"Add an append-only audit log ({_tech(ctx, 'audit_log')}) that records sign-ins, permission changes and "
           "access to sensitive data, kept apart from application logs.")
    planned = ctx.stated(_AUDIT_STMT)
    monitor = [n.id for n in ctx.nodes.values() if _MONITOR.search(_role(n))]
    if planned or monitor:
        why = f"audit logging is planned ({_cite(planned)})" if planned else f"{_names(ctx, monitor)} collects logs"
        return _result("needs_evidence", f"No audit log on the diagram, but {why}.", monitor, fix, planned)
    return _result("fail", "No audit log.", [], fix)


def check_monitoring(ctx: Context) -> dict[str, Any]:
    if not ctx.of("service", "agent", "gateway"):
        return _result("not_applicable", "No services yet.")
    mon = [n.id for n in ctx.nodes.values() if _MONITOR.search(_role(n)) or _kind(n) == "ops.monitoring"]
    if mon:
        return _result("pass", f"Systems are monitored by {_names(ctx, mon)}.", mon)
    return _result("fail", "No monitoring or alerting on the diagram.", [],
                   f"Add monitoring with alerts ({_tech(ctx, 'observability')}) for errors, latency and suspicious "
                   "activity.")


def check_backups(ctx: Context) -> dict[str, Any]:
    dbs = ctx.of("database")
    if not dbs:
        return _result("not_applicable", "No databases on the diagram.")
    backup_nodes = [n.id for n in ctx.nodes.values() if _BACKUP_NODE.search(_role(n))]
    if backup_nodes:
        return _result("pass", f"Backups are handled by {_names(ctx, backup_nodes)}.", backup_nodes)
    missing = [n for n in dbs if not (_BACKUP_NOTE.search(n.description)
                                      or any("backup" in k.lower() and not _falsy(v) for k, v in n.props.items()))]
    if not missing:
        return _result("pass", "Every database notes its backups.")
    ids = [n.id for n in missing]
    fix = (f"Turn on automated backups with point-in-time recovery for {_names(ctx, ids)}, copy them to another region "
           "or account, test a restore regularly, and note it on each database (property backup: \"daily, 35 days\").")
    planned = ctx.stated(_BACKUP_STMT)
    if planned or all(_managed(n) for n in missing):
        why = f"backups are planned ({_cite(planned)})" if planned else "managed databases back up automatically"
        return _result("needs_evidence", f"{why[0].upper()}{why[1:]}, but the backup schedule and retention are not "
                       f"noted on {_names(ctx, ids)}.", ids, fix, planned)
    return _result("fail", f"No backups for {_names(ctx, ids)}.", ids, fix)


def check_resilience(ctx: Context) -> dict[str, Any]:
    dbs = ctx.of("database")
    if not dbs:
        return _result("not_applicable", "No databases on the diagram.")
    single = [n.id for n in dbs if not (_REPLICATED.search(_full(n)))]
    if not single:
        return _result("pass", "Every database is replicated or can fail over.")
    planned = ctx.stated(_AVAIL_STMT)
    note = f" An availability target is stated ({_cite(planned)})." if planned else ""
    return _result("needs_evidence", f"Replication or failover is not noted on {_names(ctx, single)}.{note}", single,
                   f"Run {_names(ctx, single)} across availability zones with automatic failover, and state the "
                   "recovery time (RTO) and data-loss (RPO) targets.", planned)


def check_pii_tagging(ctx: Context) -> dict[str, Any]:
    stores = _stores(ctx)
    if not stores:
        return _result("not_applicable", "No databases or storage on the diagram.")
    tagged = [n for n in stores if any(k.lower() in _CLASS_KEYS for k in n.props) or _CLASS_NOTE.search(n.description)]
    missing = [n.id for n in stores if n not in tagged]
    if not missing:
        return _result("pass", "Every data store says what kind of data it holds.")
    fix = (f"Set a data_classification property on {_names(ctx, missing)} (e.g. pii, phi, pci, internal, public), so "
           "you know where personal data lives.")
    if tagged:
        return _result("needs_evidence", f"No data classification on {_names(ctx, missing)}.", missing, fix)
    return _result("fail", f"No data store says whether it holds personal data: {_names(ctx, missing)}.", missing, fix)


def check_data_residency(ctx: Context) -> dict[str, Any]:
    stores = [n.id for n in _stores(ctx)]
    if not stores and not ctx.reqs:
        return _result("not_applicable", "No data stores or requirements yet.")
    hits = ctx.stated(_RESIDENCY) + [("component", n.name, _full(n)) for n in ctx.nodes.values()
                                     if any(k.lower() in ("region", "data_residency", "location") for k in n.props)]
    externals = [n.id for n in ctx.of("external", "llm")]
    if hits:
        extra = (f" Check that {_names(ctx, externals)} also keep data there." if externals else "")
        return _result("pass", f"Where data lives is stated ({_cite(hits)}).{extra}", stores, evidence=hits)
    fix = ("State where personal data is stored and processed (e.g. \"All personal data stays in the EU "
           "(eu-central-1)\") as a requirement or in the project memory, and set a region property on the data stores.")
    if _EU.search(ctx.text()):
        return _result("fail", "The project serves the EU, but nothing says where personal data is stored.",
                       stores + externals, fix)
    return _result("needs_evidence", "Nothing says where data is stored and processed.", stores, fix)


def check_retention(ctx: Context) -> dict[str, Any]:
    stores = [n.id for n in ctx.of("database", "storage", "queue")]
    if not stores and not ctx.reqs:
        return _result("not_applicable", "No data stores or requirements yet.")
    hits = ctx.stated(_RETENTION) + [("component", n.name, _full(n)) for n in ctx.nodes.values()
                                     if any("retention" in k.lower() or k.lower() == "ttl" for k in n.props)]
    if hits:
        return _result("pass", f"Retention is stated ({_cite(hits)}).", evidence=hits)
    return _result("fail", "No requirement says how long data is kept.", stores,
                   "Add a retention requirement per kind of data (e.g. orders 7 years, logs 1 year, chat transcripts "
                   "90 days) and a scheduled job or lifecycle rule that deletes expired data.")


def check_erasure(ctx: Context) -> dict[str, Any]:
    stores = [n.id for n in _stores(ctx)]
    if not stores:
        return _result("not_applicable", "No data stores on the diagram.")
    hits = ctx.stated(_ERASURE)
    fix = ("Add a requirement that a person's data is deleted from every store within 30 days of a request, with a "
           "deletion job that covers databases, files, caches, search indexes, backups and event logs.")
    if not hits:
        return _result("fail", "No requirement covers deleting a person's data on request.", stores, fix)
    logs = [n.id for n in ctx.nodes.values() if _EVENT_LOG.search(_role(n))]
    if logs and not ctx.stated(_SHRED):
        return _result("needs_evidence", f"Erasure is required ({_cite(hits)}), but events in {_names(ctx, logs)} "
                       "cannot be edited.", logs,
                       f"Keep personal data out of {_names(ctx, logs)} events, or encrypt it with per-person keys and "
                       "delete the key on request (crypto-shredding).", hits)
    return _result("pass", f"Erasure on request is required ({_cite(hits)}).", evidence=hits)


def check_vendor_processors(ctx: Context) -> dict[str, Any]:
    vendors = [n.id for n in ctx.nodes.values() if n.type == "external" or (n.type == "llm" and n.vendor != "oss")
               or (n.vendor == "saas" and n.type not in ("client", "note"))]
    if not vendors:
        return _result("not_applicable", "No third-party services on the diagram.")
    hits = ctx.stated(_PROCESSOR_STMT)
    if hits:
        return _result("pass", f"Agreements with third parties are covered ({_cite(hits)}).", vendors, evidence=hits)
    return _result("needs_evidence", f"Data goes to third parties: {_names(ctx, vendors)}.", vendors,
                   "Keep a list of these providers with a signed data processing agreement (GDPR), business "
                   "associate agreement (HIPAA) or PCI attestation for each, and mark this control as met.")


def check_cardholder_data(ctx: Context) -> dict[str, Any]:
    if not _PAYMENT_CTX.search(ctx.text()):
        return _result("not_applicable", "The project does not take payments.")
    stored = [n.id for n in ctx.of("database", "storage", "cache") if _CARD_STORED.search(_full(n))]
    providers = [n.id for n in ctx.nodes.values() if _PAYMENT_PROVIDER.search(_role(n)) or _kind(n) == "ops.payments"]
    if stored:
        return _result("fail", f"Card numbers may be stored in {_names(ctx, stored)}.", stored,
                       "Do not store card numbers: let the payment provider tokenize cards and keep only the token "
                       "and the last four digits.")
    if providers:
        return _result("pass", f"Cards are handled by {_names(ctx, providers)}, which keeps card numbers off your "
                       "systems.", providers)
    return _result("fail", "The project takes payments, but no payment provider is on the diagram.", [],
                   "Use a PCI DSS Level 1 provider (Stripe, Adyen, Braintree) with hosted fields or a hosted checkout, "
                   "so card numbers never reach your servers.")


def check_llm_guardrails(ctx: Context) -> dict[str, Any]:
    models = [n for n in ctx.nodes.values() if n.type == "llm" or _kind(n) == "ai.llm"]
    if not models:
        return _result("not_applicable", "No language models on the diagram.")
    guards = {n.id for n in ctx.nodes.values() if _GUARD.search(_role(n)) and n.type != "llm"}
    fix = (f"Put guardrails ({_tech(ctx, 'guardrails')}) between users and the model: block prompt injection, redact "
           "personal data in prompts and filter unsafe output.")
    if not guards:
        planned = ctx.stated(_GUARD_STMT)
        if planned:
            return _result("needs_evidence", f"Guardrails are planned ({_cite(planned)}) but not on the diagram.",
                           [n.id for n in models], fix, planned)
        return _result("fail", f"No guardrails in front of {_names(ctx, [n.id for n in models])}.",
                       [n.id for n in models], fix)

    def guarded(m: Node) -> bool:
        near = ctx.neighbours(m.id)
        return bool(near & guards) or all(bool(ctx.neighbours(c) & guards) for c in near) and bool(near)

    open_ = [m.id for m in models if not guarded(m)]
    if open_:
        return _result("needs_evidence", f"Guardrails ({_names(ctx, guards)}) are on the diagram but not connected "
                       f"to {_names(ctx, open_)}.", open_ + list(guards),
                       f"Connect {_names(ctx, guards)} to the path in front of {_names(ctx, open_)}.")
    return _result("pass", f"Model calls go through {_names(ctx, guards)}.", list(guards))


# check id → (title, function, NFR to propose when it does not pass, regex that maps requirements to it, effort)
CHECKS: dict[str, dict[str, Any]] = {
    "encryption_in_transit": {
        "title": "Encryption in transit", "run": check_encryption_in_transit, "effort": "low",
        "maps": r"\btls\b|https|in[- ]transit|\bmtls\b",
        "nfr": {"title": "Encrypt all traffic in transit", "category": "security", "priority": "high",
                "description": "Every connection, public and internal, uses TLS 1.2 or later.",
                "acceptance_criteria": ["Public endpoints accept only HTTPS / WSS with TLS 1.2+",
                                        "Service-to-service and database connections use TLS (or mTLS)",
                                        "Plain HTTP is redirected to HTTPS"]}},
    "encryption_at_rest": {
        "title": "Encryption at rest", "run": check_encryption_at_rest, "effort": "low",
        "maps": r"at[- ]rest|encrypt",
        "nfr": {"title": "Encrypt stored data at rest", "category": "security", "priority": "high",
                "description": "Every database, file store and backup is encrypted at rest with managed keys.",
                "acceptance_criteria": ["Databases, object storage and backups are encrypted with AES-256",
                                        "Keys are held in a key management service and rotated yearly"]}},
    "key_management": {
        "title": "Secrets and key management", "run": check_key_management, "effort": "low",
        "maps": r"\bkms\b|key management|secret|\bvault\b",
        "nfr": {"title": "Manage secrets and keys centrally", "category": "security", "priority": "high",
                "description": "Encryption keys and service secrets live in a managed secrets/key service.",
                "acceptance_criteria": ["No secrets in source code, images or environment files",
                                        "Keys and secrets are rotated automatically",
                                        "Access to each secret is limited to the services that need it"]}},
    "authentication": {
        "title": "Authentication at entry points", "run": check_authentication, "effort": "medium",
        "maps": r"authenticat|sign[- ]?in|log[- ]?in|\bsso\b|oauth|oidc|identity",
        "nfr": {"title": "Authenticate every request", "category": "security", "priority": "critical",
                "description": "Every entry point validates a token from the identity provider.",
                "acceptance_criteria": ["Requests without a valid token are rejected with 401",
                                        "Tokens expire within 1 hour and can be revoked"]}},
    "access_control": {
        "title": "Role-based access", "run": check_access_control, "effort": "medium", "maps": _RBAC.pattern,
        "nfr": {"title": "Role-based access control", "category": "security", "priority": "high",
                "description": "Access to features and data follows named roles with least privilege.",
                "acceptance_criteria": ["Each role and its permissions are documented",
                                        "Every API checks the caller's role before acting",
                                        "Service accounts have only the permissions they use"]}},
    "mfa": {
        "title": "Multi-factor sign-in", "run": check_mfa, "effort": "low", "maps": _MFA.pattern,
        "nfr": {"title": "Multi-factor authentication for privileged access", "category": "security",
                "priority": "high", "description": "Administrators and staff who reach sensitive data sign in with MFA.",
                "acceptance_criteria": ["Admin and support accounts cannot sign in without a second factor",
                                        "Customers can turn on MFA (TOTP or passkeys)"]}},
    "edge_protection": {
        "title": "Gateway in front of public services", "run": check_edge_protection, "effort": "medium",
        "maps": r"gateway|rate[- ]limit|throttl",
        "nfr": {"title": "Expose services only through the gateway", "category": "security", "priority": "high",
                "description": "Clients reach backend services only through the API gateway.",
                "acceptance_criteria": ["No backend service has a public address",
                                        "The gateway terminates TLS, authenticates and rate-limits requests"]}},
    "waf": {
        "title": "Web application firewall", "run": check_waf, "effort": "low", "maps": r"\bwaf\b|firewall|owasp|ddos",
        "nfr": {"title": "Web application firewall on public endpoints", "category": "security", "priority": "high",
                "description": "A WAF with managed OWASP rules filters traffic to every public endpoint.",
                "acceptance_criteria": ["OWASP Top 10 managed rules are on in blocking mode",
                                        "Rate-based rules limit abusive clients"]}},
    "network_segmentation": {
        "title": "Network segmentation", "run": check_network_segmentation, "effort": "medium",
        "maps": r"segment|private subnet|\bvpc\b|\bvnet\b|network isolation|private network",
        "nfr": {"title": "Keep data stores on a private network", "category": "security", "priority": "high",
                "description": "Databases, caches and queues are reachable only from the services that use them.",
                "acceptance_criteria": ["Data stores have no public endpoint",
                                        "Network rules allow only the owning services"]}},
    "audit_logging": {
        "title": "Audit log", "run": check_audit_logging, "effort": "medium", "maps": r"audit",
        "nfr": {"title": "Audit log of security-relevant actions", "category": "security", "priority": "high",
                "description": "Sign-ins, permission changes and access to sensitive data are recorded in an "
                               "append-only audit log.",
                "acceptance_criteria": ["Each entry records who, what, when and from where",
                                        "Audit entries cannot be changed or deleted by application users",
                                        "Audit logs are kept for at least 1 year"]}},
    "monitoring": {
        "title": "Monitoring and alerting", "run": check_monitoring, "effort": "low",
        "maps": r"monitor|alert|observab|logging|metrics",
        "nfr": {"title": "Monitoring and alerting", "category": "operability", "priority": "medium",
                "description": "Errors, latency and suspicious activity raise alerts to the on-call team.",
                "acceptance_criteria": ["Every service sends metrics, logs and traces",
                                        "Alerts reach the on-call engineer within 5 minutes"]}},
    "backups": {
        "title": "Backups", "run": check_backups, "effort": "low", "maps": _BACKUP_STMT.pattern,
        "nfr": {"title": "Back up data and test restores", "category": "reliability", "priority": "high",
                "description": "Databases are backed up automatically and restores are tested.",
                "acceptance_criteria": ["Point-in-time recovery with an RPO of 1 hour or less",
                                        "Backups are copied to a second region or account",
                                        "A restore is tested every quarter"]}},
    "resilience": {
        "title": "Database failover", "run": check_resilience, "effort": "medium", "maps": _AVAIL_STMT.pattern,
        "nfr": {"title": "Database failover", "category": "availability", "priority": "medium",
                "description": "Databases run in more than one availability zone with automatic failover.",
                "acceptance_criteria": ["Failover completes within 5 minutes (RTO)",
                                        "No committed data is lost on failover"]}},
    "pii_tagging": {
        "title": "Personal data mapped", "run": check_pii_tagging, "effort": "low",
        "maps": r"\bpii\b|personal data|\bphi\b|classif|data inventory",
        "nfr": {"title": "Record where personal data is stored", "category": "privacy", "priority": "medium",
                "description": "Every data store is classified (personal, health, card, internal or public data).",
                "acceptance_criteria": ["Each data store has a data classification",
                                        "The list of stores with personal data is reviewed every quarter"]}},
    "data_residency": {
        "title": "Data residency", "run": check_data_residency, "effort": "low", "maps": _RESIDENCY.pattern,
        "nfr": {"title": "Keep personal data in the agreed region", "category": "compliance", "priority": "high",
                "description": "Personal data is stored and processed only in the agreed region.",
                "acceptance_criteria": ["Data stores and backups are in the agreed region",
                                        "Third parties that receive personal data process it in the same region or "
                                        "under approved transfer terms"]}},
    "retention": {
        "title": "Data retention", "run": check_retention, "effort": "low", "maps": _RETENTION.pattern,
        "nfr": {"title": "Data retention periods", "category": "compliance", "priority": "medium",
                "description": "Each kind of data has a retention period, after which it is deleted automatically.",
                "acceptance_criteria": ["A retention period is documented for each kind of data",
                                        "Expired data is deleted by a scheduled job or lifecycle rule"]}},
    "erasure": {
        "title": "Deletion on request", "run": check_erasure, "effort": "medium", "maps": _ERASURE.pattern,
        "nfr": {"title": "Delete a person's data on request", "category": "privacy", "priority": "high",
                "description": "A person's data is deleted from every store on request.",
                "acceptance_criteria": ["Deletion completes within 30 days of the request",
                                        "Databases, files, caches, search indexes and event logs are covered",
                                        "The person gets a confirmation"]}},
    "vendor_processors": {
        "title": "Third-party agreements", "run": check_vendor_processors, "effort": "low",
        "maps": _PROCESSOR_STMT.pattern,
        "nfr": {"title": "Agreements with third-party processors", "category": "compliance", "priority": "medium",
                "description": "Every third party that receives personal or card data has a signed agreement.",
                "acceptance_criteria": ["A DPA (and a BAA for health data) is signed with each processor",
                                        "The processor list is reviewed every year"]}},
    "cardholder_data": {
        "title": "Card data kept off your systems", "run": check_cardholder_data, "effort": "medium",
        "maps": r"card|\bpci\b|payment",
        "nfr": {"title": "Never store card numbers", "category": "compliance", "priority": "critical",
                "description": "Card numbers are tokenized by a PCI DSS Level 1 provider and never reach our systems.",
                "acceptance_criteria": ["Checkout uses the provider's hosted fields or hosted page",
                                        "Only the token and the last four digits are stored"]}},
    "llm_guardrails": {
        "title": "AI guardrails", "run": check_llm_guardrails, "effort": "medium", "maps": _GUARD_STMT.pattern,
        "nfr": {"title": "Guardrails around AI models", "category": "security", "priority": "high",
                "description": "Prompts and model output pass through guardrails.",
                "acceptance_criteria": ["Prompt-injection attempts are blocked",
                                        "Personal data is redacted from prompts sent to third-party models",
                                        "Unsafe or off-topic output is filtered"]}},
}


# --------------------------------------------------------------------------- the report
def framework_ids(raw: str | Iterable[str] | None) -> list[str]:
    items = raw.split(",") if isinstance(raw, str) else list(raw or [])
    return list(dict.fromkeys(f.strip().lower() for f in items if f and f.strip().lower() in FRAMEWORKS))


def suggest(ctx: Context) -> list[str]:
    text = ctx.text()
    return [fid for fid, fw in FRAMEWORKS.items() if fw["detect"] is None or re.search(fw["detect"], text, re.I)]


def run_checks(ctx: Context, ids: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for cid in ids if ids is not None else CHECKS:
        spec = CHECKS[cid]
        res = spec["run"](ctx)
        pattern = re.compile(spec["maps"], re.I)
        reqs = [r.id for r in ctx.reqs.values()
                if pattern.search(" ".join([r.title, r.description, *r.acceptance_criteria]))]
        out[cid] = {"id": cid, "title": spec["title"], **res, "requirements": reqs, "effort": spec["effort"]}
    return out


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _RANK[s]) if statuses else "not_applicable"


def report(state: ProjectState, *, frameworks: Iterable[str] | None = None, memory: list[dict[str, Any]] | None = None,
           decisions: list[dict[str, Any]] | None = None, evidence: list[dict[str, Any]] | None = None,
           selected: list[str] | None = None, description: str = "") -> dict[str, Any]:
    """The compliance report: per framework a score and per control its status, components and requirements."""
    ctx = Context(state, memory, decisions, description)
    suggested = suggest(ctx)
    chosen = framework_ids(frameworks) or framework_ids(selected) or suggested or list(DEFAULT_FRAMEWORKS)
    needed = list(dict.fromkeys(c for fid in chosen for *_, checks in FRAMEWORKS[fid]["controls"] for c in checks))
    checks = run_checks(ctx, needed)
    ev = {(e["framework"], e["control_id"]): e for e in evidence or []}
    out_fws = []
    for fid in chosen:
        fw = FRAMEWORKS[fid]
        controls = []
        for cid, title, desc, check_ids in fw["controls"]:
            results = [checks[c] for c in check_ids]
            status = _worst([r["status"] for r in results])
            e = ev.get((fid, cid))
            effective = {"met": "met", "not_applicable": "not_applicable"}.get((e or {}).get("status", ""), status)
            failing = [r for r in results if r["status"] in ("fail", "needs_evidence")]
            components = list(dict.fromkeys(x for r in (failing or results) for x in r["components"]))
            controls.append({
                "id": cid, "title": title, "description": desc, "checks": check_ids, "status": status,
                "effective": effective,
                "evidence": {k: e[k] for k in ("status", "note", "updated_by", "updated_at")} if e else None,
                "summary": " ".join(r["summary"] for r in (failing or results) if r["status"] != "not_applicable")
                           or results[0]["summary"],
                "components": components,
                "component_names": [state.nodes[x].name for x in components if x in state.nodes],
                "requirements": list(dict.fromkeys(x for r in results for x in r["requirements"])),
                "fixes": [r["fix"] for r in failing if r["fix"]],
            })
        counts = Counter(c["effective"] for c in controls)
        applicable = len(controls) - counts["not_applicable"]
        done = counts["pass"] + counts["met"]
        out_fws.append({"id": fid, "name": fw["name"], "version": fw["version"], "description": fw["description"],
                        "score": round(100 * done / applicable) if applicable else None,
                        "counts": {s: counts[s] for s in ("pass", "met", "needs_evidence", "fail", "not_applicable")},
                        "controls": controls})
    return {"frameworks": out_fws, "checks": checks, "selected": chosen, "suggested": suggested,
            "available": [{"id": k, "name": v["name"], "version": v["version"], "description": v["description"]}
                          for k, v in FRAMEWORKS.items()],
            "proposals": proposals(out_fws, checks, state),
            "disclaimer": "An automated design check, not an audit or a certification: it reads the diagram, the "
                          "requirements and the project memory, and cannot see how systems are actually configured."}


def _open(fws: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [(fw, c) for fw in fws for c in fw["controls"] if c["effective"] in ("fail", "needs_evidence")]


def proposals(fws: list[dict[str, Any]], checks: dict[str, dict[str, Any]], state: ProjectState) -> list[dict[str, Any]]:
    """Requirement stubs for the open checks that no requirement covers yet (the offline proposals)."""
    titles = {r.title.strip().lower() for r in active_requirements(state).values()}
    refs: dict[str, list[str]] = {}
    for fw, c in _open(fws):
        for cid in c["checks"]:
            if checks[cid]["status"] in ("fail", "needs_evidence"):
                refs.setdefault(cid, []).append(f"{fw['name']} {c['id']}")
    out = []
    for cid, controls in refs.items():
        stub = CHECKS[cid]["nfr"]
        if checks[cid]["requirements"] or stub["title"].lower() in titles:
            continue
        out.append({"key": cid, "title": stub["title"], "description": stub["description"], "kind": "non_functional",
                    "category": stub["category"], "priority": stub["priority"],
                    "acceptance_criteria": list(stub["acceptance_criteria"]), "controls": controls})
    return out


# --------------------------------------------------------------------------- AI remediation
REVIEW_SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "summary": {"type": "string"},
    "steps": {"type": "array", "items": {"type": "object", "properties": {
        "controls": {"type": "array", "items": {"type": "string"}}, "title": {"type": "string"},
        "detail": {"type": "string"}, "components": {"type": "array", "items": {"type": "string"}},
        "effort": {"type": "string", "enum": ["low", "medium", "high"]}},
        "required": ["controls", "title", "detail", "components", "effort"]}},
    "requirements": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "description": {"type": "string"},
        "category": {"type": "string"}, "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "controls": {"type": "array", "items": {"type": "string"}}},
        "required": ["title", "description", "category", "priority", "acceptance_criteria", "controls"]}},
    "questions": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "steps", "requirements"]}


def offline_remediation(rep: dict[str, Any]) -> dict[str, Any]:
    """Remediation from the checks' own fixes (used only without a model)."""
    by_check: dict[str, dict[str, Any]] = {}
    for fw, c in _open(rep["frameworks"]):
        for cid in c["checks"]:
            chk = rep["checks"][cid]
            if chk["status"] not in ("fail", "needs_evidence") or not chk["fix"]:
                continue
            step = by_check.setdefault(cid, {"controls": [], "title": chk["title"], "detail": chk["fix"],
                                             "components": chk["components"], "effort": chk["effort"],
                                             "status": chk["status"]})
            step["controls"].append(f"{fw['name']} {c['id']}")
    steps = sorted(by_check.values(), key=lambda s: (s["status"] != "fail", -len(s["controls"])))
    for s in steps:
        s.pop("status")
    n_fail = sum(1 for _, c in _open(rep["frameworks"]) if c["effective"] == "fail")
    summary = (f"{n_fail} control(s) fail and {len(_open(rep['frameworks'])) - n_fail} need evidence."
               if steps else "No open controls.")
    return {"summary": summary, "steps": steps, "requirements": rep["proposals"], "questions": [], "engine": "offline"}


def _names_by_id(state: ProjectState) -> dict[str, str]:
    return {nid: n.name for nid, n in state.nodes.items()}


def remediate(rep: dict[str, Any], state: ProjectState, project_name: str, org_id: str | None, *,
              memory: list[dict[str, Any]] | None = None, use_ai: bool | None = None,
              version: str | None = None) -> dict[str, Any]:
    """Remediation steps and proposed NFRs: from the ``compliance.review`` agent, else from the checks."""
    use_ai = llm.available(org_id) if use_ai is None else use_ai
    open_ = _open(rep["frameworks"])
    if not open_ or not use_ai:
        return offline_remediation(rep)
    names = _names_by_id(state)
    refs = {f"{fw['name']} {c['id']}": c for fw, c in open_}
    findings = [{"control": ref, "title": c["title"], "status": c["effective"], "found": c["summary"],
                 "components": c["component_names"], "suggested_fix": " ".join(c["fixes"])}
                for ref, c in refs.items()]
    comps = [{"name": n.name, "type": n.type, "technology": n.technology, "description": n.description[:200],
              **({"properties": n.props} if n.props else {})}
             for n in state.nodes.values() if not diagram_of(state, n) and n.type != "note"]
    edges = [f"{names.get(e.source, e.source)} -> {names.get(e.target, e.target)} ({e.protocol or 'no protocol'})"
             for e in state.edges.values() if e.source in names and e.target in names]
    reqs = [{"id": r.id, "title": r.title, "kind": r.kind, "category": r.category, "description": r.description[:300]}
            for r in active_requirements(state).values()]
    try:
        raw = agents.run_json(PROMPT, REVIEW_SCHEMA, org_id=org_id, version=version,
                              question=f"How should {project_name} close its compliance gaps?",
                              variables={"project": project_name,
                                         "frameworks": ", ".join(f"{fw['name']} ({fw['version']})"
                                                                 for fw in rep["frameworks"]),
                                         "findings_json": json.dumps(findings, indent=1),
                                         "components_json": json.dumps(comps, indent=1),
                                         "connections": "\n".join(edges) or "(none)",
                                         "requirements_json": json.dumps(reqs, indent=1),
                                         "memory": "\n".join(f"- {m.get('text', '')}" for m in (memory or [])[:30])
                                                   or "(none)"})
    except (llm.LLMUnavailable, llm.LLMRefused, llm.LLMInvalidOutput) as exc:
        log.info("compliance.review fell back to the checks: %s", exc)
        return offline_remediation(rep)
    lower_refs = {r.lower(): r for r in refs}
    steps = []
    for s in raw.get("steps") or []:
        controls = [lower_refs[c.strip().lower()] for c in s.get("controls") or [] if c.strip().lower() in lower_refs]
        title = str(s.get("title") or "").strip()[:140]
        if not controls or not title:
            continue
        steps.append({"controls": controls, "title": title, "detail": str(s.get("detail") or "")[:1200],
                      "components": [nid for nid, nm in names.items()
                                     if nm in {str(x) for x in s.get("components") or []}][:MAX_LISTED],
                      "effort": s.get("effort") if s.get("effort") in ("low", "medium", "high") else "medium"})
    titles = {r.title.strip().lower() for r in active_requirements(state).values()}
    proposed = []
    for r in raw.get("requirements") or []:
        title = str(r.get("title") or "").strip()[:140]
        if not title or title.lower() in titles:
            continue
        titles.add(title.lower())
        controls = [lower_refs[c.strip().lower()] for c in r.get("controls") or [] if c.strip().lower() in lower_refs]
        criteria = [str(x).strip()[:300] for x in r.get("acceptance_criteria") or [] if str(x).strip()][:6]
        proposed.append({"key": f"ai-{len(proposed) + 1}", "title": title, "description": str(r.get("description") or "")[:800],
                         "kind": "non_functional", "category": str(r.get("category") or "compliance").lower()[:40],
                         "priority": r.get("priority") if r.get("priority") in ("low", "medium", "high", "critical")
                         else "medium", "acceptance_criteria": criteria, "controls": controls})
    if not steps:  # nothing grounded came back: the checks' own fixes are better than nothing
        return offline_remediation(rep)
    questions = [str(q)[:200] for q in (raw.get("questions") or [])[:3] if str(q).strip()]
    return {"summary": str(raw.get("summary") or "")[:600], "steps": steps[:10], "requirements": proposed[:8],
            "questions": questions, "engine": "llm"}


# --------------------------------------------------------------------------- report files
STATUS_LABEL = {"pass": "Pass", "fail": "Fail", "needs_evidence": "Needs evidence", "not_applicable": "Not applicable",
                "met": "Met by process"}


def _md(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def to_markdown(rep: dict[str, Any], project_name: str, branch: str) -> str:
    lines = [f"# {project_name}: compliance report", "",
             f"Branch `{branch}`. {rep['disclaimer']}", "", "## Summary", "",
             "| Framework | Score | Pass | Met by process | Needs evidence | Fail | Not applicable |",
             "|---|---|---|---|---|---|---|"]
    for fw in rep["frameworks"]:
        c = fw["counts"]
        score = f"{fw['score']}%" if fw["score"] is not None else "–"
        lines.append(f"| {fw['name']} ({fw['version']}) | {score} | {c['pass']} | {c['met']} | {c['needs_evidence']} | "
                     f"{c['fail']} | {c['not_applicable']} |")
    for fw in rep["frameworks"]:
        lines += ["", f"## {fw['name']}", "", fw["description"], "",
                  "| Control | Status | Finding | Components |", "|---|---|---|---|"]
        for c in fw["controls"]:
            lines.append(f"| {_md(c['id'])} {_md(c['title'])} | {STATUS_LABEL[c['effective']]} | {_md(c['summary'])} | "
                         f"{_md(', '.join(c['component_names'])) or '–'} |")
        todo = [c for c in fw["controls"] if c["effective"] in ("fail", "needs_evidence")]
        if todo:
            lines += ["", "### What to fix", ""]
            for c in todo:
                lines.append(f"- **{c['id']} {c['title']}**: {' '.join(c['fixes']) or 'Add evidence.'}")
                if c["requirements"]:
                    lines.append(f"  - Requirements: {', '.join(c['requirements'])}")
        notes = [c for c in fw["controls"] if c["evidence"]]
        if notes:
            lines += ["", "### Evidence and notes", ""]
            for c in notes:
                e = c["evidence"]
                label = STATUS_LABEL.get(e["status"], "Note") if e["status"] else "Note"
                lines.append(f"- **{c['id']}** ({label}, {e.get('updated_by') or 'unknown'}): {_md(e['note']) or '–'}")
    if rep["proposals"]:
        lines += ["", "## Proposed requirements", ""]
        for p in rep["proposals"]:
            lines.append(f"- **{p['title']}** ({p['priority']}, {p['category']}; {', '.join(p['controls'])}): "
                         f"{p['description']}")
            lines += [f"  - [ ] {a}" for a in p["acceptance_criteria"]]
    return "\n".join(lines) + "\n"


def to_html(rep: dict[str, Any], project_name: str, branch: str) -> str:
    from .exports import PRINT_CSS, markdown_to_html  # the spec export's print-ready HTML (PDF via the browser)

    title = html.escape(f"{project_name}: compliance report")
    return (f"<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>{title}</title>"
            f"<style>{PRINT_CSS}</style></head><body>\n"
            "<div class=\"toolbar\"><button onclick=\"window.print()\">Print / Save as PDF</button></div>\n"
            f"{markdown_to_html(to_markdown(rep, project_name, branch))}\n</body></html>\n")


# --------------------------------------------------------------------------- routes
class SettingsIn(BaseModel):
    frameworks: list[str] = Field(default_factory=list, max_length=len(FRAMEWORKS))


class EvidenceIn(BaseModel):
    framework: str
    control_id: str = Field(min_length=1, max_length=40)
    status: str = ""
    note: str = Field("", max_length=2000)


class ReviewIn(BaseModel):
    frameworks: list[str] = Field(default_factory=list)
    ai: bool = True


def _filename(*parts: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", "-".join(p for p in parts if p)).strip("-")[:80] or "compliance"


def register_routes(app: FastAPI, store: Any, *, authz: Any, state_or_404: Callable[[str, str], ProjectState]) -> None:
    read, edit = authz.project("read"), authz.project("edit")

    def build(pid: str, branch: str, frameworks: str | list[str] | None, project: dict[str, Any]) -> dict[str, Any]:
        state = state_or_404(pid, branch)
        settings = store.get_compliance_settings(pid)
        rep = report(state, frameworks=framework_ids(frameworks), memory=store.list_memory(pid),
                     decisions=store.list_decisions(pid), evidence=store.list_compliance_evidence(pid),
                     selected=(settings or {}).get("frameworks"), description=project.get("description") or "")
        return {**rep, "branch": branch, "saved": bool(settings)}

    @app.get("/api/projects/{pid}/compliance")
    def compliance_report(pid: str, branch: str = "main", frameworks: str = "", a=Depends(read)):
        return build(pid, branch, frameworks, a.project)

    @app.put("/api/projects/{pid}/compliance/settings")
    def compliance_settings(pid: str, body: SettingsIn, a=Depends(edit)):
        ids = framework_ids(body.frameworks)
        if not ids:
            raise HTTPException(422, f"choose at least one framework ({', '.join(FRAMEWORKS)})")
        return store.set_compliance_settings(pid, ids, a.actor)

    @app.put("/api/projects/{pid}/compliance/evidence")
    def compliance_evidence(pid: str, body: EvidenceIn, a=Depends(edit)):
        fw = FRAMEWORKS.get(body.framework)
        if not fw or body.control_id not in {c[0] for c in fw["controls"]}:
            raise HTTPException(404, f"unknown control '{body.framework} {body.control_id}'")
        if body.status not in EVIDENCE_STATUSES:
            raise HTTPException(422, "status must be 'met', 'not_applicable' or empty")
        note = body.note.strip()
        if not body.status and not note:
            store.delete_compliance_evidence(pid, body.framework, body.control_id)
            return {"framework": body.framework, "control_id": body.control_id, "deleted": True}
        return store.set_compliance_evidence(pid, body.framework, body.control_id, status=body.status, note=note,
                                             user=a.actor)

    @app.post("/api/projects/{pid}/compliance/review")
    async def compliance_review(pid: str, body: ReviewIn, branch: str = "main", a=Depends(read)):
        rep = build(pid, branch, body.frameworks, a.project)
        state = state_or_404(pid, branch)
        memory = store.list_memory(pid)
        return await asyncio.to_thread(remediate, rep, state, a.project["name"], a.org_id, memory=memory,
                                       use_ai=body.ai and llm.available(a.org_id))

    def file(content: str, media: str, name: str, inline: bool = False) -> Response:
        return Response(content, media_type=media, headers={
            "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
            "Cache-Control": "no-store"})

    @app.get("/api/projects/{pid}/compliance.md")
    def compliance_md(pid: str, branch: str = "main", frameworks: str = "", a=Depends(read)):
        rep = build(pid, branch, frameworks, a.project)
        return file(to_markdown(rep, a.project["name"], branch), "text/markdown; charset=utf-8",
                    _filename(a.project["name"], branch, "compliance") + ".md")

    @app.get("/api/projects/{pid}/compliance.json")
    def compliance_json(pid: str, branch: str = "main", frameworks: str = "", a=Depends(read)):
        rep = build(pid, branch, frameworks, a.project)
        doc = {"schema": "archflow.compliance", "schema_version": 1, "project": {"id": pid, "name": a.project["name"]},
               **rep}
        return file(json.dumps(doc, indent=2), "application/json",
                    _filename(a.project["name"], branch, "compliance") + ".json")

    @app.get("/api/projects/{pid}/compliance.html")
    def compliance_html(pid: str, branch: str = "main", frameworks: str = "", inline: bool = False, a=Depends(read)):
        rep = build(pid, branch, frameworks, a.project)
        return file(to_html(rep, a.project["name"], branch), "text/html; charset=utf-8",
                    _filename(a.project["name"], branch, "compliance") + ".html", inline)
