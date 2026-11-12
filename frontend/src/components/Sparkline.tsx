import { t } from "@/lib/strings";

const W = 100;
const H = 24;
const PAD = 2;

/** Inline SVG line; `null` values are gaps. No data at all renders a dashed placeholder. */
export function Sparkline({ values, className }: { values: (number | null)[]; className?: string }) {
  const numbers = values.filter((v): v is number => v !== null);
  if (numbers.length === 0) {
    return (
      <svg data-testid="sparkline-empty" role="img" aria-label={t.common.noData} viewBox={`0 0 ${W} ${H}`} className={className}>
        <line x1={PAD} x2={W - PAD} y1={H / 2} y2={H / 2} stroke="currentColor" strokeOpacity={0.4} strokeDasharray="3 3" />
      </svg>
    );
  }
  const min = Math.min(...numbers);
  const max = Math.max(...numbers);
  const span = max - min || 1; // flat series: avoid 0/0
  const n = values.length;
  const x = (i: number) => (n === 1 ? W / 2 : PAD + (i * (W - 2 * PAD)) / (n - 1));
  const y = (v: number) => (max === min ? H / 2 : H - PAD - ((v - min) / span) * (H - 2 * PAD));

  const parts: string[] = [];
  let run: string[] = [];
  const flush = () => {
    if (run.length === 1) run.push(run[0]!.replace("M", "L")); // a lone point becomes a dot (round caps)
    if (run.length) parts.push(run.join(" "));
    run = [];
  };
  values.forEach((v, i) => {
    if (v === null) return flush();
    run.push(`${run.length ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`);
  });
  flush();

  return (
    <svg role="img" aria-label={`${min}–${max} мс`} viewBox={`0 0 ${W} ${H}`} className={className} preserveAspectRatio="none">
      <path d={parts.join(" ")} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
