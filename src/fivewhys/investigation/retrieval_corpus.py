"""Versioned evidence from deterministic incident scenarios and operator uploads."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from fivewhys.rag.chunker import _find_headings
from fivewhys.scenarios.catalog import SCENARIOS, get_scenario

TEAMS = ("orders", "platform")
KINDS = ("log", "deployment", "report", "runbook")
DEFAULT_SCENARIO = "db_pool_exhaustion"
DEFAULT_SEED = 42
INCIDENT = f"{DEFAULT_SCENARIO}/seed-{DEFAULT_SEED}"
# Archived reports retain their source ACL independently of the active case.
ORDERS_ARCHIVE_SERVICES = {"order-service", "api-gateway", "redis-cache"}
REPORT_DIR = Path(__file__).resolve().parents[1] / "data" / "postmortems"
REPORT_SERVICES = {
    "rollback-failure": "user-service", "bad-config-rollout": "api-gateway",
    "kafka-consumer-lag": "kafka", "dns-resolution": "dns",
    "cache-stampede": "redis-cache", "tls-cert-expiry": "user-service",
    "disk-full-logging": "inventory-service", "payment-oom": "payment-service",
    "db-connection-pool": "order-service",
}
RUNBOOKS = {
    "orders": ("order-service", """# 주문 API 배포 점검
1. 배포 직전·직후 주문 조회 오류와 커넥션 획득 시간을 비교한다.
2. 변경 이력에서 활성 배포 버전과 직전 정상 버전을 확인한다.
3. 롤백 후 오류율과 커넥션 풀 대기 시간이 회복됐는지 확인한다."""),
    "platform": ("mysql", """# DBA 슬로우 쿼리 점검
1. 장애 시간대의 slow query 로그에서 반복되는 SQL 조건을 확인한다.
2. 스테이징에서 EXPLAIN으로 실행계획과 인덱스 사용 여부를 확인한다.
3. 복구 후 쿼리 소요 시간과 연결 사용량을 정상 구간과 비교한다."""),
}


def validate_team(team):
    if team not in TEAMS:
        raise ValueError("unknown investigation team")


def _digest(*values):
    data = json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()


def _chunks(kind, text):
    lines = text.splitlines()
    if kind == "report":
        starts = {text[:start].count("\n"): title for start, _, title in _find_headings(text)}
    elif kind == "runbook":
        starts = {i: line.strip() for i, line in enumerate(lines)
                  if re.match(r"^\s*(?:\d+[.)]\s+|#{1,4}\s+)", line)}
    elif kind == "deployment":
        # Timestamped/bulleted change entries may carry multiline release notes.
        entry = re.compile(r"^\s*(?:[-*]\s+|\d{4}-\d{2}-\d{2}[ T]|v\d+\.\d+)")
        starts = {i: "" for i, line in enumerate(lines) if entry.match(line)}
    else:
        starts = {}  # A log source is one service/component/severity event family.
    boundaries = sorted({0, *starts, len(lines)})
    for begin, end in zip(boundaries, boundaries[1:]):
        while begin < end and not lines[begin].strip():
            begin += 1
        while end > begin and not lines[end - 1].strip():
            end -= 1
        if begin == end:
            continue
        if begin in starts and end == begin + 1 and lines[begin].startswith("#"):
            continue
        yield begin + 1, end, starts.get(begin, ""), "\n".join(lines[begin:end])


def _document(doc, teams, origin, *, incident="", observed_at="", end_at="", representative=False):
    source, kind, title, service, text = (doc[k] for k in ("source", "kind", "title", "service", "text"))
    # The human-supplied revision is a label. A content change always produces a new snapshot.
    digest = hashlib.sha256(text.encode()).hexdigest()
    version = (doc.get("version", "") + "@" if doc.get("version") else "") + "sha256:" + digest
    rows = []
    lines = text.splitlines()
    for start, end, heading, chunk in _chunks(kind, text):
        if representative:
            end = min(start + 2, end)
            chunk = "\n".join(lines[start - 1:end])
        locator = f"L{start}-L{end}"
        heading_label = heading[:96] + ("…" if len(heading) > 96 else "")
        row = dict(kind=kind, title=title if not heading else f"{title} · {heading_label}",
                   text=chunk, source=source, version=version, locator=locator, service=service,
                   teams=sorted(teams), origin=origin, incident=incident, observed_at=observed_at,
                   end_at=end_at, source_lines=len(lines))
        row["id"] = "ev-" + _digest(row)[:32]
        rows.append(row)
    if not rows:
        raise ValueError("document must contain at least one non-heading passage")
    return rows


def upload_rows(documents, team):
    validate_team(team)
    if not isinstance(documents, list) or not 1 <= len(documents) <= 32:
        raise ValueError("submit 1 to 32 documents")
    required = {"source", "kind", "title", "service", "text"}
    rows = []
    for doc in documents:
        if not isinstance(doc, dict) or not required <= doc.keys() <= required | {"version"}:
            raise ValueError("expected source, kind, title, service, text and optional version")
        if any(not isinstance(v, str) or not v.strip() for v in doc.values()):
            raise ValueError("document fields must be nonempty strings")
        if any("\x00" in value for value in doc.values()):
            raise ValueError("document fields must not contain NUL")
        if doc["kind"] not in KINDS:
            raise ValueError("unknown evidence kind")
        if any(len(v.encode()) > (32768 if k == "text" else 512) for k, v in doc.items()):
            raise ValueError("document field exceeds the byte limit")
        chunks = _document(doc, [team], "upload")
        if any(len(json.dumps(row, ensure_ascii=False).encode()) >= 65536 for row in chunks):
            raise ValueError("document exceeds the serialized evidence size limit")
        rows.extend(chunks)
    return rows


def incident_key(scenario=DEFAULT_SCENARIO, seed=DEFAULT_SEED):
    if not isinstance(scenario, str) or scenario not in SCENARIOS:
        raise ValueError("unknown incident scenario")
    if type(seed) is not int:
        raise ValueError("scenario seed must be an integer")
    return f"{scenario}/seed-{seed}"


def seed_rows(scenario=DEFAULT_SCENARIO, seed=DEFAULT_SEED):
    incident = incident_key(scenario, seed)
    return rows_for_bundle(get_scenario(scenario, seed), incident)


def rows_for_bundle(bundle, incident):
    start, end = bundle.incident
    groups = defaultdict(list)
    for entry in bundle.logs:
        if start <= entry.ts < end and entry.level in ("ERROR", "WARN"):
            groups[(entry.service, entry.component, entry.level)].append(entry)
    rows = []
    for (service, component, level), entries in sorted(groups.items()):
        teams = bundle.service_teams[service]
        text = "\n".join(f"{entry.ts.isoformat()} {level} [{component}] {entry.message}" for entry in entries)
        rows.extend(_document(
            dict(source=f"scenario/{incident}/logs/{service}/{component}-{level}.log", kind="log",
                 title=f"{service} {component} {level}", text=text, service=service),
            teams, "scenario", incident=incident, observed_at=entries[0].ts.isoformat(),
            end_at=entries[-1].ts.isoformat(), representative=True))
    # Index only the changes observed from baseline start up to incident onset.
    for change in bundle.deploys:
        if bundle.baseline[0] <= change.ts <= start:
            rows.extend(_document(
                dict(source=f"scenario/{incident}/changes/{change.service}-{change.ts:%H%M}.txt",
                     kind="deployment", title=change.description, service=change.service,
                     text=f"{change.ts.isoformat()} {change.kind} {change.description}"),
                bundle.service_teams[change.service], "scenario", incident=incident,
                observed_at=change.ts.isoformat(), end_at=change.ts.isoformat()))
    for file in sorted(REPORT_DIR.glob("*.md")):
        body = file.read_text(encoding="utf-8")
        service = REPORT_SERVICES[file.stem[11:]]
        teams = TEAMS if service in ORDERS_ARCHIVE_SERVICES else ["platform"]
        rows.extend(_document(dict(source="postmortems/" + file.name, kind="report",
                                   title=body.splitlines()[0].lstrip("# "), service=service, text=body), teams, "archive"))
    contrast_file = REPORT_DIR.parent / "investigation" / "cache-restart.md"
    contrast = contrast_file.read_text(encoding="utf-8")
    rows.extend(_document(dict(source="investigation/cache-restart.md", kind="report", service="redis-cache",
                               title=contrast.splitlines()[0].lstrip("# "), text=contrast), TEAMS, "archive"))
    for team, (service, body) in RUNBOOKS.items():
        rows.extend(_document(dict(source=f"demo/runbooks/{team}.md", kind="runbook",
                                   title=body.splitlines()[0].lstrip("# "), service=service, text=body),
                              [team], "demo-runbook"))
    return rows
