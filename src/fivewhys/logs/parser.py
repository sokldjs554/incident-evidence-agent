"""로그 라인 파서.

표준 포맷:
    2026-07-03T14:22:05+09:00 order-service ERROR [db-pool] Connection is not available

포맷이 맞지 않는 라인도 버리지 않고 message 전체를 담은 레코드로 파싱한다
(실무 로그는 항상 지저분하다는 전제).
"""

from __future__ import annotations

import re
from datetime import datetime

from fivewhys.models import KST, LogRecord

_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?)\s+"
    r"(?P<service>[\w.-]+)\s+"
    r"(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\s+"
    r"(?:\[(?P<component>[^\]]*)\]\s+)?"
    r"(?P<message>.*)$"
)

_LEVEL_ALIASES = {"WARNING": "WARN", "CRITICAL": "FATAL"}


def _parse_ts(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=KST)
    return ts


def parse_line(line: str, default_ts: datetime | None = None) -> LogRecord | None:
    """한 줄을 LogRecord로 파싱한다. 빈 줄이면 None."""
    line = line.rstrip("\n")
    if not line.strip():
        return None

    m = _LINE_RE.match(line)
    if m is None:
        # 포맷 불일치 → 원문 보존 (unknown 서비스로 분류)
        return LogRecord(
            ts=default_ts or datetime.now(tz=KST),
            service="unknown",
            level="INFO",
            component="",
            message=line.strip(),
        )

    level = m.group("level")
    return LogRecord(
        ts=_parse_ts(m.group("ts")),
        service=m.group("service"),
        level=_LEVEL_ALIASES.get(level, level),
        component=m.group("component") or "",
        message=m.group("message").strip(),
    )


def format_line(rec: LogRecord) -> str:
    """LogRecord를 표준 포맷 문자열로 직렬화한다 (시나리오 내보내기용)."""
    comp = f"[{rec.component}] " if rec.component else ""
    return f"{rec.ts.isoformat()} {rec.service} {rec.level} {comp}{rec.message}"
