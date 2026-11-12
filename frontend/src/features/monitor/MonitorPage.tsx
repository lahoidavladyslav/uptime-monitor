import { ExternalLink, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { ApiError } from "@/api/errors";
import { NotFoundPage } from "@/components/NotFoundPage";
import { EmptyState, ErrorState, ListSkeleton } from "@/components/States";
import { StatusBadge } from "@/components/StatusBadge";
import { Button, buttonVariants } from "@/components/ui/button";
import { formatRelative } from "@/lib/format";
import { t } from "@/lib/strings";
import { cn } from "@/lib/utils";

import { ChecksTab } from "./ChecksTab";
import { IncidentsTab } from "./IncidentsTab";
import { OverviewTab } from "./OverviewTab";
import { useCheckNow, useMonitor } from "./useMonitor";

const CLICK_REST_MS = 3000;
const TABS = ["overview", "checks", "incidents"] as const;
type Tab = (typeof TABS)[number];

export function MonitorPage() {
  const { id: raw } = useParams();
  // "abc", "7x" or "0": not a monitor id, so no request is made at all
  const id = raw && /^[1-9]\d*$/.test(raw) ? Number(raw) : null;
  if (id === null) return <NotFoundPage />;
  return <MonitorView id={id} />;
}

function MonitorView({ id }: { id: number }) {
  const { data, isPending, isError, error, refetch } = useMonitor(id);
  const checkNow = useCheckNow(id);
  // After a click the button rests for a moment: the worker needs time, and the server would answer a
  // second click inside its cooldown with 429 anyway.
  const [resting, setResting] = useState(false);
  useEffect(() => {
    if (!resting) return;
    const timer = setTimeout(() => setResting(false), CLICK_REST_MS);
    return () => clearTimeout(timer);
  }, [resting]);
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab");
  const tab: Tab = TABS.find((x) => x === requested) ?? "overview";

  if (isPending) return <ListSkeleton rows={4} />;
  if (isError) {
    if (error instanceof ApiError && error.status === 404) {
      return (
        <EmptyState
          title={t.monitor.notFoundTitle}
          description={t.monitor.notFoundBody}
          action={
            <Link to="/" className={cn(buttonVariants({ variant: "outline" }), "mt-2")}>
              {t.monitor.backToList}
            </Link>
          }
        />
      );
    }
    return <ErrorState message={error.message} onRetry={() => void refetch()} />;
  }

  const feedback = checkNow.isError ? (
    <p role="alert" className="text-sm text-status-down">
      {checkNow.error.message}
    </p>
  ) : checkNow.isSuccess ? (
    <p role="status" className="text-sm text-status-up">
      {t.monitor.checkQueued}
    </p>
  ) : null;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 flex-col gap-2">
          <h1 dir="auto" className="truncate text-2xl font-semibold" title={data.name}>
            {data.name}
          </h1>
          <a
            href={data.url}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex min-w-0 items-center gap-1 text-sm text-muted-foreground hover:underline"
          >
            <span className="truncate">{data.url}</span>
            <ExternalLink aria-hidden="true" className="size-3.5 shrink-0" />
          </a>
          <div className="flex flex-wrap items-center gap-3">
            <StatusBadge status={data.status} />
            {data.down_minutes !== null && <span className="text-sm font-medium text-status-down">{t.dashboard.downFor(data.down_minutes)}</span>}
            <span className="text-sm text-muted-foreground">
              {t.monitor.lastCheck}: {formatRelative(data.last_checked_at)}
            </span>
          </div>
        </div>
        <div className="flex flex-col items-start gap-1 sm:items-end">
          <Link to={`/monitors/${id}/edit`} className={cn(buttonVariants({ variant: "outline" }))}>
            {t.monitor.edit}
          </Link>
          <Button
            onClick={() => {
              setResting(true);
              checkNow.mutate();
            }}
            disabled={checkNow.isPending || resting}
          >
            <RefreshCw aria-hidden="true" className={cn("size-4", checkNow.isPending && "animate-spin")} />
            {t.monitor.checkNow}
          </Button>
          {feedback}
        </div>
      </header>

      <div role="tablist" aria-label={t.monitor.tabs.label} className="flex gap-1 border-b border-border">
        {TABS.map((name) => (
          <button
            key={name}
            role="tab"
            type="button"
            aria-selected={tab === name}
            onClick={() => setParams(name === "overview" ? {} : { tab: name }, { replace: true })}
            className={cn(
              "-mb-px cursor-pointer border-b-2 px-3 py-2 text-sm font-medium",
              tab === name ? "border-accent text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t.monitor.tabs[name]}
          </button>
        ))}
      </div>

      {tab === "overview" && <OverviewTab monitor={data} />}
      {tab === "checks" && <ChecksTab id={id} />}
      {tab === "incidents" && <IncidentsTab id={id} />}
    </div>
  );
}
