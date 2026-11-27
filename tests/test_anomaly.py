from datetime import datetime, timedelta

from fivewhys.logs.anomaly import anomalous_templates
from fivewhys.models import KST, LogRecord
from fivewhys.store.db import LogStore

T0 = datetime(2026, 7, 3, 13, 0, tzinfo=KST)
INCIDENT = (T0 + timedelta(minutes=60), T0 + timedelta(minutes=75))
BASELINE = (T0, T0 + timedelta(minutes=60))


def _rec(ts, message, level="INFO", template_id=None, service="svc"):
    r = LogRecord(ts=ts, service=service, level=level, component="", message=message)
    r.template_id = template_id
    return r


def test_spike_template_ranked_first():
    store = LogStore()
    recs = []
    # 베이스라인: 템플릿 1(정상)이 다수
    for i in range(120):
        recs.append(_rec(BASELINE[0] + timedelta(seconds=30 * i), "request ok", "INFO", 1))
    # 장애 구간: 정상 로그 소량 + 에러 템플릿 2 급증 (베이스라인에는 없음)
    for i in range(10):
        recs.append(_rec(INCIDENT[0] + timedelta(seconds=60 * i), "request ok", "INFO", 1))
    for i in range(40):
        recs.append(
            _rec(INCIDENT[0] + timedelta(seconds=20 * i), "connection refused", "ERROR", 2)
        )
    store.add_logs(recs)
    store.set_templates({1: "request ok", 2: "connection refused"})

    result = anomalous_templates(store, BASELINE, INCIDENT, top_k=5)
    assert result
    assert result[0]["template_id"] == 2
    assert result[0]["level"] == "ERROR"
    assert result[0]["count_baseline"] == 0
    assert result[0]["count_incident"] == 40
    assert result[0]["example_log_ids"]
    store.close()


def test_steady_template_not_flagged():
    store = LogStore()
    recs = []
    for i in range(60):
        recs.append(_rec(BASELINE[0] + timedelta(minutes=i), "heartbeat", "INFO", 1))
    for i in range(15):
        recs.append(_rec(INCIDENT[0] + timedelta(minutes=i), "heartbeat", "INFO", 1))
    store.add_logs(recs)
    store.set_templates({1: "heartbeat"})

    result = anomalous_templates(store, BASELINE, INCIDENT, top_k=5)
    # 발생률이 동일한 템플릿은 이상으로 잡히지 않아야 한다
    assert result == []
    store.close()
