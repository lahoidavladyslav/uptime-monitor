import { AlertCircle, Inbox } from "lucide-react";
import type { ReactNode } from "react";

import { t } from "@/lib/strings";

import { Button } from "./ui/button";
import { Skeleton } from "./ui/skeleton";

export function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div role="alert" className="flex flex-col items-center gap-3 rounded-lg border border-border p-10 text-center">
      <AlertCircle aria-hidden="true" className="size-8 text-status-down" />
      <p>{message}</p>
      <Button variant="outline" onClick={onRetry}>
        {t.common.retry}
      </Button>
    </div>
  );
}

export function EmptyState({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-border p-10 text-center">
      <Inbox aria-hidden="true" className="size-8 text-muted-foreground" />
      <p className="font-medium">{title}</p>
      {description && <p className="text-sm text-muted-foreground">{description}</p>}
      {action}
    </div>
  );
}

export function ListSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div role="status" aria-label={t.common.loading} aria-busy="true" className="flex flex-col gap-2">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} data-skeleton-row className="h-14 w-full" />
      ))}
    </div>
  );
}
