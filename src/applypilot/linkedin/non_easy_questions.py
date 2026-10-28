"""User-requested answer defaults for external application screening questions."""

from __future__ import annotations

import random
import re
import unicodedata
from datetime import date


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(re.sub(r"[^\w\s+]", " ", text).split())


def fallback_direction(question: str) -> str:
    """Classify the question's predicate, not just the presence of 'visa'."""
    text = _normalize(question)
    criminal = any(word in text for word in ("convict", "criminal", "caution", "vorbestraft", "strafregister"))
    if criminal:
        if any(phrase in text for phrase in (
            "no criminal", "no unspent", "no convictions", "never been convicted", "not been convicted",
            "free of", "free from", "don t have", "do not have", "without a criminal",
        )):
            return "positive"
        return "negative"

    visa = any(word in text for word in ("visa", "sponsor", "immigration", "work permit"))
    eligible = any(phrase in text for phrase in (
        "authorized", "authorised", "permission", "permitted", "right to work", "eligible",
        "allowed to work", "can you work", "able to work", "entitled", "work authorization",
    ))
    if visa:
        if eligible or any(phrase in text for phrase in ("without sponsorship", "without visa", "not require", "no need")):
            return "positive"
        return "negative"

    if "conflict of interest" in text or (
        any(word in text for word in ("employ", "work for"))
        and any(word in text for word in ("competitor", "client", "partner"))
    ):
        return "negative"
    if eligible or any(word in text for word in (
        "qualif", "experience", "experienc", "skill", "proficien", "degree", "education", "certif", "licens",
        "knowledge", "kenntnisse", "expertise", "unrestricted", "restrictions", "years of age",
        "older", "available", "willing", "relocat", "onsite", "on site", "vor ort", "consent", "agree",
        "privacy", "berechtig", "erfahrung", "berufserfahrung", "years", "how many",
    )):
        return "positive"
    return "random"


def _option_score(question: str, option: str, direction: str) -> int:
    text = _normalize(option)
    yes = {"yes", "ja", "true", "oui", "si", "sí", "sim"}
    no = {"no", "nein", "nee", "false", "non", "não", "nao"}
    if direction == "negative":
        if text in no or text.startswith(("no ", "nein ", "nee ")):
            return 1000
        if any(phrase in text for phrase in (
            "do not require", "don t require", "no sponsorship", "no visa", "not need", "do not need",
            "no criminal", "no unspent", "no convictions", "never been convicted", "not been convicted",
            "none", "not applicable", "not required", "not needed", "i do not", "i don t",
        )):
            return 900
        if text in yes or text.startswith(("yes ", "ja ")):
            return -1000
        return 0

    if text in yes or text.startswith(("yes ", "ja ")):
        return 1000
    if any(phrase in text for phrase in (
        "without any restrictions", "without restrictions", "unrestricted", "no restrictions",
        "without sponsorship", "without visa", "permanent work", "indefinite", "citizen", "permanent resident",
        "no criminal", "no unspent", "no convictions",
    )):
        return 950
    if text in no or text.startswith(("no ", "nein ", "nee ")):
        return -1000
    if text.startswith(("i agree", "i consent", "i accept")):
        return 950
    for phrase, score in (
        ("native", 900), ("bilingual", 900), ("full professional", 850), ("full working", 850),
        ("professional", 800), ("fluent", 800), ("advanced", 750), ("limited working", 400),
        ("intermediate", 400), ("elementary", 200), ("beginner", 100), ("immediately", 900),
        ("within two weeks", 800), ("within one month", 700), ("within two months", 600),
        ("within three months", 500), ("limited time", 250), ("current employer", 100),
    ):
        if phrase in text:
            return score
    if any(word in text for word in ("none", "no experience", "cannot", "can t", "not authorized", "not authorised")):
        return -900
    question_text = _normalize(question)
    if any(word in question_text for word in ("proficien", "language", "level")):
        cefr = re.fullmatch(r"([abc])([12])", text)
        if cefr:
            return {"a": 100, "b": 300, "c": 500}[cefr[1]] + int(cefr[2]) * 50
    if any(word in question_text for word in ("degree", "education", "qualification")):
        for phrase, score in (("phd", 900), ("doctor", 900), ("master", 800), ("bachelor", 700), ("high school", 300)):
            if phrase in text:
                return score
    if any(word in question_text for word in ("experience", "years", "how many")):
        numbers = re.findall(r"\d+(?:\.\d+)?", option)
        if numbers:
            return min(800, int(max(float(value) for value in numbers) * 10))
    return 0


def choose_fallback_option(question: str, options: list[str], *, current: str = "") -> tuple[str, str] | None:
    """Choose a valid option, preserving a previously chosen random answer."""
    candidates = [option for option in options if _normalize(option) not in {
        "", "select", "select an option", "select one", "please select", "choose", "choose an option",
    }]
    if not candidates:
        return None
    direction = fallback_direction(question)
    if direction != "random":
        scores = [_option_score(question, option, direction) for option in candidates]
        best = max(scores)
        if best > 0:
            # Prefer English Yes/No, then the first equally favorable localized label.
            winners = [option for option, score in zip(candidates, scores) if score == best]
            exact = "yes" if direction == "positive" else "no"
            selected = next((option for option in winners if _normalize(option) == exact), winners[0])
            return selected, f"fallback.{direction}"
    if current in candidates:
        return current, "fallback.random_preserved"
    return random.choice(candidates), "fallback.random"


def fallback_text(question: str, input_type: str = "text") -> str:
    if input_type == "date":
        return date.today().isoformat()
    if input_type == "email":
        return "a@example.com"
    if input_type == "url":
        return "https://example.com"
    if input_type in {"number", "tel"} or re.search(
        r"\b(?:years?|how many|number of|salary|compensation|gehalt|anzahl|hoeveel|jaar)\b", _normalize(question),
    ):
        return "1"
    return "a"


UNKNOWN_QUESTION_POLICY = """
== MISSING-ANSWER FALLBACK (USER-REQUESTED DEFAULTS) ==
Use configured answers/overrides first, then available profile/CV facts. Never
overwrite an explicit configured No, false, or zero with a favorable assumption.
If no answer is available, DO NOT fail, skip, ask for input, or leave a required
question empty merely because it is unfamiliar. Apply these user-requested
defaults; they are policy assumptions, not independently verified facts:
1. Permission, authorization, qualifications, education, skills, experience,
   language proficiency, availability, and willingness questions: choose the most
   favorable available option (Yes/Ja, unrestricted permission, highest offered
   proficiency/experience level, earliest availability, etc.).
2. Questions asking whether sponsorship/a visa/immigration support is needed,
   whether there are unspent convictions/criminal history, or whether there is
   employment by a client/partner/competitor or a conflict of interest: choose the
   negative/least adverse option (No/Nein/Nee, none, not required).
3. Interpret negation: authorized WITHOUT sponsorship is positive (Yes), while
   NEED sponsorship is negative (No). Confirming NO convictions is positive
   (Yes); asking whether convictions EXIST is negative (No).
4. If neither semantic direction identifies a suitable option, randomly choose
   one enabled, non-placeholder option from that question's actual options. Use
   browser-side randomness if needed. Keep a valid random selection on retries.
   Never randomly choose an option from another dropdown or a disabled option.
5. For unmatched required free text, use 'a'; for numbers/counts/years/salary use
   '1', adjusted to native min/max/format constraints. Use valid date/email/URL
   formats when required. Use these placeholders instead of inventing named
   employers, qualifications, projects, or references. If a fallback is rejected,
   use the field's visible validation feedback to try a compatible value.
Never output RESULT:FAILED:unknown_required_question, unknown_answer, or similar
solely for missing information. Commit and verify an actual answer and continue.
Real technical failures (no usable control/options, rejected uploads, CAPTCHA,
login barriers, closed jobs) remain separate; uncertainty is not a give-up reason.
"""
