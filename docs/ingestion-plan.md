# Kickoff — `nba-dfs-stats-lab` · Ingestion phase

You're implementing the ingestion phase of an NBA DFS analytics project. This document is the **ground-truth spec** — the column specs, schema, and filename rules are pinned to real CSV files and are not open for redesign. Build to them. Where something genuinely needs a decision, raise it; don't silently improvise.

---

## Who you're working with

Jonny is tech-savvy and reads code fluently, but does not write it himself. So:
- **Write complete, real files** — this is the implementation session, not planning. But keep functions small and readable, and add a sentence of explanation for each non-obvious choice, because Jonny reviews everything you write.
- When there's a clear right answer, **state it and why** in one or two lines — don't present a menu of options for him to adjudicate.
- Don't over-engineer. Match the solution to the data scale (a few hundred slates, ~100 players each).
- **Work in small, runnable increments and stop at the check-in gates** below. Don't barrel through all phases in one shot.

---

## Project context

- Goal: ingest DraftKings DFS data into a new SQLite `analytics.db` to support DFS modeling.
- There is a **separate, pre-existing ops SQLite DB** of historical box scores (`bigdataball`). It is a **data-only dependency**: never import its code, never modify it, never copy its tables in. Touch it only via SQLite `ATTACH` in **read-only** mode, at query time.
- This phase ingests **three CSV sources** (salary+actuals, projections, ranked lineups) into five tables. Showdown game style and actual-ownership data are explicitly **out of scope** for this phase.

---

## Environment (verify before assuming)

Start by reading the repo: `pyproject.toml`, `uv.lock`, `.gitignore`, `src/nba_dfs_stats_lab/`. Confirm these, then proceed:

- **Package manager:** `uv`. Run things with `uv run …`. Don't add dependencies — the ingestion phase needs nothing beyond what's installed. If you think you need a new dep, stop and ask.
- **Python:** 3.14.2.
- **Installed:** runtime `pandas`, `scipy`, `statsmodels`, `scikit-learn`; dev `ruff`, `pytest`, `pytest-cov`, `ipykernel`.
- **Layout:** package is `src/nba_dfs_stats_lab/` (uv_build src layout). `config.py` exists but is empty; `__init__.py` exists.
- **OS:** Windows. Data paths are on the `G:\` Google Drive mirror with spaces and backslashes — handle carefully (see config).
- **Lint:** keep `ruff` clean.

Create the module tree under `src/nba_dfs_stats_lab/`:

```
db/        connection.py   schema.py   writers.py
ingest/    schemas.py   filenames.py   projections.py   salary.py   lineups.py   crosswalk.py   orchestrator.py
```

---

## Config

Populate `config.py` with paths. `ANALYTICS_DB` is **repo-local** (`data/analytics.db`) — never on `G:\` (Drive sync can corrupt a live SQLite file; the DB is a rebuildable artifact). `.gitignore` already covers `data/` and `*.db`.

```python
from pathlib import Path

REPO_ROOT       = Path(__file__).resolve().parents[2]   # …/nba_dfs_stats_lab/config.py → repo root
DATA_DIR        = REPO_ROOT / "data"
ANALYTICS_DB    = DATA_DIR / "analytics.db"

OPS_DB          = Path(r"G:\My Drive\Documents\bigdataball\ops_snapshot_nba_fantasy_logs.db")
PROJECTIONS_DIR = Path(r"G:\My Drive\Documents\CSV-Exports\projections")
SALARY_DIR      = Path(r"<CONFIRM WITH JONNY>")          # salary CSVs
LINEUPS_DIR     = Path(r"<CONFIRM WITH JONNY>")          # ranked-lineups CSVs
```

**In Phase 0, ask Jonny to confirm `SALARY_DIR` and `LINEUPS_DIR`** — only `PROJECTIONS_DIR` and `OPS_DB` are known.

> **Superseded — this snippet is the original Phase 0 proposal, kept for history.**
> `src/nba_dfs_stats_lab/config.py` is authoritative. `PROJECTIONS_DIR` moved to
> `G:\My Drive\Documents\NBA-DFS-25-26\NBA-25-26-Projs-CSVs` on 2026-07-26 —
> see the `PROJECTIONS_DIR` note in CLAUDE.md's Status for why. `SALARY_DIR` and
> `LINEUPS_DIR` were confirmed in Phase 0; see the key-paths table in CLAUDE.md.

---

## Ground-truth spec

### Slate key

```
slate_id = f"{date}_classic_{slate_type}"      # e.g. 2026-02-28_classic_main
```
`game_style` is constant `classic` this phase (Showdown later, its own files + tables). `slate_type` ∈ `{main, early, turbo, afternoon, night, late}`, lowercased.

> **Amended Phase 3 (2026-07-27):** `late` was added to the set above — the originally pinned five omitted it, but `Late-2026-01-04/-01-26/-02-07.csv` are real salary files. `ingest/filenames.py` is authoritative.

### Filename → (date, slate_type)

Three conventions. `Main` is **explicit** in salary filenames but **implicit** (absent ⇒ main) in projections and lineups.

```
salary:       <Type>-<YYYY-MM-DD>.csv
              ^(?P<type>[A-Za-z]+)-(?P<date>\d{4}-\d{2}-\d{2})\.csv$

projections:  NBA-Projs-[<Type>-]<YYYY-MM-DD>.csv
              ^NBA-Projs-(?:(?P<type>[A-Za-z]+)-)?(?P<date>\d{4}-\d{2}-\d{2})\.csv$

lineups:      ranked-lineups-[<Type>-]<YYYY-MM-DD>[_<HHMMSS>].csv
              ^ranked-lineups-(?:(?P<type>[A-Za-z]+)-)?(?P<date>\d{4}-\d{2}-\d{2})(?:_(?P<ts>\d{6}))?\.csv$
```

- Normalize `type`: lowercase; if the optional group is absent ⇒ `main`. Validate against the allowed set; an unknown type is a validation error.
- **Lineups keep-latest:** a slate can have multiple lineups files. Select the one with the **max** `generated_at` in `data/lineups_slate_match/manifest.csv`, keyed by `slate_id`. The `_HHMMSS` filename suffix is **not** the selector — see the amendment below.

> **Amended Phase 3 (2026-07-27):** keep-latest originally read the `_HHMMSS` suffix. The relabeling side quest (see Phase 3 below and `data/lineups_slate_match/README.md`) rewrote each file's date/type from its DK-ID content but preserved the suffix, which is a *generation* time — so for 7 slates the max suffix now points at the wrong file. `latest_lineups_by_slate()` in `ingest/lineups.py` is authoritative.

### Tables — DDL (build exactly this)

```sql
CREATE TABLE IF NOT EXISTS dk_crosswalk (
  dk_id        INTEGER PRIMARY KEY,
  player_id    INTEGER NOT NULL,            -- ops dim_players.PLAYER_ID
  display_name TEXT
);

CREATE TABLE IF NOT EXISTS slate_players (
  slate_id    TEXT    NOT NULL,
  dk_id       INTEGER NOT NULL,
  name        TEXT,
  positions   TEXT,                         -- raw, e.g. "PG/G/UTIL"
  team        TEXT,
  opp         TEXT,
  salary      INTEGER,
  actual_fpts REAL,                         -- nullable until slate is played
  PRIMARY KEY (slate_id, dk_id)
);

CREATE TABLE IF NOT EXISTS projections (
  slate_id  TEXT    NOT NULL,
  dk_id     INTEGER NOT NULL,
  minutes   REAL,
  fppm      REAL,
  proj_pts  REAL,
  proj_own  REAL,
  PRIMARY KEY (slate_id, dk_id)
);

CREATE TABLE IF NOT EXISTS lineups (
  slate_id          TEXT    NOT NULL,
  final_rank        INTEGER NOT NULL,
  lineup_score      REAL,
  total_projection  REAL,
  total_ownership   REAL,
  geomean_ownership REAL,
  proj_rank         REAL,                   -- average-rank; ties split (amended, see below)
  own_rank          REAL,                   -- average-rank; ties split
  geo_rank          REAL,                   -- average-rank; ties split (amended, see below)
  PRIMARY KEY (slate_id, final_rank)
);

CREATE TABLE IF NOT EXISTS lineup_players (
  slate_id   TEXT    NOT NULL,
  final_rank INTEGER NOT NULL,
  slot       TEXT    NOT NULL,              -- PG SG SF PF C G F UTIL
  dk_id      INTEGER NOT NULL,
  PRIMARY KEY (slate_id, final_rank, slot)
);

CREATE INDEX IF NOT EXISTS ix_lineup_players_slate_dk
  ON lineup_players (slate_id, dk_id);      -- exposure rollups
```

> **Amended Phase 3 (2026-07-27):** `lineups.proj_rank` and `geo_rank` are REAL above;
> the originally pinned DDL had them INTEGER. All three ranks are average-ranks that split ties; the sample slate
> just happened to show ties only in `own_rank`. Across the 43 reconciled slates
> `Proj_Rank` is fractional 2266× and `Geo_Rank` 3002×. `SCHEMA_VERSION` is now 2 and
> `db/schema.py` is authoritative.

### Column mappings (source header → canonical)

**Salary CSV** (fully quoted; coerce types) → `slate_players`:
`DFS ID`→`dk_id` (int) · `Name`→`name` · `Position`→`positions` (keep raw) · `Team`→`team` · `Opponent`→`opp` · `Salary`→`salary` (int) · `Actual_FPTs`→`actual_fpts` (float, allow NaN). Add `slate_id` from the filename.

**Projections CSV** (plain, unquoted) → `projections`:
`ID`→`dk_id` (int — note: different header than the salary file's `DFS ID`, both are the DK id) · `Minutes`→`minutes` · `FPPM`→`fppm` · `Projection`→`proj_pts` · `Own_Proj`→`proj_own`. Add `slate_id`. Drop `Player`/`Team`/`Opponent` as redundant.

**Lineups CSV** → split into two tables:
- `lineups` (header grain): `Final_Rank`→`final_rank` · `Lineup_Score`→`lineup_score` · `Total_Projection`→`total_projection` · `Total_Ownership`→`total_ownership` · `Geomean_Ownership`→`geomean_ownership` · `Proj_Rank`→`proj_rank` · `Own_Rank`→`own_rank` · `Geo_Rank`→`geo_rank`. Add `slate_id`.
- `lineup_players` (8 rows per lineup): melt the 8 slot columns `PG SG SF PF C G F UTIL` to long form; extract `dk_id` from cells like `"Jamal Shead (42131681)"` via `r"\((\d+)\)"`. Add `slate_id`.

All lineups metrics are **projected**, not actual — don't treat them as outcomes.

### Actuals & scope

- `actual_fpts` comes from the salary CSV and is ingested now (nullable for unplayed slates).
- A later **one-time reconciliation** will compare `slate_players.actual_fpts` against the ops box scores (via the crosswalk). Don't build it this phase — but don't design anything that blocks it.
- **Actual ownership** is not in any current file → out of scope.

---

## Build order

Phases map to the spec's milestones, with one refinement: **the crosswalk is built last**, after the full backfill, so it covers the complete player universe in one pass. It never blocks ingest (sources key on the native DK id), so there's no reason to build it early.

**Stop and report at each ✋ gate before continuing.**

### Phase 0 — Orient & configure
- Read the repo. Confirm the environment facts above.
- Ask Jonny to confirm `SALARY_DIR` and `LINEUPS_DIR`.
- Populate `config.py`. Create `data/`.
- Write a `CLAUDE.md` capturing: project purpose, the data-only ops rule, the slate key, the five tables, the filename rules, and these working rules — so future sessions inherit the spec.
- In that `CLAUDE.md`, include **(a)** a `## Status` section as a living progress log (current phase, done, next, decisions) and **(b)** a pointer near the top: *"Full build order, phase gates, and acceptance checks live in `docs/ingestion-plan.md`; read it when starting or resuming a phase."*
- Ensure this plan doc is committed in the repo at `docs/ingestion-plan.md` so a cleared session can read it on demand.
- ✋ **Gate — CLEARED:** `config.py` + `CLAUDE.md` reviewed, all five paths resolve.

### Phase 1 — DB layer
- `db/schema.py`: the DDL above + `init_db(conn)` (creates all tables idempotently) and a `SCHEMA_VERSION` constant.
- `db/connection.py`: `get_connection(db_path=ANALYTICS_DB)` setting sensible PRAGMAs (`foreign_keys=ON`, WAL); `attach_ops(conn, ops_path=OPS_DB)` using a **read-only** URI (`file:…?mode=ro`, correctly URL-encoding the Windows path's spaces/backslashes — verify the ATTACH actually opens read-only).
- `db/writers.py`: `load_slate(conn, slate_id, df, table) -> int` — inside one transaction, `DELETE … WHERE slate_id = ?` then append `df`. Idempotent re-load.
- ✋ **Gate — CLEARED 2026-07-26** (`uv run python scripts/verify_gates.py`): all five tables present in `data/analytics.db`; ops DB attached, 6 tables visible; probe write rejected with `attempt to write a readonly database`.

### Phase 2 — Projections (the reference source)
Build the four-method shape — this is the template the other two copy:
```
read_projections(path) -> DataFrame
validate_projections(df) -> ValidationReport
normalize_projections(df, slate_id) -> DataFrame
ingest_projections(path, slate_id, conn) -> int        # read→validate→(stop if errors)→normalize→load_slate
```
- `ingest/filenames.py`: the three regexes, `parse_*` → `(date, slate_type)`, and `build_slate_id`.
- `ingest/schemas.py`: declarative column contract (source col → canonical → dtype → required) that both validate and normalize read from.
- `ValidationReport` dataclass: `ok, row_count, errors, warnings`. **Surface** problems — never silently drop rows; if invalid, write nothing and return the report.
- Load one real Main slate.
- ✋ **Gate — CLEARED 2026-07-26** (`uv run python scripts/verify_gates.py`): `NBA-Projs-2026-05-18.csv` → `2026-05-18_classic_main`, 72 rows / 1 slate; re-ingest wrote 72 with the total unchanged at 72; sample rows show all five canonical columns populated.

### Phase 3 — Salary + Lineups
- `ingest/salary.py`: mirror the shape → `slate_players` (incl. `actual_fpts`, int coercion on the quoted `DFS ID`/`Salary`).
- `ingest/lineups.py`: mirror the shape, plus the melt + id-extract → `lineups` and `lineup_players` (two `load_slate` calls). `validate_lineups` must assert all 8 slots per row yield exactly one integer id **before** the melt.
- ~~Lineups discovery applies keep-latest by `_HHMMSS`.~~ **Superseded:** keep-latest selects on `manifest.csv` → `generated_at`. The relabeling side quest left the `_HHMMSS` suffix as a *generation* time, so it picks the wrong file for 7 slates. See `data/lineups_slate_match/README.md` and the CLAUDE.md Status.
- Load the same slate's salary + lineups.
- ✋ **Gate — CLEARED 2026-07-27** (`uv run python scripts/verify_phase3.py`, run on `2026-05-18_classic_main`, `2026-03-13_classic_night`, `2026-04-02_classic_main`): salary and lineups both ingested and idempotent on re-ingest; 8 players per lineup on every lineup; 0 orphan rostered players; every lineup header has its slot rows. `--list` enumerates the 43 slates with both sources.
  - 8 players per lineup: `SELECT final_rank, COUNT(*) FROM lineup_players WHERE slate_id=? GROUP BY final_rank` → all 8.
  - No orphan rostered players: `SELECT COUNT(*) FROM lineup_players lp LEFT JOIN slate_players sp ON lp.slate_id=sp.slate_id AND lp.dk_id=sp.dk_id WHERE sp.dk_id IS NULL` → 0.
  - Two spec corrections forced by the real files: `late` is a sixth slate type, and `lineups.proj_rank`/`geo_rank` are REAL not INTEGER (`SCHEMA_VERSION` 2). Both recorded in CLAUDE.md Decisions.

### Phase 4 — Orchestrator + backfill
- `ingest/orchestrator.py`: `ingest_day(date, slate_type, conn)` building `slate_id` and calling the three source ingests; a discovery routine that finds the matching files across the three dirs for a given slate; and a `--dry-run` that validates without writing.
- ✋ **Gate (dry-run half) — CLEARED 2026-07-28.** Two commands: `uv run python scripts/verify_phase4.py` (all PASS — it samples slates per coverage combination), then the orchestrator's own CLI over every slate, `uv run python -m nba_dfs_stats_lab.ingest.orchestrator --dry-run`. Result: **412 slates discovered, 0 validation failures**, 0 unresolved filenames. Coverage 363 salary-only / 43 all-three / 3 salary+projections / 3 projections-only. Would write 51,971 + 8,856 + 71,109 + 568,872 rows. Two warnings total, both the known nullable `Team`/`Opponent` rows on `2026-02-02_classic_main`. The gate proves the dry run wrote nothing by comparing all four table counts before and after.
- ✋ **Gate (backfill half) — CLEARED 2026-07-28** (`uv run python scripts/verify_phase4.py --backfill`, all PASS): **412 slates, 0 failures.** `slate_players` 51,971 / 409 slates · `projections` 8,856 / 49 · `lineups` 71,109 / 43 · `lineup_players` 568,872 / 43 — rows written match rows in the DB on every table. Whole-DB integrity all PASS (8 per lineup, 0 orphan rostered players, header↔slot symmetry both ways, projections join wherever salary exists), and re-ingesting `2026-04-02_classic_main` changed no row count. 5 warnings, all off-slate games (6 games / 209 rows, handed to the ops-reconciliation pass). **Phase 4 complete.**

### Phase 5 — Crosswalk + unmatched report
- `ingest/crosswalk.py`: pull distinct `(dk_id, name)` from `slate_players`; match `name` against ops `dim_players.PLAYER_NAME` (read-only ATTACH) with a normalized/fuzzy match + confidence score; map `dk_id → PLAYER_ID`.
- Surface low-confidence matches for Jonny to review; write **approved** mappings only into `dk_crosswalk`.
- `unmatched_report()`: `dk_id`s in `slate_players` with no `dk_crosswalk` row — for ongoing monitoring of new players.
- ✋ **Gate:** show match-rate and the low-confidence list for review before writing.

---

## Working rules (apply throughout)

- **Idempotent writes only**, via `load_slate` (delete-by-slate then insert, one transaction). Re-running a slate must not duplicate or half-write.
- **Ops DB is read-only and query-time only** — `ATTACH … mode=ro`, never written, never imported, never copied in.
- **Surface, don't drop.** Validation returns errors/warnings; bad data is reported, not silently discarded. Nothing writes if validation fails.
- **Single writer**, no concurrent ingest.
- **Maintain `## Status` in CLAUDE.md.** At each ✋ gate, before committing and `/clear`, update the Status section (done / next / decisions). This is what lets a fresh session resume without re-pasting this plan.
- **Session hygiene at gates:** finish the phase → update `## Status` → `git commit` → `/clear`. To resume, a one-line brief ("Resume per CLAUDE.md — do the phase in ## Status, stop at its gate") is enough; CLAUDE.md auto-loads and points here for detail.
- **Tests:** add focused `pytest` unit tests for the bug-prone pure functions — filename parsing (each pattern, default-to-main, invalid type), `slate_id` construction, and lineup `dk_id` extraction (include a name with an apostrophe and a malformed cell). These are cheap and catch the real bugs.
- Keep `ruff` clean. No new dependencies without asking.

---

## First action

Do Phase 0 only: read the repo, confirm the environment, ask Jonny for the two unconfirmed directories, then show the `config.py` and `CLAUDE.md` you propose. Stop at the gate.