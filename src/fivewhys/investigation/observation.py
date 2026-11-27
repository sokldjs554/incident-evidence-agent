"""Opt-in, operator-only record of provider responses and tool validation."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError

from fivewhys.agent.five_whys import MixedSubmission
from fivewhys.agent.llm import LLMResponse


# Only these projections may enter the operator record. In particular, opaque
# assistant blocks, provider exceptions, headers, HTTP bodies and environment
# configuration must never be serialized.
_CREDENTIAL = re.compile(
    r"sk-(?:ant-|proj-)?[a-zA-Z0-9_-]{8,}|Bearer\s+[^\s,;]+|"
    r"(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+|"
    r"[a-z][a-z0-9+.-]*://[^/\s@]+:[^/\s@]+@", re.I
)
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_SEARCH_TOOLS = {"search_logs", "search_deployments", "search_reports"}


def _text(value, limit):
    if not isinstance(value, str):
        return None
    for key in ("ANTHROPIC_API_KEY", "FIVEWHYS_ORDERS_TOKEN", "FIVEWHYS_PLATFORM_TOKEN",
                "FIVEWHYS_SESSION_KEY", "NEO4J_PASSWORD"):
        secret = os.environ.get(key)
        if secret:
            value = value.replace(secret, "[redacted]")
    value = _CREDENTIAL.sub("[redacted]", value).replace("\0", "�")
    value = value.encode("utf-8", errors="replace").decode()
    return value if len(value) <= limit else value[:limit] + "…"


def _scalar(value, limit):
    # Keep a malformed scalar at an allowed key so schema failures can be
    # inspected. Arbitrary nested dictionaries may contain unrelated fields.
    if value is None or isinstance(value, (bool, int)):
        return value
    return _text(value, limit)


def _hypotheses(args):
    items = args.get("hypotheses") if isinstance(args, dict) else None
    if not isinstance(items, list):
        return _scalar(items, 2000)
    result = []
    for item in items[:6]:
        if not isinstance(item, dict):
            result.append(_scalar(item, 2000))
            continue
        ids = item.get("evidence_ids")
        result.append({
            "title": _scalar(item.get("title"), 200), "description": _scalar(item.get("description"), 2000),
            "evidence_ids": [_scalar(cid, 128) for cid in ids[:16]] if isinstance(ids, list) else _scalar(ids, 128),
        })
    return result


def _reason(exc):
    if isinstance(exc, MixedSubmission):
        return "mixed_turn"
    if isinstance(exc, ValidationError):
        return "invalid_schema"
    code = getattr(exc, "code", None)
    if code in {"unknown_citation", "missing_search", "missing_current_log", "unmatched_deployment",
                "insufficient_evidence", "retrieval_failure"}:
        return code
    return "tool_error"


@dataclass
class InvestigationObservation:
    """Call save after each transition so a later failure cannot erase a first answer."""

    save: Callable[[dict], None] | None = None
    provider_attempts: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    submissions: list[dict] = field(default_factory=list)
    omitted_tool_calls: int = 0
    omitted_submissions: int = 0

    def as_dict(self):
        return {"provider_attempts": self.provider_attempts, "tool_calls": self.tool_calls,
                "submissions": self.submissions, "omitted_tool_calls": self.omitted_tool_calls,
                "omitted_submissions": self.omitted_submissions}

    def _save(self):
        if self.save:
            self.save(self.as_dict())

    def start(self, turn, attempt, endpoint):
        self.provider_attempts.append({
            "turn": turn, "attempt": attempt, "provider_endpoint": endpoint,
            "started_at": datetime.now(timezone.utc).isoformat(), "status": "started",
        })
        self._save()

    def finish(self, status: str, response: LLMResponse | None = None):
        record = self.provider_attempts[-1]
        record.update(status=status, finished_at=datetime.now(timezone.utc).isoformat())
        if response:
            record.update(model=_text(response.model, 120), message_id=_text(response.message_id, 120),
                          request_id=_text(response.request_id, 120), stop_reason=_text(response.stop_reason, 40),
                          usage={k: v for k, v in response.usage.items() if k in _TOKEN_FIELDS and type(v) is int})
            blocks = (b for b in response.content if getattr(b, "type", "") == "tool_use")
            for index, block in enumerate(blocks):
                name = block.name
                if name == "submit_investigation":
                    if len(self.submissions) < 8:
                        self.submissions.append({
                            "turn": record["turn"], "index": index, "status": "selected",
                            "hypotheses": _hypotheses(block.input),
                        })
                    else:
                        self.omitted_submissions += 1
                if len(self.tool_calls) >= 64:
                    self.omitted_tool_calls += 1
                    continue
                tool = {"turn": record["turn"], "index": index, "name": _text(name, 100), "status": "selected"}
                if name in _SEARCH_TOOLS and isinstance(block.input, dict):
                    tool["query"] = _text(block.input.get("query"), 2000)
                self.tool_calls.append(tool)
        self._save()

    def tool_result(self, turn, index, status, value):
        for item in reversed(self.submissions):
            if (item["turn"], item["index"]) == (turn, index):
                item["status"] = status
                if isinstance(value, Exception):
                    item["reason"] = _reason(value)
                break
        for call in reversed(self.tool_calls):
            if (call["turn"], call["index"]) != (turn, index):
                continue
            call["status"] = status
            if isinstance(value, Exception):
                call["reason"] = _reason(value)
            if status == "returned" and call["name"] in _SEARCH_TOOLS:
                payload = json.loads(value)
                call["returned"] = [
                    {**{key: _text(row.get(key), 512) for key in ("id", "source", "locator", "version", "kind", "service")},
                     "excerpt": _text(row.get("text"), 1200)}
                    for row in payload["evidence"][:12]
                ]
                call["retrieval_decisions"] = [
                    {key: _text(d.get(key), 400) for key in ("evidence_id", "action", "reason")}
                    for d in payload["decisions"][:12]
                ]
            break
        self._save()
