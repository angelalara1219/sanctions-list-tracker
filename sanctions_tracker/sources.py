"""Download and parse each public sanctions list into a common shape.

Every source returns a list of Entry objects, one per listed person,
entity, vessel or aircraft (aliases are folded into the primary entry).

Sources:
  * US   - OFAC SDN List (SDN.CSV + ADD.CSV for countries)
  * UK   - FCDO UK Sanctions List (the single UK list since 28 Jan 2026)
  * AU   - DFAT Australian Sanctions Consolidated List (XLSX)
  * UN   - UN Security Council Consolidated List (XML)

Parsers find columns by their header names rather than fixed positions, so a
reordered column won't break a run. If a header disappears entirely the
parser raises, and the tracker marks that source as failed instead of
writing bad data.
"""

from __future__ import annotations

import csv
import io
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "sanctions-list-tracker/1.0 (public-data research script)"}

OFAC_SDN_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV"
OFAC_ADD_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADD.CSV"
UK_PAGE_URL = "https://www.gov.uk/government/publications/the-uk-sanctions-list"
UK_FALLBACK_CSV = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv"
AU_PAGE_URL = "https://www.dfat.gov.au/international-relations/security/sanctions/consolidated-list"
UN_XML_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
# OpenSanctions republishes DFAT's original file unchanged (free for
# non-commercial use under CC BY-NC 4.0; see README).
AU_MIRROR_XLSX = "https://data.opensanctions.org/datasets/latest/au_dfat_sanctions/source.xlsx"

# Which route each source used on this run, for the status table.
ROUTES: dict[str, str] = {}


@dataclass
class Entry:
    entry_id: str
    name: str
    entry_type: str
    programs: str
    countries: str = ""


@dataclass
class Source:
    key: str
    label: str
    regulator: str
    url: str
    fetch_and_parse: object = field(repr=False, default=None)


class ParseError(RuntimeError):
    """The source downloaded but didn't look like we expected."""


# ---------------------------------------------------------------- helpers

# Some government sites (DFAT) stall on requests that don't look like a
# browser, so those sources send ordinary browser headers.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch(url: str, binary: bool = False, browser: bool = False, attempts: int = 3):
    headers = BROWSER_HEADERS if browser else HEADERS
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=(30, 180))
            resp.raise_for_status()
            break
        except (requests.Timeout, requests.ConnectionError):
            if attempt == attempts:
                raise
            time.sleep(10 * attempt)
    if binary:
        return resp.content
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def clean(text) -> str:
    return " ".join(str(text or "").split())


def join_unique(values) -> str:
    seen: list[str] = []
    for v in values:
        v = clean(v)
        if v and v not in seen:
            seen.append(v)
    return "; ".join(seen)


def norm_header(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def find_col(headers: list[str], *candidates: str, required: bool = True) -> int | None:
    """Return the index of the first header matching any candidate name.

    Exact (normalised) matches win; otherwise the first header that contains
    the candidate is used.
    """
    normed = [norm_header(h) for h in headers]
    for cand in candidates:
        c = norm_header(cand)
        if c in normed:
            return normed.index(c)
    for cand in candidates:
        c = norm_header(cand)
        for i, h in enumerate(normed):
            if c and c in h:
                return i
    if required:
        raise ParseError(f"missing column, tried: {', '.join(candidates)}")
    return None


def find_link(html: str, base: str, ext: str, hint: str = "") -> str:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text(" ") + " " + href).lower()
        if href.lower().split("?")[0].endswith(ext) and hint in text:
            return urljoin(base, href)
    return ""


# ---------------------------------------------------------------- US: OFAC

def parse_ofac(sdn_csv: str, add_csv: str = "") -> list[Entry]:
    """SDN.CSV has no header row. Columns: ent_num, SDN_Name, SDN_Type,
    Program, ... OFAC writes -0- for empty fields."""
    countries: dict[str, list[str]] = {}
    for row in csv.reader(io.StringIO(add_csv or "")):
        if len(row) >= 5 and row[0].strip().isdigit():
            country = clean(row[4])
            if country and country != "-0-":
                countries.setdefault(row[0].strip(), []).append(country)

    entries = []
    for row in csv.reader(io.StringIO(sdn_csv)):
        if len(row) < 4 or not row[0].strip().isdigit():
            continue
        ent_num = row[0].strip()
        kind = clean(row[2])
        kind = "Entity" if kind in ("", "-0-") else kind.title()
        programs = [p.strip(" []") for p in row[3].split("] [")]
        entries.append(Entry(
            entry_id=ent_num,
            name=clean(row[1]),
            entry_type=kind,
            programs=join_unique(programs),
            countries=join_unique(countries.get(ent_num, [])),
        ))
    if not entries:
        raise ParseError("no SDN rows found")
    return entries


def fetch_ofac() -> list[Entry]:
    try:
        add = fetch(OFAC_ADD_URL)
    except requests.RequestException:
        add = ""  # countries are a nice-to-have; names and programs aren't
    return parse_ofac(fetch(OFAC_SDN_URL), add)


# ---------------------------------------------------------------- UK: FCDO

def parse_uk(csv_text: str) -> list[Entry]:
    """The UK Sanctions List CSV has one row per name (primary names and
    aliases), keyed by Unique ID. A title/date line may sit above the
    header row, so look for the row that contains 'Unique ID'."""
    rows = list(csv.reader(io.StringIO(csv_text.lstrip("﻿"))))
    header_at = next((i for i, r in enumerate(rows[:20])
                      if any(norm_header(c) == "uniqueid" for c in r)), None)
    if header_at is None:
        raise ParseError("no 'Unique ID' header row")
    headers = rows[header_at]
    c_id = find_col(headers, "Unique ID")
    name_cols = [find_col(headers, f"Name {n}", required=False) for n in range(1, 7)]
    if name_cols[5] is None:
        raise ParseError("missing column, tried: Name 6")
    c_type = find_col(headers, "Designation Type", "Individual, Entity, Ship", "Entity Type", "Type")
    c_regime = find_col(headers, "Regime Name", "Regime")
    c_name_type = find_col(headers, "Name Type", required=False)
    c_country = find_col(headers, "Nationality", "Address Country", "Country", required=False)

    grouped: dict[str, dict] = {}
    for r in rows[header_at + 1:]:
        if len(r) <= c_id or not clean(r[c_id]):
            continue
        uid = clean(r[c_id])
        get = lambda i: clean(r[i]) if i is not None and i < len(r) else ""  # noqa: E731
        name = clean(" ".join(get(i) for i in name_cols if i is not None))
        is_primary = c_name_type is None or "primary" in get(c_name_type).lower()
        g = grouped.setdefault(uid, {"name": "", "primary": False, "type": "",
                                     "regimes": [], "countries": []})
        if name and (not g["name"] or (is_primary and not g["primary"])):
            g["name"] = name
            g["primary"] = is_primary
        g["type"] = g["type"] or get(c_type)
        g["regimes"].append(get(c_regime))
        g["countries"].append(get(c_country))

    entries = [Entry(uid, g["name"], g["type"].title(), join_unique(g["regimes"]),
                     join_unique(g["countries"]))
               for uid, g in grouped.items()]
    if not entries:
        raise ParseError("no UK rows found")
    return entries


def fetch_uk() -> list[Entry]:
    url = ""
    try:
        url = find_link(fetch(UK_PAGE_URL), UK_PAGE_URL, ".csv", "sanction")
    except requests.RequestException:
        pass
    return parse_uk(fetch(url or UK_FALLBACK_CSV))


# ---------------------------------------------------------------- AU: DFAT

def parse_au_rows(rows: list[list]) -> list[Entry]:
    """DFAT's list has one row per name. Primary names carry a whole-number
    Reference (e.g. 1000); aliases add a letter (1000a)."""
    header_at = next((i for i, r in enumerate(rows[:20])
                      if any(norm_header(c) == "reference" for c in r)), None)
    if header_at is None:
        raise ParseError("no 'Reference' header row")
    headers = [clean(h) for h in rows[header_at]]
    c_ref = find_col(headers, "Reference")
    c_name = find_col(headers, "Name of Individual or Entity", "Name")
    c_type = find_col(headers, "Type")
    c_name_type = find_col(headers, "Name Type", required=False)
    c_regime = find_col(headers, "Committees", "Regime")
    c_country = find_col(headers, "Citizenship", required=False)

    grouped: dict[str, dict] = {}
    for r in rows[header_at + 1:]:
        get = lambda i: clean(r[i]) if i is not None and i < len(r) else ""  # noqa: E731
        ref = get(c_ref)
        m = re.match(r"(\d+)", ref)
        if not m:
            continue
        base = m.group(1)
        is_primary = ref == base and (c_name_type is None or "primary" in get(c_name_type).lower()
                                      or not get(c_name_type))
        g = grouped.setdefault(base, {"name": "", "type": "", "regimes": [], "countries": []})
        if get(c_name) and (is_primary or not g["name"]):
            g["name"] = get(c_name)
        g["type"] = g["type"] or get(c_type)
        g["regimes"].append(get(c_regime))
        g["countries"].append(get(c_country))

    entries = [Entry(ref, g["name"], g["type"].title(), join_unique(g["regimes"]),
                     join_unique(g["countries"]))
               for ref, g in grouped.items()]
    if not entries:
        raise ParseError("no AU rows found")
    return entries


def parse_au_xlsx(data: bytes) -> list[Entry]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    return parse_au_rows(rows)


def fetch_au() -> list[Entry]:
    """Try DFAT first. DFAT's site often blocks cloud servers like GitHub's, so
    fall back to OpenSanctions' mirror of the same DFAT spreadsheet. The file
    is identical, so the parser and the DFAT reference numbers don't change."""
    try:
        url = find_link(fetch(AU_PAGE_URL, browser=True, attempts=1),
                        AU_PAGE_URL, ".xlsx", "consolidated")
        if not url:
            raise ParseError("couldn't find the Consolidated List .xlsx link on DFAT's page")
        entries = parse_au_xlsx(fetch(url, binary=True, browser=True, attempts=1))
        ROUTES["au_dfat"] = "DFAT"
        return entries
    except (requests.RequestException, ParseError):
        entries = parse_au_xlsx(fetch(AU_MIRROR_XLSX, binary=True))
        ROUTES["au_dfat"] = "mirror"
        return entries


# ---------------------------------------------------------------- UN

def parse_un(xml_text: str | bytes) -> list[Entry]:
    root = ET.fromstring(xml_text)
    entries = []

    def text(el, tag):
        child = el.find(tag)
        return clean(child.text) if child is not None and child.text else ""

    for ind in root.iter("INDIVIDUAL"):
        name = clean(" ".join(text(ind, t) for t in
                              ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")))
        nats = [clean(v.text) for v in ind.findall("./NATIONALITY/VALUE") if v.text]
        entries.append(Entry(text(ind, "REFERENCE_NUMBER") or text(ind, "DATAID"), name,
                             "Individual", text(ind, "UN_LIST_TYPE"), join_unique(nats)))
    for ent in root.iter("ENTITY"):
        countries = [clean(c.text) for c in ent.findall("./ENTITY_ADDRESS/COUNTRY") if c.text]
        entries.append(Entry(text(ent, "REFERENCE_NUMBER") or text(ent, "DATAID"),
                             text(ent, "FIRST_NAME"), "Entity", text(ent, "UN_LIST_TYPE"),
                             join_unique(countries)))
    if not entries:
        raise ParseError("no UN entries found")
    return entries


def fetch_un() -> list[Entry]:
    return parse_un(fetch(UN_XML_URL, binary=True))


# ---------------------------------------------------------------- registry

SOURCES = [
    Source("us_ofac", "US", "OFAC (Treasury) - SDN List",
           "https://sanctionslist.ofac.treas.gov/Home/SdnList", fetch_ofac),
    Source("uk_fcdo", "UK", "FCDO - UK Sanctions List", UK_PAGE_URL, fetch_uk),
    Source("au_dfat", "AU", "DFAT - Australian Sanctions Consolidated List", AU_PAGE_URL, fetch_au),
    Source("un_sc", "UN", "UN Security Council - Consolidated List",
           "https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list", fetch_un),
]
