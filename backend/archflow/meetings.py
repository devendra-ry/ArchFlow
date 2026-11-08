"""Meeting intake: turn a meeting transcript (or recording) into requirements, decisions and action items.

1. ``read_transcript`` reads WebVTT (Teams, Zoom, Google Meet exports; ``<v Speaker>`` voice tags or
   "Speaker: text"), SubRip (.srt), plain-text or Markdown transcripts ("[00:01:02] Speaker: text",
   "Speaker  0:03" on its own line followed by what they said, or just "Speaker: text"), and Word (.docx)
   transcripts, into speaker turns with timestamps. An audio or video recording (mp3, m4a, wav, webm, mp4, ogg,
   flac) is transcribed with ``llm.transcribe`` when the org has a speech-to-text model (OpenAI Whisper by
   default; ``ARCHFLOW_TRANSCRIBE=off`` keeps recordings on the server). Recordings have no speaker names.
2. ``propose`` runs the ``meetings.extract`` agent on windows of the conversation (in parallel) and merges what it
   found: requirements (with who asked for them, when, and whether the room agreed or it was only proposed),
   decisions (with rationale and the alternatives discussed), action items (owner, due date), open questions,
   assumptions, constraints, participants and a short summary. Without a model, rules read requirement sentences
   ("must", "needs to"...), decisions ("we decided", "let's go with"...), action items ("I'll", "can you"...) and
   real questions.
3. Proposed requirements are compared with the project's requirements and with each other (``intake.compare``):
   likely duplicates and conflicts are flagged.

Nothing is applied by the upload. ``accept`` turns the selection into ``add_requirement`` ops with provenance
``source = {type: "meeting", ref: <file>, section: <speaker>, timestamp: "00:12:05", at}``, proposed ADRs
(``status: proposed``) for the decisions, and project-memory notes for action items, questions, assumptions and
constraints. With ``?knowledge=true`` the transcript is also added to the project knowledge base (searchable,
cited by the assistant).

The transcript is untrusted: the prompt wraps it in ``<untrusted_data>``. With ``Accept: text/event-stream`` the
upload streams progress like document intake and can be cancelled by closing the connection.

=======  ==============================================  ==============================================
method   path                                            what
=======  ==============================================  ==============================================
POST     /api/projects/{pid}/meetings/import             raw file body, ?filename=&branch=&knowledge= → proposal
POST     /api/projects/{pid}/meetings/import/accept      {file, requirements, decisions, memory} → applied
=======  ==============================================  ==============================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import agents, conversations, intake, knowledge, llm
from .agents import prompts
from .db.types import now
from .intake import Cancelled, IntakeError, Progress, Section, _check, _clip, _quiet, _never, _step, _strings
from .models import ProjectState
from .store import MEMORY_CATEGORIES

log = logging.getLogger("archflow.meetings")

PROMPT = "meetings.extract"
TEXT_TYPES = {".vtt": "vtt", ".srt": "srt", ".txt": "txt", ".text": "txt", ".md": "md", ".markdown": "md",
              ".docx": "docx"}
AUDIO_TYPES = {".mp3", ".m4a", ".wav", ".webm", ".mp4", ".mpeg", ".mpga", ".ogg", ".oga", ".flac"}
WINDOW_CHARS = 10000
KB_WINDOW_CHARS = 1200
MAX_WINDOWS = 24
MAX_TURNS = 20000
AUDIO_MAX_MB = 25  # OpenAI's limit for one transcription request

REQ_ITEM = {"type": "object", "properties": {
    "title": {"type": "string"},
    "description": {"type": "string"},
    "kind": {"type": "string", "enum": ["functional", "non_functional"]},
    "category": {"type": "string"},
    "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
    "actors": {"type": "array", "items": {"type": "string"}},
    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
    "speaker": {"type": "string"},
    "timestamp": {"type": "string"},
    "agreement": {"type": "string", "enum": ["agreed", "proposed", "disputed"]},
    "duplicate_of": {"type": "string"},
    "conflicts_with": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["id", "reason"]}}},
    "required": ["title", "kind", "priority"]}
NOTE_ITEM = {"type": "object", "properties": {"text": {"type": "string"}, "timestamp": {"type": "string"}},
             "required": ["text"]}
SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "summary": {"type": "string"},
    "participants": {"type": "array", "items": {"type": "string"}},
    "requirements": {"type": "array", "items": REQ_ITEM},
    "decisions": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "decision": {"type": "string"}, "rationale": {"type": "string"},
        "alternatives": {"type": "array", "items": {"type": "string"}}, "decided_by": {"type": "string"},
        "timestamp": {"type": "string"}}, "required": ["title", "decision"]}},
    "action_items": {"type": "array", "items": {"type": "object", "properties": {
        "task": {"type": "string"}, "owner": {"type": "string"}, "due": {"type": "string"},
        "timestamp": {"type": "string"}}, "required": ["task"]}},
    "open_questions": {"type": "array", "items": NOTE_ITEM},
    "assumptions": {"type": "array", "items": NOTE_ITEM},
    "constraints": {"type": "array", "items": NOTE_ITEM}},
    "required": ["requirements", "decisions", "action_items"]}


# --------------------------------------------------------------------------- reading transcripts
@dataclass
class Turn:
    speaker: str
    start: float | None
    text: str


@dataclass
class Transcript:
    name: str
    type: str  # vtt | srt | txt | md | docx | audio
    turns: list[Turn] = field(default_factory=list)
    model: str = ""  # the speech-to-text model, for recordings

    @property
    def chars(self) -> int:
        return sum(len(t.text) for t in self.turns)

    @property
    def speakers(self) -> list[str]:
        return list(dict.fromkeys(t.speaker for t in self.turns if t.speaker))

    @property
    def duration(self) -> float | None:
        times = [t.start for t in self.turns if t.start is not None]
        return max(times) if times else None


def stamp(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


_TS = r"(\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"


def seconds(ts: str) -> float | None:
    m = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?", ts.strip())
    if not m:
        return None
    h, mi, s, frac = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + (int(frac) / 10 ** len(frac) if frac else 0)


def kind_of(filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    if ext in AUDIO_TYPES:
        return "audio"
    if ext in TEXT_TYPES:
        return TEXT_TYPES[ext]
    raise IntakeError(f"Unsupported file type '{ext or filename}': upload a transcript (.vtt, .srt, .txt, .md, "
                      ".docx) or a recording (.mp3, .m4a, .wav, .webm, .mp4).", 415)


def is_transcript(filename: str, data: bytes) -> bool:
    """True for files only the transcript reader understands (subtitles and recordings)."""
    ext = os.path.splitext(filename.lower())[1]
    return ext in (".vtt", ".srt") or ext in AUDIO_TYPES or data[:6] == b"WEBVTT"


_SPEAKER = r"[A-Z][\w.'\-]*(?: [\w.'\-()]+){0,4}"
_LINE_PATTERNS = [
    # [00:01:02] Speaker: text   |   00:01:02 Speaker: text   |   (00:01) Speaker - text
    re.compile(rf"^[\[(]?(?P<ts>{_TS})[\])]?\s*[-–]?\s*(?P<sp>{_SPEAKER})\s*[:\-–]\s+(?P<text>.+)$"),
    # Speaker [00:01:02]: text   |   Speaker (00:01): text
    re.compile(rf"^(?P<sp>{_SPEAKER})\s*[\[(](?P<ts>{_TS})[\])]\s*[:\-–]?\s*(?P<text>.+)$"),
    # Speaker: text
    re.compile(rf"^(?P<sp>{_SPEAKER}):\s+(?P<text>.+)$"),
]
# "Speaker   0:03" or "0:03 Speaker" alone on a line (Teams / Google Meet exports), the words follow on the next lines
_HEADER = [re.compile(rf"^(?P<sp>{_SPEAKER})\s{{1,}}(?P<ts>{_TS})$"), re.compile(rf"^(?P<ts>{_TS})\s+(?P<sp>{_SPEAKER})$")]
_NOT_SPEAKERS = {"http", "https", "note", "notes", "agenda", "action", "todo", "decision", "summary", "q", "a"}


def _speaker_ok(sp: str) -> bool:
    return bool(sp) and len(sp) <= 60 and sp.split()[0].lower().rstrip(".") not in _NOT_SPEAKERS


def parse_text(text: str) -> list[Turn]:
    """Speaker turns of a plain-text transcript. Lines that continue a turn are joined to it; text without any
    speaker marks becomes one turn per paragraph (speaker "")."""
    turns: list[Turn] = []
    cur: Turn | None = None
    pending: tuple[str, float | None] | None = None  # a "Speaker 0:03" header waiting for its words
    plain: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line:
            if cur is None and plain:
                plain.append("")
            continue
        head = next((m for p in _HEADER if (m := p.match(line))), None)
        if head and _speaker_ok(head.group("sp")):
            pending, cur = (head.group("sp").strip(), seconds(head.group("ts"))), None
            continue
        m = next((m for p in _LINE_PATTERNS if (m := p.match(line))), None)
        if m and _speaker_ok(m.group("sp")):
            ts = m.groupdict().get("ts")
            cur = Turn(m.group("sp").strip(), seconds(ts) if ts else None, m.group("text").strip())
            turns.append(cur)
            pending = None
        elif pending:
            cur = Turn(pending[0], pending[1], line)
            turns.append(cur)
            pending = None
        elif cur is not None:
            cur.text = f"{cur.text} {line}"
        else:
            plain.append(line)
    if not turns:
        paras = re.split(r"\n\s*\n", "\n".join(plain))
        turns = [Turn("", None, " ".join(p.split())) for p in paras if p.strip()]
    return turns[:MAX_TURNS]


def _cue_text(lines: list[str]) -> tuple[str, str]:
    """(speaker, text) of one subtitle cue; ``<v Name>`` voice tags or a "Name:" prefix name the speaker."""
    text = " ".join(lines)
    speaker = ""
    if m := re.search(r"<v(?:\.[\w.-]+)?\s+([^>]+)>", text):
        speaker = m.group(1).strip()
    text = re.sub(r"<[^>]+>", "", text).strip()
    if not speaker and (m := re.match(rf"^({_SPEAKER}):\s+(.+)$", text)) and _speaker_ok(m.group(1)):
        speaker, text = m.group(1), m.group(2)
    return speaker, " ".join(text.split())


def parse_subtitles(text: str) -> list[Turn]:
    """Turns of a WebVTT or SubRip file: one per cue, consecutive cues of the same speaker merged."""
    turns: list[Turn] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    arrow = re.compile(rf"^\s*({_TS})\s*-->\s*({_TS})")
    for block in blocks:
        lines = [x.strip() for x in block.split("\n") if x.strip()]
        idx = next((i for i, x in enumerate(lines) if arrow.match(x)), None)
        if idx is None:
            continue  # WEBVTT header, NOTE, STYLE blocks
        start = seconds(arrow.match(lines[idx]).group(1))
        speaker, words = _cue_text(lines[idx + 1:])
        if not words:
            continue
        if turns and turns[-1].speaker == speaker and len(turns[-1].text) < 1500:
            turns[-1].text = f"{turns[-1].text} {words}"
        else:
            turns.append(Turn(speaker, start, words))
        if len(turns) >= MAX_TURNS:
            break
    return turns


def _docx_text(data: bytes) -> str:
    sections = intake._read_docx(data)
    return "\n".join((f"{s.heading}\n" if s.heading else "") + s.text for s in sections)


def read_transcript(filename: str, data: bytes, org_id: str | None = None, progress: Progress = _quiet,
                    cancelled: Cancelled = _never) -> Transcript:
    """Speaker turns from an uploaded transcript or recording. Raises IntakeError (or LLMUnavailable for a
    recording when no speech-to-text model is configured)."""
    limit = intake.max_bytes()
    if len(data) > limit:
        raise IntakeError(f"The file is larger than {limit / (1024 * 1024):g} MB.", 413)
    if not data:
        raise IntakeError("The file is empty.")
    kind = "vtt" if data[:6] == b"WEBVTT" else kind_of(filename)
    t = Transcript(name=os.path.basename(filename) or "meeting", type=kind)
    if kind == "audio":
        if len(data) > AUDIO_MAX_MB * 1024 * 1024:
            raise IntakeError(f"Recordings can be at most {AUDIO_MAX_MB} MB: upload the meeting's transcript "
                              "instead, or a shorter recording.", 413)
        picked = llm.transcription_model(org_id)
        if not picked:
            raise IntakeError("Transcribing recordings needs a speech-to-text model (an OpenAI key in AI settings). "
                              "Upload the meeting's transcript (.vtt, .srt, .txt or .docx) instead.", 415)
        progress(_step("read", f"Transcribing the recording with {picked[0]}"))
        _check(cancelled)
        try:
            out = llm.transcribe(data, t.name, org_id)
        except llm.LLMUnavailable as exc:
            raise IntakeError(f"Could not transcribe the recording: {exc}", 502) from exc
        t.model = out["model"]
        t.turns = [Turn("", s["start"], s["text"]) for s in out["segments"] if s["text"]] or \
            [Turn("", None, p) for p in re.split(r"(?<=[.!?])\s+(?=[A-Z])", out["text"]) if p.strip()]
    elif kind == "docx":
        progress(_step("read", f"Reading the Word transcript with {intake.reader_name('docx')}"))
        t.turns = parse_text(_docx_text(data))
    else:
        if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise IntakeError("This does not look like a text file.", 415)
        text = intake._decode(data)
        progress(_step("read", "Reading the " + {"vtt": "WebVTT subtitles", "srt": "SubRip subtitles"}.get(kind, "transcript")))
        t.turns = parse_subtitles(text) if kind in ("vtt", "srt") or "-->" in text[:2000] else parse_text(text)
    t.turns = [x for x in t.turns if x.text.strip()]
    if not t.turns:
        raise IntakeError("No speech found in the transcript.")
    progress(_step("read", f"Read {len(t.turns)} turn(s) from {len(t.speakers) or 'unnamed'} speaker(s)"
                           + (f" over {stamp(t.duration)}" if t.duration else "") + f", {t.chars:,} characters"))
    return t


def _line(turn: Turn) -> str:
    ts = stamp(turn.start)
    return (f"[{ts}] " if ts else "") + (f"{turn.speaker}: " if turn.speaker else "") + turn.text


def windows(t: Transcript, size: int = WINDOW_CHARS) -> list[list[Turn]]:
    """Consecutive turns grouped into windows of about ``size`` characters."""
    out: list[list[Turn]] = []
    cur: list[Turn] = []
    n = 0
    for turn in t.turns:
        length = len(_line(turn)) + 1
        if cur and n + length > size:
            out.append(cur)
            cur, n = [], 0
        cur.append(turn)
        n += length
    if cur:
        out.append(cur)
    return out


def _span(part: list[Turn]) -> str:
    a, b = stamp(part[0].start), stamp(part[-1].start)
    return f"{a}–{b}" if a and b and a != b else a


def transcript_chunks(t: Transcript) -> list[dict[str, Any]]:
    """Knowledge-base passages: short windows with the speakers as section and the start time as locator."""
    out = []
    for part in windows(t, KB_WINDOW_CHARS):
        speakers = list(dict.fromkeys(x.speaker for x in part if x.speaker))
        out.append({"section": ", ".join(speakers)[:200], "page": None, "locator": _span(part),
                    "text": "\n".join(_line(x) for x in part)})
    return out


def render_window(part: list[Turn]) -> str:
    text = "\n".join(_line(x) for x in part)
    return re.sub(r"</?\s*untrusted_data", "[untrusted_data", text, flags=re.I)


# --------------------------------------------------------------------------- offline extraction
_DECISION = re.compile(r"\b(we(?:'ve| have)? (?:decided|agreed)|(?:the )?decision (?:is|was)|let'?s go with|we'?ll go "
                       r"with|we will go with|we(?:'re| are) going with|agreed[,:]? we|final(?:ly)?,? we'?ll use|"
                       r"we'?ll use|we will use|we should go with|let'?s use)\b", re.I)
_ACTION = re.compile(r"\b(action item|to-?do|i'?ll|i will|i can take|i'?ll take|can you|could you|will you|please "
                     r"(?:send|share|write|check|review|prepare|set up|look into))\b", re.I)
_DUE = re.compile(r"\b(?:by|before|on|until)\s+((?:next\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                  r"week|month|sprint)|tomorrow|tonight|end of (?:the )?(?:day|week|month|sprint|quarter)|eod|eow|"
                  r"\d{1,2}(?:st|nd|rd|th)? of \w+|\w+ \d{1,2}(?:st|nd|rd|th)?|\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?)\b", re.I)
_NAME_WILL = re.compile(rf"\b({_SPEAKER}) (?:will|is going to|to) (?!be\b)", re.M)
_REAL_QUESTION = re.compile(r"\b(should|do we|does it|will we|how|what|which|when|who|where|can we|is it|are we)\b",
                            re.I)
_SMALL_TALK = re.compile(r"\b(hear me|see my screen|share (?:my|your) screen|you there|on mute|muted|join(?:ed)?|"
                         r"good morning|how are you|any other questions)\b", re.I)


def offline_extract(part: list[Turn]) -> dict[str, Any]:
    """Rule-based extraction from one window (used without a model, or when its agent call failed)."""
    reqs: list[dict[str, Any]] = []
    decisions, actions, questions = [], [], []
    for turn in part:
        ts = stamp(turn.start)
        for sentence in intake._sentences(turn.text):
            s = sentence.lstrip("•").strip()
            if len(s) < 12:
                continue
            if _DECISION.search(s):
                decisions.append({"title": intake._title(s), "decision": s, "rationale": "", "alternatives": [],
                                  "decided_by": turn.speaker, "timestamp": ts})
            elif _ACTION.search(s):
                owner = turn.speaker if re.search(r"\bi'?ll\b|\bi will\b|\bi can\b", s, re.I) else ""
                if not owner and (m := _NAME_WILL.search(s)) and _speaker_ok(m.group(1)):
                    owner = m.group(1)
                due = _DUE.search(s)
                actions.append({"task": s.rstrip("."), "owner": owner, "due": due.group(1) if due else "",
                                "timestamp": ts})
            elif s.endswith("?"):
                if len(s) >= 25 and _REAL_QUESTION.search(s) and not _SMALL_TALK.search(s):
                    questions.append({"text": s, "timestamp": ts})
            else:
                found = intake.offline_extract([Section("", None, s)])["requirements"]
                for r in found:
                    reqs.append({**r, "speaker": turn.speaker, "timestamp": ts, "agreement": "proposed"})
    return {"summary": "", "participants": list(dict.fromkeys(t.speaker for t in part if t.speaker)),
            "requirements": reqs, "decisions": decisions, "action_items": actions, "open_questions": questions,
            "assumptions": [], "constraints": []}


# --------------------------------------------------------------------------- AI extraction + merge
def llm_extract(t: Transcript, part: list[Turn], index: int, total: int, existing: list[dict[str, Any]],
                org_id: str | None, version: str | None = None) -> dict[str, Any]:
    return agents.run_json(PROMPT, SCHEMA, org_id=org_id, version=version,
                           question=f"Extract requirements and decisions from {t.name} (part {index + 1} of {total})",
                           variables={"filename": t.name, "part": f"{index + 1} of {total}",
                                      "speakers": ", ".join(t.speakers) or "(not named)",
                                      "existing_json": json.dumps(existing, indent=1),
                                      "transcript": render_window(part)})


def _ts(v: Any) -> str:
    s = _clip(v, 12)
    sec = seconds(s) if s else None
    return stamp(sec) if sec is not None else ""


def _clean_req(r: dict[str, Any], part: list[Turn]) -> dict[str, Any] | None:
    title = _clip(r.get("title"), 160)
    if not title:
        return None
    kind = r.get("kind") if r.get("kind") in ("functional", "non_functional") else "functional"
    speaker = _clip(r.get("speaker"), 60)
    ts = _ts(r.get("timestamp")) or next((stamp(x.start) for x in part if speaker and x.speaker == speaker), "")
    return {"title": title, "description": _clip(r.get("description"), 2000), "kind": kind,
            "category": _clip(r.get("category"), 40).lower() if kind == "non_functional" else "",
            "priority": r.get("priority") if r.get("priority") in ("low", "medium", "high", "critical") else "medium",
            "actors": _strings(r.get("actors"), 60, 10), "acceptance_criteria": _strings(r.get("acceptance_criteria")),
            "speaker": speaker, "timestamp": ts,
            "agreement": r.get("agreement") if r.get("agreement") in ("agreed", "proposed", "disputed") else "proposed",
            "duplicate_of": _clip(r.get("duplicate_of"), 40),
            "conflicts_with": [{"id": _clip(c.get("id"), 40), "reason": _clip(c.get("reason"), 300)}
                               for c in (r.get("conflicts_with") or []) if isinstance(c, dict) and c.get("id")]}


def _source(t: Transcript, speaker: str, ts: str) -> dict[str, str]:
    return {"type": "meeting", "ref": t.name, **({"section": speaker} if speaker else {}),
            **({"timestamp": ts} if ts else {})}


def _note(item: Any) -> dict[str, str] | None:
    at = item if isinstance(item, dict) else {"text": item}
    text = _clip(at.get("text"), 400)
    return {"text": text, **({"timestamp": _ts(at.get("timestamp"))} if _ts(at.get("timestamp")) else {})} if text else None


def merge(t: Transcript, results: list[dict[str, Any]], parts: list[list[Turn]], state: ProjectState) -> dict[str, Any]:
    """Merge the windows' results; number proposals M-001…, decisions D-1…, flag duplicates and conflicts."""
    proposals: list[dict[str, Any]] = []
    seen: set[str] = set()
    decisions: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    lists: dict[str, list[dict[str, str]]] = {"open_questions": [], "assumptions": [], "constraints": []}
    participants: list[str] = list(t.speakers)
    summaries: list[str] = []
    for res, part in zip(results, parts):
        if s := _clip(res.get("summary"), 1200):
            summaries.append(s)
        for raw in res.get("requirements") or []:
            r = _clean_req(raw, part) if isinstance(raw, dict) else None
            key = " ".join(sorted(intake._tokens(intake._text(r)))) if r else ""
            if not r or key in seen or len(proposals) >= intake.MAX_PROPOSALS:
                continue
            seen.add(key)
            proposals.append(r)
        for raw in res.get("decisions") or []:
            if not isinstance(raw, dict) or not _clip(raw.get("title"), 160):
                continue
            d = {"title": _clip(raw.get("title"), 160), "decision": _clip(raw.get("decision"), 2000),
                 "rationale": _clip(raw.get("rationale"), 2000), "alternatives": _strings(raw.get("alternatives"), 160, 8),
                 "decided_by": _clip(raw.get("decided_by"), 60), "timestamp": _ts(raw.get("timestamp"))}
            if not any(intake.similarity(d["title"] + " " + d["decision"], x["title"] + " " + x["decision"]) >= 0.7
                       for x in decisions):
                decisions.append(d)
        for raw in res.get("action_items") or []:
            if not isinstance(raw, dict) or not _clip(raw.get("task"), 300):
                continue
            a = {"task": _clip(raw.get("task"), 300), "owner": _clip(raw.get("owner"), 60), "due": _clip(raw.get("due"), 60),
                 "timestamp": _ts(raw.get("timestamp"))}
            if not any(intake.similarity(a["task"], x["task"]) >= 0.8 for x in actions):
                actions.append(a)
        for k in lists:
            for item in (res.get(k) or [])[:60]:
                n = _note(item)
                if n and not any(intake.similarity(n["text"], x["text"]) >= 0.8 for x in lists[k]):
                    lists[k].append(n)
        for p in _strings(res.get("participants"), 60):
            if p.lower() not in {x.lower() for x in participants}:
                participants.append(p)

    reqs = state.requirements
    for i, p in enumerate(proposals):
        p["id"] = f"M-{i + 1:03d}"
        p["source"] = _source(t, p["speaker"], p["timestamp"])
        dups, conflicts = [], []
        llm_dup, llm_conflicts = p.pop("duplicate_of"), p.pop("conflicts_with")
        mine = intake._text(p)
        for r in reqs.values():
            if r.deleted:
                continue
            verdict, why = intake.compare(mine, intake._text(r))
            hinted = next((c["reason"] for c in llm_conflicts if c["id"] == r.id), "")
            entry = {"id": r.id, "title": r.title, "where": "project", "source": dict(r.source)}
            if hinted or verdict == "conflict":
                conflicts.append({**entry, "reason": hinted or why, "proposed_source": p["source"]})
            elif verdict == "duplicate" or llm_dup == r.id:
                dups.append({**entry, "reason": why or "the AI matched it"})
        for q in proposals[:i]:
            verdict, why = intake.compare(mine, intake._text(q))
            entry = {"id": q["id"], "title": q["title"], "where": "meeting", "source": q["source"]}
            if verdict == "conflict":
                conflicts.append({**entry, "reason": why, "proposed_source": p["source"]})
            elif verdict == "duplicate":
                dups.append({**entry, "reason": why})
        p["duplicates"], p["conflicts"] = dups, conflicts
        p["selected"] = not dups and p["agreement"] != "disputed"
    for i, d in enumerate(decisions):
        d["id"], d["selected"] = f"D-{i + 1}", True
    for i, a in enumerate(actions):
        a["id"], a["selected"] = f"A-{i + 1}", True
    return {"summary": " ".join(summaries)[:3000], "participants": participants[:40], "requirements": proposals,
            "decisions": decisions, "action_items": actions, **lists}


def extract_model(org_id: str | None, version: str | None = None) -> str:
    models = llm.status(org_id)["groups"].get(prompts.get(PROMPT, version).group) or []
    return models[0] if models else ""


def propose(t: Transcript, state: ProjectState, org_id: str | None, *, use_ai: bool | None = None,
            version: str | None = None, progress: Progress = _quiet, cancelled: Cancelled = _never) -> dict[str, Any]:
    """Proposals from a read transcript (nothing applied). ``engine`` is llm, offline or mixed."""
    parts = windows(t)
    notes: list[str] = []
    if len(parts) > MAX_WINDOWS:
        notes.append(f"The meeting is long: only the first {MAX_WINDOWS} of {len(parts)} parts were read.")
        parts = parts[:MAX_WINDOWS]
    progress(_step("split", f"Split the conversation into {len(parts)} part(s)"))
    _check(cancelled)
    use_ai = llm.available(org_id) if use_ai is None else use_ai
    model = extract_model(org_id, version) if use_ai else ""
    results: list[dict[str, Any]] = []
    engines: set[str] = set()
    if use_ai:
        existing = intake.existing_summary(state)
        who = model or "the AI"
        progress(_step("extract", f"Reading the meeting with {who}", 0, len(parts)))
        finished = [0]
        lock = threading.Lock()

        def extract(i: int, part: list[Turn]) -> dict[str, Any]:
            _check(cancelled)
            try:
                return llm_extract(t, part, i, len(parts), existing, org_id, version)
            finally:
                with lock:
                    finished[0] += 1
                    progress(_step("extract", f"Reading with {who}: part {i + 1} done ({finished[0]} of {len(parts)})",
                                   finished[0], len(parts)))

        out = agents.parallel({str(i): (lambda i=i, part=part: extract(i, part)) for i, part in enumerate(parts)},
                              name="meetings.extract", org_id=org_id, question=f"Read the meeting {t.name}")
        _check(cancelled)
        for i, part in enumerate(parts):
            res = out.get(str(i))
            if isinstance(res, Exception):
                log.info("meeting window %d fell back to rules: %s", i, type(res).__name__)
                results.append(offline_extract(part))
                engines.add("offline")
            else:
                results.append(res)
                engines.add("llm")
        if "offline" in engines:
            notes.append("The AI could not read some parts; those were read with the offline rules.")
    else:
        progress(_step("extract", "No AI model is configured: reading requirement sentences, decisions, action items "
                                  "and questions with the offline rules", 0, len(parts)))
        for i, part in enumerate(parts):
            _check(cancelled)
            results.append(offline_extract(part))
            progress(_step("extract", f"Offline rules: part {i + 1} of {len(parts)} done", i + 1, len(parts)))
        engines.add("offline")
    progress(_step("compare", f"Checking proposals against {len(state.requirements)} existing requirement(s)"))
    merged = merge(t, results, parts, state)
    engine = "mixed" if len(engines) > 1 else engines.pop()
    if engine == "offline" and not merged["summary"]:
        mins = f" over {stamp(t.duration)}" if t.duration else ""
        merged["summary"] = (f"{len(t.turns)} turns from {', '.join(t.speakers) or 'unnamed speakers'}{mins}. "
                             "(No AI model: this is not a summary of the content.)")
    return {"engine": engine, "notes": notes,
            "file": {"name": t.name, "type": t.type, "turns": len(t.turns), "speakers": t.speakers,
                     "duration": stamp(t.duration), "chars": t.chars, "chunks": len(parts)},
            "reader": {"transcription": t.model, "model": model if "llm" in engines else ""}, **merged}


# --------------------------------------------------------------------------- accept
def accept_ops(items: list[dict[str, Any]], file: dict[str, Any], at: str) -> list[dict[str, Any]]:
    ops = []
    for it in items:
        src = {k: _clip(v, 300) for k, v in (it.get("source") or {}).items() if k in ("section", "timestamp") and v}
        src.update({"type": "meeting", "ref": _clip(file.get("name"), 200) or "meeting", "at": at})
        req = {k: it[k] for k in ("title", "description", "kind", "category", "priority", "actors",
                                  "acceptance_criteria") if k in it}
        ops.append({"op": "add_requirement", "requirement": {**req, "source": src}})
    return ops


def decision_fields(d: dict[str, Any], file: dict[str, Any]) -> dict[str, Any]:
    """A proposed ADR for a decision taken in the meeting."""
    who = f" by {d['decided_by']}" if d.get("decided_by") else ""
    when = f" at {d['timestamp']}" if d.get("timestamp") else ""
    context = f"Decided in the meeting {file.get('name') or 'meeting'}{who}{when}."
    if d.get("rationale"):
        context += f"\n\nRationale given: {d['rationale']}"
    return {"status": "proposed", "context": context, "decision": d.get("decision") or d["title"],
            "alternatives": [{"name": a[:120], "summary": "Discussed in the meeting"} for a in d.get("alternatives") or []
                             if a.strip()][:8]}


class RequirementIn(intake.ProposalIn):
    pass


class DecisionIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    decision: str = Field("", max_length=4000)
    rationale: str = Field("", max_length=4000)
    alternatives: list[str] = Field([], max_length=10)
    decided_by: str = Field("", max_length=80)
    timestamp: str = Field("", max_length=20)


class AcceptIn(BaseModel):
    file: dict[str, Any]
    requirements: list[RequirementIn] = Field([], max_length=intake.MAX_PROPOSALS)
    decisions: list[DecisionIn] = Field([], max_length=60)
    memory: list[intake.MemoryNoteIn] = Field([], max_length=120)


# --------------------------------------------------------------------------- routes
def register_routes(app: FastAPI, store: Any, *, authz: Any, state_or_404: Callable[[str, str], ProjectState],
                    commit_ops: Callable[..., Awaitable[tuple[ProjectState, list[str]]]],
                    broadcast: Callable[[str, str, dict[str, Any]], Awaitable[None]],
                    notify: Callable[..., None] | None = None) -> None:
    edit = authz.project("edit")
    running: set[asyncio.Future[Any]] = set()

    def log_outcome(pid: str, branch: str, actor: str, line: str) -> None:
        state = store.get_state(pid, branch)
        store.log_activity(pid, branch, actor, [line], state.rev if state else 0)

    def read_and_propose(pid: str, a: Any, name: str, body: bytes, state: ProjectState, add_to_kb: bool,
                         progress: Progress = _quiet, cancelled: Cancelled = _never) -> dict[str, Any]:
        try:
            t = read_transcript(name, body, a.org_id, progress, cancelled)
        except llm.LLMUnavailable as exc:
            raise IntakeError(str(exc), 415) from exc
        result = propose(t, state, a.org_id, progress=progress, cancelled=cancelled)
        if add_to_kb:
            _check(cancelled)
            progress(_step("knowledge", "Adding the transcript to the knowledge base"))
            try:
                doc = knowledge.ingest(store, pid, name=t.name, type=t.type, origin="meeting",
                                       chunks=transcript_chunks(t), chars=t.chars, actor=a.actor, org_id=a.org_id)
                result["knowledge"] = {"id": doc["id"], "chunks": doc["chunks"], "embedded": bool(doc["embedding_model"])}
                if doc.get("note"):
                    result["notes"].append(doc["note"])
            except ValueError as exc:
                result["notes"].append(f"Not added to the knowledge base: {exc}")
        return result

    @app.post("/api/projects/{pid}/meetings/import")
    async def import_meeting(pid: str, request: Request, filename: str = "meeting.txt", branch: str = "main",
                             knowledge: bool = False, a=Depends(edit)):
        state = state_or_404(pid, branch)
        name = os.path.basename(filename.replace("\\", "/"))[:200] or "meeting.txt"
        limit = intake.max_bytes()
        too_big = f"The file is larger than {limit / (1024 * 1024):g} MB."
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise HTTPException(413, too_big)
        data, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, too_big)
            data.append(chunk)
        body = b"".join(data)

        async def proposed(result: dict[str, Any]) -> None:
            log_outcome(pid, branch, a.actor, f"meeting {name}: proposed {len(result['requirements'])} requirement(s), "
                        f"{len(result['decisions'])} decision(s), {len(result['action_items'])} action item(s) "
                        f"({result['engine']})")
            await broadcast(pid, branch, {"type": "activity", "actor": a.actor})
            if result.get("knowledge"):
                await broadcast(pid, "main", {"type": "knowledge", "actor": a.actor})

        if "text/event-stream" in request.headers.get("accept", ""):
            return intake.stream_job(
                lambda progress, cancelled: read_and_propose(pid, a, name, body, state, knowledge, progress, cancelled),
                running=running, first=_step("upload", f"Uploaded {name} ({len(body) / 1024:,.0f} KB)"),
                on_done=proposed, failure="Something went wrong while reading the meeting.",
                on_error=lambda msg: log_outcome(pid, branch, a.actor, f"meeting {name} rejected: {msg}"),
                on_cancel=lambda: log_outcome(pid, branch, a.actor, f"meeting {name} cancelled"),
                what=f"importing meeting {name} into {pid}",
                job={"kind": "meeting_import", "project_id": pid, "branch": branch, "file": name,
                     "created_by": a.actor})
        try:
            result = await asyncio.to_thread(read_and_propose, pid, a, name, body, state, knowledge)
        except IntakeError as exc:
            log_outcome(pid, branch, a.actor, f"meeting {name} rejected: {exc}")
            raise HTTPException(exc.status, str(exc)) from exc
        await proposed(result)
        return result

    @app.post("/api/projects/{pid}/meetings/import/accept")
    async def accept_meeting(pid: str, body: AcceptIn, branch: str = "main", a=Depends(edit)):
        state_or_404(pid, branch)
        name = _clip(body.file.get("name"), 200) or "meeting"
        at = now()
        ops = accept_ops([r.model_dump() for r in body.requirements], body.file, at)
        summary: list[str] = []
        state = None
        if ops:
            state, summary = await commit_ops(pid, branch, ops, f"{a.actor} (meeting {name})")
        adrs = []
        for d in body.decisions:
            try:
                adrs.append(store.create_decision(pid, d.title, author=a.actor, **decision_fields(d.model_dump(), body.file)))
            except ValueError as exc:
                log.info("skipped a meeting decision: %s", exc)
        saved = []
        for m in body.memory:
            cat = m.category if m.category in MEMORY_CATEGORIES else "context"
            item = conversations.add_fact(store, pid, m.text, category=cat, source="meeting", created_by=a.actor)
            if item:
                saved.append(item)
        if saved:
            await broadcast(pid, branch, {"type": "memory", "actor": a.actor, "added": [m["text"] for m in saved]})
        if adrs:
            await broadcast(pid, branch, {"type": "decisions", "actor": a.actor})
        line = (f"meeting {name}: accepted {len(ops)} requirement(s), {len(adrs)} decision(s) as proposed ADRs"
                + (f", saved {len(saved)} note(s) to memory" if body.memory else ""))
        log_outcome(pid, branch, a.actor, line)
        if notify:
            notify(a.org_id, pid, "meeting", f"{a.actor} imported the meeting {name}", line, branch=branch)
        added = [r.id for r in (state.requirements.values() if state else []) if r.source.get("at") == at]
        return {"added": added, "summary": summary, "decisions": adrs, "memory": saved,
                **({"state": state.model_dump()} if state else {})}


def to_text(result: dict[str, Any]) -> str:  # eval helper: everything found, as one text
    lines = [f"Summary: {result['summary']}"]
    lines += [f"- requirement [{r['kind']}/{r['priority']}/{r['agreement']}] {r['title']}: {r['description']}"
              for r in result["requirements"]]
    lines += [f"- decision {d['title']}: {d['decision']} ({d['rationale']})"
              + (f"; alternatives: {', '.join(d['alternatives'])}" if d["alternatives"] else "")
              for d in result["decisions"]]
    lines += [f"- action {x['task']} (owner {x['owner'] or '?'}, due {x['due'] or '?'})" for x in result["action_items"]]
    lines += [f"- question {q['text']}" for q in result["open_questions"]]
    return "\n".join(lines)

