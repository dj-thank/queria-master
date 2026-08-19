from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

from .resources import PROJECT_ROOT


SEMANTIC_INDEX_VERSION = "1"
DEFAULT_SEMANTIC_INDEX = PROJECT_ROOT / "data" / "semantic_index"
_TEXT_FIELDS = ("company_name", "full_address", "business_summary", "business_items_raw", "company_url")


class SemanticIndexError(RuntimeError):
    """Semantic index is unavailable, stale, malformed, or missing an optional dependency."""


class EmbeddingProvider(Protocol):
    model_name: str

    def encode(self, texts: Sequence[str]) -> Any:
        """Return one numeric vector per input text."""


class SentenceTransformerProvider:
    """Optional SentenceTransformers adapter.

    The dependency is imported only when semantic indexing/search is requested;
    the standard keyword index remains dependency-free beyond the base project.
    """

    def __init__(self, model_name: str, *, device: str | None = None) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on user extras.
            raise SemanticIndexError(
                "埋め込み検索には任意依存が必要です。"
                " `pip install -e \".[semantic]\"` を実行してください。"
            ) from exc
        self.model_name = str(model_name)
        kwargs = {} if device is None else {"device": device}
        self._model = SentenceTransformer(self.model_name, **kwargs)

    def encode(self, texts: Sequence[str]) -> Any:
        return self._model.encode(
            list(texts),
            batch_size=64,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )


def _paths(prefix: Path) -> tuple[Path, Path, Path]:
    prefix = Path(prefix)
    return (
        prefix.with_name(prefix.name + ".meta.json"),
        prefix.with_name(prefix.name + ".vectors.bin"),
        prefix.with_name(prefix.name + ".doc_ids.bin"),
    )


def _readonly_sqlite(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SemanticIndexError(f"検索索引がありません: {path}")
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _text_value(row: sqlite3.Row) -> str:
    values = [str(row[field]).strip() for field in _TEXT_FIELDS if row[field] not in (None, "")]
    return "\n".join(values)


def _numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - depends on user extras.
        raise SemanticIndexError(
            "高速な埋め込み検索には NumPy が必要です。"
            " `pip install -e \".[semantic]\"` を実行してください。"
        ) from exc
    return np


def _normalise_rows(vectors: Any, *, dimension: int, dtype: str) -> bytes:
    np = None
    try:
        np = _numpy()
    except SemanticIndexError:
        pass
    if np is not None:
        array = np.asarray(vectors, dtype=np.float32)
        if array.ndim != 2 or array.shape[1] != dimension:
            raise SemanticIndexError(
                f"埋め込み次元が一致しません: expected={dimension}, actual={getattr(array, 'shape', None)}"
            )
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        array = array / norms
        target = np.float16 if dtype == "float16" else np.float32
        return np.asarray(array, dtype=target).tobytes(order="C")

    if len(vectors) == 0:
        return b""
    number_format = "e" if dtype == "float16" else "f"
    row_format = f"<{dimension}{number_format}"
    payload = bytearray()
    for vector in vectors:
        values = [float(value) for value in vector]
        if len(values) != dimension:
            raise SemanticIndexError(f"埋め込み次元が一致しません: expected={dimension}, actual={len(values)}")
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        payload.extend(struct.pack(row_format, *(value / norm for value in values)))
    return bytes(payload)


def build_semantic_index(
    *,
    search_index_path: Path,
    output_prefix: Path = DEFAULT_SEMANTIC_INDEX,
    model: EmbeddingProvider,
    batch_size: int = 256,
    dtype: str = "float16",
    min_text_chars: int = 16,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build a compact, memory-mapped vector index over text-rich companies.

    This intentionally does not create a dense vector for all 5.8m companies by
    default.  Rows without usable descriptive text are skipped, and query-time
    callers can first narrow candidates with the SQLite FTS/category index.
    """

    if batch_size < 1:
        raise ValueError("batch_size は1以上で指定してください。")
    if dtype not in {"float16", "float32"}:
        raise ValueError("dtype は float16 または float32 です。")
    if min_text_chars < 0:
        raise ValueError("min_text_chars は0以上で指定してください。")
    if limit is not None and limit < 1:
        raise ValueError("limit は1以上で指定してください。")

    output_prefix = Path(output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    metadata_path, vectors_path, doc_ids_path = _paths(output_prefix)
    connection = _readonly_sqlite(Path(search_index_path))
    temporary = Path(tempfile.mkdtemp(prefix=f"{output_prefix.name}-", dir=output_prefix.parent))
    temp_prefix = temporary / output_prefix.name
    temp_metadata, temp_vectors, temp_doc_ids = _paths(temp_prefix)
    row_count = 0
    dimension: int | None = None
    try:
        source_metadata = dict(
            connection.execute("SELECT key, value FROM index_metadata").fetchall()
        )
        if source_metadata.get("index_version") is None:
            raise SemanticIndexError("検索索引のメタデータが不正です。")
        query = """
            SELECT doc_id, company_name, full_address, business_summary,
                   business_items_raw, company_url
            FROM company_docs
            ORDER BY doc_id
        """
        cursor = connection.execute(query)
        with temp_vectors.open("wb") as vector_file, temp_doc_ids.open("wb") as doc_id_file:
            while True:
                source_rows = cursor.fetchmany(batch_size)
                if not source_rows:
                    break
                texts: list[str] = []
                doc_ids: list[int] = []
                for row in source_rows:
                    text = _text_value(row)
                    if len(text) < min_text_chars:
                        continue
                    texts.append(text)
                    doc_ids.append(int(row["doc_id"]))
                    if limit is not None and row_count + len(doc_ids) >= limit:
                        break
                if not texts:
                    if limit is not None and row_count >= limit:
                        break
                    continue
                vectors = model.encode(texts)
                if len(vectors) != len(texts):
                    raise SemanticIndexError(
                        f"モデルの出力行数が不一致です: texts={len(texts)}, vectors={len(vectors)}"
                    )
                if dimension is None:
                    try:
                        dimension = int(len(vectors[0]))
                    except (IndexError, TypeError) as exc:
                        raise SemanticIndexError("モデルが空のベクトルを返しました。") from exc
                vector_file.write(_normalise_rows(vectors, dimension=dimension, dtype=dtype))
                doc_id_file.write(struct.pack(f"<{len(doc_ids)}q", *doc_ids))
                row_count += len(doc_ids)
                if limit is not None and row_count >= limit:
                    break
        if dimension is None or row_count == 0:
            raise SemanticIndexError("埋め込み対象の法人がありません。min_text_charsを下げてください。")
        vector_bytes = 2 if dtype == "float16" else 4
        expected_vector_bytes = row_count * dimension * vector_bytes
        if temp_vectors.stat().st_size != expected_vector_bytes:
            raise SemanticIndexError("ベクトルファイルのサイズ検証に失敗しました。")
        metadata = {
            "semantic_index_version": SEMANTIC_INDEX_VERSION,
            "search_index_version": source_metadata.get("index_version"),
            "search_index_refresh_id": source_metadata.get("refresh_id"),
            "search_index": str(Path(search_index_path).resolve()),
            "model_name": str(getattr(model, "model_name", type(model).__name__)),
            "dimension": dimension,
            "row_count": row_count,
            "dtype": dtype,
            "metric": "cosine",
            "text_fields": list(_TEXT_FIELDS),
            "min_text_chars": min_text_chars,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        temp_metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # The metadata file is replaced last, so a reader never accepts a new
        # generation before its vector and doc-id payloads are present.
        os.replace(temp_vectors, vectors_path)
        os.replace(temp_doc_ids, doc_ids_path)
        os.replace(temp_metadata, metadata_path)
        return {
            "output_prefix": str(output_prefix.resolve()),
            "metadata": metadata,
            "vector_bytes": vectors_path.stat().st_size,
            "doc_id_bytes": doc_ids_path.stat().st_size,
        }
    finally:
        connection.close()
        for child in temporary.glob("*"):
            child.unlink(missing_ok=True)
        temporary.rmdir()


class SemanticIndex:
    def __init__(
        self,
        prefix: Path = DEFAULT_SEMANTIC_INDEX,
        *,
        search_index_path: Path | None = None,
    ) -> None:
        self.prefix = Path(prefix)
        self.metadata_path, self.vectors_path, self.doc_ids_path = _paths(self.prefix)
        if not self.metadata_path.is_file() or not self.vectors_path.is_file() or not self.doc_ids_path.is_file():
            raise SemanticIndexError(
                f"埋め込み索引が揃っていません: {self.metadata_path}, {self.vectors_path}, {self.doc_ids_path}"
            )
        self.metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        if self.metadata.get("semantic_index_version") != SEMANTIC_INDEX_VERSION:
            raise SemanticIndexError("埋め込み索引のバージョンが一致しません。再構築してください。")
        if search_index_path is not None:
            source = _readonly_sqlite(Path(search_index_path))
            try:
                current = dict(source.execute("SELECT key, value FROM index_metadata").fetchall())
            finally:
                source.close()
            if (
                current.get("index_version") != self.metadata.get("search_index_version")
                or current.get("refresh_id") != self.metadata.get("search_index_refresh_id")
            ):
                raise SemanticIndexError(
                    "埋め込み索引が検索索引より古いです。build-semantic-indexを再実行してください。"
                )
        self.dimension = int(self.metadata["dimension"])
        self.row_count = int(self.metadata["row_count"])
        self.dtype = str(self.metadata["dtype"])
        if self.dtype not in {"float16", "float32"}:
            raise SemanticIndexError("埋め込み索引のdtypeが不正です。")
        np = _numpy()
        numpy_dtype = np.float16 if self.dtype == "float16" else np.float32
        expected_vectors = self.row_count * self.dimension * np.dtype(numpy_dtype).itemsize
        if self.vectors_path.stat().st_size != expected_vectors:
            raise SemanticIndexError("ベクトルファイルが壊れています。")
        if self.doc_ids_path.stat().st_size != self.row_count * 8:
            raise SemanticIndexError("doc_idファイルが壊れています。")
        self._np = np
        self._vectors = np.memmap(
            self.vectors_path,
            dtype=numpy_dtype,
            mode="r",
            shape=(self.row_count, self.dimension),
        )
        self._doc_ids = np.memmap(self.doc_ids_path, dtype="<i8", mode="r", shape=(self.row_count,))

    def close(self) -> None:
        if hasattr(self, "_vectors"):
            del self._vectors
        if hasattr(self, "_doc_ids"):
            del self._doc_ids

    def __enter__(self) -> "SemanticIndex":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def search(
        self,
        query: str,
        model: EmbeddingProvider,
        *,
        top_k: int = 100,
        candidate_doc_ids: Sequence[int] | None = None,
        chunk_size: int = 50_000,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError("検索文を指定してください。")
        if not 1 <= top_k <= 100_000:
            raise ValueError("top_k は1〜100000の範囲で指定してください。")
        if chunk_size < 1:
            raise ValueError("chunk_size は1以上で指定してください。")
        encoded = model.encode([query])
        array = self._np.asarray(encoded, dtype=self._np.float32)
        if array.ndim != 2 or array.shape != (1, self.dimension):
            raise SemanticIndexError(
                f"クエリ埋め込みの次元が不一致です: expected={(1, self.dimension)}, actual={array.shape}"
            )
        vector = array[0]
        norm = float(self._np.linalg.norm(vector)) or 1.0
        vector = vector / norm
        if candidate_doc_ids is None:
            positions = None
            count = self.row_count
        else:
            candidates = self._np.fromiter({int(value) for value in candidate_doc_ids}, dtype=self._np.int64)
            if candidates.size == 0:
                return []
            positions = self._np.flatnonzero(self._np.isin(self._doc_ids, candidates))
            count = int(positions.size)
        best_scores: list[Any] = []
        best_ids: list[Any] = []
        for start in range(0, count, chunk_size):
            stop = min(start + chunk_size, count)
            selected = positions[start:stop] if positions is not None else slice(start, stop)
            scores = self._np.asarray(self._vectors[selected], dtype=self._np.float32) @ vector
            ids = self._doc_ids[selected]
            if scores.size > top_k:
                local = self._np.argpartition(scores, -top_k)[-top_k:]
                scores = scores[local]
                ids = ids[local]
            best_scores.extend(scores.tolist())
            best_ids.extend(ids.tolist())
        if not best_scores:
            return []
        scores_array = self._np.asarray(best_scores, dtype=self._np.float32)
        ids_array = self._np.asarray(best_ids, dtype=self._np.int64)
        keep = min(top_k, int(scores_array.size))
        if scores_array.size > keep:
            selected = self._np.argpartition(scores_array, -keep)[-keep:]
            scores_array = scores_array[selected]
            ids_array = ids_array[selected]
        order = self._np.argsort(-scores_array, kind="stable")
        return [
            {"doc_id": int(ids_array[index]), "score": float(scores_array[index])}
            for index in order
        ]


def hydrate_semantic_hits(search_index_path: Path, hits: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join vector hits back to the compact company records in SQLite."""

    if not hits:
        return []
    connection = _readonly_sqlite(Path(search_index_path))
    try:
        by_id = {int(hit["doc_id"]): float(hit["score"]) for hit in hits}
        ids = list(by_id)
        records: dict[int, dict[str, Any]] = {}
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = connection.execute(
                f"""
                SELECT doc_id, corporate_number, company_name, prefecture_name, city_name,
                       jsic_major_codes, jsic_middle_codes, employee_number, capital_stock,
                       representative_name, company_url, business_summary
                FROM company_docs WHERE doc_id IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in rows:
                item = dict(row)
                item["semantic_score"] = by_id[int(item.pop("doc_id"))]
                records[int(row["doc_id"])] = item
        return [records[int(hit["doc_id"])] for hit in hits if int(hit["doc_id"]) in records]
    finally:
        connection.close()


def doc_ids_for_corporate_numbers(search_index_path: Path, corporate_numbers: Sequence[str]) -> list[int]:
    """Resolve FTS candidate corporate numbers to semantic-index doc_ids."""

    if not corporate_numbers:
        return []
    connection = _readonly_sqlite(Path(search_index_path))
    try:
        result: list[int] = []
        values = [str(value) for value in corporate_numbers]
        for start in range(0, len(values), 500):
            batch = values[start : start + 500]
            placeholders = ", ".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT doc_id FROM company_docs WHERE corporate_number IN ({placeholders})",
                batch,
            ).fetchall()
            result.extend(int(row["doc_id"]) for row in rows)
        return result
    finally:
        connection.close()


__all__ = [
    "DEFAULT_SEMANTIC_INDEX",
    "EmbeddingProvider",
    "SEMANTIC_INDEX_VERSION",
    "SemanticIndex",
    "SemanticIndexError",
    "SentenceTransformerProvider",
    "build_semantic_index",
    "doc_ids_for_corporate_numbers",
    "hydrate_semantic_hits",
]
