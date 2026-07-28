# NBA DFS Stats Lab — Project Guide

> Full build order, phase gates, and acceptance checks live in `docs/ingestion-plan.md`. CLAUDE.md is the always-loaded condensed reference; the plan doc is the phase-by-phase detail. Read it when starting or resuming a phase.

## Purpose

Ingest DraftKings DFS data into a local SQLite analytics DB (`data/analytics.db`) to support DFS modeling. The analytics DB is the output artifact of this project; the ops DB (`bigdataball`) is a read-only data dependency.

---

## Status

_Update at every gate before `/clear`: done / next / decisions. Keep it short._

**Current phase:** Phase 4 — orchestrator + backfill (not started)
**Last gate cleared:** Phase 3 — `scripts/verify_phase3.py` all-PASS on the Windows machine, 2026-07-27

**Done**
- Phase 0: `config.py` populated; `SALARY_DIR`/`LINEUPS_DIR` confirmed; `data/` created.
- Phase 1 (code): `db/schema.py`, `db/connection.py`, `db/writers.py` — written in a cloud session; read-only ATTACH unit-tested against a temp DB, not the real ops DB.
- Phase 2 (code): `ingest/filenames.py`, `ingest/schemas.py` (contracts + generic validate/normalize + `ValidationReport`), `ingest/projections.py` (read/validate/normalize/ingest). 60 pytest tests, ruff clean.
- PR #2 merged (squash) after three review rounds; `scripts/verify_gates.py` runs both deferred gates locally.
- **Phase 1 + 2 gates cleared** (2026-07-26, `uv run python scripts/verify_gates.py`, all PASS):
  - all five tables created in `data/analytics.db`; ops DB attached with 6 tables visible; probe write rejected with `attempt to write a readonly database` (proves `mode=ro`).
  - `NBA-Projs-2026-05-18.csv` → `2026-05-18_classic_main`, 72 rows; re-ingest wrote 72 with total unchanged at 72 (idempotent). `minutes`/`fppm`/`proj_pts`/`proj_own` all populated.
- `data/analytics.db` now exists and holds exactly that one projections slate. It is gitignored and rebuildable — delete it and re-run the gate any time.
- Phase 3: `ingest/salary.py` + `ingest/lineups.py` in the same four-method shape; `SALARY_SCHEMA` / `LINEUPS_SCHEMA` added to `ingest/schemas.py`; manifest-driven lineups discovery (`load_lineups_manifest` / `latest_lineups_by_slate` / `find_lineups_file`); `parse_slate_id` + `salary_filename` inverses in `filenames.py`; `db/schema.py` gained `migrate()`. 109 pytest tests, ruff clean.
- **Phase 3 gate cleared** (2026-07-27, `uv run python scripts/verify_phase3.py`, all PASS on three slates):
  - `2026-05-18_classic_main` — 72 players, 50 lineups / 400 lineup_players; `2026-03-13_classic_night` — 107 players, 2484/19872; `2026-04-02_classic_main` — 216 players, 3000/24000.
  - All three: re-ingest idempotent on both tables, 8 players per lineup everywhere, **0 orphan rostered players**, every lineup header has its slot rows.
  - `--list` shows **43 slates** with both a salary CSV and a lineups file. All 7 keep-latest picks that `_HHMMSS` would get wrong match the README's corrected table.

**Side quest (done): lineups filename reconciliation**
- The optimizer named `ranked-lineups-*` files from run time, not slate date. `scripts/match_lineups_to_slates.py` matches each file to its true slate by DK ID set intersection (DK ID blocks are disjoint across slates — verified, 0 collisions across 409 slates). 219/226 matched at 100% coverage; 26 dates and 5 slate types corrected. Outputs + full write-up in `data/lineups_slate_match/README.md` (gitignored); corrected copies in `relabeled/`.
- **Phase 3 lineups loader must not use `_HHMMSS` for keep-latest.** Renaming preserves the suffix but the suffix is a *generation* time, so for 7 slates it now selects the wrong file. Use `manifest.csv` → `generated_at` / `is_latest_for_slate` instead.
- 7 files are unmatchable: no slate CSV exists for Feb 13–22, 2026. Parked in `unmatched/`.

**Next**
- Phase 4: `ingest/orchestrator.py` — `ingest_day()`, cross-source discovery, `--dry-run`, then the full backfill. Ship `scripts/verify_phase4.py` in the same PR. Discovery already exists for lineups (`latest_lineups_by_slate`) and for salary paths (`salary_filename`); projections discovery is the missing piece. Expect ~409 salary slates, 49 projections files, 43 slates with lineups — so most slates will load salary only, which is fine and should be reported, not treated as an error.

**Decisions / notes**
- **`late` added to `SLATE_TYPES`** (2026-07-27, confirmed with Jonny). Three real salary files use it — `Late-2026-01-04/-01-26/-02-07` — and two also have projections + lineups. The originally pinned five-type set was drawn from an incomplete sample.
- **`lineups.proj_rank` and `geo_rank` changed INTEGER → REAL; `SCHEMA_VERSION` 1 → 2.** All three rank columns are average-ranks that split ties; the pinned DDL only marked `own_rank` REAL because the sample slate happened to show ties only there. Across the 43 reconciled slates `Proj_Rank` is fractional 2266× and `Geo_Rank` 3002×. `db/schema.py:migrate()` handles v1→v2 by recreating the (necessarily empty) `lineups` table, and refuses rather than dropping data if it isn't empty.
- **Lineups ingest reads `data/lineups_slate_match/relabeled/`, never `LINEUPS_DIR`.** `LINEUPS_DIR` holds the original misnamed files. New config constants: `LINEUPS_MATCH_DIR`, `LINEUPS_RELABELED_DIR`, `LINEUPS_MANIFEST`. Keep-latest selects on the manifest's `generated_at`; a missing manifest raises with the `scripts/match_lineups_to_slates.py` rebuild command.
- `ingest_lineups` returns a `LineupsLoad(lineups, lineup_players)` named tuple and makes two `load_slate` calls — separate transactions, so a crash between them can leave `lineups` fresh and `lineup_players` stale. Re-running the slate repairs it; nothing duplicates within a table.
- Data facts confirmed by probing all 409 salary + 219 lineups files: one salary header variant, no duplicate `DFS ID` anywhere, `Team`/`Opponent` null on 36 rows (warning, nullable), `Actual_FPTs` populated on every row. One lineups file (`ranked-lineups-2026-03-12_161516.csv`) has a blank row plus a glued-on exposure report — validation rejects it cleanly, and it isn't the keep-latest for its slate anyway.
- Phase 1+2 shipped in one PR: Phase 1 code was never pushed from the earlier session, and Phase 2 depends on it.
- **Every ✋ gate needs a runnable check Jonny can execute** — a `scripts/verify_*.py` with PASS/FAIL output (see `scripts/verify_gates.py`) or exact paste-able commands in the gate report. Jonny reads code but doesn't write it; a gate described only in prose ("confirm X works") is not actionable. Phase 3+ sessions: ship the gate script in the same PR as the phase code.
- **`PROJECTIONS_DIR` moved** from `CSV-Exports\projections` to `NBA-DFS-25-26\NBA-25-26-Projs-CSVs` (2026-07-26). The old directory's 302 files were unusable: names carried a `_HH-MM-SS` suffix the pinned regex rejects, and the files only had `ID,Projection,Own_Proj` — no `Minutes`/`FPPM`, both required by `PROJECTIONS_SCHEMA`. The new directory's 49 files parse and validate 49/49 clean with zero warnings. Note `docs/ingestion-plan.md` still shows the old path in its Phase 0 snippet; config.py is authoritative.
- The new projections files were date-corrected by content in a separate session, the same defect the lineups side quest fixed. Verified independently here by DK ID intersection: **46 of 49 confirmed against their claimed slate, 0 mismatches**. The 3 unconfirmed (`2026-02-19`, `-02-20`, `-02-22`) are the ones with no salary CSV at all.
- `ingest_*` raises `SlateValidationError` (carrying the `ValidationReport`) on validation errors instead of returning the report — keeps the pinned `-> int` signature; the orchestrator will catch per-slate.
- Normalized ints use pandas nullable `Int64`; the writer converts `NA` → SQL NULL.
- `get_connection` opens with `uri=True` so `ATTACH 'file:…?mode=ro'` is parsed as a URI.
- Cloud Python: the session-start hook tries the pinned 3.14.2 first and falls back to the image's system Python 3.13 (exporting `UV_PYTHON=3.13`) only if the download fails. uv fetches managed CPython from `releases.astral.sh`, so cloud environments whose Custom network allowlist includes `*.astral.sh` run the pinned 3.14.2; environments without it run the 3.13 fallback. **Both are healthy states** — don't "fix" whichever one fired. `requires-python` stays `>=3.13` so the fallback resolves; the lockfile pins identical package versions on both interpreters.
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
