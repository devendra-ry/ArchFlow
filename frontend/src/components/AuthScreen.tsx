import { Check } from "lucide-react";
import { Brand } from "./Logo";
import { useEffect, useState } from "react";
import { api } from "../api";
import type { InviteInfo, Me } from "../types";

interface Props {
  mode: "login" | "signup";
  invite?: string | null;
  /** From the SSO callback: ask for the two-step code with this challenge. */
  mfaToken?: string | null;
  /** From the SSO callback: why single sign-on failed. */
  ssoError?: string | null;
  onMode: (mode: "login" | "signup") => void;
  onSignedIn: (me: Me) => void;
  themeToggle?: React.ReactNode;
}

const MIN_PASSWORD = 10;

/**
 * Sign in / create an account. With an invite token, signing up joins that organization. Sign-in can continue
 * with a two-step code, or go through the organization's single sign-on.
 */
export function AuthScreen({ mode, invite, mfaToken, ssoError, onMode, onSignedIn, themeToggle }: Props) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(ssoError ?? "");
  const [busy, setBusy] = useState(false);
  const [info, setInfo] = useState<InviteInfo | null>(null);
  const [step, setStep] = useState<"password" | "code" | "sso">(mfaToken ? "code" : "password");
  const [challenge, setChallenge] = useState(mfaToken ?? "");
  const [code, setCode] = useState("");

  useEffect(() => {
    if (!invite) { setInfo(null); return; }
    api.inviteInfo(invite).then((i) => { setInfo(i); if (i.email) setEmail(i.email); })
      .catch((e) => setError((e as Error).message));
  }, [invite]);
  useEffect(() => { if (mfaToken) { setChallenge(mfaToken); setStep("code"); } }, [mfaToken]);
  useEffect(() => { if (ssoError) setError(ssoError); }, [ssoError]);

  const finish = async (me: Me) => {
    if (invite) { await api.acceptInvite(invite); me = await api.me(); }
    onSignedIn(me);
  };

  const run = async (fn: () => Promise<void>) => {
    setError("");
    setBusy(true);
    try { await fn(); } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (mode === "signup" && password.length < MIN_PASSWORD) {
      setError(`Password must be at least ${MIN_PASSWORD} characters.`);
      return;
    }
    run(async () => {
      if (mode === "signup") {
        onSignedIn(await api.signup(email.trim(), name.trim(), password, invite ?? undefined));
        return;
      }
      const r = await api.login(email.trim(), password);
      if ("mfa_required" in r) { setChallenge(r.mfa_token); setCode(""); setStep("code"); return; }
      await finish(r);
    });
  };

  const submitCode = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => finish(await api.mfaVerify(challenge, code.trim())));
  };

  const submitSso = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => {
      const r = await api.ssoDiscover(email.trim());
      window.location.href = r.start_url; // the identity provider's sign-in page, then back here
    });
  };

  const back = () => { setError(""); setStep("password"); setCode(""); };

  return (
    <div className="auth">
      <aside className="auth-aside">
        <a className="brand-link" href="#/" title="Back to the home page"><Brand /></a>
        <div className="auth-pitch">
          <h1>Design the system once. Keep every view of it in sync.</h1>
          <ul>
            <li><Check size={16} strokeWidth={2.2} aria-hidden /> Requirements traced to the components that satisfy them</li>
            <li><Check size={16} strokeWidth={2.2} aria-hidden /> A live canvas your team and AI agents edit together</li>
            <li><Check size={16} strokeWidth={2.2} aria-hidden /> Versions, branches and decisions, like code</li>
          </ul>
        </div>
        <p className="auth-flow">Requirements → Architecture → Canvas → Code</p>
      </aside>
      <main className="auth-main">
        <div className="auth-top">{themeToggle}</div>
      {step === "code" ? (
        <form className="card auth-card" onSubmit={submitCode}>
          <h2>Enter your code</h2>
          <p className="small muted">Open your authenticator app and enter the 6-digit code for ArchFlow. Lost your phone? Use a recovery code.</p>
          <label className="field"><span>Code</span>
            <input required autoFocus autoComplete="one-time-code" inputMode="text" maxLength={20} value={code}
              onChange={(e) => setCode(e.target.value)} placeholder="123456" />
          </label>
          {error && <p className="error small" role="alert">{error}</p>}
          <div className="row between">
            <button type="button" className="link" onClick={back}>Start over</button>
            <button className="primary" disabled={busy || !code.trim()}>{busy ? "…" : "Continue"}</button>
          </div>
        </form>
      ) : step === "sso" ? (
        <form className="card auth-card" onSubmit={submitSso}>
          <h2>Sign in with SSO</h2>
          <p className="small muted">Enter your work email. We'll send you to your company's sign-in page.</p>
          <label className="field"><span>Work email</span>
            <input type="email" required autoFocus autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          {error && <p className="error small" role="alert">{error}</p>}
          <div className="row between">
            <button type="button" className="link" onClick={back}>Use a password instead</button>
            <button className="primary" disabled={busy}>{busy ? "…" : "Continue"}</button>
          </div>
        </form>
      ) : (
        <form className="card auth-card" onSubmit={submit}>
          <h2>{mode === "signup" ? "Create your account" : "Welcome back"}</h2>
          <p className="small muted auth-sub">{mode === "signup" ? "Start from a blank canvas or the example project." : "Sign in to your ArchFlow workspace."}</p>
          {info && (
            <p className="small invite-note">
              You're invited to <b>{info.org_name}</b> as <b>{info.role}</b>.
              {mode === "login" ? " Sign in to accept, or create an account." : " Create your account to join."}
            </p>
          )}
          <label className="field"><span>Email</span>
            <input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          {mode === "signup" && (
            <label className="field"><span>Your name (shown to collaborators)</span>
              <input required maxLength={80} autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} />
            </label>
          )}
          <label className="field"><span>Password{mode === "signup" ? ` (at least ${MIN_PASSWORD} characters)` : ""}</span>
            <input type="password" required autoComplete={mode === "signup" ? "new-password" : "current-password"}
              minLength={mode === "signup" ? MIN_PASSWORD : undefined} value={password} onChange={(e) => setPassword(e.target.value)} />
          </label>
          {error && <p className="error small" role="alert">{error}</p>}
          <div className="row between">
            <button type="button" className="link" onClick={() => { setError(""); onMode(mode === "signup" ? "login" : "signup"); }}>
              {mode === "signup" ? "Already have an account? Sign in" : "New here? Create an account"}
            </button>
            <button className="primary" disabled={busy}>{busy ? "…" : mode === "signup" ? "Create account" : "Sign in"}</button>
          </div>
          {mode === "login" && (
            <div className="auth-sso">
              <span className="muted small">or</span>
              <button type="button" onClick={() => { setError(""); setStep("sso"); }}>Sign in with SSO</button>
            </div>
          )}
        </form>
      )}
      </main>
    </div>
  );
}
