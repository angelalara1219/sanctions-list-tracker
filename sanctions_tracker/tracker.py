"""Compare today's sanctions lists to the last snapshot and log what changed.

For each source:
  1. download and parse the current list
  2. compare it to data/snapshots/<source>.csv
  3. append additions, removals and amendments to data/changes.csv
  4. save the new snapshot

Then rebuild the tables in README.md and data/coverage.md.

Safety checks: a source that fails to download or parse is skipped and
flagged in the README, and its old snapshot is kept. A list that suddenly
shrinks by more than half is treated as a bad download, not a mass delisting.
"""

from __future__ import annotations

import csv
import sys
import traceback
from collections import Counter
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path

from .sources import ROUTES, SOURCES, Entry, Source

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots"
CHANGES_PATH = DATA / "changes.csv"
STATUS_PATH = DATA / "status.csv"
COVERAGE_PATH = DATA / "coverage.md"
README_PATH = ROOT / "README.md"

ENTRY_COLUMNS = [f.name for f in fields(Entry)]
SHRINK_LIMIT = 0.5  # refuse snapshots that lose more than half the list


@dataclass
class Change:
    detected_on: str
    source: str
    change: str  # Added, Removed, Amended
    entry_id: str
    name: str
    entry_type: str
    programs: str
    detail: str


CHANGE_COLUMNS = [f.name for f in fields(Change)]


@dataclass
class Status:
    source: str
    label: str
    regulator: str
    url: str
    entries: str
    last_checked: str
    last_change: str
    result: str


STATUS_COLUMNS = [f.name for f in fields(Status)]


# ---------------------------------------------------------------- storage

def read_csv(path: Path, cls):
    if not path.exists():
        return []
    cols = [f.name for f in fields(cls)]
    with path.open(newline="", encoding="utf-8") as f:
        return [cls(**{c: row.get(c, "") for c in cols}) for row in csv.DictReader(f)]


def write_csv(path: Path, rows, cls) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[x.name for x in fields(cls)])
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def snapshot_path(key: str) -> Path:
    return SNAPSHOTS / f"{key}.csv"


# ---------------------------------------------------------------- diff

def diff(source: str, old: list[Entry], new: list[Entry], today: str) -> list[Change]:
    before = {e.entry_id: e for e in old}
    after = {e.entry_id: e for e in new}
    changes: list[Change] = []
    for eid in sorted(after.keys() - before.keys()):
        e = after[eid]
        changes.append(Change(today, source, "Added", eid, e.name, e.entry_type, e.programs, ""))
    for eid in sorted(before.keys() - after.keys()):
        e = before[eid]
        changes.append(Change(today, source, "Removed", eid, e.name, e.entry_type, e.programs, ""))
    for eid in sorted(before.keys() & after.keys()):
        a, b = before[eid], after[eid]
        notes = []
        # Ignore capitalization/spacing-only edits (e.g. "Zhi Chen" -> "Zhi CHEN").
        if not same(a.name, b.name):
            notes.append(f"name: {a.name} -> {b.name}")
        if not same(a.programs, b.programs):
            notes.append(f"programs: {a.programs} -> {b.programs}")
        if notes:
            changes.append(Change(today, source, "Amended", eid, b.name, b.entry_type,
                                  b.programs, "; ".join(notes)))
    return changes


def same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def check_shrink(old: list[Entry], new: list[Entry]) -> None:
    if old and len(new) < len(old) * SHRINK_LIMIT:
        raise RuntimeError(f"list shrank from {len(old)} to {len(new)} entries; "
                           "treating as a bad download")


# ---------------------------------------------------------------- tables

README_STATUS = ("<!-- STATUS:START -->", "<!-- STATUS:END -->")
README_LATEST = ("<!-- LATEST:START -->", "<!-- LATEST:END -->")


def status_table(statuses: list[Status]) -> str:
    lines = ["| List | Regulator | Entries | Last change | Last checked | Status |",
             "|---|---|---|---|---|---|"]
    for s in statuses:
        if s.result == "ok":
            ok = "OK"
        elif s.result == "ok-mirror":
            ok = "OK (via [OpenSanctions mirror](https://www.opensanctions.org/datasets/au_dfat_sanctions/) of DFAT's file)"
        else:
            ok = f"Needs a look: {s.result}"
        lines.append(f"| {s.label} | [{s.regulator}]({s.url}) | {s.entries or '-'} | "
                     f"{s.last_change or '-'} | {s.last_checked or '-'} | {ok} |")
    return "\n".join(lines)


def latest_table(changes: list[Change], n: int = 15) -> str:
    rows = sorted(changes, key=lambda c: c.detected_on, reverse=True)[:n]
    if not rows:
        return "_No changes logged yet. The first run saves a baseline; changes show up from the next run on._"
    lines = ["| Detected | List | Change | Name | Type | Program |", "|---|---|---|---|---|---|"]
    labels = {s.key: s.label for s in SOURCES}
    for c in rows:
        name = c.name.replace("|", "/")
        lines.append(f"| {c.detected_on} | {labels.get(c.source, c.source)} | {c.change} | "
                     f"{name} | {c.entry_type} | {c.programs.replace('|', '/')} |")
    return "\n".join(lines)


def replace_block(text: str, markers: tuple[str, str], body: str) -> str:
    start, end = markers
    if start not in text or end not in text:
        return text
    head, rest = text.split(start, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{start}\n{body}\n{end}{tail}"


def update_readme(statuses: list[Status], changes: list[Change], path: Path = README_PATH) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    text = replace_block(text, README_STATUS, status_table(statuses))
    text = replace_block(text, README_LATEST, latest_table(changes))
    path.write_text(text, encoding="utf-8")


# Country names differ between lists ("Iran (Islamic Republic of)" vs "Iran").
COUNTRY_ALIASES = {
    "iran (islamic republic of)": "Iran",
    "islamic republic of iran": "Iran",
    "russian federation": "Russia",
    "democratic people's republic of korea": "North Korea",
    "korea, north": "North Korea",
    "korea, democratic people's republic of": "North Korea",
    "dprk": "North Korea",
    "syrian arab republic": "Syria",
    "venezuela (bolivarian republic of)": "Venezuela",
    "bolivarian republic of venezuela": "Venezuela",
    "united kingdom of great britain and northern ireland": "United Kingdom",
    "united states of america": "United States",
    "türkiye": "Turkey",
    "turkiye": "Turkey",
    "united arab emirates": "UAE",
    "burma": "Myanmar",
    "china, people's republic of": "China",
    "people's republic of china": "China",
}


def norm_country(name: str) -> str:
    key = name.strip().lower()
    return COUNTRY_ALIASES.get(key, name.strip())


def coverage_markdown(snapshots: dict[str, list[Entry]], top: int = 20) -> str:
    labels = {s.key: s.label for s in SOURCES}
    keys = [s.key for s in SOURCES if s.key in snapshots]
    by_country: dict[str, Counter] = {}
    for key in keys:
        for e in snapshots[key]:
            for c in {norm_country(x) for x in e.countries.split(";") if x.strip()}:
                by_country.setdefault(c, Counter())[key] += 1
    ranked = sorted(by_country.items(), key=lambda kv: sum(kv[1].values()), reverse=True)[:top]

    out = ["# Sanctions coverage", "",
           "Generated from the latest snapshots. A listing is counted under every country tied to it "
           "(nationality, citizenship or address, depending on the list). This shows where listings "
           "are concentrated. It is not a list of embargoed countries.", "",
           "## Listings by country", "",
           "| Country | " + " | ".join(labels[k] for k in keys) + " |",
           "|---|" + "---|" * len(keys)]
    for country, counts in ranked:
        out.append(f"| {country} | " + " | ".join(str(counts.get(k, 0)) for k in keys) + " |")

    for key in keys:
        progs = Counter()
        for e in snapshots[key]:
            for p in e.programs.split(";"):
                if p.strip():
                    progs[p.strip()] += 1
        out += ["", f"## {labels[key]}: largest programs / regimes", "",
                "| Program / regime | Listings |", "|---|---|"]
        for p, n in progs.most_common(10):
            out.append(f"| {p.replace('|', '/')} | {n} |")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- main

def run_source(src: Source, today: str, prev: Status | None) -> tuple[Status, list[Change], list[Entry]]:
    old = read_csv(snapshot_path(src.key), Entry)
    new = src.fetch_and_parse()
    check_shrink(old, new)
    changes = diff(src.key, old, new, today) if old else []
    write_csv(snapshot_path(src.key),
              sorted(new, key=lambda e: (e.entry_id.zfill(12), e.entry_id)), Entry)
    last_change = today if changes else (prev.last_change if prev else "")
    status = Status(src.key, src.label, src.regulator, src.url, str(len(new)), today,
                    last_change, "ok-mirror" if ROUTES.get(src.key) == "mirror" else "ok")
    return status, changes, new


def run() -> int:
    today = date.today().isoformat()
    previous = {s.source: s for s in read_csv(STATUS_PATH, Status)}
    all_changes = read_csv(CHANGES_PATH, Change)
    statuses: list[Status] = []
    snapshots: dict[str, list[Entry]] = {}
    failures = 0

    for src in SOURCES:
        prev = previous.get(src.key)
        try:
            status, changes, entries = run_source(src, today, prev)
            all_changes.extend(changes)
            snapshots[src.key] = entries
            counts = Counter(c.change for c in changes)
            note = "baseline saved" if not prev or not prev.entries else \
                f"+{counts['Added']} / -{counts['Removed']} / ~{counts['Amended']}"
            print(f"{src.label}: {len(entries)} entries ({note})")
        except Exception as exc:  # keep going; one bad source shouldn't block the rest
            failures += 1
            traceback.print_exc()
            reason = type(exc).__name__
            status = Status(src.key, src.label, src.regulator, src.url,
                            prev.entries if prev else "", today,
                            prev.last_change if prev else "", reason)
            old = read_csv(snapshot_path(src.key), Entry)
            if old:
                snapshots[src.key] = old
            print(f"{src.label}: FAILED ({reason}); kept the previous snapshot", file=sys.stderr)
        statuses.append(status)

    all_changes.sort(key=lambda c: (c.detected_on, c.source, c.change, c.entry_id), reverse=True)
    write_csv(CHANGES_PATH, all_changes, Change)
    write_csv(STATUS_PATH, statuses, Status)
    COVERAGE_PATH.write_text(coverage_markdown(snapshots), encoding="utf-8")
    update_readme(statuses, all_changes)

    # Fail the run (so GitHub emails a heads-up) only if every source failed.
    return 1 if failures == len(SOURCES) else 0


if __name__ == "__main__":
    sys.exit(run())
