"""Versioned prompts, kept as YAML in ``archflow/prompts/`` (one file per prompt id).

File format::

    id: compare.evaluate            # dotted id, also the file name (compare.evaluate.yaml)
    feature: decisions              # usage / trace attribution
    group: reasoning                # model group in llm.py (reasoning | fast)
    description: ...
    active: v2                      # the version served by default
    versions:
      v1:
        date: 2026-10-10
        notes: what changed and why
        max_tokens: 8000            # optional
        system: |                   # Jinja2 template (StrictUndefined: a missing variable is an error)
          ...
        user: |                     # optional Jinja2 template for the first user message
          ...

Released versions are immutable: ``prompts.lock.json`` records a hash of every version, and the test suite fails
when a locked version's text changes. To change a prompt, add a new version (v3), point ``active`` at it, run
``archflow-prompts lock`` and compare the two with ``archflow-eval --prompt <id>=v2`` against ``=v3``.

Which version runs:
1. an explicit ``version=`` (evals, A/B runs),
2. ``ARCHFLOW_PROMPTS="compare.evaluate=v1,design.turn=v2"`` (pin or roll back without a deploy),
3. the file's ``active`` version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
LOCK_FILE = PROMPTS_DIR / "prompts.lock.json"

_env = Environment(undefined=StrictUndefined, keep_trailing_newline=False, autoescape=False,
                   trim_blocks=True, lstrip_blocks=True)


class PromptError(KeyError):
    """Unknown prompt id or version, or an invalid prompt file."""


@dataclass(frozen=True)
class PromptVersion:
    id: str
    version: str
    feature: str
    group: str
    system: str
    user: str = ""
    max_tokens: int | None = None
    date: str = ""
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        """``id@version``: what traces and usage rows record."""
        return f"{self.id}@{self.version}"

    @property
    def sha(self) -> str:
        return content_hash(self.system, self.user)

    def render_system(self, **variables: Any) -> str:
        return _env.from_string(self.system).render(**variables).strip()

    def render_user(self, **variables: Any) -> str:
        return _env.from_string(self.user).render(**variables).strip() if self.user else ""


def content_hash(system: str, user: str = "") -> str:
    return hashlib.sha256(json.dumps([system, user]).encode()).hexdigest()[:16]


_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}


def _load_file(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    pid = data.get("id")
    if pid != path.stem:
        raise PromptError(f"{path.name}: id '{pid}' must match the file name")
    versions = data.get("versions") or {}
    if not versions:
        raise PromptError(f"{path.name}: no versions")
    if data.get("active") not in versions:
        raise PromptError(f"{path.name}: active version '{data.get('active')}' is not defined")
    for v, body in versions.items():
        if not isinstance(body, dict) or not str(body.get("system") or "").strip():
            raise PromptError(f"{path.name}: version {v} needs a system prompt")
    return data


def _all() -> dict[str, dict[str, Any]]:
    with _lock:
        if not _cache:
            for path in sorted(PROMPTS_DIR.glob("*.yaml")):
                data = _load_file(path)
                _cache[data["id"]] = data
        return _cache


def overrides() -> dict[str, str]:
    """``ARCHFLOW_PROMPTS`` as {prompt id: version}."""
    out: dict[str, str] = {}
    for item in os.environ.get("ARCHFLOW_PROMPTS", "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


_run_overrides: dict[str, str] = {}


def set_run_overrides(mapping: dict[str, str]) -> None:
    """Process-wide pins used by the eval runner (``--prompt id=version``)."""
    _run_overrides.clear()
    _run_overrides.update(mapping)


def ids() -> list[str]:
    return sorted(_all())


def versions(pid: str) -> list[str]:
    data = _all().get(pid)
    if data is None:
        raise PromptError(f"unknown prompt '{pid}'")
    return list(data["versions"])


def active_version(pid: str) -> str:
    data = _all().get(pid)
    if data is None:
        raise PromptError(f"unknown prompt '{pid}'")
    return _run_overrides.get(pid) or overrides().get(pid) or data["active"]


def get(pid: str, version: str | None = None) -> PromptVersion:
    data = _all().get(pid)
    if data is None:
        raise PromptError(f"unknown prompt '{pid}'")
    v = version or active_version(pid)
    body = data["versions"].get(v)
    if body is None:
        raise PromptError(f"prompt '{pid}' has no version '{v}' (has {', '.join(data['versions'])})")
    known = {"system", "user", "max_tokens", "date", "notes"}
    return PromptVersion(id=pid, version=v, feature=str(data.get("feature") or "other"),
                         group=str(body.get("group") or data.get("group") or "reasoning"),
                         system=str(body["system"]), user=str(body.get("user") or ""),
                         max_tokens=body.get("max_tokens"), date=str(body.get("date") or ""),
                         notes=str(body.get("notes") or ""),
                         extra={k: v for k, v in body.items() if k not in known and k != "group"})


def catalog() -> list[dict[str, Any]]:
    """Every prompt with its versions (for the admin UI and ``archflow-prompts list``)."""
    out = []
    for pid, data in _all().items():
        active = active_version(pid)
        out.append({"id": pid, "feature": data.get("feature", "other"), "description": data.get("description", ""),
                    "active": active, "default": data["active"],
                    "versions": [{"version": v, "date": str(b.get("date") or ""), "notes": b.get("notes", ""),
                                  "sha": content_hash(str(b["system"]), str(b.get("user") or ""))}
                                 for v, b in data["versions"].items()]})
    return out


# --------------------------------------------------------------------------- lock file
def read_lock() -> dict[str, str]:
    try:
        return json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def lock_problems() -> list[str]:
    """Locked versions whose text changed, and versions that are not locked yet."""
    lock, problems = read_lock(), []
    for entry in catalog():
        for v in entry["versions"]:
            ref = f"{entry['id']}@{v['version']}"
            if ref not in lock:
                problems.append(f"{ref} is not locked (run `archflow-prompts lock`)")
            elif lock[ref] != v["sha"]:
                problems.append(f"{ref} changed after it was locked: add a new version instead of editing it")
    return problems


def write_lock(force: bool = False) -> list[str]:
    """Lock every version not locked yet. Refuses to re-lock a changed version unless ``force``."""
    lock = read_lock()
    added = []
    for entry in catalog():
        for v in entry["versions"]:
            ref = f"{entry['id']}@{v['version']}"
            if ref in lock and lock[ref] != v["sha"] and not force:
                raise PromptError(f"{ref} changed after it was locked; add a new version (or pass --force)")
            if lock.get(ref) != v["sha"]:
                lock[ref] = v["sha"]
                added.append(ref)
    LOCK_FILE.write_text(json.dumps(dict(sorted(lock.items())), indent=1) + "\n", encoding="utf-8")
    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archflow-prompts", description="List, check and lock versioned prompts.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="every prompt and its versions")
    sub.add_parser("check", help="fail when a locked version changed or a version is not locked")
    lk = sub.add_parser("lock", help="lock new versions")
    lk.add_argument("--force", action="store_true", help="also re-lock versions whose text changed")
    show = sub.add_parser("show", help="print one prompt version")
    show.add_argument("ref", help="id or id@version")
    args = parser.parse_args(argv)
    if args.cmd == "list":
        for e in catalog():
            vs = ", ".join(v["version"] + ("*" if v["version"] == e["active"] else "") for v in e["versions"])
            print(f"{e['id']:<24} {e['feature']:<14} {vs}")
        return 0
    if args.cmd == "check":
        problems = lock_problems()
        for p in problems:
            print(p, file=sys.stderr)
        return 1 if problems else 0
    if args.cmd == "lock":
        for ref in write_lock(force=args.force):
            print(f"locked {ref}")
        return 0
    pid, _, ver = args.ref.partition("@")
    p = get(pid, ver or None)
    print(f"# {p.ref} ({p.feature}, {p.group}) sha={p.sha}\n## system\n{p.system}\n## user\n{p.user}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
