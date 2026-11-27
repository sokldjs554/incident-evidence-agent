from datetime import datetime, timedelta

import pytest

from fivewhys.models import KST, DeployEvent, LogRecord
from fivewhys.store.db import LogStore

T0 = datetime(2026, 7, 3, 14, 0, tzinfo=KST)


def _rec(minute: float, service="svc-a", level="INFO", message="hello world"):
    return LogRecord(
        ts=T0 + timedelta(minutes=minute),
        service=service,
        level=level,
        component="app",
        message=message,
    )


@pytest.fixture()
def store():
    s = LogStore()
    yield s
    s.close()


def test_search_ranks_matching_logs(store):
    store.add_logs(
        [
            _rec(1, message="Connection pool exhausted after timeout"),
            _rec(2, message="Request completed status=200"),
            _rec(3, message="Connection opened id=7"),
        ]
    )
    rows = store.search_logs("connection pool")
    assert rows
    assert "pool" in rows[0]["message"].lower()


def test_search_filters(store):
    store.add_logs(
        [
            _rec(1, service="a", level="ERROR", message="disk full error"),
            _rec(2, service="b", level="ERROR", message="disk full error"),
            _rec(3, service="a", level="INFO", message="disk check ok"),
        ]
    )
    rows = store.search_logs("disk", service="a", level="ERROR")
    assert len(rows) == 1
    assert rows[0]["service"] == "a"


def test_search_time_window(store):
    store.add_logs([_rec(1, message="cache miss"), _rec(30, message="cache miss")])
    rows = store.search_logs("cache", start=T0, end=T0 + timedelta(minutes=10))
    assert len(rows) == 1


def test_search_hostile_query_no_crash(store):
    store.add_logs([_rec(1, message="ok")])
    # FTS5 문법 문자를 포함한 질의가 예외 없이 처리돼야 한다
    assert store.search_logs('AND OR NOT "(*)" NEAR/3') == []
    assert store.search_logs("   ") == []


def test_window(store):
    store.add_logs([_rec(i, message=f"line {i}") for i in range(10)])
    target = store.search_logs("line")[0]
    rows = store.get_window(target["id"], before=2, after=2)
    assert 3 <= len(rows) <= 5
    assert any(r["id"] == target["id"] for r in rows)


def test_error_timeline_buckets(store):
    store.add_logs(
        [
            _rec(1, service="a", level="ERROR", message="boom"),
            _rec(1.5, service="a", level="ERROR", message="boom"),
            _rec(6, service="b", level="WARN", message="slow"),
            _rec(2, service="c", level="INFO", message="fine"),  # INFO 제외
        ]
    )
    timeline = store.error_timeline(T0, T0 + timedelta(minutes=10), bucket_minutes=5)
    assert {(t["service"], t["level"]) for t in timeline} == {("a", "ERROR"), ("b", "WARN")}
    a_row = next(t for t in timeline if t["service"] == "a")
    assert a_row["count"] == 2
    assert a_row["window_start_min"] == 0


def test_template_counts_and_info(store):
    recs = [_rec(1, message="oom killed"), _rec(2, message="oom killed")]
    for r in recs:
        r.template_id = 7
    store.add_logs(recs)
    store.set_templates({7: "oom killed"})
    counts = store.template_counts(T0, T0 + timedelta(minutes=5))
    assert counts == {7: 2}
    info = store.template_info(7, T0, T0 + timedelta(minutes=5))
    assert info["template"] == "oom killed"
    assert len(info["example_log_ids"]) == 2


def test_deploys(store):
    store.add_deploys(
        [DeployEvent(ts=T0, service="svc", kind="deploy", description="v1 배포")]
    )
    rows = store.list_deploys()
    assert rows[0]["description"] == "v1 배포"
