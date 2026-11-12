import { Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { EmptyState, ErrorState, ListSkeleton } from "@/components/States";
import { buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { t } from "@/lib/strings";
import { cn } from "@/lib/utils";

import { MonitorRow } from "./MonitorRow";
import { SummaryBar } from "./SummaryBar";
import { useDashboard } from "./useDashboard";

const FILTERS = ["up", "down", "maintenance", "paused", "unknown"] as const;
type Filter = "all" | (typeof FILTERS)[number];

export function DashboardPage() {
  const { data, isPending, isError, error, refetch } = useDashboard();
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<Filter>("all");

  const rows = useMemo(() => {
    const q = query.trim().toLocaleLowerCase("uk");
    return (data?.monitors ?? []).filter(
      (m) => (status === "all" || m.status === status) && (!q || m.name.toLocaleLowerCase("uk").includes(q)),
    );
  }, [data, query, status]);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">{t.dashboard.title}</h1>
        <Link to="/monitors/new" className={cn(buttonVariants())}>
          <Plus aria-hidden="true" className="size-4" />
          {t.dashboard.add}
        </Link>
      </div>

      {isPending && <ListSkeleton rows={5} />}
      {isError && <ErrorState message={error.message} onRetry={() => void refetch()} />}

      {data && (
        <>
          <SummaryBar summary={data.summary} />
          {data.monitors.length === 0 ? (
            <EmptyState
              title={t.dashboard.emptyTitle}
              description={t.dashboard.emptyBody}
              action={
                <Link to="/monitors/new" className={cn(buttonVariants(), "mt-2")}>
                  {t.dashboard.addFirst}
                </Link>
              }
            />
          ) : (
            <>
              <div className="flex flex-col gap-2 sm:flex-row">
                <div className="relative flex-1">
                  <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground" />
                  <Input aria-label={t.dashboard.search} className="pl-9" value={query} onChange={(e) => setQuery(e.target.value)} />
                </div>
                <select
                  aria-label={t.dashboard.statusFilter}
                  value={status}
                  onChange={(e) => setStatus(e.target.value as Filter)}
                  className="h-9 rounded-lg border border-border bg-card px-3 text-sm"
                >
                  <option value="all">{t.dashboard.allStatuses}</option>
                  {FILTERS.map((s) => (
                    <option key={s} value={s}>
                      {t.status[s]}
                    </option>
                  ))}
                </select>
              </div>
              {rows.length === 0 ? (
                <EmptyState title={t.dashboard.noMatchTitle} description={t.dashboard.noMatchBody} />
              ) : (
                <div className="flex flex-col gap-2">
                  {rows.map((m) => (
                    <MonitorRow key={m.id} row={m} />
                  ))}
                </div>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}
