import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

from sanctions_tracker import sources as src
from sanctions_tracker import tracker as t

FIX = Path(__file__).parent / "fixtures"


def au_rows():
    return [
        ["Australian Sanctions Consolidated List"],
        ["Reference", "Type", "Name of Individual or Entity", "Name Type", "Alias Strength",
         "Date of Birth", "Place of Birth", "Citizenship", "Address", "Additional Information",
         "Listing Information", "IMO Number", "Committees", "Control Date"],
        ["1000", "Individual", "Ivan PETROV", "Primary Name", "", "", "", "Russia", "", "", "", "",
         "Autonomous (Russia)", "2026-10-01"],
        ["1000a", "Individual", "Ivan PETROFF", "Alias", "Strong", "", "", "Russia", "", "", "", "",
         "Autonomous (Russia)", "2026-10-01"],
        ["1001", "Entity", "EXAMPLE TRADING CORPORATION", "Primary Name", "", "", "", "", "", "", "",
         "", "1718 (DPRK)", "2026-10-02"],
    ]


class ParserTests(unittest.TestCase):
    def test_ofac(self):
        entries = src.parse_ofac((FIX / "ofac_sdn.csv").read_text(),
                                 (FIX / "ofac_add.csv").read_text())
        by_id = {e.entry_id: e for e in entries}
        self.assertEqual(len(entries), 3)  # trailing EOF marker ignored
        self.assertEqual(by_id["36"].entry_type, "Entity")
        self.assertEqual(by_id["173"].programs, "SDGT; IRGC")
        self.assertEqual(by_id["173"].countries, "Iran; United Arab Emirates")
        self.assertEqual(by_id["306"].entry_type, "Vessel")

    def test_uk_groups_aliases_and_prefers_primary_name(self):
        entries = src.parse_uk((FIX / "uk.csv").read_text())
        by_id = {e.entry_id: e for e in entries}
        self.assertEqual(len(entries), 3)
        self.assertEqual(by_id["RUS0001"].name, "Ivan PETROV")
        self.assertEqual(by_id["CYB0003"].name, "HACKER GROUP")  # alias came first
        self.assertEqual(by_id["IRN0002"].programs, "Iran")
        self.assertEqual(by_id["RUS0001"].countries, "Russia")

    def test_uk_missing_header_raises(self):
        with self.assertRaises(src.ParseError):
            src.parse_uk("just,some,columns\n1,2,3\n")

    def test_au_rows_and_xlsx(self):
        entries = src.parse_au_rows(au_rows())
        by_id = {e.entry_id: e for e in entries}
        self.assertEqual(len(entries), 2)
        self.assertEqual(by_id["1000"].name, "Ivan PETROV")
        self.assertEqual(by_id["1001"].programs, "1718 (DPRK)")

        wb = Workbook()
        for row in au_rows():
            wb.active.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        self.assertEqual(len(src.parse_au_xlsx(buf.getvalue())), 2)

    def test_un(self):
        entries = src.parse_un((FIX / "un.xml").read_bytes())
        by_id = {e.entry_id: e for e in entries}
        self.assertEqual(len(entries), 3)
        self.assertEqual(by_id["QDi.900"].name, "ABDUL EXAMPLE")
        self.assertEqual(by_id["KPe.900"].entry_type, "Entity")
        self.assertIn("Korea", by_id["KPe.900"].countries)

    def test_find_link(self):
        html = '<a href="/docs/old.pdf">PDF</a><a href="/sites/x/Consolidated-List.xlsx?v=2">XLSX</a>'
        self.assertEqual(src.find_link(html, "https://www.dfat.gov.au/a/b", ".xlsx", "consolidated"),
                         "https://www.dfat.gov.au/sites/x/Consolidated-List.xlsx?v=2")


class TrackerTests(unittest.TestCase):
    def test_diff(self):
        old = [src.Entry("1", "A", "Entity", "IRAN"), src.Entry("2", "B", "Entity", "CUBA")]
        new = [src.Entry("1", "A", "Entity", "IRAN; SDGT"), src.Entry("3", "C", "Individual", "RUSSIA")]
        changes = t.diff("us_ofac", old, new, "2026-11-09")
        kinds = {(c.change, c.entry_id) for c in changes}
        self.assertEqual(kinds, {("Added", "3"), ("Removed", "2"), ("Amended", "1")})
        amended = [c for c in changes if c.change == "Amended"][0]
        self.assertIn("IRAN -> IRAN; SDGT", amended.detail)

    def test_capitalization_only_changes_are_ignored(self):
        old = [src.Entry("GHR0179", "Zhi Chen", "Individual", "Global Human Rights")]
        new = [src.Entry("GHR0179", "Zhi  CHEN", "Individual", "Global Human Rights")]
        self.assertEqual(t.diff("uk_fcdo", old, new, "2026-11-14"), [])

    def test_au_falls_back_to_mirror_when_dfat_blocks(self):
        wb = Workbook()
        for row in au_rows():
            wb.active.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        xlsx = buf.getvalue()

        def fake_fetch(url, binary=False, browser=False, attempts=3):
            if "dfat.gov.au" in url:
                raise src.requests.ReadTimeout("blocked")
            self.assertEqual(url, src.AU_MIRROR_XLSX)
            return xlsx

        with mock.patch.object(src, "fetch", fake_fetch):
            entries = src.fetch_au()
        self.assertEqual(len(entries), 2)
        self.assertEqual(src.ROUTES["au_dfat"], "mirror")
        status = t.Status("au_dfat", "AU", "DFAT", "https://x", "2", "d", "", "ok-mirror")
        self.assertIn("OpenSanctions mirror", t.status_table([status]))

    def test_shrink_guard(self):
        old = [src.Entry(str(i), "x", "Entity", "P") for i in range(10)]
        with self.assertRaises(RuntimeError):
            t.check_shrink(old, old[:4])
        t.check_shrink(old, old[:6])  # normal churn is fine

    def test_full_run_baseline_then_changes_and_failure(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            readme = d / "README.md"
            readme.write_text("x\n<!-- STATUS:START -->\n<!-- STATUS:END -->\n"
                              "<!-- LATEST:START -->\n<!-- LATEST:END -->\n")
            day1 = [src.Entry("1", "A", "Entity", "IRAN", "Iran")]
            day2 = [src.Entry("1", "A", "Entity", "IRAN", "Iran"),
                    src.Entry("2", "B | Co", "Entity", "CUBA", "Cuba")]
            feed = {"data": day1}

            def good():
                return feed["data"]

            def bad():
                raise src.ParseError("layout changed")

            sources = [src.Source("us_ofac", "US", "OFAC", "https://x", good),
                       src.Source("uk_fcdo", "UK", "FCDO", "https://y", bad)]
            patches = [mock.patch.object(t, "SOURCES", sources),
                       mock.patch.object(t, "DATA", d / "data"),
                       mock.patch.object(t, "SNAPSHOTS", d / "data" / "snapshots"),
                       mock.patch.object(t, "CHANGES_PATH", d / "data" / "changes.csv"),
                       mock.patch.object(t, "STATUS_PATH", d / "data" / "status.csv"),
                       mock.patch.object(t, "COVERAGE_PATH", d / "data" / "coverage.md"),
                       mock.patch.object(t, "README_PATH", readme)]
            for p in patches:
                p.start()
            self.addCleanup(mock.patch.stopall)
            # update_readme's default path was bound at import time
            orig = t.update_readme
            mock.patch.object(t, "update_readme",
                              lambda s, c: orig(s, c, readme)).start()

            self.assertEqual(t.run(), 0)  # one source failing isn't fatal
            self.assertEqual(t.read_csv(d / "data" / "changes.csv", t.Change), [])  # baseline

            feed["data"] = day2
            t.run()
            changes = t.read_csv(d / "data" / "changes.csv", t.Change)
            self.assertEqual([(c.change, c.entry_id) for c in changes], [("Added", "2")])
            text = readme.read_text()
            self.assertIn("| Added | B / Co |", text)
            self.assertIn("Needs a look: ParseError", text)
            self.assertIn("| Iran | 1 |", (d / "data" / "coverage.md").read_text())


if __name__ == "__main__":
    unittest.main()
