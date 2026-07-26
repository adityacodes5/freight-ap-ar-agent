"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// Top-level tabs. Analytics is a section — cash flow, overdue AR/AP, margins and
// customers all live under it (see AnalyticsTabs), so it stays highlighted for
// any of those routes.
const TABS = [
  { href: "/", label: "Today", match: (p: string) => p === "/" },
  { href: "/runs", label: "Runs", match: (p: string) => p.startsWith("/runs") },
  {
    href: "/analytics",
    label: "Analytics",
    match: (p: string) =>
      p.startsWith("/analytics") || p.startsWith("/overdue") || p.startsWith("/payables"),
  },
  { href: "/receivables", label: "Receivables", match: (p: string) => p.startsWith("/receivables") },
  { href: "/lookup", label: "Lookup", match: (p: string) => p.startsWith("/lookup") },
  { href: "/agent", label: "Agent", match: (p: string) => p.startsWith("/agent") },
];

export function Nav({ email, onSignOut }: { email?: string; onSignOut?: () => void }) {
  const path = usePathname();
  return (
    <div className="flex items-center justify-between gap-3 mb-6 sm:mb-8">
      <div className="flex items-center gap-1 bg-[#141414] border border-[#242424] rounded-xl p-1 overflow-x-auto no-scrollbar">
        {TABS.map((t) => {
          const active = t.match(path);
          return (
            <Link
              key={t.href}
              href={t.href}
              className={`shrink-0 px-3 sm:px-4 py-2 rounded-lg text-sm font-medium transition-all whitespace-nowrap ${
                active ? "bg-[#262626] text-white" : "text-gray-500 hover:text-gray-300"
              }`}
            >
              {t.label}
            </Link>
          );
        })}
      </div>
      {email && (
        <div className="flex items-center gap-3 text-sm shrink-0">
          <span className="text-gray-500 hidden md:inline">{email}</span>
          {onSignOut && (
            <button onClick={onSignOut} className="text-gray-500 hover:text-gray-300">
              Sign out
            </button>
          )}
        </div>
      )}
    </div>
  );
}
