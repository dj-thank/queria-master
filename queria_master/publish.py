from __future__ import annotations

"""Generation-gated publication of the runtime DB and its search index."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .enrichment import DEFAULT_ENRICHMENT_DB, _WriterLock
from .resources import DEFAULT_DB
from .runtime import DEFAULT_RUNTIME_DB, build_runtime_database
from .search_index import DEFAULT_SEARCH_INDEX, SearchIndex, build_search_index


class PublishError(RuntimeError):
    """A runtime/index generation could not be validated or promoted."""


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def publish_runtime_bundle(
    database_path: Path = DEFAULT_DB,
    *,
    enrichment_path: Path = DEFAULT_ENRICHMENT_DB,
    runtime_path: Path = DEFAULT_RUNTIME_DB,
    search_index_path: Path = DEFAULT_SEARCH_INDEX,
    threads: int = 4,
    memory_limit: str = "8GB",
    batch_size: int = 20_000,
) -> dict[str, Any]:
    """Build and validate a complete generation before exposing either file.

    Promotion uses two atomic replacements.  During the very short interval
    between them, generation validation makes every reader reject the stale
    index and fall back to DuckDB; a mixed pair is therefore never queried.
    """

    database_path = Path(database_path).resolve()
    enrichment_path = Path(enrichment_path).resolve()
    runtime_path = Path(runtime_path).resolve()
    search_index_path = Path(search_index_path).resolve()
    artifacts = {
        "canonical DB": database_path,
        "enrichment DB": enrichment_path,
        "runtime DB": runtime_path,
        "検索索引": search_index_path,
    }
    if len(set(artifacts.values())) != len(artifacts):
        duplicates = sorted(
            str(path)
            for path in set(artifacts.values())
            if list(artifacts.values()).count(path) > 1
        )
        raise PublishError(
            "canonical/enrichment/runtime/検索索引はすべて別ファイルにしてください: "
            + ", ".join(duplicates)
        )
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    search_index_path.parent.mkdir(parents=True, exist_ok=True)
    publish_lock = _WriterLock(runtime_path, timeout_seconds=120.0)
    canonical_lock = _WriterLock(database_path, timeout_seconds=120.0)
    enrichment_lock = _WriterLock(enrichment_path, timeout_seconds=120.0)
    publish_locked = False
    canonical_locked = False
    enrichment_locked = False
    runtime_staging_root: Path | None = None
    index_staging_root: Path | None = None
    try:
        publish_lock.acquire()
        publish_locked = True
        canonical_lock.acquire()
        canonical_locked = True
        enrichment_lock.acquire()
        enrichment_locked = True
        runtime_staging_root = Path(
            tempfile.mkdtemp(prefix="queria-publish-runtime-", dir=str(runtime_path.parent))
        )
        index_staging_root = Path(
            tempfile.mkdtemp(prefix="queria-publish-index-", dir=str(search_index_path.parent))
        )
        staged_runtime = runtime_staging_root / runtime_path.name
        staged_index = index_staging_root / search_index_path.name
        runtime_stats = build_runtime_database(
            database_path,
            enrichment_path,
            staged_runtime,
            threads=threads,
            memory_limit=memory_limit,
        )
        index_stats = build_search_index(staged_runtime, staged_index, batch_size=batch_size)
        with SearchIndex(staged_index, database_path=staged_runtime) as index:
            index_metadata = dict(index.metadata)
        generation_id = str(runtime_stats.get("generation_id") or "")
        if not generation_id or index_metadata.get("runtime_generation_id") != generation_id:
            raise PublishError("staging runtime/indexのgeneration_idが一致しません。")

        receipt = {
            "generation_id": generation_id,
            "runtime_database": str(runtime_path),
            "search_index": str(search_index_path),
            "runtime": runtime_stats,
            "index": index_stats,
        }
        receipt_path = search_index_path.with_suffix(search_index_path.suffix + ".generation.json")
        receipt_tmp = index_staging_root / receipt_path.name
        receipt_tmp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        previous_runtime = runtime_staging_root / (runtime_path.name + ".previous")
        had_runtime = runtime_path.is_file()
        if had_runtime:
            try:
                os.link(runtime_path, previous_runtime)
            except OSError as exc:
                raise PublishError("旧runtimeのno-clobber rollback linkを作成できません。") from exc

        # runtime first is intentional: readers holding the previous index see
        # a generation mismatch and fail closed until the second replace.
        os.replace(staged_runtime, runtime_path)
        _fsync_directory(runtime_path.parent)
        try:
            os.replace(staged_index, search_index_path)
            _fsync_directory(search_index_path.parent)
        except OSError as exc:
            try:
                if had_runtime:
                    os.replace(previous_runtime, runtime_path)
                else:
                    runtime_path.unlink(missing_ok=True)
                _fsync_directory(runtime_path.parent)
            except OSError as rollback_exc:
                raise PublishError(
                    "検索索引の公開に失敗し、旧runtimeのrollbackにも失敗しました。"
                ) from rollback_exc
            raise PublishError("検索索引の公開に失敗したため旧runtimeへrollbackしました。") from exc
        try:
            os.replace(receipt_tmp, receipt_path)
            _fsync_directory(receipt_path.parent)
            receipt["receipt_status"] = "published"
        except OSError as exc:
            # The runtime/index pair is already generation-validated and is
            # the operational contract.  A diagnostic receipt failure must
            # not report the valid pair as a failed publication.
            receipt["receipt_status"] = "warning"
            receipt["receipt_error"] = str(exc)
        return receipt
    finally:
        if runtime_staging_root is not None:
            shutil.rmtree(runtime_staging_root, ignore_errors=True)
        if index_staging_root is not None:
            shutil.rmtree(index_staging_root, ignore_errors=True)
        if enrichment_locked:
            enrichment_lock.release()
        if canonical_locked:
            canonical_lock.release()
        if publish_locked:
            publish_lock.release()


__all__ = ["PublishError", "publish_runtime_bundle"]
