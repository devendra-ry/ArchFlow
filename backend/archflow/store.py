"""Persistence for orgs, accounts, projects, branches, versions, decisions, conversations, OpenSpec and AI usage.

One ``Store`` over SQLAlchemy 2.0 ORM models (``db/models.py``) for both databases:

* SQLite (``Store("path.db")``, ``Store(":memory:")``): the zero-setup default. One shared connection; every
  statement runs under ``self.lock``, so read-modify-write cycles are serialized in-process.
* Postgres (``Store("postgresql://...")``, selected by ``DATABASE_URL``): a connection pool; ``transaction``
  locks the branch row (``SELECT ... FOR UPDATE``) so it is safe across server processes.

The schema is created and upgraded by Alembic on startup (``db/migrate.py``). Methods return plain dicts (and
``ProjectState``) so callers do not depend on the ORM. Each branch holds its working state as JSON; versions are
immutable snapshots. People are accounts (``users``) with browser ``sessions``; org access is a ``memberships``
row (org, user, role), see users.py / authz.py.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import URL, Engine, case, create_engine, delete, event, func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .db import migrate
from .db.models import (Activity, AITrace, AIUsage, ApiToken, Approval, Base, Branch, CodeGraph, Comment, Conversation, ConversationMessage,
                        Decision, Invite, Membership, Notification, OAuthClient, OAuthCode, OpenSpecChange, OpenSpecFile, Org, OrgAIBudget,
                        OrgApprovalPolicy, OrgAIConfig, OrgAIKey, Project, ProjectMemory, RequirementRevision, User, UserSession, Version,
                        YDoc, ExternalLink, OrgJiraConnection, KbChunk, KbDocument, OrgIntegration,
                        ComplianceEvidence, ComplianceSettings)
from .db.types import now
from .models import ProjectState

__all__ = ["Store", "default_store", "DEFAULT_ORG_ID", "DECISION_STATUSES", "CONVERSATION_KINDS", "MESSAGE_MODES",
           "MEMORY_CATEGORIES", "OPENSPEC_STATUSES", "COMMENT_TARGETS", "now"]

DEFAULT_ORG_ID = "org-default"
DEFAULT_ORG_NAME = "Default"

# ---- validation shared with the API layers ----
DECISION_STATUSES = ("proposed", "accepted", "rejected", "superseded")
DECISION_FIELDS = ("title", "status", "context", "decision", "consequences", "alternatives", "chosen",
                   "requirement_ids", "node_ids", "evaluation")
DECISION_COLS = ("id", "title", "status", "context", "decision", "consequences", "alternatives", "chosen",
                 "requirement_ids", "node_ids", "evaluation", "author", "created_at", "updated_at")
CODE_GRAPH_COLS = ("id", "project_id", "source", "repo_url", "commit_sha", "created_at", "created_by", "status",
                   "summary")
CONVERSATION_KINDS = ("assistant", "design")
MESSAGE_MODES = ("ask", "command", "design", "note")
MEMORY_CATEGORIES = ("constraint", "preference", "decision", "requirement", "context", "fact")
CONVERSATION_COLS = ("id", "project_id", "branch", "kind", "title", "summary", "summarized_seq", "created_by",
                     "created_by_id", "created_at", "updated_at")
CONVERSATION_FIELDS = ("title", "summary", "summarized_seq", "branch")
MESSAGE_COLS = ("seq", "role", "mode", "content", "data", "author", "created_at")
MEMORY_COLS = ("id", "text", "category", "pinned", "source", "conversation_id", "created_by", "created_at", "updated_at")
MEMORY_FIELDS = ("text", "category", "pinned")
OPENSPEC_STATUSES = ("draft", "valid", "invalid", "archived")
OPENSPEC_CHANGE_COLS = ("change_id", "title", "status", "engine", "branch", "scope", "validation", "archived_as",
                        "created_by", "created_at", "updated_at")
OPENSPEC_CHANGE_FIELDS = ("title", "status", "engine", "branch", "scope", "validation", "archived_as")
AI_USAGE_FIELDS = ("org_id", "project_id", "feature", "model", "prompt_tokens", "completion_tokens", "cost_usd",
                   "cost_source", "latency_ms", "prompt", "user_id")
AI_TRACE_FILTERS = ("project_id", "feature", "prompt", "status", "user_id", "parent_id")
# The trace list leaves out the heavy columns (input, output, spans).
AI_TRACE_LIST_COLS = ("id", "org_id", "project_id", "parent_id", "user_id", "user_name", "feature", "prompt", "agent",
                      "status", "error", "question", "answer", "models", "prompt_tokens", "completion_tokens",
                      "cost_usd", "latency_ms", "model_calls", "tool_calls", "created_at")
COMMENT_TARGETS = ("project", "node", "decision")
COMMENT_COLS = ("id", "project_id", "branch", "target_type", "target_id", "parent_id", "author_id", "author_name",
                "body", "mentions", "resolved", "resolved_by", "created_at", "edited_at")
NOTIFICATION_COLS = ("id", "project_id", "branch", "comment_id", "thread_id", "version", "kind", "actor_name", "preview",
                     "read", "created_at")
APPROVAL_ACTIONS = ("request", "approve", "request_changes")
APPROVAL_COLS = ("id", "version", "branch", "action", "actor_id", "actor_name", "comment", "created_at")
USER_COLS = ("id", "email", "name", "created_at", "last_login_at")
INVITE_COLS = ("id", "org_id", "email", "role", "created_by", "created_at", "expires_at", "accepted_at",
               "accepted_by", "revoked")
PROJECT_COLS = ("id", "name", "description", "created_at", "org_id")
EXTERNAL_LINK_COLS = ("object_kind", "object_id", "external_key", "external_url", "content_hash", "status",
                      "status_category", "status_checked_at", "updated_at")
JIRA_CONNECTION_COLS = ("org_id", "site_url", "email", "ciphertext", "last4", "project_key", "webhook_ciphertext",
                        "webhook_created_at", "updated_by", "updated_at")
INTEGRATION_COLS = ("org_id", "kind", "config", "ciphertext", "last4", "updated_by", "updated_at")
COMPLIANCE_EVIDENCE_COLS = ("framework", "control_id", "status", "note", "updated_by", "updated_at")
KB_DOCUMENT_COLS = ("id", "project_id", "name", "type", "origin", "chars", "chunks", "embedding_model", "created_by",
                    "created_at")
KB_CHUNK_COLS = ("id", "document_id", "ord", "section", "page", "locator", "text", "embedding")
TOKEN_COLS = ("id", "kind", "org_id", "user_name", "user_id", "scopes", "client_id", "name", "resource",
              "created_at", "expires_at", "last_used_at", "revoked")


def code_graph_id() -> str:
    return f"cg-{secrets.token_hex(6)}"


def conversation_id() -> str:
    return f"cv-{secrets.token_hex(6)}"


def memory_id() -> str:
    return f"mem-{secrets.token_hex(5)}"


def comment_id() -> str:
    return f"cm-{secrets.token_hex(6)}"


def decision_id(seq: int) -> str:
    return f"ADR-{seq:03d}"


def check_memory_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Keep only updatable memory fields and validate them; raises ValueError."""
    out = {k: v for k, v in fields.items() if k in MEMORY_FIELDS and v is not None}
    if "text" in out:
        out["text"] = str(out["text"]).strip()
        if not out["text"]:
            raise ValueError("text must not be empty")
    if "category" in out and out["category"] not in MEMORY_CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(MEMORY_CATEGORIES)}")
    if "pinned" in out:
        out["pinned"] = bool(out["pinned"])
    return out


def check_decision_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Keep only updatable decision fields and validate status; raises ValueError."""
    out = {k: v for k, v in fields.items() if k in DECISION_FIELDS}
    if "status" in out and out["status"] not in DECISION_STATUSES:
        raise ValueError(f"status must be one of {', '.join(DECISION_STATUSES)}")
    if "title" in out and not str(out["title"] or "").strip():
        raise ValueError("title must not be empty")
    for k in ("alternatives", "requirement_ids", "node_ids"):
        if k in out and out[k] is None:
            out[k] = []
    return out


_delete = delete  # write_openspec_files has a ``delete`` parameter


def _engine(url: str, pool_size: int) -> Engine:
    if url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
        return create_engine(url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True)
    path = url.removeprefix("sqlite:///")
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(URL.create("sqlite", database=path), poolclass=StaticPool,
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys = ON")

    return engine


class Store:
    """``Store(url_or_path)``: a SQLite path, ``":memory:"``, or a ``postgresql://`` URL."""

    def __init__(self, url: str | Path, max_size: int = 10):
        self.url = str(url)
        self.engine = _engine(self.url, max_size)
        self.dialect = self.engine.dialect.name
        self.is_pg = self.dialect == "postgresql"
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        # SQLite has one shared connection: serialize every statement. Postgres only serializes transaction()
        # within a process; the row lock covers other processes.
        self.lock = threading.RLock()
        self._local = threading.local()  # .session = the active transaction() session on this thread
        migrate.upgrade(self.engine)
        with self._s() as s:
            if s.get(Org, DEFAULT_ORG_ID) is None:
                s.add(Org(id=DEFAULT_ORG_ID, name=DEFAULT_ORG_NAME, created_at=now()))

    def close(self) -> None:
        self.engine.dispose()

    # ---- sessions -----------------------------------------------------------
    @contextmanager
    def _s(self) -> Iterator[Session]:
        """The active transaction's session on this thread, else a new one committed on exit."""
        active = getattr(self._local, "session", None)
        if active is not None:
            yield active
            return
        with (nullcontext() if self.is_pg else self.lock), self.Session.begin() as s:
            yield s

    def _upsert(self, s: Session, model: type[Base], values: dict[str, Any], keys: list[str],
                update_cols: list[str] | None = None, where: Any = None) -> None:
        stmt = (postgresql.insert if self.is_pg else sqlite.insert)(model).values(**values)
        cols = update_cols if update_cols is not None else [k for k in values if k not in keys]
        if cols:
            stmt = stmt.on_conflict_do_update(index_elements=keys, set_={c: stmt.excluded[c] for c in cols},
                                              where=where(stmt) if where else None)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=keys)
        s.execute(stmt)

    def _lock_row(self, s: Session, stmt: Any) -> Any:
        return s.execute(stmt.with_for_update() if self.is_pg else stmt).first()

    @contextmanager
    def transaction(self, pid: str, branch: str) -> Iterator[ProjectState | None]:
        """Serialized read-modify-write on one branch; yields its current state (None if missing).

        Store calls on this thread inside the block share one transaction: committed on normal exit, rolled back
        on exception. On Postgres the branch row is locked (SELECT ... FOR UPDATE), so this is safe across
        server processes."""
        if getattr(self._local, "session", None) is not None:
            raise RuntimeError("Store.transaction() is not re-entrant")
        with self.lock, self.Session.begin() as s:
            row = self._lock_row(s, select(Branch.state_json).where(Branch.project_id == pid, Branch.name == branch))
            self._local.session = s
            try:
                yield row[0] if row else None
            finally:
                self._local.session = None

    # ---- orgs -----------------------------------------------------------
    def create_org(self, name: str) -> dict[str, Any]:
        oid = f"org-{secrets.token_hex(4)}"
        with self._s() as s:
            s.add(Org(id=oid, name=name, created_at=now()))
        return self.get_org(oid)

    def list_orgs(self) -> list[dict[str, Any]]:
        with self._s() as s:
            return [o.to_dict() for o in s.scalars(select(Org).order_by(Org.created_at, Org.id))]

    def get_org(self, org_id: str) -> dict[str, Any] | None:
        with self._s() as s:
            o = s.get(Org, org_id)
            return o.to_dict() if o else None

    def update_org(self, org_id: str, name: str) -> dict[str, Any] | None:
        with self._s() as s:
            s.execute(update(Org).where(Org.id == org_id).values(name=name))
        return self.get_org(org_id)

    # ---- memberships ------------------------------------------------------
    _MEMBER = (Membership.org_id, Membership.user_id, User.email, User.name, Membership.role, Membership.created_at)

    def add_member(self, org_id: str, user_id: str, role: str) -> dict[str, Any]:
        """Add a member or change their role (upsert)."""
        with self._s() as s:
            self._upsert(s, Membership, {"org_id": org_id, "user_id": user_id, "role": role, "created_at": now()},
                         ["org_id", "user_id"], ["role"])
        return self.get_member(org_id, user_id)

    def get_member(self, org_id: str, user_id: str) -> dict[str, Any] | None:
        with self._s() as s:
            row = s.execute(select(*self._MEMBER).join(User, User.id == Membership.user_id)
                            .where(Membership.org_id == org_id, Membership.user_id == user_id)).first()
            return dict(row._mapping) if row else None

    def get_role(self, org_id: str, user_id: str) -> str | None:
        with self._s() as s:
            m = s.get(Membership, (org_id, user_id))
            return m.role if m else None

    def list_members(self, org_id: str) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.execute(select(*self._MEMBER).join(User, User.id == Membership.user_id)
                             .where(Membership.org_id == org_id).order_by(Membership.created_at, User.email))
            return [dict(r._mapping) for r in rows]

    def remove_member(self, org_id: str, user_id: str) -> bool:
        with self._s() as s:
            return s.execute(delete(Membership).where(Membership.org_id == org_id,
                                                      Membership.user_id == user_id)).rowcount > 0

    def count_role(self, org_id: str, role: str) -> int:
        with self._s() as s:
            return s.scalar(select(func.count()).select_from(Membership)
                            .where(Membership.org_id == org_id, Membership.role == role)) or 0

    def list_user_orgs(self, user_id: str) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.execute(select(Org.id, Org.name, Org.created_at, Membership.role)
                             .join(Membership, Membership.org_id == Org.id).where(Membership.user_id == user_id)
                             .order_by(Org.created_at, Org.id))
            return [dict(r._mapping) for r in rows]

    # ---- users and sessions -------------------------------------------------
    def count_users(self) -> int:
        with self._s() as s:
            return s.scalar(select(func.count()).select_from(User)) or 0

    def create_user(self, email: str, name: str, password_hash: str) -> dict[str, Any]:
        uid = f"u-{secrets.token_hex(6)}"
        with self._s() as s:
            s.add(User(id=uid, email=email, name=name, password_hash=password_hash, created_at=now()))
        return self.get_user(uid)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._s() as s:
            u = s.get(User, user_id)
            return u.to_dict(*USER_COLS) if u else None

    def get_user_by_email(self, email: str, with_hash: bool = False) -> dict[str, Any] | None:
        with self._s() as s:
            u = s.scalars(select(User).where(User.email == email)).first()
            return u.to_dict(*USER_COLS, *(["password_hash"] if with_hash else [])) if u else None

    def touch_login(self, user_id: str) -> None:
        with self._s() as s:
            s.execute(update(User).where(User.id == user_id).values(last_login_at=now()))

    def create_session(self, token_hash: str, user_id: str, expires_at: float, user_agent: str = "") -> None:
        with self._s() as s:
            s.add(UserSession(token_hash=token_hash, user_id=user_id, created_at=now(), expires_at=expires_at,
                              user_agent=user_agent[:300]))

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        """Active session (not revoked, not expired)."""
        with self._s() as s:
            u = s.scalars(select(UserSession).where(UserSession.token_hash == token_hash, UserSession.revoked.is_(False),
                                                    UserSession.expires_at >= time.time())).first()
            return u.to_dict("token_hash", "user_id", "created_at", "expires_at", "user_agent") if u else None

    def extend_session(self, token_hash: str, expires_at: float) -> None:
        with self._s() as s:
            s.execute(update(UserSession).where(UserSession.token_hash == token_hash).values(expires_at=expires_at))

    def revoke_session(self, token_hash: str) -> None:
        with self._s() as s:
            s.execute(update(UserSession).where(UserSession.token_hash == token_hash).values(revoked=True))

    # ---- invites --------------------------------------------------------------
    def create_invite(self, org_id: str, token_hash: str, role: str, expires_at: float, email: str | None = None,
                      created_by: str | None = None) -> dict[str, Any]:
        iid = f"inv-{secrets.token_hex(6)}"
        with self._s() as s:
            inv = Invite(id=iid, org_id=org_id, token_hash=token_hash, email=email, role=role, created_by=created_by,
                         created_at=now(), expires_at=expires_at, revoked=False)
            s.add(inv)
            s.flush()
            return s.get(Invite, iid).to_dict(*INVITE_COLS)

    def _usable_invites(self):
        return select(Invite).where(Invite.revoked.is_(False), Invite.accepted_at.is_(None),
                                    Invite.expires_at >= time.time())

    def get_invite(self, token_hash: str) -> dict[str, Any] | None:
        """Usable invite by hash (not revoked, not accepted, not expired)."""
        with self._s() as s:
            inv = s.scalars(self._usable_invites().where(Invite.token_hash == token_hash)).first()
            return inv.to_dict(*INVITE_COLS) if inv else None

    def list_invites(self, org_id: str) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.scalars(self._usable_invites().where(Invite.org_id == org_id).order_by(Invite.created_at.desc()))
            return [i.to_dict(*INVITE_COLS) for i in rows]

    def revoke_invite(self, org_id: str, invite_id: str) -> bool:
        with self._s() as s:
            return s.execute(update(Invite).where(Invite.id == invite_id, Invite.org_id == org_id,
                                                  Invite.revoked.is_(False), Invite.accepted_at.is_(None))
                             .values(revoked=True)).rowcount > 0

    def accept_invite(self, invite_id: str, user_id: str) -> bool:
        """Mark an invite used (single use, atomic). False if it was already used, revoked or expired."""
        with self._s() as s:
            return s.execute(update(Invite).where(Invite.id == invite_id, Invite.accepted_at.is_(None),
                                                  Invite.revoked.is_(False), Invite.expires_at >= time.time())
                             .values(accepted_at=now(), accepted_by=user_id)).rowcount > 0

    # ---- per-org AI provider keys (encrypted by the caller, see secrets_box.py) ----------
    def set_ai_key(self, org_id: str, provider: str, ciphertext: str, last4: str, user_id: str | None) -> None:
        with self._s() as s:
            self._upsert(s, OrgAIKey, {"org_id": org_id, "provider": provider, "ciphertext": ciphertext,
                                       "last4": last4, "updated_by": user_id, "updated_at": now()},
                         ["org_id", "provider"])

    def delete_ai_key(self, org_id: str, provider: str) -> None:
        with self._s() as s:
            s.execute(delete(OrgAIKey).where(OrgAIKey.org_id == org_id, OrgAIKey.provider == provider))

    def list_ai_keys(self, org_id: str) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.scalars(select(OrgAIKey).where(OrgAIKey.org_id == org_id).order_by(OrgAIKey.provider))
            return [k.to_dict("provider", "ciphertext", "last4", "updated_by", "updated_at") for k in rows]

    # ---- per-org Jira connection (token encrypted by the caller, see jira.py) ----------
    def set_jira_connection(self, org_id: str, *, site_url: str, email: str, ciphertext: str, last4: str,
                            project_key: str, user_id: str | None) -> None:
        with self._s() as s:
            self._upsert(s, OrgJiraConnection, {"org_id": org_id, "site_url": site_url, "email": email,
                                                "ciphertext": ciphertext, "last4": last4, "project_key": project_key,
                                                "updated_by": user_id, "updated_at": now()}, ["org_id"])

    def get_jira_connection(self, org_id: str) -> dict[str, Any] | None:
        with self._s() as s:
            c = s.get(OrgJiraConnection, org_id)
            return c.to_dict(*JIRA_CONNECTION_COLS) if c else None

    def delete_jira_connection(self, org_id: str) -> None:
        with self._s() as s:
            s.execute(delete(OrgJiraConnection).where(OrgJiraConnection.org_id == org_id))

    def set_jira_webhook(self, org_id: str, ciphertext: str) -> bool:
        """Store (or with "" remove) the org's encrypted Jira webhook secret. False when Jira is not connected."""
        with self._s() as s:
            return s.execute(update(OrgJiraConnection).where(OrgJiraConnection.org_id == org_id)
                             .values(webhook_ciphertext=ciphertext,
                                     webhook_created_at=now() if ciphertext else None)).rowcount > 0

    # ---- org integrations (GitHub, Confluence, Slack, Teams) ------------------------
    def set_integration(self, org_id: str, kind: str, *, config: dict[str, Any], ciphertext: str, last4: str,
                        user_id: str | None) -> None:
        with self._s() as s:
            self._upsert(s, OrgIntegration, {"org_id": org_id, "kind": kind, "config": config, "ciphertext": ciphertext,
                                             "last4": last4, "updated_by": user_id, "updated_at": now()},
                         ["org_id", "kind"])

    def get_integration(self, org_id: str, kind: str) -> dict[str, Any] | None:
        with self._s() as s:
            c = s.get(OrgIntegration, (org_id, kind))
            return c.to_dict(*INTEGRATION_COLS) if c else None

    def list_integrations(self, org_id: str) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.scalars(select(OrgIntegration).where(OrgIntegration.org_id == org_id).order_by(OrgIntegration.kind))
            return [x.to_dict(*INTEGRATION_COLS) for x in rows]

    def delete_integration(self, org_id: str, kind: str) -> None:
        with self._s() as s:
            s.execute(delete(OrgIntegration).where(OrgIntegration.org_id == org_id, OrgIntegration.kind == kind))

    # ---- knowledge base (documents and chunks) ---------------------------------------
    def add_kb_document(self, pid: str, *, name: str, type: str, origin: str, chars: int, embedding_model: str,
                        created_by: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        """Store a document and its chunks ({section, page, locator, text, embedding}) in one transaction."""
        did = f"kb-{secrets.token_hex(6)}"
        with self._s() as s:
            s.add(KbDocument(id=did, project_id=pid, name=name, type=type, origin=origin, chars=chars,
                             chunks=len(chunks), embedding_model=embedding_model, created_by=created_by,
                             created_at=now()))
            s.flush()
            for i, c in enumerate(chunks):
                s.add(KbChunk(document_id=did, project_id=pid, ord=i, section=c.get("section") or "",
                              page=c.get("page"), locator=c.get("locator") or "", text=c["text"],
                              embedding=c.get("embedding")))
            s.flush()
            return s.get(KbDocument, did).to_dict(*KB_DOCUMENT_COLS)

    def list_kb_documents(self, pid: str) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.scalars(select(KbDocument).where(KbDocument.project_id == pid)
                             .order_by(KbDocument.created_at.desc(), KbDocument.id))
            return [x.to_dict(*KB_DOCUMENT_COLS) for x in rows]

    def get_kb_document(self, pid: str, did: str) -> dict[str, Any] | None:
        with self._s() as s:
            d = s.get(KbDocument, did)
            return d.to_dict(*KB_DOCUMENT_COLS) if d and d.project_id == pid else None

    def delete_kb_document(self, pid: str, did: str) -> bool:
        with self._s() as s:
            s.execute(delete(KbChunk).where(KbChunk.document_id == did, KbChunk.project_id == pid))
            return bool(s.execute(delete(KbDocument).where(KbDocument.id == did, KbDocument.project_id == pid)).rowcount)

    def list_kb_chunks(self, pid: str, document_id: str | None = None) -> list[dict[str, Any]]:
        """Every chunk of the project (or one document), with its document's name and type."""
        with self._s() as s:
            q = (select(KbChunk, KbDocument.name, KbDocument.type).join(KbDocument, KbDocument.id == KbChunk.document_id)
                 .where(KbChunk.project_id == pid))
            if document_id:
                q = q.where(KbChunk.document_id == document_id)
            rows = s.execute(q.order_by(KbChunk.document_id, KbChunk.ord))
            return [{**c.to_dict(*KB_CHUNK_COLS), "document": name, "type": typ} for c, name, typ in rows]

    def set_kb_embeddings(self, did: str, model: str, vectors: dict[int, list[float]]) -> None:
        """Replace a document's embeddings ({chunk id: vector}) and record the model that made them."""
        with self._s() as s:
            for cid, vec in vectors.items():
                s.execute(update(KbChunk).where(KbChunk.id == cid, KbChunk.document_id == did).values(embedding=vec))
            s.execute(update(KbDocument).where(KbDocument.id == did).values(embedding_model=model))

    # ---- compliance frameworks (settings and evidence per control) -----------------
    def get_compliance_settings(self, pid: str) -> dict[str, Any] | None:
        with self._s() as s:
            c = s.get(ComplianceSettings, pid)
            return c.to_dict("frameworks", "updated_by", "updated_at") if c else None

    def set_compliance_settings(self, pid: str, frameworks: list[str], user: str | None) -> dict[str, Any]:
        with self._s() as s:
            self._upsert(s, ComplianceSettings, {"project_id": pid, "frameworks": list(frameworks), "updated_by": user,
                                                 "updated_at": now()}, ["project_id"])
        return self.get_compliance_settings(pid)

    def list_compliance_evidence(self, pid: str) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.scalars(select(ComplianceEvidence).where(ComplianceEvidence.project_id == pid)
                             .order_by(ComplianceEvidence.framework, ComplianceEvidence.control_id))
            return [x.to_dict(*COMPLIANCE_EVIDENCE_COLS) for x in rows]

    def set_compliance_evidence(self, pid: str, framework: str, control_id: str, *, status: str, note: str,
                                user: str | None) -> dict[str, Any]:
        with self._s() as s:
            self._upsert(s, ComplianceEvidence, {"project_id": pid, "framework": framework, "control_id": control_id,
                                                 "status": status, "note": note, "updated_by": user, "updated_at": now()},
                         ["project_id", "framework", "control_id"])
            row = s.get(ComplianceEvidence, (pid, framework, control_id))
            return row.to_dict(*COMPLIANCE_EVIDENCE_COLS)

    def delete_compliance_evidence(self, pid: str, framework: str, control_id: str) -> bool:
        with self._s() as s:
            return s.execute(delete(ComplianceEvidence).where(
                ComplianceEvidence.project_id == pid, ComplianceEvidence.framework == framework,
                ComplianceEvidence.control_id == control_id)).rowcount > 0

    # ---- links to external trackers (Jira issues) ---------------------------------
    def list_external_links(self, pid: str, system: str = "jira") -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.scalars(select(ExternalLink).where(ExternalLink.project_id == pid, ExternalLink.system == system)
                             .order_by(ExternalLink.object_kind, ExternalLink.object_id))
            return [x.to_dict(*EXTERNAL_LINK_COLS) for x in rows]

    def save_external_link(self, pid: str, object_kind: str, object_id: str, *, external_key: str,
                           external_url: str = "", content_hash: str = "", system: str = "jira") -> None:
        """Insert or update the link of one object (keyed by project, system, kind and id)."""
        with self._s() as s:
            self._upsert(s, ExternalLink, {"project_id": pid, "system": system, "object_kind": object_kind,
                                           "object_id": object_id, "external_key": external_key,
                                           "external_url": external_url, "content_hash": content_hash,
                                           "updated_at": now()},
                         ["project_id", "system", "object_kind", "object_id"])

    def set_external_status(self, pid: str, external_key: str, status: str, category: str,
                            system: str = "jira") -> None:
        with self._s() as s:
            s.execute(update(ExternalLink).where(ExternalLink.project_id == pid, ExternalLink.system == system,
                                                 ExternalLink.external_key == external_key)
                      .values(status=status, status_category=category, status_checked_at=now()))

    def find_external_links_by_key(self, org_id: str, external_key: str, system: str = "jira") -> list[dict[str, Any]]:
        """Every object linked to this issue key in the org's projects (a webhook names the issue, not the project)."""
        with self._s() as s:
            rows = s.execute(select(ExternalLink.project_id, ExternalLink.object_kind, ExternalLink.object_id)
                             .join(Project, Project.id == ExternalLink.project_id)
                             .where(Project.org_id == org_id, ExternalLink.system == system,
                                    ExternalLink.external_key == external_key)
                             .order_by(ExternalLink.project_id, ExternalLink.object_kind, ExternalLink.object_id))
            return [{"project_id": p, "object_kind": k, "object_id": o} for p, k, o in rows]

    def delete_external_links_by_key(self, pid: str, external_key: str, system: str = "jira") -> int:
        with self._s() as s:
            return s.execute(delete(ExternalLink).where(ExternalLink.project_id == pid, ExternalLink.system == system,
                                                        ExternalLink.external_key == external_key)).rowcount

    def delete_external_link(self, pid: str, object_kind: str, object_id: str, system: str = "jira") -> None:
        with self._s() as s:
            s.execute(delete(ExternalLink).where(ExternalLink.project_id == pid, ExternalLink.system == system,
                                                 ExternalLink.object_kind == object_kind,
                                                 ExternalLink.object_id == object_id))

    # ---- projects -------------------------------------------------------
    def create_project(self, name: str, description: str = "", state: ProjectState | None = None,
                       org_id: str = DEFAULT_ORG_ID) -> dict[str, Any]:
        pid, ts = f"p-{secrets.token_hex(4)}", now()
        with self._s() as s:
            s.add(Project(id=pid, name=name, description=description, created_at=ts, org_id=org_id))
            s.flush()
            s.add(Branch(project_id=pid, name="main", state_json=state or ProjectState(), base_version=None,
                         created_at=ts))
        return self.get_project(pid)

    def list_projects(self, org_id: str | None = None) -> list[dict[str, Any]]:
        q = select(Project).order_by(Project.created_at.desc(), Project.id)
        if org_id is not None:
            q = q.where(Project.org_id == org_id)
        with self._s() as s:
            return [p.to_dict(*PROJECT_COLS) for p in s.scalars(q)]

    def get_project(self, pid: str) -> dict[str, Any] | None:
        with self._s() as s:
            p = s.get(Project, pid)
            return p.to_dict(*PROJECT_COLS) if p else None

    def delete_project(self, pid: str) -> None:
        with self._s() as s:
            s.execute(delete(Project).where(Project.id == pid))

    # ---- branches -------------------------------------------------------
    def list_branches(self, pid: str) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.execute(select(Branch.name, Branch.base_version, Branch.created_at)
                             .where(Branch.project_id == pid).order_by(Branch.created_at, Branch.name))
            return [dict(r._mapping) for r in rows]

    def get_state(self, pid: str, branch: str) -> ProjectState | None:
        with self._s() as s:
            return s.scalar(select(Branch.state_json).where(Branch.project_id == pid, Branch.name == branch))

    def save_state(self, pid: str, branch: str, state: ProjectState) -> None:
        with self._s() as s:
            s.execute(update(Branch).where(Branch.project_id == pid, Branch.name == branch).values(state_json=state))

    def create_branch(self, pid: str, name: str, state: ProjectState, base_version: int | None) -> None:
        with self._s() as s:
            s.add(Branch(project_id=pid, name=name, state_json=state, base_version=base_version, created_at=now()))

    # ---- versions -------------------------------------------------------
    def commit(self, pid: str, branch: str, message: str, author: str, state: ProjectState) -> dict[str, Any]:
        with self._s() as s:
            self._lock_row(s, select(Project.id).where(Project.id == pid))  # serializes version numbers
            number = (s.scalar(select(func.max(Version.number)).where(Version.project_id == pid)) or 0) + 1
            parent = s.scalar(select(func.max(Version.number)).where(Version.project_id == pid, Version.branch == branch))
            if parent is None:
                parent = s.scalar(select(Branch.base_version).where(Branch.project_id == pid, Branch.name == branch))
            s.add(Version(project_id=pid, number=number, branch=branch, message=message, author=author, parent=parent,
                          state_json=state, created_at=now()))
        return self.get_version(pid, number, with_state=False)

    def head_version(self, pid: str, branch: str) -> int | None:
        with self._s() as s:
            return s.scalar(select(func.max(Version.number)).where(Version.project_id == pid, Version.branch == branch))

    def list_versions(self, pid: str) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.execute(select(Version.number, Version.branch, Version.message, Version.author, Version.parent,
                                    Version.created_at).where(Version.project_id == pid).order_by(Version.number.desc()))
            return [dict(r._mapping) for r in rows]

    def get_version(self, pid: str, number: int, with_state: bool = True) -> dict[str, Any] | None:
        with self._s() as s:
            v = s.get(Version, (pid, number))
            if v is None:
                return None
            out = v.to_dict("project_id", "number", "branch", "message", "author", "parent", "created_at")
            if with_state:
                out["state"] = v.state_json
            return out

    # ---- activity -------------------------------------------------------
    def log_activity(self, pid: str, branch: str, actor: str, summary: list[str], rev: int) -> None:
        if not summary:
            return
        with self._s() as s:
            s.add(Activity(project_id=pid, branch=branch, actor=actor, summary=list(summary), rev=rev, created_at=now()))

    def list_activity(self, pid: str, branch: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.scalars(select(Activity).where(Activity.project_id == pid, Activity.branch == branch)
                             .order_by(Activity.id.desc()).limit(limit))
            return [a.to_dict("actor", "summary", "rev", "created_at") for a in rows]

    # ---- requirement history (see history.py) ------------------------------
    def log_requirement_revisions(self, pid: str, branch: str, actor: str, rev: int,
                                  entries: list[dict[str, Any]]) -> None:
        """``entries``: [{requirement_id, action, changes: [{field, before, after}]}]."""
        if not entries:
            return
        with self._s() as s:
            ts = now()
            s.add_all(RequirementRevision(project_id=pid, branch=branch, actor=actor, rev=rev, created_at=ts,
                                          requirement_id=e["requirement_id"], action=e["action"],
                                          changes=list(e["changes"])) for e in entries)

    def list_requirement_revisions(self, pid: str, branch: str, rid: str, limit: int = 200) -> list[dict[str, Any]]:
        """Newest first."""
        with self._s() as s:
            rows = s.scalars(select(RequirementRevision)
                             .where(RequirementRevision.project_id == pid, RequirementRevision.branch == branch,
                                    RequirementRevision.requirement_id == rid)
                             .order_by(RequirementRevision.id.desc()).limit(limit))
            return [r.to_dict("id", "requirement_id", "rev", "action", "actor", "changes", "created_at") for r in rows]

    # ---- collaborative documents (binary Yjs state) -----------------------
    def get_ydoc(self, pid: str, branch: str) -> bytes | None:
        with self._s() as s:
            data = s.scalar(select(YDoc.data).where(YDoc.project_id == pid, YDoc.branch == branch))
            return bytes(data) if data is not None else None

    def put_ydoc(self, pid: str, branch: str, data: bytes) -> None:
        with self._s() as s:
            self._upsert(s, YDoc, {"project_id": pid, "branch": branch, "data": data, "updated_at": now()},
                         ["project_id", "branch"])

    def delete_ydoc(self, pid: str, branch: str) -> None:
        with self._s() as s:
            s.execute(delete(YDoc).where(YDoc.project_id == pid, YDoc.branch == branch))

    # ---- OAuth clients, authorization codes and API tokens ----------------------
    # Secrets are never stored: codes and tokens are kept as SHA-256 hashes (see auth.py).
    def save_oauth_client(self, client_id: str, data: dict[str, Any]) -> None:
        with self._s() as s:
            self._upsert(s, OAuthClient, {"client_id": client_id, "data": data, "created_at": now()},
                         ["client_id"], ["data"])

    def get_oauth_client(self, client_id: str) -> dict[str, Any] | None:
        with self._s() as s:
            return s.scalar(select(OAuthClient.data).where(OAuthClient.client_id == client_id))

    def save_auth_code(self, code_hash: str, data: dict[str, Any], expires_at: float) -> None:
        with self._s() as s:
            s.execute(delete(OAuthCode).where(OAuthCode.expires_at < time.time()))
            self._upsert(s, OAuthCode, {"code_hash": code_hash, "data": data, "expires_at": expires_at},
                         ["code_hash"])

    def get_auth_code(self, code_hash: str) -> dict[str, Any] | None:
        with self._s() as s:
            return s.scalar(select(OAuthCode.data).where(OAuthCode.code_hash == code_hash,
                                                         OAuthCode.expires_at >= time.time()))

    def pop_auth_code(self, code_hash: str) -> dict[str, Any] | None:
        """Single use: returns the code's data and deletes it (None if missing or expired)."""
        with self._s() as s:
            row = s.execute(delete(OAuthCode).where(OAuthCode.code_hash == code_hash)
                            .returning(OAuthCode.data, OAuthCode.expires_at)).first()
        if not row or row.expires_at < time.time():
            return None
        return row.data

    def create_token(self, token_hash: str, kind: str, org_id: str, user_name: str, scopes: list[str],
                     client_id: str | None = None, name: str = "", expires_at: float | None = None,
                     resource: str | None = None, user_id: str | None = None) -> dict[str, Any]:
        tid = f"tok-{secrets.token_hex(6)}"
        with self._s() as s:
            s.add(ApiToken(id=tid, token_hash=token_hash, kind=kind, org_id=org_id, user_name=user_name,
                           user_id=user_id, scopes=list(scopes), client_id=client_id, name=name, resource=resource,
                           created_at=now(), expires_at=expires_at, revoked=False))
        return self.get_token_by_id(tid)

    def get_token(self, token_hash: str) -> dict[str, Any] | None:
        """Active token by hash (not revoked, not expired)."""
        with self._s() as s:
            t = s.scalars(select(ApiToken).where(
                ApiToken.token_hash == token_hash, ApiToken.revoked.is_(False),
                (ApiToken.expires_at.is_(None)) | (ApiToken.expires_at >= time.time()))).first()
            return t.to_dict(*TOKEN_COLS) if t else None

    def get_token_by_id(self, token_id: str) -> dict[str, Any] | None:
        with self._s() as s:
            t = s.get(ApiToken, token_id)
            return t.to_dict(*TOKEN_COLS) if t else None

    def touch_token(self, token_hash: str) -> None:
        with self._s() as s:
            s.execute(update(ApiToken).where(ApiToken.token_hash == token_hash).values(last_used_at=now()))

    def revoke_token(self, token_hash: str) -> None:
        with self._s() as s:
            s.execute(update(ApiToken).where(ApiToken.token_hash == token_hash).values(revoked=True))

    def revoke_token_id(self, token_id: str) -> bool:
        with self._s() as s:
            return s.execute(update(ApiToken).where(ApiToken.id == token_id, ApiToken.revoked.is_(False))
                             .values(revoked=True)).rowcount > 0

    def revoke_user_tokens(self, org_id: str, user_id: str) -> None:
        with self._s() as s:
            s.execute(update(ApiToken).where(ApiToken.org_id == org_id, ApiToken.user_id == user_id).values(revoked=True))

    def list_tokens(self, org_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        q = select(ApiToken).where(ApiToken.org_id == org_id, ApiToken.revoked.is_(False))
        if kind:
            q = q.where(ApiToken.kind == kind)
        with self._s() as s:
            return [t.to_dict(*TOKEN_COLS) for t in s.scalars(q.order_by(ApiToken.created_at.desc(), ApiToken.id))]

    # ---- architecture decision records ----------------------------------
    def create_decision(self, pid: str, title: str, *, author: str = "anonymous", **fields: Any) -> dict[str, Any]:
        data = check_decision_fields({"title": title, **fields})
        ts = now()
        with self._s() as s:
            self._lock_row(s, select(Project.id).where(Project.id == pid))  # serializes ADR numbers
            seq = (s.scalar(select(func.max(Decision.seq)).where(Decision.project_id == pid)) or 0) + 1
            did = decision_id(seq)
            s.add(Decision(project_id=pid, id=did, seq=seq, title=data["title"].strip(),
                           status=data.get("status") or "proposed", context=data.get("context", ""),
                           decision=data.get("decision", ""), consequences=data.get("consequences", ""),
                           alternatives=data.get("alternatives", []), chosen=data.get("chosen", ""),
                           requirement_ids=data.get("requirement_ids", []), node_ids=data.get("node_ids", []),
                           evaluation=data.get("evaluation"), author=author, created_at=ts, updated_at=ts))
        return self.get_decision(pid, did)

    def list_decisions(self, pid: str) -> list[dict[str, Any]]:
        with self._s() as s:
            return [d.to_dict(*DECISION_COLS) for d in s.scalars(select(Decision).where(Decision.project_id == pid)
                                                                 .order_by(Decision.seq))]

    def get_decision(self, pid: str, did: str) -> dict[str, Any] | None:
        with self._s() as s:
            d = s.get(Decision, (pid, did))
            return d.to_dict(*DECISION_COLS) if d else None

    def update_decision(self, pid: str, did: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Update the given fields (see DECISION_FIELDS); returns the decision, or None if it does not exist."""
        data = check_decision_fields(fields)
        if data:
            with self._s() as s:
                s.execute(update(Decision).where(Decision.project_id == pid, Decision.id == did)
                          .values(**data, updated_at=now()))
        return self.get_decision(pid, did)

    # ---- code graphs (repository analysis) ------------------------------------
    def add_code_graph(self, pid: str, *, source: str, repo_url: str | None, commit_sha: str | None,
                       created_by: str, status: str = "ready", summary: dict[str, Any] | None = None,
                       graph: bytes | None = None) -> dict[str, Any]:
        gid = code_graph_id()
        with self._s() as s:
            s.add(CodeGraph(id=gid, project_id=pid, source=source, repo_url=repo_url, commit_sha=commit_sha,
                            created_at=now(), created_by=created_by, status=status, summary=summary or {}, graph=graph))
        return self.get_code_graph(pid, gid)

    @staticmethod
    def _code_graph(g: CodeGraph | None, with_graph: bool) -> dict[str, Any] | None:
        if g is None:
            return None
        d = g.to_dict(*CODE_GRAPH_COLS)
        d["summary"] = d["summary"] or {}
        if with_graph:
            d["graph"] = bytes(g.graph) if g.graph is not None else None
        return d

    def get_code_graph(self, pid: str, gid: str, with_graph: bool = False) -> dict[str, Any] | None:
        with self._s() as s:
            g = s.scalars(select(CodeGraph).where(CodeGraph.project_id == pid, CodeGraph.id == gid)).first()
            return self._code_graph(g, with_graph)

    def list_code_graphs(self, pid: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.scalars(select(CodeGraph).where(CodeGraph.project_id == pid)
                             .order_by(CodeGraph.created_at.desc(), CodeGraph.id.desc()).limit(limit))
            return [self._code_graph(g, False) for g in rows]

    def latest_code_graph(self, pid: str, with_graph: bool = False) -> dict[str, Any] | None:
        with self._s() as s:
            g = s.scalars(select(CodeGraph).where(CodeGraph.project_id == pid, CodeGraph.status == "ready")
                          .order_by(CodeGraph.created_at.desc(), CodeGraph.id.desc()).limit(1)).first()
            return self._code_graph(g, with_graph)

    # ---- conversations (AI chat threads) -----------------------------------
    def create_conversation(self, pid: str, branch: str, kind: str, title: str, created_by: str,
                            created_by_id: str | None = None) -> dict[str, Any]:
        cid, ts = conversation_id(), now()
        with self._s() as s:
            s.add(Conversation(id=cid, project_id=pid, branch=branch, kind=kind, title=title, created_by=created_by,
                               created_by_id=created_by_id, created_at=ts, updated_at=ts))
        return self.get_conversation(cid)

    def _conversations(self):
        count = (select(func.count()).where(ConversationMessage.conversation_id == Conversation.id)
                 .correlate(Conversation).scalar_subquery())
        return select(Conversation, count.label("message_count"))

    @staticmethod
    def _conversation(row: Any) -> dict[str, Any]:
        return {**row[0].to_dict(*CONVERSATION_COLS), "message_count": row[1]}

    def get_conversation(self, cid: str) -> dict[str, Any] | None:
        with self._s() as s:
            row = s.execute(self._conversations().where(Conversation.id == cid)).first()
            return self._conversation(row) if row else None

    def list_conversations(self, pid: str, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = self._conversations().where(Conversation.project_id == pid)
        if kind:
            q = q.where(Conversation.kind == kind)
        with self._s() as s:
            rows = s.execute(q.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(limit))
            return [self._conversation(r) for r in rows]

    def update_conversation(self, cid: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        data = {k: v for k, v in fields.items() if k in CONVERSATION_FIELDS}
        if data:
            with self._s() as s:
                s.execute(update(Conversation).where(Conversation.id == cid).values(**data, updated_at=now()))
        return self.get_conversation(cid)

    def delete_conversation(self, cid: str) -> bool:
        with self._s() as s:
            return s.execute(delete(Conversation).where(Conversation.id == cid)).rowcount > 0

    def add_message(self, cid: str, role: str, mode: str, content: str, data: dict[str, Any] | None,
                    author: str) -> dict[str, Any]:
        ts = now()
        with self._s() as s:
            self._lock_row(s, select(Conversation.id).where(Conversation.id == cid))  # serializes seq numbers
            seq = (s.scalar(select(func.max(ConversationMessage.seq))
                            .where(ConversationMessage.conversation_id == cid)) or 0) + 1
            msg = ConversationMessage(conversation_id=cid, seq=seq, role=role, mode=mode, content=content,
                                      data=data or {}, author=author, created_at=ts)
            s.add(msg)
            s.execute(update(Conversation).where(Conversation.id == cid).values(updated_at=ts))
            s.flush()
            return msg.to_dict(*MESSAGE_COLS)

    def list_messages(self, cid: str, after_seq: int = 0) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == cid,
                                                               ConversationMessage.seq > after_seq)
                             .order_by(ConversationMessage.seq))
            return [{**m.to_dict(*MESSAGE_COLS), "data": m.data or {}} for m in rows]

    def delete_messages_from(self, cid: str, seq: int) -> int:
        """Delete messages with seq >= ``seq`` (regenerate, clear); the summary is reset if it covered them."""
        with self._s() as s:
            n = s.execute(delete(ConversationMessage).where(ConversationMessage.conversation_id == cid,
                                                            ConversationMessage.seq >= seq)).rowcount
            covered = Conversation.summarized_seq >= seq
            s.execute(update(Conversation).where(Conversation.id == cid).values(
                summary=case((covered, ""), else_=Conversation.summary),
                summarized_seq=case((covered, 0), else_=Conversation.summarized_seq), updated_at=now()))
        return n

    # ---- project memory (facts every AI call sees) -----------------------------
    def list_memory(self, pid: str) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.scalars(select(ProjectMemory).where(ProjectMemory.project_id == pid).order_by(
                ProjectMemory.pinned.desc(), ProjectMemory.created_at.desc(), ProjectMemory.id.desc()))
            return [m.to_dict(*MEMORY_COLS) for m in rows]

    def get_memory(self, pid: str, mid: str) -> dict[str, Any] | None:
        with self._s() as s:
            m = s.scalars(select(ProjectMemory).where(ProjectMemory.project_id == pid, ProjectMemory.id == mid)).first()
            return m.to_dict(*MEMORY_COLS) if m else None

    def add_memory(self, pid: str, text: str, *, category: str = "fact", pinned: bool = False, source: str = "user",
                   conversation_id: str | None = None, created_by: str = "anonymous") -> dict[str, Any]:
        data = check_memory_fields({"text": text, "category": category, "pinned": pinned})
        mid, ts = memory_id(), now()
        with self._s() as s:
            s.add(ProjectMemory(id=mid, project_id=pid, text=data["text"], category=data["category"],
                                pinned=data["pinned"], source=source, conversation_id=conversation_id,
                                created_by=created_by, created_at=ts, updated_at=ts))
        return self.get_memory(pid, mid)

    def update_memory(self, pid: str, mid: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        data = check_memory_fields(fields)
        if data:
            with self._s() as s:
                s.execute(update(ProjectMemory).where(ProjectMemory.project_id == pid, ProjectMemory.id == mid)
                          .values(**data, updated_at=now()))
        return self.get_memory(pid, mid)

    def delete_memory(self, pid: str, mid: str) -> bool:
        with self._s() as s:
            return s.execute(delete(ProjectMemory).where(ProjectMemory.project_id == pid,
                                                         ProjectMemory.id == mid)).rowcount > 0

    # ---- comments and notifications ------------------------------------------
    def add_comment(self, pid: str, branch: str, *, author_id: str, author_name: str, body: str,
                    target_type: str = "project", target_id: str = "", parent_id: str = "",
                    mentions: list[str] | None = None) -> dict[str, Any]:
        if target_type not in COMMENT_TARGETS:
            raise ValueError(f"target_type must be one of {', '.join(COMMENT_TARGETS)}")
        cid = comment_id()
        with self._s() as s:
            s.add(Comment(id=cid, project_id=pid, branch=branch, target_type=target_type, target_id=target_id,
                          parent_id=parent_id, author_id=author_id, author_name=author_name, body=body,
                          mentions=list(dict.fromkeys(mentions or [])), created_at=now()))
        return self.get_comment(pid, cid)

    def get_comment(self, pid: str, cid: str) -> dict[str, Any] | None:
        with self._s() as s:
            c = s.scalars(select(Comment).where(Comment.project_id == pid, Comment.id == cid)).first()
            return c.to_dict(*COMMENT_COLS) if c else None

    def list_comments(self, pid: str, branch: str | None = None) -> list[dict[str, Any]]:
        """Every comment of a project, oldest first: a branch's own, plus decision threads (decisions are
        project-wide)."""
        with self._s() as s:
            q = select(Comment).where(Comment.project_id == pid)
            if branch is not None:
                q = q.where((Comment.branch == branch) | (Comment.target_type == "decision"))
            return [c.to_dict(*COMMENT_COLS) for c in s.scalars(q.order_by(Comment.created_at, Comment.id))]

    def thread_participants(self, pid: str, thread_id: str) -> list[str]:
        """User ids who wrote in a thread (its root and replies)."""
        with self._s() as s:
            return list(s.scalars(select(Comment.author_id).where(
                Comment.project_id == pid, (Comment.id == thread_id) | (Comment.parent_id == thread_id)).distinct()))

    def update_comment(self, pid: str, cid: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        data = {k: v for k, v in fields.items() if k in ("body", "mentions", "resolved", "resolved_by", "edited_at")}
        if data:
            with self._s() as s:
                s.execute(update(Comment).where(Comment.project_id == pid, Comment.id == cid).values(**data))
        return self.get_comment(pid, cid)

    def delete_comment(self, pid: str, cid: str) -> bool:
        """Delete a comment; deleting a thread's root deletes its replies too."""
        with self._s() as s:
            ids = list(s.scalars(select(Comment.id).where(Comment.project_id == pid,
                                                          (Comment.id == cid) | (Comment.parent_id == cid))))
            if not ids:
                return False
            s.execute(delete(Notification).where(Notification.comment_id.in_(ids)))
            s.execute(delete(Comment).where(Comment.id.in_(ids)))
            return True

    def add_notifications(self, user_ids: list[str], *, comment: dict[str, Any], kind: str, actor_name: str) -> None:
        if not user_ids:
            return
        thread = comment["parent_id"] or comment["id"]
        preview = comment["body"][:200]
        with self._s() as s:
            for uid in dict.fromkeys(user_ids):
                s.add(Notification(user_id=uid, project_id=comment["project_id"], branch=comment["branch"],
                                   comment_id=comment["id"], thread_id=thread, kind=kind, actor_name=actor_name,
                                   preview=preview, created_at=now()))

    def add_review_notifications(self, user_ids: list[str], *, pid: str, branch: str, version: int, kind: str,
                                 actor_name: str, preview: str = "") -> None:
        """Notify people about a version review (requested, approved, changes requested); see approvals.py."""
        with self._s() as s:
            for uid in dict.fromkeys(user_ids):
                s.add(Notification(user_id=uid, project_id=pid, branch=branch, comment_id=None, thread_id="",
                                   version=version, kind=kind, actor_name=actor_name, preview=preview[:200],
                                   created_at=now()))

    def list_notifications(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.execute(select(Notification, Project.name).join(Project, Project.id == Notification.project_id)
                             .where(Notification.user_id == user_id)
                             .order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit))
            return [{**n.to_dict(*NOTIFICATION_COLS), "project_name": name} for n, name in rows]

    def count_unread_notifications(self, user_id: str) -> int:
        with self._s() as s:
            return s.scalar(select(func.count()).select_from(Notification)
                            .where(Notification.user_id == user_id, Notification.read == False)) or 0  # noqa: E712

    def mark_notifications_read(self, user_id: str, ids: list[int] | None = None) -> int:
        """Mark the given notifications (default: all of them) read; returns how many changed."""
        with self._s() as s:
            q = update(Notification).where(Notification.user_id == user_id, Notification.read == False)  # noqa: E712
            if ids is not None:
                q = q.where(Notification.id.in_(ids))
            return s.execute(q.values(read=True)).rowcount

    # ---- version approvals ---------------------------------------------------
    def add_approval(self, pid: str, version: int, branch: str, action: str, *, actor_id: str, actor_name: str,
                     comment: str = "") -> dict[str, Any]:
        if action not in APPROVAL_ACTIONS:
            raise ValueError(f"action must be one of {', '.join(APPROVAL_ACTIONS)}")
        with self._s() as s:
            row = Approval(project_id=pid, version=version, branch=branch, action=action, actor_id=actor_id,
                           actor_name=actor_name, comment=comment, created_at=now())
            s.add(row)
            s.flush()
            return row.to_dict(*APPROVAL_COLS)

    def list_approvals(self, pid: str, version: int | None = None) -> list[dict[str, Any]]:
        """Review actions of a project (or of one version), oldest first."""
        with self._s() as s:
            q = select(Approval).where(Approval.project_id == pid)
            if version is not None:
                q = q.where(Approval.version == version)
            return [a.to_dict(*APPROVAL_COLS) for a in s.scalars(q.order_by(Approval.id))]

    def get_approval_policy(self, org_id: str) -> dict[str, Any]:
        with self._s() as s:
            p = s.get(OrgApprovalPolicy, org_id)
            if p is None:
                return {"required_approvals": 1, "require_for_adopt": False, "updated_by": None, "updated_at": None}
            return p.to_dict("required_approvals", "require_for_adopt", "updated_by", "updated_at")

    def set_approval_policy(self, org_id: str, required_approvals: int, require_for_adopt: bool,
                            user: str) -> dict[str, Any]:
        with self._s() as s:
            self._upsert(s, OrgApprovalPolicy, {"org_id": org_id, "required_approvals": required_approvals,
                                                "require_for_adopt": require_for_adopt, "updated_by": user,
                                                "updated_at": now()}, ["org_id"])
        return self.get_approval_policy(org_id)

    # ---- OpenSpec files and changes ---------------------------------------
    def list_openspec_files(self, pid: str, prefix: str = "", with_content: bool = True) -> list[dict[str, Any]]:
        cols = ["path", "updated_by", "updated_at", *(["content"] if with_content else [])]
        with self._s() as s:
            rows = s.scalars(select(OpenSpecFile).where(OpenSpecFile.project_id == pid,
                                                        func.substr(OpenSpecFile.path, 1, len(prefix)) == prefix)
                             .order_by(OpenSpecFile.path))
            return [f.to_dict(*cols) for f in rows]

    def get_openspec_file(self, pid: str, path: str) -> dict[str, Any] | None:
        with self._s() as s:
            f = s.get(OpenSpecFile, (pid, path))
            return f.to_dict("path", "content", "updated_by", "updated_at") if f else None

    def write_openspec_files(self, pid: str, files: dict[str, str], actor: str, delete: list[str] | None = None) -> None:
        """Upsert ``files`` (rows whose content is unchanged are left alone) and delete the ``delete`` paths."""
        ts = now()
        with self._s() as s:
            if delete:
                s.execute(_delete(OpenSpecFile).where(OpenSpecFile.project_id == pid, OpenSpecFile.path.in_(delete)))
            for path, content in files.items():
                self._upsert(s, OpenSpecFile, {"project_id": pid, "path": path, "content": content,
                                               "updated_by": actor, "updated_at": ts}, ["project_id", "path"],
                             where=lambda stmt: OpenSpecFile.content != stmt.excluded.content)

    def save_openspec_change(self, pid: str, change_id: str, fields: dict[str, Any], actor: str) -> dict[str, Any]:
        data = {k: v for k, v in fields.items() if k in OPENSPEC_CHANGE_FIELDS}
        if "status" in data and data["status"] not in OPENSPEC_STATUSES:
            raise ValueError(f"status must be one of {', '.join(OPENSPEC_STATUSES)}")
        for k in ("scope", "validation"):
            if k in data:
                data[k] = data[k] or {}
        ts = now()
        with self._s() as s:
            c = s.get(OpenSpecChange, (pid, change_id))
            if c is None:
                s.add(OpenSpecChange(project_id=pid, change_id=change_id, created_by=actor, created_at=ts,
                                     updated_at=ts, **data))
            elif data:
                for k, v in data.items():
                    setattr(c, k, v)
                c.updated_at = ts
        return self.get_openspec_change(pid, change_id)

    def get_openspec_change(self, pid: str, change_id: str) -> dict[str, Any] | None:
        with self._s() as s:
            c = s.get(OpenSpecChange, (pid, change_id))
            return c.to_dict(*OPENSPEC_CHANGE_COLS) if c else None

    def list_openspec_changes(self, pid: str) -> list[dict[str, Any]]:
        with self._s() as s:
            rows = s.scalars(select(OpenSpecChange).where(OpenSpecChange.project_id == pid)
                             .order_by(OpenSpecChange.updated_at.desc(), OpenSpecChange.change_id))
            return [c.to_dict(*OPENSPEC_CHANGE_COLS) for c in rows]

    def delete_openspec_change(self, pid: str, change_id: str) -> bool:
        prefix = f"openspec/changes/{change_id}/"
        with self._s() as s:
            s.execute(delete(OpenSpecFile).where(OpenSpecFile.project_id == pid,
                                                 func.substr(OpenSpecFile.path, 1, len(prefix)) == prefix))
            return s.execute(delete(OpenSpecChange).where(OpenSpecChange.project_id == pid,
                                                          OpenSpecChange.change_id == change_id)).rowcount > 0

    # ---- AI usage and budgets --------------------------------------------
    def record_ai_usage(self, event: dict[str, Any]) -> None:
        with self._s() as s:
            s.add(AIUsage(**{k: event[k] for k in AI_USAGE_FIELDS if k in event and event[k] is not None},
                          created_at=now()))

    def ai_spend(self, org_id: str, since: str) -> float:
        with self._s() as s:
            return float(s.scalar(select(func.coalesce(func.sum(AIUsage.cost_usd), 0))
                                  .where(AIUsage.org_id == org_id, AIUsage.created_at >= since)) or 0)

    def ai_usage_summary(self, org_id: str, since: str) -> dict[str, Any]:
        where = (AIUsage.org_id == org_id, AIUsage.created_at >= since)
        day = (func.to_char(func.timezone("UTC", AIUsage.created_at), "YYYY-MM-DD") if self.is_pg
               else func.substr(AIUsage.created_at, 1, 10))
        cost = func.coalesce(func.sum(AIUsage.cost_usd), 0)
        sums = (func.count().label("calls"), func.coalesce(func.sum(AIUsage.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(AIUsage.completion_tokens), 0).label("completion_tokens"), cost.label("cost_usd"))

        with self._s() as s:
            def rows(by: Any) -> list[dict[str, Any]]:
                q = (select(by.label("key"), *sums).where(*where).group_by(by)
                     .order_by(cost.desc(), func.count().desc()))
                return [{**r._mapping, "cost_usd": float(r.cost_usd or 0)} for r in s.execute(q)]

            total = s.execute(select(*sums).where(*where)).first()
            return {"total": {**total._mapping, "cost_usd": float(total.cost_usd or 0)},
                    "by_feature": rows(AIUsage.feature), "by_model": rows(AIUsage.model),
                    "by_project": rows(AIUsage.project_id), "by_prompt": rows(AIUsage.prompt),
                    "by_user": rows(AIUsage.user_id), "daily": rows(day)}

    def get_ai_budget(self, org_id: str) -> dict[str, Any]:
        with self._s() as s:
            b = s.get(OrgAIBudget, org_id)
            if b is None:
                return {"monthly_limit_usd": None, "alert_percent": 80, "updated_by": None, "updated_at": None}
            return b.to_dict("monthly_limit_usd", "alert_percent", "updated_by", "updated_at")

    def set_ai_budget(self, org_id: str, monthly_limit_usd: float | None, alert_percent: int, user: str) -> dict[str, Any]:
        with self._s() as s:
            self._upsert(s, OrgAIBudget, {"org_id": org_id, "monthly_limit_usd": monthly_limit_usd,
                                          "alert_percent": alert_percent, "updated_by": user, "updated_at": now()},
                         ["org_id"])
        return self.get_ai_budget(org_id)

    def get_ai_config(self, org_id: str) -> dict[str, Any]:
        with self._s() as s:
            return s.scalar(select(OrgAIConfig.config).where(OrgAIConfig.org_id == org_id)) or {}

    def set_ai_config(self, org_id: str, config: dict[str, Any], user: str) -> dict[str, Any]:
        with self._s() as s:
            self._upsert(s, OrgAIConfig, {"org_id": org_id, "config": config, "updated_by": user, "updated_at": now()},
                         ["org_id"])
        return self.get_ai_config(org_id)

    # ---- AI traces ----------------------------------------------------------
    _TRACE_ATTRS = {c.key for c in AITrace.__mapper__.column_attrs} - {"metadata_", "created_at"}

    def save_ai_trace(self, trace: dict[str, Any]) -> None:
        """Store a finished trace (agents.tracing.Trace.to_dict()); saving the same id again replaces it."""
        values = {k: v for k, v in trace.items() if k in self._TRACE_ATTRS and (v is not None or k in ("input", "output"))}
        with self._s() as s:
            s.merge(AITrace(**values, metadata_=trace.get("metadata") or {},
                            created_at=trace.get("created_at") or trace.get("started_at") or now()))

    def list_ai_traces(self, org_id: str, *, limit: int = 50, before: str | None = None, top_level: bool = False,
                       **filters: str | None) -> list[dict[str, Any]]:
        """Newest first. ``top_level`` leaves out runs fired by another run (they are listed under it)."""
        q = select(AITrace).where(AITrace.org_id == org_id)
        if top_level:
            q = q.where(AITrace.parent_id == "")
        for k in AI_TRACE_FILTERS:
            if filters.get(k) is not None and filters.get(k) != "":
                q = q.where(getattr(AITrace, k) == filters[k])
        if before:
            q = q.where(AITrace.created_at < before)
        with self._s() as s:
            rows = s.scalars(q.order_by(AITrace.created_at.desc(), AITrace.id).limit(max(1, min(limit, 200))))
            return [t.to_dict(*AI_TRACE_LIST_COLS) for t in rows]

    def get_ai_trace(self, org_id: str, tid: str) -> dict[str, Any] | None:
        with self._s() as s:
            t = s.scalars(select(AITrace).where(AITrace.org_id == org_id, AITrace.id == tid)).first()
            if t is None:
                return None
            d = t.to_dict()
            d["metadata"] = d.pop("metadata_") or {}
            d["children"] = [c.to_dict(*AI_TRACE_LIST_COLS) for c in s.scalars(
                select(AITrace).where(AITrace.org_id == org_id, AITrace.parent_id == tid)
                .order_by(AITrace.created_at, AITrace.id))]
            return d

    def prune_ai_traces(self, before: str) -> int:
        with self._s() as s:
            return s.execute(delete(AITrace).where(AITrace.created_at < before)).rowcount


def default_store() -> Store:
    """Postgres when DATABASE_URL is a postgres URL, otherwise SQLite (ARCHFLOW_DB or backend/data/archflow.db)."""
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith(("postgres://", "postgresql://")):
        return Store(url)
    return Store(os.environ.get("ARCHFLOW_DB", Path(__file__).resolve().parent.parent / "data" / "archflow.db"))
