# NBA DFS Stats Lab — Project Guide

## Purpose

Ingest DraftKings DFS data into a local SQLite analytics DB (`data/analytics.db`) to support DFS modeling. The analytics DB is the output artifact of this project; the ops DB (`bigdataball`) is a read-only data dependency.

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
| `OPS_DB` | `G:\My Drive\Documents\bigdataball\bigdataball-backup\backup_nba_fantasy_logs.db` |
| `PROJECTIONS_DIR` | `G:\My Drive\Documents\CSV-Exports\projections` |
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
`slate_type` ∈ `{main, early, turbo, afternoon, night}` — always lowercase.

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
- Type must be in `{main, early, turbo, afternoon, night}`; unknown type is a validation error.
- `Main` is **explicit** in salary filenames; implicit (absent) in projections and lineups.
- **Lineups keep-latest:** when multiple files share `(date, type)`, select the one with the highest `_HHMMSS` suffix.

---

## Five tables

| Table | Grain | Source |
|---|---|---|
| `slate_players` | `(slate_id, dk_id)` | salary CSV |
| `projections` | `(slate_id, dk_id)` | projections CSV |
| `lineups` | `(slate_id, final_rank)` | lineups CSV (header rows) |
| `lineup_players` | `(slate_id, final_rank, slot)` | lineups CSV (melted slots) |
| `dk_crosswalk` | `dk_id` | built last, from ops DB match |

### Column mappings

**Salary → `slate_players`:** `DFS ID`→`dk_id` (int) · `Name`→`name` · `Position`→`positions` (raw) · `Team`→`team` · `Opponent`→`opp` · `Salary`→`salary` (int) · `Actual_FPTs`→`actual_fpts` (float, nullable).

**Projections → `projections`:** `ID`→`dk_id` (int) · `Minutes`→`minutes` · `FPPM`→`fppm` · `Projection`→`proj_pts` · `Own_Proj`→`proj_own`. Drop `Player`/`Team`/`Opponent`.

**Lineups → `lineups`:** `Final_Rank`→`final_rank` · `Lineup_Score`→`lineup_score` · `Total_Projection`→`total_projection` · `Total_Ownership`→`total_ownership` · `Geomean_Ownership`→`geomean_ownership` · `Proj_Rank`→`proj_rank` · `Own_Rank`→`own_rank` (REAL) · `Geo_Rank`→`geo_rank`.

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

---

## Out of scope (this phase)

- Showdown game style (its own files and tables — later phase).
- Actual ownership (not in any current file).
- Ops reconciliation (comparing `actual_fpts` against box scores — one-time pass after backfill, not now).
