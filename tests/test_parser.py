from datetime import datetime

from fivewhys.logs.parser import format_line, parse_line
from fivewhys.models import KST, LogRecord


def test_parse_standard_line():
    rec = parse_line(
        "2026-07-03T14:22:05+09:00 order-service ERROR [db-pool] Connection is not available"
    )
    assert rec.service == "order-service"
    assert rec.level == "ERROR"
    assert rec.component == "db-pool"
    assert rec.message == "Connection is not available"
    assert rec.ts == datetime(2026, 7, 3, 14, 22, 5, tzinfo=KST)


def test_parse_without_component():
    rec = parse_line("2026-07-03T14:22:05+09:00 mysql WARN Slow query detected")
    assert rec.component == ""
    assert rec.message == "Slow query detected"


def test_level_alias_normalized():
    rec = parse_line("2026-07-03T14:22:05+09:00 svc WARNING something")
    assert rec.level == "WARN"


def test_malformed_line_preserved():
    rec = parse_line("kernel: BUG at mm/slab.c:123")
    assert rec.service == "unknown"
    assert "BUG at" in rec.message


def test_empty_line_returns_none():
    assert parse_line("   \n") is None


def test_roundtrip_format_parse():
    original = LogRecord(
        ts=datetime(2026, 7, 3, 14, 0, tzinfo=KST),
        service="api-gateway",
        level="INFO",
        component="proxy",
        message="Request completed status=200",
    )
    parsed = parse_line(format_line(original))
    assert parsed.service == original.service
    assert parsed.level == original.level
    assert parsed.component == original.component
    assert parsed.message == original.message
    assert parsed.ts == original.ts
