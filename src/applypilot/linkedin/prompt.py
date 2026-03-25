"""Deterministic LinkedIn Easy Apply prompt builder."""

from __future__ import annotations

import json


def build_prompt(job_url: str, config: dict, resume_path: str = None, dry_run: bool = False) -> str:
    """Build a deterministic LinkedIn Easy Apply prompt.

    Args:
        job_url: LinkedIn job URL to apply to
        config: Config dict with profile, answers, etc.
        resume_path: Override resume path (else use config["resume_path"])
        dry_run: If True, stop before submitting (output RESULT:DRY_RUN instead)

    Returns:
        Prompt string for Claude CLI.
    """
    profile = config.get("profile", {})
    answers = config.get("answers", {})
    resume_path = resume_path or config.get("resume_path", "resume.pdf")
    years_exp = answers.get("years_experience", {})

    # Build field mapping rules for Claude to reference
    field_mapping = _build_field_mapping_reference(profile, answers, years_exp)

    # Convert config to JSON for embedding in prompt
    config_json = json.dumps({
        "profile": profile,
        "answers": {k: v for k, v in answers.items() if k != "years_experience"},
        "years_experience": years_exp,
    }, indent=2)

    prompt = f"""You are an autonomous agent controlling a browser to apply for LinkedIn jobs.

CRITICAL RULES:
1. ONLY apply to jobs with an "Easy Apply" button. If Easy Apply is not present, output RESULT:SKIPPED:no_easy_apply
2. Do NOT generate any text, cover letters, or creative writing.
3. Do NOT fill in free-text answer fields (open-ended questions). If required, output RESULT:SKIPPED:free_text_required
4. Use ONLY the provided profile data and pre-defined answers.
5. Never guess or assume answers. If unsure, skip the job.
6. Do NOT apply to jobs requiring sponsorship if user is not eligible.

== JOB ==
URL: {job_url}

== FILES ==
Resume path: {resume_path}

== USER PROFILE ==
{config_json}

== FIELD MAPPING RULES ==
{field_mapping}

== TASK ==
1. Open the job page at {job_url}
2. Wait for page to load
3. Take a screenshot to see the page
4. Look for "Easy Apply" button
5. If no Easy Apply button found:
   → Output: RESULT:SKIPPED:no_easy_apply
6. Click "Easy Apply" button
7. A modal form will appear (may be multi-step)
8. Fill the form following these rules:

   A. For each field, identify the field label or question text
   B. Check if the field is in the FIELD MAPPING RULES above
   C. If yes, use the mapped value from the config
   D. If no, check if it's a required field
   E. If field is required and not in config, output RESULT:SKIPPED:unknown_required_field:{{field_name}}
   F. If field is optional and not in config, leave it blank
   G. Do NOT write any text. All answers must come from config.

9. When uploading resume:
   → Use the file at: {resume_path}

10. If multi-step form:
    → Fill all steps following the same rules
    → Click "Next" between steps
    → On final "Review" screen:
         - Check that all answers are correct
         - UNCHECK "Follow company" if it's checked
         - {"STOP - do NOT submit (dry run mode). Output: RESULT:DRY_RUN" if dry_run else "Click Submit button"}

11. After submission (if not dry_run):
    → Wait 2 seconds for confirmation
    → Look for "Application sent" or "Thank you" message
    → Output: RESULT:APPLIED

== FAILURE CONDITIONS ==
If ANY of these occur, skip the job immediately:
- Form requires a cover letter field → RESULT:SKIPPED:cover_letter_required
- Form requires free-text answer → RESULT:SKIPPED:free_text_required
- Form requires essay/open question → RESULT:SKIPPED:open_question_required
- Form requires screening questions beyond the pre-defined answers → RESULT:SKIPPED:unknown_screening_questions
- Resume upload fails → RESULT:FAILED:resume_upload_error
- Page loads as a third-party ATS (Workday, Taleo, BambooHR, etc.) → RESULT:SKIPPED:external_ats
- You are redirected to an external website → RESULT:SKIPPED:external_redirect
- The job has already been applied to (error message) → RESULT:SKIPPED:already_applied

== SUCCESS OUTPUT ==
After successfully completing the application, output exactly:
RESULT:APPLIED

== ERROR OUTPUT FORMAT ==
If something goes wrong, output exactly:
RESULT:FAILED:brief_reason

For example:
RESULT:FAILED:form_field_missing
RESULT:FAILED:could_not_find_submit_button
RESULT:FAILED:browser_error

== SKIP OUTPUT FORMAT ==
If you skip for any reason, output exactly:
RESULT:SKIPPED:reason

For example:
RESULT:SKIPPED:no_easy_apply
RESULT:SKIPPED:cover_letter_required
RESULT:SKIPPED:free_text_required

== BROWSER TIPS ==
- Use browser_screenshot to see the current state
- Use browser_click to click buttons
- Use browser_fill to fill text fields
- Use browser_select to select from dropdowns
- Scroll within the modal if needed
- If a field has a dropdown, select the value that matches the config
- Some fields may have radio buttons instead of text inputs - select the matching option

== WHEN TO GIVE UP ==
If you've tried 3 times to find or fill a field and failed, skip the job.
If you get a browser error or timeout, output RESULT:FAILED:browser_error
If the form structure is unclear or doesn't match expectations, skip the job.

START NOW. Navigate to the job page and begin the application process.
"""

    return prompt


def _build_field_mapping_reference(profile: dict, answers: dict, years_exp: dict) -> str:
    """Build a text reference for field mapping rules."""
    return f"""
Use these exact mappings when filling form fields:

TEXT FIELDS:
- "First Name", "First name", "first_name" → {profile.get('first_name', 'UNKNOWN')}
- "Last Name", "Last name", "last_name" → {profile.get('last_name', 'UNKNOWN')}
- "Email", "Email address", "email" → {profile.get('email', 'UNKNOWN')}
- "Phone", "Phone number", "mobile", "contact number" → {profile.get('phone_number', 'UNKNOWN')}
- "City", "Location", "Current location", "based in" → {profile.get('city', 'UNKNOWN')}
- "Country code" or "Country" → Use prefix: {profile.get('phone_country_code', '+1')}

PREDEFINED ANSWERS (select the matching option from dropdown/radio):
- Visa sponsorship / Visa required → {answers.get('visa_sponsorship', 'UNKNOWN')}
- Authorized to work / Work authorization → {answers.get('authorized_to_work', 'UNKNOWN')}
- Onsite / In-office / Office location → {answers.get('onsite', 'UNKNOWN')}
- English level / Language proficiency / English proficiency → {answers.get('english_level', 'UNKNOWN')}

YEARS OF EXPERIENCE:
For questions like "Years of experience in X":
- C++: {years_exp.get('C++', 'UNKNOWN')}
- Python: {years_exp.get('Python', 'UNKNOWN')}
- Java: {years_exp.get('Java', 'UNKNOWN')}
- JavaScript: {years_exp.get('JavaScript', 'UNKNOWN')}
- Go: {years_exp.get('Go', 'UNKNOWN')}
- Rust: {years_exp.get('Rust', 'UNKNOWN')}

MATCHING LOGIC FOR SKILLS:
If a question asks "How many years of experience do you have in [SKILL]?":
1. Extract the skill name from the question
2. Look for an exact match in the years_experience dict
3. If found, use that value
4. If not found, skip the job (RESULT:SKIPPED:unknown_skill_experience)

DROPDOWN SELECTION RULES:
- For "Yes/No" questions: select the value from the config that matches (Yes/No)
- For "Sponsor/No sponsor" questions: use answers.visa_sponsorship value
- For "Remote/Onsite/Hybrid" questions: use answers.onsite value
- For language level dropdowns: find the option closest to answers.english_level
  (e.g., if config says "Native or Bilingual", select that exact option, or "Bilingual" if exact match missing)

COMMON FIELD PATTERNS TO RECOGNIZE:
- "I am" + options → select based on work authorization
- "Do you require" + sponsorship options → use visa_sponsorship
- "Willing to relocate" → check if matches answers
- "Can start" → dates (if not in config, skip)
- "Salary expectation" → if not in config, skip the job
- "Years in industry" → if not in config, skip the job

ALWAYS SKIP IF:
- You don't have the answer in the config
- The field asks for something not in this mapping
- The question is open-ended or requires creative writing
"""
