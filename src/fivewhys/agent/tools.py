"""에이전트 도구 정의 + 실행기.

도구는 "에이전트가 증거를 수집하는 유일한 통로"다. 각 도구의 description은
언제 그 도구를 호출해야 하는지까지 명시한다 (모델의 도구 선택 품질은
description 품질에 크게 좌우된다).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from fivewhys.logs.anomaly import anomalous_templates
from fivewhys.rag.retriever import HybridRetriever
from fivewhys.scenarios.generator import ScenarioBundle
from fivewhys.store.db import LogStore

SUBMIT_TOOL = "submit_root_cause_report"


def build_tool_definitions() -> list[dict]:
    return [
        {
            "name": "get_error_timeline",
            "description": (
                "장애 구간의 서비스별 WARN/ERROR 발생량을 시간 버킷 단위로 집계한다. "
                "어느 서비스에서 가장 먼저 에러가 시작됐고 어떤 순서로 전파됐는지 "
                "파악할 때 가장 먼저 호출하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "bucket_minutes": {
                        "type": "integer",
                        "description": "집계 버킷 크기(분). 기본 2",
                    }
                },
            },
        },
        {
            "name": "get_anomalous_templates",
            "description": (
                "베이스라인 대비 장애 구간에서 비정상적으로 급증한 로그 템플릿을 "
                "점수순으로 반환한다 (Drain 템플릿 마이닝 + 빈도 비교). '평소에 없던 "
                "에러 패턴'이 무엇인지 찾을 때 호출하라. 각 항목의 example_log_ids는 "
                "증거 인용에 사용할 수 있다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "top_k": {"type": "integer", "description": "반환 개수. 기본 10"}
                },
            },
        },
        {
            "name": "search_logs",
            "description": (
                "로그 본문을 BM25로 전문 검색한다. 특정 에러 메시지/키워드의 실제 "
                "발생 로그를 찾아 증거(log id)를 확보할 때 호출하라. 질의는 로그에 "
                "등장할 법한 키워드로 구성하라 (예: 'connection pool timeout')."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색 키워드"},
                    "service": {"type": "string", "description": "서비스 이름 필터(선택)"},
                    "level": {
                        "type": "string",
                        "description": "로그 레벨 필터(선택): INFO|WARN|ERROR|FATAL",
                    },
                    "limit": {"type": "integer", "description": "최대 결과 수. 기본 15"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_log_context",
            "description": (
                "특정 로그 id 전후의 주변 로그를 시간순으로 보여준다. 에러 직전에 "
                "무슨 일이 있었는지(선행 사건) 확인할 때 호출하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "log_id": {"type": "integer", "description": "기준 로그 id"},
                    "before": {"type": "integer", "description": "앞쪽 라인 수. 기본 5"},
                    "after": {"type": "integer", "description": "뒤쪽 라인 수. 기본 5"},
                },
                "required": ["log_id"],
            },
        },
        {
            "name": "list_change_events",
            "description": (
                "최근 배포/설정 변경 이벤트 목록을 반환한다. 장애 시작 시각과 가까운 "
                "변경이 있는지 반드시 확인하라 — 장애의 다수는 변경에서 시작된다."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "search_past_incidents",
            "description": (
                "과거 장애 보고서(포스트모템) 지식베이스를 하이브리드 검색(BM25+벡터"
                "+RRF)한다. 현재 증상과 유사한 과거 장애와 그때의 재발 방지 대책을 "
                "참고할 때 호출하라."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "증상/원인 키워드"},
                    "k": {"type": "integer", "description": "반환 청크 수. 기본 4"},
                },
                "required": ["query"],
            },
        },
        {
            "name": SUBMIT_TOOL,
            "description": (
                "최종 근본원인분석 보고서를 제출한다. 증거 수집이 끝난 뒤 마지막에 "
                "정확히 한 번 호출하라. five_whys의 각 단계는 반드시 evidence_log_ids로 "
                "실제 로그를 인용해야 한다."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "장애 증상/영향 요약 (2~3문장)"},
                    "five_whys": {
                        "type": "array",
                        "description": "왜? 체인 (3단계 이상, 증상→근본원인 순서)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "answer": {"type": "string"},
                                "evidence_log_ids": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                },
                            },
                            "required": ["question", "answer"],
                        },
                    },
                    "root_cause": {"type": "string", "description": "근본 원인 설명"},
                    "root_cause_service": {
                        "type": "string",
                        "description": "근본 원인이 위치한 서비스 이름",
                    },
                    "culprit_change": {
                        "type": "string",
                        "description": "원인이 된 배포/설정 변경 (없으면 생략)",
                    },
                    "remediation": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "재발 방지 대책 (1개 이상)",
                    },
                    "similar_incident": {
                        "type": "string",
                        "description": "과거 유사 장애 문서 이름 (없으면 생략)",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "분석 신뢰도 0.0~1.0",
                    },
                },
                "required": [
                    "summary",
                    "five_whys",
                    "root_cause",
                    "root_cause_service",
                    "remediation",
                    "confidence",
                ],
            },
        },
    ]


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


@dataclass
class ToolExecutor:
    """도구 이름 → 실제 저장소/검색기 호출 바인딩."""

    store: LogStore
    retriever: HybridRetriever
    baseline: tuple[datetime, datetime]
    incident: tuple[datetime, datetime]

    @classmethod
    def for_bundle(
        cls, bundle: ScenarioBundle, store: LogStore, retriever: HybridRetriever
    ) -> "ToolExecutor":
        return cls(
            store=store,
            retriever=retriever,
            baseline=bundle.baseline,
            incident=bundle.incident,
        )

    def execute(self, name: str, tool_input: dict) -> tuple[str, bool]:
        """(결과 문자열, is_error)를 반환한다."""
        try:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return f"알 수 없는 도구: {name}", True
            return _dumps(handler(**(tool_input or {}))), False
        except TypeError as e:
            return f"도구 인자 오류: {e}", True
        except Exception as e:  # 도구 실패는 에이전트가 복구하도록 전달
            return f"도구 실행 실패: {type(e).__name__}: {e}", True

    # ------------------------------------------------------------------ 도구들

    def _tool_get_error_timeline(self, bucket_minutes: int = 2) -> dict:
        bucket_minutes = max(1, int(bucket_minutes))  # 0/음수 → 0으로 나누기 방지
        timeline = self.store.error_timeline(
            self.incident[0], self.incident[1], bucket_minutes=bucket_minutes
        )
        return {
            "incident_window": [self.incident[0].isoformat(), self.incident[1].isoformat()],
            "bucket_minutes": bucket_minutes,
            "timeline": timeline,
        }

    def _tool_get_anomalous_templates(self, top_k: int = 10) -> list[dict]:
        return anomalous_templates(
            self.store, baseline=self.baseline, incident=self.incident, top_k=top_k
        )

    def _tool_search_logs(
        self,
        query: str,
        service: str | None = None,
        level: str | None = None,
        limit: int = 15,
    ) -> list[dict]:
        rows = self.store.search_logs(
            query,
            service=service,
            level=level,
            start=self.baseline[0],
            end=self.incident[1],
            limit=limit,
        )
        for r in rows:
            r.pop("rank", None)
            r.pop("template_id", None)
        return rows

    def _tool_get_log_context(
        self, log_id: int, before: int = 5, after: int = 5
    ) -> list[dict]:
        rows = self.store.get_window(log_id, before=before, after=after)
        for r in rows:
            r.pop("epoch", None)
            r.pop("template_id", None)
        return rows

    def _tool_list_change_events(self) -> list[dict]:
        rows = self.store.list_deploys()
        for r in rows:
            r.pop("epoch", None)
        return rows

    def _tool_search_past_incidents(self, query: str, k: int = 4) -> list[dict]:
        chunks = self.retriever.search(query, k=k)
        return [
            {"doc": c.doc, "section": c.section, "text": c.text, "score": c.score}
            for c in chunks
        ]
