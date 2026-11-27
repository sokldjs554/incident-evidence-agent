"""SQLite 기반 로그/문서 저장소.

- 로그 본문 검색: FTS5 가상 테이블 + bm25() 랭킹 (Elasticsearch의 BM25와
  동일한 랭킹 함수 계열 — 운영 환경에서는 store/es.py 어댑터로 교체 가능)
- 포스트모템 청크 검색: 별도 FTS5 테이블
- 배포 이벤트, 템플릿 메타데이터 저장
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from fivewhys.models import DeployEvent, LogRecord

_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_./-]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    epoch REAL NOT NULL,
    service TEXT NOT NULL,
    level TEXT NOT NULL,
    component TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    template_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_logs_epoch ON logs(epoch);
CREATE INDEX IF NOT EXISTS idx_logs_service ON logs(service);
CREATE INDEX IF NOT EXISTS idx_logs_template ON logs(template_id);
CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts USING fts5(message);

CREATE TABLE IF NOT EXISTS templates(
    id INTEGER PRIMARY KEY,
    template TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deploys(
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    epoch REAL NOT NULL,
    service TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY,
    doc TEXT NOT NULL,
    section TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text);
"""


def _fts_query(query: str) -> str:
    """사용자 질의를 FTS5 문법 오류가 나지 않는 안전한 질의로 변환한다.

    토큰을 큰따옴표로 감싸 OR로 연결한다 — 재현율을 우선하고 순위는
    bm25가 결정하게 한다.
    """
    tokens = _TOKEN_RE.findall(query)
    return " OR ".join(f'"{t}"' for t in tokens)


class LogStore:
    def __init__(self, path: str = ":memory:", check_same_thread: bool = True) -> None:
        # check_same_thread=False는 MCP 서버처럼 도구 호출이 워커 스레드에서
        # 실행되는 환경용이다. 이때 호출 측에서 락으로 직렬화해야 한다.
        self._conn = sqlite3.connect(path, check_same_thread=check_same_thread)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ 적재

    def add_logs(self, records: list[LogRecord]) -> None:
        cur = self._conn.cursor()
        for rec in records:
            cur.execute(
                "INSERT INTO logs(ts, epoch, service, level, component, message, template_id)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    rec.ts.isoformat(),
                    rec.ts.timestamp(),
                    rec.service,
                    rec.level,
                    rec.component,
                    rec.message,
                    rec.template_id,
                ),
            )
            rec.id = cur.lastrowid
            cur.execute(
                "INSERT INTO logs_fts(rowid, message) VALUES(?,?)", (rec.id, rec.message)
            )
        self._conn.commit()

    def set_templates(self, templates: dict[int, str]) -> None:
        self._conn.executemany(
            "INSERT OR REPLACE INTO templates(id, template) VALUES(?,?)",
            list(templates.items()),
        )
        self._conn.commit()

    def add_deploys(self, events: list[DeployEvent]) -> None:
        cur = self._conn.cursor()
        for ev in events:
            cur.execute(
                "INSERT INTO deploys(ts, epoch, service, kind, description) VALUES(?,?,?,?,?)",
                (ev.ts.isoformat(), ev.ts.timestamp(), ev.service, ev.kind, ev.description),
            )
            ev.id = cur.lastrowid
        self._conn.commit()

    def add_chunk(self, doc: str, section: str, text: str) -> int:
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO chunks(doc, section, text) VALUES(?,?,?)", (doc, section, text)
        )
        chunk_id = cur.lastrowid
        cur.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (chunk_id, text))
        self._conn.commit()
        return chunk_id

    # ------------------------------------------------------------------ 조회

    def search_logs(
        self,
        query: str,
        service: str | None = None,
        level: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 20,
    ) -> list[dict]:
        match = _fts_query(query)
        if not match:
            return []
        sql = (
            "SELECT l.id, l.ts, l.service, l.level, l.component, l.message, l.template_id,"
            " bm25(logs_fts) AS rank"
            " FROM logs_fts f JOIN logs l ON l.id = f.rowid"
            " WHERE logs_fts MATCH ?"
        )
        params: list = [match]
        if service:
            sql += " AND l.service = ?"
            params.append(service)
        if level:
            sql += " AND l.level = ?"
            params.append(level)
        if start:
            sql += " AND l.epoch >= ?"
            params.append(start.timestamp())
        if end:
            sql += " AND l.epoch <= ?"
            params.append(end.timestamp())
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params)]

    def get_log(self, log_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM logs WHERE id = ?", (log_id,)).fetchone()
        return dict(row) if row else None

    def get_window(self, log_id: int, before: int = 5, after: int = 5) -> list[dict]:
        """대상 로그 전후의 라인들(시간순 인접 삽입 가정)을 반환한다."""
        rows = self._conn.execute(
            "SELECT * FROM logs WHERE id BETWEEN ? AND ? ORDER BY id",
            (log_id - before, log_id + after),
        )
        return [dict(r) for r in rows]

    def error_timeline(
        self, start: datetime, end: datetime, bucket_minutes: int = 5
    ) -> list[dict]:
        """서비스×레벨×시간버킷 단위 카운트 (장애 시작점/전파 순서 파악용)."""
        bucket = bucket_minutes * 60
        rows = self._conn.execute(
            "SELECT service, level,"
            " CAST((epoch - ?) / ? AS INTEGER) AS bucket, COUNT(*) AS count"
            " FROM logs WHERE epoch BETWEEN ? AND ? AND level IN ('WARN','ERROR','FATAL')"
            " GROUP BY service, level, bucket ORDER BY bucket, service",
            (start.timestamp(), bucket, start.timestamp(), end.timestamp()),
        )
        out = []
        for r in rows:
            offset = r["bucket"] * bucket_minutes
            out.append(
                {
                    "service": r["service"],
                    "level": r["level"],
                    "window_start_min": offset,
                    "count": r["count"],
                }
            )
        return out

    def first_occurrence(
        self, service: str, level: str, start: datetime, end: datetime
    ) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM logs WHERE service=? AND level=? AND epoch BETWEEN ? AND ?"
            " ORDER BY epoch LIMIT 1",
            (service, level, start.timestamp(), end.timestamp()),
        ).fetchone()
        return dict(row) if row else None

    def template_counts(self, start: datetime, end: datetime) -> dict[int, int]:
        # 반개구간 [start, end): 베이스라인 끝 == 장애 시작 경계의 로그가
        # 양쪽 구간에 이중 집계되는 것을 막는다
        rows = self._conn.execute(
            "SELECT template_id, COUNT(*) AS c FROM logs"
            " WHERE epoch >= ? AND epoch < ? AND template_id IS NOT NULL"
            " GROUP BY template_id",
            (start.timestamp(), end.timestamp()),
        )
        return {r["template_id"]: r["c"] for r in rows}

    def template_info(
        self, template_id: int, start: datetime, end: datetime, examples: int = 3
    ) -> dict:
        tpl = self._conn.execute(
            "SELECT template FROM templates WHERE id=?", (template_id,)
        ).fetchone()
        rows = self._conn.execute(
            "SELECT id, service, level FROM logs"
            " WHERE template_id=? AND epoch BETWEEN ? AND ? ORDER BY epoch LIMIT ?",
            (template_id, start.timestamp(), end.timestamp(), examples),
        ).fetchall()
        return {
            "template": tpl["template"] if tpl else "",
            "service": rows[0]["service"] if rows else "",
            "level": rows[0]["level"] if rows else "",
            "example_log_ids": [r["id"] for r in rows],
        }

    def list_deploys(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM deploys ORDER BY epoch")
        return [dict(r) for r in rows]

    def search_chunks(self, query: str, limit: int = 20) -> list[dict]:
        match = _fts_query(query)
        if not match:
            return []
        rows = self._conn.execute(
            "SELECT c.id, c.doc, c.section, c.text, bm25(chunks_fts) AS rank"
            " FROM chunks_fts f JOIN chunks c ON c.id = f.rowid"
            " WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        )
        return [dict(r) for r in rows]

    def all_chunks(self) -> list[dict]:
        rows = self._conn.execute("SELECT id, doc, section, text FROM chunks ORDER BY id")
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
