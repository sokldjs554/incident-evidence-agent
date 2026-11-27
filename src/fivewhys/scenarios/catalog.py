"""시나리오 카탈로그."""

from __future__ import annotations

from typing import Callable

from fivewhys.scenarios.generator import (
    ScenarioBundle,
    build_cache_stampede,
    build_cert_expiry,
    build_db_pool_exhaustion,
    build_disk_full,
    build_memory_leak,
)

SCENARIOS: dict[str, Callable[[int], ScenarioBundle]] = {
    "db_pool_exhaustion": build_db_pool_exhaustion,
    "memory_leak": build_memory_leak,
    "disk_full": build_disk_full,
    "cert_expiry": build_cert_expiry,
    "cache_stampede": build_cache_stampede,
}


def get_scenario(name: str, seed: int = 42) -> ScenarioBundle:
    if name not in SCENARIOS:
        raise KeyError(f"알 수 없는 시나리오: {name} (사용 가능: {', '.join(SCENARIOS)})")
    return SCENARIOS[name](seed)
