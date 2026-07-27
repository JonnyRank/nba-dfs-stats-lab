"""Match each ranked-lineups CSV to the slate it actually belongs to.

The optimizer names lineup files from the wall-clock time of the run, not the
slate they were built for, so the date in the filename is unreliable. DraftKings
assigns a disjoint block of DFS IDs to every slate (verified at runtime: zero ID
overlap between slates), so the player IDs inside a lineups file identify its
slate unambiguously.

Both source directories are read strictly read-only. Everything this writes goes
to data/lineups_slate_match/:

  lineup_slate_matches.csv   full per-file match report
  lineup_slate_matches.json  same, machine-readable
  relabeled/                 corrected COPIES of the matched files
  unmatched/                 copies of files no slate could claim
  manifest.csv               relabeled/ contents + true generation time
  README.md                  summary of what happened and what to watch

Run:  uv run python scripts/match_lineups_to_slates.py [--no-copy]
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nba_dfs_stats_lab.config import DATA_DIR, LINEUPS_DIR, SALARY_DIR  # noqa: E402

OUT_DIR = DATA_DIR / "lineups_slate_match"
RELABELED_DIR = OUT_DIR / "relabeled"
UNMATCHED_DIR = OUT_DIR / "unmatched"

SALARY_RE = re.compile(r"^(?P<type>[A-Za-z]+)-(?P<date>\d{4}-\d{2}-\d{2})\.csv$")
LINEUPS_RE = re.compile(
    r"^ranked-lineups-(?:(?P<type>[A-Za-z]+)-)?(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:_(?P<ts>\d{6}))?\.csv$"
)
DK_ID_RE = re.compile(r"\((\d+)\)")

SLOTS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]

# Slate types the analytics schema accepts. "Late" exists in the salary
# directory but is not in this set; any lineups file matching one is reported
# rather than silently relabeled.
KNOWN_TYPES = {"main", "early", "turbo", "afternoon", "night"}


@dataclass
class Slate:
    path: Path
    slate_type: str
    date: str
    ids: set[str]

    @property
    def slate_id(self) -> str:
        return f"{self.date}_classic_{self.slate_type}"


@dataclass
class LineupFile:
    path: Path
    named_date: str
    named_type: str | None
    ts: str | None
    ids: set[str] = field(default_factory=set)
    n_lineups: int = 0
    malformed_cells: int = 0

    @property
    def generated_at(self) -> str:
        """True wall-clock time of the optimizer run, from the (bad) filename.

        The date is wrong for the *slate*, but it is right for *when the file
        was produced* -- which is what keep-latest actually needs.
        """
        t = self.ts or "000000"
        return f"{self.named_date}T{t[0:2]}:{t[2:4]}:{t[4:6]}"


def load_slates() -> list[Slate]:
    slates: list[Slate] = []
    for path in sorted(SALARY_DIR.glob("*.csv")):
        m = SALARY_RE.match(path.name)
        if not m:
            print(f"  ! unparseable slate filename, skipped: {path.name}")
            continue
        with path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        ids = {r["DFS ID"].strip() for r in rows if (r.get("DFS ID") or "").strip()}
        slates.append(Slate(path, m.group("type").lower(), m.group("date"), ids))
    return slates


def load_lineup_files() -> list[LineupFile]:
    files: list[LineupFile] = []
    for path in sorted(LINEUPS_DIR.glob("*.csv")):
        m = LINEUPS_RE.match(path.name)
        if not m:
            print(f"  ! unparseable lineups filename, skipped: {path.name}")
            continue
        lf = LineupFile(
            path=path,
            named_date=m.group("date"),
            named_type=(m.group("type").lower() if m.group("type") else None),
            ts=m.group("ts"),
        )
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                lf.n_lineups += 1
                for slot in SLOTS:
                    cell = (row.get(slot) or "").strip()
                    if not cell:
                        continue
                    hit = DK_ID_RE.search(cell)
                    if hit:
                        lf.ids.add(hit.group(1))
                    else:
                        lf.malformed_cells += 1
        files.append(lf)
    return files


def proposed_name(slate: Slate, lf: LineupFile) -> str:
    prefix = "" if slate.slate_type == "main" else f"{slate.slate_type.capitalize()}-"
    ts_part = f"_{lf.ts}" if lf.ts else ""
    return f"ranked-lineups-{prefix}{slate.date}{ts_part}.csv"


def main() -> int:
    do_copy = "--no-copy" not in sys.argv

    print(f"slates dir : {SALARY_DIR}")
    print(f"lineups dir: {LINEUPS_DIR}\n")

    slates = load_slates()
    files = load_lineup_files()
    print(f"loaded {len(slates)} slates, {len(files)} lineup files\n")

    # The whole method rests on DK ID blocks being disjoint across slates.
    # Verify rather than assume -- if it ever fails, the matches are suspect.
    seen: dict[str, str] = {}
    collisions = 0
    for s in slates:
        for i in s.ids:
            if i in seen and seen[i] != s.path.name:
                collisions += 1
            seen[i] = s.path.name
    print(f"disjointness check: {collisions} DFS IDs shared between slates")
    if collisions:
        print("  ! matches below may be ambiguous -- review runner_up columns")
    print()

    results = []
    for lf in files:
        hits = sorted(
            ((s, len(lf.ids & s.ids)) for s in slates),
            key=lambda t: -t[1],
        )
        hits = [(s, n) for s, n in hits if n]
        best, best_n = hits[0] if hits else (None, 0)
        second_n = hits[1][1] if len(hits) > 1 else 0
        coverage = best_n / len(lf.ids) if lf.ids and best else 0.0

        if best is None:
            status = "NO_MATCH"
        elif second_n:
            status = "AMBIGUOUS"
        elif coverage < 1.0:
            status = "PARTIAL"
        elif best.slate_type not in KNOWN_TYPES:
            status = "UNKNOWN_TYPE"
        else:
            status = "OK"

        results.append(
            {
                "current_name": lf.path.name,
                "generated_at": lf.generated_at,
                "named_date": lf.named_date,
                "ts": lf.ts or "",
                "n_lineups": lf.n_lineups,
                "n_unique_ids": len(lf.ids),
                "malformed_cells": lf.malformed_cells,
                "matched_slate_file": best.path.name if best else "",
                "matched_slate_id": best.slate_id if best else "",
                "matched_date": best.date if best else "",
                "matched_type": best.slate_type if best else "",
                "ids_matched": best_n,
                "coverage": round(coverage, 4),
                "date_shifted": bool(best and best.date != lf.named_date),
                "type_shifted": bool(best and best.slate_type != (lf.named_type or "main")),
                "runner_up_slate": hits[1][0].path.name if len(hits) > 1 else "",
                "runner_up_ids": second_n,
                "status": status,
                "proposed_name": proposed_name(best, lf) if best else "",
            }
        )

    # --- keep-latest analysis -------------------------------------------------
    # Renaming rewrites the date but keeps the HHMMSS suffix, so for slates whose
    # files were generated across more than one calendar day the HHMMSS suffix no
    # longer encodes true recency. Resolve keep-latest on generated_at instead
    # and flag every slate where the two disagree.
    by_slate: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        if r["matched_slate_id"]:
            by_slate[r["matched_slate_id"]].append(r)

    keep_latest_conflicts = []
    for sid, rs in by_slate.items():
        true_latest = max(rs, key=lambda r: r["generated_at"])
        ts_latest = max(rs, key=lambda r: r["ts"])
        for r in rs:
            r["is_latest_for_slate"] = r is true_latest
        if true_latest is not ts_latest:
            keep_latest_conflicts.append(
                {
                    "slate_id": sid,
                    "true_latest": true_latest["current_name"],
                    "true_latest_generated_at": true_latest["generated_at"],
                    "hhmmss_would_pick": ts_latest["proposed_name"],
                    "hhmmss_would_pick_generated_at": ts_latest["generated_at"],
                }
            )
    for r in results:
        r.setdefault("is_latest_for_slate", False)

    name_counts = Counter(r["proposed_name"] for r in results if r["proposed_name"])
    for r in results:
        r["proposed_name_collision"] = bool(
            r["proposed_name"] and name_counts[r["proposed_name"]] > 1
        )

    # --- write reports --------------------------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fields = list(results[0].keys())
    with (OUT_DIR / "lineup_slate_matches.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(results)
    (OUT_DIR / "lineup_slate_matches.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    (OUT_DIR / "keep_latest_conflicts.json").write_text(
        json.dumps(keep_latest_conflicts, indent=2), encoding="utf-8"
    )

    # --- materialise corrected copies ----------------------------------------
    copied = unmatched_copied = 0
    if do_copy:
        for d in (RELABELED_DIR, UNMATCHED_DIR):
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True)
        for r in results:
            src = LINEUPS_DIR / r["current_name"]
            if r["status"] in ("OK", "UNKNOWN_TYPE") and not r["proposed_name_collision"]:
                shutil.copy2(src, RELABELED_DIR / r["proposed_name"])
                copied += 1
            else:
                shutil.copy2(src, UNMATCHED_DIR / r["current_name"])
                unmatched_copied += 1

        manifest = [
            {
                "relabeled_name": r["proposed_name"],
                "original_name": r["current_name"],
                "slate_id": r["matched_slate_id"],
                "generated_at": r["generated_at"],
                "is_latest_for_slate": r["is_latest_for_slate"],
                "n_lineups": r["n_lineups"],
            }
            for r in results
            if r["status"] in ("OK", "UNKNOWN_TYPE") and not r["proposed_name_collision"]
        ]
        manifest.sort(key=lambda m: (m["slate_id"], m["generated_at"]))
        with (OUT_DIR / "manifest.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(manifest[0].keys()))
            w.writeheader()
            w.writerows(manifest)

    # --- summary --------------------------------------------------------------
    print("status counts:")
    for k, v in sorted(Counter(r["status"] for r in results).items()):
        print(f"  {k:13s} {v}")
    print(f"\ndate corrected : {sum(1 for r in results if r['date_shifted'])}")
    print(f"type corrected : {sum(1 for r in results if r['type_shifted'])}")
    print(f"name collisions: {sum(1 for r in results if r['proposed_name_collision'])}")
    print(f"slates covered : {len(by_slate)}")
    print(f"keep-latest conflicts: {len(keep_latest_conflicts)}")
    for c in keep_latest_conflicts:
        print(f"  {c['slate_id']}: true latest {c['true_latest']} "
              f"(gen {c['true_latest_generated_at']}) but HHMMSS picks "
              f"{c['hhmmss_would_pick']} (gen {c['hhmmss_would_pick_generated_at']})")
    if do_copy:
        print(f"\ncopied {copied} relabeled -> {RELABELED_DIR}")
        print(f"copied {unmatched_copied} unmatched -> {UNMATCHED_DIR}")
    print(f"\nreports in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
