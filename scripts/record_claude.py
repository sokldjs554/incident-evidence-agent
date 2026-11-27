#!/usr/bin/env python3
"""Capture one platform job through the local Compose stack. No fixture mode."""
import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4


QUESTION = "배포 이후 주문 서비스의 응답이 느려졌다. 어떤 변경이 관련돼 있나?"
PAYLOAD = {"question": QUESTION, "engine": "claude", "callback": True}
ROOT = Path(__file__).resolve().parents[1]


class CaptureError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise CaptureError(message)


def utc():
    return datetime.now(timezone.utc).isoformat()


def encode(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()


class Archive:
    def __init__(self, directory):
        self.path = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)

    def write(self, name, content):
        if not isinstance(content, bytes):
            content = encode(content)
        # Cookies, bearer tokens, API keys and DSNs are never evidence.
        for key in ("ANTHROPIC_API_KEY", "FIVEWHYS_PLATFORM_TOKEN", "FIVEWHYS_ORDERS_TOKEN", "FIVEWHYS_SESSION_KEY", "NEO4J_PASSWORD"):
            value = os.environ.get(key)
            require(not value or value.encode() not in content, "기록에 인증 정보가 포함됨")
        require(not re.search(rb"sk-(?:ant-|proj-)[A-Za-z0-9_-]{8,}|Bearer\s+[^\s,;]+|"
                              rb"[a-z][a-z0-9+.-]*://[^/\s:]+:[^/\s@]+@", content, re.I),
                "기록에 비밀값 패턴이 포함됨")
        path = self.path / name
        with path.open("xb") as out:
            out.write(content)
        path.chmod(0o400)

    def finish(self):
        rows = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
                for p in sorted(self.path.iterdir()) if p.is_file()]
        self.write("SHA256SUMS", "".join(rows).encode())


def compose(*args, stdin=None):
    completed = subprocess.run(["docker", "compose", "exec", "-T", *args], input=stdin,
                               cwd=ROOT, text=True, capture_output=True, timeout=60)
    require(completed.returncode == 0, f"Compose 서비스 명령 실패 ({args[0]})")
    return completed.stdout


def sql(statement):
    return compose("postgres", "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
                   "-U", "fivewhys", "-d", "fivewhys", stdin=statement)


WORKER_CONFIG = """
import json, os
import anthropic
with anthropic.Anthropic(max_retries=0) as client:
    print(json.dumps({"official_messages_endpoint": str(client.base_url).rstrip("/") == "https://api.anthropic.com",
                      "configured_model": os.getenv("FIVEWHYS_MODEL", "claude-opus-5"),
                      "source_revision": os.getenv("FIVEWHYS_REVISION", "local"),
                      "sdk_version": anthropic.__version__}))
"""

# UUIDs are parsed before SQL interpolation. One SELECT takes a single MVCC snapshot.
def snapshot(job_id, capture_id):
    job, capture = UUID(job_id), UUID(capture_id)
    return json.loads(sql(f"""
        SELECT json_build_object(
          'captured_at', clock_timestamp(), 'id', j.id, 'team', j.team, 'kind', j.kind,
          'payload', j.payload, 'runs', j.runs, 'llm_calls', j.llm_calls, 'status', j.status,
          'finished_at', j.finished_at, 'error_code', j.error_code, 'callback_status', j.callback_status,
          'result_sha256', encode(sha256(convert_to(j.result::text,'UTF8')), 'hex'),
          'callbacks', (SELECT json_agg(row_to_json(c) ORDER BY c.attempt)
                        FROM investigation_callbacks c WHERE c.job_id=j.id),
          'receipt', (SELECT row_to_json(r) FROM investigation_receipts r WHERE r.job_id=j.id),
          'trace', o.trace
        ) FROM investigation_jobs j JOIN investigation_observations o ON o.job_id=j.id AND o.team=j.team
          WHERE j.id='{job}' AND o.capture_id='{capture}' AND j.team='platform'
    """))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class LocalAPI:
    def __init__(self, port, archive):
        self.url = f"http://127.0.0.1:{port}"
        self.archive = archive
        self.client = build_opener(NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.headers = {"Content-Type": "application/json", "X-Expected-Team": "platform"}
        if token := os.environ.get("FIVEWHYS_PLATFORM_TOKEN"):
            self.headers["Authorization"] = "Bearer " + token

    def call(self, path, *, data=None, headers=None):
        req = Request(self.url + path, data=encode(data) if data is not None else None,
                      headers={**self.headers, **(headers or {})})
        try:
            reply = self.client.open(req, timeout=10)
        except HTTPError as exc:
            reply = exc
        with reply:
            # Never serialize response headers wholesale (demo auth sets a cookie).
            meta = {"at": utc(), "method": req.get_method(),
                    "path": path, "status": reply.status, "location": reply.headers.get("Location")}
            return meta, reply.read()

    def save(self, name, record, expected):
        meta, body = record
        self.archive.write(name + ".http.json", meta)
        self.archive.write(name + ".body.json", body)
        require(meta["status"] == expected, f"{name} HTTP {meta['status']} (예상 {expected})")
        return json.loads(body)

    def wait(self, job_path, target, name, timeout):
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            last = self.call(job_path)
            if last[0]["status"] != 200:
                break
            status = json.loads(last[1])["callback_status"]
            if status in {target, "delivered", "exhausted"}:
                break
            time.sleep(0.5)
        require(last is not None, "조회 시간 초과")
        job = self.save(name, last, 200)
        require(job["callback_status"] == target, f"{target} 상태를 관측하지 못함")
        return job


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_delivery(job, before, after, api_before, api_after, result):
    for pg, api, state, codes in ((before, api_before, "retrying", [503]),
                                  (after, api_after, "delivered", [503, 200])):
        require(pg["id"] == api["id"] == job and pg["team"] == api["team"] == "platform"
                and pg["payload"] in (PAYLOAD, {**PAYLOAD, "scenario": "db_pool_exhaustion", "seed": 42})
                and pg["kind"] == api["kind"] == "analysis", "작업 식별 불일치")
        require(pg["status"] == api["status"] == "completed" and pg["callback_status"] == api["callback_status"] == state,
                "작업 또는 콜백 상태 불일치")
        expected = list(enumerate(codes, 1))
        for record in (pg, api):
            require([(c["attempt"], c["status_code"]) for c in record["callbacks"]] == expected,
                    "콜백 시도 번호 또는 응답 코드 불일치")
        require(pg["llm_calls"] == api["llm_calls"] and pg["runs"] == 1, "실행/호출 수 불일치")
        require(pg["receipt"]["job_id"] == job and pg["receipt"]["attempts"] == len(codes),
                "수신기 영수증 불일치")
    require(before["receipt"]["accepted_at"] is None and after["receipt"]["accepted_at"], "수신 완료 시점 불일치")
    for key in ("finished_at", "llm_calls", "result_sha256", "trace"):
        require(before[key] == after[key], f"콜백 재전송 시 {key} 변경")
    require(bool(re.fullmatch("[0-9a-f]{64}", before["result_sha256"] or "")), "결과 해시 누락")
    terminal = {"id": job, "team": "platform", "kind": "analysis", "status": "completed",
                "result": result, "error": None, "error_code": None}
    require(before["receipt"]["digest"] == after["receipt"]["digest"] == canonical_digest(terminal),
            "API 결과와 콜백 수신 본문 해시 불일치")


def validate_provider_response(reply):
    ids = [reply.get("message_id") or "", reply.get("request_id") or ""]
    stable_ids = (re.fullmatch(r"msg_[A-Za-z0-9_-]+", ids[0]) and
                  (not ids[1] or re.fullmatch(r"req_[A-Za-z0-9_-]+", ids[1])))
    identity = " ".join(ids + [reply.get("model") or ""]).lower()
    artificial = any(word in identity for word in ("fixture", "mock", "fake", "scripted"))
    require(reply.get("provider_endpoint") == "api.anthropic.com" and
            (reply.get("model") or "").startswith("claude-") and stable_ids and not artificial,
            "공급자 응답 식별자 또는 모델 확인 실패")


def validate_model(pg, result):
    trace = pg["trace"]
    attempts = trace["provider_attempts"]
    require(result["engine"] == "claude" and len(attempts) == pg["llm_calls"] > 0, "Claude 호출 수 불일치")
    replies = [a for a in attempts if a["status"] == "returned"]
    require(len(replies) == result["engine_turns"] > 0, "공급자 응답과 논리 턴 수 불일치")
    require(trace.get("omitted_tool_calls", 0) == 0, "일부 도구 호출 기록이 생략됨")
    require(trace.get("omitted_submissions", 0) == 0, "일부 모델 제출 기록이 생략됨")
    for reply in replies:
        validate_provider_response(reply)
    returned = {}
    searches = {call["name"] for call in trace["tool_calls"] if call["status"] == "returned"}
    require({"search_logs", "search_deployments", "search_reports"} <= searches, "세 검색 도구의 반환 기록 누락")
    for call in trace["tool_calls"]:
        for row in call.get("returned", []):
            returned[row["id"]] = row
    evidence = {row["id"]: row for row in result["evidence"]}
    submissions = trace["submissions"]
    require(submissions and submissions[-1]["status"] == "accepted", "검증된 모델 제출 누락")
    # Pydantic trims strings during validation. The archived first and final
    # submission snapshots always retain the text the SDK actually returned.
    submitted = [{"title": h["title"].strip(), "description": h["description"].strip(),
                  "evidence_ids": [cid.strip() for cid in h["evidence_ids"]]}
                 for h in submissions[-1]["hypotheses"]]
    require(submitted and submitted == [{k: h[k] for k in ("title", "description", "evidence_ids")}
                                        for h in result["hypotheses"]], "모델 제출과 저장 결과 불일치")
    for candidate in submitted:
        for cid in candidate["evidence_ids"]:
            require(cid in returned and cid in evidence, "모델이 받은 원문에서 인용을 찾을 수 없음")
            row = evidence[cid]
            excerpt = row["text"] if len(row["text"]) <= 1200 else row["text"][:1200] + "…"
            require(all(returned[cid][k] == row[k] for k in ("id", "source", "version", "locator"))
                    and returned[cid]["excerpt"] == excerpt, "도구 원문과 결과 인용 불일치")


REPRESENTATIVE = {
    "changes/order-service-1320.txt", "logs/mysql/slowlog-WARN.log",
    "logs/order-service/db-pool-WARN.log", "logs/order-service/db-pool-ERROR.log",
}

ORIGINAL_SOURCES = """
import json, sys
from fivewhys.investigation.retrieval_corpus import seed_rows
cited = set(json.load(sys.stdin))
references = {"scenario/db_pool_exhaustion/seed-42/" + suffix for suffix in %r}
rows = [{k:r[k] for k in ("id","kind","service","source","version","locator","text")}
        for r in seed_rows() if r["id"] in cited or r["source"] in references]
print(json.dumps(rows, ensure_ascii=False))
""" % sorted(REPRESENTATIVE)


def validate_sources(originals, result):
    rows = {row["id"]: row for row in originals}
    require({"scenario/db_pool_exhaustion/seed-42/" + s for s in REPRESENTATIVE}
            <= {r["source"] for r in originals}, "원문 대조 기준의 출처 누락")
    evidence = {row["id"]: row for row in result["evidence"]}
    for candidate in result["hypotheses"]:
        for cid in candidate["evidence_ids"]:
            require(cid in rows and cid in evidence and all(evidence[cid][k] == v for k, v in rows[cid].items()),
                    "인용과 시드 원문의 ID·버전·위치·본문 불일치")


def quote(text):
    # Indented Markdown renders arbitrary model/source strings literally.
    return "\n".join("    " + line for line in str(text).split("\n"))


def report(archive, state, error):
    pg = state.get("after") or state.get("latest") or state.get("before") or {}
    trace = pg.get("trace") or {}
    result = state.get("result") or {}
    lines = ["# 주문 지연 조사 기록", "", "## 질문", "", quote(QUESTION), "",
             f"작업 ID `{state.get('job', '미접수')}` · 팀 `platform` · 요청 엔진 `claude`",
             "", "기술 대조 " + ("실패 · " + error if error else "통과 · 근거 해석은 수동 검토 대상")]
    if "accepted" in state:
        lines.append(f"\n분석 접수 HTTP {state['accepted']['status']} · 조회 경로 `{state['accepted']['location']}`")
    lines.extend(["", "## 공급자 응답", "",
                  f"PostgreSQL 호출 예약 {pg.get('llm_calls', '미확인')}회 · "
                  f"완료 결과의 논리 턴 {result.get('engine_turns', '미확인')}회. "
                  "usage는 공급자가 반환한 필드만 기록한다.", ""])
    for attempt in trace.get("provider_attempts", []):
        lines.append(quote(json.dumps(attempt, ensure_ascii=False)))
    lines.extend(["", "## 모델이 선택한 도구와 검색어", ""])
    for call in trace.get("tool_calls", []):
        lines.extend([f"- 턴 {call['turn']} · `{call['name']}` · `{call['status']}`", "",
                      quote(call.get("query", "")), ""])
        for row in call.get("returned", []):
            lines.append(quote(f"{row['id']} · {row['source']} {row['locator']} · {row['version']}"))
    lines.extend(["", "## 합성 사건 원문과 인용 여부", "",
                  "동일 리비전과 시드에서 재생성한 기록이다. 번호는 저장된 최종 후보 순번이다.", ""])
    for row in state.get("originals", []):
        cited = [str(n) for n, h in enumerate(result.get("hypotheses", []), 1) if row["id"] in h["evidence_ids"]]
        lines.extend([quote(f"{row['id']} · {row['source']} {row['locator']} · {row['version']}\n"
                            f"최종 후보 인용: {', '.join(cited) if cited else '없음'}\n{row['text']}"), ""])
    lines.extend(["", "## 검색기의 선택", "",
                  "질문에 대한 최초 검색과 도구가 모델에 반환한 선택 사유를 순서대로 기록했다.", ""])
    for source, decisions in [("질문 최초 검색", result.get("decisions", [])),
                              *((f"{c['turn']}턴 {c['name']}", c.get("retrieval_decisions", []))
                                for c in trace.get("tool_calls", []))]:
        for decision in decisions:
            lines.extend([quote(f"{source} · {decision['evidence_id']} · {decision['action']} · {decision['reason']}"), ""])
    lines.extend(["## Claude 제출 원문과 검증 결과", "",
                  "제출 순서, 모델이 쓴 설명과 인용 ID를 보존한다. accepted와 rejected는 서버의 검증 결과다.", ""])
    for entry in trace.get("submissions", []):
        lines.extend([quote(json.dumps(entry, ensure_ascii=False, indent=2)), ""])
    lines.extend(["## 모델에 반환된 원문과 최종 인용", ""])
    refs = {}
    for i, h in enumerate(result.get("hypotheses", []), 1):
        for cid in h["evidence_ids"]:
            refs.setdefault(cid, []).append(str(i))
    rows = {r["id"]: r for c in trace.get("tool_calls", []) for r in c.get("returned", [])}
    for cid, row in rows.items():
        usage = "최종 후보 " + ", ".join(refs[cid]) if cid in refs else "최종 인용 없음"
        lines.extend([quote(f"{cid} · {usage}\n{row['source']} · {row['version']} · {row['locator']}\n"
                            + row["excerpt"]), ""])
    if result.get("hypotheses"):
        lines.extend(["## Claude의 최종 판단", ""])
        for n, h in enumerate(result["hypotheses"], 1):
            lines.extend([f"후보 {n}", "", quote(h["title"]), "", quote(h["description"]), "",
                          quote("인용 ID · " + ", ".join(h["evidence_ids"])), ""])
    lines.extend(["## 같은 작업의 콜백 전후", "",
                  "| PostgreSQL 스냅샷 | 작업 ID | 상태 | runs | llm_calls | 콜백 HTTP | result SHA-256 |",
                  "|---|---|---|---:|---:|---|---|"])
    for name in ("before", "after"):
        if row := state.get(name):
            codes = ", ".join(str(c["status_code"]) for c in row["callbacks"] or [])
            lines.append(f"| {name} | {row['id']} | {row['callback_status']} | {row['runs']} | {row['llm_calls']} "
                         f"| {codes} | {row['result_sha256']} |")
    lines.extend(["", "접수 응답은 accepted.http.json과 accepted.body.json, 원문 제출은 pg-before.json의 trace,"
                  " 완료 결과는 result.body.json, 수신기 본문 해시는 pg-before.json과 pg-after.json의 receipt.digest에서 확인한다.",
                  "", "공개 전 검토자는 13:20 배포와 customer_ref 슬로우 쿼리, 연결 대기·타임아웃을 "
                  "최종 후보의 실제 문장 및 인용과 대조한다. 실행계획 확인 여부, 과거 보고서와 현재 관찰의 구분, "
                  "모델이 직접 제시한 대안이나 유보 판단을 별도로 기록한다.", ""])
    archive.write("REPORT.md", "\n".join(lines).encode())


def capture(api, archive, state, timeout):
    config = api.save("config", api.call("/v1/demo/config"), 200)
    require(config["claude_available"], "API의 Claude 인증이 구성되지 않음")
    worker = json.loads(compose("worker", "python", "-c", WORKER_CONFIG))
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(bool(re.fullmatch(r"[0-9a-f]{40}", revision)), "소스 리비전 확인 실패")
    archive.write("worker.json", {**worker, "local_revision": revision})
    require(config.get("source_revision") == worker["source_revision"] == revision,
            "API·워커·로컬 Git 리비전 불일치")
    dirty = subprocess.check_output(["git", "status", "--porcelain", "--", "src", "Dockerfile", "docker-compose.yml",
                                     "pyproject.toml", "scripts/record_claude.py"], cwd=ROOT, text=True)
    require(not dirty, "실행에 사용될 코드에 미커밋 변경이 있음")
    require(worker["official_messages_endpoint"], "워커의 공식 Anthropic API 주소를 확인하지 못함")
    if "Authorization" not in api.headers:
        api.save("session", api.call("/v1/demo/session", data={"team": "platform"}), 200)
    capture_id = str(uuid4())
    state["capture_id"] = capture_id
    archive.write("request.json", {"at": utc(), "path": "/v1/analyses", "capture_id": capture_id, "body": PAYLOAD})
    sql(f"INSERT INTO investigation_observations(capture_id,team) VALUES ('{capture_id}','platform');")
    accepted = api.call("/v1/analyses", data=PAYLOAD, headers={"X-Investigation-Observation": capture_id})
    state["accepted"] = accepted[0]
    job = str(UUID(api.save("accepted", accepted, 202)["id"]))
    state["job"] = job
    path = f"/v1/jobs/{job}"
    require(accepted[0]["location"] == path, "202 Location의 작업 ID 불일치")
    initial = api.wait(path, "retrying", "job-before", timeout)
    before = snapshot(job, capture_id)
    archive.write("pg-before.json", before)
    state["before"] = before
    # Read the result even if validation later fails. Keep the original model submissions.
    if initial["status"] == "completed":
        state["result"] = api.save("result", api.call(path + "/result"), 200)
        cited = {cid for h in state["result"]["hypotheses"] for cid in h["evidence_ids"]}
        originals = json.loads(compose("worker", "python", "-c", ORIGINAL_SOURCES, stdin=json.dumps(sorted(cited))))
        archive.write("source-check.json", originals)
        state["originals"] = originals
    retry = api.save("retry", api.call(path + "/callback/retry", data={}), 202)
    require(retry["id"] == job, "콜백 재전송 작업 ID 불일치")
    final = api.wait(path, "delivered", "job-after", timeout)
    after = snapshot(job, capture_id)
    archive.write("pg-after.json", after)
    state["after"] = after
    require(initial["status"] == "completed", "조사 실패 · " + str(initial.get("error_code")))
    validate_delivery(job, before, after, initial, final, state["result"])
    validate_model(after, state["result"])
    validate_sources(state["originals"], state["result"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="새 비공개 출력 디렉터리")
    parser.add_argument("--timeout", type=int, default=600, help="단계별 대기 한도(초)")
    args = parser.parse_args()
    require(args.timeout > 0, "대기 한도는 양수여야 함")
    archive = Archive(args.out.resolve())
    state, error = {}, None
    try:
        api = LocalAPI(int(os.getenv("FIVEWHYS_API_PORT", "8750")), archive)
        capture(api, archive, state, args.timeout)
    except KeyboardInterrupt:
        error = "작업자가 기록을 중단함"
    except CaptureError as exc:
        error = str(exc)
    except Exception as exc:
        # Exceptions from SDKs, HTTP and subprocesses may contain credentials.
        error = type(exc).__name__
    finally:
        # A slow callback or recorder interruption may still leave a first reply in the DB.
        if error and state.get("job") and "after" not in state:
            try:
                latest = snapshot(state["job"], state["capture_id"])
                archive.write("pg-latest.json", latest)
                state["latest"] = latest
            except Exception:
                pass
        report(archive, state, error)
        archive.write("outcome.json", {"at": utc(), "job_id": state.get("job"), "technical_checks_passed": error is None,
                                       "failure": error, "publication": "requires_human_review"})
        archive.finish()
    print(f"{archive.path} · {error or '기술 대조 통과, 원문 검토 대기'}")
    return int(error is not None)


if __name__ == "__main__":
    sys.exit(main())
