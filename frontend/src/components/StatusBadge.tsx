import { AlertTriangle, CheckCircle2, HelpCircle, PauseCircle, Wrench, XCircle, type LucideIcon } from "lucide-react";

import { t } from "@/lib/strings";
import { cn } from "@/lib/utils";

export type Status = "up" | "down" | "unknown" | "maintenance" | "paused" | "degraded";

const CONFIG: Record<Status, { icon: LucideIcon; tone: string }> = {
  up: { icon: CheckCircle2, tone: "text-status-up bg-status-up/10" },
  down: { icon: XCircle, tone: "text-status-down bg-status-down/10" },
  degraded: { icon: AlertTriangle, tone: "text-status-degraded bg-status-degraded/10" },
  maintenance: { icon: Wrench, tone: "text-status-maintenance bg-status-maintenance/10" },
  paused: { icon: PauseCircle, tone: "text-status-unknown bg-status-unknown/10" },
  unknown: { icon: HelpCircle, tone: "text-status-unknown bg-status-unknown/10" },
};

/** Icon + text + colour: the colour alone never carries the meaning. */
export function StatusBadge({ status, className }: { status: Status; className?: string }) {
  const { icon: Icon, tone } = CONFIG[status];
  return (
    <span data-status={status} className={cn("inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium", tone, className)}>
      <Icon aria-hidden="true" className="size-3.5 shrink-0" />
      {t.status[status]}
    </span>
  );
}
