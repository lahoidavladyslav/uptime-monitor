import { EmptyState, ErrorState, ListSkeleton } from "@/components/States";
import { formatDateTime, formatDuration } from "@/lib/format";
import { t } from "@/lib/strings";

import { useIncidents } from "./useMonitor";

export function IncidentsTab({ id }: { id: number }) {
  const { data, isPending, isError, error, refetch } = useIncidents(id);
  if (isPending) return <ListSkeleton rows={3} />;
  if (isError) return <ErrorState message={error.message} onRetry={() => void refetch()} />;
  if (data.length === 0) return <EmptyState title={t.monitor.incidentsEmpty} description={t.monitor.incidentsEmptyBody} />;

  return (
    <ul className="flex flex-col gap-2">
      {data.map((i) => (
        <li key={i.id} className="rounded-lg border border-border bg-card p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm">
              {t.monitor.started}: {formatDateTime(i.started_at)}
            </span>
            <span className={i.resolved_at ? "text-sm text-muted-foreground" : "text-sm font-medium text-status-down"}>
              {i.resolved_at ? formatDuration(i.minutes) : t.monitor.ongoing}
            </span>
          </div>
          {!i.resolved_at && (
            <div className="text-xs text-muted-foreground">
              {t.monitor.duration}: {formatDuration(i.minutes)}
            </div>
          )}
          {i.first_error && (
            <p className="mt-1 break-words text-sm text-muted-foreground" title={t.monitor.firstError}>
              {i.first_error}
            </p>
          )}
        </li>
      ))}
    </ul>
  );
}
