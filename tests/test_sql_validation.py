import unittest

from queria_master.pipeline import PipelineError
from queria_master.query import normalize_read_only_sql


class SqlValidationTests(unittest.TestCase):
    def test_comments_and_commented_semicolons_are_ignored(self):
        sql = """
        -- SELECT 2;
        SELECT ';' AS literal_value; -- another ; in a comment
        """
        self.assertEqual(normalize_read_only_sql(sql), "SELECT ';' AS literal_value")

    def test_multiple_statements_are_rejected(self):
        with self.assertRaises(PipelineError):
            normalize_read_only_sql("SELECT 1; SELECT 2")

    def test_write_inside_cte_is_rejected(self):
        with self.assertRaises(PipelineError):
            normalize_read_only_sql("WITH x AS (DELETE FROM core.companies RETURNING *) SELECT * FROM x")

    def test_keyword_inside_string_is_allowed(self):
        self.assertEqual(
            normalize_read_only_sql("SELECT 'DELETE FROM x' AS text"),
            "SELECT 'DELETE FROM x' AS text",
        )

    def test_unclosed_block_comment_is_rejected(self):
        with self.assertRaises(PipelineError):
            normalize_read_only_sql("SELECT 1 /*")


if __name__ == "__main__":
    unittest.main()
