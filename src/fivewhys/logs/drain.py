"""Drain 방식의 온라인 로그 템플릿 마이닝 (단순화 구현).

원 논문(He et al., "Drain: An Online Log Parsing Approach with Fixed Depth
Tree", ICWS 2017)의 핵심 아이디어를 그대로 따른다:

1. 가변 파라미터(숫자, IP, UUID, hex id)를 먼저 마스킹한다.
2. (토큰 수, 앞쪽 k개 토큰)을 키로 하는 고정 깊이 prefix tree로 후보
   클러스터를 좁힌다.
3. 후보들과 토큰 단위 유사도를 계산해 임계치 이상이면 기존 클러스터에
   병합하고, 서로 다른 토큰 위치는 와일드카드 <*>로 치환한다.

대용량 로그를 소수의 "템플릿"으로 압축해 주기 때문에, 템플릿 단위 빈도
비교로 장애 구간의 이상 패턴을 찾는 기반이 된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_MASKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"), "<UUID>"),
    (re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$"), "<IP>"),
    (re.compile(r"^0x[0-9a-fA-F]+$"), "<HEX>"),
    (re.compile(r"^[0-9a-fA-F]{8,}$"), "<HEX>"),
    (re.compile(r"^-?\d+(\.\d+)?(ms|s|MB|GB|KB|%)?$"), "<NUM>"),
]

_TRAILING_PUNCT = ",.;:)]}\"'"
_LEADING_PUNCT = "([{\"'"


def mask_token(token: str) -> str:
    """토큰 하나에서 가변 파라미터를 마스킹한다."""
    lead = ""
    trail = ""
    core = token
    while core and core[0] in _LEADING_PUNCT:
        lead += core[0]
        core = core[1:]
    while core and core[-1] in _TRAILING_PUNCT:
        trail = core[-1] + trail
        core = core[:-1]
    if not core:
        return token
    for pattern, repl in _MASKS:
        if pattern.match(core):
            return lead + repl + trail
    return token


def tokenize(message: str) -> list[str]:
    return [mask_token(t) for t in message.split()]


@dataclass
class _Cluster:
    id: int
    tokens: list[str]
    count: int = 0
    example: str = ""


@dataclass
class DrainMiner:
    """온라인 템플릿 마이너.

    depth: prefix tree에서 키로 쓰는 선행 토큰 수
    sim_threshold: 같은 클러스터로 병합할 최소 토큰 일치 비율
    """

    depth: int = 2
    sim_threshold: float = 0.5
    _tree: dict[tuple, list[int]] = field(default_factory=dict)
    _clusters: dict[int, _Cluster] = field(default_factory=dict)
    _next_id: int = 1

    def _key(self, tokens: list[str]) -> tuple:
        prefix = []
        for tok in tokens[: self.depth]:
            # 파라미터성 토큰이 키가 되면 트리가 폭발하므로 와일드카드로
            prefix.append("<*>" if tok.startswith("<") and tok.endswith(">") else tok)
        return (len(tokens), *prefix)

    @staticmethod
    def _similarity(a: list[str], b: list[str]) -> float:
        """원 논문의 seqDist: 템플릿의 <*> 위치는 분자에 넣지 않는다.

        와일드카드를 일치로 세면 병합될 때마다 <*>가 늘어 유사도가 부풀고,
        결국 임계치 이상이 와일드카드인 템플릿이 같은 길이의 모든 메시지를
        흡수하는 양성 피드백(폭주 병합)이 생긴다.
        """
        if not a:
            return 0.0
        same = sum(1 for x, y in zip(a, b) if x == y and x != "<*>")
        return same / len(a)

    def add(self, message: str) -> int:
        """메시지를 추가하고 배정된 템플릿(클러스터) id를 반환한다."""
        tokens = tokenize(message)
        if not tokens:
            tokens = ["<EMPTY>"]
        key = self._key(tokens)
        candidates = self._tree.setdefault(key, [])

        best_id, best_sim = -1, 0.0
        for cid in candidates:
            sim = self._similarity(self._clusters[cid].tokens, tokens)
            if sim > best_sim:
                best_id, best_sim = cid, sim

        if best_id >= 0 and best_sim >= self.sim_threshold:
            cluster = self._clusters[best_id]
            cluster.tokens = [
                a if (a == b or a == "<*>") else "<*>"
                for a, b in zip(cluster.tokens, tokens)
            ]
            cluster.count += 1
            return cluster.id

        cid = self._next_id
        self._next_id += 1
        self._clusters[cid] = _Cluster(id=cid, tokens=tokens, count=1, example=message)
        candidates.append(cid)
        return cid

    def template(self, cluster_id: int) -> str:
        return " ".join(self._clusters[cluster_id].tokens)

    def templates(self) -> dict[int, str]:
        return {cid: self.template(cid) for cid in self._clusters}

    def count(self, cluster_id: int) -> int:
        return self._clusters[cluster_id].count
