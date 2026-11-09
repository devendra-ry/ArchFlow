/**
 * The app's icon set: one stroked family (Lucide, ISC licence) at consistent sizes, instead of text glyphs that render
 * differently per platform (and as colour emoji on some phones). Import icons from here so sizes and stroke stay uniform.
 */
import type { LucideIcon, LucideProps } from "lucide-react";

export {
  Activity, ArrowLeft, ArrowLeftRight, Bell, BookOpen, Check, ChevronDown, CircleDollarSign, Download, FileCode2, FileText,
  GitBranch, GitPullRequest, Hammer, HeartPulse, History, KeyRound, Layers, ListChecks, Maximize2, MessageSquare, Monitor,
  Moon, Plug, Plus, Scale, Search, ShieldCheck, Sparkles, SquareTerminal, Sun, Upload, Workflow, X, Zap,
} from "lucide-react";

/** Render an icon at the standard size for its context: 14 (inline, menus), 16 (buttons, rails) or 18 (headers). */
export function Icon({ as: Cmp, size = 16, ...rest }: { as: LucideIcon; size?: 14 | 16 | 18 | 20 } & Omit<LucideProps, "ref">) {
  return <Cmp size={size} strokeWidth={size <= 14 ? 2 : 1.75} aria-hidden focusable={false} className="icon" {...rest} />;
}
