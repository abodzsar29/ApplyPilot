from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from html import unescape
from pathlib import Path

import requests


BASE = "https://www.glassdoor.co.uk/Interview/Helsing-Interview-Questions-E6763957"
PAGES = range(11, 17)
OUT = Path("glassdoor_helsing_interview_reviews.txt")
MAX_FAILURES = 5
DELAY_SECONDS = 60


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


def page_url(page: int) -> str:
    return f"{BASE}_P{page}.htm"


def clean_text(value: str) -> str:
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def looks_blocked(status_code: int, text: str) -> bool:
    marker_text = text[:5000].lower()
    return (
        status_code in {401, 403, 429}
        or "security | glassdoor" in marker_text
        or "authenticating" in marker_text
        or "captcha" in marker_text
        or "access denied" in marker_text
    )


def extract_reviewish_text(html: str) -> str | None:
    text = clean_text(html)
    if not text:
        return None

    has_page_marker = "Viewing " in text and " of " in text
    has_interview_marker = "Interview" in text and ("Application" in text or "Question" in text)
    if not (has_page_marker and has_interview_marker):
        return None

    # Keep the relevant page body rather than the entire navigation/security shell.
    start = text.find("Viewing ")
    if start == -1:
        start = max(0, text.find("Interview"))
    end_candidates = [
        pos for pos in (text.find("Glassdoor", start + 50), text.find("Explore", start + 50))
        if pos != -1
    ]
    end = min(end_candidates) if end_candidates else len(text)
    body = text[start:end].strip()
    return body if len(body) > 250 else None


def fetch_page(page: int) -> tuple[str | None, list[str]]:
    url = page_url(page)
    failures: list[str] = []
    with requests.Session() as session:
        for attempt in range(1, MAX_FAILURES + 1):
            print(f"Page {page}, attempt {attempt}/{MAX_FAILURES}: {url}", flush=True)
            try:
                response = session.get(url, headers=HEADERS, timeout=30)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"attempt {attempt}: request error: {exc}")
            else:
                if looks_blocked(response.status_code, response.text):
                    title = re.search(r"<title>(.*?)</title>", response.text, flags=re.I | re.S)
                    title_text = clean_text(title.group(1)) if title else "no title"
                    failures.append(
                        f"attempt {attempt}: blocked/status {response.status_code}, title: {title_text}"
                    )
                else:
                    extracted = extract_reviewish_text(response.text)
                    if extracted:
                        print(f"Page {page}: extracted usable text.", flush=True)
                        return extracted, failures
                    failures.append(
                        f"attempt {attempt}: status {response.status_code}, no usable review text"
                    )

            if attempt < MAX_FAILURES:
                print(
                    f"Page {page}: blocked or unusable; waiting {DELAY_SECONDS} seconds.",
                    flush=True,
                )
                time.sleep(DELAY_SECONDS)

    return None, failures


def main() -> int:
    inserted: list[str] = [
        "",
        "",
        "APPENDED RETRY RESULTS FOR PAGES 11-16",
        f"Inserted at bottom of file: {datetime.now().isoformat(timespec='seconds')}",
        "Retry policy: up to 5 failed attempts per page, with a 60-second delay after each blocked/unusable attempt before retrying.",
        "",
    ]

    for page in PAGES:
        extracted, failures = fetch_page(page)
        inserted.append(f"PAGE {page} RETRY RESULT")
        if extracted:
            inserted.append(extracted)
        else:
            inserted.append("No usable public review text was returned after 5 failed attempts.")
            inserted.append("Failures:")
            inserted.extend(f"- {failure}" for failure in failures)
        inserted.append("")

    with OUT.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(inserted))
        handle.write("\n")

    print(f"Appended retry results to {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
