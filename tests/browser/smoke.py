"""Exercise the Korean desk against a running demo stack with its real worker.

python tests/browser/smoke.py --url http://localhost:8750 --output /tmp/fivewhys-captures
Requires Playwright for Python plus Chromium. No provider credentials are needed.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

VIEWPORTS = {"desktop": {"width": 1440, "height": 1000}, "mobile": {"width": 390, "height": 844}}


def fits(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Horizontal overflow"


def capture(page, output, name):
    if output:
        page.screenshot(path=output / (name + ".png"), animations="disabled")


def scenario(browser, url, output, name):
    small = name == "mobile"
    context = browser.new_context(viewport=VIEWPORTS[name], locale="ko-KR", has_touch=small)
    page = context.new_page()
    errors = []
    authenticated = []
    def record_request(request):
        if "/v1/jobs/" in request.url or request.url.endswith("/v1/analyses"):
            authenticated.append(request.headers.get("x-expected-team"))

    page.on("request", record_request)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    walkthrough_start = time.monotonic()
    page.goto(url)
    expect(page.locator("#run")).to_be_enabled()
    if small:
        page.locator("#team").select_option("platform")
        expect(page.locator("#run")).to_be_enabled()
    expect(page.locator("#question")).to_have_value("배포 이후 주문 서비스의 응답이 느려졌다. 어떤 변경이 관련돼 있나?")
    fits(page)
    capture(page, output, name + "-initial")
    request_start = time.monotonic()
    with page.expect_response(lambda r: r.url.endswith("/v1/analyses") and r.request.method == "POST") as accepted:
        page.locator("#run").click()
    assert accepted.value.status == 202
    job_id = accepted.value.json()["id"]
    expect(page.locator("#status")).to_have_attribute("data-state", "completed", timeout=90000)
    request_to_visible_result = time.monotonic() - request_start
    expect(page.locator("#retry")).to_be_enabled(timeout=45000)
    before = page.locator("#llm-count").inner_text()
    page.locator("#retry").click()
    expect(page.locator("#callback-status")).to_have_attribute("data-state", "delivered", timeout=45000)
    expect(page.locator("#llm-count")).to_have_text(before)
    expect(page.locator("#callback-history")).to_contain_text("HTTP 503")
    expect(page.locator("#callback-history")).to_contain_text("HTTP 200")
    expect(page.locator("#callback-history")).to_contain_text("재전송 전")
    expect(page.locator(".hypothesis").first).to_be_visible()
    expect(page.locator("#timeline .event").first).to_be_visible()
    expect(page.locator("#comparison .ranking")).to_have_count(2)
    page.evaluate("scrollTo(0,0)")
    fits(page)
    capture(page, output, name + "-investigation")
    page.locator("#findings").evaluate("e => e.scrollIntoView()")
    capture(page, output, name + "-hypotheses")
    opened_sources = []
    for label, kind in (("배포", "deployment"), ("로그", "log"), ("사고 보고서", "report")):
        source_button = page.locator(".hypothesis .citation").filter(has_text=re.compile("^" + label + " · ")).first
        with page.expect_response(lambda r: f"/jobs/{job_id}/evidence/" in r.url) as source:
            source_button.click()
        assert source.value.status == 200, source.value.url
        evidence = source.value.json()
        assert evidence["kind"] == kind
        expect(page.locator(".source-text")).to_have_text(evidence["text"], use_inner_text=True)
        fits(page)
        opened_sources.append({"kind": kind, "id": evidence["id"], "source": evidence["source"],
                               "locator": evidence["locator"], "characters": len(evidence["text"])})
        if kind == "log":
            capture(page, output, name + "-source")
        page.locator("#source-close").click()
    paths = page.locator("#comparison .path-detail")
    expect(paths.first).to_be_visible()
    paths.first.locator("summary").click()
    page.locator("#comparison").evaluate("e => e.scrollIntoView()")
    fits(page)
    capture(page, output, name + "-comparison")
    page.locator("#delivery").evaluate("e => e.scrollIntoView({block:'center'})")
    capture(page, output, name + "-delivery")
    current = context.request.get(f"{url}/v1/jobs/{job_id}").json()
    result = context.request.get(f"{url}/v1/jobs/{job_id}/result").json()
    assert [entry["status_code"] for entry in current["callbacks"]] == [503, 200]
    assert current["llm_calls"] == int(before) == 0
    current_team = "platform" if small else "orders"
    assert current["team"] == current_team
    assert all(current_team in item["teams"] for item in result["evidence"])
    assert authenticated and all(team == current_team for team in authenticated)
    other = context.new_page()
    other.goto(url)
    expect(other.locator("#team")).to_have_value(current_team)
    expect(other.locator("#run")).to_be_enabled()
    switched = "orders" if small else "platform"
    other.locator("#team").select_option(switched)
    expect(other.locator("#run")).to_be_enabled()
    expect(page.locator("#team")).to_have_value(switched)
    expect(page.locator("#run")).to_be_enabled()
    mismatch = context.request.get(f"{url}/v1/jobs/{job_id}", headers={"X-Expected-Team": current_team})
    assert mismatch.status == 409 and mismatch.headers.get("x-session-changed") == "1"
    other.close()
    expect(page.locator("#status")).to_have_text("실행 전")
    expect(page.locator(".hypothesis")).to_have_count(0)
    expect(page.locator("#callback-count")).to_have_text("0")
    assert context.request.get(f"{url}/v1/jobs/{job_id}").status == 404
    assert not errors, errors
    walkthrough_seconds = time.monotonic() - walkthrough_start
    context.close()
    job_elapsed = datetime.fromisoformat(current["finished_at"]) - datetime.fromisoformat(current["created_at"])
    return {
        "walkthrough_seconds": round(walkthrough_seconds, 3),
        "request_to_visible_result_seconds": round(request_to_visible_result, 3),
        "job_elapsed_seconds": round(job_elapsed.total_seconds(), 3),
        "opened_sources": opened_sources,
        "viewport": VIEWPORTS[name], "job": current, "engine": result["engine"],
        "hypotheses": len(result["hypotheses"]), "evidence": len(result["evidence"]),
        "vector": [item["id"] for item in result["vector"]],
        "graph": [item["id"] for item in result["graph"]], "paths": len(result["paths"]),
        "llm_calls_before_retry": int(before), "llm_calls_after_retry": current["llm_calls"],
        "other_team_job_http_status": 404, "stale_expected_team_http_status": mismatch.status,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8750")
    parser.add_argument("--output", type=Path, help="Save screenshots and observed metadata")
    parser.add_argument("--chromium", default=shutil.which("chromium") or shutil.which("chromium-browser"))
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.chromium)
        observed = {name: scenario(browser, args.url.rstrip("/"), args.output, name) for name in VIEWPORTS}
        browser.close()
    summary = json.dumps(observed, ensure_ascii=False, indent=2)
    if args.output:
        (args.output / "browser-run.json").write_text(summary + "\n", encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
