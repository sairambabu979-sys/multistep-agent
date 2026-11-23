"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { useSession } from "@/components/auth";
import { Button } from "@/components/ui/button";

const NAV = [
  { href: "/", label: "Mission Control" },
  { href: "/approvals", label: "Approval Center" },
  { href: "/metrics", label: "Metrics" },
  { href: "/preferences", label: "Preferences Studio" },
] as const;

type Theme = "light" | "dark" | "system";

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("system");
  useEffect(() => {
    const saved = document.documentElement.dataset.theme;
    setTheme(saved === "light" || saved === "dark" ? saved : "system");
  }, []);
  function choose(next: Theme) {
    setTheme(next);
    const root = document.documentElement;
    if (next === "system") delete root.dataset.theme;
    else root.dataset.theme = next;
    try {
      if (next === "system") window.localStorage.removeItem("aegis.theme");
      else window.localStorage.setItem("aegis.theme", next);
    } catch {
      // storage unavailable: the choice lasts for this page
    }
  }
  return (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-muted">Theme</span>
      <select
        aria-label="Theme"
        className="rounded-md border border-border bg-bg px-2 py-1"
        value={theme}
        onChange={(e) => choose(e.target.value as Theme)}
      >
        <option value="system">System</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
    </label>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const { me, signOut } = useSession();
  const path = usePathname();
  return (
    <>
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-4 py-3">
          <Link href="/" className="text-lg font-bold">
            AEGIS
          </Link>
          <nav aria-label="Main" className="flex flex-1 flex-wrap gap-1">
            {NAV.map((item) => {
              const current = item.href === "/" ? path === "/" : path.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={current ? "page" : undefined}
                  className={`rounded-md px-3 py-1.5 text-sm ${current ? "bg-raised font-semibold" : "text-muted hover:text-text"}`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
          <ThemeToggle />
          <span className="text-sm text-muted">
            {me.name} · <span className="font-medium text-text">{me.role}</span>
          </span>
          <Button variant="ghost" onClick={signOut}>
            Sign out
          </Button>
        </div>
      </header>
      <main id="main" tabIndex={-1} className="mx-auto max-w-6xl px-4 py-6">
        {children}
      </main>
    </>
  );
}
