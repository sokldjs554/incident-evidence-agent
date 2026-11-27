"""Recorder fixtures only. No network, credentials, or actual-run artifacts."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import record_claude as rec


def candidate():
    return {"title": "fixture", "description": "fixture", "evidence_ids": ["ev-fixture"]}


def model_records():
    row = {"id": "ev-fixture", "locator": "L1-L1", "version": "test", "source": "fixture", "text": "a" * 1201}
    h = candidate()
    hit = {**row, "excerpt": row["text"][:1200] + "…"}
    del hit["text"]
    result = {"engine": "claude", "engine_turns": 2, "evidence": [row], "hypotheses": [h]}
    # Deliberately invalid provider IDs. A synthetic run cannot pass the provenance gate.
    replies = [{"turn": n, "status": "returned", "model": "claude-fixture", "message_id": "msg_fixture",
                "provider_endpoint": "api.anthropic.com", "usage": {}} for n in (1, 2)]
    pg = {"llm_calls": 2, "trace": {"provider_attempts": replies, "tool_calls": [
        {"name": name, "turn": 1, "status": "returned", "returned": [hit] if name == "search_logs" else []}
        for name in ("search_logs", "search_deployments", "search_reports")],
        "submissions": [{"turn": 1, "status": "rejected", "reason": "unknown_citation", "hypotheses": []},
                        {"turn": 2, "status": "accepted", "hypotheses": [h]}]}}
    return result, pg


def delivery_records():
    result = {"fixture": True}
    job = "00000000-0000-4000-8000-000000000000"
    terminal = {"id": job, "team": "platform", "kind": "analysis", "status": "completed",
                "result": result, "error": None, "error_code": None}
    digest = rec.canonical_digest(terminal)
    rows = []
    for state, codes in (("retrying", [503]), ("delivered", [503, 200])):
        rows.append({**terminal, "runs": 1, "llm_calls": 2, "finished_at": "fixture", "trace": {},
                     "payload": {**rec.PAYLOAD, "scenario": "db_pool_exhaustion", "seed": 42},
                     "callback_status": state, "result_sha256": "f" * 64,
                     "receipt": {"job_id": job, "attempts": len(codes), "digest": digest,
                                 "accepted_at": None if len(codes) == 1 else "fixture"},
                     "callbacks": [{"attempt": i, "status_code": code} for i, code in enumerate(codes, 1)]})
    return job, rows[0], rows[1], copy.deepcopy(rows[0]), copy.deepcopy(rows[1]), result


class RecorderFixtures(unittest.TestCase):
    def test_callback_compares_same_execution_and_receipt(self):
        rec.validate_delivery(*delivery_records())
        for field, changed in (("id", "different"), ("runs", 2), ("llm_calls", 3), ("result_sha256", "a" * 64),
                               ("callbacks", [{"attempt": 1, "status_code": 200}])):
            with self.subTest(field=field):
                data = delivery_records()
                data[2][field] = changed
                with self.assertRaises(rec.CaptureError):
                    rec.validate_delivery(*data)
        bad = delivery_records()
        bad[2]["receipt"]["digest"] = "different"
        with self.assertRaises(rec.CaptureError):
            rec.validate_delivery(*bad)

    def test_fixture_response_and_incomplete_trace_fail(self):
        result, pg = model_records()
        with self.assertRaisesRegex(rec.CaptureError, "공급자"):
            rec.validate_model(pg, result)
        pg["trace"]["omitted_tool_calls"] = 1
        with self.assertRaisesRegex(rec.CaptureError, "생략"):
            rec.validate_model(pg, result)
        pg["trace"]["provider_attempts"][0]["status"] = "refused"
        with self.assertRaisesRegex(rec.CaptureError, "논리 턴"):
            rec.validate_model(pg, result)

    def test_fixture_snippet_limit_and_normalized_submission(self):
        # Isolate the citation check. The fixture SDK identity above must stay invalid.
        result, pg = model_records()
        clean = copy.deepcopy(result)
        pg["trace"]["submissions"][-1]["hypotheses"][0]["title"] = " fixture "
        with patch.object(rec, "validate_provider_response"):
            rec.validate_model(pg, clean)
            pg["trace"]["tool_calls"][0]["returned"][0]["excerpt"] = "incorrect"
            with self.assertRaisesRegex(rec.CaptureError, "원문"):
                rec.validate_model(pg, clean)

    def test_sdk_usage_is_optional_but_provenance_is_required(self):
        # Only exercise the metadata parser in memory. No submission, job or report.
        sdk_projection = {"model": "claude-unit", "message_id": "msg_0000000000",
                          "provider_endpoint": "api.anthropic.com"}
        rec.validate_provider_response(sdk_projection)
        sdk_projection["provider_endpoint"] = "other_or_unknown"
        with self.assertRaisesRegex(rec.CaptureError, "공급자"):
            rec.validate_provider_response(sdk_projection)

    def test_original_text_checked_independently(self):
        rows = [{"id": str(i), "source": "scenario/db_pool_exhaustion/seed-42/" + path, "text": "fixture"}
                for i, path in enumerate(sorted(rec.REPRESENTATIVE))]
        result = {"hypotheses": [{"evidence_ids": ["0"]}], "evidence": copy.deepcopy(rows)}
        rec.validate_sources(rows, result)
        result["evidence"][0]["text"] += " changed"
        with self.assertRaisesRegex(rec.CaptureError, "원문"):
            rec.validate_sources(rows, result)

    def test_first_rejection_and_archive_integrity_are_preserved(self):
        result, pg = model_records()
        pg.update({"id": "fixture", "callback_status": "retrying", "runs": 1, "callbacks": [], "result_sha256": None})
        with tempfile.TemporaryDirectory() as base:
            out = Path(base) / "fixture-only"
            archive = rec.Archive(out)
            rec.report(archive, {"before": pg, "job": "fixture", "result": result}, "fixture-only")
            with self.assertRaises(FileExistsError):
                archive.write("REPORT.md", b"replacement")
            with self.assertRaises(FileExistsError):
                rec.Archive(out)
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "super-secret"}):
                with self.assertRaises(rec.CaptureError):
                    archive.write("secret.json", {"unsafe": "super-secret"})
            archive.finish()
            body = (out / "REPORT.md").read_bytes()
            self.assertLess(body.index(b"rejected"), body.index(b"accepted", body.index(b"rejected")))
            self.assertIn(hashlib.sha256(body).hexdigest(), (out / "SHA256SUMS").read_text())
            self.assertFalse((out / "secret.json").exists())


if __name__ == "__main__":
    unittest.main()
