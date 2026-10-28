"""Shared, local application history and atomic company cooldown reservations."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from applypilot import config

log = logging.getLogger(__name__)
DEFAULT_COOLDOWN_DAYS = 30
RESERVATION_SECONDS = 600


class HistoryError(RuntimeError):
    """History could not be checked or saved; do not silently disable protection."""


def cooldown_days(settings: dict | None = None) -> int:
    value = (settings or {}).get("company_cooldown_days", DEFAULT_COOLDOWN_DAYS)
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value).strip()):
        raise ValueError("company_cooldown_days must be a non-negative integer (0 disables the cooldown)")
    return int(value)


def normalize_company(value) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = " ".join(re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).replace("_", " ").split())
    return "" if value in {"", "unknown", "unknown company", "n a", "none", "nan"} else value


def _url(value) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            return ""
        query = urlencode([(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
                           if not key.casefold().startswith("utm_") and key.casefold() not in {
                               "trackingid", "refid", "gh_src", "source", "ref",
                           }])
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), query, ""))
    except ValueError:
        return ""


def job_key(job: dict) -> str:
    url = _url(job.get("linkedin_url") or job.get("url") or job.get("job_url") or job.get("application_url"))
    if not url:
        raise HistoryError("Cannot record or protect an application without a job URL")
    parsed = urlsplit(url)
    if parsed.hostname == "linkedin.com" or (parsed.hostname or "").endswith(".linkedin.com"):
        match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)(?:/|$)", parsed.path)
        if match:
            return "linkedin:" + match[1]
    return url


def _company_url(job: dict) -> str:
    url = _url(job.get("company_url"))
    if url:
        parsed = urlsplit(url)
        if parsed.hostname == "linkedin.com" or (parsed.hostname or "").endswith(".linkedin.com"):
            match = re.match(r"/company/([^/]+)", parsed.path)
            return "linkedin-company:" + match[1].casefold() if match else ""
        return "company-url:" + urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    identifier = job.get("company_id")
    return "linkedin-company:" + str(identifier) if isinstance(identifier, (str, int)) and identifier else ""


def _company_name(job: dict) -> str:
    value = job.get("company") or job.get("employer_name") or ""
    if not value and job.get("strategy") == "workday_api":
        value = job.get("site", "")  # Here site is the employer, not a job board.
    return value.strip() if isinstance(value, str) else ""


def page_job_metadata(page, url: str) -> dict:
    """Read employer metadata locally; never confuse the job-board hostname with an employer."""
    job = {"url": url, "company": "", "company_url": "", "title": ""}
    try:
        values = page.evaluate(r"""() => {
          const result = {company: '', company_url: '', title: document.querySelector('h1')?.textContent?.trim() || ''};
          function visit(node) {
            if (!node || typeof node !== 'object') return;
            if (Array.isArray(node)) { node.forEach(visit); return; }
            if ([node['@type']].flat().includes('JobPosting')) {
              const org = node.hiringOrganization || {};
              result.company = org.name || result.company;
              result.company_url = org.sameAs || org.url || result.company_url;
              result.title = node.title || result.title;
            }
            if (node['@graph']) visit(node['@graph']);
          }
          for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
            try { visit(JSON.parse(el.textContent)); } catch (_) {}
          }
          if (!result.company) {
            const el = document.querySelector('[data-at="header-company-name"], [data-testid="company-name"], .job-details-jobs-unified-top-card__company-name, a.topcard__org-name-link, [aria-label^="Company,"]');
            if (el) {
              result.company = (el.getAttribute('aria-label') || '').replace(/^Company,\s*/i, '').replace(/\.$/, '') || el.textContent.trim();
              result.company_url = el.href || el.querySelector('a')?.href || '';
            }
          }
          return result;
        }""")
        if isinstance(values, dict):
            job.update({k: v for k, v in values.items() if k in job and isinstance(v, str)})
    except Exception:
        log.debug("Could not read company metadata for %s", url, exc_info=True)
    return job


def _timestamp(value) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value) if 0 < value <= time.time() + 300 else None
        parsed = datetime.fromisoformat(value)
        return _timestamp(parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp())
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


class ApplicationHistory:
    def __init__(self, settings: dict | None = None, *, db_path=None, legacy_path=None):
        self.days = cooldown_days(settings)
        self.path = Path(db_path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.connection() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS application_history (
                        job_key TEXT PRIMARY KEY, company_key TEXT NOT NULL,
                        company_name TEXT NOT NULL, company_url TEXT, job_url TEXT NOT NULL,
                        title TEXT, application_url TEXT, source TEXT NOT NULL, applied_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS application_history_company_time
                        ON application_history(company_key, applied_at);
                    CREATE TABLE IF NOT EXISTS application_company_aliases (
                        alias TEXT PRIMARY KEY, company_key TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS company_application_reservations (
                        company_key TEXT PRIMARY KEY, token TEXT NOT NULL,
                        job_url TEXT NOT NULL, expires_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS application_history_imports (
                        path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL
                    );
                """)
            self.import_legacy(Path(legacy_path) if legacy_path else config.APP_DIR / "linkedin_noneasy_applied.jsonl")
            self.import_pipeline()
        except (OSError, sqlite3.Error) as exc:
            raise HistoryError(f"Cannot initialize application history at {self.path}: {exc}") from exc

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _identity(self, conn, job):
        name = normalize_company(_company_name(job))
        url_key = _company_url(job)
        name_key = "name:" + name if name else ""
        aliases = [alias for alias in (url_key, name_key) if alias]
        key = ""
        for alias in aliases:
            row = conn.execute("SELECT company_key FROM application_company_aliases WHERE alias=?", (alias,)).fetchone()
            if row:
                # Distinct known LinkedIn company IDs must not be merged just because names coincide.
                ids = {r[0] for r in conn.execute(
                    "SELECT alias FROM application_company_aliases WHERE company_key=? AND alias LIKE 'linkedin-company:%'",
                    (row[0],))}
                if alias == name_key and url_key.startswith("linkedin-company:") and ids and url_key not in ids:
                    continue
                key = row[0]
                break
        key = key or url_key or name_key or "unknown-job:" + hashlib.sha256(job_key(job).encode()).hexdigest()
        for alias in aliases:
            conn.execute("INSERT OR IGNORE INTO application_company_aliases VALUES (?, ?)", (alias, key))
        return key

    def _write(self, conn, job, source, applied_at):
        key = self._identity(conn, job)
        url = job.get("linkedin_url") or job.get("url") or job.get("job_url") or job.get("application_url")
        conn.execute("""INSERT INTO application_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_key) DO UPDATE SET
                applied_at=MAX(application_history.applied_at, excluded.applied_at),
                company_key=CASE WHEN excluded.company_key LIKE 'unknown-job:%' THEN application_history.company_key
                    WHEN application_history.company_key LIKE 'unknown-job:%'
                      OR excluded.applied_at > application_history.applied_at
                      OR (excluded.company_url!='' AND COALESCE(application_history.company_url, '')='')
                    THEN excluded.company_key ELSE application_history.company_key END,
                company_name=COALESCE(NULLIF(excluded.company_name, ''), application_history.company_name),
                company_url=COALESCE(NULLIF(excluded.company_url, ''), application_history.company_url),
                source=CASE WHEN excluded.applied_at > application_history.applied_at
                    OR (application_history.source LIKE '%:legacy' AND excluded.source NOT LIKE '%:legacy')
                    THEN excluded.source ELSE application_history.source END
        """, (job_key(job), key, _company_name(job) if normalize_company(_company_name(job)) else "", job.get("company_url", ""), url,
              job.get("title", ""), job.get("application_url", ""), source, applied_at))

    def record(self, job: dict, source: str, *, applied_at=None):
        at = int(time.time()) if applied_at is None else _timestamp(applied_at)
        if at is None:
            raise HistoryError("A successful application must have a valid UTC timestamp")
        try:
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._write(conn, job, source, at)
        except sqlite3.Error as exc:
            raise HistoryError(f"Cannot save successful application to {self.path}: {exc}") from exc

    def import_legacy(self, path: Path):
        try:
            stat = path.stat()
        except FileNotFoundError:
            return
        fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}"
        with self.connection() as conn:
            previous = conn.execute("SELECT fingerprint FROM application_history_imports WHERE path=?", (str(path),)).fetchone()
            if previous and previous[0] == fingerprint:
                return
            conn.execute("BEGIN IMMEDIATE")
            count = 0
            with path.open(encoding="utf-8") as fh:
                for number, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    try:
                        job = json.loads(line)
                        at = _timestamp(job.get("recorded_at"))
                        if at is None:
                            raise ValueError("missing/invalid application timestamp")
                        self._write(conn, job, "linkedin-noneasy:legacy", at)
                        count += 1
                    except (ValueError, AttributeError, HistoryError):
                        log.warning("Skipping invalid legacy application history line %s in %s", number, path)
            conn.execute("INSERT OR REPLACE INTO application_history_imports VALUES (?, ?)", (str(path), fingerprint))
            log.info("Imported %s legacy application records into shared history", count)

    def import_pipeline(self):
        with self.connection() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                return
            rows = conn.execute("SELECT * FROM jobs WHERE applied_at IS NOT NULL AND apply_status='applied'").fetchall()
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                job = dict(row)
                at = _timestamp(job.get("applied_at"))
                if at is not None:
                    self._write(conn, job, "apply:legacy", at)

    def attempt(self, job: dict, source: str, *, dry_run=False):
        return ApplicationAttempt(self, job, source, dry_run)


class ApplicationAttempt:
    """Atomic check-and-reserve; heartbeat prevents another worker applying concurrently."""

    def __init__(self, history, job, source, dry_run):
        self.history, self.job, self.source, self.dry_run = history, job, source, dry_run
        self.token = uuid.uuid4().hex
        self.allowed, self.reason, self.last_applied_at = True, "", None
        self._stop = threading.Event()
        self._thread = None
        self._record_error = False
        now = int(time.time())
        try:
            with history.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self.key = history._identity(conn, job)
                if history.days:
                    recent = conn.execute("""SELECT MAX(applied_at) FROM application_history
                        WHERE company_key=? AND applied_at>?""", (self.key, now - history.days * 86400)).fetchone()[0]
                    if recent is not None:
                        self.allowed, self.reason, self.last_applied_at = False, "company_cooldown", recent
                    elif conn.execute("SELECT 1 FROM company_application_reservations WHERE company_key=? AND expires_at>?",
                                      (self.key, now)).fetchone():
                        self.allowed, self.reason = False, "company_in_progress"
                    elif not dry_run:
                        conn.execute("INSERT OR REPLACE INTO company_application_reservations VALUES (?, ?, ?, ?)",
                                     (self.key, self.token, job_key(job), now + RESERVATION_SECONDS))
        except sqlite3.Error as exc:
            raise HistoryError(f"Cannot check company application history: {exc}") from exc
        if not self.allowed:
            previous = datetime.fromtimestamp(self.last_applied_at, UTC).isoformat() if self.last_applied_at else "in progress"
            log.info("Skipping company %r: %s; last successful application=%s; cooldown=%s days",
                     _company_name(job), self.reason, previous, history.days)
        elif history.days and not dry_run:
            if self.key.startswith("unknown-job:"):
                log.warning("Employer identity unavailable for %s; only exact-job, not company-wide, protection is possible", job_key(job))
            self._thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._thread.start()

    def renew(self):
        with self.history.connection() as conn:
            return conn.execute("UPDATE company_application_reservations SET expires_at=? WHERE company_key=? AND token=?",
                                (int(time.time()) + RESERVATION_SECONDS, self.key, self.token)).rowcount == 1

    def _heartbeat(self):
        while not self._stop.wait(30):
            try:
                if not self.renew():
                    log.error("Lost company reservation for %s", self.key)
                    return
            except sqlite3.Error:
                log.exception("Could not refresh company reservation for %s", self.key)

    def success(self, job=None):
        if self.dry_run or not self.allowed:
            return
        try:
            self.history.record(job or self.job, self.source)
        except Exception:
            self._record_error = True
            raise

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.dry_run or self._record_error:
            return
        try:
            with self.history.connection() as conn:
                conn.execute("DELETE FROM company_application_reservations WHERE company_key=? AND token=?", (self.key, self.token))
        except sqlite3.Error as exc:
            raise HistoryError(f"Cannot release company reservation: {exc}") from exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
