"""템플릿 빈도 기반 이상 탐지.

베이스라인 구간과 장애 구간에서 각 템플릿의 발생률을 비교해, 장애 구간에서
비정상적으로 급증한 템플릿을 점수화한다. ERROR/FATAL 레벨에 가중치를 주어
"평소에 없던 에러 패턴"이 최상위로 올라오게 한다.

score = level_weight * count_incident * log2(rate_incident / rate_baseline)
"""

from __future__ import annotations

import math
from datetime import datetime

from fivewhys.store.db import LogStore

_LEVEL_WEIGHT = {"FATAL": 4.0, "ERROR": 3.0, "WARN": 2.0}
_EPS = 1e-9


def anomalous_templates(
    store: LogStore,
    baseline: tuple[datetime, datetime],
    incident: tuple[datetime, datetime],
    top_k: int = 10,
) -> list[dict]:
    b_start, b_end = baseline
    i_start, i_end = incident
    b_minutes = max((b_end - b_start).total_seconds() / 60.0, 1.0)
    i_minutes = max((i_end - i_start).total_seconds() / 60.0, 1.0)

    base_counts = store.template_counts(b_start, b_end)
    inc_counts = store.template_counts(i_start, i_end)

    scored: list[dict] = []
    for tid, ci in inc_counts.items():
        cb = base_counts.get(tid, 0)
        rate_i = ci / i_minutes
        rate_b = cb / b_minutes
        ratio = (rate_i + _EPS) / (rate_b + _EPS)
        if ratio <= 1.0:
            continue
        info = store.template_info(tid, i_start, i_end)
        weight = _LEVEL_WEIGHT.get(info["level"], 1.0)
        score = weight * ci * math.log2(ratio)
        scored.append(
            {
                "template_id": tid,
                "template": info["template"],
                "service": info["service"],
                "level": info["level"],
                "count_incident": ci,
                "count_baseline": cb,
                "score": round(score, 2),
                "example_log_ids": info["example_log_ids"],
            }
        )

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
