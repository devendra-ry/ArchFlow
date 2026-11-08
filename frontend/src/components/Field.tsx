import { useEffect, useRef, useState } from "react";
import { Select, plainOptions } from "./Select";

/**
 * An input that edits one field of a shared object.
 *
 * It remembers the project revision at the moment the user started editing (``baseRev``) and
 * sends it with the change, so the server can detect that someone else changed the same field
 * in the meantime instead of silently overwriting it.
 */
interface Props {
  label: string;
  value: string;
  rev: number;
  onCommit: (value: string, baseRev: number) => void;
  multiline?: boolean;
  options?: readonly string[];
  placeholder?: string;
}

export function Field({ label, value, rev, onCommit, multiline, options, placeholder }: Props) {
  const [draft, setDraft] = useState(value);
  const [theirs, setTheirs] = useState<string | null>(null); // value someone else saved while I was editing
  const editing = useRef<number | null>(null);

  useEffect(() => {
    if (editing.current === null) setDraft(value);
    else if (value !== draft) setTheirs(value);
  }, [value]); // eslint-disable-line react-hooks/exhaustive-deps

  const begin = () => { if (editing.current === null) editing.current = rev; };
  const commit = (v = draft) => {
    const base = editing.current ?? rev;
    editing.current = null;
    setTheirs(null);
    if (v !== value) onCommit(v, base);
  };
  const takeTheirs = () => { editing.current = null; setTheirs(null); setDraft(value); };

  if (options) {
    return (
      <label className="field">
        <span>{label}</span>
        <Select value={value} onChange={(v) => onCommit(v, rev)} aria-label={label} options={plainOptions(options)} />
      </label>
    );
  }
  const common = {
    value: draft,
    placeholder,
    onFocus: begin,
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => { begin(); setDraft(e.target.value); },
    onBlur: () => commit(),
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && (!multiline || e.ctrlKey || e.metaKey)) { e.preventDefault(); (e.target as HTMLElement).blur(); }
      if (e.key === "Escape") { editing.current = null; setTheirs(null); setDraft(value); (e.target as HTMLElement).blur(); }
    },
  };
  return (
    <label className="field">
      <span>{label}</span>
      {multiline ? <textarea rows={3} {...common} /> : <input {...common} />}
      {theirs !== null && (
        <span className="field-conflict" role="status">
          Someone else changed this to “{theirs || "(empty)"}”. Saving keeps yours.
          <button type="button" className="link" onMouseDown={(e) => e.preventDefault()} onClick={takeTheirs}>Use theirs</button>
        </span>
      )}
    </label>
  );
}
