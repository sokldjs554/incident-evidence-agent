"""Compare vector and graph retrieval on two incident-scoped, fixed gold sets."""

import argparse
import hashlib
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from statistics import mean, median
from uuid import uuid4

from fivewhys.agent import five_whys
from fivewhys.investigation import analysis, retrieval, retrieval_corpus
from fivewhys.rag import embedder
from fivewhys.scenarios import generator
from fivewhys.scenarios.catalog import get_scenario

GOLD_DIR = Path(__file__).parent
DB = "db_pool_exhaustion"
CERT = "cert_expiry"
SEED = 42
MODES = ("vector", "graph")
GOLD_COMMIT = "429993f446ca62299a26f6f5ada152b354ae38e5"
CERT_GOLD_SHA = "5091f265d832285df3d933a538facc527a05d2578180b54ef72ff33f85898e9d"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def coordinate(row):
    return [row["source"], row["locator"]]


def score(rows, case, manifest, change_label):
    for row in rows:
        if manifest.get(row["id"]) != row or case["team"] not in row["teams"]:
            raise ValueError(f"Evidence outside the authorized manifest: {row['id']}")
    ranked = [coordinate(row) for row in rows]
    gold_ranks = [ranked.index(gold) + 1 if gold in ranked else None for gold in case["relevant"]]
    hits = [rank for rank in gold_ranks if rank]
    result = {
        "ids": [row["id"] for row in rows],
        "sources": ranked,
        "gold_ranks": gold_ranks,
        "recall": len(hits) / len(gold_ranks),
        "reciprocal_rank": 1 / min(hits) if hits else 0,
        "deployment_ranks": [i for i, row in enumerate(rows, 1) if row["kind"] == "deployment"],
    }
    if case.get("asks_for_culprit_change"):
        result["culprit_rank"] = next((i for i, row in enumerate(rows, 1) if row["kind"] == "deployment"
                                       and row["origin"] == "scenario" and change_label in row["text"]), None)
    return result


def measure_case(case, scenario, manifest, change_label):
    authorized = {tuple(coordinate(row)) for row in manifest.values() if case["team"] in row["teams"]}
    if not case["relevant"] or any(tuple(source) not in authorized for source in case["relevant"]):
        raise ValueError(f"Missing or inaccessible gold for {case['name']}")
    run = retrieval.research(case["question"], case["team"], scenario=scenario, seed=SEED)
    result = {**case, "graph_paths": run["paths"], "decisions": run["decisions"]}
    for mode in MODES:
        result[mode] = score(run[mode], case, manifest, change_label)
    return result


def summarize(cases):
    summary = {}
    for mode in MODES:
        values = [case[mode] for case in cases]
        out = {key: mean(row[key] for row in values) for key in ("recall", "reciprocal_rank")}
        changes = [row["culprit_rank"] for row in values if "culprit_rank" in row]
        if changes:
            out["culprit_questions"] = len(changes)
            out["culprit_hit_at_k"] = mean(rank is not None for rank in changes)
            out["culprit_mrr"] = mean(1 / rank if rank else 0 for rank in changes)
        out["questions_returning_deployment"] = sum(bool(row["deployment_ranks"]) for row in values)
        summary[mode] = out
    return summary


def compare_db(current, baseline_path):
    baseline = baseline_path.read_bytes()
    reference = json.loads(baseline)
    previous = {case["name"]: case for case in reference["cases"]}
    if set(previous) != {case["name"] for case in current["cases"]} or reference["k"] != retrieval.TOP_K:
        raise ValueError("DB question set or retrieval cutoff changed")
    result = {"baseline_sha256": hashlib.sha256(baseline).hexdigest(), "baseline_summary": reference["summary"], "cases": {}}
    for case in current["cases"]:
        old = previous[case["name"]]
        if any(case[key] != old[key] for key in ("question", "team", "relevant", "asks_for_culprit_change")):
            raise ValueError("DB questions, ACL or labels changed")
        result["cases"][case["name"]] = {
            mode: {
                "same_ranked_sources": case[mode]["sources"] == old[mode]["sources"],
                "same_ranked_ids": case[mode]["ids"] == old[mode]["ids"],
                "recall_delta": case[mode]["recall"] - old[mode]["recall"],
                "reciprocal_rank_delta": case[mode]["reciprocal_rank"] - old[mode]["reciprocal_rank"],
            } for mode in MODES
        }
    return result


def latency(cases, repeats):
    """Randomize case and arm order per repeat, recording every timed request."""
    rng = random.Random(42)
    measurements = {scenario: {mode: [] for mode in MODES} for scenario in cases}
    queue = [(scenario, case) for scenario, batch in cases.items() for case in batch]
    for trial in range(1, repeats + 1):
        for scenario, case in rng.sample(queue, len(queue)):
            for mode in rng.sample(MODES, len(MODES)):
                method = retrieval.vector_search if mode == "vector" else retrieval.research
                started = time.perf_counter_ns()
                method(case["question"], case["team"], scenario=scenario, seed=SEED)
                elapsed = (time.perf_counter_ns() - started) / 1e6
                measurements[scenario][mode].append({"trial": trial, "case": case["name"], "ms": elapsed})
    for arms in measurements.values():
        for mode, samples in arms.items():
            times = sorted(sample["ms"] for sample in samples)
            arms[mode] = {"n": len(times), "p50_ms": median(times),
                          "p95_ms": times[math.ceil(.95 * len(times)) - 1], "samples": samples}
    return measurements


def probe(case):
    """Preserve the complete final claim text and cited evidence for human adjudication."""
    try:
        result = analysis.investigate(
            {"question": case["question"], "engine": "scripted", "scenario": CERT, "seed": SEED},
            case["team"], lambda: None,
        )
    except analysis.InvestigationError as exc:
        if exc.code != "insufficient_evidence":
            raise
        return {"status": exc.code, "hypotheses": [], "cited_evidence": []}
    cited = {eid for hypothesis in result["hypotheses"] for eid in hypothesis["evidence_ids"]}
    return {"status": "completed", "hypotheses": result["hypotheses"], "tool_calls": result["tool_calls"],
            "cited_evidence": [row for row in result["evidence"] if row["id"] in cited]}


def evaluate(baseline_path, repeats):
    if repeats < 1:
        raise ValueError("Need at least one paired trial")
    db_gold = GOLD_DIR / "retrieval_gold.json"
    cert_gold = GOLD_DIR / "cert_expiry_gold.json"
    if digest(cert_gold) != CERT_GOLD_SHA:
        raise ValueError("Certificate gold differs from its preregistration")
    cert = json.loads(cert_gold.read_text())
    cases = {DB: json.loads(db_gold.read_text()), CERT: cert["cases"]}
    if cert["k"] != retrieval.TOP_K or cert["seed"] != SEED or cert["incident"] != CERT:
        raise ValueError("Study settings differ from preregistration")
    client, _, driver = retrieval._stores()
    temporary = "fivewhys_eval_" + uuid4().hex
    if client.has_collection(temporary):
        raise RuntimeError("Unexpected collection collision")
    previous = os.environ.get("MILVUS_COLLECTION")
    os.environ["MILVUS_COLLECTION"] = temporary
    try:
        for scenario in cases:
            retrieval.seed_corpus(scenario=scenario, seed=SEED)
        result = {
            "started_utc": datetime.now(timezone.utc).isoformat(),
            "source_revision": os.getenv("FIVEWHYS_REVISION", "unspecified"),
            "seed": SEED, "k": retrieval.TOP_K, "gold_commit": GOLD_COMMIT,
            "gold_sha256": {DB: digest(db_gold), CERT: digest(cert_gold)},
            "runtime_sha256": {Path(path).name: digest(path) for path in
                               (__file__, retrieval.__file__, retrieval_corpus.__file__, analysis.__file__,
                                generator.__file__, embedder.__file__, five_whys.__file__)},
            "servers": {"milvus": client.get_server_version(), "neo4j": driver.get_server_info().agent},
            "packages": {pkg: version(pkg) for pkg in ("pymilvus", "neo4j", "langchain-core")},
            "protocol": "Both scenarios in one fresh Milvus collection. Seeded real Neo4j paths. "
                        "Verify every returned row against its scenario/team manifest. Time independent "
                        "warm calls after one paired quality pass. Shuffle queries and arms with seed 42. "
                        "Graph includes vector retrieval, Neo4j and follow-up Milvus calls. No job, LLM or UI latency.",
            "scenarios": {},
        }
        for scenario, questions in cases.items():
            manifest = {r["id"]: r for r in retrieval_corpus.seed_rows(scenario=scenario, seed=SEED)}
            change_label = get_scenario(scenario, SEED).ground_truth.culprit_change_match
            if scenario == CERT and change_label is not None:
                raise ValueError("Generator now labels a causal change")
            measured = [measure_case(case, scenario, manifest, change_label) for case in questions]
            result["scenarios"][scenario] = {
                "seed_chunks": len(manifest), "manifest_sha256": hashlib.sha256(
                    "\n".join(sorted(manifest)).encode()).hexdigest(),
                "summary": summarize(measured), "cases": measured,
            }
        result["db_regression"] = compare_db(result["scenarios"][DB], baseline_path)
        result["latency"] = latency(cases, repeats)
        result["causal_probes"] = {case["name"]: probe(case) for case in cases[CERT] if case["causal_probe"]}
        return result
    finally:
        if previous is None:
            os.environ.pop("MILVUS_COLLECTION", None)
        else:
            os.environ["MILVUS_COLLECTION"] = previous
        if client.has_collection(temporary):
            client.drop_collection(temporary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True, help="Original DB retrieval-benchmark.json")
    parser.add_argument("--repeats", type=int, default=5, help="Independent warm trials per question and arm")
    args = parser.parse_args()
    print(json.dumps(evaluate(args.baseline, args.repeats), indent=2, ensure_ascii=False))
