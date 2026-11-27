"""Run identical queries against the seeded live services and print paired recall/MRR."""

import argparse
import json
import hashlib
import math
import os
import random
import time
from pathlib import Path
from statistics import mean, median
from uuid import uuid4

from fivewhys.investigation.retrieval import TOP_K, _stores, research, seed_corpus, vector_search
from fivewhys.investigation.retrieval_corpus import seed_rows
from fivewhys.scenarios.catalog import get_scenario


def metrics(rows, gold):
    found = {row["id"] for row in rows}.intersection(gold)
    rank = next((index for index, row in enumerate(rows, start=1) if row["id"] in gold), None)
    return {"recall": len(found) / len(gold), "reciprocal_rank": 1 / rank if rank else 0.0}


def _paired_latency(cases, repeats):
    if repeats < 1:
        raise ValueError("repeats must be positive")
    rng = random.Random(42)
    samples = {"vector": [], "graph": []}
    for _ in range(repeats):
        for case in rng.sample(cases, len(cases)):
            for name in rng.sample(list(samples), len(samples)):
                method = vector_search if name == "vector" else research
                started = time.perf_counter_ns()
                method(case["question"], case["team"])
                samples[name].append((time.perf_counter_ns() - started) / 1e6)
    return {name: {"n": len(ms), "p50_ms": median(ms), "p95_ms": sorted(ms)[math.ceil(.95 * len(ms)) - 1],
                   "samples_ms": ms} for name, ms in samples.items()}


def _measure(repeats):
    cases = json.loads(Path(__file__).with_name("retrieval_gold.json").read_text(encoding="utf-8"))
    corpus = seed_rows()
    change_label = get_scenario('db_pool_exhaustion', 42).ground_truth.culprit_change_match
    results = []
    manifest = {r["id"]: r for r in corpus}
    checked = 0
    for case in cases:
        coordinates = {tuple(source) for source in case["relevant"]}
        gold = {r["id"] for r in corpus if (r["source"], r["locator"]) in coordinates and case["team"] in r["teams"]}
        if len(gold) != len(coordinates):
            raise ValueError(f"gold contains missing or inaccessible passages for {case['name']}")
        run = research(case["question"], case["team"])
        result = {**case, "gold_ids": sorted(gold)}
        for mode in ("vector", "graph"):
            for row in run[mode]:
                if manifest.get(row["id"]) != row or case["team"] not in row["teams"]:
                    raise ValueError("retrieved evidence differs from the authorized seed manifest")
                checked += 1
            result[mode] = {**metrics(run[mode], gold), "ids": [row["id"] for row in run[mode]],
                            "sources": [[r["source"], r["locator"]] for r in run[mode]]}
            if case["asks_for_culprit_change"]:
                rank = next((i for i, row in enumerate(run[mode], start=1)
                             if row["kind"] == "deployment" and row["origin"] == "scenario"
                             and change_label in row["text"]), None)
                result[mode]["culprit_rank"] = rank
        result["same_order"] = result["vector"]["ids"] == result["graph"]["ids"]
        result["graph_paths"] = run["paths"]
        result["decisions"] = run["decisions"]
        results.append(result)
    summary = {mode: {metric: mean(r[mode][metric] for r in results) for metric in ("recall", "reciprocal_rank")}
               for mode in ("vector", "graph")}
    for mode in ("vector", "graph"):
        ranks = [r[mode]["culprit_rank"] for r in results if r["asks_for_culprit_change"]]
        summary[mode]["culprit_questions"] = len(ranks)
        summary[mode]["culprit_hit_at_k"] = mean(int(rank is not None) for rank in ranks)
        summary[mode]["culprit_mrr"] = mean(1/rank if rank else 0.0 for rank in ranks)
    return {"incident": "db_pool_exhaustion", "seed": 42, "k": TOP_K,
            "manifest_sha256": hashlib.sha256("\n".join(sorted(manifest)).encode()).hexdigest(),
            "seed_chunks": len(manifest), "returned_rows_checked": checked,
            "collection_isolation": "fresh run-scoped Milvus collection; all ranked evidence verified against seed IDs",
            "summary": summary, "cases": results, "latency": _paired_latency(cases, repeats),
            "measurement": "Sequential warm services, paired queries; shuffled case and method order, seed 42; wall clock including embedding and retrieval, excluding LLM and job scheduling."}


def evaluate(repeats=5):
    """CLI process only: seed a fresh collection so operational uploads never enter rankings."""
    client, _, _ = _stores()
    temporary = "fivewhys_eval_" + uuid4().hex
    if client.has_collection(temporary):
        raise RuntimeError("benchmark collection unexpectedly already exists")
    previous = os.environ.get("MILVUS_COLLECTION")
    os.environ["MILVUS_COLLECTION"] = temporary
    try:
        seed_corpus()
        return _measure(repeats)
    finally:
        if previous is None:
            os.environ.pop("MILVUS_COLLECTION", None)
        else:
            os.environ["MILVUS_COLLECTION"] = previous
        if client.has_collection(temporary):
            client.drop_collection(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=5, help="paired warm latency trials per case")
    args = parser.parse_args()
    result = json.dumps(evaluate(args.repeats), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(result + "\n", encoding="utf-8")
    else:
        print(result)
