from __future__ import annotations

import io
import json
from types import SimpleNamespace

from queria_master import resident


def test_resident_session_normalizes_request_and_forces_fast_mode():
    calls = []
    session = resident.ResidentSearchSession.__new__(resident.ResidentSearchSession)
    session.index = SimpleNamespace(
        search=lambda *args, **kwargs: calls.append((args, kwargs)) or [{"company_name": "A"}]
    )

    rows = session.search(
        {
            "keyword": "会社",
            "industry_majors": "G",
            "industry_middles": ["39"],
            "min_employees": "10",
            "limit": 1000,
            "has_web": True,
        }
    )

    assert rows == [{"company_name": "A"}]
    assert calls[0][0] == ("会社",)
    assert calls[0][1]["industry_majors"] == ("G",)
    assert calls[0][1]["industry_middles"] == ("39",)
    assert calls[0][1]["min_employees"] == 10
    assert calls[0][1]["fast"] is True
    assert calls[0][1]["limit"] == 1000


def test_jsonl_protocol_keeps_one_session_and_returns_compact_rows(monkeypatch):
    instances = []

    class FakeSession:
        metadata = {"index_version": "5"}

        def __init__(self, **kwargs):
            instances.append(kwargs)

        def search(self, request):
            assert request["keyword"] == "ソフトウェア"
            return [{"corporate_number": "1", "company_name": "A"}]

        def close(self):
            pass

    monkeypatch.setattr(resident, "ResidentSearchSession", FakeSession)
    input_stream = io.StringIO(
        json.dumps({"op": "ping"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"op": "search", "keyword": "ソフトウェア", "limit": 1000}, ensure_ascii=False)
        + "\n"
        + json.dumps({"op": "shutdown"})
        + "\n"
    )
    output_stream = io.StringIO()

    assert resident.run_jsonl_protocol(input_stream=input_stream, output_stream=output_stream) == 0
    messages = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert [message["op"] for message in messages[:1]] == ["pong"]
    assert messages[1]["rows"][0][0] == "1"
    assert messages[1]["count"] == 1
    assert len(instances) == 1
