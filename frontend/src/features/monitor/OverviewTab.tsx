import { lazy, Suspense, useState } from "react";

import type { components } from "@/api/schema";
import { ErrorState } from "@/components/States";
import { Skeleton } from "@/components/ui/skeleton";
import { formatUptime } from "@/lib/format";
import { t } from "@/lib/strings";
import { cn } from "@/lib/utils";

import { MaintenanceSection } from "./MaintenanceSection";
import { useSeries, useStats, type Period } from "./useMonitor";

// Recharts is ~half of the bundle: only this page needs it, so load it on demand.
const ResponseChart = lazy(() => import("./ResponseChart").then((m) => ({ default: m.ResponseChart })));

type Monitor = components["schemas"]["MonitorDetailOut"];

function Card({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div data-testid={testId} className="rounded-lg border border-border bg-card p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-xl font-semibold tabular-nums">{value}</div>
    </div>
  );
}

export function OverviewTab({ monitor }: { monitor: Monitor }) {
  const [period, setPeriod] = useState<Period>("24h");
  const stats = useStats(monitor.id, period);
  const series = useSeries(monitor.id, period);
  const dash = "—";

  return (
    <div className="flex flex-col gap-6">
      <div data-testid="uptime-cards" className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Card label={t.monitor.uptimeCards.h24} value={formatUptime(monitor.uptime.h24)} />
        <Card label={t.monitor.uptimeCards.d7} value={formatUptime(monitor.uptime.d7)} />
        <Card label={t.monitor.uptimeCards.d30} value={formatUptime(monitor.uptime.d30)} />
        <Card label={t.monitor.uptimeCards.d90} value={formatUptime(monitor.uptime.d90)} />
      </div>

      <section aria-labelledby="chart-title" className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-3">
          <h2 id="chart-title" className="text-lg font-semibold">
            {t.monitor.chartTitle}
          </h2>
          <div role="group" aria-label={t.monitor.period.label} className="inline-flex rounded-lg border border-border p-0.5">
            {(["24h", "7d"] as const).map((p) => (
              <button
                key={p}
                type="button"
                aria-pressed={period === p}
                onClick={() => setPeriod(p)}
                className={cn("cursor-pointer rounded-md px-3 py-1 text-sm", period === p ? "bg-muted font-medium" : "text-muted-foreground")}
              >
                {t.monitor.period[p]}
              </button>
            ))}
          </div>
        </div>
        {series.isPending ? <Skeleton className="h-64 w-full" /> : series.isError ? <ErrorState message={series.error.message} onRetry={() => void series.refetch()} /> : (
          <Suspense fallback={<Skeleton className="h-64 w-full" />}>
            <ResponseChart points={series.data} />
          </Suspense>
        )}
      </section>

      {stats.data && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          <Card label={t.monitor.stats.checks} value={String(stats.data.total)} />
          <Card label={t.monitor.stats.failures} value={String(stats.data.down)} />
          <Card label={t.monitor.stats.avg} value={stats.data.rt_avg === null ? dash : t.monitor.ms(stats.data.rt_avg)} />
          <Card label={t.monitor.stats.min} value={stats.data.rt_min === null ? dash : t.monitor.ms(stats.data.rt_min)} />
          <Card label={t.monitor.stats.max} value={stats.data.rt_max === null ? dash : t.monitor.ms(stats.data.rt_max)} />
        </div>
      )}

      {monitor.regions.length > 1 && (
        <section aria-labelledby="regions-title" className="rounded-lg border border-border bg-card p-4">
          <h2 id="regions-title" className="text-sm font-medium text-muted-foreground">
            {t.monitor.regions.title}
          </h2>
          <ul className="mt-2 flex flex-wrap gap-2">
            {monitor.regions.map((r) => (
              <li key={r.region} data-region={r.region} data-status={r.status} className="flex items-center gap-2 rounded-full border border-border px-3 py-1 text-sm">
                <span className="font-medium uppercase">{r.region}</span>
                <span className={cn(r.status === "up" && "text-status-up", r.status === "down" && "text-status-down", (r.status === "stale" || r.status === "unknown") && "text-muted-foreground")}>
                  {r.status === "up" ? "✓ " : r.status === "down" ? "✕ " : "– "}
                  {t.monitor.regions[r.status]}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-muted-foreground">
            {t.monitor.regions.quorum}
            {monitor.regions.some((r) => r.status === "stale") && ` ${t.monitor.regions.staleHint}`}
          </p>
        </section>
      )}

      <section aria-labelledby="ssl-title" className="rounded-lg border border-border bg-card p-4">
        <h2 id="ssl-title" className="text-sm font-medium text-muted-foreground">
          {t.monitor.ssl}
        </h2>
        <p className="mt-1">
          {monitor.ssl_error ? (
            <span className="text-status-down">{monitor.ssl_error}</span>
          ) : monitor.ssl_days_left === null ? (
            <span className="text-muted-foreground">{t.monitor.sslNone}</span>
          ) : (
            <span className={cn(monitor.ssl_days_left <= 14 && "font-medium text-status-degraded")}>{t.monitor.sslDays(monitor.ssl_days_left)}</span>
          )}
        </p>
      </section>

      <MaintenanceSection monitorId={monitor.id} />
    </div>
  );
}
