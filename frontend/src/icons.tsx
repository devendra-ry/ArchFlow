/**
 * The app's icon set: one stroked family (Lucide, ISC licence) at consistent sizes, instead of text glyphs that render
 * differently per platform (and as colour emoji on some phones). Import icons from here so sizes and stroke stay uniform.
 */
import type { LucideIcon, LucideProps } from "lucide-react";

export {
  Activity, ArrowLeft, ArrowLeftRight, ArrowUp, ArrowUpDown, Bell, BookOpen, Box, Check, ChevronDown, ChevronLeft,
  ChevronRight, ChevronUp, Circle, CircleCheck, CircleDollarSign, CornerDownRight, Download, ExternalLink, FileCode2,
  FileText, GitBranch, GitPullRequest, Hammer, HeartPulse, History, Info, KeyRound, Layers, ListChecks, Maximize2,
  MessageSquare, Mic, Monitor, Moon, Pencil, Plug, Plus, Printer, RefreshCw, RotateCw, Scale, Search, ShieldCheck,
  Sparkles, SquareTerminal, Star, Sun, Target, Trash2, TriangleAlert, Undo2, Upload, Workflow, X, Zap,
} from "lucide-react";

/** Render an icon at the standard size for its context: 12 (badges, inline marks), 14 (inline, menus), 16 (buttons, rails)
 * or 18 (headers). */
export function Icon({ as: Cmp, size = 16, ...rest }: { as: LucideIcon; size?: 12 | 14 | 16 | 18 | 20 } & Omit<LucideProps, "ref">) {
  return <Cmp size={size} strokeWidth={size <= 12 ? 2.25 : size <= 14 ? 2 : 1.75} aria-hidden focusable={false} className="icon" {...rest} />;
}
