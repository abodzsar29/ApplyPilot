"""Local preparation tests. Model requests and PDF tools are replaced, not paid."""

import json
import os
import re
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from applypilot.linkedin import intelligent_apply as ia
from applypilot.linkedin import resume_latex as latex
from applypilot.qwen_mcp import (
    NonEasyConnection,
    TextCompletionClient,
    TextCompletionError,
    get_dashscope_compatible_base_url,
)

BULLETS = (r"\item Built a \textbf{Python} reporting service, reducing manual work by 15\%.",
           r"\item Developed \textbf{PostgreSQL} cost forecasts with 27\% lower prediction error.")
ANSWER = "The requested bullets:\n```latex\n" + "\n".join(BULLETS) + "\n```\nOnly those lines should change."


def cv_source(uk):
    first = 222 if uk else 166
    lines = ["\\documentclass{article}\n"]
    if uk:
        lines.append("\\newenvironment{highlights}{\\begin{itemize}}{\\end{itemize}}\n")
    lines.extend(["\\begin{document}\n", "Andrew Bodzsar\n", "\\section{Experience}\n", "NOMOS Glashütte\n"])
    lines.extend(["% padding\n"] * (first - 2 - len(lines)))
    environment = "highlights" if uk else "itemize"
    lines.extend([f"\\begin{{{environment}}}\n", "    \\item Created an internal portal with original tools\n"])
    if uk:
        lines.append("    % Tools comment must remain at line 223\n")
    lines.extend(["    \\item Used Gradient Boosting for original estimates\n",
                  "    \\item Contributed to a pipeline with unchanged content\n",
                  f"\\end{{{environment}}}\n", "\\section{Education}\n", "University\n", "\\end{document}\n"])
    return "".join(lines).encode("utf-8")


class IntelligentApplyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"NON_EASY_PROVIDER": "openrouter",
                                                       "OPENROUTER_API_KEY": "secret-test-key",
                                                       "NON_EASY_MODEL": "inclusionai/ling-3.0-flash"}, clear=True))
        self.stack.enter_context(patch.object(ia.shutil, "which", side_effect=lambda name: "/tools/" + name))
        (self.root / "pyproject.toml").write_text("[project]\nname='fixture'\n")
        for branch in ("UK", "EU", "prompts"):
            (self.root / "tailored" / branch).mkdir(parents=True, exist_ok=True)
        (self.root / "tailored/UK/main.tex").write_bytes(cv_source(True))
        (self.root / "tailored/EU/mmayer.tex").write_bytes(cv_source(False))
        (self.root / "tailored/prompts/path1_prompt.txt").write_text("UK full instructions\n\nPreserve this last line.\n")
        (self.root / "tailored/prompts/path2_prompt.txt").write_text("EU full instructions\n")
        with zipfile.ZipFile(self.root / "tailored/EU/EUCV.zip", "w") as archive:
            archive.writestr("mmayer.tex", "old archive source with wrong line numbers")
            archive.writestr("altacv.cls", "class fixture")
            archive.writestr("portrait.png", b"photo fixture")
            archive.writestr("pubs-num.tex", "unused helper")
        self.settings = {"intelligent_apply": "true", "resume_uk": "true", "_project_root": str(self.root),
                         "path1_prompt": "tailored/prompts/path1_prompt.txt", "path2_prompt": "tailored/prompts/path2_prompt.txt",
                         "latexresume_uk": "tailored/UK/main.tex", "latexresume_eu": "tailored/EU/mmayer.tex",
                         "latexresume_uk_lines": {"from": "222", "to": "224"},
                         "latexresume_eu_lines": {"from": "166", "to": "167"},
                         "resume_path": "data/AndrewBodzsarCV.pdf", "profile": {"first_name": "Andrew", "last_name": "Bodzsar"},
                         "timezone_id": "Europe/London",
                         "intelligent_apply_cache": {"openrouter_provider_only": ["novita", "deepinfra/bf16"]}}
        self.job = {"job_id": "4468555976", "company": "SAP Fioneer", "title": "Graduate AI Engineer",
                    "url": "https://www.linkedin.com/jobs/view/4468555976/", "description": "Requirements\nPython\nPostgreSQL"}
        self.sent = []
        self.reply_answer = ANSWER
        self.replies = []
        self.usage = {"prompt_tokens": 5000, "completion_tokens": 100, "total_tokens": 5100,
                      "prompt_tokens_details": {"cached_tokens": 4500}, "cost": 0.0002, "extra_usage": "preserved"}

        def endpoint(request):
            self.sent.append(json.loads(request.content))
            reply = self.replies.pop(0) if self.replies else {}
            return httpx.Response(200, json={"id": reply.get("id", "gen-fixture"),
                                             "provider": "DeepInfra", "model": "resolved-model",
                                             "choices": [{"message": {"content": reply.get("answer", self.reply_answer)},
                                                          "finish_reason": reply.get("finish_reason", "stop")}],
                                             "usage": reply.get("usage", self.usage)})
        self.http = httpx.Client(transport=httpx.MockTransport(endpoint))
        self.addCleanup(self.http.close)
        self.client = TextCompletionClient(ia.resolve_non_easy_connection(), client=self.http)

    def prepare(self, *, settings=None, job=None):
        def compile_fixture(source, **kwargs):
            pdf = source.parent / "AndrewBodzsarCV.pdf"
            pdf.write_bytes(b"%PDF-1.7\nverified test fixture " + source.read_bytes())
            return pdf

        def verify_fixture(pdf, *args, **kwargs):
            return {"pages": 2, "text": "Andrew Bodzsar Experience Education NOMOS tailored text",
                    "sha256": latex.sha256(pdf.read_bytes())}

        with patch.object(latex, "compile_resume", side_effect=compile_fixture), \
                patch.object(latex, "verify_pdf", side_effect=verify_fixture):
            result = ia.prepare_application(job or self.job, settings or self.settings, project_root=self.root,
                                            config_path="config/linkedin_apply.json", completion_client=self.client)
        self.addCleanup(lambda: result.finish("failed") if not result.finalized else None)
        return result

    def test_flags_active_branch_and_missing_output(self):
        for value in (True, "true", " TRUE "):
            self.assertTrue(ia.intelligent_enabled({"intelligent_apply": value}))
        for value in (False, "false", " FALSE "):
            self.assertFalse(ia.intelligent_enabled({"intelligent_apply": value}))
        for value in (1, None, "yes", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ia.intelligent_enabled({"intelligent_apply": value})
        settings = self.settings | {"latexresume_eu": "missing", "path2_prompt": "missing"}
        inputs = ia.validate_intelligent_config(settings)
        self.assertEqual(inputs.snapshot()[2].indices, (221, 223))
        self.assertFalse((self.root / "data/AndrewBodzsarCV.pdf").exists())
        self.assertIsNone(ia.validate_intelligent_config({"intelligent_apply": False}))

    def test_invalid_preflight_inputs_and_host_options(self):
        for override in ({"resume_uk": None}, {"latexresume_uk_lines": {"from": "224", "to": "222"}},
                         {"latexresume_uk_lines": {"from": True, "to": "224"}},
                         {"path1_prompt": "missing"}, {"intelligent_apply_cache": []},
                         {"intelligent_apply_cache": {"openrouter_provider_only": "novita"}}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                ia.validate_intelligent_config(self.settings | override)
        with patch.dict(os.environ, {"NON_EASY_PROVIDER": "claude"}), self.assertRaisesRegex(ValueError, "Claude Code"):
            ia.validate_intelligent_config(self.settings)
        with patch.dict(os.environ, {"NON_EASY_PROVIDER": "dashscope"}), self.assertRaisesRegex(ValueError, "OpenRouter"):
            ia.validate_intelligent_config(self.settings)

    def test_bulletpoint_model_is_independent_and_model_edits_change_cache_prefix(self):
        settings = self.settings | {"browser_model": "fixture/browser-agent", "bulletpoint_model": "fixture/bullets"}
        inputs = ia.validate_intelligent_config(settings, model="cli-fallback")
        self.assertEqual(inputs.connection.model, "fixture/bullets")
        self.client = TextCompletionClient(inputs.connection, client=self.http)
        a = self.prepare(settings=settings)
        a.finish("ready")
        b = self.prepare(settings=settings | {"browser_model": "fixture/other-browser"})
        b.finish("ready")
        changed = settings | {"bulletpoint_model": "fixture/other-bullets"}
        self.client = TextCompletionClient(ia.validate_intelligent_config(changed).connection, client=self.http)
        c = self.prepare(settings=changed)
        c.finish("ready")
        self.assertEqual([request["model"] for request in self.sent],
                         ["fixture/bullets", "fixture/bullets", "fixture/other-bullets"])
        self.assertEqual(self.sent[0]["session_id"], self.sent[1]["session_id"])
        self.assertNotEqual(self.sent[0]["session_id"], self.sent[2]["session_id"])
        response = json.loads((c.folder / f"{c.folder.name}_response.json").read_text())
        self.assertEqual(response["configured_model"], "fixture/other-bullets")

    def test_session_client_uses_bulletpoint_override_and_blank_values_inherit_defaults(self):
        settings = self.settings | {"browser_model": "fixture/browser", "bulletpoint_model": "fixture/bullets"}
        with (patch.object(ia, "TextCompletionClient") as client_factory,
              ia.IntelligentApplySession(settings, model="cli-fallback") as session):
            self.assertEqual(session.connection.model, "fixture/bullets")
            self.assertEqual(client_factory.call_args.args[0].model, "fixture/bullets")
        for value in (None, "", " "):
            inputs = ia.validate_intelligent_config(settings | {"bulletpoint_model": value})
            self.assertEqual(inputs.connection.model, "inclusionai/ling-3.0-flash")
        with self.assertRaisesRegex(ValueError, "bulletpoint_model"):
            ia.validate_intelligent_config(settings | {"bulletpoint_model": True})

    def test_full_uk_pipeline_exact_line_changes_and_usage(self):
        original = (self.root / "tailored/UK/main.tex").read_bytes()
        result = self.prepare()
        result.verify_published()
        self.assertTrue(result.folder.name.startswith("SAP-Fionee_Graduate-A_"))
        actual = (result.folder / "main.tex").read_bytes().splitlines(keepends=True)
        before = original.splitlines(keepends=True)
        self.assertEqual([i + 1 for i, (a, b) in enumerate(zip(actual, before)) if a != b], [222, 224])
        self.assertEqual(actual[222], before[222])
        self.assertEqual(len(actual), len(before))
        self.assertEqual((self.root / "tailored/UK/main.tex").read_bytes(), original)
        self.assertEqual(result.pdf_path.read_bytes(), result.published_pdf.read_bytes())
        response = json.loads((result.folder / f"{result.folder.name}_response.json").read_text())
        self.assertEqual(response["raw_response"], ANSWER)
        self.assertEqual(response["usage"], self.usage)
        self.assertEqual(response["cache"]["status"], "hit")
        self.assertIsNone(response["cache"]["cache_write_tokens"])
        self.assertEqual(response["provider"], "DeepInfra")
        self.assertEqual(response["generation_id"], "gen-fixture")
        self.assertEqual(response["attempt_count"], 1)
        self.assertEqual(response["total_reported_cost"], self.usage["cost"])
        self.assertEqual(len(self.sent), 1)
        request = self.sent[0]
        self.assertEqual(request["messages"][0]["content"], (self.root / self.settings["path1_prompt"]).read_text())
        self.assertEqual(request["messages"][1]["content"], "Source CV in LaTeX:\n" + original.decode())
        self.assertEqual(request["messages"][2]["content"], "Job description:\n" + self.job["description"])
        self.assertEqual(request["provider"], {"only": ["deepinfra/bf16", "novita"]})
        result.finish("ready")
        audit = result.audit.path.read_text()
        self.assertIn("Application outcome: ready", audit)
        self.assertNotIn("secret-test-key", audit + json.dumps(response))
        self.assertTrue(all(re.match(r"\d{4}-\d\d-\d\dT", line) for line in audit.splitlines()))

    def test_eu_uses_configured_source_over_stale_archive_and_only_one_tex(self):
        source = (self.root / "tailored/EU/mmayer.tex").read_bytes()
        archive = (self.root / "tailored/EU/EUCV.zip").read_bytes()
        result = self.prepare(settings=self.settings | {"resume_uk": False})
        self.assertEqual([p.name for p in result.folder.rglob("*.tex")], ["mmayer.tex"])
        for name in ("portrait.png", "altacv.cls", f"{result.folder.name}.zip"):
            self.assertTrue((result.folder / name).is_file())
        self.assertEqual((self.root / "tailored/EU/EUCV.zip").read_bytes(), archive)
        actual = (result.folder / "mmayer.tex").read_bytes().splitlines(keepends=True)
        self.assertEqual([i + 1 for i, (a, b) in enumerate(zip(actual, source.splitlines(keepends=True))) if a != b], [166, 167])
        self.assertTrue(self.sent[0]["session_id"].startswith("resume-eu-"))

    def test_different_jobs_reuse_prefix_but_file_edits_invalidate_it(self):
        a = self.prepare()
        a.finish("applied")
        b = self.prepare(job=self.job | {"job_id": "2", "company": "Other", "description": "Different job"})
        b.finish("ready")
        self.assertEqual(self.sent[0]["session_id"], self.sent[1]["session_id"])
        self.assertEqual(self.sent[0]["messages"][:2], self.sent[1]["messages"][:2])
        path = self.root / self.settings["path1_prompt"]
        path.write_text(path.read_text() + "Changed instructions.\n")
        c = self.prepare()
        c.finish("ready")
        self.assertNotEqual(self.sent[0]["session_id"], self.sent[2]["session_id"])
        self.assertNotEqual(a.folder, c.folder)

    def test_prefix_changes_with_branch_model_layout_endpoint_and_policy(self):
        messages = ia.build_messages("instructions", "source", "description")
        connection = self.client.connection
        initial = ia.prefix_fingerprint(messages, connection, "uk", {})
        for branch, model, url, policy in (("eu", connection.model, connection.base_url, {}),
                                           ("uk", "other-model", connection.base_url, {}),
                                           ("uk", connection.model, "https://other.test/v1", {}),
                                           ("uk", connection.model, connection.base_url, {"only": ["novita"]})):
            other = NonEasyConnection("openrouter", model, url, "not-part-of-hash")
            self.assertNotEqual(initial, ia.prefix_fingerprint(messages, other, branch, policy))
        messages[2]["content"] = "Changed job"
        self.assertEqual(initial, ia.prefix_fingerprint(messages, connection, "uk", {}))
        with patch.object(ia, "LAYOUT_VERSION", 2):
            self.assertNotEqual(initial, ia.prefix_fingerprint(messages, connection, "uk", {}))

    def test_invalid_response_is_saved_before_edit_and_old_pdf_survives(self):
        (self.root / "data").mkdir()
        published = self.root / "data/AndrewBodzsarCV.pdf"
        published.write_bytes(b"previous verified CV")
        self.reply_answer = r"\item Only one bullet"
        with self.assertRaises(ia.PreparationError) as raised:
            self.prepare()
        folder = raised.exception.folder
        response = json.loads((folder / f"{folder.name}_response.json").read_text())
        self.assertEqual(response["raw_response"], self.reply_answer)
        self.assertEqual(response["validation"], "failed")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(response["attempt_count"], 2)
        self.assertTrue(all(attempt["validation"] == "failed" for attempt in response["attempts"]))
        self.assertTrue(all(attempt["validation_error"] for attempt in response["attempts"]))
        self.assertFalse((folder / "main.tex").exists())
        self.assertEqual(published.read_bytes(), b"previous verified CV")
        self.assertIn("llm_response FAILED", (folder / f"{folder.name}_logs.txt").read_text())

    def test_formatting_variations_prepare_without_retry_or_content_changes(self):
        answers = ["Two replacements:\n\n**First:**\n```latex\n" + BULLETS[0]
                   + "\n```\n\n**Second:**\n```latex\n" + BULLETS[1] + "\n```",
                   "Two replacements:\n```latex\n% Line 222 replacement\n" + BULLETS[0]
                   + "\n\n% Line 224 replacement\n" + BULLETS[1] + "\n```"]
        for answer in answers:
            with self.subTest(answer=answer):
                self.reply_answer = answer
                before = len(self.sent)
                result = self.prepare()
                result.finish("ready")
                self.assertEqual(len(self.sent) - before, 1)
                response = json.loads((result.folder / f"{result.folder.name}_response.json").read_text())
                self.assertEqual(response["raw_response"], answer)
                self.assertEqual(response["bullet_lines"], list(BULLETS))
                source = (result.folder / "main.tex").read_text().splitlines()
                self.assertEqual([source[i].strip() for i in (221, 223)], list(BULLETS))

    def test_clarification_response_retries_once_preserving_prefix_and_both_charges(self):
        clarification = "The CV employer and the target vacancy are different. Which company's description should I use?"
        first_usage = {"cost": 0.00012, "prompt_tokens_details": {"cached_tokens": 0}}
        self.replies = [{"answer": clarification, "usage": first_usage, "id": "first"},
                        {"answer": "\n".join(BULLETS), "id": "corrected"}]
        result = self.prepare()
        result.finish("ready")
        self.assertEqual(len(self.sent), 2)
        first, second = self.sent
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["provider"], second["provider"])
        self.assertEqual(first["messages"][:2], second["messages"][:2])
        self.assertEqual(len(second["messages"]), 3)
        self.assertIn("Expected exactly two LaTeX bullets", second["messages"][2]["content"])
        self.assertTrue(second["messages"][2]["content"].endswith(first["messages"][2]["content"]))
        self.assertNotIn(clarification, json.dumps(second))
        response = json.loads((result.folder / f"{result.folder.name}_response.json").read_text())
        self.assertEqual(response["schema_version"], 2)
        self.assertEqual(response["attempt_count"], 2)
        self.assertEqual(response["generation_id"], "corrected")
        self.assertEqual(response["validation"], "passed")
        self.assertIsNone(response["validation_error"])
        attempts = response["attempts"]
        self.assertEqual(attempts[0]["raw_response"], clarification)
        self.assertEqual(attempts[0]["usage"], first_usage)
        self.assertEqual(attempts[0]["validation"], "failed")
        self.assertEqual(attempts[0]["generation_id"], "first")
        self.assertEqual(attempts[1]["usage"], self.usage)
        self.assertEqual(attempts[1]["bullet_lines"], list(BULLETS))
        self.assertAlmostEqual(response["total_reported_cost"], first_usage["cost"] + self.usage["cost"])
        for request, attempt in zip(self.sent, attempts):
            self.assertEqual(attempt["messages_sha256"], ia.canonical_hash(request["messages"]))
        audit = result.audit.path.read_text()
        self.assertIn("llm_response RETRY", audit)
        self.assertIn("Completion attempt 2/2", audit)
        self.assertIn("Application outcome: ready", audit)

    def test_retry_with_missing_cost_retains_unknown_total(self):
        self.replies = [{"answer": "Please clarify the employer", "usage": None}, {"answer": ANSWER}]
        result = self.prepare()
        result.finish("ready")
        response = json.loads((result.folder / f"{result.folder.name}_response.json").read_text())
        self.assertIsNone(response["total_reported_cost"])
        self.assertIsNone(response["attempts"][0]["usage"])
        self.assertEqual(response["attempts"][1]["usage"], self.usage)

    def test_truncated_response_retry_preserves_explicit_cache_boundary(self):
        self.client.connection = NonEasyConnection("openrouter", "anthropic/claude-sonnet-4",
                                                  "https://fixture/v1", "secret-test-key")
        self.replies = [{"answer": BULLETS[0], "finish_reason": "length"}, {"answer": ANSWER}]
        result = self.prepare()
        result.finish("ready")
        first, second = self.sent
        self.assertEqual(first["messages"][:2], second["messages"][:2])
        self.assertEqual(first["session_id"], second["session_id"])
        source_block, = second["messages"][1]["content"]
        self.assertEqual(source_block["cache_control"], {"type": "ephemeral"})
        self.assertEqual(source_block["text"], "Source CV in LaTeX:\n" + cv_source(True).decode())
        response = json.loads((result.folder / f"{result.folder.name}_response.json").read_text())
        self.assertEqual(response["attempts"][0]["validation_error"], "Completion did not finish normally: length")
        self.assertEqual(response["validation"], "passed")

    def test_failed_or_interrupted_retry_preserves_first_response_without_staging(self):
        original_complete = self.client.complete
        for error in (TextCompletionError("provider_credit_low", fatal=True), KeyboardInterrupt()):
            self.reply_answer = "Please clarify the employer"
            before = len(self.sent)
            calls = []
            def complete(*args, _calls=calls, _error=error, **kwargs):
                if _calls:
                    raise _error
                _calls.append(True)
                return original_complete(*args, **kwargs)
            with (self.subTest(error=type(error).__name__),
                  patch.object(self.client, "complete", side_effect=complete),
                  self.assertRaises(KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else ia.PreparationError)
                  as raised):
                self.prepare()
            self.assertEqual(len(self.sent) - before, 1)
            if isinstance(error, TextCompletionError):
                self.assertTrue(raised.exception.fatal)
                self.assertEqual(raised.exception.stage, "llm_request")
        for folder in (self.root / "JobApplications").iterdir():
            response = json.loads((folder / f"{folder.name}_response.json").read_text())
            self.assertEqual(response["raw_response"], self.reply_answer)
            self.assertEqual(response["attempt_count"], 1)
            self.assertEqual(response["validation"], "failed")
            self.assertIn("llm_request FAILED", (folder / f"{folder.name}_logs.txt").read_text())
            self.assertFalse((folder / "main.tex").exists())
        self.assertFalse((self.root / "data/AndrewBodzsarCV.pdf").exists())

    def test_provider_filter_or_tool_response_is_saved_without_format_retry(self):
        for reason in ("content_filter", "tool_calls"):
            self.replies = [{"answer": "", "finish_reason": reason}]
            before = len(self.sent)
            with self.subTest(reason=reason), self.assertRaises(ia.PreparationError) as raised:
                self.prepare()
            self.assertEqual(len(self.sent) - before, 1)
            folder = raised.exception.folder
            response = json.loads((folder / f"{folder.name}_response.json").read_text())
            self.assertEqual(response["finish_reason"], reason)
            self.assertEqual(response["validation"], "failed")
            self.assertEqual(response["attempt_count"], 1)

    def test_cache_miss_and_missing_usage_do_not_fail_preparation(self):
        for usage, status in ((None, "unknown"), ({}, "unknown"),
                              ({"prompt_tokens_details": {"cached_tokens": 0}}, "miss")):
            self.usage = usage
            result = self.prepare()
            result.finish("ready")
            response = json.loads((result.folder / f"{result.folder.name}_response.json").read_text())
            self.assertEqual(response["cache"]["status"], status)
            self.assertIsNone(response["cache"]["cost"])
            self.assertIsNone(response["total_reported_cost"])

    def test_compile_or_verify_failure_never_publishes(self):
        for name in ("compile_resume", "verify_pdf"):
            with self.subTest(name=name), patch.object(latex, name, side_effect=ValueError("failed check")):
                if name == "verify_pdf":
                    with (patch.object(latex, "compile_resume", return_value=self.root / "absent.pdf"),
                          self.assertRaises(ia.PreparationError)):
                        ia.prepare_application(self.job, self.settings, project_root=self.root,
                                               config_path="fixture", completion_client=self.client)
                else:
                    with self.assertRaises(ia.PreparationError):
                        ia.prepare_application(self.job, self.settings, project_root=self.root,
                                               config_path="fixture", completion_client=self.client)
            self.assertFalse((self.root / "data/AndrewBodzsarCV.pdf").exists())

    def test_session_lock_cleanup_and_changed_published_pdf(self):
        with patch.object(ia, "TextCompletionClient", return_value=Mock()):
            with (ia.IntelligentApplySession(self.settings), self.assertRaisesRegex(ValueError, "lock"),
                  ia.IntelligentApplySession(self.settings)):
                self.fail("second run acquired lock")
            with ia.IntelligentApplySession(self.settings):
                pass
        result = self.prepare()
        result.published_pdf.write_bytes(b"changed")
        with self.assertRaises(ia.PreparationError) as caught:
            result.verify_published()
        self.assertTrue(caught.exception.fatal)

    def test_interrupt_retains_artifacts_logs_failure_and_does_not_publish(self):
        with patch.object(latex, "compile_resume", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            ia.prepare_application(self.job, self.settings, project_root=self.root,
                                   config_path="fixture", completion_client=self.client)
        folder, = (self.root / "JobApplications").iterdir()
        self.assertTrue((folder / f"{folder.name}_response.json").exists())
        self.assertIn("compile FAILED Interrupted", (folder / f"{folder.name}_logs.txt").read_text())
        self.assertFalse((self.root / "data/AndrewBodzsarCV.pdf").exists())

    def test_final_audit_failure_preserves_the_actual_submitted_outcome(self):
        result = self.prepare()
        with patch.object(result.audit, "record", side_effect=OSError("disk full")), \
                self.assertRaisesRegex(ia.PreparationError, "outcome=applied") as raised:
            result.finish("applied")
        self.assertTrue(raised.exception.fatal)
        self.assertTrue(result.finalized)
        self.assertTrue(result.audit.handle.closed)

    def test_directory_sanitization_date_and_collisions(self):
        inputs = ia.validate_intelligent_config(self.settings)
        job = self.job | {"company": "../Example/VeryLong", "title": "Test:* Engineer??"}
        a = ia.create_job_directory(inputs, job)
        b = ia.create_job_directory(inputs, job)
        c = ia.create_job_directory(inputs, job)
        self.assertEqual(a.parent, inputs.root / "JobApplications")
        self.assertRegex(a.name, r"^Example-Ve_Test-Engin_\d{8}$")
        self.assertTrue(b.name.startswith(a.name + "__"))
        self.assertTrue(c.name.endswith("_retry2"))


class LatexTests(unittest.TestCase):
    def test_compile_arguments_output_rename_stale_output_and_timeout(self):
        with tempfile.TemporaryDirectory(prefix="cv with spaces ") as directory:
            source = Path(directory) / "main.tex"
            source.write_text("trusted source")
            record = Mock()
            def compile_fixture(command, **kwargs):
                self.assertEqual(command[-1], str(source))
                self.assertEqual(command[1:4], ["-X", "compile", "--keep-logs"])
                self.assertEqual(kwargs["cwd"], source.parent)
                self.assertNotIn("shell", kwargs)
                source.with_suffix(".pdf").write_bytes(b"%PDF-1.7\ncompiled fixture")
                return Mock(returncode=0, stdout="build success", stderr="")
            with patch.object(latex.subprocess, "run", side_effect=compile_fixture):
                pdf = latex.compile_resume(source, root=source.parent, record=record)
            self.assertEqual(pdf.name, "AndrewBodzsarCV.pdf")
            self.assertFalse(source.with_suffix(".pdf").exists())
            source.with_suffix(".pdf").write_bytes(b"stale PDF")
            with self.assertRaisesRegex(ValueError, "stale"):
                latex.compile_resume(source, root=source.parent, record=record)
            source.with_suffix(".pdf").unlink()
            with (patch.object(latex.subprocess, "run", side_effect=subprocess.TimeoutExpired("tectonic", 1, b"last output")),
                  self.assertRaisesRegex(ValueError, "timed out")):
                latex.compile_resume(source, root=source.parent, record=record, timeout=1)
            record.assert_any_call("compile", "OUTPUT", "last output")

    def test_response_validation_and_no_document_commands(self):
        self.assertEqual(latex.parse_bullets(ANSWER), BULLETS)
        self.assertEqual(latex.parse_bullets("\n".join(BULLETS)), BULLETS)
        invalid = ["", BULLETS[0], "\n".join((*BULLETS, r"\item extra")),
                   "\n".join((BULLETS[0], "wrapped continuation", BULLETS[1])),
                   "\n".join((BULLETS[0], BULLETS[1] + r"\input{secret}")),
                   "\n".join((BULLETS[0], r"\item broken \textbf{braces")),
                   "\n".join((BULLETS[0], r"\item invalid 10% symbol")),
                   "```latex\n" + BULLETS[0] + "\nwrapped\n" + BULLETS[1] + "\n```",
                   "```latex\n" + "\n".join(BULLETS),
                   ANSWER + "\n```latex\n\\item Alternative third bullet\n```",
                   ANSWER + "\n\\item Extra bullet outside the code block",
                   "```latex\n" + BULLETS[0] + "\n% slot comment\nwrapped text\n" + BULLETS[1] + "\n```",
                   "```latex\n\\begin{itemize}\n" + "\n".join(BULLETS) + "\n\\end{itemize}\n```",
                   "\n".join((BULLETS[0], "% harmless label", "wrapped continuation", BULLETS[1])),
                   "\n".join((BULLETS[0], BULLETS[1] + " % trailing unescaped comment"))]
        for answer in invalid:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                latex.parse_bullets(answer)

    def test_standalone_comments_and_separate_fences_do_not_change_bullets(self):
        answers = ["% first slot\n" + BULLETS[0] + "\n% second slot\n" + BULLETS[1],
                   "```tex\n% first slot\n" + BULLETS[0] + "\n```\nSecond bullet:\n"
                   + "```latex\n% second slot\n" + BULLETS[1] + "\n```",
                   "```latex\n% heading\n" + BULLETS[0] + "\n\n% \\item commented example\n"
                   + BULLETS[1] + "\n```"]
        for answer in answers:
            with self.subTest(answer=answer):
                self.assertEqual(latex.parse_bullets(answer), BULLETS)
                self.assertEqual(latex.parse_bullets(answer.replace("\n", "\r\n")), BULLETS)

    def test_target_guards_and_crlf_preservation(self):
        source = cv_source(True).decode().replace("\n", "\r\n").encode()
        targets = latex.locate_targets(source.decode(), 222, 224)
        patched = latex.replace_bullets(source, targets, BULLETS, Mock())
        self.assertEqual(patched.count(b"\r\n"), source.count(b"\r\n"))
        self.assertEqual(len(patched.splitlines()), len(source.splitlines()))
        for invalid in (source.decode().replace("NOMOS", "Other employer"),
                        source.decode().replace("Created an internal portal", "Changed original wording"),
                        source.decode().replace("\\begin{highlights}", "\\begin{other}")):
            with self.assertRaises(ValueError):
                latex.locate_targets(invalid, 222, 224)
        with self.assertRaises(ValueError):
            latex.locate_targets(source.decode(), 223, 224)

    def test_unsafe_archive_and_atomic_publish_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            archive = folder / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../outside", "bad")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                latex.validate_archive(archive)
            target = folder / "cv.pdf"
            target.write_bytes(b"old verified")
            with patch.object(latex.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
                latex.atomic_write(target, b"new")
            self.assertEqual(target.read_bytes(), b"old verified")
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["cv.pdf", "unsafe.zip"])

    def test_pdf_content_checks_and_missing_generated_bullet(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "cv.pdf"
            pdf.write_bytes(b"%PDF-1.7\nfixture")
            content = "Andrew Bodzsar NOMOS Experience Education\n" + "\n".join(
                latex.latex_plain_text(item) for item in (*BULLETS, r"\item Third unchanged bullet")
            )
            with patch.object(latex.subprocess, "run", side_effect=[Mock(stdout="Pages: 2\n"), Mock(stdout=content)]):
                checked = latex.verify_pdf(pdf, BULLETS, r"\item Third unchanged bullet", "Andrew Bodzsar", root=pdf.parent)
                self.assertEqual(checked["pages"], 2)
            with (patch.object(latex.subprocess, "run", side_effect=[Mock(stdout="Pages: 2\n"), Mock(stdout=content[:80])]),
                  self.assertRaisesRegex(ValueError, "verification")):
                latex.verify_pdf(pdf, BULLETS, r"\item Third unchanged bullet", "Andrew Bodzsar", root=pdf.parent)


class CompletionTests(unittest.TestCase):
    def test_dashscope_text_endpoint_matches_configured_native_region(self):
        for native in ("https://dashscope.aliyuncs.com/api/v1", "https://dashscope-intl.aliyuncs.com/api/v1"):
            with patch.dict(os.environ, {"DASHSCOPE_HTTP_URL": native}, clear=True):
                self.assertEqual(get_dashscope_compatible_base_url(), native.replace("/api/", "/compatible-mode/"))
        with patch.dict(os.environ, {"DASHSCOPE_COMPATIBLE_BASE_URL": "https://custom.test/v1/"}, clear=True):
            self.assertEqual(get_dashscope_compatible_base_url(), "https://custom.test/v1")
        with (patch.dict(os.environ, {"DASHSCOPE_HTTP_URL": "https://custom.test/native"}, clear=True),
              self.assertRaisesRegex(ValueError, "DASHSCOPE_COMPATIBLE_BASE_URL")):
            get_dashscope_compatible_base_url()

    def test_dashscope_omits_openrouter_options_and_explicit_marker_is_scoped(self):
        sent = []
        def endpoint(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})
        with httpx.Client(transport=httpx.MockTransport(endpoint)) as http:
            client = TextCompletionClient(NonEasyConnection("dashscope", "qwen-flash", "https://fixture/v1", "key"), client=http)
            messages = ia.build_messages("instructions", "source", "job")
            prepared, mode = client.prepare_messages(messages, {"only": ["novita"]})
            client.complete(prepared, session_id="not-for-dashscope", provider_options={"only": ["novita"]})
            self.assertNotIn("session_id", sent[0])
            self.assertNotIn("provider", sent[0])
            self.assertEqual(prepared, messages)
            self.assertEqual(mode, "implicit-or-unavailable")
            client.connection = NonEasyConnection("openrouter", "anthropic/claude-sonnet-4", "https://fixture/v1", "key")
            prepared, mode = client.prepare_messages(messages, {})
            self.assertEqual(prepared[1]["content"][0]["cache_control"], {"type": "ephemeral"})
            self.assertEqual(prepared[2], messages[2])
            self.assertEqual(mode, "explicit-after-cv")

    def test_bounded_retries_reasoning_compatibility_and_sanitized_errors(self):
        replies = [httpx.Response(429, text="try later", headers={"Retry-After": "0"}),
                   httpx.Response(400, text="Reasoning is mandatory for this endpoint"),
                   httpx.Response(200, json={"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                                            "usage": {"prompt_tokens_details": {"cached_tokens": 0}, "cost": 0}})]
        sent = []
        def endpoint(request):
            sent.append(json.loads(request.content))
            return replies.pop(0)
        with httpx.Client(transport=httpx.MockTransport(endpoint)) as http, patch("applypilot.qwen_mcp.time.sleep"):
            client = TextCompletionClient(NonEasyConnection("openrouter", "model", "https://fixture/v1", "secret"), client=http)
            result = client.complete(ia.build_messages("all", "full", "job"), session_id="stable")
            self.assertEqual(result.answer, "answer")
            self.assertTrue(sent[2]["reasoning"]["enabled"])
            self.assertEqual([item["session_id"] for item in sent], ["stable"] * 3)
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(402, text="secret from request"))) as http:
            client = TextCompletionClient(NonEasyConnection("openrouter", "model", "https://fixture/v1", "secret"), client=http)
            with self.assertRaises(TextCompletionError) as error:
                client.complete(ia.build_messages("all", "full", "job"))
            self.assertTrue(error.exception.fatal)
            self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
