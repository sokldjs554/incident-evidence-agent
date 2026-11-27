import pytest

from fivewhys.scenarios.catalog import SCENARIOS, get_scenario


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario_builds_consistently(name):
    bundle = get_scenario(name, seed=42)
    assert bundle.logs, "로그가 비어 있으면 안 된다"
    assert bundle.baseline[1] == bundle.incident[0]

    # 정답 서비스의 로그가 장애 구간에 실제 존재해야 한다
    gt = bundle.ground_truth
    i_start, i_end = bundle.incident
    incident_logs = [r for r in bundle.logs if i_start <= r.ts <= i_end]
    assert any(r.service == gt.root_cause_service for r in incident_logs)

    # 장애 구간에는 베이스라인보다 높은 밀도의 ERROR가 있어야 한다
    errors_incident = sum(1 for r in incident_logs if r.level in ("ERROR", "FATAL"))
    assert errors_incident >= 10

    # 로그는 시간순 정렬 상태
    assert all(a.ts <= b.ts for a, b in zip(bundle.logs, bundle.logs[1:]))


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario_deterministic_by_seed(name):
    a = get_scenario(name, seed=7)
    b = get_scenario(name, seed=7)
    assert [(r.ts, r.message) for r in a.logs] == [(r.ts, r.message) for r in b.logs]

    c = get_scenario(name, seed=8)
    assert [(r.ts, r.message) for r in a.logs] != [(r.ts, r.message) for r in c.logs]


def test_culprit_change_exists_in_deploys_when_labeled():
    for name in SCENARIOS:
        bundle = get_scenario(name)
        match = bundle.ground_truth.culprit_change_match
        if match is not None:
            assert any(match in d.description for d in bundle.deploys), (
                f"{name}: culprit_change_match '{match}'가 변경 이벤트에 없음"
            )


def test_unknown_scenario_raises():
    with pytest.raises(KeyError):
        get_scenario("nope")
