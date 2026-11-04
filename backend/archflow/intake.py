"""Document intake: turn a requirements document (PDF, Word, Markdown, plain text) into proposed requirements.

1. ``read_document`` extracts the text, keeping headings as sections and PDF page numbers, within
   ``ARCHFLOW_INTAKE_MAX_MB`` (default 20).
2. ``chunks`` splits long documents at section boundaries.
3. ``propose`` runs the ``intake.extract`` agent on every chunk in parallel (``agents.parallel``) and merges the
   results: functional and non-functional requirements with the section and page they came from, plus
   assumptions, constraints, open questions and stakeholders. Without a model (or for a chunk whose agent
   failed) a heuristic reads "shall / must / should" sentences and bullets under requirement headings instead.
4. Every proposal is compared with the project's requirements and the rest of the document: likely duplicates
   and conflicts (different targets for the same thing) are flagged with both sources.

Nothing is applied by the upload. ``accept`` turns the proposals a person selected (and possibly edited) into
``add_requirement`` ops with ``source`` provenance {type, ref, section, page, at}, and can save assumptions,
constraints and open questions to project memory.

Document text is untrusted: the prompt wraps it in ``<untrusted_data>`` and tells the model never to follow
instructions inside it.

With ``Accept: text/event-stream`` the upload answers with server-sent events instead: ``progress`` events
{stage, message, done?, total?} while it reads (which library, which page, which model, which part), then one
``done`` (the proposal), ``error`` {status, message} or ``cancelled``. Closing the connection cancels the import:
pages and parts not started yet are skipped (a model call already running finishes and is discarded).

=======  ================================================  ============================================
method   path                                              what
=======  ================================================  ============================================
POST     /api/projects/{pid}/requirements/import           raw file body, ?filename=&branch= → proposal
POST     /api/projects/{pid}/requirements/import/accept    {file, requirements, memory} → applied
=======  ================================================  ============================================
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import threading
import zipfile
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import agents, conversations, llm
from .agents import prompts
from .db.types import now
from .models import ProjectState, Requirement
from .store import MEMORY_CATEGORIES

log = logging.getLogger("archflow.intake")

PROMPT = "intake.extract"
TYPES = {".pdf": "pdf", ".docx": "docx", ".md": "md", ".markdown": "md", ".txt": "txt", ".text": "txt"}
CHUNK_CHARS = 12000
MAX_CHUNKS = 24
MAX_PAGES = 500
MAX_PROPOSALS = 300
DUPLICATE_AT = 0.6   # token similarity at or above which a proposal is a likely duplicate
RELATED_AT = 0.4     # ... above which differing numbers or a negation make it a likely conflict

SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "requirements": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "kind": {"type": "string", "enum": ["functional", "non_functional"]},
        "category": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "actors": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "section": {"type": "string"},
        "page": {"type": "integer"},
        "duplicate_of": {"type": "string"},
        "conflicts_with": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["id", "reason"]}}},
        "required": ["title", "kind", "priority"]}},
    "assumptions": {"type": "array", "items": {"type": "string"}},
    "constraints": {"type": "array", "items": {"type": "string"}},
    "open_questions": {"type": "array", "items": {"type": "string"}},
    "stakeholders": {"type": "array", "items": {"type": "string"}}},
    "required": ["requirements"]}


Progress = Callable[[dict[str, Any]], None]
Cancelled = Callable[[], bool]


def _quiet(_: dict[str, Any]) -> None:
    pass


def _never() -> bool:
    return False


class ImportCancelled(Exception):
    """The person cancelled the import (closed the upload) before it finished."""


def _check(cancelled: Cancelled) -> None:
    if cancelled():
        raise ImportCancelled()


def _step(stage: str, message: str, done: int | None = None, total: int | None = None) -> dict[str, Any]:
    return {"stage": stage, "message": message, **({"done": done, "total": total} if total is not None else {})}


class IntakeError(ValueError):
    """A document that cannot be imported; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def max_bytes() -> int:
    try:
        mb = float(os.environ.get("ARCHFLOW_INTAKE_MAX_MB", "20"))
    except ValueError:
        mb = 20.0
    return int(max(mb, 0.001) * 1024 * 1024)


# --------------------------------------------------------------------------- reading documents
@dataclass
class Section:
    heading: str
    page: int | None
    text: str


@dataclass
class Document:
    name: str
    type: str  # pdf | docx | md | txt
    sections: list[Section] = field(default_factory=list)
    pages: int | None = None

    @property
    def chars(self) -> int:
        return sum(len(s.text) for s in self.sections)


def doc_type(filename: str, data: bytes) -> str:
    ext = os.path.splitext(filename.lower())[1]
    if data.startswith(b"%PDF"):
        return "pdf"
    if ext in TYPES:
        return TYPES[ext]
    raise IntakeError(f"Unsupported file type '{ext or filename}': upload a PDF, Word (.docx), Markdown or text "
                      "file.", 415)


_MODAL = re.compile(r"\b(shall|must|should|is required to|are required to|needs? to|has to|have to)\b", re.I)
_NUMBERED = re.compile(r"^((?:\d+\.)*\d+)\.?\s+(\S.{0,100})$")
_BULLET = re.compile(r"^\s*(?:[-*+•·▪◦]|\(?\d{1,3}[.)]|\(?[a-z][.)])\s+")


def _heading(line: str) -> str | None:
    """A heading in plain text or extracted PDF text: '2.1 Login', 'NON-FUNCTIONAL REQUIREMENTS', '# Scope'."""
    s = line.strip()
    if not s or len(s) > 100 or _MODAL.search(s):
        return None
    if m := re.match(r"^#{1,6}\s+(.+?)\s*#*$", s):
        return m.group(1).strip()
    words = s.split()
    if _NUMBERED.match(s) and len(words) <= 12 and not s.endswith((".", ",", ";", ":")) and s.split()[1][:1].isupper():
        return s
    if len(words) <= 10 and len(s) >= 3 and s.isupper() and any(c.isalpha() for c in s):
        return s.title() if len(s) > 4 else s
    return None


def _sections_from_lines(lines: list[tuple[str, int | None]], markdown: bool = False) -> list[Section]:
    out: list[Section] = []
    cur = Section("", None, "")
    body: list[str] = []
    in_code = False

    def flush() -> None:
        text = "\n".join(body).strip()
        if text:
            out.append(Section(cur.heading, cur.page, text))
        body.clear()

    for line, page in lines:
        if markdown and line.strip().startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code:  # code samples are not requirements
            continue
        if page != cur.page and body:  # a section that runs over a page break continues on the next page
            flush()
        if page != cur.page:
            cur = Section(cur.heading, page, "")
        h = _heading(line) if not markdown else (
            m.group(1).strip() if (m := re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*$", line)) else None)
        if h:
            flush()
            cur = Section(h, page, "")
        else:
            body.append(line.rstrip())
    flush()
    return out


def _version(dist: str) -> str:
    try:
        return f"{dist} {metadata.version(dist)}"
    except metadata.PackageNotFoundError:
        return dist


def reader_name(kind: str) -> str:
    """The library that reads a document type, with its version (shown while importing)."""
    return {"pdf": _version("pypdf"), "docx": _version("python-docx"),
            "md": "ArchFlow's Markdown reader", "txt": "ArchFlow's text reader"}[kind]


def _read_pdf(data: bytes, progress: Progress = _quiet, cancelled: Cancelled = _never) -> tuple[list[Section], int]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise IntakeError("This PDF is password-protected: remove the password and upload it again.")
        pages = reader.pages
        n = len(pages)
        lines: list[tuple[str, int | None]] = []
        last = min(n, MAX_PAGES)
        for i, page in enumerate(pages[:MAX_PAGES]):
            _check(cancelled)
            if i == 0 or (i + 1) % 5 == 0 or i + 1 == last:
                progress(_step("read", f"Reading page {i + 1} of {last}", i + 1, last))
            for line in (page.extract_text() or "").splitlines():
                lines.append((line, i + 1))
    except IntakeError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise IntakeError(f"Could not read this PDF: {exc}") from exc
    return _sections_from_lines(lines), n


def _read_docx(data: bytes) -> list[Section]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > 10 * max_bytes():
                raise IntakeError("This Word file expands to too much data.", 413)
        import docx
        d = docx.Document(io.BytesIO(data))
    except IntakeError:
        raise
    except Exception as exc:  # noqa: BLE001 - python-docx raises many kinds for a broken file
        raise IntakeError(f"Could not read this Word file: {exc}") from exc
    out: list[Section] = []
    heading, body = "", []

    def flush() -> None:
        text = "\n".join(body).strip()
        if text:
            out.append(Section(heading, None, text))
        body.clear()

    for block in d.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "tbl":
            from docx.table import Table
            for row in Table(block, d).rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    body.append(" | ".join(dict.fromkeys(cells)))  # merged cells repeat their text
            continue
        if tag != "p":
            continue
        from docx.text.paragraph import Paragraph
        p = Paragraph(block, d)
        text, style = p.text.strip(), (p.style.name if p.style is not None else "") or ""
        if not text:
            continue
        if style.startswith("Heading") or style == "Title":
            flush()
            heading = text
        elif style.startswith("List") or p._p.pPr is not None and p._p.pPr.numPr is not None:
            body.append(f"- {text}")
        else:
            body.append(text)
    flush()
    return out


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def read_document(filename: str, data: bytes, progress: Progress = _quiet, cancelled: Cancelled = _never) -> Document:
    """Extract sections from an uploaded file. Raises IntakeError (unsupported, too large, unreadable, empty)."""
    limit = max_bytes()
    if len(data) > limit:
        raise IntakeError(f"The file is larger than {limit / (1024 * 1024):g} MB.", 413)
    if not data:
        raise IntakeError("The file is empty.")
    kind = doc_type(filename, data)
    doc = Document(name=os.path.basename(filename) or "document", type=kind)
    what = {"pdf": "PDF", "docx": "Word file", "md": "Markdown"}.get(kind, "text")
    progress(_step("read", f"Reading the {what} with {reader_name(kind)}"))
    if kind == "pdf":
        doc.sections, doc.pages = _read_pdf(data, progress, cancelled)
    elif kind == "docx":
        doc.sections = _read_docx(data)
    else:
        if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise IntakeError("This does not look like a text file.", 415)
        lines = [(line, None) for line in _decode(data).splitlines()]
        doc.sections = _sections_from_lines(lines, markdown=kind == "md")
    if not doc.chars:
        raise IntakeError("No text found in the document (a scanned PDF needs OCR first).")
    progress(_step("read", f"Read {len(doc.sections)} section(s)"
                           + (f" on {doc.pages} page(s)" if doc.pages else "") + f", {doc.chars:,} characters"))
    return doc


def chunks(doc: Document, size: int | None = None) -> list[list[Section]]:
    """Split the document at section boundaries into parts of about ``size`` (default CHUNK_CHARS) characters;
    long sections are split at paragraphs. ``propose`` reads at most MAX_CHUNKS parts and notes the rest."""
    size = size or CHUNK_CHARS
    pieces: list[Section] = []
    for s in doc.sections:
        if len(s.text) <= size:
            pieces.append(s)
            continue
        buf = ""
        for para in re.split(r"\n\s*\n|\n(?=\s*[-*•])", s.text):
            while len(para) > size:  # one huge paragraph: hard split
                if buf:
                    pieces.append(Section(s.heading, s.page, buf))
                    buf = ""
                pieces.append(Section(s.heading, s.page, para[:size]))
                para = para[size:]
            if buf and len(buf) + len(para) > size:
                pieces.append(Section(s.heading, s.page, buf))
                buf = ""
            buf = f"{buf}\n\n{para}" if buf else para
        if buf.strip():
            pieces.append(Section(s.heading, s.page, buf))
    out: list[list[Section]] = []
    cur: list[Section] = []
    n = 0
    for p in pieces:
        if cur and n + len(p.text) > size:
            out.append(cur)
            cur, n = [], 0
        cur.append(p)
        n += len(p.text)
    if cur:
        out.append(cur)
    return out


def _marker(s: Section) -> str:
    return f"[section: {s.heading or '(untitled)'}" + (f"; page {s.page}" if s.page else "") + "]"


def render_chunk(part: list[Section]) -> str:
    # The document may contain our own delimiter; neutralise it so it cannot close the untrusted block.
    text = "\n\n".join(f"{_marker(s)}\n{s.text}" for s in part)
    return re.sub(r"</?\s*untrusted_data", "[untrusted_data", text, flags=re.I)


# --------------------------------------------------------------------------- similarity
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "be", "is", "are", "by", "as", "at",
         "it", "its", "this", "that", "shall", "must", "should", "will", "can", "able", "system", "all", "any",
         "from", "their", "they", "user", "users"}


def _tokens(text: str) -> set[str]:
    return {w.rstrip("s") if len(w) > 3 else w for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOP}


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def _negated(text: str) -> bool:
    return bool(re.search(r"\b(not|never|no|without|cannot)\b", text.lower()))


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _text(r: dict[str, Any] | Requirement) -> str:
    if isinstance(r, Requirement):
        return f"{r.title}. {r.description}"
    return f"{r.get('title', '')}. {r.get('description', '')}"


def compare(a: str, b: str) -> tuple[str | None, str]:
    """('duplicate' | 'conflict' | None, reason) for two requirement texts."""
    sim = similarity(a, b)
    na, nb = _numbers(a), _numbers(b)
    if sim >= RELATED_AT and na and nb and na != nb:
        return "conflict", f"different targets: {', '.join(sorted(na - nb))} vs {', '.join(sorted(nb - na))}"
    if sim >= RELATED_AT and _negated(a) != _negated(b):
        return "conflict", "one says the opposite of the other"
    if sim >= DUPLICATE_AT:
        return "duplicate", f"{sim:.0%} similar"
    return None, ""


# --------------------------------------------------------------------------- offline extraction
_NFR = {
    "performance": r"latency|response time|\bms\b|millisecond|throughput|per second|\brps\b|fast",
    "availability": r"availability|uptime|99\.\d|downtime|failover",
    "security": r"encrypt|authenticat|authori[sz]|password|\btls\b|\bmfa\b|secret|access control|audit",
    "compliance": r"gdpr|hipaa|pci|soc ?2|complian|regulat|retention|data residency",
    "scalability": r"concurrent|scal|peak|load of|\d[\d,]* (users|requests|orders)",
    "reliability": r"backup|recover|\brpo\b|\brto\b|durab|no data loss|retry",
    "usability": r"usab|accessib|wcag|intuitive|mobile[- ]friendly",
    "maintainability": r"maintainab|logging|monitor|observab|deploy",
}
_SUBJECT = re.compile(r"^(?:the\s+|an?\s+|each\s+|every\s+|all\s+)?([a-z][a-z \-]{1,40}?)\s+(?:shall|must|should|can|may)"
                      r"\s+(?:be able to|be allowed to)\b", re.I)  # "Admins must be able to ..." names an actor
_CAPABILITY = re.compile(r"\b(be able to|be allowed to|can|allow|let|enable)\b", re.I)
_SYSTEM_WORDS = {"system", "application", "app", "platform", "service", "solution", "api", "it", "software",
                 "product", "website", "site", "backend", "server", "data", "this"}


def _sentences(text: str) -> list[str]:
    """Bullets and sentences of a section (wrapped lines of one bullet or paragraph are joined)."""
    items: list[str] = []
    cur = ""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if cur:
                items.append(cur)
            cur = ""
        elif _BULLET.match(line):
            if cur:
                items.append(cur)
            cur = "•" + _BULLET.sub("", line).strip()
        else:
            cur = f"{cur} {s}" if cur else s
    if cur:
        items.append(cur)
    out: list[str] = []
    for item in items:
        bullet = item.startswith("•")
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", item.lstrip("•"))
        out += [("•" if bullet else "") + p.strip() for p in parts if p.strip()]
    return out


def _title(sentence: str) -> str:
    s = re.sub(r"^(?:the\s+)?(?:system|application|platform|service|solution)\s+(?:shall|must|should)\s+"
               r"(?:be able to\s+)?", "", sentence, flags=re.I).rstrip(".;: ")
    s = s[:1].upper() + s[1:]
    return s if len(s) <= 90 else s[:87].rsplit(" ", 1)[0] + "…"


def offline_extract(part: list[Section]) -> dict[str, Any]:
    """Heuristic extraction from one chunk: modal-verb sentences and bullets under requirement headings."""
    reqs, assumptions, constraints, questions, stakeholders = [], [], [], [], []
    for s in part:
        h = s.heading.lower()
        where = {"section": s.heading, "page": s.page}
        for raw in _sentences(s.text):
            bullet = raw.startswith("•")
            text = raw.lstrip("•").strip()
            if len(text) < 8:
                continue
            low = text.lower()
            if "stakeholder" in h and bullet:
                stakeholders.append(text.rstrip("."))
            elif text.endswith("?") or re.search(r"\b(tbd|tbc|to be (decided|confirmed|defined))\b", low) \
                    or "open question" in h:
                questions.append({"text": text, **where})
            elif "assum" in h or re.match(r"^(we )?assum", low):
                assumptions.append({"text": text, **where})
            elif "constraint" in h or re.match(r"^(constraint|limitation)s?\b", low):
                constraints.append({"text": text, **where})
            elif _MODAL.search(text) or (bullet and "requirement" in h):
                category = next((c for c, rx in _NFR.items() if re.search(rx, low)), "")
                if re.search(r"non[- ]?functional|quality", h):
                    nfr = True
                elif "functional" in h or _CAPABILITY.search(low):  # a capability, even one that names a password
                    nfr = False
                else:
                    nfr = bool(category)
                priority = ("high" if re.search(r"\b(shall|must|is required to|are required to)\b", low)
                            else "low" if re.search(r"\b(may|could|nice to have)\b", low) else "medium")
                actors = []
                if (m := _SUBJECT.match(text)) and m.group(1).lower().split()[-1] not in _SYSTEM_WORDS:
                    actors = [m.group(1).strip().capitalize()]
                reqs.append({"title": _title(text), "description": text, "kind": "non_functional" if nfr else
                             "functional", "category": category if nfr else "", "priority": priority,
                             "actors": actors, "acceptance_criteria": [], "section": s.heading,
                             "page": s.page or 0})
    return {"requirements": reqs, "assumptions": assumptions, "constraints": constraints,
            "open_questions": questions, "stakeholders": stakeholders}


# --------------------------------------------------------------------------- LLM extraction + merge
def existing_summary(state: ProjectState, limit: int = 150) -> list[dict[str, Any]]:
    return [{"id": r.id, "title": r.title, "kind": r.kind, **({"category": r.category} if r.category else {}),
             "description": r.description[:240]} for r in list(state.requirements.values())[:limit]]


def llm_extract(doc: Document, part: list[Section], index: int, total: int, existing: list[dict[str, Any]],
                org_id: str | None, version: str | None = None) -> dict[str, Any]:
    return agents.run_json(PROMPT, SCHEMA, org_id=org_id, version=version,
                           question=f"Extract requirements from {doc.name} (part {index + 1} of {total})",
                           variables={"filename": doc.name, "part": f"{index + 1} of {total}",
                                      "existing_json": json.dumps(existing, indent=1),
                                      "document": render_chunk(part)})


def _clip(v: Any, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def _strings(items: Any, n: int = 400, limit: int = 30) -> list[str]:
    return [s for s in (_clip(x, n) for x in (items or [])[:limit] if isinstance(x, (str, int, float))) if s]


def _clean_req(r: dict[str, Any], part: list[Section]) -> dict[str, Any] | None:
    title = _clip(r.get("title"), 160)
    if not title:
        return None
    section = _clip(r.get("section"), 160)
    known = {s.heading: s.page for s in part}
    page = r.get("page") if isinstance(r.get("page"), int) and r.get("page") > 0 else None
    if page is None:
        page = known.get(section) or next((s.page for s in part if s.page), None)
    if not section and part:
        section = part[0].heading
    kind = r.get("kind") if r.get("kind") in ("functional", "non_functional") else "functional"
    return {"title": title, "description": _clip(r.get("description"), 2000), "kind": kind,
            "category": _clip(r.get("category"), 40).lower() if kind == "non_functional" else "",
            "priority": r.get("priority") if r.get("priority") in ("low", "medium", "high", "critical") else "medium",
            "actors": _strings(r.get("actors"), 60, 10), "acceptance_criteria": _strings(r.get("acceptance_criteria")),
            "section": section, "page": page,
            "duplicate_of": _clip(r.get("duplicate_of"), 40),
            "conflicts_with": [{"id": _clip(c.get("id"), 40), "reason": _clip(c.get("reason"), 300)}
                               for c in (r.get("conflicts_with") or []) if isinstance(c, dict) and c.get("id")]}


def _src(doc: Document, section: str, page: int | None) -> dict[str, str]:
    return {"type": doc.type, "ref": doc.name, **({"section": section} if section else {}),
            **({"page": str(page)} if page else {})}


def merge(doc: Document, results: list[dict[str, Any]], parts: list[list[Section]],
          state: ProjectState) -> dict[str, Any]:
    """Merge per-chunk results: clean, number proposals P-001…, flag duplicates and conflicts with both sources."""
    proposals: list[dict[str, Any]] = []
    seen: set[str] = set()
    lists: dict[str, list[dict[str, Any]]] = {"assumptions": [], "constraints": [], "open_questions": []}
    stakeholders: list[str] = []
    for res, part in zip(results, parts):
        # The AI returns plain sentences: attribute them to the part's section / page only when it has just one.
        heads, pages = {s.heading for s in part}, {s.page for s in part}
        where = {"section": heads.pop() if len(heads) == 1 else "", "page": pages.pop() if len(pages) == 1 else None}
        for raw in res.get("requirements") or []:
            r = _clean_req(raw, part) if isinstance(raw, dict) else None
            key = " ".join(sorted(_tokens(_text(r)))) if r else ""
            if not r or key in seen or len(proposals) >= MAX_PROPOSALS:
                continue
            seen.add(key)
            proposals.append(r)
        for k in lists:
            for item in (res.get(k) or [])[:60]:
                at = item if isinstance(item, dict) else {"text": item, **where}
                text = _clip(at.get("text"), 400)
                if text and not any(text == x["text"] or similarity(text, x["text"]) >= 0.8 for x in lists[k]):
                    lists[k].append({"text": text, **{kk: at[kk] for kk in ("section", "page") if at.get(kk)}})
        for s in _strings(res.get("stakeholders"), 120):
            if s.lower() not in {x.lower() for x in stakeholders}:
                stakeholders.append(s)

    reqs = state.requirements
    for i, p in enumerate(proposals):
        p["id"] = f"P-{i + 1:03d}"
        p["source"] = _src(doc, p["section"], p["page"])
        dups: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        llm_dup, llm_conflicts = p.pop("duplicate_of"), p.pop("conflicts_with")
        mine = _text(p)
        for r in reqs.values():
            verdict, why = compare(mine, _text(r))
            hinted = next((c["reason"] for c in llm_conflicts if c["id"] == r.id), "")
            entry = {"id": r.id, "title": r.title, "where": "project", "source": dict(r.source)}
            if hinted or verdict == "conflict":
                conflicts.append({**entry, "reason": hinted or why, "proposed_source": p["source"]})
            elif verdict == "duplicate" or llm_dup == r.id:
                dups.append({**entry, "reason": why or "the AI matched it"})
        for q in proposals[:i]:
            verdict, why = compare(mine, _text(q))
            entry = {"id": q["id"], "title": q["title"], "where": "document", "source": q["source"]}
            if verdict == "conflict":
                conflicts.append({**entry, "reason": why, "proposed_source": p["source"]})
            elif verdict == "duplicate":
                dups.append({**entry, "reason": why})
        p["duplicates"], p["conflicts"] = dups, conflicts
        p["selected"] = not dups
    return {"requirements": proposals, **lists, "stakeholders": stakeholders}


def extract_model(org_id: str | None, version: str | None = None) -> str:
    """The model the ``intake.extract`` agent calls first for this org ("" when none is configured)."""
    models = llm.status(org_id)["groups"].get(prompts.get(PROMPT, version).group) or []
    return models[0] if models else ""


def propose(doc: Document, state: ProjectState, org_id: str | None, *, use_ai: bool | None = None,
            version: str | None = None, progress: Progress = _quiet,
            cancelled: Cancelled = _never) -> dict[str, Any]:
    """Extract proposals from a read document (not applied). ``engine`` is llm, offline or mixed."""
    parts = chunks(doc)
    notes: list[str] = []
    if len(parts) > MAX_CHUNKS:
        notes.append(f"The document is long: only the first {MAX_CHUNKS} of {len(parts)} parts were read.")
        parts = parts[:MAX_CHUNKS]
    progress(_step("split", f"Split into {len(parts)} part(s) of up to {CHUNK_CHARS:,} characters at section "
                            "boundaries"))
    _check(cancelled)
    use_ai = llm.available(org_id) if use_ai is None else use_ai
    model = extract_model(org_id, version) if use_ai else ""
    results: list[dict[str, Any]] = []
    engines: set[str] = set()
    if use_ai:
        existing = existing_summary(state)
        who = model or "the AI"
        progress(_step("extract", f"Extracting requirements with {who}", 0, len(parts)))
        finished = [0]
        lock = threading.Lock()

        def extract(i: int, part: list[Section]) -> dict[str, Any]:
            _check(cancelled)  # parts still queued when the person cancels are never sent to the model
            try:
                return llm_extract(doc, part, i, len(parts), existing, org_id, version)
            finally:
                with lock:
                    finished[0] += 1
                    progress(_step("extract", f"Extracting with {who}: part {i + 1} done "
                                              f"({finished[0]} of {len(parts)})", finished[0], len(parts)))

        out = agents.parallel({str(i): (lambda i=i, part=part: extract(i, part)) for i, part in enumerate(parts)},
                              name="intake.extract", org_id=org_id, question=f"Extract requirements from {doc.name}")
        _check(cancelled)
        for i, part in enumerate(parts):
            res = out.get(str(i))
            if isinstance(res, Exception):
                log.info("intake chunk %d fell back to heuristics: %s", i, type(res).__name__)
                results.append(offline_extract(part))
                engines.add("offline")
            else:
                results.append(res)
                engines.add("llm")
        if "offline" in engines:
            notes.append("The AI could not read some parts; those were read with the offline rules.")
    else:
        progress(_step("extract", 'No AI model is configured: reading "shall / must / should" sentences and '
                                  "requirement bullets with the offline rules", 0, len(parts)))
        for i, part in enumerate(parts):
            _check(cancelled)
            results.append(offline_extract(part))
            progress(_step("extract", f"Offline rules: part {i + 1} of {len(parts)} done", i + 1, len(parts)))
        engines.add("offline")
    found = sum(len(r.get("requirements") or []) for r in results)
    progress(_step("compare", f"Checking {found} proposal(s) for duplicates and conflicts with "
                              f"{len(state.requirements)} existing requirement(s)"))
    merged = merge(doc, results, parts, state)
    used = {"library": reader_name(doc.type), "model": model if "llm" in engines else ""}
    engine = "mixed" if len(engines) > 1 else engines.pop()
    return {"engine": engine, "notes": notes,
            "file": {"name": doc.name, "type": doc.type, "pages": doc.pages, "sections": len(doc.sections),
                     "chars": doc.chars, "chunks": len(parts)}, "reader": used, **merged}


# --------------------------------------------------------------------------- accept
SOURCE_KEYS = ("type", "ref", "url", "section", "page")


def accept_ops(items: list[dict[str, Any]], file: dict[str, Any], at: str) -> list[dict[str, Any]]:
    """``add_requirement`` ops for the accepted proposals, each with ``source`` provenance."""
    ftype = file.get("type") if file.get("type") in set(TYPES.values()) else "txt"
    ops = []
    for it in items:
        src = {k: _clip(v, 300) for k, v in (it.get("source") or {}).items() if k in SOURCE_KEYS and v}
        src.update({"type": ftype, "ref": _clip(file.get("name"), 200) or "document", "at": at})
        req = {k: it[k] for k in ("title", "description", "kind", "category", "priority", "actors",
                                  "acceptance_criteria") if k in it}
        ops.append({"op": "add_requirement", "requirement": {**req, "source": src}})
    return ops


class ProposalIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    description: str = Field("", max_length=4000)
    kind: str = Field("functional", pattern="^(functional|non_functional)$")
    category: str = Field("", max_length=60)
    priority: str = Field("medium", pattern="^(low|medium|high|critical)$")
    actors: list[str] = Field([], max_length=20)
    acceptance_criteria: list[str] = Field([], max_length=40)
    source: dict[str, Any] = {}


class MemoryNoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=400)
    category: str = "context"


class AcceptIn(BaseModel):
    file: dict[str, Any]
    requirements: list[ProposalIn] = Field([], max_length=MAX_PROPOSALS)
    memory: list[MemoryNoteIn] = Field([], max_length=60)


# --------------------------------------------------------------------------- routes
def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def stream_job(work: Callable[[Progress, Cancelled], dict[str, Any]], *, running: set[Any], first: dict[str, Any],
               on_done: Callable[[dict[str, Any]], Awaitable[None]], on_error: Callable[[str], None],
               on_cancel: Callable[[], None], failure: str, what: str) -> StreamingResponse:
    """Run ``work(progress, cancelled)`` in a thread and answer with server-sent events: ``progress`` while it runs,
    then ``done`` (its result), ``error`` {status, message} (an IntakeError, or ``failure`` for anything else) or
    ``cancelled``. Closing the connection cancels the job (``cancelled()`` turns true). Used by document and meeting
    imports."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    cancel = threading.Event()

    def emit(kind: str, payload: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

    def run() -> None:
        try:
            emit("done", work(lambda step: emit("progress", step), cancel.is_set))
        except ImportCancelled:
            emit("cancelled", {})  # nobody is listening any more; ends the queue
        except IntakeError as exc:
            emit("error", {"status": exc.status, "message": str(exc)})
        except Exception:  # noqa: BLE001 - reported to the person, details logged
            log.exception("%s failed", what)
            emit("error", {"status": 500, "message": failure})

    task = asyncio.ensure_future(asyncio.to_thread(run))
    running.add(task)
    task.add_done_callback(running.discard)

    async def events():
        ended = False
        try:
            yield _sse("progress", first)
            while True:
                kind, payload = await queue.get()
                if kind == "done":
                    await on_done(payload)
                elif kind == "error":
                    on_error(payload["message"])
                ended = kind != "progress"
                yield _sse(kind, payload)
                if ended:
                    return
        finally:
            if not ended:  # the person cancelled: the connection closed before the result was sent
                cancel.set()
                on_cancel()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def register_routes(app: FastAPI, store: Any, *, authz: Any, state_or_404: Callable[[str, str], ProjectState],
                    commit_ops: Callable[..., Awaitable[tuple[ProjectState, list[str]]]],
                    broadcast: Callable[[str, str, dict[str, Any]], Awaitable[None]],
                    notify: Callable[..., None] | None = None) -> None:
    edit = authz.project("edit")
    running: set[asyncio.Future[Any]] = set()  # imports still reading after their stream closed

    def log_outcome(pid: str, branch: str, actor: str, line: str) -> None:
        state = store.get_state(pid, branch)
        store.log_activity(pid, branch, actor, [line], state.rev if state else 0)

    @app.post("/api/projects/{pid}/requirements/import")
    async def import_document(pid: str, request: Request, filename: str = "document.txt", branch: str = "main",
                              a=Depends(edit)):
        state = state_or_404(pid, branch)
        name = os.path.basename(filename.replace("\\", "/"))[:200] or "document.txt"
        limit = max_bytes()
        declared = request.headers.get("content-length")
        too_big = f"The file is larger than {limit / (1024 * 1024):g} MB."
        if declared and declared.isdigit() and int(declared) > limit:
            log_outcome(pid, branch, a.actor, f"import {name} rejected: too large")
            raise HTTPException(413, too_big)
        data, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                log_outcome(pid, branch, a.actor, f"import {name} rejected: too large")
                raise HTTPException(413, too_big)
            data.append(chunk)
        body = b"".join(data)

        async def proposed(result: dict[str, Any]) -> None:
            r = result["requirements"]
            flagged = sum(1 for p in r if p["duplicates"] or p["conflicts"])
            log_outcome(pid, branch, a.actor, f"import {name}: proposed {len(r)} requirement(s)"
                        + (f", {flagged} flagged" if flagged else "") + f" ({result['engine']})")
            await broadcast(pid, branch, {"type": "activity", "actor": a.actor})

        if "text/event-stream" in request.headers.get("accept", ""):
            return stream_import(pid, branch, a, name, body, state, proposed)
        try:
            doc = await asyncio.to_thread(read_document, name, body)
        except IntakeError as exc:
            log_outcome(pid, branch, a.actor, f"import {name} rejected: {exc}")
            raise HTTPException(exc.status, str(exc)) from exc
        result = await asyncio.to_thread(propose, doc, state, a.org_id)
        await proposed(result)
        return result

    def stream_import(pid: str, branch: str, a: Any, name: str, body: bytes, state: ProjectState,
                      proposed: Callable[[dict[str, Any]], Awaitable[None]]) -> StreamingResponse:
        def work(progress: Progress, cancelled: Cancelled) -> dict[str, Any]:
            doc = read_document(name, body, progress, cancelled)
            return propose(doc, state, a.org_id, progress=progress, cancelled=cancelled)

        return stream_job(work, running=running, first=_step("upload", f"Uploaded {name} ({len(body) / 1024:,.0f} KB)"),
                          on_done=proposed, failure="Something went wrong while reading the document.",
                          on_error=lambda msg: log_outcome(pid, branch, a.actor, f"import {name} rejected: {msg}"),
                          on_cancel=lambda: log_outcome(pid, branch, a.actor, f"import {name} cancelled"),
                          what=f"importing {name} into {pid}")

    @app.post("/api/projects/{pid}/requirements/import/accept")
    async def accept_import(pid: str, body: AcceptIn, branch: str = "main", a=Depends(edit)):
        state_or_404(pid, branch)
        name = _clip(body.file.get("name"), 200) or "document"
        at = now()
        ops = accept_ops([p.model_dump() for p in body.requirements], body.file, at)
        summary: list[str] = []
        state = None
        if ops:
            state, summary = await commit_ops(pid, branch, ops, f"{a.actor} (import {name})")
        saved = []
        for m in body.memory:
            cat = m.category if m.category in MEMORY_CATEGORIES else "context"
            item = conversations.add_fact(store, pid, m.text, category=cat, source="import", created_by=a.actor)
            if item:
                saved.append(item)
        if saved:
            await broadcast(pid, branch, {"type": "memory", "actor": a.actor, "added": [m["text"] for m in saved]})
        line = (f"import {name}: accepted {len(ops)} requirement(s)"
                + (f", saved {len(saved)} note(s) to memory" if body.memory else ""))
        log_outcome(pid, branch, a.actor, line)
        if notify and ops:
            notify(a.org_id, pid, "import", f"{a.actor} imported requirements from {name}", line, branch=branch)
        added = [r.id for r in (state.requirements.values() if state else []) if r.source.get("at") == at]
        return {"added": added, "summary": summary, "memory": saved,
                **({"state": state.model_dump()} if state else {})}
