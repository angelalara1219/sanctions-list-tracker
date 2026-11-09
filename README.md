# Sanctions List Tracker

A small, self-updating log of who gets added to, removed from, or amended on four public sanctions lists: the US (OFAC), the UK (FCDO), Australia (DFAT), and the UN Security Council.

## Why I built this

Screening tools tell you whether a name matches. They don't always make it easy to see what changed on a list this week, across regimes, in one place. When I managed sanctions watchlist updates across the US, Canada and Australia, that "what changed and does it affect us" question came up constantly, and the answer usually meant checking several government sites one by one.

So I automated it. Every weekday, a GitHub Action downloads each list, compares it to the last snapshot, and logs every addition, removal and amendment to [`data/changes.csv`](data/changes.csv). The commit history doubles as a dated record of each change.

— Angela Lara, CAMS

## List status

<!-- STATUS:START -->
| List | Regulator | Entries | Last change | Last checked | Status |
|---|---|---|---|---|---|
| US | [OFAC (Treasury) - SDN List](https://sanctionslist.ofac.treas.gov/Home/SdnList) | - | - | - | Waiting for first run |
| UK | [FCDO - UK Sanctions List](https://www.gov.uk/government/publications/the-uk-sanctions-list) | - | - | - | Waiting for first run |
| AU | [DFAT - Australian Sanctions Consolidated List](https://www.dfat.gov.au/international-relations/security/sanctions/consolidated-list) | - | - | - | Waiting for first run |
| UN | [UN Security Council - Consolidated List](https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list) | - | - | - | Waiting for first run |
<!-- STATUS:END -->

## Latest changes

<!-- LATEST:START -->
_No changes logged yet. The first run saves a baseline; changes show up from the next run on._
<!-- LATEST:END -->

Full log: [`data/changes.csv`](data/changes.csv) · Where listings are concentrated, by country and program: [`data/coverage.md`](data/coverage.md)

## Why these four lists

- **US (OFAC SDN List):** the list most global companies screen against, whatever country they're in.
- **UK (FCDO UK Sanctions List):** the single official UK list since the OFSI Consolidated List closed on 28 January 2026.
- **Australia (DFAT Consolidated List):** covers Australia's autonomous sanctions plus the UN listings it implements.
- **UN Security Council Consolidated List:** most countries without their own autonomous regime, including much of Latin America, implement UN sanctions. Tracking the UN list is the most accurate way to cover them. There's no single "LATAM sanctions list."

## What's in the data

`data/changes.csv`, one row per change:

| Column | What it is |
|---|---|
| `detected_on` | The date this tracker spotted the change |
| `source` | `us_ofac`, `uk_fcdo`, `au_dfat` or `un_sc` |
| `change` | Added, Removed, or Amended (name or program changed) |
| `entry_id` | The regulator's own ID: OFAC ent_num, UK Unique ID, DFAT Reference, UN reference number |
| `name` | Primary name (aliases are folded into the primary entry) |
| `entry_type` | Individual, Entity, Vessel, etc. |
| `programs` | Sanctions program(s) or regime(s) |
| `detail` | For amendments, what changed |

`data/snapshots/` holds the latest full copy of each list in the same shape, so you can see exactly what the tracker compared against.

## How it works

```
OFAC SDN.CSV ──────────┐
UK Sanctions List CSV ─┤
DFAT Consolidated XLSX ┼─> sources.py ─> tracker.py ─> snapshots + changes.csv ─> README tables
UN Consolidated XML ───┘                     ▲
                                             └── GitHub Action, every weekday
```

1. [`sanctions_tracker/sources.py`](sanctions_tracker/sources.py) downloads each list and turns it into one row per listed person or entity. Columns are found by header name, not position, so a reordered file doesn't break anything.
2. [`sanctions_tracker/tracker.py`](sanctions_tracker/tracker.py) compares each list to its last snapshot and logs the differences.
3. [`.github/workflows/update.yml`](.github/workflows/update.yml) runs the tests, then the tracker, and commits the results. The "Last checked" date updates every run, so there's a commit each weekday even when no list changed.

## Built-in safety checks

- **One bad source doesn't stop the others.** If a list fails to download or parse, it's flagged in the status table and its previous snapshot is kept.
- **No phantom delistings.** If a list suddenly shrinks by more than half, the tracker treats it as a bad download, not a mass removal, and doesn't log it.
- **The first run is a baseline.** It saves snapshots without logging thousands of "additions."

## Run it yourself

```bash
pip install -r requirements.txt
python -m sanctions_tracker
python -m unittest discover -s tests
```

You can also kick off a run from the **Actions** tab with **Run workflow**.

## Notes

- Public government data only. Nothing here comes from any employer or non-public source.
- This is a monitoring convenience, not a screening tool and not legal advice. Screening decisions should always be made against the official list and with the full identifying details.
- The country view in `coverage.md` shows where listings are concentrated (by nationality, citizenship or address). It is not a list of embargoed countries.
- Possible next additions: the EU Financial Sanctions File and OFAC's non-SDN Consolidated List.

## License

MIT
