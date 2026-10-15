"""Qwen Agent integration for MCP-driven browser automation."""

from __future__ import annotations

import importlib.util
import os
from copy import deepcopy
from typing import Any


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


def get_effective_model_and_provider(cli_model: str) -> tuple[str, str]:
    """Resolve the actual provider and model after env overrides."""
    provider = get_qwen_provider()
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
                    "model_server": (
                        os.environ.get("OPENROUTER_BASE_URL")
                        or os.environ.get("OPENAI_BASE_URL")
                        or "https://openrouter.ai/api/v1"
                    ).rstrip("/"),
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

    def _run_once(self, prompt: str, log_handle) -> str:
        """Execute one prompt against the current assistant config."""
        rendered = ""
        last_rendered = ""
        for response in self._assistant.run(messages=[{"role": "user", "content": prompt}]):
            if not isinstance(response, list):
                continue
            rendered = _render_messages(response)
            if not rendered:
                continue
            delta = rendered[len(last_rendered):] if rendered.startswith(last_rendered) else rendered
            if delta:
                log_handle.write(delta)
            last_rendered = rendered
        if rendered:
            log_handle.write("\n")
        return rendered

    def _run_with_reasoning_fallback(self, prompt: str, log_handle) -> str:
        """Run once, enabling mandatory reasoning when a provider requires it."""
        try:
            return self._run_once(prompt, log_handle)
        except Exception as exc:
            message = str(exc)
            needs_reasoning = (
                "Reasoning is mandatory for this endpoint and cannot be disabled" in message
            )
            if not needs_reasoning:
                raise
            self._rebuild_with_reasoning(True)
            log_handle.write("[INFO] Retrying with reasoning enabled.\n")
            return self._run_once(prompt, log_handle)

    def run_prompt(self, prompt: str, log_path=None) -> str:
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
                output = self._run_with_reasoning_fallback(attempt_prompt, log_handle)
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
