"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// Sub-navigation for the Analytics section. Rendered under the main Nav on every
// analytics-family page so the group reads as one area with its own tabs.
const SUBTABS = [
  { href: "/analytics", label: "Cash flow" },
  { href: "/overdue", label: "Overdue AR" },
  { href: "/payables", label: "Overdue AP" },
  { href: "/analytics/performance", label: "Margins & timing" },
  { href: "/analytics/customers", label: "Customers" },
  { href: "/analytics/carriers", label: "Carriers" },
  { href: "/analytics/statements", label: "Statements" },
];

export function AnalyticsTabs() {
  const path = usePathname();
  return (
    <div className="flex flex-wrap items-center gap-1 mb-6 border-b border-[#1e1e1e] pb-3">
      {SUBTABS.map((t) => {
        // exact match, except cash flow ("/analytics") must not swallow its children
        const active = path === t.href;
        return (
          <Link
            key={t.href}
            href={t.href}
            className={`px-3.5 py-1.5 rounded-lg text-sm font-medium transition-all ${
              active ? "bg-[#1f1f1f] text-white" : "text-gray-500 hover:text-gray-300 hover:bg-[#161616]"
            }`}
          >
            {t.label}
          </Link>
        );
      })}
    </div>
  );
}
