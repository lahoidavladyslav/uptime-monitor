import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { components } from "@/api/schema";
import { EmptyState } from "@/components/States";
import { t } from "@/lib/strings";

type Point = components["schemas"]["SeriesPointOut"];

/** Response time line; failed checks are gaps in the line plus red dots on the baseline (shape + colour). */
export function ResponseChart({ points }: { points: Point[] }) {
  const timed = points.map((p) => p.ms).filter((v): v is number => v !== null);
  if (points.length === 0) return <EmptyState title={t.monitor.chartEmpty} />;
  const label = timed.length ? t.monitor.chartLabel(Math.min(...timed), Math.max(...timed)) : t.monitor.chartEmptyLabel;
  const data = points.map((p) => ({ ...p, fail: p.down ? 0 : null }));
  return (
    <figure className="m-0">
      <div role="img" aria-label={label} className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" vertical={false} />
            <XAxis dataKey="t" tick={{ fontSize: 11 }} minTickGap={32} stroke="var(--color-muted-foreground)" />
            <YAxis tick={{ fontSize: 11 }} width={44} unit=" мс" stroke="var(--color-muted-foreground)" />
            <Tooltip
              formatter={(value, name) => (name === "fail" ? [t.monitor.chartFail, ""] : [t.monitor.ms(Number(value)), t.monitor.chartTitle])}
            />
            <Line type="monotone" dataKey="ms" stroke="var(--color-accent)" strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />
            <Line dataKey="fail" stroke="none" dot={{ r: 4, fill: "var(--color-status-down)", stroke: "none" }} activeDot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <figcaption className="mt-1 text-xs text-muted-foreground">{t.monitor.chartUtc}</figcaption>
    </figure>
  );
}
