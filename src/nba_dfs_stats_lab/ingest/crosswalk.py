"""`slate_players.name` → ops `dim_players.PLAYER_ID`, confidence-scored.

The one sanctioned read of the ops DB: a read-only ATTACH at query time, taking
`dim_players` only. Nothing here writes to `ops.*` or copies it into
`analytics.db` — see the Ops DB rule in CLAUDE.md.

Two facts about the data shape this module:

1. **`dk_id` is per-slate, not per-player.** All 51,971 `slate_players` rows have
   a distinct `dk_id`, but only 610 distinct names — DraftKings re-issues an id
   for every slate. So matching is done over *names* (610 decisions) and the
   result is fanned out to `dk_crosswalk`'s `dk_id` grain (51,971 rows). That
   keeps the review list human-sized while leaving `dk_crosswalk` joinable
   straight from `slate_players` on `dk_id`, as its pinned DDL intends.

2. **Fuzzy matches are never written without approval.** On the real data the
   one true fuzzy match (`Yanic Niederhauser` → `Yanic Konan Niederhauser`)
   scores 0.93 while the best *false* candidate (`RJ Davis` → `JD Davison`)
   scores 0.39 — a wide gap, but a gap between two handfuls of names, not a law.
   `RJ Davis`/`JD Davison` and `Cameron Matthews`/`Garrison Mathews` are exactly
   the pairs a threshold would eventually get wrong, and a wrong crosswalk row
   silently mis-attributes every box score for that player. Only the two
   deterministic tiers auto-approve; everything else goes to Jonny.

The four-method ingest shape doesn't apply here — there is no CSV and no slate
grain. The pipeline is `match_players()` → review → `write_crosswalk()`.
"""

import argparse
import csv
import logging
import re
import sqlite3
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from pathlib import Path

from nba_dfs_stats_lab.db.connection import attach_ops, get_connection
from nba_dfs_stats_lab.db.schema import SchemaMigrationError, init_db

logger = logging.getLogger(__name__)

# Trailing generational suffixes. Dropped only from the *end* of a name: a token
# like "v" or "ii" mid-name would be part of the name proper, and the whole
# point of normalizing is to not invent differences or erase real ones.
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})

# Punctuation removed outright rather than replaced with a space, so "P.J."
# collapses to "pj" (matching an ops "PJ Washington") instead of splitting into
# two tokens. Hyphens are the exception — they separate real name parts.
_STRIP = re.compile(r"[.'’,]")
_HYPHEN = re.compile(r"[-‐-―]")

# Candidates scoring below this are reported as unmatched rather than queued for
# review. Nothing is hidden by it: `unmatched_report` still carries each name's
# best candidate and score, so a real match that lands under the floor is
# visible, not silently dropped.
REVIEW_FLOOR = 0.60

_MAX_CANDIDATES = 3  # per name, in the review list


class Tier(str, Enum):
    """How a name was matched. Only the first two write without review."""

    EXACT = "exact"  # source strings identical
    NORMALIZED = "normalized"  # identical after `normalize_name`
    REVIEW = "review"  # fuzzy candidate at or above REVIEW_FLOOR
    AMBIGUOUS = "ambiguous"  # >1 ops player behind one normalized key
    NONE = "none"  # no candidate above the floor


AUTO_TIERS = frozenset({Tier.EXACT, Tier.NORMALIZED})


def normalize_name(name: str) -> str:
    """Casefold, strip accents and punctuation, drop trailing suffixes.

    `Marjon Beauchamp` → `marjon beauchamp` (matching ops `MarJon Beauchamp`),
    `Patrick Baldwin Jr.` → `patrick baldwin`, `Luka Dončić` → `luka doncic`.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode()
    cleaned = _HYPHEN.sub(" ", _STRIP.sub("", ascii_only)).lower()
    tokens = cleaned.split()
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclass(frozen=True)
class OpsPlayer:
    player_id: int
    name: str
    normalized: str


@dataclass(frozen=True)
class Candidate:
    """A scored ops player proposed for a DK name."""

    player_id: int
    ops_name: str
    score: float
    reason: str


@dataclass
class NameMatch:
    """One DK name's outcome. `dk_ids` is what a write fans out to."""

    name: str
    tier: Tier
    dk_ids: tuple[int, ...] = ()
    slate_count: int = 0
    player_id: int | None = None
    ops_name: str | None = None
    score: float = 0.0
    candidates: tuple[Candidate, ...] = ()

    @property
    def approvable(self) -> bool:
        """True when this match may be written without Jonny's sign-off."""
        return self.tier in AUTO_TIERS and self.player_id is not None


@dataclass
class MatchReport:
    """The whole matching run: every name, tiered."""

    matches: list[NameMatch] = field(default_factory=list)
    ops_player_count: int = 0

    def by_tier(self, *tiers: Tier) -> list[NameMatch]:
        wanted = set(tiers)
        return [m for m in self.matches if m.tier in wanted]

    @property
    def auto(self) -> list[NameMatch]:
        return [m for m in self.matches if m.approvable]

    @property
    def needs_review(self) -> list[NameMatch]:
        """Ambiguous first — an ambiguous name is a data problem, not a guess."""
        return sorted(
            self.by_tier(Tier.REVIEW, Tier.AMBIGUOUS),
            key=lambda m: (m.tier is not Tier.AMBIGUOUS, -m.score, m.name),
        )

    @property
    def unmatched(self) -> list[NameMatch]:
        return sorted(self.by_tier(Tier.NONE), key=lambda m: (-len(m.dk_ids), m.name))

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys((t.value for t in Tier), 0)
        for match in self.matches:
            counts[match.tier.value] += 1
        return counts

    def match_rate(self) -> float:
        """Share of *names* auto-matched. 0.0 when there are no names at all."""
        return len(self.auto) / len(self.matches) if self.matches else 0.0

    def dk_id_coverage(self) -> tuple[int, int]:
        """(dk_ids the auto tiers cover, dk_ids in slate_players)."""
        total = sum(len(m.dk_ids) for m in self.matches)
        return sum(len(m.dk_ids) for m in self.auto), total


# --- Reading both sides --------------------------------------------------------


def load_ops_players(conn: sqlite3.Connection, alias: str = "ops") -> list[OpsPlayer]:
    """Read `dim_players` through an already-attached read-only ops connection."""
    rows = conn.execute(
        f"SELECT PLAYER_ID, PLAYER_NAME FROM {alias}.dim_players "  # noqa: S608 — alias is checked by attach_ops
        "WHERE PLAYER_NAME IS NOT NULL"
    ).fetchall()
    return [OpsPlayer(int(pid), name, normalize_name(name)) for pid, name in rows]


def load_slate_names(conn: sqlite3.Connection) -> list[NameMatch]:
    """Distinct `slate_players.name`, each carrying every `dk_id` issued for it.

    Grouped by the raw name, not the normalized one: two spellings of the same
    player are two decisions to make and should be seen as two lines.
    """
    rows = conn.execute(
        "SELECT name, dk_id, slate_id FROM slate_players "
        "WHERE name IS NOT NULL AND TRIM(name) <> '' ORDER BY name, dk_id"
    ).fetchall()
    grouped: dict[str, tuple[list[int], set[str]]] = {}
    for name, dk_id, slate_id in rows:
        ids, slates = grouped.setdefault(name.strip(), ([], set()))
        ids.append(int(dk_id))
        slates.add(slate_id)
    return [
        NameMatch(name=name, tier=Tier.NONE, dk_ids=tuple(ids), slate_count=len(slates))
        for name, (ids, slates) in sorted(grouped.items())
    ]


# --- Scoring -------------------------------------------------------------------


def score_candidate(dk_normalized: str, ops_normalized: str) -> tuple[float, str]:
    """Blend sequence similarity, token containment, and a surname signal.

    The signals disagree in the way that matters. `Yanic Niederhauser` vs
    `Yanic Konan Niederhauser` is only 0.86 by raw character ratio — a dropped
    middle name moves a lot of characters — but every one of its tokens appears
    in the ops name and the surnames match, which lifts it to 0.93. `RJ Davis`
    vs `JD Davison` is a deceptively high 0.78 by ratio alone and shares no
    token and no surname, which drops it to 0.39. Ratio alone cannot separate
    those two; the token signals can.

    The ratio is taken over both the names as written and their tokens sorted,
    whichever is kinder. That is what catches a reversed name order: DK writes
    the Portland centre `Hansen Yang`, ops has him as `Yang Hansen`, and read
    left-to-right those two strings are only 0.57 similar — under the review
    floor, i.e. invisible. Sorted, they are the same string. Order-swapped
    given/family names are a standing feature of NBA rosters, not a one-off.

    Returns the score and a short human-readable reason for the review list.
    """
    dk_tokens = dk_normalized.split()
    ops_tokens = ops_normalized.split()
    if not dk_tokens or not ops_tokens:
        return 0.0, "empty name"

    ratio = SequenceMatcher(None, dk_normalized, ops_normalized).ratio()
    sorted_ratio = SequenceMatcher(
        None, " ".join(sorted(dk_tokens)), " ".join(sorted(ops_tokens))
    ).ratio()
    best_ratio = max(ratio, sorted_ratio)

    shared = set(dk_tokens) & set(ops_tokens)
    containment = len(shared) / len(dk_tokens)
    same_surname = dk_tokens[-1] == ops_tokens[-1]

    score = 0.5 * best_ratio + 0.3 * containment + 0.2 * float(same_surname)
    reason = (
        f"ratio {ratio:.2f}"
        + (f" (sorted {sorted_ratio:.2f})" if sorted_ratio > ratio else "")
        + f", {len(shared)}/{len(dk_tokens)} tokens shared, "
        f"surname {'match' if same_surname else 'differs'}"
    )
    return score, reason


def _candidates(dk_normalized: str, ops: Sequence[OpsPlayer]) -> list[Candidate]:
    scored = []
    for player in ops:
        score, reason = score_candidate(dk_normalized, player.normalized)
        if score >= REVIEW_FLOOR:
            scored.append(Candidate(player.player_id, player.name, score, reason))
    scored.sort(key=lambda c: (-c.score, c.ops_name))
    return scored[:_MAX_CANDIDATES]


def _best_effort_candidate(
    dk_normalized: str, ops: Sequence[OpsPlayer]
) -> tuple[Candidate, ...]:
    """The single closest ops player regardless of the floor.

    Attached to unmatched names so the report shows *what* the nearest thing was.
    A name with no candidate at all is the honest answer for a rookie the ops DB
    has never seen; showing the near miss is what makes that judgeable.
    """
    best: Candidate | None = None
    for player in ops:
        score, reason = score_candidate(dk_normalized, player.normalized)
        if best is None or score > best.score:
            best = Candidate(player.player_id, player.name, score, reason)
    return (best,) if best is not None else ()


def match_players(conn: sqlite3.Connection, alias: str = "ops") -> MatchReport:
    """Tier every distinct `slate_players.name` against ops `dim_players`.

    Pure read — writes nothing to either database.
    """
    ops = load_ops_players(conn, alias=alias)
    matches = load_slate_names(conn)

    by_raw: dict[str, list[OpsPlayer]] = {}
    by_normalized: dict[str, list[OpsPlayer]] = {}
    for player in ops:
        by_raw.setdefault(player.name, []).append(player)
        by_normalized.setdefault(player.normalized, []).append(player)

    for match in matches:
        normalized = normalize_name(match.name)
        # Raw before normalized: an identical source string is the strongest
        # evidence available and shouldn't be relabelled by a later tier.
        for tier, bucket in (
            (Tier.EXACT, by_raw.get(match.name)),
            (Tier.NORMALIZED, by_normalized.get(normalized)),
        ):
            if not bucket:
                continue
            if len(bucket) > 1:
                # Two ops players collapse to one key. Zero of these exist in the
                # current snapshot, but auto-writing a coin flip here would
                # mis-attribute every box score for the loser, so it goes to
                # review with all the colliding candidates shown.
                match.tier = Tier.AMBIGUOUS
                match.candidates = tuple(
                    Candidate(p.player_id, p.name, 1.0, f"{tier.value} tie")
                    for p in bucket
                )
                match.score = 1.0
                break
            player = bucket[0]
            match.tier = tier
            match.player_id = player.player_id
            match.ops_name = player.name
            match.score = 1.0
            match.candidates = (
                Candidate(player.player_id, player.name, 1.0, f"{tier.value} match"),
            )
            break
        else:
            candidates = _candidates(normalized, ops)
            if candidates:
                match.tier = Tier.REVIEW
                match.candidates = tuple(candidates)
                match.score = candidates[0].score
                # Deliberately not set: `player_id` stays None until approved, so
                # nothing downstream can mistake a proposal for a decision.
            else:
                match.tier = Tier.NONE
                match.candidates = _best_effort_candidate(normalized, ops)
                match.score = match.candidates[0].score if match.candidates else 0.0

    return MatchReport(matches=matches, ops_player_count=len(ops))


# --- The review round-trip -----------------------------------------------------

REVIEW_COLUMNS = (
    "approve",
    "dk_name",
    "player_id",
    "ops_name",
    "score",
    "reason",
    "tier",
    "dk_ids",
    "slates",
)


def write_review_csv(report: MatchReport, path: Path) -> int:
    """Export the review queue with a blank `approve` column. Returns rows written.

    One row per *candidate*, not per name: an ambiguous name is precisely the
    case where Jonny needs to see the alternatives side by side. Approving more
    than one candidate for a name is rejected at `--apply` time.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        for match in report.needs_review:
            for candidate in match.candidates:
                writer.writerow(
                    {
                        "approve": "",
                        "dk_name": match.name,
                        "player_id": candidate.player_id,
                        "ops_name": candidate.ops_name,
                        "score": f"{candidate.score:.3f}",
                        "reason": candidate.reason,
                        "tier": match.tier.value,
                        "dk_ids": len(match.dk_ids),
                        "slates": match.slate_count,
                    }
                )
                rows += 1
    return rows


_TRUTHY = frozenset({"y", "yes", "true", "1", "x", "approve", "approved"})


class ApprovalError(Exception):
    """The reviewed CSV can't be applied as written."""


def read_approvals(path: Path) -> dict[str, int]:
    """Parse a reviewed CSV into `{dk_name: player_id}` for approved rows only.

    Raises rather than guessing: an unreadable approval is the one place in this
    module where a wrong answer gets written to the DB.
    """
    path = Path(path)
    if not path.exists():
        raise ApprovalError(f"no such review file: {path}")

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [
            c
            for c in ("approve", "dk_name", "player_id")
            if c not in (reader.fieldnames or ())
        ]
        if missing:
            raise ApprovalError(f"{path.name} is missing column(s): {missing}")

        approved: dict[str, int] = {}
        conflicts: list[str] = []
        for line, row in enumerate(reader, start=2):
            if (row.get("approve") or "").strip().lower() not in _TRUTHY:
                continue
            name = (row.get("dk_name") or "").strip()
            raw_id = (row.get("player_id") or "").strip()
            if not name:
                raise ApprovalError(
                    f"{path.name} line {line}: approved row has no dk_name"
                )
            try:
                player_id = int(raw_id)
            except ValueError:
                raise ApprovalError(
                    f"{path.name} line {line}: player_id {raw_id!r} is not an integer"
                ) from None
            if name in approved and approved[name] != player_id:
                conflicts.append(f"{name!r} -> {approved[name]} and {player_id}")
            approved[name] = player_id

    if conflicts:
        # Approving two candidates for one name is the likeliest review mistake,
        # and last-write-wins would silently pick one.
        raise ApprovalError(
            f"{path.name}: {len(conflicts)} name(s) approved twice: {conflicts}"
        )
    return approved


def apply_approvals(report: MatchReport, approved: dict[str, int]) -> list[NameMatch]:
    """Resolve approved names against the report. Returns the matches to write.

    Validates that each approved name exists and that the approved `player_id`
    was one of the candidates actually offered — a hand-typed id that was never
    proposed is a typo, not a decision.
    """
    by_name = {m.name: m for m in report.matches}
    unknown = sorted(set(approved) - set(by_name))
    if unknown:
        raise ApprovalError(
            f"{len(unknown)} approved name(s) are not in slate_players: {unknown}"
        )

    resolved: list[NameMatch] = []
    off_menu: list[str] = []
    for name, player_id in approved.items():
        match = by_name[name]
        offered = {c.player_id for c in match.candidates}
        if player_id not in offered:
            off_menu.append(f"{name!r} -> {player_id} (offered: {sorted(offered)})")
            continue
        ops_name = next(
            c.ops_name for c in match.candidates if c.player_id == player_id
        )
        resolved.append(
            NameMatch(
                name=name,
                tier=match.tier,
                dk_ids=match.dk_ids,
                slate_count=match.slate_count,
                player_id=player_id,
                ops_name=ops_name,
                score=match.score,
                candidates=match.candidates,
            )
        )
    if off_menu:
        raise ApprovalError(
            f"{len(off_menu)} approved id(s) were never offered: {off_menu}"
        )
    return resolved


# --- Writing -------------------------------------------------------------------


def write_crosswalk(conn: sqlite3.Connection, matches: Iterable[NameMatch]) -> int:
    """Upsert `dk_crosswalk` rows for each match, fanned out to every `dk_id`.

    Upsert rather than the delete-then-insert `load_slate` uses: approvals arrive
    in batches over time, and rebuilding the table from one batch would drop
    every mapping approved in an earlier one. `--rebuild` on the CLI is the
    explicit way to clear. Re-applying the same batch is a no-op.

    Refuses a match with no `player_id`: a proposal is not a decision.
    """
    rows: list[tuple[int, int, str]] = []
    for match in matches:
        if match.player_id is None:
            raise ValueError(f"refusing to write unresolved match for {match.name!r}")
        rows.extend((dk_id, match.player_id, match.name) for dk_id in match.dk_ids)
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO dk_crosswalk (dk_id, player_id, display_name) VALUES (?, ?, ?) "
            "ON CONFLICT(dk_id) DO UPDATE SET "
            "player_id = excluded.player_id, display_name = excluded.display_name",
            rows,
        )
    return len(rows)


def clear_crosswalk(conn: sqlite3.Connection) -> int:
    """Delete every `dk_crosswalk` row. Only `--rebuild` calls this."""
    with conn:
        cursor = conn.execute("DELETE FROM dk_crosswalk")
    return cursor.rowcount


# --- Ongoing monitoring --------------------------------------------------------


@dataclass(frozen=True)
class UnmatchedRow:
    name: str
    dk_id_count: int
    slate_count: int
    first_slate: str
    last_slate: str


def unmatched_report(conn: sqlite3.Connection) -> list[UnmatchedRow]:
    """`slate_players` names with no `dk_crosswalk` row, worst offenders first.

    The standing monitor the plan calls for: run it after each new slate and a
    rookie who has just entered the league shows up as a new line. Grouped by
    name rather than listed per `dk_id`, since one new player would otherwise
    appear once per slate they've played.
    """
    rows = conn.execute(
        """
        SELECT sp.name,
               COUNT(DISTINCT sp.dk_id),
               COUNT(DISTINCT sp.slate_id),
               MIN(sp.slate_id),
               MAX(sp.slate_id)
          FROM slate_players sp
          LEFT JOIN dk_crosswalk x ON x.dk_id = sp.dk_id
         WHERE x.dk_id IS NULL AND sp.name IS NOT NULL
         GROUP BY sp.name
         ORDER BY COUNT(DISTINCT sp.dk_id) DESC, sp.name
        """
    ).fetchall()
    return [
        UnmatchedRow(n, ids, slates, first, last)
        for n, ids, slates, first, last in rows
    ]


def coverage(conn: sqlite3.Connection) -> dict[str, int]:
    """Row-level crosswalk coverage of `slate_players`, for the gate and the CLI."""
    total, covered = conn.execute(
        "SELECT COUNT(*), COUNT(x.dk_id) FROM slate_players sp "
        "LEFT JOIN dk_crosswalk x ON x.dk_id = sp.dk_id"
    ).fetchone()
    names_total, names_covered = conn.execute(
        "SELECT COUNT(*), SUM(matched) FROM ("
        "  SELECT sp.name, MAX(CASE WHEN x.dk_id IS NULL THEN 0 ELSE 1 END) AS matched"
        "    FROM slate_players sp"
        "    LEFT JOIN dk_crosswalk x ON x.dk_id = sp.dk_id"
        "   GROUP BY sp.name)"
    ).fetchone()
    return {
        "slate_player_rows": total,
        "rows_covered": covered,
        "names": names_total,
        "names_covered": names_covered or 0,
        "crosswalk_rows": conn.execute("SELECT COUNT(*) FROM dk_crosswalk").fetchone()[
            0
        ],
    }


# --- CLI -----------------------------------------------------------------------

_MAX_LISTED = 40


def print_match_report(report: MatchReport, show_unmatched: bool = True) -> None:
    """The gate's headline: match rate, then everything that isn't automatic."""
    counts = report.counts()
    auto_ids, total_ids = report.dk_id_coverage()
    print(
        f"\nCrosswalk match report — {len(report.matches)} distinct names "
        f"in slate_players, {report.ops_player_count} players in ops.dim_players"
    )
    print(
        f"  auto-matched   {len(report.auto):>4} / {len(report.matches)} names "
        f"({report.match_rate():.1%})"
    )
    print(
        f"    exact        {counts[Tier.EXACT.value]:>4}   (source strings identical)"
    )
    print(
        f"    normalized   {counts[Tier.NORMALIZED.value]:>4}   (identical after normalize_name)"
    )
    print(
        f"  needs review   {counts[Tier.REVIEW.value] + counts[Tier.AMBIGUOUS.value]:>4}   "
        f"(review {counts[Tier.REVIEW.value]}, ambiguous {counts[Tier.AMBIGUOUS.value]})"
    )
    print(f"  unmatched      {counts[Tier.NONE.value]:>4}")
    if total_ids:
        print(
            f"  dk_id coverage {auto_ids} / {total_ids} rows auto-matched "
            f"({auto_ids / total_ids:.1%})"
        )

    review = report.needs_review
    if review:
        print(
            f"\n  Low-confidence matches for review ({len(review)}) — nothing below is written:"
        )
        for match in review[:_MAX_LISTED]:
            print(
                f"    {match.name!r} [{match.tier.value}] "
                f"{len(match.dk_ids)} dk_id(s), {match.slate_count} slate(s)"
            )
            for candidate in match.candidates:
                print(
                    f"        -> {candidate.ops_name!r} (id {candidate.player_id}) "
                    f"score {candidate.score:.3f} — {candidate.reason}"
                )
        if len(review) > _MAX_LISTED:
            print(f"    ... and {len(review) - _MAX_LISTED} more")
    else:
        print("\n  No low-confidence matches.")

    if show_unmatched and report.unmatched:
        print(
            f"\n  Unmatched ({len(report.unmatched)}) — no ops candidate above "
            f"{REVIEW_FLOOR:.2f}; nearest shown for context:"
        )
        for match in report.unmatched[:_MAX_LISTED]:
            nearest = match.candidates[0] if match.candidates else None
            detail = (
                f"nearest {nearest.ops_name!r} score {nearest.score:.3f}"
                if nearest
                else "no candidate"
            )
            print(
                f"    {match.name!r}: {len(match.dk_ids)} dk_id(s), "
                f"{match.slate_count} slate(s) — {detail}"
            )
        if len(report.unmatched) > _MAX_LISTED:
            print(f"    ... and {len(report.unmatched) - _MAX_LISTED} more")


def print_unmatched(rows: Sequence[UnmatchedRow]) -> None:
    """The standing monitor: what's in `slate_players` with no `dk_crosswalk` row."""
    if not rows:
        print("\nUnmatched report: every slate_players row has a dk_crosswalk mapping.")
        return
    print(f"\nUnmatched report — {len(rows)} name(s) with no dk_crosswalk row:")
    for row in rows[:_MAX_LISTED]:
        print(
            f"  {row.name!r}: {row.dk_id_count} dk_id(s), {row.slate_count} slate(s), "
            f"{row.first_slate} .. {row.last_slate}"
        )
    if len(rows) > _MAX_LISTED:
        print(f"  ... and {len(rows) - _MAX_LISTED} more")


def print_coverage(stats: dict[str, int], header: str) -> None:
    rows, total = stats["rows_covered"], stats["slate_player_rows"]
    names, names_total = stats["names_covered"], stats["names"]
    print(f"\n{header}:")
    print(f"  dk_crosswalk rows      {stats['crosswalk_rows']}")
    print(
        f"  slate_players covered  {rows} / {total}"
        + (f" ({rows / total:.1%})" if total else "")
    )
    print(
        f"  names covered          {names} / {names_total}"
        + (f" ({names / names_total:.1%})" if names_total else "")
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nba_dfs_stats_lab.ingest.crosswalk",
        description=(
            "Match slate_players names to ops dim_players and build dk_crosswalk. "
            "Reports by default; writing requires --write or --apply."
        ),
    )
    parser.add_argument(
        "--review",
        metavar="PATH",
        help="export the low-confidence queue to a CSV for approval",
    )
    parser.add_argument(
        "--apply",
        metavar="PATH",
        help="write the auto-matched tiers plus the approved rows of a reviewed CSV",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the auto-matched (exact/normalized) tiers",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="clear dk_crosswalk before writing (requires --write or --apply)",
    )
    parser.add_argument(
        "--unmatched",
        action="store_true",
        help="print the DB-side unmatched report and exit",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show INFO logging"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    writing = bool(args.write or args.apply)
    if args.rebuild and not writing:
        print("--rebuild only makes sense with --write or --apply", file=sys.stderr)
        return 2

    conn = get_connection()
    try:
        try:
            init_db(conn)
        except SchemaMigrationError as exc:
            print(f"schema migration failed: {exc}", file=sys.stderr)
            return 1

        if args.unmatched:
            print_unmatched(unmatched_report(conn))
            print_coverage(coverage(conn), "Coverage")
            return 0

        try:
            attach_ops(conn)
        except sqlite3.OperationalError as exc:
            # Almost always the G:\ drive not being mounted. Same posture as
            # discovery's missing-directory rule: say so, don't traceback.
            print(f"could not attach the ops DB read-only: {exc}", file=sys.stderr)
            return 1

        report = match_players(conn)
        print_match_report(report)

        if args.review:
            written = write_review_csv(report, Path(args.review))
            print(f"\nWrote {written} candidate row(s) to {args.review}")
            print(
                "  Mark the `approve` column y on the rows you accept, then re-run with "
                f"--apply {args.review}"
            )

        if not writing:
            print(
                "\nNothing written (report only). Use --write for the auto-matched tiers, "
                "--apply PATH to include reviewed approvals."
            )
            return 0

        to_write = list(report.auto)
        if args.apply:
            try:
                approvals = read_approvals(Path(args.apply))
                to_write.extend(apply_approvals(report, approvals))
            except ApprovalError as exc:
                print(f"approval file rejected: {exc}", file=sys.stderr)
                return 2
            print(f"\n{len(approvals)} approved name(s) read from {args.apply}")

        if args.rebuild:
            print(f"  cleared {clear_crosswalk(conn)} existing dk_crosswalk row(s)")
        written = write_crosswalk(conn, to_write)
        print(f"  wrote {written} dk_crosswalk row(s) from {len(to_write)} name(s)")
        print_coverage(coverage(conn), "Coverage after write")
        print_unmatched(unmatched_report(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
