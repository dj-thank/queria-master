import re
import unittest

PATTERN = re.compile(r"(^|[|])G((37|38|39|40|41)[0-9]{0,3})?([|]|$)")
CURRENT_BUSINESS_ITEMS_PATTERN = re.compile(r"(^|[|\-])G:")


class IndustryFilterTests(unittest.TestCase):
    def assert_matches(self, value: str) -> None:
        self.assertIsNotNone(PATTERN.search(value), value)

    def assert_not_matches(self, value: str) -> None:
        self.assertIsNone(PATTERN.search(value), value)

    def test_major_only(self):
        self.assert_matches("G")

    def test_middle_codes(self):
        for value in ["G37", "G38", "G39", "G40", "G41"]:
            self.assert_matches(value)

    def test_detail_codes(self):
        for value in ["G37371", "G39391", "A01|G40399|H42", "G41399|R92"]:
            self.assert_matches(value)

    def test_reject_other_industries(self):
        for value in ["", "F36", "H42", "G36", "G42", "AG39", "G390000"]:
            self.assert_not_matches(value)

    def test_current_gbizinfo_business_items_format(self):
        for value in [
            "G:情報通信業",
            "G:情報通信業-40:インターネット附随サービス業-401:",
            "E:製造業-G:情報通信業",
            "E:製造業|G:情報通信業-39:情報サービス業-391:",
        ]:
            self.assertIsNotNone(CURRENT_BUSINESS_ITEMS_PATTERN.search(value), value)

        for value in ["E:製造業", "E:製造業|H:運輸業、郵便業", "H:運輸業、郵便業", "AG:不正な大分類"]:
            self.assertIsNone(CURRENT_BUSINESS_ITEMS_PATTERN.search(value), value)


if __name__ == "__main__":
    unittest.main()
