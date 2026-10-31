"""Read-only detection of missing/expired external job pages, without an LLM."""

import re

from playwright.sync_api import Error as PlaywrightError

_UNAVAILABLE = re.compile(
    r"(?:sorry[,!:.]?\s*)?(?:"
    r"(?:this\s+)?(?:url|page)\s+(?:does not exist|doesn['’]t exist|not found|cannot be found)|"
    r"(?:this\s+)?(?:job(?:\s+(?:posting|listing|opening))?|position|vacancy)\s+"
    r"(?:(?:is|was|has been)\s+)?(?:not found|not available|no longer (?:available|active|open)|"
    r"(?:has\s+)?expired|closed|removed|filled)|"
    r"(?:we are|we['’]re)\s+no longer accepting applications(?:\s+for this (?:job|position))?|"
    r"(?:this\s+)?(?:job|position)\s+is no longer accepting applications|"
    r"(?:diese\s+)?(?:stelle|position|stellenanzeige)\s+(?:(?:ist|wurde)\s+)?"
    r"(?:nicht gefunden|nicht mehr verf[üu]gbar|nicht mehr vakant|bereits besetzt|geschlossen)|"
    r"page not found|job not found|404(?:\s*(?:error|not found))?|410(?:\s*gone)?"
    r")[.!:\s]*",
    re.IGNORECASE,
)


def unavailable_job_reason(page) -> str | None:
    """Inspect visible headings/error panels, never scripts, hidden markup or job prose."""
    try:
        evidence = page.evaluate(
            r"""() => {
                const visible = el => el && !el.closest('[hidden], [aria-hidden="true"], script, style, template')
                    && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
                const headings = [...document.querySelectorAll('h1, h2, h3, [role="heading"]')]
                    .filter(visible).map(el => el.innerText.trim());
                const panels = [...document.querySelectorAll(
                    '[class*="Error404_"], [data-testid="job-not-found"], [data-testid="not-found"], [role="alert"]'
                )].filter(visible).map(el => el.innerText.trim());
                const main = document.querySelector('main, [role="main"]') || document.body;
                // Plain-text error pages sometimes have no heading. Only inspect
                // short pages without an application form as a fallback.
                const compact = main && visible(main) && !main.querySelector('form, input, textarea')
                    && main.innerText.length < 1000 ? main.innerText.trim() : '';
                return { headings, panels, compact };
            }"""
        )
    except PlaywrightError:
        return None  # Navigation/unknown state is not evidence that a job expired.
    if not isinstance(evidence, dict):
        return None
    candidates = list(evidence.get("headings", []))
    for panel in [*evidence.get("panels", []), evidence.get("compact", "")]:
        candidates.extend(panel.splitlines())
    for candidate in candidates:
        text = " ".join(candidate.split())
        if text and _UNAVAILABLE.fullmatch(text):
            return f"job_unavailable: {text}"
    return None


class JobAvailabilityMonitor:
    """Track main-document 404/410 responses and visible soft-404/closed-job pages."""

    def __init__(self, context):
        self.context = context
        self.documents = {}
        self._listener = self._on_response
        self._request_listener = self._on_request
        context.on("request", self._request_listener)
        context.on("response", self._listener)

    def _on_request(self, request):
        try:
            if request.resource_type == "document" and request.frame == request.frame.page.main_frame:
                # A new navigation supersedes an earlier failed document, even
                # before its response has arrived.
                self.documents.pop(request.frame.page, None)
        except PlaywrightError:
            pass

    def _on_response(self, response):
        try:
            if response.request.resource_type != "document":
                return
            frame = response.frame
            if frame != frame.page.main_frame:
                return
            self.documents[frame.page] = (response.url, response.status)
        except PlaywrightError:
            pass

    def reason(self, page) -> str | None:
        # The DOM read also dispatches pending browser response events from the
        # agent's separate CDP connection before examining the latest status.
        visible_reason = unavailable_job_reason(page)
        _response_url, status = self.documents.get(page, (None, None))
        # Chromium can reject an empty 404 response while retaining the previous
        # URL/DOM. It is still a failed main-document navigation, not a live job.
        if status in {404, 410}:
            return f"job_unavailable: HTTP {status}"
        return visible_reason

    def close(self):
        self.context.remove_listener("request", self._request_listener)
        self.context.remove_listener("response", self._listener)
