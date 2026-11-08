"""Conversations and project memory: the AI remembers what was said, and what the team has settled.

* **Conversations** are chat threads stored per project on the server, so they survive reloads and teammates
  see them. ``assistant`` threads hold Ask questions and canvas edit commands; ``design`` threads hold the
  guided design chat (chat.py). A design chat follows the draft branch it creates.
* **Context window.** Every AI call gets the running summary of older turns plus the newest messages verbatim.
  Once more than SUMMARIZE_AFTER messages have piled up past the summary, all but the last RECENT_MESSAGES are
  folded into it (LLM "fast" group, or an extractive digest offline). That runs after the reply is sent.
* **Project memory** holds durable facts ("Cloud: AWS", "HIPAA applies") that every AI call sees. People add,
  edit, pin and delete them. The AI proposes up to MAX_FACTS_PER_TURN new ones from each Ask/edit exchange
  (LLM only), and answers to design-chat questions are recorded as they are given. Pinned facts are always in
  the prompt; the rest go in newest first until MEMORY_PROMPT_CHARS is used.

Routes (``register_routes``): reading needs any role, so viewers can ask questions; edit commands, the design
chat and changing memory need developer or above; renaming, clearing or deleting a thread needs its creator
or an admin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any, Awaitable, Callable

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import agents, ai, cloud_docs, llm
from .store import CONVERSATION_KINDS, MEMORY_CATEGORIES

log = logging.getLogger("archflow.conversations")

AI_AUTHOR = "ArchFlow AI"
RECENT_MESSAGES = 12          # newest messages always sent verbatim
SUMMARIZE_AFTER = 20          # fold older messages into the summary past this many unsummarized messages
MAX_VERBATIM = 40             # hard cap if the summarizer has fallen behind
SUMMARY_MAX_CHARS = 4000
MESSAGE_PROMPT_CHARS = 2000   # one message, in a transcript
COMMAND_CONTEXT_MESSAGES = 8  # recent messages shown to the edit-command model
MEMORY_MAX_ITEMS = 100
MEMORY_PROMPT_CHARS = 3000
MAX_FACTS_PER_TURN = 3
FACT_MAX_CHARS = 240

# Prompts: prompts/memory.summarize.yaml, memory.extract.yaml and assistant.ask.yaml (see agents.prompts).

_FACTS_SCHEMA = {"type": "object", "properties": {"facts": {"type": "array", "items": {
    "type": "object", "properties": {
        "text": {"type": "string"},
        "category": {"type": "string", "enum": list(MEMORY_CATEGORIES)}},
    "required": ["text", "category"]}}}, "required": ["facts"]}

_summarize_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


# --------------------------------------------------------------------------- text helpers
def title_from(text: str, limit: int = 60) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def message_text(m: dict[str, Any]) -> str:
    """One stored message as plain text for a model prompt."""
    data = m.get("data") or {}
    content = (m.get("content") or "").strip()
    mode = m.get("mode")
    if m["role"] == "user":
        return f"[Canvas edit request] {content}" if mode == "command" else content
    parts = [content] if content else []
    if mode == "command":
        if data.get("summary"):
            parts.append("Changes made: " + "; ".join(data["summary"]))
        if data.get("questions"):
            parts.append("Asked: " + " | ".join(data["questions"]))
        if data.get("error"):
            parts.append(f"(The edit failed: {data['error']})")
    elif mode == "design":
        card = data.get("card") or {}
        if card.get("type") == "question":
            parts.append(f"Asked: {card.get('question', '')} (options: "
                         + "; ".join(o.get("label", "") for o in card.get("options") or []) + ")")
        elif card.get("type") == "architectures":
            parts.append("Proposed architectures: " + ", ".join(o.get("name", "") for o in card.get("options") or []))
    elif data.get("error"):
        parts.append(f"(No answer: {data['error']})")
    return "\n".join(parts)


def chat_messages(msgs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Stored messages → alternating user/assistant chat turns starting with the user (what providers expect)."""
    out: list[dict[str, str]] = []
    for m in msgs:
        text = _clip(message_text(m), MESSAGE_PROMPT_CHARS * 2)
        if not text:
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n\n" + text
        elif out or m["role"] == "user":
            out.append({"role": m["role"], "content": text})
    return out


def transcript(msgs: list[dict[str, Any]]) -> str:
    lines = []
    for m in msgs:
        text = _clip(message_text(m), MESSAGE_PROMPT_CHARS)
        if text:
            who = "ArchFlow" if m["role"] == "assistant" else f"User ({m.get('author') or 'someone'})"
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- memory
def memory_block(store: Any, pid: str) -> str:
    """The project memory for a prompt: pinned facts first, then newest, within MEMORY_PROMPT_CHARS."""
    lines, used = [], 0
    for item in store.list_memory(pid):
        line = f"- [{item['category']}] {item['text']}"
        if used + len(line) > MEMORY_PROMPT_CHARS and not item["pinned"]:
            continue
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return "<project_memory>\n" + "\n".join(lines) + "\n</project_memory>"


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def is_duplicate(text: str, existing: list[dict[str, Any]]) -> bool:
    n = _norm(text)
    if not n:
        return True
    for item in existing:
        e = _norm(item["text"])
        if n == e or (min(len(n), len(e)) >= 12 and (n in e or e in n)):
            return True
    return False


def add_fact(store: Any, pid: str, text: str, *, category: str = "fact", pinned: bool = False, source: str = "user",
             conversation_id: str | None = None, created_by: str = "anonymous") -> dict[str, Any] | None:
    """Add a fact unless it duplicates one already stored or memory is full; returns it, or None if skipped."""
    text = _clip(text, FACT_MAX_CHARS)
    existing = store.list_memory(pid)
    if is_duplicate(text, existing) or len(existing) >= MEMORY_MAX_ITEMS:
        return None
    return store.add_memory(pid, text, category=category if category in MEMORY_CATEGORIES else "fact",
                            pinned=pinned, source=source, conversation_id=conversation_id, created_by=created_by)


def learn_from_exchange(store: Any, pid: str, cid: str, user_text: str, assistant_text: str,
                        org_id: str | None) -> list[dict[str, Any]]:
    """Ask the model for durable facts in one exchange and store the new ones (LLM only; errors are logged)."""
    if len((user_text or "").strip()) < 12 or not llm.available(org_id):
        return []
    existing = store.list_memory(pid)
    try:
        data = agents.run_json("memory.extract", _FACTS_SCHEMA, org_id=org_id,
                               question=f"Remember from: {_clip(user_text, 200)}", variables={
            "existing": "\n".join(f"- {m['text']}" for m in existing) or "(empty)",
            "user_text": _clip(user_text, 3000), "assistant_text": _clip(assistant_text, 3000)})
    except (llm.LLMUnavailable, llm.LLMRefused) as exc:
        log.info("memory extraction skipped: %s", type(exc).__name__)
        return []
    added = []
    for f in (data.get("facts") or [])[:MAX_FACTS_PER_TURN]:
        item = add_fact(store, pid, str(f.get("text", "")), category=str(f.get("category", "fact")), source="ai",
                        conversation_id=cid, created_by=AI_AUTHOR)
        if item:
            added.append(item)
    return added


def learn_design_answer(store: Any, pid: str, cid: str, card: dict[str, Any], labels: list[str],
                        actor: str) -> dict[str, Any] | None:
    """Record an answer to a design-chat question ("Which cloud? → AWS"); a new answer to the same question
    replaces the old fact."""
    question = (card.get("question") or "").strip().rstrip("?").strip()
    labels = [x.strip() for x in labels if x and x.strip()]
    if not question or not labels:
        return None
    text = _clip(f"{question}: {', '.join(labels)}", FACT_MAX_CHARS)
    prefix = _norm(question)
    for item in store.list_memory(pid):
        if item["source"] == "design-chat" and _norm(item["text"].split(":", 1)[0]) == prefix:
            return store.update_memory(pid, item["id"], {"text": text})
    return add_fact(store, pid, text, category="decision", source="design-chat", conversation_id=cid, created_by=actor)


# --------------------------------------------------------------------------- context window
def window(store: Any, conv: dict[str, Any], before_seq: int | None = None) -> tuple[str, list[dict[str, Any]]]:
    """(summary, verbatim messages) for the next AI call; ``before_seq`` leaves out the message being answered."""
    msgs = store.list_messages(conv["id"], after_seq=conv.get("summarized_seq") or 0)
    if before_seq is not None:
        msgs = [m for m in msgs if m["seq"] < before_seq]
    return conv.get("summary") or "", msgs[-MAX_VERBATIM:]


def summary_block(summary: str) -> str:
    return f"<conversation_summary>\n{summary}\n</conversation_summary>" if summary.strip() else ""


def ask_variables(project_context: str, memory: str, summary: str, knowledge: str = "") -> dict[str, str]:
    """Variables of the assistant.ask prompt's system blocks (project data, memory, conversation summary and the
    knowledge-base passages that match the question)."""
    return {"project_context": project_context, "memory": memory, "summary": summary.strip(), "knowledge": knowledge}


def command_context(store: Any, pid: str, conv: dict[str, Any] | None) -> str:
    """Memory, summary and the last few turns, for the edit-command model."""
    parts = [memory_block(store, pid)]
    if conv:
        summary, msgs = window(store, conv)
        parts.append(summary_block(summary))
        recent = transcript(msgs[-COMMAND_CONTEXT_MESSAGES:])
        if recent:
            parts.append(f"<recent_conversation>\n{recent}\n</recent_conversation>")
    return "\n\n".join(p for p in parts if p)


def _digest(summary: str, fold: list[dict[str, Any]]) -> str:
    """Offline summary: the old summary plus one clipped line per folded message, newest kept when too long."""
    lines = [f"- {'ArchFlow' if m['role'] == 'assistant' else 'User'}: {_clip(message_text(m), 200)}"
             for m in fold if message_text(m)]
    text = "\n".join(x for x in [summary.strip(), *lines] if x)
    return text if len(text) <= SUMMARY_MAX_CHARS else "…" + text[-(SUMMARY_MAX_CHARS - 1):]


def maybe_summarize(store: Any, cid: str, org_id: str | None) -> bool:
    """Fold old messages into the running summary once enough have accumulated. Returns True if it did."""
    with _locks_guard:
        lock = _summarize_locks.setdefault(cid, threading.Lock())
    if not lock.acquire(blocking=False):
        return False  # another request is already summarizing this conversation
    try:
        conv = store.get_conversation(cid)
        if not conv:
            return False
        msgs = store.list_messages(cid, after_seq=conv["summarized_seq"])
        if len(msgs) <= SUMMARIZE_AFTER:
            return False
        fold = msgs[:-RECENT_MESSAGES]
        summary = None
        if llm.available(org_id):
            try:
                summary = agents.run_text("memory.summarize", org_id=org_id, question="Summarize older turns",
                                          variables={"summary": conv["summary"] or "(none)", "turns": transcript(fold)})
            except (llm.LLMUnavailable, llm.LLMRefused) as exc:
                log.info("summary via LLM failed (%s); using the offline digest", type(exc).__name__)
        summary = _clip(summary, SUMMARY_MAX_CHARS) if summary and summary.strip() else _digest(conv["summary"], fold)
        store.update_conversation(cid, {"summary": summary, "summarized_seq": fold[-1]["seq"]})
        return True
    finally:
        lock.release()


def after_turn(store: Any, pid: str, cid: str, org_id: str | None, learn: bool = True) -> list[dict[str, Any]]:
    """Housekeeping after a reply (runs after the response): summarize, then learn facts from the last exchange."""
    with llm.usage_scope(feature="memory"):
        return _after_turn(store, pid, cid, org_id, learn)


def _after_turn(store: Any, pid: str, cid: str, org_id: str | None, learn: bool) -> list[dict[str, Any]]:
    try:
        maybe_summarize(store, cid, org_id)
    except Exception:  # noqa: BLE001 - never fail a finished turn
        log.exception("summarizing conversation %s failed", cid)
    if not learn:
        return []
    msgs = store.list_messages(cid)[-2:]
    if len(msgs) < 2 or msgs[0]["role"] != "user" or msgs[1]["role"] != "assistant" or msgs[1]["data"].get("error"):
        return []
    try:
        return learn_from_exchange(store, pid, cid, msgs[0]["content"], message_text(msgs[1]), org_id)
    except Exception:  # noqa: BLE001
        log.exception("learning from conversation %s failed", cid)
        return []


# --------------------------------------------------------------------------- design chat (chat.py) persistence
def design_turns(store: Any, cid: str) -> list[dict[str, Any]]:
    """Stored design-chat messages as the turn dicts chat.next_turn expects."""
    return [{"role": m["role"], "content": m["content"],
             **{k: m["data"][k] for k in ("card", "answer", "applied") if k in m["data"]}}
            for m in store.list_messages(cid)]


def add_design_turn(store: Any, cid: str, turn: dict[str, Any], author: str, mode: str | None = None) -> dict[str, Any]:
    """Store one design-chat turn; ``note`` marks bookkeeping turns such as "Use X" / "Applied X as v3"."""
    data = {k: turn[k] for k in ("card", "answer", "applied", "engine") if turn.get(k) is not None}
    return store.add_message(cid, turn["role"], mode or ("note" if "applied" in data else "design"),
                             str(turn.get("content") or ""), data, AI_AUTHOR if turn["role"] == "assistant" else author)


# --------------------------------------------------------------------------- request bodies
class ConversationIn(BaseModel):
    kind: str = Field("assistant", pattern="^(assistant|design)$")
    title: str = Field("", max_length=120)
    # Import an earlier conversation (e.g. a design chat kept in the browser before chats were shared).
    messages: list[dict[str, Any]] = Field([], max_length=200)


class ConversationPatch(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class AskTurnIn(BaseModel):
    question: str = Field(min_length=1, max_length=8000)


class CommandTurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    selection: list[str] = []


class MemoryIn(BaseModel):
    text: str = Field(min_length=1, max_length=FACT_MAX_CHARS)
    category: str = Field("fact", pattern="^(" + "|".join(MEMORY_CATEGORIES) + ")$")
    pinned: bool = False


class MemoryPatch(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=FACT_MAX_CHARS)
    category: str | None = Field(None, pattern="^(" + "|".join(MEMORY_CATEGORIES) + ")$")
    pinned: bool | None = None


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


# --------------------------------------------------------------------------- routes
def register_routes(app: FastAPI, store: Any, *, authz: Any,
                    project_context: Callable[[str, str], str],
                    run_command: Callable[..., Awaitable[dict[str, Any]]],
                    broadcast: Callable[[str, str, dict[str, Any]], Awaitable[None]]) -> None:
    """``project_context(pid, branch)`` is the project block for Ask (model, health, versions, activity);
    ``run_command(pid, branch, text, selection, actor, context)`` applies an AI edit (main.run_ai_command)."""
    read, edit = authz.project("read"), authz.project("edit")
    running: set[asyncio.Task] = set()  # answer producers outlive a disconnected client, so they still save

    def conv_or_404(pid: str, cid: str) -> dict[str, Any]:
        conv = store.get_conversation(cid)
        if not conv or conv["project_id"] != pid:
            raise HTTPException(404, "conversation not found")
        return conv

    def check_owner(conv: dict[str, Any], a: Any) -> None:
        if conv.get("created_by_id") != a.user["id"] and not a.can("admin"):
            raise HTTPException(403, "Only the person who started this conversation or an admin can change it.")

    def state_or_404(pid: str, branch: str) -> None:
        if store.get_state(pid, branch) is None:
            raise HTTPException(404, f"branch '{branch}' not found")

    def name_thread(conv: dict[str, Any], text: str) -> None:
        if not conv["title"]:
            store.update_conversation(conv["id"], {"title": title_from(text)})

    async def housekeeping(pid: str, branch: str, cid: str, org_id: str | None, actor: str, learn: bool) -> None:
        await broadcast(pid, branch, {"type": "conversation", "id": cid, "actor": actor})
        added = await asyncio.to_thread(after_turn, store, pid, cid, org_id, learn)
        if added:
            await broadcast(pid, branch, {"type": "memory", "actor": AI_AUTHOR, "added": [m["text"] for m in added]})

    # ---- threads ----
    @app.get("/api/projects/{pid}/conversations")
    def list_conversations(pid: str, kind: str | None = None, limit: int = 50, a=Depends(read)):
        if kind and kind not in CONVERSATION_KINDS:
            raise HTTPException(422, f"kind must be one of {', '.join(CONVERSATION_KINDS)}")
        return store.list_conversations(pid, kind, max(1, min(limit, 200)))

    @app.post("/api/projects/{pid}/conversations", status_code=201)
    def create_conversation(pid: str, body: ConversationIn, branch: str = "main", a=Depends(read)):
        if body.kind == "design" and not a.can("edit"):
            raise HTTPException(403, f"Your role ({a.role}) cannot use the design chat; it needs developer or above.")
        state_or_404(pid, branch)
        conv = store.create_conversation(pid, branch, body.kind, body.title.strip(), a.actor, a.user["id"])
        for turn in body.messages:
            if turn.get("role") not in ("user", "assistant") or not (turn.get("content") or turn.get("card")):
                continue
            if body.kind == "design":
                add_design_turn(store, conv["id"], turn, a.actor)
            else:
                store.add_message(conv["id"], turn["role"], "ask", str(turn.get("content") or ""), {},
                                  AI_AUTHOR if turn["role"] == "assistant" else a.actor)
        if not body.title.strip():
            first = next((t.get("content") for t in body.messages if t.get("role") == "user" and t.get("content")), "")
            if first:
                store.update_conversation(conv["id"], {"title": title_from(first)})
        return store.get_conversation(conv["id"])

    @app.get("/api/projects/{pid}/conversations/{cid}")
    def get_conversation(pid: str, cid: str, a=Depends(read)):
        conv = conv_or_404(pid, cid)
        return {**conv, "messages": store.list_messages(cid)}

    @app.patch("/api/projects/{pid}/conversations/{cid}")
    def rename_conversation(pid: str, cid: str, body: ConversationPatch, a=Depends(read)):
        conv = conv_or_404(pid, cid)
        check_owner(conv, a)
        return store.update_conversation(cid, {"title": body.title.strip()})

    @app.delete("/api/projects/{pid}/conversations/{cid}", status_code=204)
    def delete_conversation(pid: str, cid: str, a=Depends(read)):
        check_owner(conv_or_404(pid, cid), a)
        store.delete_conversation(cid)

    @app.delete("/api/projects/{pid}/conversations/{cid}/messages", status_code=204)
    def clear_conversation(pid: str, cid: str, a=Depends(read)):
        check_owner(conv_or_404(pid, cid), a)
        store.delete_messages_from(cid, 1)

    # ---- Ask (streamed) ----
    def stream_answer(pid: str, branch: str, conv: dict[str, Any], question: str, user_msg: dict[str, Any],
                      a: Any, background: BackgroundTasks) -> StreamingResponse:
        summary, history = window(store, conv, before_seq=user_msg["seq"])
        from . import knowledge

        variables = ask_variables(project_context(pid, branch), memory_block(store, pid), summary,
                                  knowledge.context_block(store, pid, question, a.org_id))
        messages = chat_messages(history) + [{"role": "user", "content": question}]
        if len(messages) > 1 and messages[-2]["role"] == "user":  # an earlier question that got no answer
            messages[-2:] = [{"role": "user", "content": messages[-2]["content"] + "\n\n" + question}]
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def produce() -> None:
            parts: list[str] = []
            error = ""
            try:
                docs = cloud_docs.DocsRequest(query=question, about=f"{variables['project_context']}\n{summary}")
                for chunk in agents.stream_text(ai.ASK_PROMPT, variables=variables, history=messages[:-1],
                                                user=messages[-1]["content"], org_id=a.org_id,
                                                max_tokens=ai.MAX_TOKENS, docs=docs):
                    parts.append(chunk)
                    loop.call_soon_threadsafe(queue.put_nowait, ("delta", chunk))
            except llm.LLMRefused:
                error = "The model declined to answer this question."
            except llm.LLMUnavailable as exc:
                error = str(exc)
            except Exception:  # noqa: BLE001 - reported to the user, details logged
                log.exception("answering in conversation %s failed", conv["id"])
                error = "Something went wrong while answering. Try again."
            text = "".join(parts).strip()
            if not text and not error:
                error = "The model returned an empty answer. Try again."
            msg = store.add_message(conv["id"], "assistant", "ask", text,
                                    {"engine": "llm", **({"error": error} if error else {})}, AI_AUTHOR)
            loop.call_soon_threadsafe(queue.put_nowait, ("done", msg))

        task = asyncio.ensure_future(asyncio.to_thread(produce))
        running.add(task)
        task.add_done_callback(running.discard)

        async def events():
            yield _sse("start", {"user_message": user_msg})
            while True:
                kind, payload = await queue.get()
                if kind == "delta":
                    yield _sse("delta", {"text": payload})
                else:
                    yield _sse("done", {"message": payload})
                    return

        background.add_task(housekeeping, pid, branch, conv["id"], a.org_id, a.actor, True)
        return StreamingResponse(events(), media_type="text/event-stream", background=background,
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def require_ai(a: Any) -> None:
        if not llm.available(a.org_id):
            raise HTTPException(503, "No LLM is configured. Set GEMINI_API_KEY (or ANTHROPIC_API_KEY / "
                                     "OPENAI_API_KEY), or add your organization's key in AI settings, to use "
                                     "the project assistant.")

    @app.post("/api/projects/{pid}/conversations/{cid}/ask")
    async def ask(pid: str, cid: str, body: AskTurnIn, background: BackgroundTasks, branch: str = "main",
                  a=Depends(read)):
        conv = conv_or_404(pid, cid)
        if conv["kind"] != "assistant":
            raise HTTPException(409, "Questions go to an assistant conversation, not a design chat.")
        state_or_404(pid, branch)
        require_ai(a)
        question = body.question.strip()
        user_msg = store.add_message(cid, "user", "ask", question, {}, a.actor)
        name_thread(conv, question)
        return stream_answer(pid, branch, store.get_conversation(cid), question, user_msg, a, background)

    @app.post("/api/projects/{pid}/conversations/{cid}/regenerate")
    async def regenerate(pid: str, cid: str, background: BackgroundTasks, branch: str = "main", a=Depends(read)):
        """Answer the last question again (replaces the last answer)."""
        conv = conv_or_404(pid, cid)
        state_or_404(pid, branch)
        msgs = store.list_messages(cid)
        last_q = next((m for m in reversed(msgs) if m["role"] == "user"), None)
        if not last_q or last_q["mode"] != "ask":
            raise HTTPException(409, "Only the answer to the latest question can be regenerated.")
        require_ai(a)
        store.delete_messages_from(cid, last_q["seq"] + 1)
        return stream_answer(pid, branch, store.get_conversation(cid), last_q["content"], last_q, a, background)

    # ---- canvas edits ----
    @app.post("/api/projects/{pid}/conversations/{cid}/command")
    async def command(pid: str, cid: str, body: CommandTurnIn, background: BackgroundTasks, branch: str = "main",
                      a=Depends(edit)):
        conv = conv_or_404(pid, cid)
        if conv["kind"] != "assistant":
            raise HTTPException(409, "Edit commands go to an assistant conversation, not a design chat.")
        state_or_404(pid, branch)
        text = body.text.strip()
        context = command_context(store, pid, conv)  # before the new message: it is the request itself
        user_msg = store.add_message(cid, "user", "command", text, {"selection": body.selection}, a.actor)
        name_thread(conv, text)
        try:
            result = await run_command(pid, branch, text, body.selection, a.actor, context)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            result = {"applied": False, "error": exc.detail, "explanation": "", "questions": [], "engine": "llm"}
        data = {k: result[k] for k in ("applied", "summary", "questions", "question_options", "engine", "error")
                if result.get(k) is not None}
        msg = store.add_message(cid, "assistant", "command", result.get("explanation") or "", data, AI_AUTHOR)
        background.add_task(housekeeping, pid, branch, cid, a.org_id, a.actor, True)
        return {"user_message": user_msg, "message": msg, "state": result.get("state")}

    # ---- project memory ----
    @app.get("/api/projects/{pid}/memory")
    def list_memory(pid: str, a=Depends(read)):
        return store.list_memory(pid)

    @app.post("/api/projects/{pid}/memory", status_code=201)
    async def add_memory(pid: str, body: MemoryIn, branch: str = "main", a=Depends(edit)):
        existing = store.list_memory(pid)
        if len(existing) >= MEMORY_MAX_ITEMS:
            raise HTTPException(409, f"Project memory is full ({MEMORY_MAX_ITEMS} facts). Delete some first.")
        if is_duplicate(body.text, existing):
            raise HTTPException(409, "Project memory already has this fact.")
        item = store.add_memory(pid, body.text, category=body.category, pinned=body.pinned, source="user",
                                created_by=a.actor)
        await broadcast(pid, branch, {"type": "memory", "actor": a.actor})
        return item

    @app.patch("/api/projects/{pid}/memory/{mid}")
    async def update_memory(pid: str, mid: str, body: MemoryPatch, branch: str = "main", a=Depends(edit)):
        if not store.get_memory(pid, mid):
            raise HTTPException(404, "memory item not found")
        item = store.update_memory(pid, mid, body.model_dump(exclude_none=True))
        await broadcast(pid, branch, {"type": "memory", "actor": a.actor})
        return item

    @app.delete("/api/projects/{pid}/memory/{mid}", status_code=204)
    async def delete_memory(pid: str, mid: str, branch: str = "main", a=Depends(edit)):
        if not store.delete_memory(pid, mid):
            raise HTTPException(404, "memory item not found")
        await broadcast(pid, branch, {"type": "memory", "actor": a.actor})
