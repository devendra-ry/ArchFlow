"""Project knowledge base: retrieval-augmented answers over the team's own documents.

Documents (PRDs, RFPs, policies, runbooks, meeting transcripts) are uploaded to a project, read with the same
readers as document intake (PDF, Word, Markdown, text; WebVTT / SRT transcripts via ``meetings``), cut into
passages of about ``CHUNK_CHARS`` characters at section and paragraph boundaries (with a little overlap so a
sentence is never lost between two passages) and stored in ``kb_chunks`` with their section, page and, for
transcripts, the timestamp.

Retrieval is hybrid and works without any model:

* **keywords** — BM25 over the passages (always available, offline too);
* **vectors** — cosine similarity of embeddings (``llm.embed``: Gemini or OpenAI embeddings) when the org has an
  embedding model; the query is embedded with the model the index was built with;
* both rankings are merged with reciprocal-rank fusion, so an exact term (an error code, a product name) and a
  paraphrase both find their passage.

``answer`` runs the ``kb.answer`` agent on the best passages and returns an answer whose every claim cites a
passage ([K1], [K2] ...), the quoted evidence, and follow-up questions written by the model. Without a model it
returns the passages themselves (``engine: "offline"``). The project assistant (Ask) also gets the best passages
for each question as a ``<knowledge_base>`` block (``context_block``).

Passages are untrusted data: they are wrapped in ``<untrusted_data>`` and the prompts tell the model never to follow
instructions inside them. Personal data and secrets are replaced before passages are embedded (``llm.embed``) or
sent to a model (``llm._dispatch``).

Roles: anyone who can read the project searches and asks; developers and up add, re-index and remove documents.

=======  ==============================================  ==============================================
method   path                                            what
=======  ==============================================  ==============================================
GET      /api/projects/{pid}/knowledge                   documents + index status
POST     /api/projects/{pid}/knowledge                   raw file body, ?filename= → the stored document
DELETE   /api/projects/{pid}/knowledge/{doc_id}          remove a document and its passages
GET      /api/projects/{pid}/knowledge/{doc_id}/chunks   the passages of one document
POST     /api/projects/{pid}/knowledge/search            {query, limit} → ranked passages
POST     /api/projects/{pid}/knowledge/ask               {question} → cited answer
POST     /api/projects/{pid}/knowledge/reindex           embed documents that have no (or an outdated) index
=======  ==============================================  ==============================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
from collections import Counter, OrderedDict
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import agents, intake, llm

log = logging.getLogger("archflow.knowledge")

PROMPT = "kb.answer"
CHUNK_CHARS = 1200
OVERLAP_CHARS = 150
MAX_CHUNKS_PER_DOC = 4000
TOP_K = 6
CONTEXT_K = 4
RRF_K = 60
MIN_VECTOR_SCORE = 0.2
BM25_K1, BM25_B = 1.4, 0.75

ANSWER_SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "answerable": {"type": "boolean"},
    "answer": {"type": "string"},
    "citations": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "quote": {"type": "string"}}, "required": ["id", "quote"]}},
    "follow_ups": {"type": "array", "items": {"type": "string"}}},
    "required": ["answerable", "answer", "citations"]}


def max_docs() -> int:
    try:
        return max(1, int(os.environ.get("ARCHFLOW_KB_MAX_DOCS", "200")))
    except ValueError:
        return 200


# --------------------------------------------------------------------------- chunking
def _split_long(text: str, size: int) -> list[str]:
    """Paragraph-aligned pieces of at most ``size`` characters (a huge paragraph is cut at sentence ends)."""
    out: list[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:
            cut = max(para.rfind(". ", 0, size), para.rfind("\n", 0, size))
            cut = cut + 1 if cut > size // 3 else size
            if buf:
                out.append(buf)
                buf = ""
            out.append(para[:cut].strip())
            para = para[cut:].strip()
        if buf and len(buf) + len(para) + 2 > size:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out


def _tail(text: str, n: int) -> str:
    """The last ``n`` characters of a passage, starting at a word boundary (the overlap carried forward)."""
    if len(text) <= n:
        return text
    t = text[-n:]
    return t[t.find(" ") + 1:] if " " in t else t


def chunk_sections(sections: list[intake.Section], size: int = CHUNK_CHARS,
                   overlap: int = OVERLAP_CHARS) -> list[dict[str, Any]]:
    """Passages {section, page, locator, text} of a read document. Each passage stays inside one section; a long
    section's passages overlap by about ``overlap`` characters."""
    out: list[dict[str, Any]] = []
    for s in sections:
        pieces = _split_long(s.text, size)
        for i, piece in enumerate(pieces):
            text = (f"…{_tail(pieces[i - 1], overlap)}\n\n{piece}" if i and overlap else piece).strip()
            out.append({"section": s.heading, "page": s.page, "locator": "", "text": text})
    return out[:MAX_CHUNKS_PER_DOC]


# --------------------------------------------------------------------------- ranking
_STOP = intake._STOP - {"user", "users", "system"}


def tokens(text: str) -> list[str]:
    """Lower-case word tokens with a light plural strip; numbers and codes (e.g. 'p95', '99.95', 'err-42') kept."""
    out = []
    for w in re.findall(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*", text.lower()):
        if w in _STOP or len(w) < 2:
            continue
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def bm25(query: str, chunks: list[dict[str, Any]]) -> list[tuple[int, float]]:
    """(chunk index, score) for chunks sharing at least one query term, best first."""
    q = set(tokens(query))
    if not q or not chunks:
        return []
    docs = [Counter(tokens(f"{c.get('section', '')} {c['text']}")) for c in chunks]
    n = len(docs)
    avg = sum(sum(d.values()) for d in docs) / n or 1.0
    df = {t: sum(1 for d in docs if t in d) for t in q}
    scores: list[tuple[int, float]] = []
    for i, d in enumerate(docs):
        length = sum(d.values()) or 1
        s = 0.0
        for t in q:
            tf = d.get(t, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * length / avg))
        if s > 0:
            scores.append((i, s))
    return sorted(scores, key=lambda x: -x[1])


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def fuse(*rankings: list[tuple[int, float]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal-rank fusion of several rankings of the same items."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, (i, _) in enumerate(ranking):
            scores[i] = scores.get(i, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


def rank(query: str, chunks: list[dict[str, Any]], query_vector: list[float] | None = None,
         limit: int = TOP_K) -> tuple[list[dict[str, Any]], str]:
    """The best ``limit`` chunks for the query (copies with ``score``, ``keyword_rank`` and ``vector_score``) and
    the mode used: "hybrid" (keywords + vectors) or "keyword"."""
    lexical = bm25(query, chunks)
    vector: list[tuple[int, float]] = []
    if query_vector:
        sims = [(i, cosine(query_vector, c["embedding"])) for i, c in enumerate(chunks) if c.get("embedding")]
        vector = sorted([x for x in sims if x[1] >= MIN_VECTOR_SCORE], key=lambda x: -x[1])[:limit * 4]
    mode = "hybrid" if vector else "keyword"
    fused = fuse(lexical[:limit * 4], vector) if vector else lexical
    kw_rank = {i: r + 1 for r, (i, _) in enumerate(lexical)}
    vec = dict(vector)
    out = []
    for i, score in fused[:limit]:
        c = {k: v for k, v in chunks[i].items() if k != "embedding"}
        out.append({**c, "score": round(score, 4), "keyword_rank": kw_rank.get(i), "vector_score":
                    round(vec[i], 4) if i in vec else None})
    return out, mode


# --------------------------------------------------------------------------- index (store-backed)
class _ChunkCache:
    """The project's chunks, reloaded when its documents change (keyed by the document list's signature)."""

    def __init__(self, size: int = 32) -> None:
        self._data: OrderedDict[str, tuple[Any, list[dict[str, Any]]]] = OrderedDict()
        self._lock = threading.Lock()
        self.size = size

    def get(self, store: Any, pid: str) -> list[dict[str, Any]]:
        sig = tuple((d["id"], d["chunks"], d["embedding_model"]) for d in store.list_kb_documents(pid))
        with self._lock:
            hit = self._data.get(pid)
            if hit and hit[0] == sig:
                self._data.move_to_end(pid)
                return hit[1]
        chunks = store.list_kb_chunks(pid) if sig else []
        models = {d[0]: d[2] for d in sig}
        for c in chunks:
            c["embedding_model"] = models.get(c["document_id"], "")
        with self._lock:
            self._data[pid] = (sig, chunks)
            while len(self._data) > self.size:
                self._data.popitem(last=False)
        return chunks


_chunks = _ChunkCache()
_query_vectors: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_qv_lock = threading.Lock()


def _query_vector(query: str, model: str, org_id: str | None) -> list[float] | None:
    key = (model, query)
    with _qv_lock:
        if key in _query_vectors:
            _query_vectors.move_to_end(key)
            return _query_vectors[key]
    try:
        _, vecs = llm.embed([query], org_id, model=model)
    except llm.LLMUnavailable as exc:
        log.info("query embedding with %s failed (%s); keyword search only", model, exc)
        return None
    with _qv_lock:
        _query_vectors[key] = vecs[0]
        while len(_query_vectors) > 256:
            _query_vectors.popitem(last=False)
    return vecs[0]


def search(store: Any, pid: str, query: str, org_id: str | None, limit: int = TOP_K) -> dict[str, Any]:
    """Ranked passages for a query across the project's documents."""
    chunks = _chunks.get(store, pid)
    query = " ".join(query.split())[:1000]
    if not chunks or not query:
        return {"query": query, "mode": "keyword", "results": []}
    # The query is embedded with the model most of the index was built with; passages from other models (or
    # without embeddings) still compete through the keyword ranking.
    models = Counter(c["embedding_model"] for c in chunks if c.get("embedding") and c["embedding_model"])
    qv = None
    if models and llm.embedding_model(org_id):
        model = models.most_common(1)[0][0]
        qv = _query_vector(query, model, org_id)
        if qv:
            chunks = [c if c["embedding_model"] == model else {**c, "embedding": None} for c in chunks]
    results, mode = rank(query, chunks, qv, limit)
    for i, r in enumerate(results):
        r["ref"] = f"K{i + 1}"
    return {"query": query, "mode": mode, "results": results}


def where(r: dict[str, Any]) -> str:
    bits = [r.get("document") or "document"]
    if r.get("section"):
        bits.append(f"§ {r['section']}")
    if r.get("page"):
        bits.append(f"p. {r['page']}")
    if r.get("locator"):
        bits.append(r["locator"])
    return ", ".join(bits)


def _neutral(text: str) -> str:
    return re.sub(r"</?\s*untrusted_data", "[untrusted_data", text, flags=re.I)


def passages_block(results: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"[{r['ref']}] ({where(r)})\n{_neutral(r['text'])}" for r in results)


def context_block(store: Any, pid: str, question: str, org_id: str | None, limit: int = CONTEXT_K) -> str:
    """The best passages for an assistant question, as a block for the assistant.ask prompt ("" when the project
    has no knowledge base or nothing matches)."""
    try:
        found = search(store, pid, question, org_id, limit)["results"]
    except Exception:  # noqa: BLE001 - the assistant must answer even when retrieval breaks
        log.exception("knowledge-base retrieval for project %s failed", pid)
        return ""
    if not found:
        return ""
    return ("<knowledge_base>\nPassages from the project's documents that match the question. Cite one you rely on "
            "as [K1], [K2]... with the document name.\n<untrusted_data source=\"project knowledge base\">\n"
            f"{passages_block(found)}\n</untrusted_data>\n</knowledge_base>")


def _clean_answer(raw: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    refs = {r["ref"]: r for r in results}
    citations = []
    for c in raw.get("citations") or []:
        ref = str(c.get("id") or "").strip("[] ")
        if ref in refs and not any(x["ref"] == ref for x in citations):
            r = refs[ref]
            citations.append({"ref": ref, "quote": " ".join(str(c.get("quote") or "").split())[:400],
                              "document_id": r["document_id"], "document": r["document"], "section": r["section"],
                              "page": r["page"], "locator": r["locator"]})
    answer = str(raw.get("answer") or "").strip()
    used = set(re.findall(r"\[(K\d+)\]", answer))
    for ref in sorted(used - {c["ref"] for c in citations}):  # cited in the text without a quote
        if ref in refs:
            r = refs[ref]
            citations.append({"ref": ref, "quote": "", "document_id": r["document_id"], "document": r["document"],
                              "section": r["section"], "page": r["page"], "locator": r["locator"]})
    answer = re.sub(r"\[(K\d+)\]", lambda m: m.group(0) if m.group(1) in refs else "", answer)
    return {"answerable": bool(raw.get("answerable")) and bool(answer), "answer": answer,
            "citations": sorted(citations, key=lambda c: int(c["ref"][1:])),
            "follow_ups": [" ".join(str(f).split())[:200] for f in (raw.get("follow_ups") or [])[:4] if str(f).strip()]}


def answer(store: Any, pid: str, question: str, org_id: str | None, *, project_name: str = "",
           use_ai: bool | None = None, version: str | None = None) -> dict[str, Any]:
    """A cited answer from the knowledge base: {engine, mode, answer, answerable, citations, follow_ups, passages}."""
    found = search(store, pid, question, org_id, TOP_K)
    return answer_from(question, found["results"], found["mode"], org_id, project_name=project_name, use_ai=use_ai,
                       version=version)


def answer_from(question: str, results: list[dict[str, Any]], mode: str, org_id: str | None, *,
                project_name: str = "", use_ai: bool | None = None, version: str | None = None) -> dict[str, Any]:
    base = {"question": question, "mode": mode, "passages": results}
    if not results:
        return {**base, "engine": "offline", "answerable": False, "citations": [], "follow_ups": [],
                "answer": "Nothing in the knowledge base matches this question. Add the documents that cover it, "
                          "or rephrase it with the terms they use."}
    use_ai = llm.available(org_id) if use_ai is None else use_ai
    if use_ai:
        try:
            raw = agents.run_json(PROMPT, ANSWER_SCHEMA, org_id=org_id, version=version, question=question,
                                  variables={"project": project_name or "this project", "question": question,
                                             "passages": passages_block(results)})
            return {**base, "engine": "llm", **_clean_answer(raw, results)}
        except llm.LLMUnavailable as exc:
            log.info("kb.answer fell back to the passages: %s", exc)
    return {**base, "engine": "offline", "answerable": True, "follow_ups": [],
            "answer": "No AI model is available, so here are the passages that match best:\n\n"
                      + "\n".join(f"- [{r['ref']}] {where(r)}: {' '.join(r['text'].split())[:280]}" for r in results[:3]),
            "citations": [{"ref": r["ref"], "quote": "", "document_id": r["document_id"], "document": r["document"],
                           "section": r["section"], "page": r["page"], "locator": r["locator"]} for r in results[:3]]}


# --------------------------------------------------------------------------- ingest
def embed_chunks(chunks: list[dict[str, Any]], org_id: str | None) -> tuple[str, str]:
    """Add ``embedding`` to each chunk in place when an embedding model is available: (model, note)."""
    if not chunks or not llm.embedding_model(org_id):
        return "", ""
    try:
        model, vectors = llm.embed([f"{c.get('section') or ''}\n{c['text']}".strip() for c in chunks], org_id)
    except llm.LLMUnavailable as exc:
        log.info("embedding failed, keyword index only: %s", exc)
        return "", f"Embedding failed ({exc}); the document is searchable by keywords. Re-index later."
    for c, v in zip(chunks, vectors):
        c["embedding"] = v
    return model, ""


def ingest(store: Any, pid: str, *, name: str, type: str, origin: str, chunks: list[dict[str, Any]],
           chars: int, actor: str, org_id: str | None) -> dict[str, Any]:
    """Store a document's passages (embedded when possible). Returns the document plus ``note``."""
    if len(store.list_kb_documents(pid)) >= max_docs():
        raise ValueError(f"The knowledge base holds at most {max_docs()} documents: remove one first.")
    if not chunks:
        raise ValueError("No text found in the document.")
    model, note = embed_chunks(chunks, org_id)
    doc = store.add_kb_document(pid, name=name[:200], type=type, origin=origin, chars=chars, embedding_model=model,
                                created_by=actor, chunks=chunks)
    return {**doc, "note": note}


def read_upload(name: str, data: bytes, org_id: str | None) -> tuple[str, int, list[dict[str, Any]]]:
    """(type, characters, chunks) for an uploaded file: a document (intake readers) or a transcript (meetings)."""
    from . import meetings

    if meetings.is_transcript(name, data):
        t = meetings.read_transcript(name, data, org_id)
        return t.type, t.chars, meetings.transcript_chunks(t)
    doc = intake.read_document(name, data)
    return doc.type, doc.chars, chunk_sections(doc.sections)


def reindex(store: Any, pid: str, org_id: str | None) -> dict[str, Any]:
    """Embed every document whose passages have no embeddings, or embeddings from another model than the current
    one. Returns {model, reindexed, failed}."""
    picked = llm.embedding_model(org_id)
    if not picked:
        raise llm.LLMUnavailable("No embedding model is available: add a Gemini or OpenAI key in AI settings.")
    model = picked[0]
    done, failed = 0, []
    for d in store.list_kb_documents(pid):
        if d["embedding_model"] == model:
            continue
        chunks = store.list_kb_chunks(pid, d["id"])
        try:
            used, vectors = llm.embed([f"{c['section']}\n{c['text']}".strip() for c in chunks], org_id)
        except llm.LLMUnavailable as exc:
            failed.append({"id": d["id"], "name": d["name"], "error": str(exc)})
            continue
        store.set_kb_embeddings(d["id"], used, {c["id"]: v for c, v in zip(chunks, vectors)})
        done += 1
    return {"model": model, "reindexed": done, "failed": failed}


def status(store: Any, pid: str, org_id: str | None) -> dict[str, Any]:
    docs = store.list_kb_documents(pid)
    picked = llm.embedding_model(org_id)
    model = picked[0] if picked else ""
    return {"documents": docs, "embedding_model": model,
            "stale": sum(1 for d in docs if model and d["embedding_model"] != model),
            "chunks": sum(d["chunks"] for d in docs), "max_documents": max_docs(),
            "ai_available": llm.available(org_id)}


# --------------------------------------------------------------------------- routes
class SearchIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    limit: int = Field(TOP_K, ge=1, le=20)


class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


def register_routes(app: FastAPI, store: Any, *, authz: Any,
                    broadcast: Callable[[str, str, dict[str, Any]], Any] | None = None) -> None:
    read, edit = authz.project("read"), authz.project("edit")

    def log_line(pid: str, actor: str, line: str) -> None:
        state = store.get_state(pid, "main")
        store.log_activity(pid, "main", actor, [line], state.rev if state else 0)

    @app.get("/api/projects/{pid}/knowledge")
    def kb_list(pid: str, a=Depends(read)):
        return status(store, pid, a.org_id)

    @app.post("/api/projects/{pid}/knowledge", status_code=201)
    async def kb_upload(pid: str, request: Request, filename: str = "document.txt", a=Depends(edit)):
        name = os.path.basename(filename.replace("\\", "/"))[:200] or "document.txt"
        limit = intake.max_bytes()
        data, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"The file is larger than {limit / (1024 * 1024):g} MB.")
            data.append(chunk)

        def run() -> dict[str, Any]:
            typ, chars, chunks = read_upload(name, b"".join(data), a.org_id)
            return ingest(store, pid, name=name, type=typ, origin="upload", chunks=chunks, chars=chars,
                          actor=a.actor, org_id=a.org_id)

        try:
            doc = await asyncio.to_thread(run)
        except intake.IntakeError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except llm.LLMUnavailable as exc:  # e.g. an audio file without a speech-to-text model
            raise HTTPException(415, str(exc)) from exc
        log_line(pid, a.actor, f"added {name} to the knowledge base ({doc['chunks']} passages"
                 + (", embedded" if doc["embedding_model"] else ", keywords only") + ")")
        if broadcast:
            await broadcast(pid, "main", {"type": "knowledge", "actor": a.actor})
        return doc

    @app.delete("/api/projects/{pid}/knowledge/{doc_id}", status_code=204)
    async def kb_delete(pid: str, doc_id: str, a=Depends(edit)):
        doc = store.get_kb_document(pid, doc_id)
        if not doc or not store.delete_kb_document(pid, doc_id):
            raise HTTPException(404, "document not found")
        log_line(pid, a.actor, f"removed {doc['name']} from the knowledge base")
        if broadcast:
            await broadcast(pid, "main", {"type": "knowledge", "actor": a.actor})

    @app.get("/api/projects/{pid}/knowledge/{doc_id}/chunks")
    def kb_chunks(pid: str, doc_id: str, a=Depends(read)):
        doc = store.get_kb_document(pid, doc_id)
        if not doc:
            raise HTTPException(404, "document not found")
        return {**doc, "passages": [{k: v for k, v in c.items() if k != "embedding"}
                                    for c in store.list_kb_chunks(pid, doc_id)]}

    @app.post("/api/projects/{pid}/knowledge/search")
    async def kb_search(pid: str, body: SearchIn, a=Depends(read)):
        return await asyncio.to_thread(search, store, pid, body.query, a.org_id, body.limit)

    @app.post("/api/projects/{pid}/knowledge/ask")
    async def kb_ask(pid: str, body: AskIn, a=Depends(read)):
        return await asyncio.to_thread(answer, store, pid, body.question, a.org_id,
                                       project_name=a.project["name"])

    @app.post("/api/projects/{pid}/knowledge/reindex")
    async def kb_reindex(pid: str, a=Depends(edit)):
        try:
            out = await asyncio.to_thread(reindex, store, pid, a.org_id)
        except llm.LLMUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc
        if out["reindexed"]:
            log_line(pid, a.actor, f"re-indexed {out['reindexed']} knowledge-base document(s) with {out['model']}")
        return {**out, **status(store, pid, a.org_id)}


def to_json(result: dict[str, Any]) -> str:  # used by the eval suite to show what was retrieved
    return json.dumps([{k: r[k] for k in ("ref", "document", "section", "text")} for r in result["passages"]])
