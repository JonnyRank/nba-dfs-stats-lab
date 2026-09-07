# Phase 6 — Ops reconciliation

**Status:** **built 2026-09-07.** Approved 2026-08-09; see §5 *As built* for the
three places the implementation departs from what §1-§3 pinned, and why.
**Depends on:** Phase 5 (`dk_crosswalk`, 51,734 rows).
**Goal:** bring `slate_players.actual_fpts` into line with the ops box scores, with
ops as the single source of truth.

**This doc is the spec for the phase.** `docs/ingestion-plan.md` carries the
build-order entry and the gate; everything else — the survey behind each number,
the decisions with what was rejected, the module shape, the reproduce SQL — lives
here.

The contradiction that prompted it is resolved. Reconciliation used to be listed
as out of scope in CLAUDE.md while simultaneously appearing under *Next*, and
`ingestion-plan.md` said "Don't build it this phase". As of 2026-08-09: CLAUDE.md
points here and its *Out of scope* list says so explicitly, and
`ingestion-plan.md` has a **Phase 6** section plus `> **Amended Phase 6**`
blockquotes on its *Actuals & scope* bullet and its "crosswalk is built last"
line.

---

## 1. Findings — the survey that shaped the plan

All figures from a read-only probe of both DBs on **2026-08-09**. Nothing was
written. The SQL is in [Appendix A](#appendix-a--reproduce) so this is
re-derivable without the scratchpad scripts.

### 1.1 The join

`slate_players.dk_id` → `dk_crosswalk.player_id` → `ops.fantasy_logs.DK_POINTS`,
keyed on the slate date. Ops side: `fantasy_logs` covers 2025-10-21 … 2026-06-13,
28,716 rows over 212 dates and 1,322 games, with **zero NULL `DK_POINTS`**.

| | Rows | |
|---|---|---|
| `slate_players` total | 51,971 | `actual_fpts` 100% non-null, 21,303 exact zeros, range −1.75 … 107.75 |
| crosswalked | 51,734 | 99.5% |
| not crosswalked | 237 | the 11 Phase-5 unmatched names |
| **lands on an ops log by exact date** | **31,730** | |
| — agrees to <0.001 | 30,800 | **97.1%** |
| — disagrees | 930 | broken down below |
| no ops log on the slate date | 20,004 | broken down below |

### 1.2 The 930 disagreements are not random

Classified by **cause**, not by magnitude — an earlier draft of this table
bucketed by delta size, which split one cause across rows and stranded a single
−0.25 row with no explanation. The four causes are cleanly separable and each
earns its own `reason` code in `fpts_audit`:

| Cause | Rows | `reason` | What it is |
|---|---|---|---|
| Float noise in the DK CSV | 680 | `float_noise` | \|Δ\| < 0.01, effectively all ±0.00333. Jordan Goodwin `25.746666666666663` vs ops `25.75`; Kawhi `67.74666666666667` vs `67.75`. DK points are quarter-granular by construction — ops is, the CSV isn't. |
| Off-slate game | 109 | `off_slate` | Whole game at 0 in the DK file, real box score in ops. §1.3. |
| **DK settled a player at 0 who played** | **61** | `dk_unscored` | Same shape as off-slate but at **player** grain, not game grain. New — see below. |
| Stat correction | 80 | `stat_correction` | Both values non-zero and DK-legal apart. The case for the phase. |

680 + 109 + 61 + 80 = 930.

#### Stat corrections (80 rows) — the case for the phase

Every delta is a legal DK scoring quantum, and they **cancel within a game**:

| Δ | Rows | DK quantum |
|---|---|---|
| ±2.00 | 31 | one steal or one block |
| ±1.25 | 25 | one rebound |
| ±0.50 | 15 | one 3PM made, or one turnover |
| ±1.50 | 7 | one assist |
| −1.00 | 1 | one point |
| −0.25 | 1 | not a single quantum — net of two, see below |

31 + 25 + 15 + 7 + 1 + 1 = 80.

| Slate | Player | analytics | ops | Δ |
|---|---|---|---|---|
| 2025-11-01 night | Jalen Duren (DET) | 54.00 | 52.00 | −2 |
| 2025-11-01 night | Javonte Green (DET) | 17.50 | 19.50 | +2 |
| 2025-11-03 night | Marcus Smart (LAL) | 25.00 | 23.00 | −2 |
| 2025-11-03 night | Nick Smith Jr. (LAL) | 38.25 | 40.25 | +2 |
| 2025-11-21 main | Nikola Jokic (DEN) | 69.50 | 67.50 | −2 |
| 2025-11-21 main | PJ Washington (DAL) | 43.25 | 45.25 | +2 |

A stat credited to the wrong player at settlement, corrected afterwards.
**DK's file is frozen at contest settlement; ops is post-correction.** That is
the whole case for the phase.

**The lone −0.25:** Pelle Larsson, `2026-03-08_classic_afternoon`, MIA vs DET —
DK 23.50, ops 23.75. Ops reconstructs exactly from its own box score under DK
scoring: 10 PTS + one 3PM (0.5) + 5 REB (6.25) + 5 AST (7.5) − 1 TOV (0.5) =
**23.75**, with no double-double bonus since only points reached 10. So ops is
arithmetically right and DK's file is 0.25 low. 0.25 is not reachable by any
single DK stat, so it is the *net* of at least two corrections — one rebound out
(−1.25) and one point in (+1.00) fits. And it isn't a lone oddity: five other
players in that same game moved too (−1.25, −1.25, −2.00, −1.25, +1.50), so the
game took a scorer's-table correction pass and Larsson caught two of them.

#### DK settled a player at 0 who played (61 rows) — new

**18 distinct players, all with real ops minutes (0.9 to 31.0).** DK's file has
them at exactly `0`; ops has a full box score.

| Slate | Player | analytics | ops |
|---|---|---|---|
| 2026-03-31 main + turbo | Moussa Cisse (DAL) | 0.00 | 33.75 |
| 2025-11-23 main | Jase Richardson (ORL) | 0.00 | 28.75 |
| 2026-03-01 early | Grant Nelson (BKN) | 0.00 | 24.50 |
| 2026-02-04 main + night | Jahmai Mashack (MEM) | 0.00 | 20.50 |

Behaviourally identical to the off-slate class — DK 0, ops real — but scoped to
a player rather than a whole game, so `check_zero_scored_games` cannot see it and
neither did the Phase 4 backfill. The names are fringe and two-way players, and
they recur across slates (Jase Richardson four times, Cisse and Mashack twice
each on the same date across two slate types), which points at late roster
additions DK never scored rather than anything random.

**The cause is not established** and this doc does not claim one. It does not
change the design — ops wins either way under D2 — but the 61 rows get their own
`reason` so they stay separable, and the report should list them for a look
during the build.

### 1.3 The off-slate games were played

New, and it inverts the parked assumption in
`docs/actual-fpts-zero-games.local.md` ("null the 208 values, or overwrite…").
**5 of the 6 have full ops box scores.** 2025-10-26 LAC/POR is not an unrecorded
game — ops has Harden at 52.0 and Kawhi at 55.0 against analytics' 0.0.

| Slate | Game | Rows | Ops box score? |
|---|---|---|---|
| 2025-10-26 main | LAC/POR | 35 | ✅ present |
| 2025-10-28 main | GSW/LAC | 34 | ✅ present |
| 2025-12-07 early | BOS/TOR | 34 | ✅ present |
| 2025-12-07 early | NYK/ORL | 34 | ✅ present |
| 2026-02-02 main | CHA/NOP | 36 | ✅ present |
| **2026-01-25 main** | **DAL/MIL** | **36** | ❌ **absent on every 2026 date** |

For DAL/MIL the nearest ops meetings are 2025-11-10 and 2026-03-31, both
separately scheduled. Those 36 rows have no ops evidence at all. *Jonny is
investigating this one separately — he and a friend looked into it recently.*

**All 209 rows are tagged `off_slate`, but only 109 change value:**

| | Rows | |
|---|---|---|
| corrected from an ops box score | 109 | the repair |
| ops row exists and already agrees | 5 | genuine DNPs in a played game, already 0 |
| no ops row — didn't play | 59 | DNPs in the 5 played games; stay 0 |
| no ops row — **whole game absent** | 36 | 2026-01-25 DAL/MIL; stay 0, `reason='no_ops_game'` |

The tag goes on all 209 so the affected population is one query away; the action
on 100 of them is `unchanged` or `no_ops_row`.

### 1.4 A two-day playoff slate, not a date bug

20,004 crosswalked rows have no ops log on the slate date. 19,790 hold exactly
`0` — genuine DNPs and inactives. But **214 are non-zero, and every single one
is rescued by ops date +1 with the value matching exactly.**

Wembanyama, analytics `2026-05-17` = 84.0 → ops `2026-05-18` SAS vs OKC = 84.0.
`Main-2026-05-17.csv` carries **both** CLE/DET and SAS/OKC; ops dates CLE@DET to
05-17 and SAS@OKC to 05-18.

Jonny's read, and it's the right one: **DK ran a genuine two-calendar-day slate**
during the conference finals. Neither source is wrong. So the ±1 resolution is
the correct model of the data, not a workaround for a defect.

> **Amended 2026-09-07 (build):** it is **ten** such slates, not one. The
> conference finals ran two series on alternating nights and DK listed **both**
> on every slate file, so exactly one of the two games always falls on the next
> calendar day. `2026-05-12` (CLE/DET), `2026-05-17` (OKC/SAS), and every slate
> from `05-18` through `05-25` — 10 slate dates, **20 game-sides**, all resolving
> at +1, and all 214 rescued rows are inside them. Nothing about the design
> changes: game-level ±1 handles ten exactly as it handles one, and the
> resolution counts below (2,284 same-day) already included them.

> **+1 must be tried before -1, and the build pins that.** On the `05-21` slate
> OKC/SAS exists in ops at *both* 05-20 and 05-22. A slate can only carry a game
> that has yet to tip off, so the later one is the right answer; trying -1 first
> would attribute Game 3's box scores to Game 4's slate.

> ### ⚠ The trap — why this must resolve at game level, not player level
> **907 of the 19,790 zero rows also have an ops log at +1 day** — players who
> simply played the next night. A blanket per-player ±1 window would write the
> wrong game's score onto 907 rows. The shift has to be established for a whole
> **matchup** and then applied to the players in it.

Game-level resolution is clean and available:

- `ops.map_teams` has all 30 teams; **zero unmapped values in either direction**
  (no ops `TEAM` outside it, no `slate_players.team` outside it).
- Of 2,306 distinct `(slate date, team, opp)` game-sides: **2,284 match ops
  same-day**, 30 match at +1, 32 at −1, ~22 need the shift or don't exist.
- Same-day always wins, so a team playing on both `d` and `d+1` is never
  ambiguous — ±1 is consulted only when same-day finds nothing.

### 1.5 The 237 uncrosswalked rows

Exactly the 11 Phase-5 unmatched names fanned out to the `dk_id` grain: Thomas
Sorber (108), Nikola Djurisic (54), Eli Ndiaye (35), Alex Toohey (26), Kyle
Mangas (5), Tyreke Key (4), then Augustas Marciulionis, Cameron Matthews, RJ
Davis, Taevion Kinsey, Zack Austin at 1 each. 108+54+35+26+5+4+5 = 237.

With no `dk_crosswalk` row there is no `player_id`, so ops is unreachable for
them — and **all 237 already hold `actual_fpts = 0.0`**, consistent with Phase
5's finding that they never logged an NBA second. Nothing to correct. They get
`action='unmapped'` and stay visible. If one debuts, `unmatched_report()` catches
it and the next reconcile picks him up.

---

## 2. Decisions (approved 2026-08-09)

### D1 — Overwrite in place, plus a full-census audit table

`UPDATE slate_players.actual_fpts` with the ops value, and record **every** row
in a new `fpts_audit` table — not just the changed ones. A census makes the
audit self-verifying (`COUNT(fpts_audit) == COUNT(slate_players)` is a gate
check) and makes "why is this row 0?" answerable for all 51,971 rows.

Rejected: keeping both columns (pushes the which-is-truth decision into every
future query), and a separate table + view (safest, but doesn't literally
modify the records, and downstream code must remember the view).

### D2 — Game-level join with ±1 resolution; no nulling

Fixes the 930 exact-date mismatches plus the 214 date-shifted rows, ~1,144 rows
(2.2%). DNP zeros **stay 0** — DK scores an unused roster slot as 0 and nulling
them would break lineup scoring. Rows with no ops evidence (2026-01-25 DAL/MIL,
the 237 uncrosswalked) are left untouched and reported.

`actual_fpts` stays 100% non-null. This phase introduces no NULLs.

### D3 — Overwrite the off-slate rows, tagged

Apply the real box scores per the ops-as-truth rule, but tag all 209 rows with
`reason='off_slate'` so a backtest can exclude them deliberately. (`off_slate`
is a **reason**, not an action — 109 of the 209 are `action='corrected'` and the
rest are `unchanged` or `no_ops_row`; see the §1.3 breakdown. An earlier draft of
this doc wrote it as `action='off_slate'`, which would have collided with the
four-value action vocabulary.)

**This is the one place the two truths genuinely conflict.** If the game tipped
outside the contest window, DK's `0` was correct *contest* semantics — those
players were unscoreable in that slate — while ops' box score is correct *player*
semantics. Overwriting means a backtest of the 43 slates with lineups will score
players who didn't count. The tag is what makes that recoverable.

### D4 — Docs

**Done 2026-08-09**, before any code — appends and targeted edits only, never a
rewrite:

| File | Change |
|---|---|
| `CLAUDE.md` | Status → Phase 6 + pointer here; *Out of scope* bullet replaced with an explicit "now in scope as Phase 6" line so it can't drift back; *Next* expanded; three new Decisions (two-day slate, game-grain-not-player-grain, DNP zeros stay 0); amendment on the existing off-slate decision |
| `docs/ingestion-plan.md` | new **Phase 6** section with the build-order entry and the ✋ gate; `> **Amended Phase 6**` on the *Actuals & scope* bullet and on "the crosswalk is built last" |
| `docs/phase6-ops-reconciliation.md` | this doc — the spec |

The split: `ingestion-plan.md` stays the phase register (what to build, where the
gate is), this doc holds the detail. That keeps the register readable and means a
resumed session reads one file, not a phase's worth of findings inlined into the
build order.

---

## 3. The plan

### 3.1 Files

| Path | What |
|---|---|
| `src/nba_dfs_stats_lab/ingest/reconcile.py` | new module |
| `src/nba_dfs_stats_lab/db/schema.py` | `fpts_audit` DDL, `SCHEMA_VERSION` 2 → 3, `migrate()` step |
| `scripts/verify_phase6.py` | the runnable gate |
| `tests/test_reconcile.py`, `tests/test_verify_phase6.py` | unit tests both layers |

`reconcile.py` lives in `ingest/` rather than a new package: it's a post-ingest
step of the same pipeline, `orchestrator.py` is already there, and one module
doesn't justify a third package.

### 3.2 Schema

> **Amended 2026-09-07 (build):** `fpts_audit` gained an **`off_slate INTEGER
> NOT NULL DEFAULT 0`** column, and the reason counts in the table below moved
> with it. The DDL and the census as built are in §5.1; the reasoning is §5.2.
> The nine columns below are otherwise exactly as shipped.

```sql
CREATE TABLE IF NOT EXISTS fpts_audit (
  slate_id   TEXT    NOT NULL,
  dk_id      INTEGER NOT NULL,
  player_id  INTEGER,            -- NULL when uncrosswalked
  game_date  TEXT,               -- the ops DATE actually used; may differ from the slate date
  dk_value   REAL,               -- the pristine value as ingested from the DK CSV
  ops_value  REAL,               -- ops DK_POINTS; NULL when no ops row
  delta      REAL,               -- ops_value - dk_value
  action     TEXT    NOT NULL,   -- corrected | unchanged | no_ops_row | unmapped
  reason     TEXT,               -- see below
  PRIMARY KEY (slate_id, dk_id)
);
```

`action` is **what happened** — one of exactly four:
`corrected` · `unchanged` · `no_ops_row` · `unmapped`.

`reason` is **why**, and the two are orthogonal — `off_slate` in particular spans
three actions (§1.3):

| `reason` | Rows today | Usual `action` |
|---|---|---|
| `float_noise` | 680 | `corrected` |
| `stat_correction` | 80 | `corrected` |
| `dk_unscored` | 61 | `corrected` |
| `off_slate` | 209 | 109 `corrected`, 5 `unchanged`, 95 `no_ops_row` |
| `date_shift` | 214 | `corrected` |
| `dnp` | ~19,700 | `no_ops_row` |
| `no_ops_game` | 36 | `no_ops_row` |
| `no_crosswalk` | 237 | `unmapped` |
| *(none)* | ~30,800 | `unchanged` |

`dk_unscored` is the class §1.2 turned up: DK settled a player at 0 who played
real minutes. It is not folded into `off_slate` because that one is a property of
the **game** and this one is a property of a **player** — merging them would make
the off-slate population unqueryable.

`SCHEMA_VERSION` → 3. `migrate()` needs no destructive step — the table is new
and `init_db`'s `IF NOT EXISTS` creates it. Follow the existing rule: detect
drift from **live column types**, not from `user_version`.

### 3.3 Shape

```python
build_game_index(conn, alias="ops")  -> dict[(date, team, opp), OpsGame]
resolve_game_dates(conn, index)      -> dict[(slate_date, team, opp), GameLink]  # exact -> +1 -> -1
load_ops_points(conn, alias)         -> dict[(player_id, date), float]
off_slate_games(conn)                -> set[(slate_id, team, opp)]
reconcile(conn, slate_ids=None)      -> ReconcileReport of AuditRow   # pure, writes nothing
write_audit(conn, rows)              -> int
apply_corrections(conn, rows)        -> int
```

`reconcile()` computes; nothing writes without `--write`. Same posture as
`crosswalk.py`, where reporting is the default and writing is opt-in.

Rows with NULL `team`/`opp` (Collin Sexton on `2026-02-02_classic_main` is the
one remaining) fall back to the exact `(player_id, slate_date)` lookup — no game
to resolve, so no shift is possible for them.

### 3.4 Idempotency — the one genuinely tricky bit

`load_slate` is **delete-then-insert**, so re-ingesting any slate silently
reverts its reconciliation. Two consequences:

1. Reconcile must be re-runnable and produce the identical result every time.
2. `dk_value` must stay the *pristine* DK value across re-runs. Reading it back
   from `slate_players` after a correction would capture the ops value and
   destroy the record of what DK said.

Rule: **reuse the existing audit row's `dk_value` when the correction is still
in place** — i.e. when `slate_players.actual_fpts` equals that row's
`ops_value`. Otherwise the row has been re-ingested or hand-edited, so take the
current `slate_players` value as the new pristine `dk_value`. That handles
Jonny editing a source CSV (as he did for Sexton) without a manual reset.

Wiring reconcile into the orchestrator as an automatic post-ingest step is
**deferred** — decide it after the one-time pass has run.

### 3.5 CLI

```
uv run python -m nba_dfs_stats_lab.ingest.reconcile              # report only, writes nothing
uv run python -m nba_dfs_stats_lab.ingest.reconcile --write      # apply + audit
uv run python -m nba_dfs_stats_lab.ingest.reconcile --slate ID   # one slate
uv run python -m nba_dfs_stats_lab.ingest.reconcile --audit      # print the standing audit rollup
```

### 3.6 The gate — `scripts/verify_phase6.py`

> **Amended 2026-09-07 (build):** shipped with **27 checks**, not 14 — the 15
> below plus the write-stage and idempotency checks they imply, each split so a
> failure names one thing. Checks 12 and 15 changed shape with `off_slate`; the
> as-built list and the revised expected counts are in §5.3.

Two stages like Phase 5: report-only first so the change list is in front of
Jonny before anything writes, then `--write` and re-check against the DB.

**Every check below must be able to FAIL on some data.** Phase 5's PR #10 review
found three that couldn't — a check that cannot fail reads as coverage it isn't
providing. The "what would make this fail" column is the test of each one.

| # | Check | What would make it FAIL |
|---|---|---|
| 1 | ops attaches read-only; probe write rejected | a writable ATTACH |
| 2 | every `slate_players` row has exactly one `fpts_audit` row | a slate skipped by the census |
| 3 | `action` totals sum to 51,971 | a row classified twice or not at all |
| 4 | no `action='corrected'` row with NULL `ops_value` | a correction with nothing behind it |
| 5 | `dk_value + delta == ops_value` on every corrected row | arithmetic/normalization drift |
| 6 | post-write, every corrected row's `actual_fpts == ops_value` | the UPDATE missing rows |
| 7 | every `unchanged` / `no_ops_row` / `unmapped` row's `actual_fpts == dk_value` | the UPDATE over-reaching |
| 8 | every `game_date` is within ±1 day of the slate date | a bad game-index match |
| 9 | every row on a shifted `game_date` belongs to a matchup shifted **as a whole** | the 907-row per-player trap firing |
| 10 | `actual_fpts` has no NULLs | D2 violated |
| 11 | the 237 uncrosswalked rows are untouched and still 0 | reaching rows with no `player_id` |
| 12 | `reason='off_slate'` rows == 209, of which 109 `corrected` | the off-slate detector drifting |
| 13 | re-running changes no value and no audit row count | non-idempotent write |
| 14 | ops table row counts identical before and after | anything written to ops |
| 15 | the `reason` census matches §1.2: 680 / 109 / 61 / 80 | the classifier drifting, or new data of a class we haven't seen |

Check 15 earns its place because it is the only one that fails on *new* data
rather than on a code defect — a new slate, or a refreshed ops snapshot, moves
those counts. That is a prompt to look, not a bug: re-derive with Appendix A, and
if the new rows fall into an existing class, re-pin the numbers.

**Expected on today's data:**

| `action` | Rows |
|---|---|
| `corrected` | ~1,144 |
| `unchanged` | ~30,800 |
| `no_ops_row` | 20,004 |
| `unmapped` | 237 |
| **total** | **51,971** |

---

## 4. Loose ends

- **2026-01-25 DAL/MIL (36 rows)** — Jonny is investigating whether the game was
  played. Until then: untouched, `action='no_ops_row'`, `reason='no_ops_game'`.
- **`docs/actual-fpts-zero-games.local.md`** — its repair suggestion ("null the
  208 values, or overwrite…") is settled: overwrite, because §1.3 shows the games
  were played. The file is gitignored and still reads as an open question; worth
  a line when the phase lands.
- **Orchestrator wiring** — deferred, see §3.4.
- **~~Backtest impact~~** — cleared 2026-08-09. D3 moves 209 rows across 5 slates,
  one of which (2025-12-07 early) has lineups, but Jonny has not run any
  backtests yet, so nothing downstream moves with it.

`docs/ingestion-plan.md` is no longer a loose end — its Phase 6 section and both
amendments landed 2026-08-09 (D4).

---

## Appendix A — reproduce

Run against `data/analytics.db` with ops attached read-only:

```python
from nba_dfs_stats_lab.db.connection import get_connection, attach_ops
conn = get_connection(); attach_ops(conn)
```

**The join and the headline split:**

```sql
CREATE TEMP TABLE j AS
SELECT sp.slate_id, sp.dk_id, sp.name, sp.team, sp.opp, sp.actual_fpts,
       substr(sp.slate_id,1,10) AS d, x.player_id,
       f.DK_POINTS AS ops_fpts, f.MINUTES AS ops_min
  FROM slate_players sp
  LEFT JOIN dk_crosswalk x USING(dk_id)
  LEFT JOIN ops.fantasy_logs f
         ON f.PLAYER_ID = x.player_id AND f.DATE = substr(sp.slate_id,1,10);

SELECT COUNT(*)                                                        AS total,
       SUM(player_id IS NULL)                                          AS no_crosswalk,
       SUM(player_id IS NOT NULL AND ops_fpts IS NULL)                 AS no_ops_row,
       SUM(ops_fpts IS NOT NULL AND ABS(actual_fpts-ops_fpts)<0.001)   AS agree,
       SUM(ops_fpts IS NOT NULL AND ABS(actual_fpts-ops_fpts)>=0.001)  AS disagree
  FROM j;                       -- 51971 | 237 | 20004 | 30800 | 930
```

**The delta histogram** (shows the ±0.00333 float noise and the ±2.0 blocks):

```sql
SELECT ROUND(actual_fpts-ops_fpts,4) delta, COUNT(*) n
  FROM j WHERE ops_fpts IS NOT NULL AND ABS(actual_fpts-ops_fpts) >= 0.001
 GROUP BY 1 ORDER BY n DESC;
```

**The 930 by cause (§1.2)** — returns `680 | 109 | 61 | 80`. Off-slate games are
derived the same way `check_zero_scored_games` does it: every rostered player on
**both** sides of a matchup at exactly 0.

```sql
CREATE TEMP TABLE side AS
  SELECT slate_id, team, opp, MAX(ABS(actual_fpts)) mx
    FROM slate_players WHERE team IS NOT NULL AND opp IS NOT NULL
   GROUP BY slate_id, team, opp;

CREATE TEMP TABLE offslate AS          -- 12 rows = the 6 known games, both sides
  SELECT a.slate_id, a.team FROM side a
    JOIN side b ON b.slate_id = a.slate_id AND b.team = a.opp AND b.opp = a.team
   WHERE a.mx = 0 AND b.mx = 0;

SELECT CASE
         WHEN ABS(sp.actual_fpts - f.DK_POINTS) < 0.01 THEN 'float_noise'
         WHEN EXISTS (SELECT 1 FROM offslate o
                       WHERE o.slate_id = sp.slate_id AND o.team = sp.team)
                                                       THEN 'off_slate'
         WHEN sp.actual_fpts = 0                       THEN 'dk_unscored'
         ELSE                                               'stat_correction'
       END AS reason,
       COUNT(*) n
  FROM slate_players sp
  JOIN dk_crosswalk x USING(dk_id)
  JOIN ops.fantasy_logs f
    ON f.PLAYER_ID = x.player_id AND f.DATE = substr(sp.slate_id,1,10)
 WHERE ABS(sp.actual_fpts - f.DK_POINTS) >= 0.001
 GROUP BY 1;
```

**The +1-day rescue, and the trap** — the first returns `214 | 214 | 214`
(all rescued, all values matching); the second returns `19790 | 907`:

```sql
WITH miss AS (
  SELECT substr(sp.slate_id,1,10) d, x.player_id, sp.actual_fpts
    FROM slate_players sp JOIN dk_crosswalk x USING(dk_id)
   WHERE sp.actual_fpts <> 0                    -- flip to = 0 for the second query
     AND NOT EXISTS (SELECT 1 FROM ops.fantasy_logs f
                      WHERE f.PLAYER_ID = x.player_id
                        AND f.DATE = substr(sp.slate_id,1,10))
)
SELECT COUNT(*),
       SUM(EXISTS(SELECT 1 FROM ops.fantasy_logs f
                   WHERE f.PLAYER_ID = miss.player_id
                     AND f.DATE = date(miss.d,'+1 day'))),
       SUM((SELECT ABS(f.DK_POINTS-miss.actual_fpts)<0.001 FROM ops.fantasy_logs f
             WHERE f.PLAYER_ID = miss.player_id AND f.DATE = date(miss.d,'+1 day')))
  FROM miss;
```

**Game-level resolution** — returns `2306 | 2284 | 30 | 32`:

```sql
WITH g AS (
  SELECT DISTINCT substr(slate_id,1,10) d, team, opp FROM slate_players
   WHERE team IS NOT NULL AND opp IS NOT NULL),
og AS (
  SELECT DISTINCT f.DATE d, mt.TEAM_ABBREVIATION t, mo.TEAM_ABBREVIATION o
    FROM ops.fantasy_logs f
    JOIN ops.map_teams mt ON mt.RAW_TEAM_NAME = f.TEAM
    JOIN ops.map_teams mo ON mo.RAW_TEAM_NAME = f.OPPONENT
   WHERE f.DATE >= '2025-10-01')
SELECT COUNT(*),
       SUM(EXISTS(SELECT 1 FROM og WHERE og.d=g.d               AND og.t=g.team AND og.o=g.opp)),
       SUM(EXISTS(SELECT 1 FROM og WHERE og.d=date(g.d,'+1 day') AND og.t=g.team AND og.o=g.opp)),
       SUM(EXISTS(SELECT 1 FROM og WHERE og.d=date(g.d,'-1 day') AND og.t=g.team AND og.o=g.opp))
  FROM g;
```


---

## 5. As built (2026-09-07)

Everything in §1 re-derived exactly against the same two snapshots before a line
was written — 51,971 | 237 | 20,004 | 30,800 | 930, causes 680 / 109 / 61 / 80,
game-sides 2,306 | 2,284 | 30 | 32. Three things then changed, two of them
because the data said so and one because a test caught a bug.

### 5.1 `off_slate` is a column, not a `reason` value

**The problem.** `reason` is a single column, and §1.3 asked it to carry two
orthogonal facts at once: all 209 off-slate rows tagged `off_slate` (D3, check
12) *and* the 36 DAL/MIL rows reading `no_ops_game` (§1.3, §3.2). A third
collision turned up during the build: **Alex Toohey** (GSW, `2025-10-28`) is
inside an off-slate game *and* one of the 11 uncrosswalked names, so his row
wants `no_crosswalk` too. That is why a detector reading `reason` alone counts
208 where D3 says 209.

**The fix** (Jonny, 2026-09-07): `fpts_audit.off_slate INTEGER NOT NULL DEFAULT
0`. Off-slate is a property of the **game**; `reason` stays one primary cause per
row. The flag carries the whole population — a backtest excludes it with
`WHERE off_slate = 1` — and the three rows where the two disagree keep their
narrower cause.

| | Rows |
|---|---|
| `off_slate = 1` | **209** — 109 `corrected`, 5 `unchanged`, 94 `no_ops_row`, 1 `unmapped` |
| of which `reason = 'off_slate'` | 172 |
| `reason = 'no_ops_game'` (DAL/MIL) | 36 |
| `reason = 'no_crosswalk'` (Toohey) | 1 |

Reason precedence, in order: `no_crosswalk` → `no_ops_game` → `off_slate` → the
value causes / `date_shift` / `dnp`. `off_slate` sits above the value causes so
the 109 corrections on games DK never scored keep a cause of their own rather
than collapsing into `dk_unscored` — which is *true* of them but would hide the
game-level fact and make the off-slate corrections unfindable by reason.

### 5.2 `date_shift` corrects nothing — it marks where a value came from

§3.2 pinned `date_shift` as **214 `corrected`** rows. It is not: those 214 rows
*already agree* with ops once the right date is used. The shift repairs
`game_date`, not the value.

The 20 shifted game-sides hold 359 `slate_players` rows: **232** crosswalked rows
that match ops exactly, **122** DNPs with no ops log, **5** uncrosswalked, and
**0** corrections. So (Jonny, 2026-09-07) `reason = 'date_shift'` marks the **232
rows that actually took a value from a shifted date**, all of them
`action = 'unchanged'`. The 122 DNPs read `dnp`; their shift is still visible in
`game_date`, which is why the gate reports **354** rows on a shifted `game_date`
(232 + 122) rather than 359 — the 5 uncrosswalked rows never get one. A shifted
row that also needed a correction would take the value cause, since that is the
actionable half — none exist today.

### 5.3 The census as built, and the two checks that fail on new data

| `action` | Rows | | `reason` | Rows | Usual `action` |
|---|---|---|---|---|---|
| `corrected` | 930 | | *(none)* | 30,795 | `unchanged` |
| `unchanged` | 31,032 | | `dnp` | 19,678 | `no_ops_row` |
| `no_ops_row` | 19,772 | | `float_noise` | 680 | `corrected` |
| `unmapped` | 237 | | `no_crosswalk` | 237 | `unmapped` |
| **total** | **51,971** | | `date_shift` | 232 | `unchanged` |
| | | | `off_slate` | 172 | 109 `corrected`, 5 `unchanged`, 58 `no_ops_row` |
| | | | `stat_correction` | 80 | `corrected` |
| | | | `dk_unscored` | 61 | `corrected` |
| | | | `no_ops_game` | 36 | `no_ops_row` |

§3.6's estimate of ~1,144 `corrected` / ~30,800 `unchanged` counted the 214
date-shifted rows as corrections. They are not, so they sit in `unchanged`
instead: 930 and 31,032. The three-way split of the 20,004 rows with no ops log
on the *slate* date is 19,772 still without one plus the 232 the shift rescued.

`EXPECTED_ACTIONS`, `EXPECTED_REASONS` and the two `EXPECTED_OFF_SLATE_*`
constants in `scripts/verify_phase6.py` pin these. They are the checks that fail
on **new data** rather than on a code defect — a new slate or a refreshed ops
snapshot moves them. That is a prompt to look, not a bug: re-derive with
[Appendix A](#appendix-a--reproduce), and if the new rows fall into an existing
class, re-pin the constants.

The gate ships **27 checks**: 15 in the report-only stage, 11 more after
`--write`, and one comparing every ops table's row count to what it was at
startup. Every one can FAIL on some data — `tests/test_verify_phase6.py` proves
it for the censuses, the ops dependency, the correction/over-reach pair, the
no-NULL rule and the split-matchup trap by injecting each failure and asserting
the FAIL line.

### 5.4 The bug the idempotency test found: off-slate detection ate itself

Worth recording, because it is the sharpest instance of the §3.4 problem and
nothing in the survey predicted it.

The off-slate signature is *"every rostered player on both sides of a matchup at
exactly 0"* — and `apply_corrections` overwrites precisely those zeros. Read from
the live `slate_players.actual_fpts`, the detector finds the six games on the
first pass and **nothing** on the second: `off_slate` silently drops from all 209
rows, and with it the one thing that makes them excludable from a backtest.
Nothing would have flagged it — the second run passes every other check.

The fix is the same rule `_pristine_dk_value` already applied to values, extended
to the detector: `reconcile()` reconstructs the pristine DK value for every row
*first*, then derives the off-slate games from those. `off_slate_sides()` takes
the values as an argument rather than a connection so it cannot accidentally read
the corrupted column; `off_slate_games(conn)` remains as the pre-write probe.
Pinned by `test_the_off_slate_flag_survives_a_second_pass`.

Related, and load-bearing: **`write_audit` runs before `apply_corrections`.** A
crash between them leaves an audit row whose `ops_value` differs from the
untouched `actual_fpts`, which is exactly the "correction is not in place" branch
of `_pristine_dk_value`, so the next pass still recovers the true DK value. The
other order would lose it permanently.

### 5.5 Still open

- **2026-01-25 DAL/MIL (36 rows)** — unchanged from §4. `action='no_ops_row'`,
  `reason='no_ops_game'`, `off_slate=1`. The only matchup in 2,306 that resolves
  to nothing on any of the three dates.
- **`dk_unscored` (61 rows / 18 players)** — the cause is still not established.
  The report lists all 61 by name under `--write`-less runs so they can be looked
  at; ops wins either way under D2.
- **Orchestrator wiring** — still deferred, per §3.4.
