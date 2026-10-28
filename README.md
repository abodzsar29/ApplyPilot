The project is being developed to apply to LinkedIn EasyApply jobs through Playwright + Python based on hard-coded rule matching, and apply to external jobs after searching on LinkedIn with interaction through MCP, for as little of a cost as possible, currently with capabilities through OpenRouter and Claude.

## README.md from the time of forking below

<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a 6-stage autonomous job application pipeline. It discovers jobs across 5+ boards, scores them against your resume with AI, tailors your resume per job, writes cover letters, and **submits applications for you**. It navigates forms, uploads documents, answers screening questions, all hands-free.

Three commands. That's it.

```bash
pip install applypilot
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
applypilot apply         # autonomous browser-driven submission
applypilot apply -w 3    # parallel apply (3 Chrome instances)
applypilot apply --dry-run  # fill forms without submitting
applypilot linkedin-apply --setup  # open LinkedIn session and keep it live until Ctrl+C
applypilot linkedin-apply --exactlocation  # tick the exact LinkedIn location filter from config
```

> **Why two install commands?** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine at runtime with any modern numpy. The `--no-deps` flag bypasses the resolver; the second command installs jobspy's actual runtime dependencies. Everything except `python-jobspy` installs normally.

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, Node.js (for npx), Gemini API key (free), Claude Code CLI, Chrome

Runs all 6 stages, from job discovery to autonomous application submission. This is the full power of ApplyPilot.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, Gemini API key (free)

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Scrapes 5 job boards (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs) + 48 Workday employer portals + 30 direct career sites |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI rewrites your resume per job: reorganizes, emphasizes relevant experience, adds keywords. Never fabricates |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | Claude Code navigates application forms, fills fields, uploads documents, answers questions, and submits |

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | 5 boards + Workday + direct sites | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job AI rewrite | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs, 46 Workday portals, 28 direct sites | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| OpenRouter or DashScope Qwen API key | LinkedIn non-Easy-Apply via Qwen | Set `OPENROUTER_API_KEY` for OpenRouter or `DASHSCOPE_API_KEY` for DashScope |
| Gemini API key | Scoring, tailoring, cover letters | Free tier (15 RPM / 1M tokens/day) is enough |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply | Install from [claude.ai/code](https://claude.ai/code) |
| `qwen-agent[mcp]` | LinkedIn non-Easy-Apply via Qwen | Install with `pip install -U "qwen-agent[mcp]"` |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). Qwen via OpenRouter or DashScope / Model Studio, OpenAI, and local models (Ollama/llama.cpp) are also supported.

### Optional

| Component | What It Does |
|-----------|-------------|
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile, FunCaptcha). Without it, CAPTCHA-blocked applications just fail gracefully |

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine with modern numpy at runtime.

---

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults. Powers scoring, tailoring, and form auto-fill.

### `searches.yaml`
Job search queries, target titles, locations, boards. Run multiple searches with different parameters.

### `.env`
API keys and runtime config: `NON_EASY_PROVIDER`, `NON_EASY_MODEL`, `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `QWEN_MODEL`, `DASHSCOPE_API_KEY`, `GEMINI_API_KEY`, `LLM_MODEL`, `CAPSOLVER_API_KEY` (optional).

### `config/linkedin_apply.json`
LinkedIn application config, shared by `linkedin-apply` and `linkedin-noneasy`.

Set `"company_blacklist": ["Crossing Hurdles", "Avaloq"]` to skip those companies
before résumé generation or opening the application. Both commands use the same
matching rules: capitalization, accents, punctuation, and repeated whitespace are
normalized, and either name containing the other counts as a match (for example,
`Avaloq` matches `Avaloq Group AG`). Matching uses the company name, not the job
title or description. A missing or empty list disables the filter; blank entries
are ignored. Non-Easy Apply also accepts `null` to disable it, logs every blacklist
skip, and reports a `Company blacklisted` count in its summary. Blacklist skips do
not consume its application limit or create application-history records.

Set `browser` to `chrome` or `brave` to launch that installed browser instead of the default Playwright Chromium browser. Optional browser-context settings include `locale`, `timezone_id`, `geolocation`, `permissions`, `user_agent`, `viewport`, `device_scale_factor`, `accept_language`, and `extra_http_headers`. Set `search_url` only to reuse a LinkedIn jobs route; the generated `job_title`, `location`, and Easy Apply filter still come from config so stale copied URLs cannot override them. Set `"old_url": true` to preserve the copied `search_url` parameters while replacing its `keywords` value with `job_title` from config. Set `use_search_results_url` to `true` to open LinkedIn's `/jobs/search-results/` route instead of `/jobs/search/`. Add intentional extra query values with `search_params`, for example a fresh `geoId` or `distance`. For phone dropdowns, set `profile.phone_country` to the country name LinkedIn shows, for example `United Kingdom`.

If LinkedIn displays a different search location, Easy Apply corrects it through the location button/popover or legacy inline input, selects the matching suggestion, and verifies the updated location before processing jobs. An unresolved location mismatch still stops the run to avoid applying in the wrong place.

Both LinkedIn application commands honor `max_scan_pages` in this JSON file (a positive
integer, for example `2000`). It limits search-result pages, while `max_applications`
limits successful applications (or ready applications in a dry run); skipped/failed
jobs do not consume the application limit. Without `max_scan_pages`, Easy Apply scans
all available pages and Non-Easy Apply retains its default of three pages.
Both commands poll for up to ten seconds for fresh job IDs after clicking Next,
ignoring temporary empty/stale results. Their summaries show pages scanned and an
explicit scan stop reason: `no_next_page` when no enabled Next control remains,
`pagination_no_new_results` for a loading/no-progress timeout, or
`max_scan_pages_reached` / `max_applications_reached` for configured limits. A
timeout is not reported as proof that all remaining jobs were exhausted.

Set `"to_ignore": ["senior", "lead", "scientist"]` to skip job titles containing
**any** listed text in `linkedin-apply` and `linkedin-noneasy`, regardless of
capitalization. A single string such as `"to_ignore": "scientist"` still works. This is a
literal substring match against the title only, not the company or description;
for example, it excludes `Climate Simulation & AI Scientist, Built Environment`.
Each entry can also be a phrase; a comma within a string is literal, not a separator.
Missing, null, empty, or whitespace-only values (including an empty list) disable the filter. Matching jobs
are logged and skipped before résumé tailoring or application handoff, whether
Intelligent Apply is enabled or not. `title_keyword` remains the separate positive
title filter; a job must pass both filters.

### Shared application history and company cooldown

Every application route now shares `application_history` in the existing
`~/.applypilot/applypilot.db` SQLite database (or the `APPLYPILOT_DIR` location):
`linkedin-apply`, `linkedin-noneasy`, StepStone, the legacy URL-list/simple apply
runner, and the original `apply` workers. A successful submission records the
company identity and display name, job URL/title, external application URL,
source command, and UTC application time. Canonical job keys prevent duplicate
records; ordinary tracking parameters do not create a new job identity.

Set `"company_cooldown_days": 30` in your LinkedIn or StepStone JSON config. This
is a rolling 30-day window, not a calendar month. Missing values default to 30;
`0` disables the company cooldown but still records successful applications.
The original worker command accepts `applypilot apply --company-cooldown-days 30`.
`apply --gen` only generates a debugging prompt; if you submit an application
outside the managed runners, use `apply --mark-applied URL` to add that success
to the shared history.
The standalone pipeline's tailoring and cover-letter stages use the same default
or `company_cooldown_days` in `~/.applypilot/profile.json`.

The check runs before application filling/uploads and job-specific résumé
generation. Tailoring/cover-letter pipeline stages also skip companies currently
on cooldown. Skips log the previous successful application date. A failure, a
dry run, setup mode, or just preparing a résumé does not start a cooldown.
The existing permanent exact-job duplicate check in non-Easy Apply remains in
place independently of the company cooldown.

Names are matched exactly after Unicode/case, accent, whitespace and punctuation
normalization, never by blacklist-style substring matching. Known company URLs
(including LinkedIn company IDs/slugs) connect name aliases. Corporate suffixes
are not guessed away. Two known, different LinkedIn company IDs remain distinct.
When an employer cannot be identified from stored data or page metadata, the
runner logs a warning and uses a job-specific identity rather than treating all
"Unknown company" postings as one employer. Cross-job company protection is not
possible for those unidentified employers. A job-board hostname is never used
as if it were the hiring company.

On first use, successful records with valid timestamps are automatically imported
from `linkedin_noneasy_applied.jsonl` and existing applied pipeline jobs. Imports
are idempotent and preserve the old file. Invalid/missing timestamps are not
replaced with today's date; invalid JSONL records are warned about and skipped.
Older records cannot provide a company cooldown when no employer identity was
originally saved or can be recovered. No historical Easy Apply submissions are
invented: only newly recorded successes and available legacy evidence are used.

Parallel workers atomically check and reserve a company using SQLite transactions.
A heartbeat renews the ten-minute reservation during long applications. Normal
failures release it; a crashed process's reservation expires. If saving a reported
success fails, the run stops and briefly retains its reservation to reduce the
risk of an immediate duplicate. Check the actual application status before
retrying after such an uncertain outcome. Database errors never silently disable
the protection. History matching, migration, and reservations make no LLM/API
requests.

### Search pagination (`linkedin-apply` and `linkedin-noneasy`)

Set `"max_scan_pages": 20` in `config/linkedin_apply.json` to scan up to 20
LinkedIn results pages. This must be a positive integer; when absent, Easy Apply
scans all available pages and Non-Easy Apply defaults to 3. It is independent of
`max_applications`. Non-Easy Apply's existing candidate safety cap of four times
`max_applications` also applies to that mode; neither limit guarantees that many
successful applications when jobs are skipped or fail.

Both application modes reuse the same pagination checks and support LinkedIn's
newer icon-only Next button. After clicking, they wait up to 10 seconds
for new job IDs rather than treating an unchanged or temporarily empty page as
successful pagination. The CLI prints the page limit, pages scanned, and scan stop
reason (such as `max_scan_pages_reached`, `no_next_page`, or
`pagination_no_new_results`). Repeated pages do not trigger duplicate applications.

### External job-board filter (`linkedin-noneasy` only)

Set `"jobboard_filter": true` in `config/linkedin_apply.json` to apply only when
the external application URL's hostname matches an entry in the project-root
`jobboards.json` (the current working directory, not `config/jobboards.json`).
JSON booleans are recommended; the strings `"true"` and `"false"` are also accepted.
Missing or `false` keeps the existing unrestricted external-site behavior.

The file groups hostname strings by job board, for example:

```json
{
  "ashby": ["jobs.ashbyhq.com"],
  "greenhouse": ["job-boards.greenhouse.io", "boards.greenhouse.io"],
  "personio": ["*.jobs.personio.de", "*-jobs.personio.de"]
}
```

Matching is case-insensitive and applies to the parsed hostname only, never text
in the URL path or query. Plain entries match exactly. An explicit `*` matches a
non-empty part of a **single** hostname label, so `*.jobs.personio.de` matches
`acme.jobs.personio.de`, not `jobs.personio.de` or `x.acme.jobs.personio.de`.
Do not include schemes, paths, or ports in the file.

The bot checks the actual landed URL after LinkedIn's Apply click and rechecks
it before browser-agent handoff, including after résumé preparation. Unmatched
sites are logged and skipped without filling forms or recording an application;
initially unmatched sites also skip résumé generation. The browser agent is
instructed to check the same allowlist before entering data, uploading, or
submitting after later redirects. This is application filtering, not a network
firewall; normal page assets and the configured verification mailbox are not
blocked. When enabled, a missing, malformed, or empty allowlist stops the run
before the browser opens, rather than silently disabling the filter.

### Configured answers on external applications

`linkedin-noneasy` passes the **complete** `profile` and `answers` objects to its
browser agent, including full-question keys, lists, nested mappings, booleans,
and zero experience. Screening overrides take precedence over normal mappings.
Only these applicant sections are included, not unrelated top-level credentials.

On Greenhouse, a bounded Playwright prefill first resolves known fields directly
from config, then applies the external-question defaults below. It fills contact details and clicks the real options in React Select
dropdowns, including asynchronous city suggestions; typing into a search box is
not treated as a committed answer. It preserves successful selections across
input re-renders and reports unresolved fields to the agent. Hidden required
mirror inputs, file uploads, and submission are not handled by this prefill.
The existing browser agent still completes and validates the application.

Use exact question keys in `answers` for employer-specific questions. Explicit
answers, including No/false/zero, override every fallback. For combined
work-authorization-without-sponsorship questions, the prefill
uses both `authorized_to_work` and `visa_sponsorship`, rather than mistakenly
answering the sponsorship question alone. Residence comes from `profile.city`,
not the advertised job location.

For `linkedin-noneasy`, unanswered permission, qualifications, skills, experience,
proficiency, and availability questions use the most favorable available choice.
Questions asking whether a visa/sponsorship is needed, whether there are unspent
convictions, or whether there is a client/partner/competitor employment conflict
use the negative/least adverse choice. Negation matters: authorization **without**
sponsorship is affirmative; confirming **no** convictions is affirmative.
If no suitable semantic choice exists, select a random enabled, non-placeholder
option and preserve it on retries. Unknown free text uses `a`, numbers use `1`,
and dates/email/URLs use compatible formats. These user-requested defaults are
logged as `fallback.*` assumptions, not verified applicant facts.

This policy is passed to every external browser agent, not only Greenhouse.
Unknown answers are no longer a permitted reason to skip a job. An agent that
still reports an unknown-answer failure gets a fresh same-application retry.
If it refuses again, the run stops explicitly instead of silently failing that
job and continuing. Actual broken controls, missing options, validation errors,
CAPTCHAs, and login/upload failures can still prevent completion; the policy
does not bypass those technical constraints or the jobboard allowlist.

### Screening-question fallback

`linkedin-apply` uses configured profile/answer values first. For an unmatched
choice question it tries **Yes**, then **Ja**, and otherwise randomly selects
an enabled, non-placeholder option. A valid selection is preserved on step
retries. This applies to radio groups, checkbox option groups (including unnamed
SDUI inputs with empty labels and sibling option text), native selects, and custom
combobox/listbox controls (including portalled dropdown menus). An unanswered
checkbox option group gets one choice; independent required consent boxes are
handled separately and follow-company boxes stay unchecked. Free-text fields receive `"a"`
and numeric/count fields receive `"1"`.

If a text/number field reports "Invalid input", "Invalid type", or equivalent
number/type validation, Easy Apply retries only that field: an applicable
configured answer first, then `"1"` (number) and `"a"` (text), excluding values
already rejected and formats blocked by the input's native constraints. Attempts
are tracked per question across form re-renders, and corrected values are kept
on subsequent passes. Review/Next and final Submit validation both use this
fallback; exhausted choices stop that application instead of repeating forever.

Selections are verified with short timeouts. No LLM or offline classifier is used.
File uploads, saved résumé selections, and hidden inputs are kept separate from
text answers. Add explicit answers for any personal facts you do not want the
random fallback to choose, including demographic questions.

Open education and work-experience editors in LinkedIn Easy Apply are deleted
using their own Delete control before any fields are filled. Closed/saved entries
and unrelated questions are left alone. The bot verifies deletion, handles an
entry-specific confirmation dialog if present, and stops that application if the
entry cannot be deleted; it does not fill or save the open editor as a fallback.
This also applies during required-field validation retries.

Every document upload request receives the selected job-specific CV again,
including transcripts, cover letters, certificates, and unfamiliar attachments.
Easy Apply supports multiple direct file inputs and SDUI file-picker buttons such
as "Upload Transcript", and avoids uploading completed fields again on retries.
The non-Easy-Apply agent receives the same document-upload instructions. This reuses
the same file, does not generate other documents, and does not replace the separate
résumé upload. File-type restrictions are respected: image-only fields still use
`photo_path` when configured, and a required incompatible upload reports a failure.

### Per-job intelligent résumés

Both `linkedin-apply` and `linkedin-noneasy` support `"intelligent_apply": true` in
`config/linkedin_apply.json`. After the existing job filters pass, they save the
description, query the configured non-Easy-Apply API, replace just the two NOMOS
bullets, compile and verify a UK/EU PDF, publish it to `data/AndrewBodzsarCV.pdf`,
and use that exact résumé for the application. Prompt/source files are reread per
job; cache-aware requests and cache/cost telemetry are included.

Set `browser_model` and `bulletpoint_model` in the same JSON file to select the
external browser agent and résumé generation models independently. Nonempty
config values override environment model defaults. Easy Apply's browser uses
Playwright directly; `browser_model` applies to `linkedin-noneasy`.

See [Intelligent Apply](INTELLIGENT_APPLY.md) for configuration, artifacts, safety
checks, and tests. `--dry-run` still calls the API and overwrites the published CV;
it stops before final application submission. `linkedin-apply --setup` bypasses
résumé preparation.

### Scrape LinkedIn job descriptions

```bash
applypilot linkedin-scrape --config config/linkedin_apply.json
applypilot linkedin-scrape -c config/linkedin_apply.json --headless
applypilot linkedin-scrape -c config/linkedin_apply.json --setup  # Manual login, then Ctrl+C
```

Uses the same saved LinkedIn browser session and browser settings as `linkedin-apply`.
Close any other command using that session before starting. `--exactlocation` is also supported.
No resume, application answers, or LLM credentials are needed. A minimal config is:

```json
{
  "job_title": "Data AI Engineer",
  "location": "Berlin, Germany",
  "only_english": true,
  "max_applications": 20
}
```

Searches with `job_title`, includes both Easy Apply and external jobs, and requires all
title words to appear as whole words in the displayed title (case insensitive, any order).
For example, `Data AI Engineer` matches `Data & AI Engineer (all genders)`, and `AI` does
not match `Retail`. The separate application setting `title_keyword` is not used.
`only_english` uses the existing English/German description heuristic; when enabled,
German and unclear descriptions are skipped. `max_applications` limits **saved jobs**.
Failed extractions and jobs rejected by the title or language filter do not consume the
limit. For example, with a limit of 5, the scraper keeps searching until it saves 5 jobs
or exhausts the available results, even if this requires more than 5 detail-page attempts.
Duplicate jobs and results whose titles do not match are skipped before opening a detail page.
The spelling `max_application` is also accepted; conflicting values are rejected.
The default limit is 20, and zero writes an empty JSON array without opening a browser.

Results are a JSON array in `scrapedjobs/<job_title>_<YYYY-MM-DD_HH-MM-SS-microseconds>.json`
relative to the current directory. Filename-unsafe characters are replaced with `_`;
timestamps keep separate runs from overwriting one another. Each entry includes the job
ID, title, URL, company and company URL, location, posting/applicant text, workplace and
employment types, badges, application URL, full description, company description/details,
detected language, and scrape timestamp. `metadata` and `job_card_text` preserve additional
card information; fields unavailable in the page are empty strings/lists.

The scraper follows scrolling and pagination until it reaches the limit or exhausts
available results. LinkedIn may expose fewer matching jobs than requested. Results are
saved after each accepted job, including before interruption. Progress logs report attempts,
saves, failures, and why a job was skipped. A run with extraction failures and no saved jobs
exits with an error. `delay` controls the pause before each detail request.

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/sites.yaml` - Direct career sites (30+), blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes 48 Workday employer portals (configurable in `employers.yaml`). Hits 30 direct career sites with custom extractors. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. 9-10 = strong match, 7-8 = good, 5-6 = moderate, 1-4 = skip. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
Claude Code launches a Chrome instance, navigates to each application page, detects the form type, fills personal information and work history, uploads the tailored resume and cover letter, answers screening questions with AI, and submits. A live dashboard shows progress in real-time.

The Playwright MCP server is configured automatically at runtime per worker. No manual MCP setup needed.

```bash
# Utility modes (no Chrome/Claude needed)
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset all failed jobs for retry
applypilot apply --gen --url URL       # generate prompt file for manual debugging
```

---

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Parallel discovery/enrichment
applypilot run --stream                 # Concurrent stages (streaming mode)
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot apply                        # Launch auto-apply
applypilot apply --workers 3            # Parallel browser workers
applypilot apply --dry-run              # Fill forms without submitting
applypilot apply --continuous           # Run forever, polling for new jobs
applypilot apply --headless             # Headless browser mode
applypilot apply --url URL              # Apply to a specific job
applypilot linkedin-apply --exactlocation  # Tick the exact LinkedIn Location filter
applypilot status                       # Pipeline statistics
applypilot dashboard                    # Open HTML results dashboard
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
