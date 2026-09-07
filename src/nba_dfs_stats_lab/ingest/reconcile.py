"""`slate_players.actual_fpts` reconciled against the ops box scores.

DraftKings' salary CSV is frozen at contest settlement; ops is post-correction.
Where they disagree, ops wins — that is the whole case for the phase, and the
80 `stat_correction` rows are the proof: every delta is a legal DK scoring
quantum (±2.00 a steal or block, ±1.25 a rebound, ±1.50 an assist) and they
**cancel within a game**, i.e. a stat credited to the wrong player at settlement
and moved afterwards. Full survey in `docs/phase6-ops-reconciliation.md`.

Three things shape this module, and each one is a way to get it silently wrong:

1. **The join resolves a game date, not the slate date.** DK ran genuine
   two-calendar-day slates through the conference finals: two series playing on
   alternating nights, both listed on every slate file. `2026-05-18_classic_main`
   carries OKC/SAS *and* CLE/NYK; ops dates the first to 05-18 and the second to
   05-19. Ten slates are like this (2026-05-12, 05-17, and every slate from
   05-18 through 05-25), 20 game-sides in all. Neither source is wrong.

2. **The ±1 shift is established per *matchup*, never per player.** 907 of the
   19,790 zero-valued rows also have an ops log at +1 day — players who simply
   played the next night. A blanket per-player ±1 window would write the wrong
   game's score onto all 907. So `resolve_game_dates` settles a whole
   `(date, team, opp)` first and `reconcile` applies that one answer to every
   player in it. Same-day always wins, so a team playing both nights is never
   ambiguous, and +1 is tried before -1: on the 05-21 slate OKC/SAS exists in ops
   at both 05-20 and 05-22, and only the later one could still be on a slate.

3. **DNP zeros stay 0 and no NULL is ever written.** DK scores an unused roster
   slot as 0. 19,736 crosswalked rows have no ops log because the player didn't
   play, and nulling those would break lineup scoring. `actual_fpts` is 100%
   non-null before this phase and stays that way after it.

`reconcile()` is pure — it reads both databases and returns a report. Nothing
reaches the DB without `write_audit` / `apply_corrections`, the same posture as
`crosswalk.py` where reporting is the default and writing is opt-in.
"""

import argparse
import datetime as dt
import logging
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from nba_dfs_stats_lab.db.connection import attach_ops, get_connection
from nba_dfs_stats_lab.db.schema import SchemaMigrationError, init_db

logger = logging.getLogger(__name__)

# Day offsets tried, in order. Same-day first so a team playing on both `d` and
# `d+1` is never ambiguous; +1 before -1 because a slate can only contain a game
# that has yet to tip off — see the module docstring.
SHIFTS = (0, 1, -1)

# Any real difference is a correction. Deliberately not a tolerance: the 680
# `float_noise` rows differ by ~0.00333 and are exactly the rows the phase is
# meant to clean up (DK points are quarter-granular by construction; ops stores
# 25.75 where the CSV carries 25.746666666666663). On today's data the 30,800
# agreeing rows agree *exactly*, so this epsilon separates nothing — it is here
# so a future float round-trip through the CSV doesn't register as 51,971
# corrections.
CHANGE_EPSILON = 1e-9

# Below this, a disagreement is DK's float representation rather than a scoring
# change. The nearest real DK quantum is 0.25, so the gap is two orders wide.
FLOAT_NOISE_LIMIT = 0.01

# `action` — what happened to the row. Exactly four, and every row gets one.
ACTIONS = ("corrected", "unchanged", "no_ops_row", "unmapped")

# `reason` — why, as a single primary cause. Orthogonal to `action`.
REASONS = (
    "float_noise",  # DK's CSV float vs ops' quarter-granular value
    "stat_correction",  # a real post-settlement scoring change
    "dk_unscored",  # DK settled a player at 0 who played real minutes
    "off_slate",  # the whole game was at 0 in the DK file
    "date_shift",  # the value came from a resolved date != the slate date
    "dnp",         # crosswalked, game found, no ops log — didn't play
    "no_ops_game",  # the matchup is absent from ops on every date tried
    "no_crosswalk",  # no dk_crosswalk row, so ops is unreachable
)


class ReconcileError(Exception):
    """The reconcile pass can't run or can't be applied as computed."""


def _shift(date: str, days: int) -> str:
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


# --- Reading the ops side ------------------------------------------------------


@dataclass(frozen=True)
class OpsGame:
    """One side of one ops game: `team` hosting/visiting `opp` on `date`."""

    date: str
    team: str
    opp: str
    player_count: int


@dataclass(frozen=True)
class GameLink:
    """A slate game-side resolved onto an ops date, or not.

    `game_date is None` means the matchup is absent from ops on the slate date
    and both neighbours — 2026-01-25 DAL/MIL is the only one today.
    """

    slate_date: str
    team: str
    opp: str
    game_date: str | None
    shift_days: int | None

    @property
    def shifted(self) -> bool:
        return self.game_date is not None and self.game_date != self.slate_date


def _check_alias(alias: str) -> None:
    """`alias` is interpolated into SQL, so it is validated at every entry point.

    Not redundant with `attach_ops`: these functions take their own alias
    parameter, and a caller can pass a different string than the one that was
    attached.
    """
    if not alias.isidentifier():
        raise ValueError(f"invalid attach alias: {alias!r}")


def build_game_index(
    conn: sqlite3.Connection, alias: str = "ops", since: str = "2025-10-01"
) -> dict[tuple[str, str, str], OpsGame]:
    """Every ops game-side as `(date, team, opp) -> OpsGame`, team abbreviations.

    `ops.map_teams` translates ops' raw team names to the abbreviations
    `slate_players` uses. All 30 teams are in it with zero unmapped values in
    either direction, but a row whose team is missing from the map is dropped
    here rather than guessed at — the gate counts what the index covers, so a
    future gap shows up as unresolved game-sides instead of as silence.

    `since` bounds the scan to the current season; ops holds 190k rows over many
    seasons and only 2025-10-01 onward can be reached from a slate date.
    """
    _check_alias(alias)
    rows = conn.execute(
        f"""
        SELECT f.DATE, mt.TEAM_ABBREVIATION, mo.TEAM_ABBREVIATION, COUNT(*)
          FROM {alias}.fantasy_logs f
          JOIN {alias}.map_teams mt ON mt.RAW_TEAM_NAME = f.TEAM
          JOIN {alias}.map_teams mo ON mo.RAW_TEAM_NAME = f.OPPONENT
         WHERE f.DATE >= ?
         GROUP BY f.DATE, mt.TEAM_ABBREVIATION, mo.TEAM_ABBREVIATION
        """,  # noqa: S608 — alias checked above
        (since,),
    ).fetchall()
    return {(d, t, o): OpsGame(d, t, o, n) for d, t, o, n in rows}


def load_ops_points(
    conn: sqlite3.Connection, alias: str = "ops", since: str = "2025-10-01"
) -> dict[tuple[int, str], float]:
    """`(player_id, date) -> DK_POINTS` for the current season.

    Ops has zero NULL `DK_POINTS`, so a missing key means "no log for that
    player on that date" and never "logged but unscored" — which is what lets
    `reconcile` read absence as a DNP rather than as missing data.
    """
    _check_alias(alias)
    rows = conn.execute(
        f"SELECT PLAYER_ID, DATE, DK_POINTS FROM {alias}.fantasy_logs "  # noqa: S608 — alias checked above
        "WHERE DATE >= ? AND DK_POINTS IS NOT NULL",
        (since,),
    ).fetchall()
    return {(int(pid), date): float(pts) for pid, date, pts in rows}


# --- Reading the analytics side ------------------------------------------------


def slate_game_sides(
    conn: sqlite3.Connection, slate_ids: Sequence[str] | None = None
) -> set[tuple[str, str, str]]:
    """Distinct `(slate date, team, opp)` in `slate_players`. 2,306 today."""
    where, params = _slate_filter(slate_ids)
    rows = conn.execute(
        "SELECT DISTINCT substr(slate_id, 1, 10), TRIM(team), TRIM(opp) "
        f"FROM slate_players WHERE {where} "  # noqa: S608 — placeholders only
        "AND team IS NOT NULL AND TRIM(team) <> '' "
        "AND opp IS NOT NULL AND TRIM(opp) <> ''",
        params,
    ).fetchall()
    return {(d, t, o) for d, t, o in rows}


def resolve_game_dates(
    index: dict[tuple[str, str, str], OpsGame], sides: Iterable[tuple[str, str, str]]
) -> dict[tuple[str, str, str], GameLink]:
    """Settle each slate game-side onto an ops date: exact, then +1, then -1.

    This is the whole defence against the 907-row trap. The answer is computed
    once per *matchup* and `reconcile` then applies it to every player in that
    matchup, so a player who merely happened to play the next night can never
    pull his own row onto a different date than his team's.
    """
    links: dict[tuple[str, str, str], GameLink] = {}
    for slate_date, team, opp in sides:
        for offset in SHIFTS:
            candidate = _shift(slate_date, offset)
            if (candidate, team, opp) in index:
                links[slate_date, team, opp] = GameLink(
                    slate_date, team, opp, candidate, offset
                )
                break
        else:
            links[slate_date, team, opp] = GameLink(slate_date, team, opp, None, None)
    return links


def off_slate_sides(
    rows: Iterable[tuple[str, str | None, str | None, float | None]],
) -> set[tuple[str, str, str]]:
    """`(slate_id, team, opp)` sides where BOTH teams scored exactly 0.

    Takes `(slate_id, team, opp, value)` tuples rather than a connection because
    of an idempotency trap this used to fall into: the signature is "every
    rostered player on both sides at exactly 0", and `apply_corrections`
    overwrites precisely those zeros. Read from the live `actual_fpts`, the
    detector therefore finds the games on the first pass and *nothing* on the
    second — silently dropping the `off_slate` flag from all 209 rows, which is
    the one thing that keeps them excludable from a backtest (D3). So
    `reconcile` feeds it the **pristine** DK values, the same ones
    `_pristine_dk_value` reconstructs.

    The same discriminator `ingest.salary.check_zero_scored_games` uses, and for
    the same reason: single zeros are normal and common, so nothing keyed on the
    value alone can find these without destroying real data. Both sides of one
    matchup scoring nothing is what cannot happen in a played game.

    The pairing must reciprocate — a third zeroed team merely *naming* one of
    these would otherwise be counted into a game that never existed.

    Six games, 209 rows today. Five of them turn out to have full ops box scores
    (2025-10-26 LAC/POR has Harden at 52.0 against analytics' 0.0): they were
    played, DK just never scored them into that contest.
    """
    peak: dict[tuple[str, str, str], float] = {}
    for slate_id, team, opp, value in rows:
        if not team or not opp or value is None:
            continue
        key = (slate_id, team, opp)
        peak[key] = max(peak.get(key, 0.0), abs(value))
    return {
        (slate_id, team, opp)
        for (slate_id, team, opp), mx in peak.items()
        if mx == 0 and peak.get((slate_id, opp, team)) == 0
    }


def off_slate_games(
    conn: sqlite3.Connection, slate_ids: Sequence[str] | None = None
) -> set[tuple[str, str, str]]:
    """`off_slate_sides` over the live `slate_players` values.

    Correct only *before* `apply_corrections` has run — see the trap in
    `off_slate_sides`. `reconcile` does not use this; it is here for probing a
    freshly-ingested DB and for the tests that pin the detector itself.
    """
    where, params = _slate_filter(slate_ids)
    rows = conn.execute(
        "SELECT slate_id, TRIM(team), TRIM(opp), actual_fpts FROM slate_players "
        f"WHERE {where}",  # noqa: S608 — placeholders only
        params,
    ).fetchall()
    return off_slate_sides(rows)


def _slate_filter(slate_ids: Sequence[str] | None) -> tuple[str, tuple]:
    """A `WHERE` fragment scoping a query to `slate_ids`, or to everything."""
    if slate_ids is None:
        return "1 = 1", ()
    if not slate_ids:
        # An empty *sequence* is a caller bug: `None` means "all slates", and
        # `[]` silently reconciling all of them is the same class of accident
        # `load_slate` refuses for an empty frame.
        raise ReconcileError("slate_ids is empty — pass None to reconcile every slate")
    return f"slate_id IN ({', '.join('?' * len(slate_ids))})", tuple(slate_ids)


def load_prior_audit(
    conn: sqlite3.Connection, slate_ids: Sequence[str] | None = None
) -> dict[tuple[str, int], tuple[float | None, float | None]]:
    """`(slate_id, dk_id) -> (dk_value, ops_value)` from a previous run."""
    where, params = _slate_filter(slate_ids)
    rows = conn.execute(
        f"SELECT slate_id, dk_id, dk_value, ops_value FROM fpts_audit WHERE {where}",  # noqa: S608 — placeholders only
        params,
    ).fetchall()
    return {(s, int(d)): (v, o) for s, d, v, o in rows}


# --- The reconciliation --------------------------------------------------------


@dataclass(frozen=True)
class AuditRow:
    """One `slate_players` row's verdict. The census is one of these per row."""

    slate_id: str
    dk_id: int
    player_id: int | None
    game_date: str | None
    dk_value: float | None
    ops_value: float | None
    action: str
    reason: str | None
    off_slate: bool
    # Not persisted — `fpts_audit` is keyed on (slate_id, dk_id) and these three
    # live in `slate_players`. `name` is carried so the report can name the
    # players Jonny is being asked to look at rather than list bare DK ids;
    # `team`/`opp` so the gate can re-derive each row's matchup and check that
    # a shift was applied to the whole of it (the 907-row trap) from the rows
    # themselves rather than from the resolver that produced them.
    name: str | None = None
    team: str | None = None
    opp: str | None = None

    @property
    def matchup(self) -> tuple[str, str, str] | None:
        """`(slate_id, team, opp)` — None when the row carries no matchup."""
        if not self.team or not self.opp:
            return None
        return (self.slate_id, self.team, self.opp)

    @property
    def delta(self) -> float | None:
        if self.dk_value is None or self.ops_value is None:
            return None
        return self.ops_value - self.dk_value

    @property
    def slate_date(self) -> str:
        return self.slate_id[:10]

    @property
    def shifted(self) -> bool:
        return self.game_date is not None and self.game_date != self.slate_date


@dataclass
class ReconcileReport:
    """Every `slate_players` row, classified. Computed; nothing written."""

    rows: list[AuditRow] = field(default_factory=list)
    links: dict[tuple[str, str, str], GameLink] = field(default_factory=dict)
    ops_game_count: int = 0

    @property
    def corrections(self) -> list[AuditRow]:
        return [r for r in self.rows if r.action == "corrected"]

    def action_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(ACTIONS, 0)
        for row in self.rows:
            counts[row.action] = counts.get(row.action, 0) + 1
        return counts

    def reason_counts(self) -> dict[str | None, int]:
        return Counter(row.reason for row in self.rows)

    def off_slate_rows(self) -> list[AuditRow]:
        return [r for r in self.rows if r.off_slate]

    def shifted_rows(self) -> list[AuditRow]:
        return [r for r in self.rows if r.shifted]

    def by_reason(self, reason: str) -> list[AuditRow]:
        return [r for r in self.rows if r.reason == reason]

    def split_matchups(self) -> list[tuple[tuple[str, str, str], list[str]]]:
        """Matchups whose rows did not all land on a single `game_date`.

        Empty by construction — `resolve_game_dates` answers once per matchup —
        but asked of the produced rows rather than of the resolver, so a future
        refactor that reintroduces per-player date logic shows up here. That is
        the 907-row trap: 907 zero-valued rows have an ops log at +1 day for a
        game their team did not play, and a per-player window would pull each of
        them onto its own date.
        """
        dates: dict[tuple[str, str, str], set[str]] = {}
        for row in self.rows:
            key = row.matchup
            if key is None or row.game_date is None:
                continue
            dates.setdefault(key, set()).add(row.game_date)
        return sorted(
            (key, sorted(seen)) for key, seen in dates.items() if len(seen) > 1
        )

    def unresolved_sides(self) -> list[GameLink]:
        return sorted(
            (link for link in self.links.values() if link.game_date is None),
            key=lambda link: (link.slate_date, link.team),
        )


def _pristine_dk_value(
    current: float | None, prior: tuple[float | None, float | None] | None
) -> float | None:
    """The value DraftKings shipped, across re-runs of this pass.

    Reading `slate_players.actual_fpts` back after a correction would capture the
    *ops* value and destroy the record of what DK said — and `load_slate` is
    delete-then-insert, so re-ingesting a slate silently reverts its
    reconciliation. Those two facts pull in opposite directions, and this is the
    rule that satisfies both:

    reuse the prior audit row's `dk_value` when the correction is still in place
    (i.e. `actual_fpts` still equals the `ops_value` we wrote), otherwise take
    the current value as the new pristine one. A re-ingested slate, or a source
    CSV Jonny has fixed by hand, lands in the second branch and re-pins itself
    with no manual reset.

    This is also why `write_audit` runs *before* `apply_corrections`: a crash
    between them leaves an audit row whose `ops_value` differs from the
    untouched `actual_fpts`, which is exactly the second branch, so the next run
    still sees the true DK value. The other order would lose it.
    """
    if prior is None:
        return current
    prior_dk, prior_ops = prior
    if prior_ops is not None and current is not None and current == prior_ops:
        return prior_dk
    return current


def _classify(
    dk_value: float | None,
    ops_value: float | None,
    link: GameLink | None,
    is_off_slate: bool,
) -> tuple[str, str | None]:
    """`(action, reason)` for one crosswalked row. See REASONS for the vocabulary.

    Reason precedence, and why it runs in this order:

    - `no_ops_game` first, because a matchup missing from ops is a statement
      about the data rather than about the player, and 2026-01-25 DAL/MIL is
      still under investigation.
    - `off_slate` next, so the 109 corrections on games DK never scored keep a
      cause of their own instead of collapsing into `dk_unscored` — which is
      true of them but hides the game-level fact. The `off_slate` *column*
      carries the full 209-row population regardless; the reason carries only
      the rows where it is the primary cause.
    - then the value causes, and `date_shift` for a row whose value came from a
      resolved date other than the slate date. A shifted row that also needs a
      correction takes the value cause, since that is the actionable half and
      `game_date` still records the shift; today there are none.
    """
    if ops_value is None:
        if link is not None and link.game_date is None:
            return "no_ops_row", "no_ops_game"
        return "no_ops_row", "off_slate" if is_off_slate else "dnp"

    shifted = link is not None and link.shifted
    if dk_value is None or abs(ops_value - dk_value) > CHANGE_EPSILON:
        if is_off_slate:
            return "corrected", "off_slate"
        if dk_value is None:
            return "corrected", "stat_correction"
        if abs(ops_value - dk_value) < FLOAT_NOISE_LIMIT:
            return "corrected", "float_noise"
        if dk_value == 0:
            return "corrected", "dk_unscored"
        return "corrected", "stat_correction"

    if is_off_slate:
        return "unchanged", "off_slate"
    return "unchanged", "date_shift" if shifted else None


def reconcile(
    conn: sqlite3.Connection,
    slate_ids: Sequence[str] | None = None,
    alias: str = "ops",
) -> ReconcileReport:
    """Classify every `slate_players` row against ops. Writes nothing.

    Rows with a NULL or blank `team`/`opp` fall back to the exact
    `(player_id, slate_date)` lookup — there is no game to resolve, so no shift
    is possible for them. None exist today (Jonny's fix to the 2026-02-02
    Charlotte rows removed the last of them), but the salary contract permits a
    blank team with a warning, so the path stays.
    """
    index = build_game_index(conn, alias=alias)
    links = resolve_game_dates(index, slate_game_sides(conn, slate_ids))
    points = load_ops_points(conn, alias=alias)
    prior = load_prior_audit(conn, slate_ids)

    crosswalk = {
        int(dk_id): int(player_id)
        for dk_id, player_id in conn.execute("SELECT dk_id, player_id FROM dk_crosswalk")
    }

    where, params = _slate_filter(slate_ids)
    source = conn.execute(
        "SELECT slate_id, dk_id, TRIM(team), TRIM(opp), actual_fpts, TRIM(name) "
        f"FROM slate_players WHERE {where} ORDER BY slate_id, dk_id",  # noqa: S608 — placeholders only
        params,
    ).fetchall()

    # Two passes. The pristine DK values have to exist before off-slate games
    # can be found, because the signature is "both sides at exactly 0" and a
    # previous pass has already overwritten those zeros — see `off_slate_sides`.
    pristine = {
        (slate_id, int(dk_id)): _pristine_dk_value(actual, prior.get((slate_id, int(dk_id))))
        for slate_id, dk_id, _team, _opp, actual, _name in source
    }
    off_slate = off_slate_sides(
        (slate_id, team, opp, pristine[slate_id, int(dk_id)])
        for slate_id, dk_id, team, opp, _actual, _name in source
    )

    rows: list[AuditRow] = []
    for slate_id, dk_id, team, opp, _actual, name in source:
        dk_id = int(dk_id)
        dk_value = pristine[slate_id, dk_id]
        is_off_slate = (slate_id, team, opp) in off_slate
        player_id = crosswalk.get(dk_id)

        if player_id is None:
            # No crosswalk row means no player_id, so ops is unreachable: the
            # row cannot be corrected however much evidence exists elsewhere.
            # All 237 already hold 0.0, consistent with Phase 5's finding that
            # these 11 names never logged an NBA second.
            rows.append(
                AuditRow(
                    slate_id, dk_id, None, None, dk_value, None,
                    "unmapped", "no_crosswalk", is_off_slate, name, team, opp,
                )
            )
            continue

        # No team/opp means no matchup to resolve, so the slate date is used as
        # written and no shift is possible — `link` stays None and `_classify`
        # reads that as "the game was found, the player has no log", i.e. a DNP.
        link = links.get((slate_id[:10], team, opp)) if team and opp else None
        game_date = link.game_date if link is not None else slate_id[:10]
        ops_value = points.get((player_id, game_date)) if game_date else None
        action, reason = _classify(dk_value, ops_value, link, is_off_slate)
        rows.append(
            AuditRow(
                slate_id, dk_id, player_id, game_date, dk_value, ops_value,
                action, reason, is_off_slate, name, team, opp,
            )
        )

    return ReconcileReport(rows=rows, links=links, ops_game_count=len(index))


# --- Writing -------------------------------------------------------------------


def write_audit(conn: sqlite3.Connection, rows: Sequence[AuditRow]) -> int:
    """Replace the audit census for every slate present in `rows`. Atomic.

    Delete-then-insert per slate, like `load_slate` and unlike
    `write_crosswalk`: this table is a *census*, so a slate that loses players
    on re-ingest must lose their audit rows too, or `COUNT(fpts_audit) ==
    COUNT(slate_players)` — the gate check that makes the census self-verifying
    — would start failing on stale rows.

    Call this **before** `apply_corrections`; see `_pristine_dk_value`.
    """
    if not rows:
        return 0
    slates = sorted({row.slate_id for row in rows})
    payload = [
        (
            row.slate_id, row.dk_id, row.player_id, row.game_date,
            row.dk_value, row.ops_value, row.delta,
            row.action, row.reason, int(row.off_slate),
        )
        for row in rows
    ]
    unknown = sorted({row.action for row in rows} - set(ACTIONS))
    if unknown:
        raise ReconcileError(f"refusing to write unknown action(s): {unknown}")
    with conn:
        conn.executemany(
            "DELETE FROM fpts_audit WHERE slate_id = ?", [(s,) for s in slates]
        )
        conn.executemany(
            "INSERT INTO fpts_audit (slate_id, dk_id, player_id, game_date, dk_value, "
            "ops_value, delta, action, reason, off_slate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
    return len(payload)


def apply_corrections(conn: sqlite3.Connection, rows: Sequence[AuditRow]) -> int:
    """Overwrite `slate_players.actual_fpts` with the ops value. Returns rows hit.

    Only `action='corrected'` rows are touched — the UPDATE is keyed on the
    audit's own verdict rather than re-deriving one, so what the audit says and
    what the table holds cannot drift apart.

    A correction with no `ops_value` is refused rather than skipped: it would be
    a correction with nothing behind it, and writing NULL into `actual_fpts`
    would break the column's 100%-non-null guarantee that lineup scoring rests
    on.
    """
    targets = [row for row in rows if row.action == "corrected"]
    if not targets:
        return 0
    missing = [r for r in targets if r.ops_value is None]
    if missing:
        raise ReconcileError(
            f"{len(missing)} corrected row(s) carry no ops_value, e.g. "
            f"{[(r.slate_id, r.dk_id) for r in missing[:3]]} — refusing to write NULL"
        )
    with conn:
        cursor = conn.executemany(
            "UPDATE slate_players SET actual_fpts = ? WHERE slate_id = ? AND dk_id = ?",
            [(r.ops_value, r.slate_id, r.dk_id) for r in targets],
        )
    return cursor.rowcount if cursor.rowcount != -1 else len(targets)


def audit_rollup(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """The standing audit, straight from the DB — what `--audit` prints."""
    return {
        "actions": conn.execute(
            "SELECT action, COUNT(*) FROM fpts_audit GROUP BY action ORDER BY 2 DESC"
        ).fetchall(),
        "reasons": conn.execute(
            "SELECT COALESCE(reason, '(none)'), action, COUNT(*) FROM fpts_audit "
            "GROUP BY 1, 2 ORDER BY 3 DESC"
        ).fetchall(),
        "off_slate": conn.execute(
            "SELECT action, COUNT(*) FROM fpts_audit WHERE off_slate = 1 "
            "GROUP BY action ORDER BY 2 DESC"
        ).fetchall(),
        "shifted": conn.execute(
            "SELECT game_date, COUNT(*) FROM fpts_audit "
            "WHERE game_date IS NOT NULL AND game_date <> substr(slate_id, 1, 10) "
            "GROUP BY game_date ORDER BY 1"
        ).fetchall(),
    }


# --- CLI -----------------------------------------------------------------------

_MAX_LISTED = 25


def print_report(report: ReconcileReport) -> None:
    """The headline: what would change, and why, before anything is written."""
    actions = report.action_counts()
    total = len(report.rows)
    print(f"\nReconcile report — {total} slate_players row(s), {report.ops_game_count} ops game-sides")
    for action in ACTIONS:
        n = actions.get(action, 0)
        share = f" ({n / total:.1%})" if total else ""
        print(f"  {action:<12} {n:>7}{share}")

    print("\n  By reason:")
    reasons = report.reason_counts()
    for reason, n in sorted(reasons.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        print(f"    {(reason or '(none)'):<16} {n:>7}")

    off = report.off_slate_rows()
    if off:
        by_action = Counter(r.action for r in off)
        print(
            f"\n  Off-slate games: {len(off)} row(s) — "
            + ", ".join(f"{n} {a}" for a, n in sorted(by_action.items()))
        )

    shifted = report.shifted_rows()
    if shifted:
        sides = sorted({(r.slate_date, r.game_date) for r in shifted})
        print(f"\n  Date-shifted games: {len(shifted)} row(s) across {len(sides)} slate date(s)")
        for slate_date, game_date in sides[:_MAX_LISTED]:
            n = sum(1 for r in shifted if r.slate_date == slate_date and r.game_date == game_date)
            print(f"    {slate_date} -> {game_date}: {n} row(s)")

    unresolved = report.unresolved_sides()
    if unresolved:
        print(f"\n  Unresolved matchups ({len(unresolved)}) — absent from ops on every date tried:")
        for link in unresolved[:_MAX_LISTED]:
            print(f"    {link.slate_date} {link.team} vs {link.opp}")

    # Listed in full because the class is new and its cause is not established:
    # DK settled these players at exactly 0 while ops has a real box score.
    unscored = report.by_reason("dk_unscored")
    if unscored:
        players = sorted({r.name for r in unscored if r.name})
        print(
            f"\n  DK settled at 0 with a real ops box score "
            f"({len(unscored)} row(s), {len(players)} player(s)):"
        )
        for row in sorted(unscored, key=lambda r: -(r.ops_value or 0))[:_MAX_LISTED]:
            print(
                f"    {row.slate_id}  {(row.name or '?'):<24} "
                f"  0.00 -> {row.ops_value:>6.2f}"
            )
        if len(unscored) > _MAX_LISTED:
            print(f"    ... and {len(unscored) - _MAX_LISTED} more")

    corrections = [r for r in report.corrections if r.reason == "stat_correction"]
    if corrections:
        print(f"\n  Largest stat corrections ({len(corrections)} row(s)):")
        for row in sorted(corrections, key=lambda r: -abs(r.delta or 0))[:_MAX_LISTED]:
            print(
                f"    {row.slate_id}  {(row.name or '?'):<24} "
                f"{row.dk_value:>6.2f} -> {row.ops_value:>6.2f}  ({row.delta:+.2f})"
            )
        if len(corrections) > _MAX_LISTED:
            print(f"    ... and {len(corrections) - _MAX_LISTED} more")


def print_write_preview(report: ReconcileReport, command: str) -> None:
    """Exactly what `--write` will do to the DB, before it is run.

    A report that ends at "nothing written" leaves the reader to infer the
    consequence from a census. This spells out the two writes, the four things
    that stay put, and how to undo it — the phase overwrites a column that until
    now only `load_slate` has ever touched, so the reader deserves the sentence
    rather than the inference.
    """
    corrections = report.corrections
    by_reason = Counter(r.reason for r in corrections)
    off_slate_rows = report.off_slate_rows()
    actions = report.action_counts()

    print("\n" + "=" * 72)
    print("What --write will do")
    print("=" * 72)

    print(f"\n  1. UPDATE slate_players.actual_fpts on {len(corrections)} row(s) "
          f"of {len(report.rows)}:")
    for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"       {n:>5}  {reason or '(none)'}")
    if corrections:
        gained = sum(r.delta or 0 for r in corrections)
        biggest = max(corrections, key=lambda r: abs(r.delta or 0))
        print(f"       net change {gained:+.2f} fantasy points across the DB; "
              f"largest single move {biggest.delta:+.2f} "
              f"({biggest.name or biggest.dk_id} on {biggest.slate_id})")

    print(f"\n  2. INSERT {len(report.rows)} fpts_audit row(s) — a full census, one per "
          "slate_players row,")
    print("     carrying the pristine DK value, the ops value, the delta, an action, "
          "a reason,")
    print(f"     and an off_slate flag ({len(off_slate_rows)} row(s) flagged).")

    print("\n  Not touched:")
    print(f"       {actions.get('unchanged', 0):>5}  rows that already agree with ops")
    print(f"       {actions.get('no_ops_row', 0):>5}  rows with no ops log — DNPs stay 0.0, "
          "and no NULL is written")
    print(f"       {actions.get('unmapped', 0):>5}  uncrosswalked rows — no player_id, "
          "so ops is unreachable")
    print("           -  every other table: projections, lineups, lineup_players, "
          "dk_crosswalk")
    print("           -  the ops DB, which is attached read-only and probed every run")

    print("\n  Safety:")
    print("       - re-running is a no-op; the pass is idempotent and the gate proves it "
          "by running twice")
    print("       - the pristine DK value is kept in fpts_audit.dk_value, so nothing "
          "DraftKings said is lost")
    print("       - to undo: re-ingest the slates "
          "(`uv run python -m nba_dfs_stats_lab.ingest.orchestrator --all`),")
    print("         which restores actual_fpts from the source CSVs")

    print(f"\n  Run it with:\n       {command}\n")


def print_audit(rollup: dict[str, list[tuple]]) -> None:
    if not rollup["actions"]:
        print("\nfpts_audit is empty — run with --write to build it.")
        return
    print("\nStanding audit (fpts_audit):")
    for label, key in (("actions", "actions"), ("off-slate rows", "off_slate")):
        print(f"  {label}:")
        for action, n in rollup[key]:
            print(f"    {action:<12} {n:>7}")
    print("  reasons:")
    for reason, action, n in rollup["reasons"]:
        print(f"    {reason:<16} {action:<12} {n:>7}")
    if rollup["shifted"]:
        print("  values taken from a shifted game date:")
        for game_date, n in rollup["shifted"]:
            print(f"    {game_date}  {n:>5}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nba_dfs_stats_lab.ingest.reconcile",
        description=(
            "Reconcile slate_players.actual_fpts against the ops box scores. "
            "Reports by default; writing requires --write."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the audit census and apply the corrections",
    )
    parser.add_argument(
        "--slate", metavar="SLATE_ID", action="append", help="restrict to a slate (repeatable)"
    )
    parser.add_argument(
        "--audit", action="store_true", help="print the standing audit rollup and exit"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show INFO logging")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    conn = get_connection()
    try:
        try:
            init_db(conn)
        except SchemaMigrationError as exc:
            print(f"schema migration failed: {exc}", file=sys.stderr)
            return 1

        if args.audit:
            print_audit(audit_rollup(conn))
            return 0

        try:
            attach_ops(conn)
        except sqlite3.OperationalError as exc:
            # Almost always the G:\ drive not being mounted. Same posture as
            # discovery's missing-directory rule: say so, don't traceback.
            print(f"could not attach the ops DB read-only: {exc}", file=sys.stderr)
            return 1

        try:
            report = reconcile(conn, slate_ids=args.slate)
        except ReconcileError as exc:
            print(f"reconcile refused: {exc}", file=sys.stderr)
            return 2
        print_report(report)

        if not args.write:
            print(
                "\nNothing written (report only). Re-run with --write to apply "
                f"{len(report.corrections)} correction(s) and build the audit."
            )
            return 0

        # Audit first: a crash between the two leaves the pristine dk_value
        # recoverable. See `_pristine_dk_value`.
        try:
            written = write_audit(conn, report.rows)
            updated = apply_corrections(conn, report.rows)
        except ReconcileError as exc:
            print(f"write refused: {exc}", file=sys.stderr)
            return 2
        print(f"\nWrote {written} fpts_audit row(s); updated {updated} actual_fpts value(s).")
        print_audit(audit_rollup(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
