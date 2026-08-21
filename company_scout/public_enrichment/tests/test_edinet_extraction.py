from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import edinet_salary_enricher as edinet


class EdinetExtractionTests(unittest.TestCase):
    @staticmethod
    def make_zip(xml: str, filename: str = "XBRL/PublicDoc/report.xbrl") -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(filename, xml)
        return output.getvalue()

    def test_extracts_age_and_salary(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<xbrl xmlns='http://www.xbrl.org/2003/instance' xmlns:jppfs='urn:test'>
  <jppfs:AverageAgeYearsInformationAboutReportingCompanyInformationAboutEmployees contextRef='CurrentYearNonConsolidated'>41.2</jppfs:AverageAgeYearsInformationAboutReportingCompanyInformationAboutEmployees>
  <jppfs:AverageAnnualSalaryInformationAboutReportingCompanyInformationAboutEmployees contextRef='CurrentYearNonConsolidated'>8900000</jppfs:AverageAnnualSalaryInformationAboutReportingCompanyInformationAboutEmployees>
</xbrl>"""
        age, salary, debug = edinet.extract_xbrl_metrics(self.make_zip(xml))
        self.assertEqual(age, 41.2)
        self.assertEqual(salary, 8_900_000)
        self.assertEqual(len(debug["files_seen"]), 1)

    def test_rejects_excessive_uncompressed_zip(self) -> None:
        blob = self.make_zip("x" * 1024)
        with self.assertRaises(ValueError):
            edinet.extract_xbrl_metrics(blob, max_total_bytes=100)


if __name__ == "__main__":
    unittest.main()
