"""SQLAlchemy 2.0 ORM models: the whole ArchFlow schema, for SQLite and Postgres.

The schema is created and upgraded by Alembic (``archflow/db/migrations``); ``store.Store`` is the only module
that queries these models. Timestamps are ISO strings in Python (see ``types.UTCDateTime``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Double, ForeignKey, Index, Integer, LargeBinary,
                        Text, UniqueConstraint, false, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..models import ProjectState
from .types import JSONType, StateJSON, UTCDateTime, now

# Autoincrementing 64-bit ids (SQLite needs INTEGER for its rowid alias).
BigId = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    def to_dict(self, *attrs: str) -> dict[str, Any]:
        """The given mapped attributes (default: all of them) as a plain dict."""
        return {a: getattr(self, a) for a in (attrs or [c.key for c in self.__mapper__.column_attrs])}


def _fk(target: str) -> ForeignKey:
    return ForeignKey(target, ondelete="CASCADE")


def created() -> Mapped[str]:
    return mapped_column(UTCDateTime, nullable=False, default=now)


# --------------------------------------------------------------------------- organizations and accounts
class Org(Base):
    __tablename__ = "orgs"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = created()


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)  # stored lower-cased
    name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)  # scrypt, see users.py
    created_at: Mapped[str] = created()
    last_login_at: Mapped[str | None] = mapped_column(UTCDateTime)


class UserSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("idx_sessions_user", "user_id"),)
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)  # SHA-256 of the cookie; never the token
    user_id: Mapped[str] = mapped_column(Text, _fk("users.id"), nullable=False)
    created_at: Mapped[str] = created()
    expires_at: Mapped[float] = mapped_column(Double, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    user_agent: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (Index("idx_memberships_user", "user_id"),)
    org_id: Mapped[str] = mapped_column(Text, _fk("orgs.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, _fk("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = created()


class Invite(Base):
    __tablename__ = "invites"
    __table_args__ = (Index("idx_invites_org", "org_id"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    org_id: Mapped[str] = mapped_column(Text, _fk("orgs.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(Text)  # optional: only this address may accept
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = created()
    expires_at: Mapped[float] = mapped_column(Double, nullable=False)
    accepted_at: Mapped[str | None] = mapped_column(UTCDateTime)
    accepted_by: Mapped[str | None] = mapped_column(Text)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())


class OrgAIKey(Base):
    __tablename__ = "org_ai_keys"
    org_id: Mapped[str] = mapped_column(Text, _fk("orgs.id"), primary_key=True)
    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)  # AES-256-GCM, see secrets_box.py
    last4: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now, onupdate=now)


class OrgAIBudget(Base):
    __tablename__ = "org_ai_budgets"
    org_id: Mapped[str] = mapped_column(Text, _fk("orgs.id"), primary_key=True)
    monthly_limit_usd: Mapped[float | None] = mapped_column(Double)
    alert_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=80, server_default="80")
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now, onupdate=now)


class OrgAIConfig(Base):
    __tablename__ = "org_ai_config"
    org_id: Mapped[str] = mapped_column(Text, _fk("orgs.id"), primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict, server_default="{}")
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now, onupdate=now)


class OrgJiraConnection(Base):
    """An org's Jira Cloud site: the API token is encrypted like the AI keys (secrets_box.py)."""
    __tablename__ = "org_jira_connections"
    org_id: Mapped[str] = mapped_column(Text, _fk("orgs.id"), primary_key=True)
    site_url: Mapped[str] = mapped_column(Text, nullable=False)  # https://<site>.atlassian.net
    email: Mapped[str] = mapped_column(Text, nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)  # AES-256-GCM API token
    last4: Mapped[str] = mapped_column(Text, nullable=False)
    project_key: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now, onupdate=now)


# --------------------------------------------------------------------------- projects, branches, versions
class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (Index("idx_projects_org", "org_id"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[str] = created()
    org_id: Mapped[str] = mapped_column(Text, ForeignKey("orgs.id"), nullable=False, default="org-default", server_default="org-default")


class Branch(Base):
    __tablename__ = "branches"
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    state_json: Mapped[ProjectState] = mapped_column(StateJSON, nullable=False)
    base_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = created()


class Version(Base):
    __tablename__ = "versions"
    __table_args__ = (Index("idx_versions_branch", "project_id", "branch"),)
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    parent: Mapped[int | None] = mapped_column(Integer)
    state_json: Mapped[ProjectState] = mapped_column(StateJSON, nullable=False)
    created_at: Mapped[str] = created()


class Activity(Base):
    __tablename__ = "activity"
    __table_args__ = (Index("idx_activity_branch", "project_id", "branch", text("id DESC")),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    rev: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = created()


class RequirementRevision(Base):
    """One change to one requirement: who, at which revision, and each field's value before and after."""
    __tablename__ = "requirement_revisions"
    __table_args__ = (
        CheckConstraint("action IN ('added', 'updated', 'deleted', 'restored', 'purged')",
                        name="ck_requirement_revisions_action"),
        Index("idx_requirement_revisions", "project_id", "branch", "requirement_id", text("id DESC")),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    requirement_id: Mapped[str] = mapped_column(Text, nullable=False)
    rev: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False)  # [{field, before, after}]
    created_at: Mapped[str] = created()


class YDoc(Base):
    """Binary Yjs state of a branch's live collaborative document."""
    __tablename__ = "ydocs"
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), primary_key=True)
    branch: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now, onupdate=now)


# --------------------------------------------------------------------------- OAuth and API tokens (hashes only)
class OAuthClient(Base):
    __tablename__ = "oauth_clients"
    client_id: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[str] = created()


class OAuthCode(Base):
    __tablename__ = "oauth_codes"
    code_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    expires_at: Mapped[float] = mapped_column(Double, nullable=False)


class ApiToken(Base):
    __tablename__ = "api_tokens"
    __table_args__ = (CheckConstraint("kind IN ('access', 'refresh', 'pat')", name="ck_api_tokens_kind"),
                      Index("idx_api_tokens_org", "org_id", "kind"))
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    org_id: Mapped[str] = mapped_column(Text, _fk("orgs.id"), nullable=False)
    user_name: Mapped[str] = mapped_column(Text, nullable=False)  # display name at creation (for listings)
    user_id: Mapped[str | None] = mapped_column(Text, _fk("users.id"))
    scopes: Mapped[list[str]] = mapped_column(JSONType, nullable=False)
    client_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    resource: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = created()
    expires_at: Mapped[float | None] = mapped_column(Double)
    last_used_at: Mapped[str | None] = mapped_column(UTCDateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())


# --------------------------------------------------------------------------- decisions, code graphs
class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (UniqueConstraint("project_id", "seq", name="uq_decisions_seq"),
                      CheckConstraint("status IN ('proposed', 'accepted', 'rejected', 'superseded')",
                                      name="ck_decisions_status"))
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # ADR-001, ADR-002, ... per project
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="proposed", server_default="proposed")
    context: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    decision: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    consequences: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    alternatives: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list, server_default="[]")
    chosen: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    requirement_ids: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list, server_default="[]")
    node_ids: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list, server_default="[]")
    evaluation: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = created()
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now, onupdate=now)


class CodeGraph(Base):
    __tablename__ = "code_graphs"
    __table_args__ = (Index("idx_code_graphs_project", "project_id", text("created_at DESC")),)
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # cg-<hex>
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # git | local | upload
    repo_url: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = created()
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ready", server_default="ready")
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict, server_default="{}")
    graph: Mapped[bytes | None] = mapped_column(LargeBinary)  # zlib-compressed normalized graph JSON


# --------------------------------------------------------------------------- conversations and memory
class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (CheckConstraint("kind IN ('assistant', 'design')", name="ck_conversations_kind"),
                      Index("idx_conversations_project", "project_id", text("updated_at DESC")))
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # cv-<hex>
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # turns folded out of the context window
    summarized_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = created()
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),)
    conversation_id: Mapped[str] = mapped_column(Text, _fk("conversations.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="ask", server_default="ask")  # ask | command | design | note
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict, server_default="{}")
    author: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = created()


class ProjectMemory(Base):
    __tablename__ = "project_memory"
    __table_args__ = (Index("idx_project_memory_project", "project_id"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # mem-<hex>
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="fact", server_default="fact")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    source: Mapped[str] = mapped_column(Text, nullable=False, default="user", server_default="user")  # user | ai | design-chat | mcp
    conversation_id: Mapped[str | None] = mapped_column(Text)  # no FK: survives deleting the chat
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = created()
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now)


# --------------------------------------------------------------------------- comments and notifications
class Comment(Base):
    """A comment thread (``parent_id`` empty) or a reply, on a component, a decision or the whole project."""
    __tablename__ = "comments"
    __table_args__ = (CheckConstraint("target_type IN ('project', 'node', 'decision')", name="ck_comments_target"),
                      Index("idx_comments_project", "project_id", "branch", "created_at"),
                      Index("idx_comments_parent", "parent_id"))
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # cm-<hex>
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False, default="project", server_default="project")
    target_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # node id / ADR id
    parent_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # thread root
    author_id: Mapped[str] = mapped_column(Text, nullable=False)
    author_name: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    mentions: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list, server_default="[]")  # user ids
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    resolved_by: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[str] = created()
    edited_at: Mapped[str | None] = mapped_column(UTCDateTime)


class Notification(Base):
    """Someone mentioned you or replied in a thread you are part of (``comment_id`` set), or acted on a version
    review: asked you to review it, approved it or requested changes (``version`` set, see approvals.py)."""
    __tablename__ = "notifications"
    __table_args__ = (CheckConstraint("kind IN ('mention', 'reply', 'review_requested', 'approved', "
                                      "'changes_requested')", name="ck_notifications_kind"),
                      Index("idx_notifications_user", "user_id", text("created_at DESC")))
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(Text, _fk("users.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    comment_id: Mapped[str | None] = mapped_column(Text, _fk("comments.id"))
    thread_id: Mapped[str] = mapped_column(Text, nullable=False)  # comment thread; empty for a version review
    version: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor_name: Mapped[str] = mapped_column(Text, nullable=False)
    preview: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[str] = created()


# --------------------------------------------------------------------------- version approvals
class Approval(Base):
    """One review action on a saved version: review requested, approved, or changes requested. A version's review
    status (draft, in review, approved, changes requested) is derived from its rows in order (see approvals.py)."""
    __tablename__ = "approvals"
    __table_args__ = (CheckConstraint("action IN ('request', 'approve', 'request_changes')", name="ck_approvals_action"),
                      Index("idx_approvals_version", "project_id", "version"))
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_name: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[str] = created()


class OrgApprovalPolicy(Base):
    """How many approvals a version needs, and whether a draft must be approved before it is adopted."""
    __tablename__ = "org_approval_policy"
    org_id: Mapped[str] = mapped_column(Text, _fk("orgs.id"), primary_key=True)
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    require_for_adopt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now, onupdate=now)


# --------------------------------------------------------------------------- OpenSpec
class OpenSpecFile(Base):
    __tablename__ = "openspec_files"
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)  # e.g. openspec/specs/order-lookup/spec.md
    content: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now)


class OpenSpecChange(Base):
    __tablename__ = "openspec_changes"
    __table_args__ = (CheckConstraint("status IN ('draft', 'valid', 'invalid', 'archived')",
                                      name="ck_openspec_changes_status"),
                      Index("idx_openspec_changes_project", "project_id", text("updated_at DESC")))
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), primary_key=True)
    change_id: Mapped[str] = mapped_column(Text, primary_key=True)  # files under openspec/changes/<change_id>/
    title: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")
    engine: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    branch: Mapped[str] = mapped_column(Text, nullable=False, default="main", server_default="main")
    scope: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict, server_default="{}")
    validation: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict, server_default="{}")
    archived_as: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = created()
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now)


# --------------------------------------------------------------------------- external trackers (Jira)
class ExternalLink(Base):
    """One ArchFlow object pushed to an external tracker: the issue it became, and a hash of what was pushed, so
    re-pushes update or skip instead of creating duplicates."""
    __tablename__ = "external_links"
    __table_args__ = (UniqueConstraint("project_id", "system", "object_kind", "object_id", name="uq_external_links_object"),
                      Index("idx_external_links_key", "system", "external_key"))
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), nullable=False)
    system: Mapped[str] = mapped_column(Text, nullable=False, default="jira", server_default="jira")
    object_kind: Mapped[str] = mapped_column(Text, nullable=False)  # epic | requirement | subtask | link
    object_id: Mapped[str] = mapped_column(Text, nullable=False)  # node id, requirement id, "REQ-001:node-id", ...
    external_key: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. SHOP-42 (an issue-link id for links)
    external_url: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    content_hash: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # pulled from Jira
    status_category: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # new | indeterminate | done
    status_checked_at: Mapped[str | None] = mapped_column(UTCDateTime)
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now, onupdate=now)


# --------------------------------------------------------------------------- AI usage and traces
class AIUsage(Base):
    """One LLM call: tokens and estimated cost, attributed to an org, project, feature, prompt version and user."""
    __tablename__ = "ai_usage"
    __table_args__ = (Index("idx_ai_usage_org_time", "org_id", "created_at"),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(Text, nullable=False)  # no FK: usage history survives deleting projects
    project_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    feature: Mapped[str] = mapped_column(Text, nullable=False, default="other", server_default="other")
    model: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Double, nullable=False, default=0.0, server_default="0")
    cost_source: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # litellm | table | estimate | unknown
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # id@version
    user_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[str] = created()


class AITrace(Base):
    """One agent run: who asked what, what the AI answered, every model and tool call, tokens, cost, latency."""
    __tablename__ = "ai_traces"
    __table_args__ = (Index("idx_ai_traces_org_time", "org_id", "created_at"),
                      Index("idx_ai_traces_parent", "parent_id"))
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    org_id: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    parent_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # the run that fanned this one out
    user_id: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    user_name: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    feature: Mapped[str] = mapped_column(Text, nullable=False, default="other", server_default="other")
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # id@version
    agent: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ok", server_default="ok")  # ok | error
    error: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    question: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # what the person asked (preview)
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # what the AI answered (preview)
    input: Mapped[Any | None] = mapped_column(JSONType, nullable=True)  # NULL when ARCHFLOW_TRACES=metadata
    output: Mapped[Any | None] = mapped_column(JSONType, nullable=True)
    spans: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list, server_default="[]")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, nullable=False, default=dict, server_default="{}")
    models: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list, server_default="[]")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Double, nullable=False, default=0.0, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[str] = created()


# --------------------------------------------------------------------------- knowledge base (RAG)
class KbDocument(Base):
    """A document in a project's knowledge base: an upload, an imported spec or a meeting transcript."""
    __tablename__ = "kb_documents"
    __table_args__ = (Index("idx_kb_documents_project", "project_id", "created_at"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # kb-<hex>
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)  # pdf | docx | md | txt | vtt | srt | audio
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="upload", server_default="upload")  # upload | import | meeting
    chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # "" = keywords only
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[str] = created()


class KbChunk(Base):
    """One retrievable passage of a knowledge-base document, with its embedding when a model made one."""
    __tablename__ = "kb_chunks"
    __table_args__ = (Index("idx_kb_chunks_project", "project_id"), Index("idx_kb_chunks_document", "document_id", "ord"))
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(Text, _fk("kb_documents.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), nullable=False)
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    page: Mapped[int | None] = mapped_column(Integer)
    locator: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")  # e.g. a meeting timestamp
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(JSONType, nullable=True)


# --------------------------------------------------------------------------- integrations (GitHub, Confluence, Slack, Teams)
class OrgIntegration(Base):
    """An org's connection to one outside service. The secret (token or webhook URL) is encrypted like the AI keys."""
    __tablename__ = "org_integrations"
    org_id: Mapped[str] = mapped_column(Text, _fk("orgs.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)  # github | confluence | slack | teams
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict, server_default="{}")
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    last4: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now, onupdate=now)


# --------------------------------------------------------------------------- compliance frameworks
class ComplianceSettings(Base):
    """Which compliance frameworks (soc2, hipaa, gdpr, pci, iso27001) apply to a project."""
    __tablename__ = "compliance_settings"
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), primary_key=True)
    frameworks: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list, server_default="[]")
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now, onupdate=now)


class ComplianceEvidence(Base):
    """A person's evidence or note on one control: "met" (by process), "not_applicable", or a note only ("")."""
    __tablename__ = "compliance_evidence"
    project_id: Mapped[str] = mapped_column(Text, _fk("projects.id"), primary_key=True)
    framework: Mapped[str] = mapped_column(Text, primary_key=True)
    control_id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(UTCDateTime, nullable=False, default=now, onupdate=now)
