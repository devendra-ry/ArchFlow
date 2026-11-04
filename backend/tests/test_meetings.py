"""Meeting intake: WebVTT / SRT / text / Word transcripts, offline and (faked) AI extraction of requirements,
decisions, action items and questions, accepting them as requirements with provenance, proposed ADRs and memory,
adding the transcript to the knowledge base, and recordings without a speech-to-text model."""

import io
import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from authhelp import member_client, new_client, signup  # noqa: E402

from archflow import llm, meetings  # noqa: E402
from archflow.agents import tracing  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.models import ProjectState, Requirement  # noqa: E402
from archflow.store import Store  # noqa: E402

from test_mcp_remote import BASE  # noqa: E402

VTT = b"""WEBVTT

1
00:00:05.000 --> 00:00:09.000
<v Priya Shah>Thanks for joining. Can everyone see my screen?</v>

2
00:00:10.000 --> 00:00:18.500
<v Priya Shah>Customers must be able to track their courier on a live map.</v>

3
00:00:19.000 --> 00:00:25.000
<v Tom Berg>Agreed. We decided to use PostgreSQL with PostGIS for the location data.</v>

4
00:00:26.000 --> 00:00:31.000
<v Tom Berg>I'll write the ADR by Friday.</v>

5
00:00:32.000 --> 00:00:40.000
<v Priya Shah>Should we support offline mode for couriers in tunnels?</v>
"""

SRT = b"""1
00:00:01,000 --> 00:00:04,000
Ana: The checkout must respond in under 300 ms.

2
00:00:04,500 --> 00:00:06,000
Ana: Even on Black Friday.
"""

TEAMS_TXT = b"""Priya Shah   0:03
The dashboard must show orders from the last 30 days.

Tom Berg   0:15
Let's go with React for the dashboard.
"""


def make_docx(lines: list[str]) -> bytes:
    import docx
    d = docx.Document()
    for line in lines:
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- reading
def test_reads_webvtt_voice_tags_with_times():
    t = meetings.read_transcript("standup.vtt", VTT)
    assert t.type == "vtt" and t.speakers == ["Priya Shah", "Tom Berg"]
    assert t.turns[0].start == 5.0 and "track their courier" in t.turns[0].text  # same speaker cues merged
    assert meetings.stamp(t.duration) == "00:00:32"


def test_reads_srt_and_merges_consecutive_cues():
    t = meetings.read_transcript("call.srt", SRT)
    assert len(t.turns) == 1 and t.turns[0].speaker == "Ana" and "Black Friday" in t.turns[0].text


def test_reads_plain_text_formats():
    t = meetings.read_transcript("teams.txt", TEAMS_TXT)
    assert [(x.speaker, x.start) for x in t.turns] == [("Priya Shah", 3.0), ("Tom Berg", 15.0)]
    t = meetings.read_transcript("notes.md", b"[00:01:02] Ana: The app must work offline.\nIt syncs later.\n"
                                            b"Ben: Sounds good.")
    assert t.turns[0].start == 62 and t.turns[0].text.endswith("It syncs later.") and t.turns[1].speaker == "Ben"
    t = meetings.read_transcript("raw.txt", b"We talked about the roadmap.\n\nThe API must be versioned.")
    assert [x.speaker for x in t.turns] == ["", ""]
    t = meetings.read_transcript("call.docx", make_docx(["Ana Ruiz  1:05", "The export must include VAT.",
                                                           "Ben: OK."]))
    assert t.turns[0].speaker == "Ana Ruiz" and t.turns[0].start == 65


def test_rejects_unknown_types_and_recordings_without_a_model():
    with pytest.raises(meetings.IntakeError) as e:
        meetings.read_transcript("deck.pptx", b"PK")
    assert e.value.status == 415
    with pytest.raises(meetings.IntakeError) as e:
        meetings.read_transcript("call.m4a", b"\x00\x00\x00 ftypM4A ")
    assert e.value.status == 415 and "speech-to-text" in str(e.value)


def test_recordings_are_transcribed_when_a_model_exists(monkeypatch):
    monkeypatch.setattr(llm, "transcription_model", lambda org_id=None: ("openai/whisper-1", "k"))
    monkeypatch.setattr(llm, "transcribe", lambda data, name, org_id=None: {
        "model": "openai/whisper-1", "text": "x",
        "segments": [{"start": 0.0, "end": 4.0, "text": "The app must send a receipt by email."}]})
    t = meetings.read_transcript("call.mp3", b"ID3fake")
    assert t.type == "audio" and t.model == "openai/whisper-1" and t.turns[0].text.startswith("The app must")


# ---------------------------------------------------------------- extraction
def test_offline_extraction_finds_requirements_decisions_actions_and_real_questions():
    t = meetings.read_transcript("standup.vtt", VTT)
    out = meetings.propose(t, ProjectState(), None, use_ai=False)
    assert out["engine"] == "offline"
    assert [r["title"] for r in out["requirements"]] == ["Customers must be able to track their courier on a live map"]
    r = out["requirements"][0]
    assert r["speaker"] == "Priya Shah" and r["timestamp"] == "00:00:05" and r["source"]["type"] == "meeting"
    assert len(out["decisions"]) == 1 and "PostGIS" in out["decisions"][0]["decision"]
    assert out["decisions"][0]["decided_by"] == "Tom Berg"
    a = out["action_items"][0]
    assert a["owner"] == "Tom Berg" and a["due"].lower() == "friday"
    assert [q["text"] for q in out["open_questions"]] == ["Should we support offline mode for couriers in tunnels?"]
    assert out["participants"] == ["Priya Shah", "Tom Berg"]


def test_ai_extraction_is_merged_flagged_and_marks_disputed(monkeypatch):
    state = ProjectState(requirements={"REQ-001": Requirement(
        id="REQ-001", title="Live courier map", description="Customers can track their courier on a live map.")})
    seen = {}

    def fake(system, messages, schema, **kw):
        seen["user"] = messages[-1]["content"]
        return {"summary": "Agreed the tracking map and the database.", "participants": ["Priya Shah", "Tom Berg"],
                "requirements": [
                    {"title": "Track courier on a live map", "description": "Customers must be able to track their "
                     "courier on a live map.", "kind": "functional", "priority": "high", "speaker": "Priya Shah",
                     "timestamp": "00:00:10", "agreement": "agreed", "duplicate_of": "REQ-001"},
                    {"title": "Offline mode for couriers", "kind": "functional", "priority": "medium",
                     "speaker": "Priya Shah", "timestamp": "0:32", "agreement": "disputed"}],
                "decisions": [{"title": "PostgreSQL with PostGIS", "decision": "Store locations in PostGIS",
                               "rationale": "Spatial queries", "alternatives": ["MongoDB"], "decided_by": "Tom Berg",
                               "timestamp": "00:00:19"}],
                "action_items": [{"task": "Write the ADR", "owner": "Tom Berg", "due": "Friday"}],
                "open_questions": [{"text": "Offline mode?", "timestamp": "00:00:32"}]}

    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(meetings, "extract_model", lambda *a, **k: "gemini/test")
    monkeypatch.setattr(llm, "complete_json", fake)
    t = meetings.read_transcript("standup.vtt", VTT)
    out = meetings.propose(t, state, "org-x")
    assert out["engine"] == "llm" and out["summary"].startswith("Agreed")
    first, second = out["requirements"]
    assert first["duplicates"][0]["id"] == "REQ-001" and first["selected"] is False
    assert second["agreement"] == "disputed" and second["selected"] is False and second["timestamp"] == "00:00:32"
    assert out["decisions"][0]["alternatives"] == ["MongoDB"] and out["action_items"][0]["id"] == "A-1"
    assert "<untrusted_data" in seen["user"] and "[00:00:05] Priya Shah:" in seen["user"]


# ---------------------------------------------------------------- API
@pytest.fixture()
def app():
    yield create_app(Store(":memory:"))
    tracing.set_sink(None)


@pytest.fixture()
def client(app):
    with new_client(app, base_url=BASE) as c:
        signup(c)
        yield c


def test_import_accept_creates_requirements_adrs_memory_and_knowledge(client, app):
    pid = client.post("/api/projects", json={"name": "Couriers", "template": "blank"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/meetings/import", params={"filename": "standup.vtt", "knowledge": "true"},
                    content=VTT)
    assert r.status_code == 200, r.text
    prop = r.json()
    assert prop["file"]["speakers"] == ["Priya Shah", "Tom Berg"] and prop["knowledge"]["chunks"] >= 1
    kb = client.get(f"/api/projects/{pid}/knowledge").json()["documents"]
    assert kb[0]["origin"] == "meeting" and kb[0]["type"] == "vtt"
    assert client.get(f"/api/projects/{pid}").json()["state"]["requirements"] == {}  # nothing applied yet

    a = prop["action_items"][0]
    r = client.post(f"/api/projects/{pid}/meetings/import/accept", json={
        "file": prop["file"], "requirements": prop["requirements"], "decisions": prop["decisions"],
        "memory": [{"text": f"Action: {a['task']} ({a['owner']}, {a['due']})", "category": "context"},
                   {"text": prop["open_questions"][0]["text"], "category": "context"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    req = client.get(f"/api/projects/{pid}").json()["state"]["requirements"][body["added"][0]]
    assert req["source"]["type"] == "meeting" and req["source"]["ref"] == "standup.vtt"
    assert req["source"]["section"] == "Priya Shah" and req["source"]["timestamp"] == "00:00:05"
    adr = body["decisions"][0]
    assert adr["status"] == "proposed" and "Tom Berg" in adr["context"] and "PostGIS" in adr["decision"]
    assert len(body["memory"]) == 2
    acts = " ".join(" ".join(x["summary"]) for x in client.get(f"/api/projects/{pid}/activity").json())
    assert "meeting standup.vtt: accepted 1 requirement(s), 1 decision(s)" in acts

    # the assistant's knowledge block now finds the meeting
    from archflow import knowledge
    assert "PostGIS" in knowledge.context_block(app.state.store, pid, "which database for location data", None)


def test_streams_progress_and_checks_roles(client, app):
    pid = client.post("/api/projects", json={"name": "M", "template": "blank"}).json()["id"]
    url = f"/api/projects/{pid}/meetings/import"
    with client.stream("POST", url, params={"filename": "teams.txt"}, content=TEAMS_TXT,
                       headers={"Accept": "text/event-stream"}) as r:
        events = [line.split(": ", 1)[1] for line in r.iter_lines() if line.startswith("event: ")]
    assert events[0] == "progress" and events[-1] == "done"
    r = client.post(url, params={"filename": "call.wav"}, content=b"RIFF....WAVE")
    assert r.status_code == 415
    viewer = member_client(app, client, "viewer", "viewer@example.com")
    assert viewer.post(url, params={"filename": "t.txt"}, content=TEAMS_TXT).status_code == 403


def test_meetings_eval_suite_scores_a_case(monkeypatch):
    from archflow.evals import runner, suites

    case = next(c for c in runner.load_suites(runner.DATASETS_DIR, ["meetings"])["meetings"]
                if c["id"] == "design-review-decision-and-actions")
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", lambda system, messages, schema, **kw: {
        "summary": "The team chose Kafka.",
        "requirements": [{"title": "Process 2,000 orders per second", "description": "The pipeline must process "
                          "2,000 orders per second at peak.", "kind": "non_functional", "category": "performance",
                          "priority": "high", "speaker": "Lena", "timestamp": "00:01:10", "agreement": "agreed"}],
        "decisions": [{"title": "Use Kafka", "decision": "Use Kafka for order events", "rationale": "replay",
                       "alternatives": ["RabbitMQ"], "decided_by": "Omar", "timestamp": "00:03:00"}],
        "action_items": [{"task": "Size the Kafka cluster", "owner": "Omar", "due": "next Tuesday"}],
        "open_questions": [{"text": "Who owns the schema registry?"}]})
    out = suites.run_meetings(case, None)
    assert all(ok for _, ok, _ in out.checks), out.checks
