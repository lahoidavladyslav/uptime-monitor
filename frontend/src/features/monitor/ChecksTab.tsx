import { useState } from "react";

import { EmptyState, ErrorState, ListSkeleton } from "@/components/States";
import { Button } from "@/components/ui/button";
import { formatDateTime } from "@/lib/format";
import { t } from "@/lib/strings";
import { cn } from "@/lib/utils";

import { useChecks } from "./useMonitor";

export function ChecksTab({ id }: { id: number }) {
  const [page, setPage] = useState(1);
  const [onlyFailed, setOnlyFailed] = useState(false);
  const { data, isPending, isError, error, refetch, isPlaceholderData } = useChecks(id, page, onlyFailed);

  if (isPending) return <ListSkeleton rows={5} />;
  if (isError) return <ErrorState message={error.message} onRetry={() => void refetch()} />;

  return (
    <div className="flex flex-col gap-4">
      <label className="inline-flex w-fit cursor-pointer items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={onlyFailed}
          onChange={(e) => {
            setOnlyFailed(e.target.checked);
            setPage(1); // the filtered list is shorter: page 3 of it may not exist
          }}
        />
        {t.monitor.onlyFailed}
      </label>

      {data.items.length === 0 ? (
        <EmptyState
          title={onlyFailed ? t.monitor.checksEmptyFailed : t.monitor.checksEmpty}
          description={onlyFailed ? undefined : t.monitor.checksEmptyBody}
        />
      ) : (
        <div className={cn("overflow-x-auto rounded-lg border border-border", isPlaceholderData && "opacity-60")}>
          <table className="w-full text-left text-sm">
            <thead className="border-b border-border bg-muted text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">{t.monitor.checksTable.time}</th>
                <th className="px-3 py-2 font-medium">{t.monitor.checksTable.result}</th>
                <th className="px-3 py-2 font-medium">{t.monitor.checksTable.code}</th>
                <th className="px-3 py-2 font-medium">{t.monitor.checksTable.rt}</th>
                <th className="px-3 py-2 font-medium">{t.monitor.checksTable.error}</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((c) => (
                <tr key={c.id} className="border-b border-border last:border-0">
                  <td className="whitespace-nowrap px-3 py-2">{formatDateTime(c.checked_at)}</td>
                  <td className={cn("px-3 py-2 font-medium", c.is_up ? "text-status-up" : "text-status-down")}>
                    {c.is_up ? `✓ ${t.monitor.checksTable.up}` : `✕ ${t.monitor.checksTable.down}`}
                  </td>
                  <td className="px-3 py-2 tabular-nums">{c.status_code ?? "—"}</td>
                  <td className="px-3 py-2 tabular-nums">{c.response_time_ms === null ? "—" : t.monitor.ms(c.response_time_ms)}</td>
                  <td className="max-w-xs truncate px-3 py-2 text-muted-foreground" title={c.error ?? undefined}>
                    {c.error ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.pages > 1 && (
        <div className="flex items-center justify-between gap-3">
          <Button variant="outline" size="sm" disabled={data.page <= 1} onClick={() => setPage(data.page - 1)}>
            {t.monitor.prev}
          </Button>
          <span className="text-sm text-muted-foreground">{t.monitor.pageOf(data.page, data.pages)}</span>
          <Button variant="outline" size="sm" disabled={data.page >= data.pages} onClick={() => setPage(data.page + 1)}>
            {t.monitor.next}
          </Button>
        </div>
      )}
    </div>
  );
}
