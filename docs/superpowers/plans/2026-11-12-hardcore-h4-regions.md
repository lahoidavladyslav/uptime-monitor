# Hardcore H4 — multi-region checks and quorum

Spec: `uptime-monitor-hardcore.md` §H4. Branch `hardcore/h4-regions` (stacked on H1).

**Decisions**
- `CheckResult.region`; workers run with `CHECK_REGION` and listen on `checks.<region>`; `schedule_checks` / "check now" fan out one task per region of `CHECK_REGIONS` (default `eu,us,asia`; tests use one region so every older test keeps its meaning).
- Claim is per (monitor, region): new `MonitorRegion(monitor, region, last_checked_at)` clock; the same conditional-UPDATE idea as before, so a duplicate for the *same* region is skipped while the other regions still run. `Monitor.last_checked_at` stays the scheduler/dashboard clock.
- Quorum is a pure function of "the latest result of each region" and is evaluated under the monitor row lock (`select_for_update`) after every write, so order of arrival cannot change the outcome and two regions finishing together cannot open two incidents (the partial unique constraint stays as the last line of defence).
  - votes: a region's latest result counts only if it is within 2 min of the result being processed;
  - participants: regions with a result newer than `max(5 min, 2×interval)`, plus regions that have not reported *yet* while the monitor is younger than that (so the very first round cannot be decided by one region); silent regions are excluded, never counted as up or down;
  - `need = participants // 2 + 1` (2 of 3; 2 of 2; 1 of 1); `down ≥ need` → down, `up ≥ need` → up, otherwise undecided (nothing changes). Opening and closing use the same quorum, which is what stops flapping.
- UI: monitor detail (Django + API `regions[]` + SPA overview card) shows each region: up / down / no data (silent).

**Tests pin:** 2-of-3 opens, 1-of-3 never; every permutation of arrival gives the same single incident; two threads recording different regions at once → one incident, one outbox row; silent region excluded and *not* counted as up; first round of a new monitor undecided on one vote; closing needs a quorum too; per-region claim; fan-out queues; compose has three region workers.
