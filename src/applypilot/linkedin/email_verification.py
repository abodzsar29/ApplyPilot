"""Bounded, local Greenhouse/Proton email-code handoff; never submit or log codes."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from applypilot.linkedin.jobboards import load_jobboard_filter

log = logging.getLogger(__name__)

_GROUPS = 'fieldset, [role="group"]'
_INPUTS = 'input:not([type="hidden"]):not([type="submit"]):not([type="button"])'
_ARTICLES = 'article[data-testid^="message-view-"]'
_FRAMES = 'iframe[data-testid="content-iframe"], iframe[title="Email content"]'
_EMAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[a-z]{2,}", re.IGNORECASE)
_SUBJECT = re.compile(r"^Security code for your application to\s+(.+?)\s*$", re.IGNORECASE)


def _identity(value: str) -> str:
    return re.sub(r"[\W_]+", "", value).casefold()


def is_verification_field(control) -> bool:
    """Exclude security codes from the ordinary configured-answer/default prefill."""
    return control.evaluate(r"""el => {
        if (el.autocomplete === 'one-time-code') return true;
        const group = el.closest('fieldset, [role="group"]');
        const label = [el.getAttribute('aria-label'), el.id, el.name,
            ...[...(el.labels || [])].map(node => node.innerText),
            group?.getAttribute('aria-label'), group?.innerText].filter(Boolean).join(' ');
        return /(?:security|verification|one[- ]time)\s*(?:code|passcode)/i.test(label);
    }""") is True


def _challenge(page):
    evidence = page.evaluate(r"""() => {
        const visible = el => el.getClientRects().length > 0
            && !el.closest('[hidden], [aria-hidden="true"]')
            && getComputedStyle(el).visibility !== 'hidden';
        const groups = [...document.querySelectorAll('fieldset, [role="group"]')];
        for (const [index, group] of groups.entries()) {
            if (!visible(group)) continue;
            const labelled = (group.getAttribute('aria-labelledby') || '').split(/\s+/)
                .map(id => document.getElementById(id)?.innerText || '').join(' ');
            const text = [group.getAttribute('aria-label'), labelled, group.innerText].filter(Boolean).join(' ');
            if (!/(?:security|verification|one[- ]time)\s*(?:code|passcode)/i.test(text)) continue;
            const inputs = [...group.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"])')]
                .filter(el => visible(el) && !el.disabled && !el.readOnly);
            const length = Number(text.match(/(\d+)[-\s]*(?:character|digit)/i)?.[1])
                || (inputs.length === 1 ? inputs[0].maxLength : inputs.length);
            if (length < 4 || length > 12 || ![1, length].includes(inputs.length)) continue;
            const invalid = inputs.some(el => el.getAttribute('aria-invalid') === 'true')
                || /(?:invalid|incorrect|expired)\s+(?:(?:security|verification)\s+)?code/i.test(text);
            return {index, text, length, invalid, values: inputs.map(el => el.value)};
        }
        return null;
    }""")
    return evidence if isinstance(evidence, dict) else None


def _usable_inputs(page, challenge):
    group = page.locator(_GROUPS).nth(challenge["index"])
    controls = group.locator(_INPUTS)
    return [controls.nth(index) for index in range(controls.count())
            if controls.nth(index).is_visible() and controls.nth(index).is_enabled()
            and controls.nth(index).get_attribute("readonly") is None]


def _fill_code(page, code: str, destination, recipient: str) -> bool:
    """Use real input events and reacquire each input after React re-renders."""
    def current_challenge():
        current = urlsplit(page.url)
        if (current.scheme, current.netloc, current.path) != destination:
            return None
        evidence = _challenge(page)
        recipients = {address.casefold() for address in _EMAIL.findall(evidence["text"])} if evidence else set()
        return evidence if not recipients or recipients == {recipient} else None

    challenge = current_challenge()
    if not challenge or len(code) != challenge["length"]:
        return False
    inputs = _usable_inputs(page, challenge)
    if len(inputs) == 1:
        inputs[0].fill(code, timeout=1000)
    elif len(inputs) == len(code):
        for index, character in enumerate(code):
            current = current_challenge()
            if not current or current["length"] != len(code):
                return False
            controls = _usable_inputs(page, current)
            if len(controls) != len(code):
                return False
            controls[index].fill(character, timeout=1000)
    else:
        return False
    current = current_challenge()
    return bool(current and not current["invalid"] and "".join(current["values"]) == code)


def _message_metadata(article) -> dict:
    return article.evaluate(r"""el => {
        const sender = el.querySelector('[data-testid="recipients:sender"], [data-testid^="recipient:details-dropdown-"]');
        const recipients = el.querySelector('[data-testid="message-header-expanded:To"]');
        const raw = el.querySelector('time[datetime]')?.getAttribute('datetime') || '';
        // Proton uses a human-readable local datetime, not always ISO 8601.
        const received = Date.parse(raw.replace(/\s+at\s+/i, ' '));
        return {sender: sender?.getAttribute('href') || sender?.getAttribute('title') || '',
            recipients: [...(recipients?.querySelectorAll('a[href^="mailto:"]') || [])]
                .map(node => node.getAttribute('href')).join(' '),
            received: Number.isFinite(received) ? received / 1000 : null};
    }""")


class EmailVerificationHandler:
    """Handle an actual challenge in the existing session, with at most three polls."""

    def __init__(self, page, mailbox, settings: dict, company: str, *, poll_delay: float = 10):
        self.page, self.mailbox, self.settings = page, mailbox, settings
        self.email = str((settings.get("profile") or {}).get("email") or "").strip().casefold()
        self.inbox = str(settings.get("mail_inbox_url") or "https://mail.proton.me/u/0/inbox")
        self.mail_host = urlsplit(self.inbox).hostname
        self.company_names = {_identity(company), _identity(urlsplit(page.url).path.strip("/").split("/")[0])}
        self.company_names.discard("")
        self.poll_delay = poll_delay
        self.started_at = time.time()
        self.attempted: set[str] = set()
        self.failure: str | None = None

    def _subject_matches(self, subject: str) -> bool:
        match = _SUBJECT.fullmatch(subject.strip())
        return bool(match and _identity(match[1]) in self.company_names)

    def _read_code(self, length: int) -> str | None:
        articles = self.mailbox.locator(_ARTICLES)
        candidates = []
        for index in range(min(articles.count(), 30)):
            article = articles.nth(index)
            metadata = _message_metadata(article)
            addresses = _EMAIL.findall(metadata["sender"])
            domain = addresses[0].split("@")[-1].lower() if addresses else ""
            if not any(domain == host or domain.endswith("." + host)
                       for host in ("greenhouse-mail.io", "greenhouse.io")):
                continue
            received = metadata["received"]
            # Minute-resolution timestamps need a small tolerance, not an old
            # code from another day or a previous application to this company.
            if received is None or not self.started_at - 60 <= received <= time.time() + 60:
                continue
            candidates.append((received, index))
        for _, index in sorted(candidates, reverse=True)[:3]:
            article = articles.nth(index)
            if article.get_attribute("data-expanded") == "false":
                article.locator('[data-testid^="message-header-collapsed:"]').click(timeout=800)
            metadata = _message_metadata(article)
            recipients = {address.casefold() for address in _EMAIL.findall(metadata["recipients"])}
            if self.email not in recipients:
                continue
            frames = article.locator(_FRAMES)
            try:
                frames.first.wait_for(state="attached", timeout=500)
            except PlaywrightTimeoutError:
                continue
            for frame_index in range(min(frames.count(), 3)):
                iframe = frames.nth(frame_index)
                if not self._subject_matches(iframe.get_attribute("data-subject") or ""):
                    continue
                element = iframe.element_handle(timeout=800)
                frame = element.content_frame() if element else None
                if frame is None:
                    continue
                headings = frame.locator("h1:visible")
                try:
                    headings.first.wait_for(state="visible", timeout=1000)
                except PlaywrightTimeoutError:
                    return None
                codes = set(headings.all_text_contents())
                codes = {text.strip() for text in codes if re.fullmatch(rf"[A-Za-z0-9]{{{length}}}", text.strip())}
                if len(codes) == 1:
                    code = codes.pop()
                    if hashlib.sha256(code.encode()).hexdigest() not in self.attempted:
                        return code
                # Never fall back to an older code when the newest matching
                # message is still loading, ambiguous, or already attempted.
                return None
        return None

    def _open_message(self):
        headings = self.mailbox.get_by_role("heading", level=2)
        for index in range(min(headings.count(), 50)):
            heading = headings.nth(index)
            if heading.is_visible() and self._subject_matches(heading.inner_text(timeout=800)):
                heading.click(timeout=800)
                return
        # An unrelated conversation can already be open. Return to the inbox,
        # preserving Proton's current /u/N account instead of assuming /u/0.
        current = urlsplit(self.mailbox.url)
        account = re.match(r"/u/\d+", current.path)
        inbox_path = account[0] + "/inbox" if account else urlsplit(self.inbox).path
        if current.path != inbox_path:
            self.mailbox.goto(urlunsplit((current.scheme, current.netloc, inbox_path, "", "")),
                              wait_until="domcontentloaded", timeout=5000)

    def check(self) -> str | None:
        """A terminal failure or None; a verified fill is not submission success."""
        try:
            current = urlsplit(self.page.url)
            host = current.hostname or ""
            if current.scheme not in {"http", "https"} or not (host == "greenhouse.io" or host.endswith(".greenhouse.io")):
                return None
            allowlist = load_jobboard_filter(self.settings)
            if allowlist and not allowlist.matches(self.page.url):
                return None
            challenge = _challenge(self.page)
            if not challenge:
                return None
            if self.failure:
                return self.failure
            if not challenge["invalid"] and len("".join(challenge["values"])) == challenge["length"]:
                return None
            recipient = {address.casefold() for address in _EMAIL.findall(challenge["text"])}
            if (not self.email or (recipient and recipient != {self.email}) or not self.mailbox
                    or self.mailbox.is_closed() or urlsplit(self.mailbox.url).hostname != self.mail_host):
                self.failure = "RESULT:FAILED:email_2fa_unavailable"
                return self.failure
            if len(self.attempted) >= 3:
                self.failure = "RESULT:FAILED:email_verification_rejected"
                return self.failure
            for attempt in range(3):
                code = self._read_code(challenge["length"])
                if code is None:
                    self._open_message()
                    code = self._read_code(challenge["length"])
                if code:
                    self.attempted.add(hashlib.sha256(code.encode()).hexdigest())
                    if _fill_code(self.page, code, (current.scheme, current.netloc, current.path), self.email):
                        log.info("Email verification: filled and verified the security-code inputs; code omitted")
                        return None
                    self.failure = "RESULT:FAILED:email_verification_input_failed"
                    return self.failure
                if attempt < 2:
                    self.mailbox.wait_for_timeout(self.poll_delay * 1000)
            self.failure = "RESULT:FAILED:email_2fa_unavailable"
        except PlaywrightError:
            self.failure = "RESULT:FAILED:email_verification_input_failed"
        log.info("Email verification could not complete: %s", self.failure)
        return self.failure
