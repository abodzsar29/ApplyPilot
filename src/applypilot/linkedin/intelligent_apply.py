"""Shared, opt-in preparation of per-job LinkedIn application résumés."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from applypilot.linkedin import resume_latex as latex
from applypilot.qwen_mcp import (
    NonEasyConnection,
    TextCompletionClient,
    get_configured_model,
    resolve_non_easy_connection,
)

log = logging.getLogger(__name__)
LAYOUT_VERSION = 1
MAX_RESPONSE_ATTEMPTS = 2


def boolean(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{name} must be a JSON boolean or a true/false string")


def intelligent_enabled(settings: dict) -> bool:
    return boolean(settings.get("intelligent_apply", False), "intelligent_apply")


def _line_number(value, name: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d*", str(value).strip()):
        raise ValueError(f"{name} must be a positive one-based integer")
    return int(value)


def _path(root: Path, settings: dict, key: str) -> Path:
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must name a readable file")
    path = (root / value).resolve()
    if not path.is_file():
        raise ValueError(f"{key} is not a file: {path}")
    return path


@dataclass(frozen=True)
class Inputs:
    root: Path
    uk: bool
    prompt_path: Path
    source_path: Path
    first: int
    last: int
    archive: Path | None
    timezone: ZoneInfo | None
    provider_options: dict
    connection: NonEasyConnection

    def snapshot(self):
        prompt = self.prompt_path.read_bytes()
        source = self.source_path.read_bytes()
        if not prompt.decode("utf-8").strip():
            raise ValueError("The selected prompt file is empty")
        targets = latex.locate_targets(source.decode("utf-8"), self.first, self.last)
        return prompt, source, targets


def validate_intelligent_config(settings: dict, *, model: str = "qwen-flash",
                                project_root: Path | None = None) -> Inputs | None:
    """Read-only preflight. No model request, browser launch, or PDF publication."""
    if not intelligent_enabled(settings):
        return None
    root = Path(project_root or settings.get("_project_root") or Path.cwd()).resolve()
    if not (root / "pyproject.toml").is_file():
        raise ValueError("Run intelligent_apply from the ApplyPilot project root containing pyproject.toml")
    uk = boolean(settings.get("resume_uk"), "resume_uk")
    branch = "uk" if uk else "eu"
    lines = settings.get(f"latexresume_{branch}_lines")
    if not isinstance(lines, dict):
        raise ValueError(f"latexresume_{branch}_lines must contain from and to")  # noqa: TRY004 — config validation
    first, last = (_line_number(lines.get(key), f"latexresume_{branch}_lines.{key}") for key in ("from", "to"))
    cache = settings.get("intelligent_apply_cache", {})
    if not isinstance(cache, dict):
        raise ValueError("intelligent_apply_cache must be an object")  # noqa: TRY004 — config validation
    only = cache.get("openrouter_provider_only", [])
    if (not isinstance(only, list) or any(not isinstance(host, str) or not host.strip() for host in only)):
        raise ValueError("intelligent_apply_cache.openrouter_provider_only must be a list of provider slugs")
    connection = resolve_non_easy_connection(model, model_override=get_configured_model(settings, "bulletpoint_model"))
    if only and connection.provider != "openrouter":
        raise ValueError("openrouter_provider_only requires the OpenRouter connection")
    provider_options = {"only": sorted({host.strip() for host in only})} if only else {}
    timezone = ZoneInfo(settings["timezone_id"]) if settings.get("timezone_id") else None
    inputs = Inputs(root, uk, _path(root, settings, "path1_prompt" if uk else "path2_prompt"),
                    _path(root, settings, f"latexresume_{branch}"), first, last,
                    None if uk else root / "tailored/EU/EUCV.zip", timezone, provider_options, connection)
    inputs.snapshot()
    if inputs.archive:
        latex.validate_archive(inputs.archive)
    for tool in ("tectonic", "pdftotext", "pdfinfo"):
        if not shutil.which(tool):
            raise ValueError(f"intelligent_apply requires {tool} on PATH")
    for directory in (root / "JobApplications", root / "data"):
        ancestor = directory
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK):
            raise ValueError(f"Application output directory is not writable: {directory}")
    return inputs


def canonical_hash(value) -> str:
    return latex.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode("utf-8"))


def build_messages(prompt: str, source: str, description: str) -> list[dict]:
    return [{"role": "system", "content": prompt},
            {"role": "user", "content": "Source CV in LaTeX:\n" + source},
            {"role": "user", "content": "Job description:\n" + description}]


def prefix_fingerprint(messages: list[dict], connection, branch: str, policy: dict) -> str:
    return canonical_hash({"branch": branch, "model": connection.model, "api_service": connection.provider,
                           "base_url": connection.base_url, "layout_version": LAYOUT_VERSION,
                           "messages": messages[:2], "policy": policy})


def cache_metrics(usage: dict | None) -> dict:
    usage = usage or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if isinstance(cached, bool) or not isinstance(cached, (int, float)) or cached < 0:
        cached = None
    return {"status": "unknown" if cached is None else "hit" if cached > 0 else "miss",
            "cached_tokens": cached,
            "cache_write_tokens": details.get("cache_write_tokens") if isinstance(details, dict) else None,
            "cost": usage.get("cost")}


def _component(value: str, fallback: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = "".join("-" if unicodedata.category(char).startswith("C") else char for char in value)
    value = re.sub(r'[\s/\\<>:"|?*]+', "-", value).strip(" .-_")[:10].rstrip(" .-_")
    return value or fallback


def create_job_directory(inputs: Inputs, job: dict) -> Path:
    today = datetime.now(inputs.timezone).astimezone(inputs.timezone).strftime("%d%m%Y")
    base = f"{_component(job.get('company'), 'Company')}_{_component(job.get('title'), 'Job')}_{today}"
    parent = inputs.root / "JobApplications"
    parent.mkdir(parents=True, exist_ok=True)
    suffix = _component(job.get("job_id"), "job")
    attempt = 0
    while True:
        name = base if attempt == 0 else f"{base}__{suffix}" + (f"_retry{attempt}" if attempt > 1 else "")
        folder = parent / name
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            attempt += 1


class JobAudit:
    def __init__(self, folder: Path, job_id: str):
        self.path = folder / f"{folder.name}_logs.txt"
        self.job_id = job_id
        self.handle = self.path.open("x", encoding="utf-8")

    def record(self, stage: str, outcome: str, message: str):
        for line in str(message).splitlines() or [""]:
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            self.handle.write(f"{stamp} job={self.job_id} {stage} {outcome} {line}\n")
            if outcome != "OUTPUT":
                log.info("Intelligent Apply job=%s %s %s: %s", self.job_id, stage, outcome, line)
        self.handle.flush()

    def close(self):
        self.handle.close()


class PreparationError(RuntimeError):
    def __init__(self, stage: str, reason: str, *, folder: Path | None = None, fatal: bool = False):
        super().__init__(f"Intelligent Apply {stage}: {reason}" + (f" (artifacts: {folder})" if folder else ""))
        self.stage, self.folder, self.fatal = stage, folder, fatal


@dataclass
class PreparedApplication:
    folder: Path
    pdf_path: Path
    published_pdf: Path
    text: str
    pdf_sha256: str
    audit: JobAudit
    finalized: bool = False

    def verify_published(self):
        if latex.sha256(self.published_pdf.read_bytes()) != self.pdf_sha256:
            raise PreparationError("publish", "The published PDF changed before application handoff",
                                   folder=self.folder, fatal=True)

    def finish(self, result: str):
        if self.finalized:
            return
        try:
            outcome = "SUCCESS" if result in {"applied", "ready"} else "SKIPPED" if result == "skipped" else "FAILED"
            self.audit.record("application", outcome, f"Application outcome: {result}")
        except OSError as exc:
            raise PreparationError("logs", f"Application outcome={result}, but audit write failed",
                                   folder=self.folder, fatal=True) from exc
        finally:
            self.finalized = True
            try:
                self.audit.close()
            except OSError as exc:
                raise PreparationError("logs", f"Application outcome={result}, but audit close failed",
                                       folder=self.folder, fatal=True) from exc


def _save_json(path: Path, value):
    latex.atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _save_response_attempts(path: Path, metadata: dict, attempts: list[dict]):
    """Keep every returned answer and charge; top-level fields describe the latest."""
    costs = [attempt["cache"]["cost"] for attempt in attempts]
    total = sum(costs) if all(
        isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0 for cost in costs
    ) else None
    _save_json(path, {**metadata, **attempts[-1], "attempts": attempts,
                      "attempt_count": len(attempts), "total_reported_cost": total})


def prepare_application(job_details: dict, settings: dict, *, project_root: Path, config_path: str,
                        completion_client: TextCompletionClient, inputs: Inputs | None = None) -> PreparedApplication:
    inputs = inputs or validate_intelligent_config(settings, project_root=project_root)
    folder = None
    audit = None
    stage = "folder"
    started = time.monotonic()
    try:
        folder = create_job_directory(inputs, job_details)
        audit = JobAudit(folder, str(job_details.get("job_id", "unknown")))
        record = audit.record
        branch = "uk" if inputs.uk else "eu"
        record("folder", "SUCCESS", f"Created {folder}")
        record("branch", "SUCCESS", f"resume_uk={settings.get('resume_uk')!r} in {config_path}; "
               f"using prompt path {1 if inputs.uk else 2} / {branch.upper()}")
        stage = "description"
        if not isinstance(job_details.get("description"), str) or not job_details["description"].strip():
            raise ValueError("The current job has no description")
        description_path = folder / f"{folder.name}.json"
        _save_json(description_path, [job_details])
        record(stage, "SUCCESS", f"Saved job description JSON: {description_path}")
        stage = "inputs"
        record("prompt", "START", f"Reading full instructions from {inputs.prompt_path}")
        record("prompt", "START", f"Reading full original LaTeX source from {inputs.source_path}")
        prompt, source, targets = inputs.snapshot()
        record("prompt", "START", f"Reading job description from {description_path}")
        description = json.loads(description_path.read_text(encoding="utf-8"))[0]["description"]
        messages = build_messages(prompt.decode("utf-8"), source.decode("utf-8"), description)
        messages, cache_mode = completion_client.prepare_messages(messages, inputs.provider_options)
        connection = completion_client.connection
        policy = {"provider": inputs.provider_options if connection.provider == "openrouter" else {},
                  "cache_mode": cache_mode}
        fingerprint = prefix_fingerprint(messages, connection, branch, policy)
        session_id = f"resume-{branch}-{fingerprint}" if connection.provider == "openrouter" else None
        record("prompt", "SUCCESS", f"Read {len(prompt)} instruction bytes and {len(source)} source bytes; "
               f"prefix={fingerprint}; session_id={session_id}; policy={policy}")
        response_path = folder / f"{folder.name}_response.json"
        metadata = {"schema_version": 2, "job_id": job_details.get("job_id"), "branch": branch,
                    "configured_model": connection.model, "api_service": connection.provider,
                    "input_files": {"prompt": str(inputs.prompt_path), "source": str(inputs.source_path),
                                    "description": str(description_path)},
                    "prompt_sha256": latex.sha256(prompt), "source_sha256": latex.sha256(source),
                    "prefix_sha256": fingerprint, "layout_version": LAYOUT_VERSION,
                    "session_id": session_id, "cache_policy": policy}
        attempts = []
        request_messages = messages
        retry_feedback = None
        for attempt_number in range(1, MAX_RESPONSE_ATTEMPTS + 1):
            stage = "llm_request"
            record(stage, "START", f"Completion attempt {attempt_number}/{MAX_RESPONSE_ATTEMPTS}")
            request_started = datetime.now().astimezone().isoformat()
            request_clock = time.monotonic()
            reply = completion_client.complete(request_messages, session_id=session_id,
                                               provider_options=inputs.provider_options, record=record)
            request_duration_ms = round((time.monotonic() - request_clock) * 1000)
            stage = "llm_response"
            metrics = cache_metrics(reply.usage)
            attempt = {"attempt": attempt_number,
                       "model": reply.model, "provider": reply.provider, "generation_id": reply.generation_id,
                       "request_started_at": request_started, "responded_at": datetime.now().astimezone().isoformat(),
                       "request_duration_ms": request_duration_ms,
                       "messages_sha256": canonical_hash(request_messages), "retry_feedback": retry_feedback,
                       "raw_response": reply.answer, "usage": reply.usage, "cache": metrics,
                       "cache_discount": reply.cache_discount, "finish_reason": reply.finish_reason,
                       "validation": "pending", "validation_error": None}
            attempts.append(attempt)
            _save_response_attempts(response_path, metadata, attempts)
            record(stage, "SUCCESS", f"Saved attempt {attempt_number} to {response_path}; "
                   f"provider={reply.provider}; model={reply.model}; cache={metrics}; duration_ms={request_duration_ms}")
            try:
                if reply.finish_reason in {"length", "content_filter", "tool_calls"}:
                    raise ValueError(f"Completion did not finish normally: {reply.finish_reason}")
                # Re-read the saved answer, rather than editing from an unsaved buffer.
                bullets = latex.parse_bullets(json.loads(response_path.read_text(encoding="utf-8"))["raw_response"])
            except ValueError as exc:
                attempt.update({"validation": "failed", "validation_error": str(exc)})
                _save_response_attempts(response_path, metadata, attempts)
                if attempt_number == MAX_RESPONSE_ATTEMPTS or reply.finish_reason in {"content_filter", "tool_calls"}:
                    raise
                record(stage, "FAILED", f"Completion attempt {attempt_number}: {exc}")
                # Only protocol feedback varies; editable tailoring instructions
                # and the full original CV remain the identical cached prefix.
                retry_feedback = (
                    f"Output validation failed: {exc}\n"
                    "Generate a corrected response using the supplied instructions, source CV, and job description. "
                    "Return exactly two nonempty LaTeX \\item bullets, each on one physical line. "
                    "Return only those two lines, without commentary, questions, code fences, or comments."
                )
                request_messages = [*messages[:2], {**messages[2],
                                    "content": retry_feedback + "\n\n" + messages[2]["content"]}]
                record(stage, "RETRY", "Requesting one corrected response; retaining the original answer and usage")
                continue
            attempt.update({"bullet_lines": bullets, "validation": "passed"})
            _save_response_attempts(response_path, metadata, attempts)
            break
        record(stage, "SUCCESS", "Validated exactly two single-line LaTeX bullets")
        stage = "template"
        path = latex.stage_template(folder, source, uk=inputs.uk, archive=inputs.archive)
        record(stage, "SUCCESS", f"Staged authoritative source {path}; SHA-256={latex.sha256(source)}")
        stage = "latex_replace"
        patched = latex.replace_bullets(source, targets, bullets, record)
        latex.atomic_write(path, patched)
        if path.read_bytes() != patched:
            raise ValueError("Saved LaTeX does not match the verified replacement")
        stage = "compile"
        pdf = latex.compile_resume(path, root=inputs.root, record=record)
        stage = "pdf_verify"
        profile = settings.get("profile") or {}
        name = profile.get("full_name") or profile.get("name") or " ".join(
            str(profile.get(key) or "") for key in ("first_name", "last_name")
        ).strip() or "Andrew Bodzsar"
        checked = latex.verify_pdf(pdf, bullets, targets.third_bullet, name, root=inputs.root)
        latex.atomic_write(folder / "AndrewBodzsarCV.txt", checked["text"].encode("utf-8"))
        record(stage, "SUCCESS", f"Verified candidate, sections, both generated bullets and third NOMOS bullet; "
               f"pages={checked['pages']}; SHA-256={checked['sha256']}")
        stage = "publish"
        published = inputs.root / "data/AndrewBodzsarCV.pdf"
        published.parent.mkdir(parents=True, exist_ok=True)
        contents = pdf.read_bytes()
        if latex.sha256(contents) != checked["sha256"]:
            raise ValueError("Job PDF changed after verification")
        latex.atomic_write(published, contents)
        if latex.sha256(published.read_bytes()) != checked["sha256"]:
            raise ValueError("Published PDF hash differs from verified job PDF")
        record(stage, "SUCCESS", f"Verified PDF copied to {published}; elapsed={time.monotonic() - started:.2f}s")
        return PreparedApplication(folder, pdf, published, checked["text"], checked["sha256"], audit)
    except BaseException as exc:
        fatal = isinstance(exc, OSError) or bool(getattr(exc, "fatal", False))
        if audit:
            try:
                audit.record(stage, "FAILED", "Interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc))
            except OSError:
                fatal = True
            finally:
                try:
                    audit.close()
                except OSError:
                    fatal = True
        if not isinstance(exc, Exception):
            raise
        raise PreparationError(stage, str(exc), folder=folder, fatal=fatal) from exc


class IntelligentApplySession:
    """Hold the shared-PDF lock until the application runner has fully finished."""

    def __init__(self, settings: dict, *, model: str = "qwen-flash"):
        self.settings = settings
        self.inputs = validate_intelligent_config(settings, model=model)
        self.connection = self.inputs.connection
        self.prepared = []
        self._lock = None
        self.client = None

    def __enter__(self):
        import fcntl

        parent = self.inputs.root / "JobApplications"
        parent.mkdir(parents=True, exist_ok=True)
        self._lock = (parent / ".intelligent_apply.lock").open("a+")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock.close()
            raise ValueError("Another intelligent application run holds this project's résumé lock") from exc
        try:
            self.client = TextCompletionClient(self.connection)
        except BaseException:
            self._lock.close()
            raise
        return self

    def prepare(self, job: dict) -> PreparedApplication:
        result = prepare_application(job, self.settings, project_root=self.inputs.root,
                                     config_path=self.settings.get("_config_path", "config/linkedin_apply.json"),
                                     completion_client=self.client, inputs=self.inputs)
        self.prepared.append(result)
        return result

    def __exit__(self, exc_type, exc, traceback):
        failure = None
        try:
            for result in self.prepared:
                if not result.finalized:
                    try:
                        result.finish("interrupted" if exc_type is KeyboardInterrupt else "failed")
                    except OSError as error:
                        failure = error
                    except PreparationError as error:
                        failure = error
        finally:
            try:
                if self.client:
                    self.client.close()
            finally:
                self._lock.close()
        if failure and exc is None:
            raise failure


def intelligent_session(function):
    """Keep legacy signatures/behavior while protecting both intelligent runners."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        settings = bound.arguments["config_dict"]
        if bound.arguments.get("setup", False) or not intelligent_enabled(settings):
            return function(*args, **kwargs)
        from applypilot.config import load_env

        load_env()
        with IntelligentApplySession(settings, model=bound.arguments.get("model", "qwen-flash")) as session:
            bound.arguments["config_dict"] = {**settings, "_intelligent_preparer": session}
            return function(*bound.args, **bound.kwargs)
    return wrapped
