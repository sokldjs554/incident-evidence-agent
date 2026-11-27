"""공통 데이터 모델.

로그/배포 이벤트는 가벼운 dataclass, 에이전트가 제출하는 RCA 보고서는
스키마 검증이 필요하므로 pydantic 모델을 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

# 프로젝트 전체에서 사용하는 표준 시간대 (한국)
KST = timezone(timedelta(hours=9), "KST")


@dataclass
class LogRecord:
    """정규화된 로그 한 줄."""

    ts: datetime
    service: str
    level: str
    component: str
    message: str
    id: int | None = None
    template_id: int | None = None


@dataclass
class DeployEvent:
    """배포/설정 변경 이벤트."""

    ts: datetime
    service: str
    kind: str  # "deploy" | "config"
    description: str
    id: int | None = None


class WhyStep(BaseModel):
    """5 Whys 체인의 한 단계. 반드시 로그 증거를 인용해야 한다."""

    question: str = Field(description="이 단계에서 던진 '왜?' 질문")
    answer: str = Field(description="증거에 기반한 답변")
    evidence_log_ids: list[int] = Field(
        default_factory=list, description="답변의 근거가 되는 로그 id 목록"
    )


class RCAReport(BaseModel):
    """에이전트가 최종 제출하는 근본원인분석 보고서."""

    summary: str = Field(description="장애 증상과 영향 요약 (2~3문장)")
    five_whys: list[WhyStep] = Field(min_length=3, description="왜? 체인 (3단계 이상)")
    root_cause: str = Field(description="근본 원인 설명")
    root_cause_service: str = Field(description="근본 원인이 위치한 서비스 이름")
    culprit_change: str | None = Field(
        default=None, description="원인이 된 배포/설정 변경 (있는 경우)"
    )
    remediation: list[str] = Field(min_length=1, description="재발 방지 대책 목록")
    similar_incident: str | None = Field(
        default=None, description="과거 유사 장애 문서 (있는 경우)"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="분석 신뢰도 (0~1)")
