import { Link } from "react-router-dom";

import type { components } from "@/api/schema";
import { Sparkline } from "@/components/Sparkline";
import { StatusBadge } from "@/components/StatusBadge";
import { formatRelative, formatUptime } from "@/lib/format";
import { t } from "@/lib/strings";

type Row = components["schemas"]["MonitorRowOut"];

export function MonitorRow({ row }: { row: Row }) {
  return (
    <Link
      to={`/monitors/${row.id}`}
      className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 rounded-lg border border-border bg-card p-3 hover:bg-muted md:grid-cols-[minmax(0,1fr)_8rem_auto]"
    >
      {/* min-w-0 + truncate: a 200-character or emoji/RTL name must not push the badge off the row */}
      <div className="flex min-w-0 flex-col gap-1.5">
        <span dir="auto" title={row.name} className="truncate font-medium">
          {row.name}
        </span>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={row.status} />
          {row.down_minutes !== null && <span className="text-xs font-medium text-status-down">{t.dashboard.downFor(row.down_minutes)}</span>}
        </div>
      </div>
      <Sparkline values={row.spark} className="hidden h-6 w-full text-accent md:block" />
      <div className="text-right">
        <div className="text-sm font-medium tabular-nums" title={t.dashboard.uptime24h}>
          {formatUptime(row.uptime_24h)}
        </div>
        <div className="text-xs text-muted-foreground" title={t.dashboard.lastCheck}>
          {formatRelative(row.last_checked_at)}
        </div>
      </div>
    </Link>
  );
}
