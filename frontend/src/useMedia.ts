import { useEffect, useState } from "react";

/** Whether a CSS media query matches, kept up to date as the window changes. */
export function useMedia(query: string): boolean {
  const get = () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(query).matches;
  const [matches, setMatches] = useState(get);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(query);
    const on = () => setMatches(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return matches;
}

/** Below this width the side panels become drawers and the canvas gets a floating action bar (keep in sync with styles.css). */
export const COMPACT_QUERY = "(max-width: 1024px)";
