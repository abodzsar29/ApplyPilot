# Non-Easy-Apply Planning Notes

## Question 1

**User:** don't make any modificaitons on the repo, but give me a plan on how I could apply to non-easy apply jobs where the DOM and HTML tree changes from website to website, without knowing beforehand what third party website each job is using, because it would be impossible to get the HTML DOM of each website for each job?

**Answer:** You should treat this as a runtime perception problem, not a predefined-selector problem.

The repo already has the right pattern on the discovery side: `smartextract.py` collects page signals first, then chooses a strategy. For non-Easy-Apply jobs, I would mirror that for application flows instead of trying to know every third-party DOM in advance.

### Core approach

Build a 4-stage apply pipeline:

1. `Classify the outbound apply target`
   From the `application_url` gathered in `detail.py`, navigate and classify:
   - Known ATS: Workday, Greenhouse, Lever, Ashby, Taleo, iCIMS, SmartRecruiters, etc.
   - Employer-hosted custom form
   - SSO / CAPTCHA / blocked flow
   - Multi-step external flow with account creation

   Classification should rely on runtime signals:
   - final hostname
   - URL patterns
   - page title
   - form presence
   - labels like `Apply`, `Continue`, `Upload Resume`, `Create account`
   - hidden platform markers like `__NEXT_DATA__`, JSON-LD, form action URLs, vendor JS names

2. `Route by capability level`
   Don’t make “fully automated” your only mode. Use levels:
   - Level 0: skip and mark manual
   - Level 1: open target, prefill obvious fields, stop for user review
   - Level 2: fill standard fields and upload files
   - Level 3: full multi-step submit when confidence is high

   This matters because the hard problem is not finding inputs. It is knowing when submission is safe.

3. `Use semantic field extraction, not DOM-specific selectors`
   For any unknown form, inspect the live page and build a normalized field model:
   - field type: text, email, phone, textarea, select, checkbox, radio, file
   - label text
   - nearby helper text
   - required/optional
   - candidate semantic meaning: first name, last name, work authorization, salary expectation, LinkedIn URL, portfolio, etc.

   Then map that normalized field model to your profile/answers config. Your current LinkedIn form logic in `simple_apply.py` already does a primitive version of this by label matching. Generalize that idea across sites.

4. `Keep a human-in-the-loop fallback`
   Unknown sites will never be 100% solvable ahead of time. The system should pause when:
   - confidence is low
   - required field meaning is ambiguous
   - account creation is required
   - CAPTCHA appears
   - assessment/test appears
   - final submit button is detected but validation state is unclear

   In those cases, the bot should leave the browser on the exact step, show a compact summary of unresolved fields, and let you finish manually.

### Practical design

I would implement this mentally as three layers:

- `Platform adapters`
  For known ATS families, build one adapter per family. You do not need one per employer, because many employers share the same ATS shell.

- `Generic form engine`
  This is the fallback for unknown/custom sites:
  - enumerate visible controls
  - infer labels from `label[for]`, `aria-label`, `placeholder`, nearby text, fieldset legends
  - infer step buttons: `Next`, `Continue`, `Review`, `Submit`
  - fill only high-confidence fields
  - after each click, re-scan the page

- `Decision policy`
  A rules/confidence layer that decides:
  - continue automatically
  - ask user to review
  - skip as manual
  - blacklist this domain/flow for future runs

### How to handle “impossible to know the HTML beforehand”

You do not need the HTML beforehand if you collect it at runtime from the page you are already on.

The trick is:
- discover the target URL first
- load it in Playwright
- extract a semantic snapshot of the current step
- decide strategy from that snapshot
- fill one step at a time
- rescan after each navigation or modal transition

So the system learns the flow while executing it. That is the only scalable approach.

### What I would prioritize first

1. Build hostname-based ATS detection for the biggest vendors.
2. Add a generic “form snapshot” schema for any page.
3. Add conservative autofill for standard fields only.
4. Add confidence-based pause-before-submit.
5. Persist per-domain outcomes:
   - success
   - manual-only
   - blocked by CAPTCHA
   - requires account
   - unsupported

That gives you useful coverage quickly without pretending you can fully automate arbitrary sites.

### Important constraint

A fully general autonomous submitter across arbitrary third-party job sites is not realistically reliable enough without human review. The robust version is:
- automate discovery
- automate routing/classification
- automate high-confidence prefills
- automate known ATS families
- hand off the last mile when confidence drops

If you want, I can next turn this into a concrete implementation plan for this repo, mapped to modules like `pipeline.py`, `detail.py`, and the current apply modules, without editing anything.

## Question 2

**User:** So what would I have to provide you, step by step?

**Answer:** You would need to provide it in layers, from static data first to live flow examples later.

### Step 1: A canonical applicant profile

Give the bot one normalized source of truth for everything commonly asked in applications:
- full name
- email
- phone
- location
- LinkedIn URL
- portfolio/GitHub URL
- work authorization
- visa sponsorship answer
- years of experience
- salary expectations
- start date / notice period
- education
- resume file path
- cover letter template or generation rules

This should be structured data, not just free text.

### Step 2: A reusable answer bank

Provide standard answers for recurring questions:
- `Are you authorized to work...`
- `Will you require sponsorship...`
- `Are you willing to relocate...`
- `Do you have X years of experience...`
- demographic/self-ID preferences
- remote / hybrid / onsite preferences

This is what the generic form engine will map into unknown forms.

### Step 3: Your automation policy

Define what the bot is allowed to do:
- auto-submit or stop before final submit
- create accounts or never create accounts
- handle demographic forms or skip them
- skip salary questions or answer them
- skip CAPTCHAs or pause for you
- maximum number of steps before handing off
- blacklist certain ATS/vendors/domains

Without this, the bot can fill fields but not make safe decisions.

### Step 4: A target-site classifier seed

Give an initial list of known platforms and policies for each:
- `greenhouse.io`
- `lever.co`
- `myworkdayjobs.com`
- `ashbyhq.com`
- `smartrecruiters.com`
- `icims.com`
- `taleo.net`

For each one:
- supported?
- manual only?
- pause before submit?
- account creation allowed?

This list starts small and improves over time.

### Step 5: A few real examples per major ATS

You do not need every website. You need sample flows from the major ATS families.
For each sample, provide:
- the application URL
- whether login was required
- what steps appeared
- what fields were asked
- where it broke
- screenshot or DOM snippet if possible

Even 3 to 5 examples per ATS is enough to design adapters.

### Step 6: A generic field-mapping dataset

For unknown sites, the system needs semantic matching. You should provide examples of how labels map to your canonical profile, such as:
- `Given Name` -> `first_name`
- `Mobile Number` -> `phone`
- `Portfolio / Website` -> `portfolio_url`
- `Legally authorized to work` -> `authorized_to_work`

This can start as a YAML/JSON dictionary and expand from logs.

### Step 7: Failure samples

Provide examples of hard cases:
- CAPTCHA
- SSO redirects
- account creation
- resume parsing popups
- required essay questions
- assessment/test pages
- file upload failures

These are critical because the system needs routing rules for when not to proceed.

### Step 8: Browser-session assumptions

You need to decide whether the bot can rely on:
- an already logged-in browser profile
- stored cookies
- manual login before starting
- persistent Playwright context

For third-party sites, session persistence matters a lot.

### Step 9: Human handoff format

Define how you want the bot to stop and hand work back to you:
- leave browser open on unresolved step
- print unresolved required fields
- ask for one answer interactively
- mark the job as `manual_required`
- save a screenshot + page summary

This is essential for unknown flows.

### Step 10: Feedback loop data

After each run, store:
- domain
- platform guess
- fields recognized
- fields filled
- reason for stop/failure
- whether manual completion succeeded

That is how the system becomes better without pre-knowing every DOM.

### Minimum viable set to get started

If you want the shortest path, provide only:
1. applicant profile
2. reusable answer bank
3. automation policy
4. 10-20 real application URLs across a few ATS families
5. notes/screenshots for where each flow breaks

That is enough to build a first usable non-Easy-Apply strategy.

If you want, I can next turn this into a concrete checklist template you could fill in for this repo.
