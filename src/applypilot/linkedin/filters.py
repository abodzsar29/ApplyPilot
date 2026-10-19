"""Small, shared filters for LinkedIn application candidates."""


def configured_title_exclusion(settings: dict) -> str | list[str]:
    """Read one literal exclusion or a list; empty entries disable matching."""
    value = settings.get("to_ignore")
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item.strip() for item in value if item.strip()]
    raise ValueError("to_ignore must be a string, a list of strings, or null")


def title_is_ignored(title: str, excluded_text: str | list[str]) -> bool:
    """Match any configured literal substring, independent of capitalization."""
    exclusions = [excluded_text] if isinstance(excluded_text, str) else excluded_text
    folded_title = title.casefold()
    return any(text and text.casefold() in folded_title for text in exclusions)


def title_exclusion_label(exclusions: str | list[str]) -> str:
    """Format the validated exclusions for the CLI without JSON/list punctuation."""
    return (", ".join(exclusions) if isinstance(exclusions, list) else exclusions) or "disabled"
