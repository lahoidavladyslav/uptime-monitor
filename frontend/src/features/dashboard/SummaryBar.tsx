import type { components } from "@/api/schema";
import { t } from "@/lib/strings";
import { cn } from "@/lib/utils";

type Summary = components["schemas"]["SummaryOut"];

function Stat({ id, label, value, tone }: { id: string; label: string; value: number; tone?: string }) {
  return (
    <div data-stat={id} className="rounded-lg border border-border bg-card px-4 py-3">
      <div className={cn("text-2xl font-semibold tabular-nums", tone)}>{value}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  );
}

export function SummaryBar({ summary }: { summary: Summary }) {
  const other = summary.unknown + summary.maintenance + summary.paused;
  return (
    <section aria-label={t.dashboard.summary} className="grid grid-cols-2 gap-3 md:grid-cols-4">
      <Stat id="up" label={t.dashboard.up} value={summary.up} tone="text-status-up" />
      <Stat id="down" label={t.dashboard.down} value={summary.down} tone={summary.down ? "text-status-down" : undefined} />
      <Stat id="incidents" label={t.dashboard.incidents} value={summary.open_incidents} tone={summary.open_incidents ? "text-status-down" : undefined} />
      <Stat id="other" label={t.dashboard.other} value={other} />
    </section>
  );
}
