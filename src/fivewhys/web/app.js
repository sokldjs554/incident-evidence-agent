"use strict";

const el = id => document.getElementById(id);
const kinds = {log: "로그", deployment: "배포", report: "사고 보고서", runbook: "운영 지침"};
const states = {queued: "대기 중", running: "분석 중", completed: "분석 완료", failed: "분석 실패"};
const deliveries = {
  disabled: "콜백 미사용", pending: "전송 대기", retrying: "전송 실패 · 재시도 대기",
  delivered: "결과 전달 완료", exhausted: "자동 재시도 종료"
};
const teamKey = "fivewhys.demo.team";
const validTeam = team => team === "orders" || team === "platform";
const state = {
  activeTeam: null, generation: 0, controller: new AbortController(), timer: null, ready: false, busy: false,
  sessionPending: false, config: null, job: null, result: null, runEngine: null,
  beforeRetry: null, retryPending: false, sourceSequence: 0, connectionFailures: 0, pollSequence: 0
};
let sessionChain = Promise.resolve();
const kstTime = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
});

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined && text !== null) item.textContent = String(text);
  return item;
}

function details(label, child, className = "technical") {
  const section = node("details", className);
  section.append(node("summary", "", label), child);
  return section;
}

function values(pairs) {
  const list = node("dl", "key-values");
  for (const [label, value] of pairs) {
    if (value === null || value === undefined || value === "") continue;
    const row = node("div");
    row.append(node("dt", "", label), node("dd", "", value));
    list.append(row);
  }
  return list;
}

function clock(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : kstTime.format(date);
}

async function api(path, options = {}) {
  const generation = state.generation;
  const {method = "GET", body, signal = state.controller.signal} = options;
  const headers = new Headers();
  if (body !== undefined) headers.set("Content-Type", "application/json");
  if (!path.startsWith("/v1/demo/") && state.activeTeam)
    headers.set("X-Expected-Team", state.activeTeam);
  const response = await fetch(path, {
    method, credentials: "same-origin", cache: "no-store", signal, headers,
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    if (guard(generation) && response.status === 409 && response.headers.get("X-Session-Changed") === "1") {
      resetTeam(storedTeam() || el("team").value);
      state.ready = false;
      controls();
      notice("데모 계정이 다른 탭에서 변경됐습니다. 현재 팀을 확인하고 다시 연결하세요.", "config");
    }
    const detail = data?.detail || data?.error;
    const readable = typeof detail === "string" ? detail : detail?.message;
    throw new Error(readable || "HTTP " + response.status);
  }
  return data;
}

function notice(text, action = "") {
  el("notice").hidden = !text;
  el("notice").textContent = text;
  el("reconnect").hidden = !action;
  el("reconnect").dataset.action = action;
}

function guard(generation) {
  return generation === state.generation;
}

function controls() {
  el("team").disabled = state.sessionPending;
  el("run").disabled = !state.ready || state.busy || !el("question").value.trim();
  el("engine").disabled = state.busy || !state.ready;
  el("retry").disabled = state.retryPending || state.sessionPending ||
    !state.job || state.job.callback_status !== "retrying";
}

function mode() {
  const claude = el("engine").value === "claude";
  el("mode-note").textContent = claude
    ? "Claude가 허용된 근거로 가설을 작성합니다. 실행 시 API 호출이 발생합니다."
    : "규칙 기반 엔진이 검색 결과를 분석합니다. 외부 LLM 호출은 0회입니다." +
      (state.config && !state.config.claude_available ? " Claude 연결은 설정되어 있지 않습니다." : "");
  if (!state.job) el("engine-tag").textContent = claude ? "Claude" : "Scripted";
}

function clearRun() {
  state.generation++;
  state.controller.abort();
  state.controller = new AbortController();
  clearTimeout(state.timer);
  state.job = null;
  state.result = null;
  state.runEngine = null;
  state.beforeRetry = null;
  state.retryPending = false;
  state.busy = false;
  state.connectionFailures = 0;
  state.sourceSequence++;
  if (el("source-dialog").open) el("source-dialog").close();
  el("source-content").replaceChildren();
  el("source-heading").textContent = "근거 원문";
  notice("");
  renderJob();
  renderResult();
  mode();
  return state.generation;
}

function storedTeam() {
  try {
    const team = localStorage.getItem(teamKey);
    return validTeam(team) ? team : null;
  } catch {
    return null;
  }
}

function resetTeam(team) {
  el("team").value = team;
  el("question").value = el("question").defaultValue;
  el("engine").value = "scripted";
  el("job-details").open = false;
  el("callback-details").open = false;
  state.activeTeam = null;
  return clearRun();
}

function followTeam(team) {
  resetTeam(team);
  state.activeTeam = team;
  state.ready = true;
  controls();
}

async function changeTeam(team) {
  const generation = resetTeam(team);
  state.sessionPending = true;
  state.ready = false;
  controls();
  const setSession = async () => {
    const result = await api("/v1/demo/session", {method: "POST", body: {team}, signal: null});
    if (result.team !== team) throw new Error("서버가 선택한 팀을 확인하지 못했습니다.");
    try { localStorage.setItem(teamKey, team); } catch { /* Per-request identity checks still apply. */ }
  };
  // Serialize cookie and storage updates, including requests from another tab.
  const session = sessionChain.then(() => navigator.locks
    ? navigator.locks.request(teamKey, setSession) : setSession());
  sessionChain = session.catch(() => {});
  try {
    await session;
    if (guard(generation)) {
      state.activeTeam = storedTeam() || team;
      el("team").value = state.activeTeam;
      state.ready = true;
    }
  } catch (error) {
    if (guard(generation)) notice("팀 세션을 열 수 없습니다. " + error.message, "config");
  } finally {
    if (guard(generation)) {
      state.sessionPending = false;
      controls();
    }
  }
}

window.addEventListener("storage", event => {
  if (event.key !== teamKey || !state.config?.demo_accounts || state.sessionPending) return;
  const team = storedTeam();
  if (team && team !== state.activeTeam) followTeam(team);
});

async function configure() {
  const generation = clearRun();
  state.ready = false;
  controls();
  try {
    const config = await api("/v1/demo/config");
    if (!guard(generation)) return;
    state.config = config;
    el("session-controls").hidden = !config.demo_accounts;
    const claude = el("engine").querySelector('[value="claude"]');
    claude.disabled = !config.claude_available;
    claude.textContent = config.claude_available ? "Claude · API 연결" : "Claude · 연결 필요";
    if (!config.claude_available) el("engine").value = "scripted";
    mode();
    if (config.demo_accounts) {
      await changeTeam(storedTeam() || el("team").value);
    } else {
      notice("이 환경에서는 브라우저 데모 계정을 사용할 수 없습니다. 서버에서 FIVEWHYS_DEMO_ACCOUNTS=1을 설정하면 조사를 실행할 수 있습니다.");
      controls();
    }
  } catch (error) {
    if (guard(generation)) notice("실행 환경을 읽을 수 없습니다. " + error.message, "config");
  }
}

function jobPath(suffix = "") {
  return "/v1/jobs/" + encodeURIComponent(state.job.id) + suffix;
}

async function run(event) {
  event.preventDefault();
  if (el("run").disabled) return;
  const question = el("question").value.trim();
  const generation = clearRun();
  state.busy = true;
  const engine = el("engine").value;
  state.runEngine = engine;
  controls();
  el("status").textContent = "작업 요청 중";
  try {
    const job = await api("/v1/analyses", {method: "POST", body: {question, engine, callback: true}});
    if (!guard(generation)) return;
    state.job = job;
    renderJob();
    await poll(generation);
  } catch (error) {
    if (!guard(generation)) return;
    notice("작업을 요청할 수 없습니다. " + error.message);
    state.busy = false;
    renderJob();
  }
}

function schedulePoll(generation, delay) {
  clearTimeout(state.timer);
  state.timer = setTimeout(() => poll(generation), delay);
}

async function poll(generation) {
  if (!guard(generation) || !state.job) return;
  const sequence = ++state.pollSequence;
  const current = () => guard(generation) && state.pollSequence === sequence;
  try {
    const job = await api(jobPath());
    if (!current()) return;
    state.job = job;
    if (job.status === "completed" && !state.result) {
      const result = await api(jobPath("/result"));
      if (!current()) return;
      state.result = result;
      renderResult();
    }
    state.busy = !["completed", "failed"].includes(job.status);
    state.connectionFailures = 0;
    notice(job.status === "failed" ? "분석에 실패했습니다. " + (job.error || "작업 세부 정보를 확인하세요.") : "");
    renderJob();
    const callbackPending = ["pending", "retrying"].includes(job.callback_status);
    if (state.busy || callbackPending) schedulePoll(generation, state.busy ? 750 : 1200);
  } catch (error) {
    if (!current()) return;
    state.connectionFailures++;
    renderJob();
    notice("작업 상태를 읽을 수 없습니다. " + error.message, "poll");
    if (state.connectionFailures < 4) schedulePoll(generation, state.connectionFailures * 1500);
  }
}

async function retryCallback() {
  if (el("retry").disabled) return;
  const generation = state.generation;
  state.retryPending = true;
  state.beforeRetry = state.job.llm_calls;
  clearTimeout(state.timer);
  controls();
  try {
    await api(jobPath("/callback/retry"), {method: "POST"});
    if (guard(generation)) {
      el("callback-details").open = true;
      await poll(generation);
    }
  } catch (error) {
    if (guard(generation)) {
      notice("콜백을 재전송할 수 없습니다. " + error.message, "poll");
      schedulePoll(generation, 1600);
    }
  } finally {
    if (guard(generation)) {
      state.retryPending = false;
      controls();
    }
  }
}

function renderJob() {
  const job = state.job;
  const status = job?.status;
  el("status").textContent = states[status] || "실행 전";
  el("status").dataset.state = status || "";
  el("engine-tag").textContent = (state.runEngine || el("engine").value) === "claude" ? "Claude" : "Scripted";
  el("findings-jump").hidden = !state.result;
  el("llm-count").textContent = job?.llm_calls ?? "—";
  el("hyp-count").textContent = state.result?.hypotheses?.length ?? "—";
  el("ev-count").textContent = state.result?.evidence?.length ?? "—";
  const info = el("job-info");
  if (!job) {
    info.replaceChildren(node("p", "sub", "실행 후 작업 ID와 처리 상태를 확인할 수 있습니다."));
  } else {
    const meta = values([
      ["작업 ID", job.id], ["팀", job.team], ["처리 상태", job.status],
      ["접수 (KST)", clock(job.created_at)], ["시작 (KST)", clock(job.started_at)],
      ["종료 (KST)", clock(job.finished_at)], ["엔진 턴", state.result?.engine_turns],
      ["오류", job.error]
    ]);
    // Update the metadata without closing a disclosure the reader has already opened.
    const json = info.querySelector("details");
    const raw = node("pre", "", JSON.stringify({job, result: state.result}, null, 2));
    const expander = details("API 응답 JSON", raw);
    expander.open = json?.open || false;
    info.replaceChildren(meta, expander);
  }
  const callback = job?.callback_status;
  el("callback-status").textContent = deliveries[callback] || "전송 전";
  el("callback-status").dataset.state = callback || "";
  const attempts = job?.callbacks || [];
  el("callback-count").textContent = attempts.length;
  const history = el("callback-history");
  history.replaceChildren();
  if (attempts.length) {
    const list = node("ol", "attempt-list");
    for (const item of attempts) {
      const code = item.status_code == null ? "응답 없음" : "HTTP " + item.status_code;
      const time = clock(item.created_at);
      list.append(node("li", "", [code, time && time + " KST", item.error !== code && item.error].filter(Boolean).join(" · ")));
    }
    history.append(list);
  } else {
    history.append(node("p", "sub", "전송 기록이 아직 없습니다."));
  }
  if (state.beforeRetry !== null && state.beforeRetry !== undefined) {
    history.append(node("p", "note", "LLM 호출 · 재전송 전 " + state.beforeRetry +
      "회 / 현재 " + (job?.llm_calls ?? "—") + "회"));
  }
  controls();
}

function evidenceTitle(evidence) {
  return evidence?.title || evidence?.source || evidence?.id || "근거";
}

function evidenceButton(evidence, className = "source-link", label) {
  const button = node("button", className, label || evidenceTitle(evidence));
  button.type = "button";
  button.dataset.evidence = evidence.id;
  button.addEventListener("click", () => openEvidence(evidence.id));
  return button;
}

function renderHypotheses(result, lookup) {
  const target = el("hypotheses");
  target.replaceChildren();
  if (!result?.hypotheses?.length) {
    target.append(node("p", "empty", result ? "이 조사에서 제시한 원인 가설이 없습니다." :
      "질문을 실행하면 원인 가설과 인용 근거를 볼 수 있습니다."));
    return;
  }
  for (const hypothesis of result.hypotheses) {
    const article = node("article", "hypothesis");
    article.append(node("h3", "", hypothesis.title), node("p", "", hypothesis.description));
    const citations = node("div", "citations");
    for (const id of hypothesis.evidence_ids || []) {
      const evidence = lookup.get(id);
      if (evidence) citations.append(evidenceButton(evidence, "citation",
        (kinds[evidence.kind] || "근거") + " · " + evidenceTitle(evidence)));
    }
    if (citations.childElementCount) article.append(citations);
    if (hypothesis.paths?.length) {
      const paths = node("div");
      for (const path of hypothesis.paths) paths.append(graphPath(path, lookup));
      article.append(details("인용 근거의 그래프 경로 · " + hypothesis.paths.length + "건", paths));
    }
    target.append(article);
  }
}

function renderTimeline(result) {
  const target = el("timeline");
  const rows = (result?.evidence || []).filter(item =>
    item.observed_at && ["log", "deployment"].includes(item.kind));
  rows.sort((a, b) => Date.parse(a.observed_at) - Date.parse(b.observed_at));
  if (!rows.length) {
    target.replaceChildren(node("p", "empty small", result ?
      "조회된 근거 중 시각이 기록된 배포 또는 로그가 없습니다." : "확인한 사건이 아직 없습니다."));
    return;
  }
  const list = node("ol", "timeline");
  for (const item of rows) {
    const row = node("li");
    const start = clock(item.observed_at);
    const end = clock(item.end_at);
    const time = node("time", "", start + (end && end !== start ? "\n– " + end : ""));
    time.dateTime = item.observed_at;
    const event = node("div", "event");
    event.append(evidenceButton(item), node("div", "event-meta",
      [kinds[item.kind], item.service, "KST"].filter(Boolean).join(" · ")));
    row.append(time, event);
    list.append(row);
  }
  target.replaceChildren(list);
}

function ranking(title, entries, other, decisions) {
  const section = node("section", "ranking");
  section.append(node("h3", "", title + " · " + entries.length + "건"));
  const list = node("ol");
  for (const entry of entries) {
    const row = node("li");
    const content = node("div");
    content.append(evidenceButton(entry), node("span", "kind-tag",
      [kinds[entry.kind] || entry.kind, other.has(entry.id) ? "공통 근거" : "이 검색에서만 선택"].join(" · ")));
    const decision = decisions.get(entry.id);
    if (decision) {
      const label = decision.action === "excluded" ? "제외 이유" : "선택 이유";
      content.append(node("p", "rank-reason", label + " · " + decision.reason));
    }
    row.append(content);
    list.append(row);
  }
  section.append(entries.length ? list : node("p", "sub", "선택된 근거가 없습니다."));
  return section;
}

function graphPath(path, lookup) {
  const title = id => lookup.has(id) ? evidenceTitle(lookup.get(id)) : id;
  const content = node("div");
  // Neo4j traversal can follow a relationship in reverse. Preserve its stored direction.
  if (path.edges?.length) {
    const edges = path.incident_edge ? [...path.edges, path.incident_edge] : path.edges;
    for (const edge of edges) content.append(node("p", "path-line",
      title(edge.source) + " —[" + edge.type + "]→ " + title(edge.target)));
  } else {
    const readable = (path.nodes || []).map((id, i) =>
      i === 0 ? title(id) : " —[" + (path.relations?.[i-1] || "관계") + "]— " + title(id)).join("");
    content.append(node("p", "path-line", readable));
  }
  const evidence = lookup.get(path.evidence_id);
  if (evidence) content.append(evidenceButton(evidence, "source-link", "이 경로의 근거 원문"));
  return details(title(path.evidence_id), content, "path-detail");
}

function renderComparison(result, lookup) {
  const target = el("compare-content");
  if (!result) {
    target.replaceChildren(node("p", "empty", "조사 후 두 검색의 근거 순위와 그래프 경로를 비교할 수 있습니다."));
    return;
  }
  const vectors = result.vector || [];
  const graphs = result.graph || [];
  const v = new Set(vectors.map(e => e.id));
  const g = new Set(graphs.map(e => e.id));
  const common = [...v].filter(id => g.has(id)).length;
  const added = g.size - common;
  const removed = v.size - common;
  const sameOrder = vectors.every((e, i) => graphs[i]?.id === e.id);
  const summary = added || removed ? "그래프 검색에서 근거 " + added + "건이 추가되고 " + removed + "건이 제외됐습니다."
    : (sameOrder ? "두 검색이 같은 근거를 같은 순서로 선택했습니다." : "선택된 근거는 같고 순위가 달라졌습니다.");
  const grid = node("div", "compare-grid");
  const decisions = new Map((result.decisions || []).map(item => [item.evidence_id, item]));
  const excluded = new Map([...decisions].filter(([, decision]) => decision.action === "excluded"));
  const retained = new Map([...decisions].filter(([, decision]) => decision.action === "retained"));
  grid.append(ranking("벡터 단독", vectors, g, excluded), ranking("벡터 + 그래프", graphs, v, retained));
  target.replaceChildren(node("p", "compare-summary", summary + " 공통 근거 " + common + "건."), grid);
  const paths = result.paths || [];
  const pathsBlock = node("div", "paths");
  pathsBlock.append(node("h3", "", "그래프 경로 · " + paths.length + "건"));
  if (!paths.length) pathsBlock.append(node("p", "sub", "검색에서 반환한 관계 경로가 없습니다."));
  for (const path of paths) pathsBlock.append(graphPath(path, lookup));
  target.append(pathsBlock);
}

function renderResult() {
  const result = state.result;
  const lookup = new Map([...(result?.vector || []), ...(result?.graph || []),
    ...(result?.evidence || [])].map(item => [item.id, item]));
  renderHypotheses(result, lookup);
  renderTimeline(result);
  renderComparison(result, lookup);
}

async function openEvidence(id) {
  if (!state.job) return;
  const generation = state.generation;
  const sequence = ++state.sourceSequence;
  const dialog = el("source-dialog");
  el("source-heading").textContent = "근거 원문";
  el("source-content").replaceChildren(node("p", "sub", "원문을 불러오는 중입니다."));
  if (!dialog.open) dialog.showModal();
  try {
    const evidence = await api(jobPath("/evidence/" + encodeURIComponent(id)));
    if (!guard(generation) || state.sourceSequence !== sequence || !dialog.open) return;
    el("source-heading").textContent = evidenceTitle(evidence);
    const meta = values([["유형", kinds[evidence.kind] || evidence.kind], ["서비스", evidence.service],
      ["출처", evidence.source], ["원문 위치", evidence.locator]]);
    el("source-content").replaceChildren(meta, node("pre", "source-text", evidence.text),
      details("버전과 근거 식별 정보", values([["버전", evidence.version],
        ["근거 ID", evidence.id], ["시작 시각", evidence.observed_at],
        ["종료 시각", evidence.end_at]])));
  } catch (error) {
    if (guard(generation) && sequence === state.sourceSequence)
      el("source-content").replaceChildren(node("p", "sub", "원문을 열 수 없습니다. " + error.message));
  }
}

el("ask-form").addEventListener("submit", run);
el("question").addEventListener("input", controls);
el("engine").addEventListener("change", mode);
el("team").addEventListener("change", () => changeTeam(el("team").value));
el("retry").addEventListener("click", retryCallback);
el("reconnect").addEventListener("click", () => {
  if (el("reconnect").dataset.action === "poll") {
    clearTimeout(state.timer);
    poll(state.generation);
  } else configure();
});
el("source-close").addEventListener("click", () => el("source-dialog").close());
el("source-dialog").addEventListener("click", event => {
  if (event.target !== el("source-dialog")) return;
  const r = el("source-dialog").getBoundingClientRect();
  if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom)
    el("source-dialog").close();
});

function highlightTab() {
  const active = document.querySelector('.section-nav a[href="' +
    (["#findings","#comparison","#delivery"].includes(location.hash) ? location.hash : "#ask") + '"]');
  document.querySelectorAll(".section-nav a").forEach(link => link.removeAttribute("aria-current"));
  active.setAttribute("aria-current", "location");
}
window.addEventListener("hashchange", highlightTab);
highlightTab();
configure();
