"""Opt-in, file-backed hostname allowlist for external LinkedIn applications."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def jobboard_filter_enabled(settings: dict) -> bool:
    value = settings.get("jobboard_filter", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError("jobboard_filter must be a JSON boolean or a true/false string")


def _hostname(value: str, *, wildcards: bool = False) -> str:
    """Normalize a DNS hostname, allowing explicit single-label glob patterns."""
    name = value.strip().rstrip(".").lower()
    labels = name.split(".")
    if len(labels) < 2 or not name or len(name) > 253:
        raise ValueError("expected a hostname without a scheme, port, path, or query")
    normalized = []
    for label in labels:
        if not label.isascii():
            label = label.encode("idna").decode("ascii")
        allowed = r"[a-z0-9*](?:[a-z0-9*-]{0,61}[a-z0-9*])?" if wildcards else (
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        )
        if not re.fullmatch(allowed, label) or label.count("*") > int(wildcards):
            raise ValueError("expected DNS hostname labels; only '*' wildcards are supported")
        normalized.append(label)
    return ".".join(normalized)


@dataclass(frozen=True)
class JobboardFilter:
    path: Path
    patterns: tuple[str, ...]
    expressions: tuple[re.Pattern, ...]

    def matches(self, url: str) -> bool:
        """Match only the parsed HTTP(S) hostname, never a path or query string."""
        if not isinstance(url, str) or any(char.isspace() for char in url) or "\\" in url:
            return False
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return False
            if parsed.username is not None or parsed.password is not None:
                return False
            # Reading port validates malformed/out-of-range ports as well.
            _ = parsed.port
            hostname = _hostname(parsed.hostname)
        except (ValueError, UnicodeError):
            return False
        return any(expression.fullmatch(hostname) for expression in self.expressions)


def load_jobboard_filter(settings: dict) -> JobboardFilter | None:
    """Read project-root jobboards.json; a broken enabled filter fails closed."""
    if not jobboard_filter_enabled(settings):
        return None
    cached = settings.get("_jobboard_filter")
    if isinstance(cached, JobboardFilter):
        return cached
    root = Path(settings.get("_project_root") or Path.cwd()).resolve()
    path = root / "jobboards.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read jobboard_filter allowlist {path}: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError(f"{path} must map jobboard names to lists of hostnames")
    patterns = []
    for board, entries in data.items():
        if not board.strip() or not isinstance(entries, list):
            raise ValueError(f"{path}: {board!r} must contain a list of hostname strings")
        for entry in entries:
            if not isinstance(entry, str):
                raise ValueError(f"{path}: {board!r} contains a non-string hostname")  # noqa: TRY004 -- config error
            try:
                normalized = _hostname(entry, wildcards=True)
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"{path}: invalid hostname {entry!r} for {board!r}: {exc}") from exc
            if normalized not in patterns:
                patterns.append(normalized)
    if not patterns:
        raise ValueError(f"{path} must contain at least one allowed hostname")
    expressions = tuple(re.compile(re.escape(pattern).replace(r"\*", "[a-z0-9-]+")) for pattern in patterns)
    return JobboardFilter(path, tuple(patterns), expressions)
