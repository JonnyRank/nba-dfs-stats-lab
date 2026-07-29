# NBA DFS Stats Lab — Project Guide

> Full build order, phase gates, and acceptance checks live in `docs/ingestion-plan.md`. CLAUDE.md is the always-loaded condensed reference; the plan doc is the phase-by-phase detail. Read it when starting or resuming a phase.

## Purpose

Ingest DraftKings DFS data into a local SQLite analytics DB (`data/analytics.db`) to support DFS modeling. The analytics DB is the output artifact of this project; the ops DB (`bigdataball`) is a read-only data dependency.

---

## Status

_Update at every gate before `/clear`: done / next / decisions. Keep it short._

**Current phase:** Phase 5 — crosswalk (not started)
**Last gate cleared:** Phase 4 — `uv run python scripts/verify_phase4.py --backfill`, all PASS 2026-07-28. **`data/analytics.db` is fully backfilled.**

### Backfill results (2026-07-28) — 412 slates, 0 failures

| Table | Rows | Slates |
|---|---|---|
| `slate_players` | 51,971 | 409 |
| `projections` | 8,856 | 49 |
| `lineups` | 71,109 | 43 |
| `lineup_players` | 568,872 | 43 |

- Rows written == rows in the DB on all four tables. Re-ingesting `2026-04-02_classic_main` changed no row count.
- Coverage: 363 salary-only · 43 all-three · 3 salary+projections · 3 projections-only. Types: 188 main, 142 night, 58 turbo, 17 early, 4 afternoon, 3 late.
- Whole-DB integrity all PASS: 8 players per lineup everywhere, **0 orphan rostered players**, header↔slot symmetry both ways, projections join to `slate_players` wherever salary exists.
- 5 validation warnings, **all off-slate games** (below). 0 unresolved filenames.
- **Off-slate rollup — 6 games, 209 rows:** `2025-10-26` LAC/POR (35) · `2025-10-28` GSW/LAC (34) · `2025-12-07_classic_early` BOS/TOR (34) + NYK/ORL (34) · `2026-01-25` DAL/MIL (36) · `2026-02-02` CHA/NOP (36). Handed to the ops-reconciliation pass.
  - 6 games but 5 warnings: the per-file check reports an all-zero *slate* once rather than per game, and the DB rollup decomposes `2025-12-07` into its two. 209 not 208 because Jonny's Sexton fix moved CHA from 17 players to 18.

**Done** — all four phases shipped; each PR carries its own review-round detail in git history.
- **Phases 0-2** (PR #2): `config.py`; `db/` (`schema.py` + `init_db`/`migrate`, `connection.py`, `writers.py:load_slate`); `ingest/` (`filenames.py`, `schemas.py` contracts + generic validate/normalize + `ValidationReport`, `projections.py`). Gate also proved the ops ATTACH is genuinely read-only — a probe write was rejected with `attempt to write a readonly database`. Nothing since re-verifies that, so it stands on the 2026-07-26 run.
- **Phase 3** (PR #7): `ingest/salary.py`, `ingest/lineups.py` (two tables from one file), manifest-driven lineups discovery, `db/schema.py:migrate()`.
- **Phase 4** (PR #9): `ingest/orchestrator.py` — `discover_slates()` unions all three sources into `slate_id → SlateSources`; `ingest_slate()` / `ingest_day()` run whichever sources a slate has, returning a `SlateResult` of per-source `Status`; `backfill()` loops them into a `BackfillSummary` (per-table totals, status counts, failures, warnings). CLI: `--list`, `--dry-run`, `--all`, `--slate`, `--date`, `--limit`, `--per-slate`, `-v`. Plus `check_zero_scored_games` in `salary.py` and `scripts/verify_phase4.py`. **207 pytest tests, ruff clean.**

**Side quest (done): lineups filename reconciliation**
- The optimizer named `ranked-lineups-*` files from run time, not slate date. `scripts/match_lineups_to_slates.py` matches each file to its true slate by DK ID set intersection (DK ID blocks are disjoint across slates — verified, 0 collisions across 409 slates). 219/226 matched at 100% coverage; 26 dates and 5 slate types corrected. Write-up in `data/lineups_slate_match/README.md` (gitignored); corrected copies in `relabeled/`.
- 7 files are unmatchable: no slate CSV exists for Feb 13–22, 2026. Parked in `unmatched/`.

**Next**
- **Phase 5** — `ingest/crosswalk.py`: name-match `slate_players` against ops `dim_players` via read-only ATTACH, confidence-scored; surface low-confidence matches for review and write only approved ones to `dk_crosswalk`. Then `unmatched_report()`. Ship `scripts/verify_phase5.py` in the same PR.
- **Then the ops-reconciliation pass** (needs the crosswalk): compare `actual_fpts` against box scores, starting with the 209 off-slate rows above. Check whether those games were rescheduled and played before nulling anything.
- Small follow-up: warnings are logged twice on the write path — once by `ingest_*` (keyed by filename) and once by `ingest_slate` (keyed by source), so `--backfill` prints each one as a pair. Cosmetic only; the counted tally is right.

**Local working notes** (gitignored via `docs/*.local.md`, so a fresh session sees them on disk but not in git):
- `docs/lineups-filename-discovery.local.md` — the plan for cutting lineups discovery over to filenames once the optimizer emits correctly-named files. Parked until that folder is ready.
- `docs/actual-fpts-zero-games.local.md` — the off-slate-game finding in full.

**Decisions / notes** — the *why*, where the code alone doesn't carry it.
- **Off-slate games are warned about, never dropped.** Some games carry `Actual_FPTs = 0` on **every** player of **both** sides. Confirmed with Jonny 2026-07-28: they tipped off outside the slate's window (an odd-hour start, e.g. 3:30pm Sunday when everything else began at 6), so the game was never in that contest. Null-based validation cannot see this — the cells hold `0`, not blank, and there is not one blank `Actual_FPTs` cell in all 409 files — so it lands in `actual_fpts` as a real `0.0`, indistinguishable from a DNP. The discriminator must be the whole game, not the value: single zeros are normal and common, so a value-keyed rule would destroy real data. Warning, not error; the rows load, and exclusion is the ops-reconciliation pass's call. Full reasoning in `ingest/salary.py:check_zero_scored_games`.
- **Partial coverage is a reported state, not a failure.** `Status.ABSENT` contributes nothing to the failure count — 363 of 412 slates are salary-only. Only a file that is *present and unusable* (`INVALID`/`ERROR`) fails, and one bad file never aborts a backfill.
- **Lineups require the slate to have players; projections don't.** `lineup_players.dk_id` must join to `slate_players` (0-orphans is a gate check), so lineups are `SKIPPED` when the slate has no players. Projections are deliberately not gated that way: `2026-02-19/-02-20/-02-22` have projections and no salary CSV at all, and refusing them would discard data rather than surface the gap.
- **Discovery is loud about what it can't resolve.** A missing source directory raises rather than globbing to nothing (on `G:\` an empty glob means the drive isn't mounted). Unparseable names and slate_id collisions land in `Discovery.skipped_files`; the gate FAILs if that list is non-empty. Currently empty on the real data.
- **`--all` is required to write every slate.** A restriction (`--slate`/`--date`/`--limit`) implies intent, but `--limit` counts only when it *actually restricts* — `--limit 500` against 412 slates selects all of them.
- **`late` is a real slate type** (2026-07-27). Three salary files use it; **no projections and no lineups exist for any of them** (confirmed 2026-07-28), so `late` slates load salary only. The typeless `NBA-Projs-2026-01-26/-02-07.csv` belong to the `Main-` slates of those dates, which also exist — no wrong-slate write is possible.
- **`lineups.proj_rank`/`own_rank`/`geo_rank` are all REAL; `SCHEMA_VERSION` 2.** They're average-ranks that split ties (`Proj_Rank` fractional 2266×, `Geo_Rank` 3002×); the pinned DDL marked only `own_rank` REAL because the sample slate happened to tie only there. `db/schema.py:migrate()` detects drift from **live column types**, not `user_version` — `init_db`'s DDL is all `IF NOT EXISTS`, so it can stamp a version onto a schema it never changed. It refuses rather than dropping data if either lineups table holds rows.
- **Lineups ingest reads `data/lineups_slate_match/relabeled/`, never `LINEUPS_DIR`** — the latter holds the original misnamed files. Keep-latest selects on the manifest's `generated_at` **parsed to a `datetime`**, never the `_HHMMSS` filename suffix (a *generation* time, wrong for 7 slates) and never a raw string compare (`fromisoformat` accepts separators and offsets that don't sort lexically). A missing or drifted manifest raises with the rebuild command. See `docs/lineups-filename-discovery.local.md` for the eventual cutover.
- `ingest_lineups` makes two `load_slate` calls in **separate transactions**, so a crash between them can leave `lineups` fresh and `lineup_players` stale. Re-running the slate repairs it; nothing duplicates within a table.
- **`PROJECTIONS_DIR` moved** to `NBA-DFS-25-26\NBA-25-26-Projs-CSVs` (2026-07-26); the old `CSV-Exports\projections` had 302 files lacking `Minutes`/`FPPM` and carrying a suffix the regex rejects. `config.py` is authoritative — `docs/ingestion-plan.md` still shows the old path in its Phase 0 snippet. Those files were date-corrected by content in a separate session; verified here by DK ID intersection, **46 of 49 confirmed, 0 mismatches** (the 3 unconfirmed are the ones with no salary CSV).
- Source-data facts from probing all 409 salary + 219 lineups files: one salary header variant, no duplicate `DFS ID` anywhere, no blank `Actual_FPTs` anywhere. One lineups file (`ranked-lineups-2026-03-12_161516.csv`) has a blank row plus a glued-on exposure report — validation rejects it cleanly, and it isn't the keep-latest for its slate anyway.
- **Every ✋ gate needs a runnable check Jonny can execute** — a `scripts/verify_*.py` with PASS/FAIL output and a nonzero exit, shipped in the same PR as the phase code. Jonny reads code but doesn't write it; a gate described only in prose is not actionable.
- Conventions: `ingest_*` raises `SlateValidationError` (carrying the report) rather than returning it, keeping the pinned `-> int` signature; normalized ints use pandas nullable `Int64` and the writer converts `NA` → SQL NULL; `get_connection` opens with `uri=True` so `ATTACH 'file:…?mode=ro'` parses as a URI.
- **Cloud environment:** the session-start hook tries the pinned Python 3.14.2 and falls back to the image's 3.13 (exporting `UV_PYTHON=3.13`) if uv can't reach `releases.astral.sh`. **Both are healthy** — don't "fix" whichever fired. Never `uv self update`: cloud GitHub access is repo-scoped and the proxy's 403 gets misreported as a rate limit, so the hook updates uv from PyPI. `.claude/hooks/*.sh` must be mode 100755 in git — a 100644 file exits 126 as a *non-blocking* SessionStart error, so the session runs on with no context injected and nothing obvious in the transcript (`pr-review-posture.sh` shipped that way and never fired). `tests/test_hooks.py` pins it.
- Cloud Python: the session-start hook tries the pinned 3.14.2 first and falls back to the image's system Python 3.13 (exporting `UV_PYTHON=3.13`) only if the download fails. uv fetches managed CPython from `releases.astral.sh`, so cloud environments whose Custom network allowlist includes `*.astral.sh` run the pinned 3.14.2; environments without it run the 3.13 fallback. **Both are healthy states** — don't "fix" whichever one fired. `requires-python` stays `>=3.13` so the fallback resolves; the lockfile pins identical package versions on both interpreters.
- **`.claude/hooks/*.sh` must be mode 100755 in git.** Hooks are invoked as bare paths, so a 100644 file exits 126 "Permission denied" — a *non-blocking* SessionStart error, meaning the session runs on with no context injected and nothing obvious in the transcript. `pr-review-posture.sh` shipped that way and never fired. Fix with `git update-index --chmod=+x <path>`; `tests/test_hooks.py` pins it.
- Cloud GitHub access is repo-scoped: a proxy 403s every GitHub path outside the session's bound repos, at every network access level. So never `uv self update` (it hits the GitHub API and misreports the 403 as a rate limit) — the hook updates uv from PyPI instead.

---

## Ops DB rule (hard constraint)

`bigdataball` is a **pre-existing, separate SQLite DB** of historical box scores. It is a **data-only dependency**:

- Never import its code or modules.
- Never modify it, never copy its tables into `analytics.db`.
- Touch it **only via SQLite `ATTACH … mode=ro`** at query time.
- The crosswalk phase reads `dim_players.PLAYER_ID` from it via ATTACH — that is the only sanctioned use.

---

## Key paths (`src/nba_dfs_stats_lab/config.py`)

| Constant | Location |
|---|---|
| `ANALYTICS_DB` | `data/analytics.db` (repo-local, rebuildable — never on G:\) |
| `OPS_DB` | `G:\My Drive\Documents\bigdataball\ops_snapshot_nba_fantasy_logs.db` |
| `PROJECTIONS_DIR` | `G:\My Drive\Documents\NBA-DFS-25-26\NBA-25-26-Projs-CSVs` |
| `SALARY_DIR` | `G:\My Drive\Documents\NBA-DFS-25-26\NBA-25-26-Classic-Slates` |
| `LINEUPS_DIR` | `G:\My Drive\Documents\NBA-DFS-25-26\NBA-25-26-Classic-Ranked-Lineups` |

`data/` and `*.db` are gitignored — `analytics.db` is never committed.

---

## Slate key

```
slate_id = f"{date}_classic_{slate_type}"
# e.g. "2026-02-28_classic_main"
```

`game_style` is always `classic` this phase (Showdown is out of scope).
`slate_type` ∈ `{main, early, turbo, afternoon, night, late}` — always lowercase. (`late` added Phase 3; see Decisions.)

---

## Filename conventions

```
salary:      <Type>-<YYYY-MM-DD>.csv
             ^(?P<type>[A-Za-z]+)-(?P<date>\d{4}-\d{2}-\d{2})\.csv$

projections: NBA-Projs-[<Type>-]<YYYY-MM-DD>.csv
             ^NBA-Projs-(?:(?P<type>[A-Za-z]+)-)?(?P<date>\d{4}-\d{2}-\d{2})\.csv$

lineups:     ranked-lineups-[<Type>-]<YYYY-MM-DD>[_<HHMMSS>].csv
             ^ranked-lineups-(?:(?P<type>[A-Za-z]+)-)?(?P<date>\d{4}-\d{2}-\d{2})(?:_(?P<ts>\d{6}))?\.csv$
```

- Type group absent → `main` (default).
- Type must be in `{main, early, turbo, afternoon, night, late}`; unknown type is a validation error.
- `Main` is **explicit** in salary filenames; implicit (absent) in projections and lineups. `salary_filename(date, type)` is the inverse; `parse_slate_id(slate_id)` inverts `build_slate_id`.
- **Lineups keep-latest: do NOT use `_HHMMSS`.** Select on `manifest.csv` → `generated_at` via `latest_lineups_by_slate()`. The suffix is a *generation* time, and after the relabeling side quest it points at the wrong file for 7 slates.

---

## Five tables

| Table | Grain | Source |
|---|---|---|
| `slate_players` | `(slate_id, dk_id)` | salary CSV |
| `projections` | `(slate_id, dk_id)` | projections CSV |
| `lineups` | `(slate_id, final_rank)` | lineups CSV (header rows) — `relabeled/`, keep-latest by manifest |
| `lineup_players` | `(slate_id, final_rank, slot)` | lineups CSV (melted slots) |
| `dk_crosswalk` | `dk_id` | built last, from ops DB match |

### Column mappings

**Salary → `slate_players`:** `DFS ID`→`dk_id` (int) · `Name`→`name` · `Position`→`positions` (raw) · `Team`→`team` · `Opponent`→`opp` · `Salary`→`salary` (int) · `Actual_FPTs`→`actual_fpts` (float, nullable).

**Projections → `projections`:** `ID`→`dk_id` (int) · `Minutes`→`minutes` · `FPPM`→`fppm` · `Projection`→`proj_pts` · `Own_Proj`→`proj_own`. Drop `Player`/`Team`/`Opponent`.

**Lineups → `lineups`:** `Final_Rank`→`final_rank` · `Lineup_Score`→`lineup_score` · `Total_Projection`→`total_projection` · `Total_Ownership`→`total_ownership` · `Geomean_Ownership`→`geomean_ownership` · `Proj_Rank`→`proj_rank` · `Own_Rank`→`own_rank` · `Geo_Rank`→`geo_rank`. **All three ranks are REAL** — they're average-ranks and split ties.

**Lineups → `lineup_players`:** melt slots `PG SG SF PF C G F UTIL`; extract `dk_id` from `"Player Name (12345678)"` via `r"\((\d+)\)"`.

---

## Module layout

```
src/nba_dfs_stats_lab/
  config.py
  db/
    connection.py   # get_connection(), attach_ops()
    schema.py       # DDL, init_db(), SCHEMA_VERSION
    writers.py      # load_slate() — idempotent delete-then-insert
  ingest/
    filenames.py    # regex parsers, build_slate_id
    schemas.py      # declarative column contracts
    projections.py  # read/validate/normalize/ingest
    salary.py       # read/validate/normalize/ingest
    lineups.py      # read/validate/normalize/ingest (two tables)
    crosswalk.py    # name-match against ops, confidence scores
    orchestrator.py # ingest_day(), discovery, --dry-run, backfill
```

---

## Working rules

- **Idempotent writes only.** `load_slate` does `DELETE WHERE slate_id = ?` then insert, in one transaction. Re-running never duplicates.
- **Surface, don't drop.** `ValidationReport` captures errors/warnings. Bad data is reported; nothing writes if validation fails.
- **Single writer**, no concurrent ingest.
- **Ops DB read-only and query-time only.** `ATTACH … mode=ro`; correctly URL-encode Windows paths (spaces → `%20`, backslashes → `/`).
- **No new dependencies** without asking. Everything needed is already installed.
- **Keep ruff clean.**
- **Tests:** pytest unit tests for filename parsing (all three patterns, default-to-main, invalid type), `slate_id` construction, and lineup `dk_id` extraction (apostrophe in name, malformed cell).
- **Maintain `## Status`.** At each gate, before committing and `/clear`, update the Status section (done / next / decisions). This is what lets a fresh session resume without re-pasting the plan.
- **Gates ship with runnable verification.** Any gate Jonny must run locally gets a `scripts/verify_*.py` (PASS/FAIL per check, nonzero exit on failure — see `scripts/verify_gates.py`) or exact paste-able commands, delivered in the same PR as the phase code. Exercise the script against synthetic stand-ins in-session before shipping; the real run needs Jonny's machine. Never leave a gate as prose instructions only.

---

## Out of scope (this phase)

- Showdown game style (its own files and tables — later phase).
- Actual ownership (not in any current file).
- Ops reconciliation (comparing `actual_fpts` against box scores — one-time pass after backfill, not now).
