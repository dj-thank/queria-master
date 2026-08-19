import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from queria_master.semantic_index import build_semantic_index


class FakeEmbeddingProvider:
    model_name = "test-embedding"

    def encode(self, texts):
        return [[float(len(text)), 1.0, 0.0] for text in texts]


class SemanticIndexBuildTests(unittest.TestCase):
    def test_builds_compact_text_rich_vector_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            search_index = root / "search.sqlite"
            output = root / "semantic_index"
            con = sqlite3.connect(search_index)
            try:
                con.executescript(
                    """
                    CREATE TABLE index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO index_metadata VALUES
                        ('index_version', '3'), ('refresh_id', 'refresh-1');
                    CREATE TABLE company_docs (
                        doc_id INTEGER PRIMARY KEY,
                        company_name TEXT,
                        full_address TEXT,
                        business_summary TEXT,
                        business_items_raw TEXT,
                        company_url TEXT
                    );
                    INSERT INTO company_docs VALUES
                        (1, 'A社', '東京都', 'ソフトウェア開発', '', 'https://a.example'),
                        (2, 'B社', '大阪府', '', '', '');
                    """
                )
                con.commit()
            finally:
                con.close()

            stats = build_semantic_index(
                search_index_path=search_index,
                output_prefix=output,
                model=FakeEmbeddingProvider(),
                dtype="float16",
                min_text_chars=1,
            )
            metadata = json.loads((root / "semantic_index.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(stats["metadata"]["row_count"], 2)
            self.assertEqual(metadata["dimension"], 3)
            self.assertEqual(metadata["dtype"], "float16")
            self.assertEqual((root / "semantic_index.vectors.bin").stat().st_size, 2 * 3 * 2)
            self.assertEqual((root / "semantic_index.doc_ids.bin").stat().st_size, 2 * 8)


if __name__ == "__main__":
    unittest.main()
