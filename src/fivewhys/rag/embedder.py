"""임베딩 추상화 + 기본 구현.

Embedder 프로토콜만 만족하면 어떤 임베딩 모델이든 끼울 수 있다
(예: sentence-transformers, Voyage AI 등 — README 참고).

기본 구현 HashingEmbedder는 feature hashing(해싱 트릭) 기반의 문자
n-gram + 단어 TF 벡터라이저다. 외부 모델/네트워크 의존 없이 완전히
결정적으로 동작하므로 테스트와 오프라인 데모의 기반이 된다. 로그/장애
도메인은 전문 용어(HikariPool, OOM 등) 겹침이 신호의 대부분이라 이런
lexical 임베딩도 실용적인 베이스라인이 된다.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


_WORD_RE = re.compile(r"[0-9A-Za-z가-힣_]+")


def _stable_hash(feature: str) -> int:
    """플랫폼/프로세스와 무관하게 결정적인 해시 (built-in hash는 salt됨)."""
    return int.from_bytes(hashlib.md5(feature.encode("utf-8")).digest()[:8], "big")


class HashingEmbedder:
    def __init__(self, dim: int = 512, char_ngram: int = 3) -> None:
        self.dim = dim
        self.char_ngram = char_ngram

    def _features(self, text: str) -> dict[str, int]:
        text = text.lower()
        feats: dict[str, int] = {}
        words = _WORD_RE.findall(text)
        for w in words:
            feats[f"w:{w}"] = feats.get(f"w:{w}", 0) + 1
            if len(w) >= self.char_ngram:
                for i in range(len(w) - self.char_ngram + 1):
                    g = w[i : i + self.char_ngram]
                    feats[f"g:{g}"] = feats.get(f"g:{g}", 0) + 1
        return feats

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feat, count in self._features(text).items():
            h = _stable_hash(feat)
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec
