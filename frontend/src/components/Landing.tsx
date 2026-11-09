import type { MouseEvent, ReactNode } from "react";
import {
  ArrowLeftRight, ArrowRight, Bot, Cpu, FileCode2, GitBranch, HeartPulse, Plug, SquareTerminal, Users, type LucideIcon,
} from "lucide-react";
import { Brand } from "./Logo";

interface Props {
  themeToggle?: ReactNode;
  onSignIn: () => void;
  onSignUp: () => void;
}

const STEPS = [
  { title: "Describe it", text: "Tell the design chat what you are building. It asks a few multiple-choice questions and records your answers as requirements." },
  { title: "Pick an architecture", text: "Compare generated options, then refine on a live canvas: drag components, connect them, or type “add Redis between Orders and its DB”." },
  { title: "Check it", text: "Health checks flag missing pieces, and every requirement is traced to the components that satisfy it." },
  { title: "Ship it", text: "Turn components into OpenSpec change specs for coding agents, and compare the design with your real repository." },
];

const FEATURES: { icon: LucideIcon; title: string; text: string }[] = [
  { icon: Bot, title: "AI design chat", text: "From a one-line idea to a reviewed architecture, with the reasoning saved to project memory." },
  { icon: Users, title: "Live collaboration", text: "Edit the same canvas together with presence and instant sync. Offline edits sync when you reconnect." },
  { icon: GitBranch, title: "Branches & versions", text: "Try ideas on a branch, diff versions, restore any commit and adopt a design through a recorded decision." },
  { icon: HeartPulse, title: "Health & traceability", text: "Rules catch single points of failure, missing auth and orphan components, and show which requirements each part covers." },
  { icon: SquareTerminal, title: "Vibe code with OpenSpec", text: "Select components and generate a structured change spec that Claude Code, Codex or Cursor can implement." },
  { icon: FileCode2, title: "Code analysis", text: "Reconstruct a repository's architecture, detect drift from the design and estimate the impact of a change." },
  { icon: Plug, title: "MCP for agents", text: "Connect Claude, Codex and other agents so they can read and update the architecture over MCP." },
  { icon: Cpu, title: "Bring your own model", text: "Gemini, Claude, OpenAI, OpenRouter, Mistral, Groq, Azure or a self-hosted model, with fallbacks and budgets." },
  { icon: ArrowLeftRight, title: "Convert & export", text: "Swap a design between AWS, Azure, Google Cloud and open source, then export PNG, SVG or interactive HTML." },
];

/** A small, static architecture sketch for the hero, drawn like the real canvas. */
function HeroDiagram() {
  const boxes = [
    { x: 24, y: 36, w: 132, label: "Web & WhatsApp", sub: "Client", kind: "client" },
    { x: 196, y: 36, w: 128, label: "API Gateway", sub: "Gateway", kind: "edge" },
    { x: 364, y: 36, w: 132, label: "Support Agent", sub: "AI agent", kind: "ai" },
    { x: 196, y: 150, w: 128, label: "Orders Service", sub: "Service", kind: "svc" },
    { x: 364, y: 150, w: 132, label: "MCP Tools", sub: "MCP", kind: "ai" },
    { x: 196, y: 262, w: 128, label: "Orders DB", sub: "PostgreSQL", kind: "db" },
    { x: 364, y: 262, w: 132, label: "Redis Cache", sub: "Cache", kind: "db" },
  ];
  const lines: [number, number, number, number][] = [[156, 61, 196, 61], [324, 61, 364, 61], [260, 86, 260, 150], [430, 86, 430, 150],
    [260, 200, 260, 262], [324, 175, 364, 175], [324, 287, 364, 287]];
  return (
    <svg className="landing-diagram" viewBox="0 0 520 336" role="img" aria-label="Example architecture on the ArchFlow canvas">
      <defs>
        <pattern id="lgrid" width="16" height="16" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".9" className="lgrid-dot" /></pattern>
        <marker id="larrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0L8 4L0 8z" className="larrow" />
        </marker>
      </defs>
      <rect width="520" height="336" fill="url(#lgrid)" />
      {lines.map(([x1, y1, x2, y2], i) => <line key={i} x1={x1} y1={y1} x2={x2} y2={y2} className="lline" markerEnd="url(#larrow)" />)}
      {boxes.map((b) => (
        <g key={b.label} className={`lnode ${b.kind}`}>
          <rect x={b.x} y={b.y} width={b.w} height="50" rx="9" className="lbox" />
          <rect x={b.x + 10} y={b.y + 13} width="24" height="24" rx="6" className="ltile" />
          <text x={b.x + 42} y={b.y + 23} className="ltext">{b.label}</text>
          <text x={b.x + 42} y={b.y + 37} className="lsub">{b.sub}</text>
        </g>
      ))}
      <g className="lchip">
        <rect x="24" y="148" width="144" height="58" rx="10" />
        <circle cx="40" cy="168" r="4" className="lchip-dot" />
        <text x="52" y="172" className="lchip-t">Health 98%</text>
        <text x="36" y="192" className="lchip-s">17 requirements traced</text>
      </g>
    </svg>
  );
}

const scrollTo = (id: string) => (e: MouseEvent) => {
  e.preventDefault();
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
};

/** Public landing page, shown at / to signed-out visitors. */
export function Landing({ themeToggle, onSignIn, onSignUp }: Props) {
  return (
    <div className="landing">
      <header className="landing-bar">
        <Brand />
        <nav className="landing-nav">
          <a href="#how" onClick={scrollTo("how")}>How it works</a>
          <a href="#features" onClick={scrollTo("features")}>Features</a>
        </nav>
        <span className="spacer" />
        {themeToggle}
        <button className="ghost" onClick={onSignIn}>Sign in</button>
        <button className="primary" onClick={onSignUp}>Get started</button>
      </header>

      <section className="landing-hero">
        <div className="landing-copy">
          <p className="landing-eyebrow"><span className="pulse-dot" aria-hidden /> Open source · self-hosted · works with or without AI</p>
          <h1>Architecture as one living model, <span className="landing-h1-muted">from requirements to code.</span></h1>
          <p className="landing-lead">ArchFlow turns an idea into requirements, an architecture and a live canvas your team and AI agents
            edit together, then into change specs your coding agents can build.</p>
          <div className="landing-cta">
            <button className="primary big" onClick={onSignUp}>Create a free account <ArrowRight size={16} strokeWidth={2} aria-hidden /></button>
            <button className="big" onClick={onSignIn}>Sign in</button>
          </div>
        </div>
        <div className="landing-visual">
          <div className="landing-window-bar" aria-hidden><span /><span /><span /><em>support-platform · main</em></div>
          <HeroDiagram />
        </div>
      </section>

      <section id="how" className="landing-section">
        <p className="landing-kicker">How it works</p>
        <h2>From a sentence to a design you can build.</h2>
        <ol className="landing-steps">
          {STEPS.map((s, i) => (
            <li key={s.title} className="landing-step">
              <span className="landing-step-n">{String(i + 1).padStart(2, "0")}</span>
              <b>{s.title}</b>
              <p>{s.text}</p>
            </li>
          ))}
        </ol>
      </section>

      <section id="features" className="landing-section">
        <p className="landing-kicker">Features</p>
        <h2>Everything in one model.</h2>
        <p className="landing-sub">No more diagrams that drift from the docs, or docs that drift from the code.</p>
        <div className="landing-features">
          {FEATURES.map((f) => (
            <div key={f.title} className="landing-feature">
              <f.icon size={18} strokeWidth={1.75} aria-hidden className="landing-icon" />
              <b>{f.title}</b>
              <p>{f.text}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="landing-final">
        <h2>Start with an idea. Leave with a design you can build.</h2>
        <p>Create an account, then start from a blank canvas or the AI customer-support example.</p>
        <button className="big" onClick={onSignUp}>Get started <ArrowRight size={16} strokeWidth={2} aria-hidden /></button>
      </section>

      <footer className="landing-foot">
        <Brand small />
        <span>Requirements → Architecture → Canvas → Code</span>
      </footer>
    </div>
  );
}
