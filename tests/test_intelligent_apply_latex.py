"""Opt-in real compiler smoke test; no live LLM, browser, or production publication."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from applypilot.linkedin import intelligent_apply as ia
from applypilot.linkedin import resume_latex as latex
from applypilot.qwen_mcp import NonEasyConnection, TextCompletionClient

REPOSITORY = Path(__file__).resolve().parents[1]
BULLETS = (
    r"\item Created an internal portal with \textbf{Python} and \textbf{PostgreSQL}, automating reporting workflows and reducing manual data processing by approximately 15\%.",
    r"\item Used Gradient Boosting to forecast costs and compare scenarios, improving prediction accuracy by 27\% and helping teams make data-driven planning decisions.",
)


@unittest.skipUnless(os.environ.get("RUN_LATEX_TESTS") == "1", "Set RUN_LATEX_TESTS=1 for real local CV builds")
class RealLatexTests(unittest.TestCase):
    def test_uk_and_eu_prepare_with_real_templates_and_compiler(self):
        # Retain artifacts for visual inspection. Never touch the real data/ PDF.
        root = Path(tempfile.mkdtemp(prefix="intelligent-apply-smoke-"))
        (root / "pyproject.toml").write_text("[project]\nname='cv-smoke'\n", encoding="utf-8")
        for relative in ("tailored/UK/main.tex", "tailored/EU/mmayer.tex", "tailored/EU/EUCV.zip",
                         "tailored/prompts/path1_prompt.txt", "tailored/prompts/path2_prompt.txt"):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY / relative, target)
        source_hashes = {name: latex.sha256((REPOSITORY / name).read_bytes())
                         for name in ("tailored/UK/main.tex", "tailored/EU/mmayer.tex")}
        published = REPOSITORY / "data/AndrewBodzsarCV.pdf"
        old_published = published.read_bytes() if published.exists() else None
        settings = {
            "intelligent_apply": True, "_project_root": str(root),
            "path1_prompt": "tailored/prompts/path1_prompt.txt", "path2_prompt": "tailored/prompts/path2_prompt.txt",
            "latexresume_uk": "tailored/UK/main.tex", "latexresume_eu": "tailored/EU/mmayer.tex",
            "latexresume_uk_lines": {"from": "222", "to": "224"},
            "latexresume_eu_lines": {"from": "166", "to": "167"},
            "profile": {"first_name": "Andrew", "last_name": "Bodzsar"},
        }
        connection = NonEasyConnection("openrouter", "inclusionai/ling-3.0-flash",
                                       "https://openrouter.ai/api/v1", "unused-smoke-test-key")
        answers = iter([
            "Which employer's description should I use? The CV and target vacancy have different employers.",
            "First replacement:\n```latex\n" + BULLETS[0] + "\n```\nSecond replacement:\n```latex\n"
            + BULLETS[1] + "\n```",
            "```latex\n% Line 166 replacement\n" + BULLETS[0] + "\n\n% Line 167 replacement\n"
            + BULLETS[1] + "\n```",
        ])
        requests = []
        def endpoint(request):
            requests.append(json.loads(request.content))
            self.assertEqual(len(requests[-1]["messages"]), 3)
            return httpx.Response(200, json={"choices": [{"message": {"content": next(answers)},
                                                         "finish_reason": "stop"}]})
        print(f"\nReal CV build artifacts: {root}", flush=True)
        with (httpx.Client(transport=httpx.MockTransport(endpoint)) as http,
              patch.object(ia, "resolve_non_easy_connection", return_value=connection)):
            client = TextCompletionClient(connection, client=http)
            for uk in (True, False):
                with self.subTest(branch="UK" if uk else "EU"):
                    result = ia.prepare_application(
                        {"job_id": f"smoke-{uk}", "company": "SmokeUK" if uk else "SmokeEU", "title": "Engineer",
                         "description": "Python and PostgreSQL engineering with analytical modelling."},
                        settings | {"resume_uk": uk}, project_root=root, config_path="local-smoke",
                        completion_client=client,
                    )
                    try:
                        result.verify_published()
                        self.assertIn("NOMOS", result.text)
                        self.assertEqual(result.pdf_path.read_bytes(), result.published_pdf.read_bytes())
                        response = json.loads((result.folder / f"{result.folder.name}_response.json").read_text())
                        self.assertEqual(response["attempt_count"], 2 if uk else 1)
                        self.assertEqual(response["bullet_lines"], list(BULLETS))
                        self.assertEqual(response["validation"], "passed")
                    finally:
                        result.finish("ready")
                    print(f"{'UK' if uk else 'EU'} verified: {result.pdf_path}", flush=True)
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0]["messages"][:2], requests[1]["messages"][:2])
        self.assertEqual(requests[0]["session_id"], requests[1]["session_id"])
        for relative, expected in source_hashes.items():
            self.assertEqual(latex.sha256((REPOSITORY / relative).read_bytes()), expected)
        self.assertEqual(published.read_bytes() if published.exists() else None, old_published)


if __name__ == "__main__":
    unittest.main()
