/** The ArchFlow mark: two components flowing into a third, on an ink tile. */
export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg className="logo" width={size} height={size} viewBox="0 0 28 28" aria-hidden focusable={false}>
      <rect width="28" height="28" rx="7" className="logo-tile" />
      <path d="M9.5 10.5v2.5a2 2 0 0 0 2 2h5a2 2 0 0 0 2-2v-2.5M14 15v2.5" className="logo-flow" />
      <rect x="6.5" y="6" width="6" height="4.5" rx="1.4" className="logo-node" />
      <rect x="15.5" y="6" width="6" height="4.5" rx="1.4" className="logo-node" />
      <rect x="10.5" y="17.5" width="7" height="4.5" rx="1.4" className="logo-node accent" />
    </svg>
  );
}

/** Logo and wordmark, as used in bars and on the sign-in screen. */
export function Brand({ small }: { small?: boolean }) {
  return <span className={`home-brand ${small ? "small" : ""}`}><Logo size={small ? 22 : 28} /> ArchFlow</span>;
}
