import { Activity, FileText, Globe, LogOut, ShieldCheck, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { t } from "@/lib/strings";
import { cn } from "@/lib/utils";

import { ThemeSwitch } from "./ThemeSwitch";
import { Button } from "./ui/button";

const ITEMS: { to: string; label: string; icon: LucideIcon; end?: boolean }[] = [
  { to: "/", label: t.nav.monitors, icon: Activity, end: true },
  { to: "/status-pages", label: t.nav.statusPages, icon: Globe },
  { to: "/reports", label: t.nav.reports, icon: FileText },
  { to: "/security", label: t.nav.security, icon: ShieldCheck },
];

const linkClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    "flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium",
    isActive ? "bg-accent/10 text-accent" : "text-muted-foreground hover:bg-muted hover:text-foreground",
  );

export function AppShell({ username, onLogout, children }: { username: string; onLogout: () => void; children: ReactNode }) {
  return (
    <div className="min-h-screen md:flex">
      {/* ≥768px: sidebar */}
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col gap-6 border-r border-border p-4 md:flex">
        <div className="flex items-center gap-2 px-2 font-semibold">
          <span aria-hidden="true" className="inline-block size-3 rounded-full bg-accent" />
          {t.appName}
        </div>
        <nav aria-label={t.nav.main} className="flex flex-col gap-1">
          {ITEMS.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} className={linkClass}>
              <Icon aria-hidden="true" className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col pb-16 md:pb-0">
        <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-2">
          <span className="font-semibold md:invisible">{t.appName}</span>
          <div className="flex items-center gap-1">
            <ThemeSwitch />
            <span className="max-w-32 truncate px-2 text-sm text-muted-foreground" title={username}>
              {username}
            </span>
            <Button variant="ghost" size="icon" aria-label={t.nav.logout} onClick={onLogout}>
              <LogOut aria-hidden="true" className="size-4" />
            </Button>
          </div>
        </header>
        <main className="mx-auto w-full max-w-5xl flex-1 p-4 md:p-8">{children}</main>
      </div>

      {/* <768px: bottom bar */}
      <nav aria-label={t.nav.bottom} className="fixed inset-x-0 bottom-0 z-10 flex border-t border-border bg-card md:hidden">
        {ITEMS.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end} className={({ isActive }) => cn("flex flex-1 flex-col items-center gap-0.5 py-2 text-[11px]", isActive ? "text-accent" : "text-muted-foreground")}>
            <Icon aria-hidden="true" className="size-5" />
            {label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
