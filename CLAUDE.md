# NBA DFS Stats Lab — Project Guide

> Full build order, phase gates, and acceptance checks live in `docs/ingestion-plan.md`. CLAUDE.md is the always-loaded condensed reference; the plan doc is the phase-by-phase detail. Read it when starting or resuming a phase.

## Purpose

Ingest DraftKings DFS data into a local SQLite analytics DB (`data/analytics.db`) to support DFS modeling. The analytics DB is the output artifact of this project; the ops DB (`bigdataball`) is a read-only data dependency.

---

## Status

_Update at every gate before `/clear`: done / next / decisions. Keep it short._

**Current phase:** Phase 6 **done and merged** (`6bcf62a`, PR #11) — ops reconciliation shipped and applied 2026-09-07. `docs/phase6-ops-reconciliation.md` is the spec; its **§5 *As built*** carries the three places the implementation departs from §1-§3 and why. Next phase not yet chosen.
**Last gate cleared:** Phase 6 — `uv run python scripts/verify_phase6.py --write`, all 27 PASS 2026-09-07. **`fpts_audit` is built: 51,971 rows; 930 `actual_fpts` values corrected.**

### Phase 6 results (2026-09-07) — 930 of 51,971 rows corrected, 27 of 27 PASS

**Merged as `6bcf62a` (PR #11)** after three review rounds — the Claude reviewer twice and CodeRabbit once, final verdict **Go**. See the PR #11 review-round bullet under *Done* for what they caught and why the gate couldn't have.

**Reproduce from scratch** (no human in the loop — `reconcile` is idempotent and re-derives everything from the two DBs):
```sh
uv run python scripts/verify_phase6.py            # report only, writes nothing
uv run python scripts/verify_phase6.py --write    # apply + build the audit, then re-check
```

| `action` | Rows | | `reason` | Rows |
|---|---|---|---|---|
| `corrected` | 930 | | *(none)* | 30,795 |
| `unchanged` | 31,032 | | `dnp` | 19,678 |
| `no_ops_row` | 19,772 | | `float_noise` | 680 |
| `unmapped` | 237 | | `no_crosswalk` | 237 |
| **total** | **51,971** | | `date_shift` | 232 |
| | | | `off_slate` | 172 |
| | | | `stat_correction` | 80 |
| | | | `dk_unscored` | 61 |
| | | | `no_ops_game` | 36 |

The 930 corrections are 680 `float_noise` + 109 `off_slate` + 80 `stat_correction` + 61 `dk_unscored`. Net change **+2,747.42** fantasy points; largest single move **+55.00** (Kawhi Leonard, `2025-10-26_classic_main` — an off-slate 0.00 becoming a real box score, not a scoring change).

**Two incidental confirmations from the write output**, worth more than the checks that asserted them:
- `TOTAL(actual_fpts)` after the write is **698,900.75**, *exactly* `TOTAL(ops_value)`. That can only hold if every one of the 20,009 rows without an ops value sits at exactly 0.0 — D2's "DNP zeros stay 0" verified by arithmetic rather than by rule.
- `TOTAL(dk_value)` is **696,153.32999998**. That trailing `.32999998` **is** the 680 float-noise rows; post-correction the total is a clean quarter-granular figure.

**The `off_slate` flag is 209 rows spanning three reasons** — 172 `off_slate`, 36 `no_ops_game` (DAL/MIL), 1 `no_crosswalk` (Alex Toohey) — and four actions: 109 `corrected`, 94 `no_ops_row`, 5 `unchanged`, 1 `unmapped`. That spread is exactly why it is a column and not a `reason` value (see Decisions).

### Phase 5 results (2026-08-09) — 599 of 610 names mapped

**Reproduce the whole crosswalk from scratch** (approvals are tracked in git, so this needs no human):
```sh
uv run python scripts/verify_phase5.py                                          # report only, writes nothing
uv run python scripts/verify_phase5.py --write --apply docs/crosswalk-approvals.csv
```

| | Names | `slate_players` rows |
|---|---|---|
| auto-matched (593 exact + 4 normalized) | 597 | 51,633 (99.3%) |
| + Jonny-approved | 2 | 101 |
| **`dk_crosswalk` total** | **599** | **51,734 (99.5%)** |
| unmatched | 11 | 237 |

All 22 gate checks PASS: 0 ambiguous, 0 normalization collisions on either side, ops probe write still rejected, 0 orphan `dk_id`, every `player_id` real in ops, nothing unapproved written, re-running changes no row count.

**Approved (2 of 4 reviewed):** `Yanic Niederhauser` → `Yanic Konan Niederhauser` (1642949) · `Hansen Yang` → `Yang Hansen` (1642905).
**Rejected:** `RJ Davis`, `Cameron Matthews` — the candidates offered (`Ed`/`Johnny`/`Terence Davis`, `Wesley Matthews`) are different people, and both DK names are themselves absent from ops.

**11 unmatched (237 rows) — all genuinely absent from ops, not a matcher failure.** Verified: not one of those surnames exists anywhere in `dim_players`, and ops holds no non-ASCII names, so nothing is hiding behind a fold. Thomas Sorber (108), Nikola Djurisic (54), Eli Ndiaye (35), Alex Toohey (26), Kyle Mangas (5), Tyreke Key (4), Augustas Marciulionis, Cameron Matthews, RJ Davis, Taevion Kinsey, Zack Austin (1 each). Rookies and two-ways who made DK's player pool but never logged an NBA second. `unmatched_report()` is the standing monitor — re-run it after each new slate.

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
- **Phase 5** (this branch): `ingest/crosswalk.py` — `normalize_name`, `score_candidate`, `match_players()` → a `MatchReport` of `Tier`-ed `NameMatch`es; the review CSV round-trip (`write_review_csv` / `read_approvals` / `apply_approvals`); `write_crosswalk` (upsert), `clear_crosswalk`, `unmatched_report()`, `coverage()`. CLI: `--review`, `--apply`, `--write`, `--rebuild`, `--unmatched`, `-v`. Plus `scripts/verify_phase5.py`. **285 pytest tests, ruff clean.**
  - **PR #10 review round** closed four silent-write holes, none of them reachable on today's snapshot but all of them the same failure mode the module exists to prevent: a `dk_id` under two names (see Decisions), an empty normalized key auto-matching two unrelated players, a sub-floor near miss being approvable, and the gate's `dk_id` uniqueness check being a tautology over its own PRIMARY KEY. Also: `_failures` now resets per `main()` (the gate was not re-entrant, which made the suite order-dependent), a missing `ops.dim_players` is a FAIL line rather than a traceback, the collision gate filters NULL/blank names and buckets ops `player_id`s so duplicate ops names are visible to it, `TRIM(name)` is consistent across all five name-grouping queries, and the tracked approvals CSV is pinned by tests.
  - **A gate check that cannot fail is worse than no check** — it reads as coverage. Three were found this round. Two were tautologies over a PRIMARY KEY or over `approvable`'s own definition (no data could fail either); the third only ran under `--write`, so it answered its question after the write it guarded. When adding a check, ask what data would make it FAIL — if the answer is "none", it belongs somewhere else or nowhere. `--rebuild` also picked up its first tests at both layers.

- **Phase 6** (merged `6bcf62a`, PR #11): `ingest/reconcile.py` — `build_game_index`, `resolve_game_dates` (exact → +1 → −1, per *matchup*), `load_ops_points`, `off_slate_sides`/`off_slate_games`, `reconcile()` → a `ReconcileReport` of `AuditRow`s, `write_audit` (delete-then-insert per slate), `apply_corrections`, `audit_rollup`. CLI: `--write`, `--slate`, `--audit`, `-v`, and a **"What --write will do"** preview on the report-only path. Plus `fpts_audit` DDL + `SCHEMA_VERSION` 3 + a v2→v3 `migrate()` step, and `scripts/verify_phase6.py` (27 checks). **361 pytest tests, ruff clean.**
  - **A bug the idempotency test found, that nothing else would have.** The off-slate signature is "every rostered player on both sides at exactly 0" — and `apply_corrections` overwrites precisely those zeros. Read from the live `actual_fpts`, the detector finds the six games on the first pass and **nothing** on the second: `off_slate` silently drops from all 209 rows, taking D3's whole point with it, while every other check still passes. Fixed by deriving off-slate from the **pristine** DK values (see Decisions). The same class as §3.4's `dk_value` problem, one level up — the reconciliation corrupting the very signal it was computed from.
  - **`write_audit` runs before `apply_corrections`**, and the order is load-bearing. A crash between them leaves an audit row whose `ops_value` differs from the untouched `actual_fpts` — exactly the "correction is not in place" branch of `_pristine_dk_value` — so the next pass still recovers the true DK value. The other order loses it permanently.
  - **PR #11 review round — three reviewers, and every finding was *outside* what the gate checks.** The gate passed 27/27 both before and after. It asserts things about the **data**; all six findings were in the code *around* the data, where it has nothing to say. Closed: `verify_phase6.py --write --slate X` reconciling and overwriting **every** slate (`idempotency_gate` called the unscoped `reconcile`, so the unrequested write to all 412 slates landed before the run exited 1, with no FAIL line naming it) · a hardcoded `since="2025-10-01"` that would have silently turned every row of next season's first slate into `no_ops_game` · a `TypeError` crash in `print_report`/`print_write_preview` on a nullable value, i.e. a traceback replacing the whole report on the path the write decision is made from · `off_slate_sides` reading a half-NULL side as off-slate · a `_pristine_dk_value` docstring that overclaimed (a hand-fix landing *exactly* on the ops value keeps the stale `dk_value` forever, undetectably) · and `print_write_preview` never wired into the module CLI.
  - **Three lessons worth more than the fixes.** (1) **A green gate is not a green codebase** — 27/27 says nothing about argument combinations, constants, or formatters, so don't let a passing gate stand in for reading the code. (2) **Test both sides of a flag.** `--slate` was tested only on the report-only path, which is exactly why the write-path bug was invisible; a flag with two modes needs two tests. (3) **An edit can silently fail to apply.** The `print_write_preview` wiring *was* written and didn't land — nothing caught it because nothing exercised that CLI's output. After a scripted or bulk edit, grep for the new call site rather than trusting the tool's success.

**Side quest (done): lineups filename reconciliation**
- The optimizer named `ranked-lineups-*` files from run time, not slate date. `scripts/match_lineups_to_slates.py` matches each file to its true slate by DK ID set intersection (DK ID blocks are disjoint across slates — verified, 0 collisions across 409 slates). 219/226 matched at 100% coverage; 26 dates and 5 slate types corrected. Write-up in `data/lineups_slate_match/README.md` (gitignored); corrected copies in `relabeled/`.
- 7 files are unmatchable: no slate CSV exists for Feb 13–22, 2026. Parked in `unmatched/`.

**Phase 6 — shipped 2026-09-07.** Spec, survey and *As built*: **`docs/phase6-ops-reconciliation.md`** (tracked). The survey below is the 2026-08-09 read-only pass that justified the phase; §5 of the spec records where the build departed from it.

- **Approved decisions:** overwrite `slate_players.actual_fpts` in place from ops + a full-census `fpts_audit` table · game-level join with ±1-day resolution, no nulling · overwrite the 209 off-slate rows but tag them `off_slate` · this doc is the spec, `docs/ingestion-plan.md` gets its Phase 6 section when the phase is built.
- **The survey (2026-08-09, read-only, nothing written):** 31,730 rows land on an ops log by exact date and **30,800 agree (97.1%)**. The 930 that don't split cleanly by cause — **680** float noise in the DK CSV (`25.746666666666663` against ops' `25.75`; ops is quarter-granular, the CSV isn't) · **109** off-slate · **61** DK settling a player at 0 who played · **80** stat corrections, every delta a legal DK quantum (±2.0 a steal/block, ±1.25 a rebound, ±1.5 an assist) **cancelling within a game**. That last class is the case for the phase: **DK's file is frozen at settlement, ops is post-correction.**
- **New class found 2026-08-09 — `dk_unscored`, 61 rows / 18 players.** DK's file has them at exactly `0`; ops has a full box score with real minutes (0.9–31.0). Moussa Cisse 0 vs 33.75, Jase Richardson 0 vs 28.75. Same shape as an off-slate game but at **player** grain, so `check_zero_scored_games` can't see it and the Phase 4 backfill never flagged it. Fringe and two-way names, recurring across slates — looks like late roster additions DK never scored, but **the cause is not established**. Doesn't change the design (ops wins either way); it gets its own `reason` so it stays separable.
- **The off-slate games were played** — this inverts the parked assumption. 5 of the 6 have full ops box scores (2025-10-26 LAC/POR has Harden 52.0, Kawhi 55.0 against analytics' 0.0), so the repair is overwrite, not null. Only **2026-01-25 DAL/MIL (36 rows)** is absent from ops on every 2026 date; Jonny is investigating that one separately.
- **Built as specified except for three things**, all in the spec's §5: `off_slate` became a **column** rather than a `reason` value; `date_shift` marks the 232 rows that *took* a shifted value rather than 214 corrections (the shift repairs `game_date`, not the value); and the two-day-slate finding is **10 slates / 20 game-sides**, not one. The gate shipped with **27** checks, not 14.
- **Still open:** 2026-01-25 DAL/MIL (36 rows, `no_ops_game`) — Jonny investigating · `dk_unscored`'s cause still not established · orchestrator wiring still deferred.
- Small follow-up: warnings are logged twice on the write path — once by `ingest_*` (keyed by filename) and once by `ingest_slate` (keyed by source), so `--backfill` prints each one as a pair. Cosmetic only; the counted tally is right.

**Local working notes** (gitignored via `docs/*.local.md`, so a fresh session sees them on disk but not in git):
- `docs/lineups-filename-discovery.local.md` — the plan for cutting lineups discovery over to filenames once the optimizer emits correctly-named files. Parked until that folder is ready.
- `docs/actual-fpts-zero-games.local.md` — the off-slate-game finding in full. Its repair suggestion ("null the 208 values, or overwrite…") is settled: **overwrite** — see the amendment in Decisions.

**Decisions / notes** — the *why*, where the code alone doesn't carry it.
- **`off_slate` is a column on `fpts_audit`, not a `reason` value** (2026-09-07, Jonny). `reason` holds one primary cause per row, and the spec asked it to carry two orthogonal facts at once: D3 wants all **209** off-slate rows tagged, while §1.3 wants the 36 DAL/MIL rows reading `no_ops_game`. A third collision turned up in the build — **Alex Toohey** (GSW, `2025-10-28`) is inside an off-slate game *and* one of the 11 uncrosswalked names, so his row wants `no_crosswalk` too. That single row is why a `reason`-only detector counts 208 where D3 says 209. Off-slate is a property of the **game**; the cause is a property of the **row**. So the flag carries the population (`WHERE off_slate = 1`, 209 rows, 4 actions) and `reason` keeps the narrower cause (172 `off_slate` + 36 `no_ops_game` + 1 `no_crosswalk`). Precedence: `no_crosswalk` → `no_ops_game` → `off_slate` → value cause / `date_shift` / `dnp`. `off_slate` sits *above* the value causes so the 109 corrections on games DK never scored don't collapse into `dk_unscored` — true of them, but it would hide the game-level fact and make those corrections unfindable by reason.
- **The off-slate detector must read the *pristine* DK values, or it eats itself on the second pass** (2026-09-07, found by a test). The signature is "every rostered player on both sides of a matchup at exactly 0" — and `apply_corrections` overwrites exactly those zeros. Sourced from the live `actual_fpts`, the detector finds the six games once and **never again**: `off_slate` silently drops from all 209 rows, and with it the only thing that makes them excludable from a backtest. Nothing else catches it — the second run passes every other check, because every other check is consistent with the corrupted state. So `reconcile()` reconstructs every row's pristine `dk_value` **first**, then derives the off-slate games from those. `off_slate_sides()` takes values as an argument rather than a connection so it *cannot* read the corrupted column; `off_slate_games(conn)` survives as the pre-write probe only. This is the §3.4 `dk_value` problem one level up — a reconciliation corrupting the signal it was computed from — so assume any other derived-from-`actual_fpts` signal has it too. Pinned by `test_the_off_slate_flag_survives_a_second_pass`.
- **`date_shift` marks where a value came *from*, not that it changed** (2026-09-07, Jonny). The spec pinned it as 214 `corrected` rows; it is 232 `unchanged` ones. Those rows *already agree* with ops once the right date is used — the ±1 resolution repairs `game_date`, not the value, and **zero** corrections live on a shifted game. The 20 shifted game-sides hold 359 rows: 232 that take a value from the shifted date, 122 DNPs (`dnp`; their shift is still visible in `game_date`), and 5 uncrosswalked, which is why the gate reports **354** rows on a shifted `game_date` rather than 359. Related: the two-day-slate finding is bigger than §1.4 recorded — **10 slate dates, 20 game-sides** (`2026-05-12`, `05-17`, and every slate `05-18`→`05-25`), because the conference finals ran two series on alternating nights and DK listed **both** on every file. And **+1 must be tried before −1**: on the `05-21` slate OKC/SAS exists in ops at both 05-20 and 05-22, and only a game yet to tip off can be on a slate.
- **`dk_id` is per-slate, not per-player** (found 2026-08-09). All 51,971 `slate_players` rows carry a distinct `dk_id` but there are only **610 distinct names** — DK re-issues an id every slate. So the crosswalk *matches* over names (610 human-sized decisions) and *writes* at the `dk_id` grain (51,971 rows), which is what makes `dk_crosswalk` joinable straight from `slate_players` as its pinned DDL intends. Any future "one row per player" instinct about that table is wrong.
- **Only the two deterministic tiers auto-write; fuzzy always goes to Jonny.** `EXACT` (identical source strings) and `NORMALIZED` (identical after `normalize_name`) write unattended. Everything else is a *proposal* — `NameMatch.player_id` stays `None` until approved, so nothing downstream can mistake one for a decision. The gap on the current data is wide (true 0.93/0.80 vs best false 0.73), but it's a gap between two handfuls of names, not a law: `RJ Davis`/`Ed Davis` and `Cameron Matthews`/`Wesley Matthews` are the pairs a threshold eventually gets wrong, and a bad crosswalk row silently mis-attributes *every* box score for that player. Cheap to review, expensive to be wrong.
- **`score_candidate` blends three signals because ratio alone provably can't do it.** Character ratio ranks the false `RJ Davis`→`JD Davison` (0.78) *above* the true `Yanic Niederhauser`→`Yanic Konan Niederhauser` (0.86 but with far more moved characters). Adding token containment + a surname check separates them. The ratio is also taken over **sorted tokens**, whichever is kinder — that is what catches `Hansen Yang` → ops `Yang Hansen`, which read left-to-right is only 0.57 similar, i.e. *below the review floor and invisible*. Reversed given/family order is a standing feature of NBA rosters, not a one-off; it was caught only because the report prints the nearest candidate for unmatched names too.
- **The `REVIEW_FLOOR` (0.60) hides nothing.** Names below it are reported as unmatched *with their nearest candidate and score*, precisely so a real match under the floor stays visible. That property is what surfaced Hansen Yang before the token-sort fix existed.
- **Ambiguity is refused, never resolved.** Two ops players collapsing to one normalized key ⇒ `Tier.AMBIGUOUS`, all candidates shown, nothing written. Zero exist in the current snapshot (1181 ops names → 1181 keys; 610 DK names → 610 keys) — the guard is for the next snapshot. An identical *raw* string still wins over a normalized collision, since that's unambiguous evidence.
- **A `dk_id` claimed by two `player_id`s is refused, not resolved** (PR #10 review). `slate_players` is keyed `(slate_id, dk_id)`, so the schema permits one `dk_id` under two names, and `load_slate_names` groups by name — two names, two `NameMatch`es, both fanning out to that id. The upsert would have applied whichever `executemany` reached last, silently. `write_crosswalk` now raises. The gate check that looked like it covered this (`dk_crosswalk GROUP BY dk_id HAVING COUNT(*) > 1`) was a **tautology** — `dk_id` is that table's PRIMARY KEY, so it returns 0 however the write went; it now asks the source instead (`slate_players … HAVING COUNT(DISTINCT TRIM(name)) > 1`). Zero on today's data, but the Yanic rename proves two-spellings is real. Note the direction: two *names* for one player is legitimate and still allowed (see below); it is two *players* for one id that's refused.
- **`write_crosswalk` upserts; it does not delete-then-insert like `load_slate`.** Approvals arrive in batches over time, so rebuilding from one batch would silently drop mappings approved in an earlier one. `--rebuild` is the explicit way to clear. Re-approving a name corrects it in place.
- **The approvals live in git (`docs/crosswalk-approvals.csv`), not in `data/`.** They are the **only non-reproducible input in the project** — everything else rebuilds from source CSVs and code. `data/` is gitignored and the docs actively tell you to delete `analytics.db` and re-ingest, which would silently rebuild the crosswalk for 597 names and drop the 2 approved ones. Keeping the ticked CSV tracked makes the whole crosswalk reproducible with no human in the loop, and makes the one genuinely human decision reviewable in a PR.
- **One ops player can legitimately have two DK spellings; the reverse must never happen.** DK renamed Yanic Niederhauser mid-season — `Yanic Niederhauser` for the first 3 slates (2025-10-22 … 10-28), then `Yanic Konan Niederhauser` for the next 97 (from 2025-10-31). Both now map to ops 1642949, which is why `dk_crosswalk` holds 599 names but 598 distinct `player_id`s. That direction is correct and expected: it stitches the October slates onto the rest of his season instead of orphaning them. The gate enforces the *other* direction — "each name maps to one `player_id` across all its `dk_ids`" — because one DK name resolving to two players would be the actual corruption. **Had the approval been rejected, those 3 slates would have been silently lost from his box-score joins.**
- **The 11 unmatched names are genuinely absent from ops**, not a matcher failure — verified 2026-08-09 by searching `dim_players` for each surname (zero hits) and confirming ops holds **no non-ASCII names at all**, so nothing is hiding behind an accent fold. They're rookies/two-ways with no logged games in the snapshot. `unmatched_report()` is the standing monitor: re-run after each new slate and a newly-arrived player shows up as a line.
- **Off-slate games are warned about, never dropped.** Some games carry `Actual_FPTs = 0` on **every** player of **both** sides. Confirmed with Jonny 2026-07-28: they tipped off outside the slate's window (an odd-hour start, e.g. 3:30pm Sunday when everything else began at 6), so the game was never in that contest. Null-based validation cannot see this — the cells hold `0`, not blank, and there is not one blank `Actual_FPTs` cell in all 409 files — so it lands in `actual_fpts` as a real `0.0`, indistinguishable from a DNP. The discriminator must be the whole game, not the value: single zeros are normal and common, so a value-keyed rule would destroy real data. Warning, not error; the rows load, and exclusion is the ops-reconciliation pass's call. Full reasoning in `ingest/salary.py:check_zero_scored_games`. **Amended 2026-08-09:** 5 of the 6 games turn out to have full ops box scores — they *were* played, DK just didn't score them into that contest — so Phase 6 overwrites rather than nulls. Only 2026-01-25 DAL/MIL has no ops row on any date. Note this is the one place ops-as-truth and DK-contest-reality genuinely conflict: DK's `0` was correct *contest* semantics (the player was unscoreable in that window), the box score is correct *player* semantics. Overwriting is the approved call; the `off_slate` audit tag is what keeps it excludable from a backtest.
- **DK ran a two-calendar-day slate in the conference finals** (2026-08-09, Jonny). `Main-2026-05-17.csv` carries both CLE/DET *and* SAS/OKC, but ops dates CLE@DET to 05-17 and SAS@OKC to 05-18. Neither source is wrong — it is a real two-day slate, not a date bug in either DB. 214 non-zero rows sit on the far side of it, and every one is rescued by ops date **+1 with the value matching exactly** (Wembanyama 84.0 on the slate = 84.0 in ops the next day). So the ops join must resolve a **game date**, not assume the slate date.
- **That ±1 resolution must happen at game grain, never per player.** 907 of the 19,790 zero-valued rows *also* have an ops log at +1 day — players who simply played the next night. A per-player ±1 window would write the wrong game's score onto all 907, which is the same silent mis-attribution class the crosswalk refusals exist to prevent. Resolve the whole `(date, team, opp)` matchup first, then apply it to that game's players. Same-day always wins, so a team playing both nights is never ambiguous. `ops.map_teams` makes this viable: all 30 teams, zero unmapped values in either direction, and 2,284 of 2,306 slate game-sides match ops same-day.
- **DNP zeros stay `0`; Phase 6 introduces no NULLs.** 19,790 crosswalked rows have no ops log because the player didn't play — DK scores an unused roster slot as `0`, and nulling those would break lineup scoring. `actual_fpts` is currently 100% non-null and stays that way, so no downstream aggregate needs a NULL check.
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

## Six tables

| Table | Grain | Source |
|---|---|---|
| `slate_players` | `(slate_id, dk_id)` | salary CSV |
| `projections` | `(slate_id, dk_id)` | projections CSV |
| `lineups` | `(slate_id, final_rank)` | lineups CSV (header rows) — `relabeled/`, keep-latest by manifest |
| `lineup_players` | `(slate_id, final_rank, slot)` | lineups CSV (melted slots) |
| `dk_crosswalk` | `dk_id` | built after the backfill, from ops DB match |
| `fpts_audit` | `(slate_id, dk_id)` | Phase 6 — a **full census** of `slate_players`, not a change log |

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

_Ops reconciliation was listed here through Phase 5, came **into scope** as Phase 6, and **shipped 2026-09-07** — see `docs/phase6-ops-reconciliation.md`._
