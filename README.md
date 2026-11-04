<div align="center">

<img src="docs/logo.svg" width="80" height="80" alt="ArchFlow logo" />

# ArchFlow - AI-Native System Architecture Platform

**Self-hosted · Open Source · Requirements → Architecture → Code**

Turn a product idea into requirements, an architecture and a live canvas that your team and AI agents edit together, then hand it to coding agents as validated OpenSpec changes — powered by FastAPI, React, Yjs and LiteLLM.

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)](https://react.dev)
[![TypeScript](https://img.shields.io/badge/TypeScript-7-3178C6?logo=typescript&logoColor=white)](https://www.typescriptlang.org)
[![LiteLLM](https://img.shields.io/badge/LiteLLM-10_providers-7C3AED)](https://docs.litellm.ai)
[![Yjs](https://img.shields.io/badge/Yjs-Hocuspocus-F59E0B)](https://tiptap.dev/docs/hocuspocus)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-or_SQLite-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![MCP](https://img.shields.io/badge/MCP-OAuth_2.1-111827)](https://modelcontextprotocol.io)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose)
[![License: CC BY-NC-ND 4.0](https://img.shields.io/badge/License-CC%20BY--NC--ND%204.0-lightgrey)](LICENSE)

</div>

---

ArchFlow treats requirements, architecture, decisions and versions as **one structured, versioned, collaborative project model**, not separate documents or pictures. People dragging boxes, the built-in AI assistant and external agents over MCP all edit the same model through the same operation pipeline, so every change is validated, versioned, conflict-checked and shown live to everyone.

> Business idea → Requirements → Architecture → Canvas → Vibe coding → Code → Tests → Deployment

---

## Screenshots

### Landing Page
![Landing Page](docs/screenshots/01_landing.png)

### Home — Projects, AI Status and Organization
![Home](docs/screenshots/02_home.png)

### Workspace — Architecture Canvas
![Workspace](docs/screenshots/03_workspace.png)

### Assistant — Design Mode (guided questions → architecture options)
![Assistant Design](docs/screenshots/04_assistant_design.png)

### Assistant — Edit Mode (suggestions built from your canvas)
![Assistant Edit](docs/screenshots/05_assistant_edit.png)

### Component Inspector
![Inspector](docs/screenshots/06_inspector.png)

### Requirements & Traceability
![Requirements](docs/screenshots/07_requirements.png)

### Health Dashboard
![Health](docs/screenshots/08_health.png)

### Versions, Branches & Activity
![Versions](docs/screenshots/09_versions.png)

### Live Collaboration — Presence, Cursors & Selections
![Live Collaboration](docs/screenshots/21_live_collaboration.png)

### Comments — Threads, Replies & @Mentions
![Comments](docs/screenshots/19_comments.png)

### Notifications — Mentions & Replies
![Notifications](docs/screenshots/20_notifications.png)

### Architecture Decisions (ADRs)
![Decisions](docs/screenshots/10_decisions.png)

### Low-Level Design (component internals)
![Low-Level Design](docs/screenshots/17_low_level_design.png)

### Vibe Code — OpenSpec Changes for Coding Agents
![Vibe Code](docs/screenshots/16_vibe_code.png)

### Code → Architecture (reconstructed from a repository)
![Code to Architecture](docs/screenshots/12_code_to_architecture.png)

### Code Analysis — Drift & Change Impact
![Code Analysis](docs/screenshots/12b_code_analysis.png)

### Tools Menu — Code, MCP, Convert & Export
![Tools](docs/screenshots/11_tools_menu.png)

### Connect AI Agents over MCP
![Connect MCP](docs/screenshots/15_connect_mcp.png)

### AI Settings — Providers & Models (LiteLLM)
![AI Settings](docs/screenshots/13_ai_settings.png)

### AI Usage & Monthly Budget
![AI Usage](docs/screenshots/14_ai_usage.png)

### Dark Mode
![Dark Mode](docs/screenshots/18_dark_mode.png)

---

## ✨ Features

### 🧭 AI Assistant — Design, Edit & Ask

One assistant panel with three modes, docked **left**, **right** or **floating** over the canvas (remembered per browser).

- **Design** — describe an idea; ArchFlow asks multiple-choice questions (single/multi-select, a recommended option, "Other"), then offers 2–4 ranked architectures with pros/cons, cost and complexity. Apply one to the canvas or open it as a draft branch. Starting prompts come from the project (its requirements, current canvas, name and description). Works offline with a built-in question bank; adaptive with an LLM
- **Edit** — natural-language commands ("Add Redis between Booking Service and Booking DB") become validated operations on the model; every change is listed. Suggestions are built from your selection, open health issues and gaps in the architecture, using your component names. A built-in rule parser covers common commands offline
- **Ask** — "Why was Redis added?", "What happens if the DB fails?", one-click architecture review. Answers come from the model, version history and activity log, stream in as Markdown with component names and requirement IDs linked to the canvas, and can be regenerated
- **Shared conversations** — threads are stored on the server, survive reloads, appear live for teammates, and reopen from the history menu. Follow-ups work ("now put a cache in front of *it*"): each call gets recent turns verbatim plus a running summary of older ones
- **Project memory** — settled facts ("Cloud: AWS", "data stays in the EU") that every answer, edit and design respects; people add, edit, pin and delete them, and the AI proposes facts it hears

---

### 🗺️ Architecture Canvas

- **React Flow canvas** — drag from the palette, connect, move, multi-select, delete, copy/paste/duplicate, search, zoom/pan, minimap
- **Tidy up** (`L`) — arranges a diagram in tiers (clients → gateways → services → caches/queues → databases → external/LLMs), ordered to reduce crossings, each store under its service; one undo step. **Auto-arrange after AI edits**, and new components never land on existing ones
- **Typed components** — `service`, `database`, `cache`, `queue`, `gateway`, `agent`, `llm`, `mcp`, `security`, … with technology, description and linked requirements
- **HLD → LLD drill-down (C4)** — double-click a component to open its own low-level diagram. **Generate low-level design** creates controllers, services, repositories, API endpoints, tables for connected databases and events for connected queues. Inspector **Views**: API list (OpenAPI), data model and request flow, drawn with Mermaid
- **More diagrams from the same model** (Tools → **Diagrams**) — always in step with the canvas, nothing to redraw:
  - **C4 system context** (clients as users, external systems and LLMs around the system) and **C4 containers** (gateways, services, agents, stores and queues inside the system boundary)
  - **Sequence diagram per critical flow** — traced along your connections from a client through gateways and services to each data store, with calls to auth, caches and LLMs on the way and asynchronous hops through queues. Flows are named after the requirement they implement; with AI on, the `diagrams.sequence` agent names them after the user goal and picks among the traced paths (it cannot invent connections)
  - **ER diagram** of every table across all low-level designs, grouped by database, with relations from LLD connections and foreign-key columns (`customer_id` → `customers`)
  - **Deployment** — components grouped by platform (AWS, Azure, Google Cloud, SaaS, self-hosted) and runtime (Kubernetes, serverless, containers, managed data services…) from vendor, catalog entry, technology and `runtime` / `hosting` props
  - **Data flow with trust boundaries** — public clients, edge / DMZ (gateways and security controls), private services, data stores and third parties; boundary-crossing flows are drawn thick, and a client reaching a service or database without a gateway is flagged
  - Every diagram comes with a plain-language **alt text** (set on the rendered SVG and embedded as Mermaid `accTitle` / `accDescr`)
- **Convert** a whole design to **AWS / Azure / Google Cloud / open source**, or swap one component for its equivalent
- **Dark / light mode**, keyboard shortcuts (`Ctrl+K` project switcher, `L` tidy, `F` full screen)

---

### 🧱 Component Catalog (151 components)

- Generic building blocks plus **AWS, Azure, Google Cloud, self-hosted open source and SaaS** services, each mapped to a cloud-neutral kind
- Searchable palette with vendor filters, vendor badges on nodes, technology logos (Simple Icons: PostgreSQL, Redis, Kafka, Kubernetes, Claude, Stripe, …)
- Also over MCP: `search_catalog`, `convert_cloud`

---

### 📋 Requirements & Traceability

- **Structured FR/NFR objects** — actors, acceptance criteria, priority, status, category
- **Completeness lint** — vague terms ("fast", "scalable"…), missing acceptance criteria or actors, missing NFR categories
- **Traceability** — requirement ↔ component links; click a requirement to highlight everything that implements it; untraced requirements are flagged
- **History** — every change to a requirement is recorded with who made it (a person, the AI, or an agent via MCP), when, and each field's value before → after; open a requirement and choose **History** (`GET /api/projects/{id}/requirements/{req}/history`)
- **Soft delete** — deleting a requirement moves it to the **Deleted** filter with its component links kept, so **Restore** brings it back intact; deleted requirements are left out of health, traceability, AI context, coding briefs, OpenSpec changes and MCP tools. Admins can **Delete permanently** (the history is kept)
- **Stale links** — when a requirement changes after a component linked to it last changed, the component gets a ⟳ badge on the canvas and the Inspector lists what changed; **Mark reviewed** confirms the component still fits. Staleness is computed from revision numbers (`stale_links` in the analysis), nothing extra is stored
- **Import from a document** — upload a PRD, RFP or spec (PDF, Word `.docx`, Markdown, text; up to `ARCHFLOW_INTAKE_MAX_MB`, default 20 MB). The `intake.extract` agent reads long documents in parallel chunks and proposes FRs and NFRs with the section and page they came from, plus assumptions, constraints, open questions and stakeholders; likely duplicates and conflicts with existing requirements (or elsewhere in the document) are flagged with both sources. Review, edit and tick what to keep — nothing is added until you accept. Accepted requirements keep their provenance (file, section, page, import time) and assumptions/constraints/questions can be saved to project memory. Without an AI model, "shall / must / should" sentences and bullets under requirement headings are read instead. Document text is treated as untrusted input, never as instructions
- **Import from a meeting** (🎙 Meeting) — upload a Teams / Zoom / Google Meet transcript (`.vtt` with voice tags, `.srt`, text or Markdown with "Speaker: text" or "[00:12:05] Speaker: text", Word `.docx`) or a recording (`.mp3`, `.m4a`, `.wav`, `.webm`, `.mp4`; transcribed by the org's speech-to-text model, OpenAI Whisper by default). The `meetings.extract` agent reads the conversation in parallel windows and proposes the requirements people asked for (who said it, when, and whether the room **agreed**, only **proposed** it or **disputed** it), the **decisions** taken (rationale and the alternatives discussed), **action items** (owner, due date), open questions, assumptions, constraints, participants and a summary. Duplicates and conflicts with existing requirements are flagged. Accepting adds the requirements with provenance (`source.type = meeting`, speaker, timestamp), the decisions as **proposed ADRs**, and action items and questions as project memory; the transcript can also go into the knowledge base. Without a model, rules read requirement sentences, "we decided / let's go with", "I'll / can you … by Friday" and real questions (small talk is skipped)

### 📚 Knowledge Base (RAG)

- **Tools → Knowledge base** — upload the documents the team decides from (PRDs, RFPs, policies, runbooks, contracts, meeting transcripts). They are split into passages at section and paragraph boundaries (with a little overlap) and kept with their section, page and, for transcripts, the timestamp
- **Hybrid retrieval** — BM25 keyword ranking always (offline too: error codes and product names match exactly) plus vector similarity when the org has an embedding model (Gemini `gemini-embedding-001` or OpenAI `text-embedding-3-small`, or `ARCHFLOW_MODEL_EMBEDDING`), merged with reciprocal-rank fusion. Documents added before a model was available are re-indexed with one click
- **Cited answers** — the `kb.answer` agent answers only from the retrieved passages, cites every claim ([K1], [K2]…) with a verbatim quote, document, section and page, says when the documents do not answer, flags passages that disagree, and suggests follow-up questions drawn from the passages
- **The assistant uses it** — every Ask question also gets the best matching passages (`assistant.ask` v4), so answers about the project can quote its documents
- Passages are untrusted input (wrapped and never followed as instructions); personal data and secrets are masked before passages are embedded or sent to a model. Viewers search and ask; developers and up add and remove documents. REST: `GET|POST /api/projects/{id}/knowledge`, `POST …/knowledge/search | ask | reindex`, `DELETE …/knowledge/{doc}`

### 💲 Cost Estimation

- **Tools → Cost estimate** — a monthly cost range for the architecture on the canvas, one line per component: priced from its catalog kind (containers, Kubernetes, functions, VMs, relational / document / vector / search databases, warehouses, caches, queues, streams, object and file storage, CDN, load balancers, API gateways, identity, secrets, WAF, LLMs, ML endpoints, monitoring, workflows, email, SMS) and its vendor (AWS, Azure, Google Cloud, self-managed open source, SaaS), with the formula behind every number and a low–high range (usage-priced lines are less certain than fixed instances)
- **Assumptions from the project** — monthly active users, peak requests per second, stored data, data transfer, availability, LLM calls and tokens, emails and messages per user are read from the requirements and memory ("5,000 concurrent users", "300 requests per second", "2 TB of documents", "99.95%"); every value says where it came from and can be changed in the dialog. 99.95% or more doubles stateful tiers (multi-AZ). Components can pin their own numbers with the properties `monthly_cost`, `instances`, `vcpu`, `storage_gb`
- **LLM costs** use the model's token prices (LiteLLM's price map); a staging environment (30% of production by default), the region and data transfer out are included
- **Budget check** — a cost requirement ("must cost less than $2,000 per month", "6k USD per year") is compared with the range: within budget, at risk or over
- **Ways to save** — the `costs.optimize` agent writes recommendations grounded in the line items (components named, saving capped at what they cost, trade-offs against the NFRs); without a model, rules suggest the standard ones (commitments for steady instances, serverless for low traffic, a CDN for heavy egress, model routing for LLM-heavy designs). **CSV** download for spreadsheets
- Prices are indicative on-demand list prices built into `costs.PRICES` (`ARCHFLOW_COST_PRICES` overrides any of them) — for comparing options and finding the big lines, not a quote. REST: `POST /api/projects/{id}/costs`, `GET …/costs.csv`

---

### 🩺 Health & Validation

- **Architecture checks** — orphans, client → database access, shared databases, unguarded LLMs, missing auth or PII controls, missing gateway; **data-flow checks** (unauthenticated entry points, queues without producers or consumers, realtime channels without a return path, stores nobody writes) and **domain completeness checklists** (capabilities and requirement areas a food-delivery, fintech, healthcare… product usually needs)
- **Option review in the design chat** — every proposed architecture is checked before you see it: technologies normalized to one per component, structural and data-flow problems repaired deterministically (auth on entry points, realtime return paths, key management, cache), missing functional areas added as requirement stubs; with an LLM, flawed options get one repair pass (`design.repair` agent) and each card shows its review
- **Health dashboard** — requirements completeness and approval, traceability %, architecture score, issue counts, and how well the design matches the code
- **AI review** — four specialist agents (security & privacy, reliability, performance & scale, cost & operations) review the design **in parallel**; findings are ranked by severity, name the components and requirements at risk, and come with **Fix with AI**, an instruction the Edit mode applies after you check it

---

### 🌿 Versions, Branches & Diff

- Save versions, restore any version (the state it replaces is saved first, so a restore can be undone), branch from any version, switch branches
- **Structural diff** between versions or branches, rendered **on the canvas** (added / changed / removed ghosts) plus text
- **Branch merge** — merge any branch into another (Versions → Branches → **Merge into…**) with a **three-way diff** from the last version both share (or the version the branch was created from). Changes made on one side merge automatically, field by field; the same field changed differently on both sides, or a component deleted on one side and changed on the other, is a **conflict** you resolve with base / ours / theirs side by side (or a custom value). Properties merge key by key, requirement links as sets; connections left without an end are dropped and listed. **Suggest with AI** proposes a resolution per conflict with a rationale (you still choose). The merge is applied as ordinary operations, so it is validated, attributed in the activity log and synced live; a safety version of the target is saved first, and the merge point is recorded so the next merge starts from there
- **Activity history** — who changed what, and when, per branch
- **Approvals** — a saved version moves **Draft → In review → Approved / Changes requested**. Editors request a review; reviewers, architects, admins and owners approve or request changes with a comment. The version's author cannot approve it (unless they are the org's only owner). Each org sets how many approvals a version needs (default 1) and whether a draft must be approved, with no changes since, before it is **adopted** into `main` (otherwise adopting returns 409). Every action is recorded (who, when, which version, comment), logged in the activity history and sent as an in-app notification; the badge shows in the version list, and the MCP tool `get_approval_status` reads it

---

### ⚖️ Architecture Decisions (ADRs)

- ADRs (ADR-001, …) with status, context, decision, consequences, alternatives and linked requirements
- **Compare drafts** — scores 2–4 branches against the requirements/NFRs on cost, scalability, performance, security, reliability, operability and delivery speed. The [Jev](https://thejevai.com/docs) decision API (`JEV_API_KEY`) scores and picks while an LLM agent, **in parallel**, explains every score, lists strengths, risks and when each option wins, and gives a **second opinion** (agree / disagree is shown). Without Jev the LLM scores; without either, an offline heuristic. Re-weight the criteria to your priorities, and **ask** follow-up questions (the AI suggests them)
- Record the result as an ADR and **adopt** the chosen draft into `main` (a safety version first)

---

### ⚙️ Vibe Code with OpenSpec

- **⚙ Vibe code** hands the design to a coding agent through spec-driven changes made with [OpenSpec](https://github.com/Fission-AI/openspec) itself (npm dependency in `backend/openspec`)
- For each artifact of OpenSpec's workflow (proposal → specs + design → tasks) ArchFlow fetches OpenSpec's own instructions and template, and the LLM writes it from the selection, traced requirements, connections, internal designs, decisions and project memory
- `openspec validate --strict` checks the change; reported issues go back to the LLM to fix
- All OpenSpec files live in the database: edit (re-validated on save), **archive** to merge deltas into living specs, download the `openspec/` folder, or copy an agent prompt. **Quick brief** is the instant one-file prompt

---

### ⟨/⟩ Code ↔ Architecture

- **Analyse a public Git repository** (or upload a graph made locally by `archflow-codegraph` — no source code leaves your machine) with [Graphify](https://pypi.org/project/graphifyy/): tree-sitter code graph of files, symbols, calls and imports, communities, god nodes, import cycles
- **Deterministic detections** — languages, manifests, docker-compose, Dockerfiles, Kubernetes, Terraform, databases/caches/queues/LLM providers from imported libraries, entry points
- **Reconstruct architecture** opens a `draft/from-code-<sha>` branch with services, clients, infrastructure, connections and catalog matches; components keep their code paths
- **Drift** compares any branch with the code (in code but not designed, designed but not in code, undeclared connections, technology mismatches)
- **Change impact** maps changed files → dependent symbols → components → requirements

---

### 👥 Real-Time Collaboration

- Google Docs-style editing on a shared **Yjs** document (Hocuspocus server): edits merge automatically, offline edits sync on reconnect, undo/redo (`Ctrl+Z` / `Ctrl+Y`)
- Presence avatars, live cursors and selection outlines
- AI, MCP and API edits merge into the live document **field by field**, so they never overwrite what people are typing; if someone changes the field you are editing, you get a "Use theirs" option
- **Comments** on components or the whole project: threaded replies, resolve / reopen, edit and delete, live for everyone; components with open threads show a 💬 badge on the canvas
- **@mentions** — type `@` to tag a teammate; they get a notification in the 🔔 bell (mentions and replies), which jumps straight to the thread

---

### 🤖 MCP for AI Agents

- **Remote MCP server** at `/mcp` (Streamable HTTP) with **OAuth 2.1** (dynamic client registration, PKCE, consent page, rotating refresh tokens, revocation) or personal access tokens
- Works with **Claude Code, Claude.ai, Claude Desktop, Codex** and other MCP clients; **41 tools**, org-scoped, read-only or read & edit, capped by your role
- Agent edits go through the same pipeline as the canvas and appear live, attributed to "*user* via MCP"
- Coding loop: **list_tasks → get_task_brief → implement → report_progress** (status and PR link show in the activity)

---

### ⚡ AI Providers, Spend & Budgets

- **Bring your own model** — organization admins add keys and choose models in **AI settings**; every call goes through **LiteLLM**
- A **reasoning** and a **fast** model, each with up to 3 fallbacks; a **Test** button makes one real call
- Retries and cooldowns, exponential backoff with jitter (honouring `Retry-After`), cross-provider fallback, a per-model circuit breaker and a per-org request limit
- **AI usage** — every call recorded with tokens and an estimated cost per organization, project, feature and model; a **monthly budget** pauses AI features (they fall back to built-in behaviour) when it is used up
- Keys are encrypted at rest (AES-256-GCM); only the last 4 characters are ever shown

| Provider | LiteLLM prefix | Key | Notes |
|---|---|---|---|
| Google Gemini | `gemini/` | required | Server default |
| Anthropic Claude | `anthropic/` | required | |
| OpenAI | `openai/` | required | |
| OpenRouter | `openrouter/` | required | Hundreds of models with one key |
| Mistral | `mistral/` | required | |
| Groq | `groq/` | required | |
| DeepSeek | `deepseek/` | required | |
| xAI Grok | `xai/` | required | |
| Azure OpenAI | `azure/` | required | Endpoint + API version |
| OpenAI-compatible | `openai/` + endpoint | optional | Ollama, vLLM, LM Studio, a LiteLLM proxy |

### 🧠 Agents, Versioned Prompts, Evals & Traces

- **Multi-agent** — the AI review fires four specialist agents at once and Compare drafts runs Jev and an LLM agent side by side (`agents.parallel`); one failing agent never loses the others' results
- **Pydantic AI agents** — every AI feature (canvas edits, Ask, design chat, comparisons, suggestions, specs, LLD, memory, code refinement) is a Pydantic AI `Agent` whose model sends each request through the LiteLLM gateway, so fallbacks, per-org keys, budgets and usage accounting still apply. Canvas edits are a real tool loop: each tool call is applied to a working copy and its result (or why it was rejected) goes back to the model
- **Official cloud documentation as context** — when a design-chat turn, an Ask question, an AI review or a low-level design is about **Azure** or **AWS**, the agent first searches that provider's documentation MCP server — [Microsoft Learn](https://learn.microsoft.com/api/mcp) or [AWS Knowledge](https://github.com/awslabs/mcp/tree/main/src/aws-knowledge-mcp-server) — and gets the top reference architectures, Well-Architected and service guides in its prompt, so service choices and limits follow current documentation, and answers cite the URLs. Only a search phrase (the request plus the services on the canvas) leaves ArchFlow; results are cached, each lookup appears in the run's trace, and a source that is down is skipped. Swap in the local [AWS Documentation server](https://github.com/awslabs/mcp/tree/main/src/aws-documentation-mcp-server) with `ARCHFLOW_DOCS_AWS=stdio:uvx awslabs.aws-documentation-mcp-server@latest`, or turn it off with `ARCHFLOW_DOCS=off`
- **LLM follow-ups instead of fixed prompts** — starting prompts, follow-ups after each answer, design-chat next steps and comparison questions are written by the `assistant.suggestions` agent from the project's components, requirements, health issues and the last exchange (the rule-based ones remain only for offline use)
- **The AI asks in bubbles** — in Edit mode clarifying questions arrive as chat bubbles with answers to tap; in Ask mode the assistant asks back when a question is ambiguous and offers quick replies; the Design interview asks multiple-choice questions
- **Prompts in YAML, versioned** — `backend/archflow/prompts/<id>.yaml` holds every version of a prompt; released versions are locked by hash in `prompts.lock.json` (tests fail if one is edited), a change ships as a new version, and `ARCHFLOW_PROMPTS=compare.evaluate=v1` pins or rolls back without a deploy. Every usage row and trace records `id@version`
- **Evals** — `uv run archflow-eval` runs 80+ golden cases (`archflow/evals/datasets/*.yaml`: compare, ask, command, design, suggestions, review, lld, merge, intake, guide, diagrams, and an adversarial red-team suite with prompt injection, system-prompt extraction, canvas-wipe attempts and PII echo) across fintech, healthcare, e-commerce, IoT and AI-agent projects against your models with deterministic checks, and with `--ragas` adds RAGAS metrics (faithfulness, groundedness, answer accuracy, aspect critics) judged through the same gateway. `--suite review` runs one suite; `--prompt id=v2 --baseline old.json` A/B tests a prompt version; `--fail-under 0.7` gates CI (pushes to `main`, when a `GEMINI_API_KEY` secret is set)
- **Traces** — each run records **who asked**, **what they asked**, **what the AI answered**, the full input and output, every LLM call (model, tokens, cost, latency), every tool and API call (arguments, result), and the prompt version. Agents fired in parallel (the AI review, the Jev + LLM comparison panel) appear as one run with its child agents. Admins browse them under **Traces** on the home page, filter by person, feature or status, and see the prompt catalog; **Usage & budget** breaks spend down by feature, model, project, prompt version and person. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to also export the agents' OpenTelemetry spans to Langfuse, Phoenix, Jaeger or Logfire

```bash
uv run archflow-prompts list | check | lock | show compare.evaluate@v1
uv run archflow-eval --suite compare --suite ask
uv run --with ragas==0.3.9 --with "langchain-community>=0.3,<0.4" archflow-eval --ragas --baseline evals/reports/last.json
```

---

### 📤 Export

- **PNG** or **SVG** image of the diagram you are looking at (system or a component's internals)
- **Mermaid and PlantUML text** of the high-level architecture and of every diagram above (C4 context and containers with the C4-PlantUML library, sequence, ER, deployment, data flow): copy either source or save the SVG from Tools → Diagrams; `GET /api/projects/{id}/diagrams` lists them with alt text, `GET /api/projects/{id}/diagrams/{diagram}?format=mermaid|plantuml` downloads one (viewers can read), and agents use the `get_diagram` MCP tool
- **Interactive HTML diagram** — a standalone, ArchFlow-branded viewer (explore nodes, trace paths, themes, PNG/SVG/WebM export) made by ArchFlow's renderer in `backend/renderer`
- **Markdown spec** of a branch (Tools → Export spec & agent guides): overview, requirements (FR/NFR with acceptance criteria, priority, status and the components that implement them), architecture by component type (technology, description, connections), low-level designs, ADRs, project memory and a health summary. Same model, same bytes
- **PDF** — a print-ready page of the same spec (light background, a page break per chapter); choose *Save as PDF* in the print dialog. The API serves HTML because PDF libraries that render it well need system libraries or bundled fonts
- **JSON / YAML** — the full project (branch state, decisions, memory, versions and branches metadata) with `schema: archflow.project` and `schema_version` for tools; `exports.load_document` reads it back
- **CLAUDE.md, AGENTS.md, .cursorrules** for coding agents, previewed before download: stack, services and their responsibilities, data stores, NFRs as constraints, accepted ADRs as rules and a folder layout, all derived from the model, plus project-specific conventions, do / don't and testing guidance written by the `exports.agent_guide` agent (skeleton only without an AI model). The OpenSpec download (`openspec.zip`) includes the three files at its root
- REST: `GET /api/projects/{id}/export/spec.md | spec.html | project.json | project.yaml`, `POST …/export/guides`, `GET …/export/guides/{claude-md|agents-md|cursorrules}` (any project role); MCP: `export_spec` (markdown or json)

---

### 🔐 Accounts, Roles & Security

- Email + password accounts (scrypt), opaque session token in an `HttpOnly`, `SameSite=Lax` cookie with a 14-day sliding expiry; sign-in attempts rate limited per email + IP
- Organizations with six roles, single-use invite links, org-scoped projects
- **CSRF / CORS** — unsafe requests must come from an allowed origin; CORS allows exactly those origins
- **Real-time tickets** — a 2-minute HMAC-signed ticket opens the live document; viewers and reviewers connect **read-only**

| Role | Can |
|---|---|
| viewer | Read projects, versions, diffs, analysis, decisions and members; ask the AI questions; connect MCP agents read-only |
| reviewer | viewer + change a decision's status (accept / reject / supersede); approve versions or request changes |
| developer | Edit the canvas, requirements, versions, branches; request version reviews; AI commands, design chat, convert, low-level design, decisions, code analysis |
| architect | developer + approve versions or request changes |
| admin | + members and roles (up to admin), invite links, access tokens and connected apps, AI settings and budget, approval settings, delete projects |
| owner | + rename the org, grant/remove the owner role (an org always keeps one owner) |

---

## 🏗️ System Architecture & Data Flow

### High-Level System Architecture

```mermaid
flowchart LR
    subgraph Clients
        B["Browser<br/>React 19 · React Flow · Yjs"]
        A["AI agents<br/>Claude Code · Codex · Claude.ai"]
    end
    subgraph ArchFlow
        W["Web (nginx)<br/>static app + reverse proxy"]
        API["API — FastAPI<br/>ops pipeline · analysis · AI · MCP"]
        C["Collab server<br/>Hocuspocus (Yjs)"]
        CG["Code-graph worker<br/>Graphify · tree-sitter"]
        OS["OpenSpec CLI<br/>+ diagram renderer (Node)"]
    end
    DB[("PostgreSQL / SQLite<br/>orgs · projects · branch state · Yjs docs<br/>versions · activity · conversations · specs")]
    LLM["LLM providers via LiteLLM<br/>Gemini · Claude · OpenAI · OpenRouter · …"]
    GH["Git repositories"]

    B -- HTTPS / REST + SSE --> W --> API
    B -- WebSocket (live edits, presence) --> W --> C
    A -- MCP over HTTP + OAuth 2.1 --> API
    C -- field-level patches / projection --> API
    API --> DB
    C --> DB
    API --> LLM
    API --> CG --> GH
    API --> OS
    API -. "optional: rate limits · jobs · fan-out" .-> R[("Redis")]
```

**Scale-out (optional).** One API process needs nothing but the database. To run several API instances behind a
load balancer, set `ARCHFLOW_REDIS_URL` (see `backend/archflow/scale.py`):

- **Rate limits.** The per-org LLM limit and the sign-in limiter are shared. They use atomic Redis counters; the
  keys are hashed, so no e-mail address is stored in Redis.
- **Real-time fan-out.** Each instance delivers `/ws` events to its own sockets and publishes them on a Redis
  channel per room (`archflow:room:<project>:<branch>`). A subscriber on every other instance delivers them to
  its sockets. Messages carry the sender's instance id, so the sending instance never delivers them twice.
  Presence spans instances. The subscriber logs failures and reconnects with backoff.
- **Jobs.** Status and progress of repository analyses and document and meeting imports are kept in Redis
  hashes with a 24 h TTL. Any instance answers `GET /api/projects/{pid}/jobs/{id}` and
  `…/codegraph/jobs/{id}`, and a cancel sent to any instance stops the job. The work itself runs on the
  instance that accepted it; an import's SSE stream stays on that instance.
- **Health.** `/api/health` reports the Redis mode and the subscriber state. `/api/ready` pings Redis but does not
  fail on it.
- **Without Redis**, all of this stays in-process, as before. If a configured Redis fails, each call falls back
  to in-process behaviour and logs a warning.
- **Still per instance:**
  - LLM circuit breakers and router cooldowns.
  - The AI-budget cache (at most 15 s stale).
  - Knowledge-base and code-graph caches, which hold derived data.
- **Already shared:** OAuth consent requests, codes and tokens are in the database, and `/mcp` is stateless HTTP.
- **Collab server.** Hocuspocus keeps each live Yjs document in the memory of one process. Run a single `collab`
  instance, or route each document to a fixed instance (sticky routing by the `<project>:<branch>` document
  name). The API's `ARCHFLOW_COLLAB_URL` patches must go through the same routing.
  `@hocuspocus/extension-redis` is the path to running several active collab instances; it is not wired yet.

### The Operation Pipeline

Every mutation — from a person, the assistant, an MCP agent or a restore — is a list of operations (`add_node`, `update_node`, `remove_node`, `add_edge`, `update_edge`, `remove_edge`, `add_requirement`, `update_requirement`, `remove_requirement` (a soft delete), `restore_requirement`, `purge_requirement` (admins), `mark_reviewed`) applied by `ops.apply_ops`. A batch is atomic, validated and logged. Update ops carry `base_rev`, the revision the editor last saw, so overlapping edits are detected per field (HTTP 409 for strict API/MCP callers).

---

### Step-by-Step Data Flows

#### 1. Sign-In

```
User submits email + password
  → POST /api/auth/login (origin checked: CSRF guard)
  → scrypt verify · rate limit per email + IP
  → random session token; only its SHA-256 is stored
  → HttpOnly, SameSite=Lax cookie (Secure on https), 14-day sliding expiry
  → every route checks the caller's role in the project's org (401 / 404 / 403)
```

#### 2. Live Canvas Editing

```
User drags or edits a component
  → change applied to the shared Yjs document in the browser
  → WebSocket → collab server (ticket: 2-minute HMAC, read-only for viewers)
  → broadcast to every open browser (cursors, presence, selection)
  ↓
Collab server, debounced:
  → stores the binary Yjs doc
  → projects it into ops → API (ops.apply_ops) → branch state + activity log
```

#### 3. AI Edit (Assistant → Edit)

```
"Add Redis between Booking Service and Booking DB" (+ current selection)
  → POST /api/projects/{pid}/conversations/{cid}/command
  → context: canvas, requirements, project memory, recent turns + thread summary
  → LLM (reasoning chain via LiteLLM) returns tool calls → operations
     (offline: built-in rule parser)
  → ops.apply_ops validates and applies atomically
  → patches merged into the live Yjs doc field by field → everyone sees it
  → auto-arrange places new components; change list shown in the thread
```

#### 4. Guided Design (Assistant → Design)

```
User describes an idea (or picks a project-based starter)
  → LLM asks up to 7 multiple-choice questions (offline: question bank)
  → answers recorded as project memory
  → 2–4 ranked architecture options (pros/cons, cost, complexity)
  → apply to the canvas, or open as a draft branch → Compare drafts → ADR → adopt
```

#### 5. Code → Architecture

```
Public Git URL                         Private code
  → https only, public IPs only,         → archflow-codegraph <folder> -o graph.json
    DNS pinned (git 2.37+)                 (runs locally; paths + symbols, no source)
  → shallow clone, no hooks/LFS/symlinks → upload graph
  ↓                                        ↓
Separate worker process: Graphify + detections (never executes repo code)
  → normalized graph stored (zlib-compressed); clone deleted
  → Reconstruct: code units → runtime services / clients / infrastructure
  → optional LLM pass may only rename services
  → draft/from-code-<sha> branch · Drift vs any branch · Change impact
```

#### 6. Vibe Code (OpenSpec)

```
Select components (or the whole design) → ⚙ Vibe code → Write with AI
  → openspec status: artifact order (proposal → specs + design → tasks)
  → for each artifact: openspec instructions --json (OpenSpec's own template)
  → LLM writes it from design, requirements, decisions, memory, living specs
  → openspec validate --strict → issues back to the LLM until valid
  → files stored in the database → edit · archive · download · copy agent prompt
```

#### 7. AI Call Routing & Budget

```
Any feature calls llm.py
  → org budget check (monthly limit) → paused? fall back to built-in behaviour
  → per-org rate limit
  → org's chain from AI settings (else server defaults from env keys)
  → LiteLLM Router: retries, cooldowns, Retry-After, circuit breaker
  → next model in the chain on failure
  → tokens + estimated cost recorded (feature · model · project · org)
  → optional Langfuse trace
```

---

## 🔌 Integrations

| Area | Integrations |
|---|---|
| **LLM providers** | Gemini, Anthropic Claude, OpenAI, OpenRouter, Mistral, Groq, DeepSeek, xAI, Azure OpenAI, any OpenAI-compatible server (Ollama, vLLM, LM Studio) |
| **AI agents (MCP)** | Claude Code, Claude.ai, Claude Desktop, Codex, any MCP client (Streamable HTTP + OAuth 2.1, or a local stdio server) |
| **Coding agents (OpenSpec / briefs)** | Claude Code, Codex, Cursor and others via OpenSpec changes or a one-file prompt |
| **Cloud catalogs** | AWS, Azure, Google Cloud, self-hosted open source, SaaS (151 components) |
| **Cloud documentation (MCP)** | Microsoft Learn MCP server, AWS Knowledge MCP server (hosted), AWS Documentation MCP server (local, optional) |
| **Code analysis** | Public Git repositories (HTTPS), local graphs via `archflow-codegraph`; ~30 languages through tree-sitter |
| **Observability** | Langfuse (optional LLM tracing) |
| **Issue tracking** | Jira Cloud: preview, then push requirements as epics (one per owning component), stories with Given/When/Then criteria and story points, sub-tasks from the low-level design and "blocks" links from connections; idempotent re-push, 429 backoff, status read back next to requirements. Admins connect it per organization (API token encrypted); developers push |
| **GitHub Issues** | Preview, then push requirements as issues (`REQ-001: …`, description, acceptance criteria as a task list, priority, implementing components, source; labels `archflow`, `priority:<p>`, `requirement` / `nfr:<category>`). Re-pushes update changed issues and skip unchanged ones (content hash), an unrecorded issue is found again by a hidden marker, a removed requirement closes its issue; issue state is read back next to requirements. GitHub Enterprise Server via its `/api/v3` URL |
| **Confluence** | Publish a branch's architecture specification (the Markdown / PDF export) as a page in a space, optionally under a parent page; re-publishing updates the same page as a new version and skips an unchanged spec. Can reuse the org's Jira credentials on the same Atlassian site |
| **Slack / Microsoft Teams** | Incoming webhooks (Slack, Teams Workflows) get a short message with a link back when a version is saved, a review is requested or answered, a document or meeting is imported, or the project is pushed to Jira / GitHub / Confluence; each channel chooses its events. Sent in the background (a failing webhook never fails the action; the last error is shown in settings) |
| **Meetings** | Transcripts from Teams, Zoom and Google Meet (`.vtt`, `.srt`, text, Word); recordings through OpenAI Whisper (or any LiteLLM speech-to-text model) |
| **Embeddings** | Gemini or OpenAI embeddings for the knowledge base (any LiteLLM embedding model via `ARCHFLOW_MODEL_EMBEDDING`) |

GitHub, Confluence, Slack and Teams are connected per organization by admins under **Integrations** on the projects page; tokens and webhook URLs are encrypted with `ARCHFLOW_SECRET_KEY` and never shown again (only their last 4 characters). Outbound requests go only to public https hosts and never follow redirects.

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Python 3.11+, FastAPI, Pydantic v2, Uvicorn, uv |
| **AI gateway** | LiteLLM (Router: fallbacks, retries, cooldowns, cost tracking) |
| **Agents** | Pydantic AI (agents, tool loops, typed outputs) on the gateway; versioned YAML prompts (Jinja2) |
| **Evals** | `archflow-eval` golden datasets + RAGAS (optional, judged through the gateway) |
| **Frontend** | React 19, TypeScript 7, Vite 8, React Flow (`@xyflow/react` 12), Mermaid 12, Simple Icons |
| **Real-time** | Yjs + Hocuspocus 4 (Node.js collab server), WebSocket |
| **Database** | SQLAlchemy 2.0 ORM + Alembic migrations on PostgreSQL (psycopg 3) or SQLite for local use |
| **Code analysis** | Graphify (`graphifyy`), tree-sitter, isolated worker process (optional extra) |
| **Spec-driven coding** | OpenSpec 1.x (npm, run on the API server) |
| **Diagram export** | ArchFlow renderer (`backend/renderer`, Node.js 18+), html-to-image |
| **MCP** | MCP Python SDK (Streamable HTTP, OAuth 2.1) |
| **Security** | scrypt passwords, HttpOnly session cookies, AES-256-GCM secrets, HMAC collab tickets, SSRF guards |
| **Observability** | Built-in agent traces (input, output, LLM and tool calls, cost); OpenTelemetry export; Langfuse (optional) |
| **Scale-out** | Redis (optional, redis-py asyncio): shared rate limits, job state and WebSocket fan-out |
| **Deployment** | Docker Compose (nginx web, API, collab, Postgres, optional Redis), GitHub Actions CI |

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+ with [uv](https://docs.astral.sh/uv/)
- Node.js 20+ (20.19+ for OpenSpec)
- Optional: an LLM API key — or add one later in **AI settings** in the app

### 1. Clone

```bash
git clone https://github.com/narender-rk10/ArchFlow.git
cd ArchFlow
```

### 2. Configure environment

```bash
cp .env.example .env
```

For local development everything is optional. Set random values for the shared secrets, e.g.:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```env
ARCHFLOW_INTERNAL_TOKEN=<random>
POSTGRES_PASSWORD=<random>      # only if you run Postgres
GEMINI_API_KEY=...              # optional: server-wide default model
```

### 3. Start (three terminals)

```bash
# Backend — http://localhost:8000
cd backend
uv sync
uv run uvicorn archflow.main:app --reload --port 8000
# optional: OpenSpec for Vibe code
# cd backend/openspec && npm ci --omit=dev

# Collaboration server — ws://localhost:1234
cd collab
npm install
npm run dev

# Frontend — http://localhost:5173
cd frontend
npm install
npm run dev
```

Open [http://localhost:5173](http://localhost:5173) (use `localhost` exactly — the origin check rejects other hosts).

**Database.** With `DATABASE_URL` empty the API uses SQLite at `backend/data/archflow.db`, so you don't need Docker or Postgres. If your `.env` points `DATABASE_URL` at Postgres and Postgres isn't running (Docker stopped, for example), either start it with `docker compose up -d postgres` or override the value for this run only. Values already set in the environment take precedence over `.env`:

```bash
DATABASE_URL= uv run uvicorn archflow.main:app --reload --port 8000                 # bash / Git Bash
$env:DATABASE_URL=""; uv run uvicorn archflow.main:app --reload --port 8000          # PowerShell
```

**Check that everything is up:**

```bash
curl -s localhost:8000/api/health     # API → 200
curl -sI localhost:5173 | head -1     # web app → 200
# the collab terminal prints "[collab] listening on :1234"
```

### 4. First steps

1. **Create an account** — the first account becomes owner of every existing organization
2. **Set up AI** (optional) — **AI settings** on the home page: add a provider key and choose models
3. **New project** — start blank or from the **AI customer-support** example
4. **Assistant → Design** — describe your idea, answer the questions, apply an architecture
5. **Invite a teammate** — **Members & invites** → create a link; open it in a private window to see multiplayer
6. **Price it** — **Tools → Cost estimate** gives monthly production and staging cost per component, plus a CSV export. With AI on, it takes about 15 seconds
7. **Add knowledge** — **Tools → Knowledge base** → upload PRDs, policies or transcripts, then use **Ask** for answers with citations
8. **Import a meeting** — **Requirements → 🎙 Meeting** → upload a `.vtt`, `.srt`, `.docx` or recording and review the proposed requirements, decisions and action items
9. **Connect tools** (admins) — **Integrations** on the projects page: GitHub Issues, Confluence, Slack and Teams

### 5. (Optional) Repository analysis

```bash
cd backend
uv sync --extra codegraph     # then restart the API
uv run archflow-codegraph ../path/to/repo -o archflow-codegraph.json   # private code: upload only the graph
```

### 6. (Optional) Connect Claude over MCP

```bash
# Claude Code: add the server, then run /mcp and choose "Authenticate"
claude mcp add --transport http archflow http://localhost:8000/mcp

# Headless (CI): a personal access token from Tools → Connect AI agents
claude mcp add --transport http archflow http://localhost:8000/mcp --header "Authorization: Bearer afp_…"
```

**Claude.ai / Claude Desktop** connect from the internet: expose the API over HTTPS (e.g. a Cloudflare Tunnel or ngrok to port 8000), set `ARCHFLOW_PUBLIC_URL=https://your-host` and `ARCHFLOW_MCP_ALLOWED_HOSTS=your-host`, then add `https://your-host/mcp` under Settings → Connectors. A local stdio variant (`archflow-mcp`, configured in `.mcp.json`) is available for offline use.

---

## 🔧 Environment Variables

### Storage & Real-Time

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | — | `postgresql://…` switches storage to Postgres |
| `ARCHFLOW_DB` | `backend/data/archflow.db` | SQLite path when `DATABASE_URL` is unset (`:memory:` for tests) |
| `POSTGRES_PASSWORD` | — | Password for the Postgres container |
| `ARCHFLOW_REDIS_URL` | — | Optional shared Redis for several API instances (see [Scale-out](#scale-out)) |
| `ARCHFLOW_COLLAB_URL` | `http://localhost:1234` | Where the API pushes server-side edits into live documents; empty disables it |
| `ARCHFLOW_INTERNAL_TOKEN` | — | **Set it.** Shared secret between the API and the collab server (also signs collab tickets) |
| `COLLAB_PORT` / `ARCHFLOW_API` | `1234` / `http://localhost:8000` | Collab server settings |

### AI

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` / `GEMINI_API_KEYS` | — | Server-wide Gemini key, or comma-separated keys to load-balance |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | — | Server-wide fallback providers |
| `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY` | — | Optional server-wide keys for these providers |
| `ARCHFLOW_AI` | `auto` | `auto` (LLM, fall back to offline), `llm`, `offline` |
| `ARCHFLOW_MODEL_REASONING` | `gemini/gemini-3.1-pro-preview` | Default reasoning model (LiteLLM id) |
| `ARCHFLOW_MODEL_FAST` | `gemini/gemini-3.8-flash` | Default fast model |
| `ARCHFLOW_ORG_RPM` | `60` | LLM requests per minute per organization |
| `ARCHFLOW_LLM_PRICES` | — | USD per million (input, output) tokens for models LiteLLM has no price for |
| `ARCHFLOW_ALLOW_PRIVATE_AI_URLS` | `false` | Allow model endpoints on private/loopback addresses (local Ollama, vLLM); keep off on shared servers |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | — | Optional LLM tracing |
| `ARCHFLOW_DOCS` | on | `off` stops the cloud-documentation lookups (they are also off with `ARCHFLOW_AI=offline`) |
| `ARCHFLOW_DOCS_AZURE` | `https://learn.microsoft.com/api/mcp` | Azure documentation MCP server (URL, `stdio:<command>` or `off`) |
| `ARCHFLOW_DOCS_AWS` | `https://knowledge-mcp.global.api.aws` | AWS documentation MCP server, e.g. `stdio:uvx awslabs.aws-documentation-mcp-server@latest` for the local one |
| `ARCHFLOW_REDACT` | `on` | What is removed from every model request: `on` (emails, phone, card and IBAN numbers, put back into the answer; and secrets — cloud/API keys, tokens, JWTs, private keys, `password=…` — never sent nor returned), `secrets` (secrets only) or `off`. Secrets in answers are masked; counts per category and prompt-injection signals in untrusted content are recorded in the run's trace |
| `ARCHFLOW_INTAKE_MAX_MB` | `20` | Largest document accepted by **Requirements → Import**, meeting import and the knowledge base |
| `ARCHFLOW_MODEL_EMBEDDING` | Gemini, else OpenAI | Embedding model for the knowledge base (LiteLLM id); `ARCHFLOW_EMBEDDINGS=off` keeps it keyword-only |
| `ARCHFLOW_MODEL_TRANSCRIBE` | `openai/whisper-1` | Speech-to-text model for meeting recordings; `ARCHFLOW_TRANSCRIBE=off` never sends audio (recordings cannot be redacted) |
| `ARCHFLOW_KB_MAX_DOCS` | `200` | Documents per project knowledge base |
| `ARCHFLOW_COST_PRICES` | — | Override cost-estimate prices, e.g. `{"aws:db.relational": {"vcpu_hour": 0.07}}` |
| `ARCHFLOW_ALLOW_ANY_WEBHOOK` | `false` | Allow notification webhooks on hosts other than Slack's and Microsoft's (still public https only) |
| `ARCHFLOW_PROMPTS` | — | Pin prompt versions, e.g. `compare.evaluate=v1,design.turn=v2` (default: each prompt's `active` version) |
| `ARCHFLOW_TRACES` | `full` | Agent traces: `full` (with prompt and response text), `metadata` (tokens, cost, timings only) or `off` |
| `ARCHFLOW_TRACE_RETENTION_DAYS` | `30` | How long traces are kept |
| `ARCHFLOW_TRACE_MAX_CHARS` | `20000` | Per-field cap on stored trace text |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | Export agent spans over OTLP (needs `uv sync --extra otel`); `OTEL_SERVICE_NAME` defaults to `archflow` |

> **Per-organization keys and models** are set in the UI under **AI settings** and stored encrypted — the env keys above are only the defaults for organizations that configure nothing.

### Accounts, Security & MCP

| Variable | Default | Description |
|---|---|---|
| `ARCHFLOW_PUBLIC_URL` | `http://localhost:8000` | Public base URL of the API (OAuth issuer, MCP resource, `Secure` cookies on https) |
| `ARCHFLOW_ALLOWED_ORIGINS` | `http://localhost:5173` | Browser origins allowed to call the API with the session cookie |
| `ARCHFLOW_SECRET_KEY` | generated | Encrypts organizations' AI keys. If unset, the API generates `secret.key` next to the database — **set it explicitly in production** and back it up with the database |
| `ARCHFLOW_SECRET_KEY_FILE` | — | Where to keep the generated key instead |
| `ARCHFLOW_OPEN_SIGNUP` | `true` | `false` = sign-up by invite link only (the first account is always allowed) |
| `ARCHFLOW_MCP_ALLOWED_HOSTS` | — | Extra host names allowed to reach `/mcp` (your tunnel or domain) |
| `ARCHFLOW_ACCESS_TOKEN_TTL` / `ARCHFLOW_REFRESH_TOKEN_TTL` | `3600` / `2592000` | OAuth token lifetimes (seconds) |

### Repository Analysis

| Variable | Default | Description |
|---|---|---|
| `ARCHFLOW_CODEGRAPH_TIMEOUT` | `600` | Seconds the analysis worker may run |
| `ARCHFLOW_CODEGRAPH_MAX_REPO_MB` / `_CLONE_TIMEOUT` | `500` / `180` | Clone size cap and time limit |
| `ARCHFLOW_CODEGRAPH_MAX_UPLOAD_MB` | `25` | Upload size limit |
| `ARCHFLOW_CODEGRAPH_JOBS` / `_PROCS` | `1` / `2` | Concurrent analyses / parser processes per analysis |
| `ARCHFLOW_ALLOW_LOCAL_REPOS` | off | `1` lets developers analyse directories on the server (self-hosted/dev only) |
| `ARCHFLOW_ALLOW_UNPINNED_GIT` | off | With git older than 2.37, URL analysis is refused unless this is `1` (developer machines only) |

### Scale-out

| Variable | Default | Description |
|---|---|---|
| `ARCHFLOW_REDIS_URL` | — | e.g. `redis://redis:6379/0` (`rediss://` for TLS). Shares rate limits, job status/cancel and real-time fan-out between API instances. Unset = everything in-process (single instance) |
| `ARCHFLOW_REDIS_PREFIX` | `archflow` | Key and channel prefix, to share one Redis between deployments |
| `ARCHFLOW_INSTANCE_ID` | random | Name of this API instance in fan-out messages, presence and `/api/health` |

### Production Compose

| Variable | Default | Description |
|---|---|---|
| `ARCHFLOW_BUILD_EXTRAS` | `codegraph` | Empty builds the API image without repository analysis (smaller) |
| `ARCHFLOW_HTTP_PORT` | `8080` | Port the web container listens on |

See [`.env.example`](.env.example) for the full annotated list.

---

## 🐳 Docker Services

### Production (`docker-compose.prod.yml`)

| Service | Dockerfile | Port | Description |
|---|---|---|---|
| `web` | `frontend/Dockerfile` | `8080` | nginx: the built app + reverse proxy to the API and collab server |
| `api` | `backend/Dockerfile` | internal | FastAPI app (OpenSpec, diagram renderer, optional code analysis) |
| `collab` | `collab/Dockerfile` | internal | Hocuspocus real-time collaboration server |
| `postgres` | `postgres` image | internal | Database (volume `pgdata`) |
| `redis` | `redis` image | internal | Optional (profile `scale`): `docker compose --profile scale …` and `ARCHFLOW_REDIS_URL=redis://redis:6379/0` to run several API replicas |

```bash
cp .env.example .env   # set POSTGRES_PASSWORD, ARCHFLOW_INTERNAL_TOKEN, ARCHFLOW_SECRET_KEY, ARCHFLOW_PUBLIC_URL
docker compose -f docker-compose.prod.yml up -d --build   # web on :8080 — put TLS in front
```

See [docs/deploy.md](docs/deploy.md) for operations, backups and current single-instance limits.

### Development (`docker-compose.yml`)

| Service | Port | Description |
|---|---|---|
| `postgres` | `5432` | Local Postgres for running the API against it |
| `redis` | `6379` | Local Redis (`ARCHFLOW_REDIS_URL=redis://localhost:6379/0` to try scale-out) |

### Useful commands

```bash
# Production stack
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml up -d --build api      # rebuild one service

# Run the API on local Postgres
docker compose up -d postgres redis
DATABASE_URL=postgresql://archflow:<POSTGRES_PASSWORD>@localhost:5432/archflow uv run uvicorn archflow.main:app --port 8000
```

The schema is defined by the SQLAlchemy models in `backend/archflow/db/models.py`; Alembic migrations (`backend/archflow/db/migrations/`) apply automatically on startup, and databases created by earlier versions are converged in place. Branch edits take a row lock (`SELECT … FOR UPDATE`), so several API processes can run side by side.

---

## 📁 Project Structure

```
ArchFlow/
├── backend/
│   ├── archflow/
│   │   ├── main.py              # FastAPI app: REST + WebSocket routes, wiring
│   │   ├── models.py            # Node, Edge, Requirement, ProjectState
│   │   ├── ops.py               # The single mutation pipeline + conflict detection
│   │   ├── diff.py              # Structural diff between versions / branches
│   │   ├── analysis.py          # Requirement lint, architecture checks, traceability, health
│   │   ├── catalog.py           # 151-component catalog + cloud conversion
│   │   ├── templates.py         # Starter architectures
│   │   ├── lld.py               # Low-level design generation and views
│   │   ├── diagrams.py          # C4, sequence, ER, deployment, data-flow diagrams as Mermaid / PlantUML
│   │   ├── ai.py                # AI canvas edits (tool calls → ops) + offline parser
│   │   ├── chat.py              # Guided design chat
│   │   ├── conversations.py     # Shared threads, summaries, project memory
│   │   ├── llm.py               # LiteLLM gateway: chains, fallbacks, limits, budgets
│   │   ├── ai_providers.py      # Provider catalog, model ids, endpoint guard
│   │   ├── usage.py             # AI spend tracking and budgets
│   │   ├── decisions.py         # ADRs, compare drafts, adopt
│   │   ├── merge.py             # Branch merge: three-way diff, conflicts, AI-suggested resolutions
│   │   ├── evaluation.py        # Draft scoring (LLM or heuristic)
│   │   ├── openspec_service.py  # OpenSpec workflow (Vibe code)
│   │   ├── vibe.py · change_spec.py  # Quick briefs and change specs
│   │   ├── diagram_export.py    # Interactive HTML export via the renderer
│   │   ├── collab_sync.py       # API ↔ collab server (field-level patches)
│   │   ├── users.py · auth.py · authz.py · accounts.py  # Accounts, sessions, roles, orgs, CSRF
│   │   ├── secrets_box.py       # AES-256-GCM encryption of org secrets
│   │   ├── mcp_remote.py        # Remote MCP server (OAuth 2.1, 41 tools)
│   │   ├── mcp_server.py        # Local stdio MCP server
│   │   ├── store.py             # persistence (one store for SQLite and Postgres)
│   │   ├── db/                  # SQLAlchemy models, column types, Alembic migrations
│   │   ├── agents/              # Pydantic AI runtime on the gateway, prompt registry, run tracing
│   │   ├── prompts/             # Versioned YAML prompts + prompts.lock.json
│   │   ├── evals/               # archflow-eval: golden datasets, checks, RAGAS judge
│   │   ├── review.py            # AI review: specialist agents in parallel
│   │   ├── exports.py           # Spec exports (Markdown, print/PDF, JSON, YAML) and coding-agent guides
│   │   ├── suggestions.py       # LLM starting prompts, follow-ups and quick replies
│   │   ├── jev.py               # Jev decision API client (Compare drafts)
│   │   ├── observability.py     # Trace storage, retention and the admin API
│   │   └── codegraph/           # Repo analysis: Graphify adapter, worker, CLI, detections,
│   │                            # clone + SSRF guard, jobs, reconstruction, drift, impact
│   ├── openspec/                # OpenSpec npm dependency
│   ├── renderer/                # ArchFlow diagram renderer (interactive HTML export)
│   ├── tests/                   # pytest suite (SQLite + optional Postgres)
│   └── Dockerfile
├── collab/
│   ├── src/server.ts            # Hocuspocus server: tickets, persistence, projection
│   ├── src/selftest.ts          # End-to-end test: two live clients + AI/MCP edits
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── App.tsx              # Routing: landing, auth, home, workspace
│   │   ├── components/
│   │   │   ├── Landing.tsx      # Public landing page
│   │   │   ├── ProjectPicker.tsx  # Home dashboard
│   │   │   ├── Workspace.tsx    # Canvas, sync, panels, top bar
│   │   │   ├── CommandBar.tsx   # The assistant (Design / Edit / Ask, dockable)
│   │   │   ├── DesignChat.tsx   # Guided design flow
│   │   │   ├── Inspector.tsx · Palette.tsx · ArchNode.tsx · LldViews.tsx
│   │   │   ├── RequirementsPanel.tsx · IssuesPanel.tsx · VersionsPanel.tsx · DecisionsPanel.tsx
│   │   │   ├── OpenSpecPanel.tsx · CodeDialog.tsx · ConnectDialog.tsx · MemoryDialog.tsx
│   │   │   ├── DiagramsDialog.tsx  # C4, sequence, ER, deployment, data flow (Mermaid)
│   │   │   └── OrgDialogs.tsx   # Members, AI settings, AI usage
│   │   ├── api.ts · types.ts    # API client and shared types
│   │   ├── collab.ts · realtime.ts  # Yjs provider, WebSocket events
│   │   ├── assistant.ts · suggestions.ts  # Conversation state, project-based suggestions
│   │   └── layout.ts · exportImage.ts  # Tidy-up layout, PNG/SVG export
│   └── Dockerfile
├── deploy/nginx.conf.template   # Web container config
├── docs/
│   ├── screenshots/             # README screenshots
│   ├── deploy.md                # Production deployment
│   ├── production-plan.md       # Production roadmap
│   └── evaluation/              # CareLink evaluation (spec, rubric, results)
├── scripts/eval_carelink.py     # Runs the CareLink evaluation
├── .github/workflows/ci.yml     # CI: tests, builds, collab e2e, images
├── docker-compose.yml           # Dev: Postgres + Redis
├── docker-compose.prod.yml      # Production stack
└── .env.example
```

---

## 🔬 Testing

```bash
cd backend && uv run pytest        # add TEST_DATABASE_URL=postgresql://… to also run the Postgres store tests
cd frontend && npm run build       # typecheck + production build
cd collab && npm test              # end-to-end: two live clients + MCP/AI edits (needs the API and collab server)
cd frontend && npx playwright install chromium && npm run e2e   # browser end-to-end (starts its own servers)
E2E_REAL_AI=1 npm run e2e          # ... plus the AI flows, with the provider keys in .env
cd backend && uv run archflow-eval # AI quality evals against your models (see Agents, Versioned Prompts, Evals & Traces)
```

The Playwright suite (`frontend/e2e/`) starts the API, collab server and web app on their own ports with a fresh database and drives them in Chromium: sign up, create a project, edit the canvas from the assistant and the inspector, requirements, **connect code** (upload a code graph and reconstruct the architecture), **get the diagram** (PNG, SVG and interactive HTML downloads), history (save, compare, restore, undo a restore), decisions (compare drafts, record, adopt) and health. With `E2E_REAL_AI=1` it also checks the AI: starting points in Design, follow-ups after Ask answers, question bubbles in Edit, the parallel review, and traces showing who asked what.

CI (`.github/workflows/ci.yml`) runs the backend tests on SQLite and Postgres (including the migration drift checks), the frontend build and audit, the Playwright suite, the collaboration end-to-end test, and the Docker image builds.

An end-to-end AI evaluation on a complex spec (a multi-region telehealth platform) lives in [docs/evaluation](docs/evaluation): requirements, a scoring rubric and results, reproducible with `scripts/eval_carelink.py`.

---

## 🗺️ Roadmap

The full production plan is in [docs/production-plan.md](docs/production-plan.md). In rough order of leverage:

1. **Per-project role overrides, SSO** and approvals on more objects than versions
2. **Repository analysis**: GitHub App, drift on every push/PR, PR review comments
3. **Jira sync** — ✅ push (epics, stories, sub-tasks, links) with dry-run preview and status pull are done; next: Jira
   webhooks for live status, status shown on canvas components, and pulling new Jira issues back as requirements
4. **More diagrams** from the same model: ~~sequence, C4, ER, deployment, data flow~~ (done, with Mermaid / PlantUML export); still to do: agent graph, MCP topology
5. **Document intake** — ~~SRS/PDF/Word/Markdown → requirements~~ and ~~meeting transcripts and recordings → requirements,
   decisions and action items~~ (done);
   next: speaker diarization for uploaded recordings and live meeting capture
6. **Generation** of tests, infrastructure as code and documentation (SRS/BRD/runbooks)
7. **Scale-out**
   - **Done:** shared Redis (`ARCHFLOW_REDIS_URL`) for rate limits, job status and cancel, and real-time fan-out
     and presence across API instances.
   - **Next:** Redis for the Hocuspocus collab server, so several collab instances can be active (it needs sticky
     routing by document today), and a durable job queue so jobs survive restarts.

---

## 🤝 Contributing

Issues and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md) (setup, tests, migrations, prompt
versions and the contribution terms), the [code of conduct](CODE_OF_CONDUCT.md), and [SECURITY.md](SECURITY.md) for
reporting vulnerabilities privately.

---

## 📄 License

Copyright © 2026 **Narender Keswani**

Licensed under **Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International**.

- ✅ **Share** — copy and redistribute in any medium or format
- ✅ **Attribution** — credit Narender Keswani and link to this repository
- ❌ **NonCommercial** — no commercial use
- ❌ **NoDerivatives** — no modified distributions

For commercial licensing: **narender.rk10@gmail.com**
