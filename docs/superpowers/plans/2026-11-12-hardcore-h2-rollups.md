# Hardcore H2 — hourly rollups as the source for long windows

Spec: `uptime-monitor-hardcore.md` §H2. Branch `hardcore/h2-rollups`.

**Decisions**
- `HourlyStat` becomes `CheckRollupHourly` (`checks_total, checks_up, sum_response_ms, response_count, min_response_ms, max_response_ms`). `response_count` is NOT `checks_total`: failed checks have no timing, so averages are `sum/response_count`, never average-of-averages.
- `build_rollups` (every 5 min) recomputes the previous + current hour with ONE `INSERT … SELECT … ON CONFLICT DO UPDATE`; hours are cut in UTC explicitly (`AT TIME ZONE 'UTC'`), independent of `TIME_ZONE`.
- Single read path `monitors/rollups.py`: rollup rows for hours `< T`, raw rows for `[T, now)` with `T = floor_hour(now) − 1h`. Why not "raw only for the current hour": hour H−1 is final only after the first run after the boundary; reading it raw for up to 2 h removes the window where it could be incomplete. Window for N days = `[floor_hour(now) − N·24h, now)` on both paths (so they are comparable and tested equal).
- Used by: 7d stats + chart, public page (7d uptime + day bars), reports, 30/90d uptime. 24h views stay raw (≤1440 rows/monitor, indexed).
- Raw retention 7 days (hour-aligned cutoff, so no half-hour is ever left behind to be mis-rebuilt), rollups 365 days.
- `backfill_rollups --from --to` rebuilds (overwrites) day by day; only hours that still have raw rows are touched.

**Tests (each pins a pitfall):** late row lands in its own hour; avg is weighted; rollup == raw for stats/uptime/bars/chart/report at several "now" instants incl. just after midnight; reading survives deleting old raw; non-UTC `TIME_ZONE` (+5:30) changes nothing; retention; backfill overwrites corrupt rows but leaves hours without raw alone.
