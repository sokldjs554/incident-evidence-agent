"""정답 라벨이 있는 합성 장애 시나리오 생성기.

에이전트 평가(Evaluation)를 정량화하려면 "정답을 아는 장애"가 필요하다.
각 시나리오는 마이크로서비스 환경에서 실제로 흔한 장애 유형을 본떠,

- 베이스라인 구간(정상 트래픽 60분) + 장애 구간(15분)의 로그
- 배포/설정 변경 이벤트 (원인인 것 + 무관한 노이즈)
- 근본 원인 정답(GroundTruth)

을 함께 생성한다. seed가 같으면 항상 같은 데이터가 나온다(결정적).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fivewhys.models import KST, DeployEvent, LogRecord

BASE_TIME = datetime(2026, 7, 3, 13, 0, tzinfo=KST)
BASELINE_MINUTES = 60
INCIDENT_MINUTES = 15

SERVICES = [
    "api-gateway",
    "order-service",
    "payment-service",
    "user-service",
    "inventory-service",
    "mysql",
    "redis-cache",
]

# Operational access policy, copied into each scenario so a fixture can
# specify different visibility without changing other incidents or archives.
DEFAULT_SERVICE_TEAMS = {
    service: ("orders", "platform") if service in {"api-gateway", "order-service", "redis-cache"}
    else ("platform",)
    for service in SERVICES
}

# 정상 트래픽 로그 패턴 (서비스별)
_NORMAL: dict[str, list[str]] = {
    "api-gateway": [
        "Request completed method=GET path=/orders status=200 latency={lat}ms",
        "Request completed method=POST path=/payments status=201 latency={lat}ms",
        "Request completed method=GET path=/products status=200 latency={lat}ms",
        "Health check passed upstreams=7",
    ],
    "order-service": [
        "Order fetched order_id=ord-{oid} items={n}",
        "Order created order_id=ord-{oid} customer=cus-{cid}",
        "Cache hit key=product:{pid}",
    ],
    "payment-service": [
        "Payment authorized tx=tx-{tid} amount={amt}",
        "Receipt generated tx=tx-{tid}",
    ],
    "user-service": [
        "Login success user=usr-{uid}",
        "Session refreshed user=usr-{uid}",
    ],
    "inventory-service": [
        "Stock checked sku=SKU-{pid} qty={n}",
        "Stock snapshot persisted rows={n}",
    ],
    "mysql": [
        "Query OK rows={n} time={qms}ms",
        "Connection opened id={cid}",
    ],
    "redis-cache": [
        "GET product:{pid} hit",
        "SET session:{uid} ttl=3600",
    ],
}


@dataclass
class GroundTruth:
    """평가 채점에 사용하는 시나리오 정답."""

    root_cause_service: str
    root_cause_keywords: list[str]  # root_cause 서술에 하나 이상 포함돼야 함
    culprit_change_match: str | None  # 원인 배포/설정 변경 식별 문자열 (없으면 None)
    causal_chain: list[str]  # 사람이 보는 정답 인과 사슬
    remediation_keywords: list[str]


@dataclass
class ScenarioBundle:
    name: str
    title: str
    description: str
    logs: list[LogRecord]
    deploys: list[DeployEvent]
    baseline: tuple[datetime, datetime]
    incident: tuple[datetime, datetime]
    ground_truth: GroundTruth
    services: list[str] = field(default_factory=lambda: list(SERVICES))
    # Fixture topology, independent of the labels used to score RCA hypotheses.
    dependencies: tuple[tuple[str, str], ...] = ()
    service_teams: dict[str, tuple[str, ...]] = field(default_factory=DEFAULT_SERVICE_TEAMS.copy)


class _Builder:
    """시나리오 조립 헬퍼: 정상 트래픽 + 장애 버스트 + 이벤트."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.baseline_start = BASE_TIME
        self.incident_start = BASE_TIME + timedelta(minutes=BASELINE_MINUTES)
        self.incident_end = self.incident_start + timedelta(minutes=INCIDENT_MINUTES)
        self.logs: list[LogRecord] = []
        self.deploys: list[DeployEvent] = []

    # -------------------------------------------------------------- 시간 헬퍼

    def _ts_between(self, start: datetime, end: datetime) -> datetime:
        seconds = self.rng.uniform(0, (end - start).total_seconds())
        return start + timedelta(seconds=seconds)

    def _fill(self, template: str) -> str:
        r = self.rng
        return template.format(
            lat=r.randrange(8, 220),
            qms=r.randrange(2, 40),
            amt=r.randrange(1000, 90000),
            n=r.randrange(1, 40),
            oid=r.randrange(10000, 99999),
            cid=r.randrange(1000, 9999),
            pid=r.randrange(100, 999),
            tid=r.randrange(10000, 99999),
            uid=r.randrange(1000, 9999),
            sec=round(r.uniform(8.0, 25.0), 1),
        )

    # -------------------------------------------------------------- 로그 생성

    def normal_traffic(self, rate_per_min: float = 4.0) -> None:
        """전 구간(베이스라인+장애)에 걸친 정상 트래픽을 깐다."""
        for service, templates in _NORMAL.items():
            total_min = BASELINE_MINUTES + INCIDENT_MINUTES
            count = int(rate_per_min * total_min)
            for _ in range(count):
                ts = self._ts_between(self.baseline_start, self.incident_end)
                self.logs.append(
                    LogRecord(
                        ts=ts,
                        service=service,
                        level="INFO",
                        component="app",
                        message=self._fill(self.rng.choice(templates)),
                    )
                )
        # 평시에도 가끔 나오는 무해한 WARN 노이즈 (베이스라인에도 WARN이
        # 존재해야 "장애 구간 급증"이 의미를 가진다)
        for _ in range(int(0.3 * BASELINE_MINUTES)):
            ts = self._ts_between(self.baseline_start, self.incident_start)
            svc = self.rng.choice(SERVICES)
            self.logs.append(
                LogRecord(
                    ts=ts,
                    service=svc,
                    level="WARN",
                    component="app",
                    message=self._fill("Slow response detected latency={lat}ms (transient)"),
                )
            )

    def burst(
        self,
        service: str,
        level: str,
        component: str,
        template: str,
        start_min: float,
        end_min: float,
        count: int,
    ) -> None:
        """장애 구간 시작 기준 [start_min, end_min] 분 사이에 버스트를 생성한다."""
        start = self.incident_start + timedelta(minutes=start_min)
        end = self.incident_start + timedelta(minutes=end_min)
        for _ in range(count):
            self.logs.append(
                LogRecord(
                    ts=self._ts_between(start, end),
                    service=service,
                    level=level,
                    component=component,
                    message=self._fill(template),
                )
            )

    def change_event(
        self, minutes_before_incident: float, service: str, kind: str, description: str
    ) -> None:
        ts = self.incident_start - timedelta(minutes=minutes_before_incident)
        self.deploys.append(
            DeployEvent(ts=ts, service=service, kind=kind, description=description)
        )

    def bundle(
        self, name: str, title: str, description: str, ground_truth: GroundTruth,
        *, dependencies: tuple[tuple[str, str], ...]
    ) -> ScenarioBundle:
        self.logs.sort(key=lambda r: r.ts)
        self.deploys.sort(key=lambda d: d.ts)
        return ScenarioBundle(
            name=name,
            title=title,
            description=description,
            logs=self.logs,
            deploys=self.deploys,
            baseline=(self.baseline_start, self.incident_start),
            incident=(self.incident_start, self.incident_end),
            ground_truth=ground_truth,
            dependencies=dependencies,
        )


# ====================================================================== 시나리오


def build_db_pool_exhaustion(seed: int = 42) -> ScenarioBundle:
    b = _Builder(seed)
    b.normal_traffic()
    # 원인: order-service 배포에 포함된 비인덱스 쿼리
    b.change_event(40, "order-service", "deploy", "order-service v2.4.0 배포 — 주문 조회 쿼리 리팩터링 (customer_ref 조건 추가)")
    b.change_event(130, "user-service", "deploy", "user-service v1.9.1 배포 — 프로필 이미지 리사이즈 버그 수정")

    b.burst("mysql", "WARN", "slowlog", "Slow query: SELECT * FROM orders WHERE customer_ref = 'cus-{cid}' (execution time {sec}s)", 0, 14, 42)
    b.burst("order-service", "WARN", "db-pool", "HikariPool-1 - Connection acquisition time {lat}ms exceeds warning threshold", 2, 14, 26)
    b.burst("order-service", "ERROR", "db-pool", "HikariPool-1 - Connection is not available, request timed out after 30000ms", 4, 15, 58)
    b.burst("order-service", "ERROR", "api", "Failed to fetch order ord-{oid}: SQLTransientConnectionException", 4.5, 15, 30)
    b.burst("api-gateway", "ERROR", "proxy", "Upstream timeout: order-service GET /orders/ord-{oid} exceeded 10000ms, responding 504", 6, 15, 48)
    b.burst("api-gateway", "WARN", "circuit", "Circuit breaker OPEN for order-service", 8, 15, 6)

    gt = GroundTruth(
        root_cause_service="order-service",
        root_cause_keywords=["쿼리", "query", "connection", "커넥션", "v2.4.0"],
        culprit_change_match="v2.4.0",
        causal_chain=[
            "order-service v2.4.0 배포에 비인덱스 조건(customer_ref) 쿼리 포함",
            "mysql slow query 급증 → 커넥션 점유 시간 증가",
            "order-service HikariPool 커넥션 풀 고갈",
            "api-gateway 업스트림 타임아웃 → 사용자 504",
        ],
        remediation_keywords=["인덱스", "롤백", "풀", "타임아웃"],
    )
    return b.bundle(
        "db_pool_exhaustion",
        "DB 커넥션 풀 고갈",
        "배포된 비인덱스 쿼리가 slow query를 유발해 커넥션 풀 고갈 → 게이트웨이 504 연쇄",
        gt,
        dependencies=(("api-gateway", "order-service"), ("order-service", "mysql")),
    )


def build_memory_leak(seed: int = 42) -> ScenarioBundle:
    b = _Builder(seed)
    b.normal_traffic()
    b.change_event(50, "payment-service", "deploy", "payment-service v3.1.0 배포 — 결제 영수증 PDF 생성 기능 추가")
    b.change_event(140, "inventory-service", "deploy", "inventory-service v0.8.2 배포 — 재고 스냅샷 주기 조정")

    b.burst("payment-service", "WARN", "jvm", "GC pause {lat}ms exceeds threshold (old gen 92% used)", 0, 10, 30)
    b.burst("payment-service", "ERROR", "jvm", "java.lang.OutOfMemoryError: Java heap space in ReceiptRenderer.render", 3, 15, 26)
    b.burst("payment-service", "ERROR", "runtime", "Process killed by OOMKiller exit code 137, container restarting", 5, 15, 7)
    b.burst("api-gateway", "ERROR", "proxy", "Upstream connection refused: payment-service POST /payments (connection reset)", 6, 15, 40)
    b.burst("order-service", "WARN", "payment", "Payment confirmation delayed order=ord-{oid} retry={n}", 7, 15, 18)

    gt = GroundTruth(
        root_cause_service="payment-service",
        root_cause_keywords=["메모리", "memory", "oom", "heap", "누수", "v3.1.0"],
        culprit_change_match="v3.1.0",
        causal_chain=[
            "payment-service v3.1.0의 PDF 생성 기능에 메모리 누수",
            "old gen 점유율 상승 → GC pause 급증",
            "OutOfMemoryError → OOMKiller 컨테이너 강제 종료",
            "api-gateway 결제 요청 connection refused",
        ],
        remediation_keywords=["메모리", "힙", "롤백", "프로파일링", "리소스"],
    )
    return b.bundle(
        "memory_leak",
        "결제 서비스 메모리 누수(OOM)",
        "신규 배포의 메모리 누수로 GC 악화 → OOM Kill → 결제 API 연쇄 실패",
        gt,
        dependencies=(("api-gateway", "payment-service"), ("order-service", "payment-service")),
    )


def build_disk_full(seed: int = 42) -> ScenarioBundle:
    b = _Builder(seed)
    b.normal_traffic()
    b.change_event(80, "inventory-service", "config", "inventory-service 로그 로테이션 설정 변경 — retention 7d → 30d")
    b.change_event(150, "api-gateway", "deploy", "api-gateway v5.2.0 배포 — 액세스 로그 포맷 변경")

    b.burst("inventory-service", "WARN", "disk", "Disk usage {n}% on /var/log volume, above warning threshold", 0, 5, 18)
    b.burst("inventory-service", "ERROR", "disk", "No space left on device: cannot write /var/log/inventory/app.log", 3, 15, 46)
    b.burst("inventory-service", "ERROR", "api", "Failed to persist stock snapshot: java.io.IOException (disk full)", 4, 15, 24)
    b.burst("api-gateway", "ERROR", "proxy", "Upstream 500 from inventory-service GET /stock/SKU-{pid}", 7, 15, 34)

    gt = GroundTruth(
        root_cause_service="inventory-service",
        root_cause_keywords=["디스크", "disk", "space", "로테이션", "로그"],
        culprit_change_match="로테이션",
        causal_chain=[
            "로그 로테이션 retention 변경으로 로그 파일 누적",
            "/var/log 볼륨 사용률 100% 도달",
            "디스크 쓰기 실패 → 재고 스냅샷 저장 실패",
            "api-gateway 재고 조회 500 연쇄",
        ],
        remediation_keywords=["로테이션", "용량", "정리", "알람", "모니터링"],
    )
    return b.bundle(
        "disk_full",
        "디스크 용량 고갈",
        "로그 로테이션 설정 실수로 디스크 100% → 쓰기 실패 → 재고 API 장애",
        gt,
        dependencies=(("api-gateway", "inventory-service"),),
    )


def build_cert_expiry(seed: int = 42) -> ScenarioBundle:
    b = _Builder(seed)
    b.normal_traffic()
    b.change_event(150, "order-service", "deploy", "order-service v2.3.9 배포 — 로그 레벨 조정")

    b.burst("user-service", "ERROR", "tls", "SSL handshake failed with auth-provider: certificate expired (notAfter=2026-07-03T05:00:00Z)", 0, 15, 56)
    b.burst("user-service", "ERROR", "auth", "Token issuance failed: upstream TLS error for realm=main", 1, 15, 30)
    b.burst("api-gateway", "ERROR", "proxy", "Upstream 401 from user-service POST /login", 3, 15, 44)
    b.burst("payment-service", "WARN", "auth", "User verification degraded, falling back to cached session user=usr-{uid}", 5, 15, 14)

    gt = GroundTruth(
        root_cause_service="user-service",
        root_cause_keywords=["인증서", "certificate", "tls", "ssl", "만료"],
        culprit_change_match=None,
        causal_chain=[
            "auth-provider TLS 인증서 만료",
            "user-service SSL 핸드셰이크 실패 → 토큰 발급 불가",
            "api-gateway 로그인 401 연쇄",
        ],
        remediation_keywords=["인증서", "갱신", "만료", "모니터링", "자동"],
    )
    return b.bundle(
        "cert_expiry",
        "TLS 인증서 만료",
        "외부 인증 제공자의 인증서 만료로 로그인 전면 장애",
        gt,
        dependencies=(("api-gateway", "user-service"), ("payment-service", "user-service")),
    )


def build_cache_stampede(seed: int = 42) -> ScenarioBundle:
    b = _Builder(seed)
    b.normal_traffic()
    b.change_event(2, "redis-cache", "config", "redis-cache 노드 재시작 — 메모리 파편화 정리 (계획 작업)")
    b.change_event(160, "payment-service", "deploy", "payment-service v3.0.9 배포 — 로깅 개선")

    b.burst("redis-cache", "ERROR", "node", "Redis node restarting, flushing {n} client connections", 0, 1, 6)
    b.burst("redis-cache", "WARN", "node", "Loading dataset into memory, rejecting connections", 0, 3, 20)
    b.burst("order-service", "WARN", "cache", "Cache miss rate {n}% for key pattern product:* (stampede suspected)", 1, 10, 28)
    b.burst("mysql", "WARN", "slowlog", "Slow query: SELECT * FROM products WHERE id IN (...) (execution time {sec}s)", 2, 12, 36)
    b.burst("mysql", "ERROR", "conn", "Too many connections (max_connections=300 reached)", 4, 14, 28)
    b.burst("order-service", "ERROR", "db", "Product lookup failed: SQLNonTransientConnectionException (connection limit)", 5, 15, 30)
    b.burst("api-gateway", "ERROR", "proxy", "Upstream timeout: order-service GET /products/{pid}", 7, 15, 38)

    gt = GroundTruth(
        root_cause_service="redis-cache",
        root_cause_keywords=["캐시", "cache", "redis", "재시작", "stampede"],
        culprit_change_match="재시작",
        causal_chain=[
            "redis-cache 노드 재시작으로 캐시 전체 소실",
            "캐시 미스 폭증(cache stampede) → mysql 부하 집중",
            "mysql 커넥션 한도 도달 → order-service 조회 실패",
            "api-gateway 타임아웃 연쇄",
        ],
        remediation_keywords=["캐시", "워밍", "TTL", "점진", "재시작"],
    )
    return b.bundle(
        "cache_stampede",
        "캐시 스탬피드",
        "캐시 노드 재시작 → 캐시 미스 폭증 → DB 과부하 연쇄 장애",
        gt,
        dependencies=(("api-gateway", "order-service"), ("order-service", "redis-cache"),
                      ("order-service", "mysql")),
    )
