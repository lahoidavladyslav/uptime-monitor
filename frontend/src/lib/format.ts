import { t } from "@/lib/strings";

const DASH = "—";

/** "5 хв тому"; "—" for null/garbage; clock skew (future timestamps) reads as "щойно". */
export function formatRelative(iso: string | null, now: Date = new Date()): string {
  if (!iso) return DASH;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return DASH;
  const seconds = Math.floor((now.getTime() - then) / 1000);
  if (seconds < 10) return t.time.now;
  if (seconds < 60) return t.time.seconds(seconds);
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return t.time.minutes(minutes);
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t.time.hours(hours);
  return t.time.days(Math.floor(hours / 24));
}

/** "99.9%"; "—" when there is no data. Truncates (not rounds) so 99.99 is never shown as a perfect 100%. */
export function formatUptime(value: number | null): string {
  if (value === null || Number.isNaN(value)) return DASH;
  const truncated = Math.floor(value * 10) / 10;
  return `${Number.isInteger(truncated) ? truncated : truncated.toFixed(1)}%`;
}

/** "1 год 30 хв", "42 хв", "2 дн 3 год" for a number of minutes. */
export function formatDuration(minutes: number): string {
  return t.time.duration(Math.max(0, Math.floor(minutes)));
}

/** Local, locale-aware date and time for tables ("12.11.2026, 13:20"). "—" for garbage. */
export function formatDateTime(iso: string | null): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? DASH : d.toLocaleString("uk-UA", { dateStyle: "short", timeStyle: "short" });
}
