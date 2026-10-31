"""Qwen Agent integration for MCP-driven browser automation."""

from __future__ import annotations

import importlib.util
import os
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx


def get_qwen_api_key() -> str:
    """Return a configured Qwen provider API key, if present."""
    return (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
        or os.environ.get("QWEN_API_KEY")
        or os.environ.get("BAILIAN_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()


def get_qwen_model(default: str = "qwen-flash") -> str:
    """Return the configured OpenRouter/DashScope model name."""
    return (
        os.environ.get("NON_EASY_MODEL")
        or os.environ.get("APPLYPILOT_NON_EASY_MODEL")
        or os.environ.get("OPENROUTER_MODEL")
        or os.environ.get("QWEN_MODEL")
        or os.environ.get("LLM_MODEL")
        or default
    ).strip()


def get_qwen_provider() -> str:
    """Return the configured linkedin-noneasy provider."""
    explicit = (
        os.environ.get("NON_EASY_PROVIDER")
        or os.environ.get("APPLYPILOT_NON_EASY_PROVIDER")
        or ""
    ).strip().lower()
    if explicit:
        return explicit
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY") or os.environ.get("BAILIAN_API_KEY"):
        return "dashscope"
    return "dashscope"


def get_configured_model(settings: dict, key: str) -> str | None:
    """Read an optional per-purpose model; null/blank values inherit defaults."""
    value = settings.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a model name string, null, or empty")  # noqa: TRY004 — config validation
    return value.strip() or None


def get_effective_model_and_provider(cli_model: str, *, model_override: str | None = None) -> tuple[str, str]:
    """Prefer a config model override, otherwise retain existing env/CLI defaults."""
    provider = get_qwen_provider()
    if model_override is not None:
        if not isinstance(model_override, str) or not model_override.strip():
            raise ValueError("A model override must be a non-empty model name string")
        return model_override.strip(), provider
    if provider == "claude":
        model = (
            os.environ.get("NON_EASY_MODEL")
            or os.environ.get("APPLYPILOT_NON_EASY_MODEL")
            or os.environ.get("CLAUDE_MODEL")
            or cli_model
            or "haiku"
        ).strip()
    else:
        model = get_qwen_model(cli_model)
    return model, provider


def openrouter_reasoning_mode(model: str) -> bool:
    """Return whether an OpenRouter model should be called with reasoning enabled."""
    model_lower = model.lower()
    required_prefixes = (
        "openai/gpt-oss-",
    )
    required_substrings = (
        "reasoning",
        "thinking",
    )
    return model_lower.startswith(required_prefixes) or any(part in model_lower for part in required_substrings)


def get_openrouter_base_url() -> str:
    return (os.environ.get("OPENROUTER_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
            or "https://openrouter.ai/api/v1").rstrip("/")


def get_dashscope_compatible_base_url() -> str:
    """Match the browser SDK's native endpoint instead of changing API regions."""
    explicit = os.environ.get("DASHSCOPE_COMPATIBLE_BASE_URL") or os.environ.get("DASHSCOPE_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    native = os.environ.get("DASHSCOPE_HTTP_URL") or os.environ.get("DASHSCOPE_HTTP_BASE_URL")
    if not native:
        try:
            import dashscope
        except ImportError:
            native = "https://dashscope.aliyuncs.com/api/v1"
        else:
            native = dashscope.base_http_api_url
    native = native.strip().rstrip("/")
    if "{workspace_id}" in native:
        from dashscope.common.env import resolve_base_url

        native = resolve_base_url(native, os.environ.get("DASHSCOPE_WORKSPACE_ID"))
    if not re.search(r"/api/v\d+$", native):
        raise ValueError("Set DASHSCOPE_COMPATIBLE_BASE_URL for this custom DashScope endpoint")
    return re.sub(r"/api/(v\d+)$", r"/compatible-mode/\1", native)


@dataclass(frozen=True)
class NonEasyConnection:
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)


def resolve_non_easy_connection(model: str = "qwen-flash", *, model_override: str | None = None) -> NonEasyConnection:
    """Resolve the existing non-Easy-Apply credentials for a text-only API call."""
    model, provider = get_effective_model_and_provider(model, model_override=model_override)
    if provider not in {"openrouter", "dashscope"}:
        raise ValueError("intelligent_apply requires an OpenRouter or DashScope API connection; "
                         "the Claude Code CLI is not a text API endpoint")
    api_key = get_qwen_api_key()
    if not api_key:
        raise ValueError("No non-Easy-Apply API key configured (OPENROUTER_API_KEY or DASHSCOPE_API_KEY)")
    base_url = get_openrouter_base_url() if provider == "openrouter" else get_dashscope_compatible_base_url()
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("The LLM base URL must be an HTTP(S) URL without credentials, query, or fragment")
    if not model:
        raise ValueError("A non-empty non-Easy-Apply model is required")
    return NonEasyConnection(provider, model, base_url, api_key)


class TextCompletionError(RuntimeError):
    def __init__(self, reason: str, *, fatal: bool = False):
        super().__init__(reason)
        self.fatal = fatal


@dataclass
class TextCompletion:
    answer: str
    usage: dict | None
    model: str | None
    provider: str | None
    generation_id: str | None
    finish_reason: str | None
    cache_discount: Any = None


class TextCompletionClient:
    """Independent chat requests: no browser tools, history, or RESULT continuations.

    Keep the provider's complete usage object; callers must not reduce a reply to
    text before recording cache and billing information.
    """

    def __init__(self, connection: NonEasyConnection, *, client=None):
        self.connection = connection
        self._http = client or httpx.Client(timeout=120)
        self._reasoning = openrouter_reasoning_mode(connection.model)

    def prepare_messages(self, messages: list[dict], provider_options: dict) -> tuple[list[dict], str]:
        prepared = deepcopy(messages)
        # Documented OpenRouter explicit cache support. Other models (including
        # Ling) use their implicit cache; never attach unsupported generic hints.
        model = self.connection.model
        alibaba_models = {"deepseek/deepseek-v3.2", "qwen/qwen3-max", "qwen/qwen-plus",
                          "qwen/qwen3.6-plus", "qwen/qwen3-coder-plus", "qwen/qwen3-coder-flash"}
        hosts = provider_options.get("only", [])
        alibaba_only = bool(hosts) and all(host.split("/")[0] == "alibaba" for host in hosts)
        explicit = self.connection.provider == "openrouter" and (
            model.startswith("anthropic/") or (model in alibaba_models and alibaba_only)
        )
        if explicit:
            prepared[1]["content"] = [{"type": "text", "text": prepared[1]["content"],
                                       "cache_control": {"type": "ephemeral"}}]
        return prepared, "explicit-after-cv" if explicit else "implicit-or-unavailable"

    def complete(self, messages: list[dict], *, session_id: str | None = None,
                 provider_options: dict | None = None, record=None) -> TextCompletion:
        connection = self.connection
        payload = {"model": connection.model, "messages": messages, "temperature": 0,
                   "max_tokens": 4096, "stream": False}
        if connection.provider == "openrouter":
            if session_id:
                payload["session_id"] = session_id
            if provider_options:
                payload["provider"] = deepcopy(provider_options)
            payload["reasoning"] = {"enabled": self._reasoning}
        else:
            payload["enable_thinking"] = False
        for attempt in range(3):
            if record:
                record("llm_request", "START", f"API attempt {attempt + 1}/3; model={connection.model}")
            try:
                response = self._http.post(
                    f"{connection.base_url}/chat/completions", json=payload,
                    headers={"Authorization": f"Bearer {connection.api_key}"},
                )
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise TextCompletionError("LLM transport failed after three attempts", fatal=True) from exc
                if record:
                    record("llm_request", "RETRY", "Transient transport failure")
                time.sleep(2 ** attempt)
                continue
            if response.status_code >= 400:
                # Inspect only to classify errors. Never log a response body that
                # could echo the CV, request headers, or a secret.
                body = response.text.lower()
                if (connection.provider == "openrouter" and not self._reasoning and
                        "reasoning is mandatory" in body and attempt < 2):
                    self._reasoning = True
                    payload["reasoning"] = {"enabled": True}
                    if record:
                        record("llm_request", "RETRY", "Endpoint requires reasoning enabled")
                    continue
                credit_low = response.status_code == 402 or any(
                    phrase in body for phrase in ("insufficient credits", "insufficient balance", "credit balance")
                )
                if credit_low:
                    raise TextCompletionError("provider_credit_low", fatal=True)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    try:
                        wait = min(20, max(0, float(response.headers.get("Retry-After", 2 ** attempt))))
                    except ValueError:
                        wait = 2 ** attempt
                    if record:
                        record("llm_request", "RETRY", f"HTTP {response.status_code}; waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise TextCompletionError(f"LLM HTTP {response.status_code}", fatal=True)
            try:
                data = response.json()
                choice = (data.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = "".join(block.get("text", "") for block in content if block.get("type") == "text")
                return TextCompletion(
                    answer=content if isinstance(content, str) else "",
                    usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
                    model=data.get("model"), provider=data.get("provider"), generation_id=data.get("id"),
                    finish_reason=choice.get("finish_reason"), cache_discount=data.get("cache_discount"),
                )
            except (ValueError, TypeError, AttributeError, IndexError) as exc:
                raise TextCompletionError("Invalid LLM response envelope") from exc
        raise TextCompletionError("LLM attempts exhausted", fatal=True)

    def close(self):
        self._http.close()


def qwen_agent_available() -> bool:
    """Return True when qwen-agent is importable."""
    return importlib.util.find_spec("qwen_agent") is not None


def _render_messages(messages: list[dict[str, Any]]) -> str:
    """Render Qwen-Agent message batches into a stable text transcript."""
    content: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            reasoning = msg.get("reasoning_content")
            if reasoning:
                content.append(f"[THINK]\n{reasoning}")
            answer = msg.get("content")
            if answer:
                content.append(f"[ANSWER]\n{answer}")
            function_call = msg.get("function_call")
            if function_call:
                name = function_call.get("name", "")
                arguments = function_call.get("arguments", "")
                content.append(f"[TOOL_CALL] {name}\n{arguments}")
        elif role == "function":
            content.append(f"[TOOL_RESPONSE] {msg.get('name', '')}\n{msg.get('content', '')}")
    return "\n".join(content)


def _normalize_openrouter_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return OpenAI-compatible tool messages with matching call IDs.

    Qwen-Agent 0.0.34 serializes tool responses with an ``id`` field. The
    OpenAI chat-completions schema used by OpenRouter requires
    ``tool_call_id`` instead, and some providers stream an empty call ID.
    """
    normalized = deepcopy(messages)
    pending_call_ids: list[str] = []
    call_number = 0

    for message in normalized:
        if message.get("role") == "assistant":
            for tool_call in message.get("tool_calls") or []:
                call_number += 1
                call_id = str(tool_call.get("id") or "").strip()
                if not call_id:
                    call_id = f"call_applypilot_{call_number}"
                tool_call["id"] = call_id
                pending_call_ids.append(call_id)
            continue

        if message.get("role") != "tool":
            continue

        tool_call_id = message.pop("tool_call_id", "")
        legacy_id = message.pop("id", "")
        supplied_id = str(tool_call_id or legacy_id or "").strip()
        if supplied_id and supplied_id in pending_call_ids:
            call_id = supplied_id
            pending_call_ids.remove(supplied_id)
        elif pending_call_ids:
            call_id = pending_call_ids.pop(0)
        else:
            call_number += 1
            call_id = supplied_id or f"call_applypilot_{call_number}"

        message["tool_call_id"] = call_id
        # These are Qwen-Agent bookkeeping fields, not chat-completions fields.
        message.pop("extra", None)
        message.pop("function_call", None)
        message.pop("reasoning_content", None)

    return normalized


def _openrouter_chat_model(llm_cfg: dict):
    """Build Qwen-Agent's OAI model with its tool-message schema repaired."""
    from qwen_agent.llm.oai import TextChatAtOAI

    class OpenRouterTextChat(TextChatAtOAI):
        @staticmethod
        def _conv_qwen_agent_messages_to_oai(messages):
            converted = TextChatAtOAI._conv_qwen_agent_messages_to_oai(messages)
            return _normalize_openrouter_tool_messages(converted)

    return OpenRouterTextChat(llm_cfg)


def _has_terminal_result(output: str) -> bool:
    """Return True only when output contains a complete terminal result line."""
    statuses = ("APPLIED", "READY", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE")
    for line in output.splitlines():
        line = line.strip().strip('`*"').strip()
        if line == "RESULT:APPLIED (dry run)":
            return True
        if line in {f"RESULT:{status}" for status in statuses}:
            return True
        if line.startswith("RESULT:FAILED:") and line.removeprefix("RESULT:FAILED:").strip():
            return True
    return False


class QwenMCPAgent:
    """Thin wrapper around qwen-agent for MCP-driven browser tasks."""

    def __init__(self, model: str, mcp_config: dict) -> None:
        if not qwen_agent_available():
            raise RuntimeError(
                "qwen-agent is not installed. Install it with: pip install -U \"qwen-agent[mcp]\""
            )

        from qwen_agent.agents import Assistant

        api_key = get_qwen_api_key()
        if not api_key:
            raise RuntimeError(
                "No Qwen API key configured. Set OPENROUTER_API_KEY or DASHSCOPE_API_KEY."
            )

        provider = get_qwen_provider()
        llm_cfg = {"model": model, "api_key": api_key}
        if provider == "openrouter":
            reasoning_enabled = openrouter_reasoning_mode(model)
            llm_cfg.update(
                {
                    "model_type": "oai",
                    "model_server": get_openrouter_base_url(),
                    "generate_cfg": {
                        "use_raw_api": True,
                        "extra_body": {"reasoning": {"enabled": reasoning_enabled}},
                        "temperature": 0,
                    },
                }
            )
        else:
            llm_cfg.update(
                {
                    "model_type": "qwen_dashscope",
                    "generate_cfg": {
                        "use_raw_api": True,
                        "enable_thinking": False,
                        "temperature": 0,
                    },
                }
            )

        assistant_llm = _openrouter_chat_model(llm_cfg) if provider == "openrouter" else llm_cfg
        self._assistant = Assistant(
            llm=assistant_llm,
            function_list=[mcp_config],
            name="ApplyPilot Qwen MCP Agent",
            description="Autonomous browser agent for job applications.",
        )
        self._provider = provider
        self._llm_cfg = llm_cfg
        self._mcp_config = mcp_config

    def _rebuild_with_reasoning(self, enabled: bool) -> None:
        """Recreate the Assistant with the requested reasoning mode."""
        from qwen_agent.agents import Assistant

        llm_cfg = dict(self._llm_cfg)
        generate_cfg = dict(llm_cfg.get("generate_cfg", {}))
        extra_body = dict(generate_cfg.get("extra_body", {}))
        reasoning_cfg = dict(extra_body.get("reasoning", {}))
        reasoning_cfg["enabled"] = enabled
        extra_body["reasoning"] = reasoning_cfg
        generate_cfg["extra_body"] = extra_body
        llm_cfg["generate_cfg"] = generate_cfg
        self._llm_cfg = llm_cfg
        assistant_llm = _openrouter_chat_model(llm_cfg) if self._provider == "openrouter" else llm_cfg
        self._assistant = Assistant(
            llm=assistant_llm,
            function_list=[self._mcp_config],
            name="ApplyPilot Qwen MCP Agent",
            description="Autonomous browser agent for job applications.",
        )

    def _run_once(self, prompt: str, log_handle, *, stop_check=None) -> str:
        """Execute one prompt against the current assistant config."""
        rendered = ""
        last_rendered = ""
        responses = iter(self._assistant.run(messages=[{"role": "user", "content": prompt}]))
        try:
            while True:
                # A browser tool yields before the next model request. Inspect
                # deterministic terminal states here, not in another LLM turn.
                terminal = stop_check() if stop_check is not None else None
                if terminal:
                    log_handle.write(f"\n[INFO] Deterministic page guard stopped the agent.\n{terminal}\n")
                    return terminal
                try:
                    response = next(responses)
                except StopIteration:
                    break
                if not isinstance(response, list):
                    continue
                rendered = _render_messages(response)
                if not rendered:
                    continue
                delta = rendered[len(last_rendered):] if rendered.startswith(last_rendered) else rendered
                if delta:
                    log_handle.write(delta)
                last_rendered = rendered
        finally:
            close = getattr(responses, "close", None)
            if close is not None:
                close()
        if rendered:
            log_handle.write("\n")
        return rendered

    def _run_with_reasoning_fallback(self, prompt: str, log_handle, *, stop_check=None) -> str:
        """Run once, enabling mandatory reasoning when a provider requires it."""
        try:
            return self._run_once(prompt, log_handle, stop_check=stop_check)
        except Exception as exc:
            message = str(exc)
            needs_reasoning = (
                "Reasoning is mandatory for this endpoint and cannot be disabled" in message
            )
            if not needs_reasoning:
                raise
            self._rebuild_with_reasoning(True)
            log_handle.write("[INFO] Retrying with reasoning enabled.\n")
            return self._run_once(prompt, log_handle, stop_check=stop_check)

    def run_prompt(self, prompt: str, log_path=None, *, stop_check=None) -> str:
        """Execute one prompt and return the final rendered transcript."""
        if log_path is None:
            class _NullWriter:
                def write(self, _text: str) -> None:
                    return None

            log_handle = _NullWriter()
            should_close = False
        else:
            log_handle = open(log_path, "w", encoding="utf-8")
            should_close = True

        try:
            outputs: list[str] = []
            attempt_prompt = prompt
            for attempt in range(1, 4):
                if attempt > 1:
                    log_handle.write(
                        f"[INFO] No terminal RESULT line; continuing agent attempt {attempt}/3.\n"
                    )
                output = self._run_with_reasoning_fallback(attempt_prompt, log_handle, stop_check=stop_check)
                outputs.append(output)
                if _has_terminal_result(output):
                    break
                attempt_prompt = f"""{prompt}

== CONTINUATION AFTER AN INCOMPLETE AGENT TURN ==
A previous agent turn stopped without a terminal RESULT line. Inspect the current
browser state before changing anything. If the application is already submitted,
confirm the visible success state and output RESULT:APPLIED. Otherwise continue the
application now by invoking browser tools immediately. Do not narrate a future plan,
ask for permission, close the browser, or stop before one terminal RESULT line.
Continuation attempt: {attempt + 1} of 3.
"""
            return "\n".join(part for part in outputs if part)
        finally:
            if should_close:
                log_handle.close()
