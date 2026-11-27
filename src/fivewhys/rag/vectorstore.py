"""인메모리 벡터 저장소 (numpy 코사인 유사도 top-k)."""

from __future__ import annotations

import numpy as np


class VectorStore:
    def __init__(self, dim: int) -> None:
        self.dim = dim
        self._ids: list[int] = []
        self._matrix = np.zeros((0, dim), dtype=np.float32)

    def add(self, chunk_id: int, vector: list[float]) -> None:
        arr = np.asarray(vector, dtype=np.float32).reshape(1, self.dim)
        self._matrix = np.vstack([self._matrix, arr])
        self._ids.append(chunk_id)

    def search(self, query_vector: list[float], k: int = 10) -> list[tuple[int, float]]:
        if not self._ids:
            return []
        q = np.asarray(query_vector, dtype=np.float32)
        # 벡터는 임베딩 단계에서 L2 정규화되어 있으므로 내적 == 코사인 유사도
        scores = self._matrix @ q
        order = np.argsort(-scores)[:k]
        return [(self._ids[i], float(scores[i])) for i in order]

    def __len__(self) -> int:
        return len(self._ids)
