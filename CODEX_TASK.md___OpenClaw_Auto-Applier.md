# CODEX_TASK.md — OpenClaw Auto-Applier

## Context

You're building the **auto-applier module** for OpenClaw, an autonomous job application system. This module will be orchestrated by an AI assistant (Hex) running on the same EC2 instance.

### How It Will Be Used

1. **Hex monitors job boards** via cron jobs (every 15 min)
2. When a new job is found, Hex calls this module:
   ```bash
   python -m openclaw.applier "https://job-url" --company "Stripe" --role "SWE Intern"
   ```
3. The applier fills out the application form using the user's profile
4. If successful → logs result, Hex notifies user
5. If CAPTCHA/blocker → flags it, Hex sends URL to user for manual completion
6. Top-tier companies (FAANG, etc.) are flagged for human review before submit

### System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         EC2 Instance                         │
│                                                              │
│  ┌──────────┐    ┌─────────────┐    ┌───────────────────┐   │
│  │   Hex    │───▶│ Job Monitor │───▶│  Auto-Applier     │   │
│  │ (Claude) │    │  (cron)     │    │  (this module)    │   │
│  └──────────┘    └─────────────┘    └───────────────────┘   │
│       │                                      │               │
│       │         ┌─────────────┐              │               │
│       └────────▶│   Notion    │◀─────────────┘               │
│                 │  Dashboard  │  (logs applications)         │
│                 └─────────────┘                              │
└─────────────────────────────────────────────────────────────┘
                           │
                           │ CAPTCHA notification
                           ▼
                  ┌─────────────────┐
                  │  User's Mac     │
                  │ (isolated Chrome)│
                  └─────────────────┘
```

---

## What To Build

### Module Structure

```
/home/ubuntu/clawd/openclaw/
├── __init__.py
├── applier.py          # Main entry point
├── ats/
│   ├── __init__.py
│   ├── base.py         # Base ATS handler
│   ├── greenhouse.py   # Greenhouse-specific logic
│   ├── lever.py        # Lever-specific logic
│   ├── ashby.py        # Ashby-specific logic
│   ├── workday.py      # Workday-specific logic
│   └── generic.py      # Fallback for unknown ATS
├── captcha/
│   ├── __init__.py
│   ├── detector.py     # Detect CAPTCHA presence
│   └── solver.py       # 2captcha integration (optional)
├── profile.py          # Load user profile/resume
├── questions.py        # LLM-powered custom question answering
└── utils.py            # Screenshots, logging, etc.
```

### Core Requirements

#### 1. ATS Detection & Handling

Detect ATS from URL patterns:
- `greenhouse.io`, `boards.greenhouse.io` → Greenhouse
- `lever.co`, `jobs.lever.co` → Lever
- `ashbyhq.com`, `jobs.ashbyhq.com` → Ashby
- `myworkdayjobs.com` → Workday
- Unknown → Generic (use Skyvern AI navigation)

Each ATS handler should know:
- How to find/click the Apply button
- Field selectors for that platform
- How to upload resume
- How to handle multi-page forms

#### 2. Form Filling

**Standard fields to fill:**
| Field | Value Source |
|-------|--------------|
| First Name | `profile.json → identity.name.split()[0]` |
| Last Name | `profile.json → identity.name.split()[1:]` |
| Email | `profile.json → identity.email` |
| Phone | `resume.json → contact.phone` |
| LinkedIn | `profile.json → identity.linkedin` |
| GitHub | `profile.json → identity.github` |
| School | `resume.json → education[0].institution` |
| Degree | `resume.json → education[0].degree` |
| GPA | `resume.json → education[0].gpa` |
| Graduation | `resume.json → education[0].graduationDate` |
| Resume | Upload from `memory/resume.pdf` |

**Standard answers:**
| Question Pattern | Answer |
|-----------------|--------|
| Work authorization | Yes |
| Require sponsorship | No |
| How did you hear | Online Job Board |
| Willing to relocate | Yes |
| Start date | Summer 2026 |
| Salary expectations | Open / Negotiable |

#### 3. Custom Questions (LLM-Powered)

For questions that don't match standard patterns (e.g., "Why do you want to work at X?"):

1. Extract the question text
2. Call Claude API with context:
   - The question
   - Company name and role
   - User's profile summary
   - Keep response concise (2-3 sentences for short fields, 1 paragraph for essays)
3. Fill the response

Use AWS Bedrock (credentials via instance role):
```python
import boto3
client = boto3.client('bedrock-runtime', region_name='us-east-1')
# Model: anthropic.claude-sonnet-4-5-20250929-v1:0 (higher quality, still cost-effective)
```

#### 4. CAPTCHA Handling

**Detection:**
- Check for reCAPTCHA iframe
- Check for hCaptcha
- Check for "verify you're human" text
- Check for Cloudflare challenge

**When detected:**
```python
return {
    "status": "captcha_blocked",
    "captcha_type": "recaptcha_v2",
    "url": current_url,
    "screenshot": "path/to/screenshot.png",
    "message": "CAPTCHA detected - manual intervention needed"
}
```

**Optional 2captcha integration:**
- If `TWOCAPTCHA_API_KEY` env var is set, attempt auto-solve
- Otherwise, flag for manual

#### 5. Top-Tier Company Handling

These companies should be flagged for human review (don't auto-submit):
```python
TOP_TIER = [
    "Google", "Meta", "Apple", "Amazon", "Microsoft", "Netflix",
    "OpenAI", "Anthropic", "DeepMind", "Stripe", "Databricks",
    "Scale AI", "Anduril", "Palantir", "SpaceX", "Tesla",
    "Jane Street", "Citadel", "Two Sigma", "DE Shaw", "HRT"
]
```

For these:
1. Fill the form completely
2. Take screenshot
3. Return `status: "needs_review"` instead of submitting
4. Hex will notify user to review and submit manually

#### 6. Output Format

```python
# Success
{
    "status": "success",
    "company": "Gusto",
    "role": "Software Engineer Intern",
    "url": "https://...",
    "application_id": "app_20260208_gusto_123",
    "screenshots": ["01-job-page.png", "02-form-filled.png", "03-submitted.png"],
    "fields_filled": 12,
    "custom_questions": 2,
    "submitted": True,
    "timestamp": "2026-02-08T08:30:00Z"
}

# Needs Review (top-tier)
{
    "status": "needs_review",
    "company": "Stripe",
    "reason": "Top-tier company - manual review required",
    "url": "https://...",
    "screenshot": "form-ready-to-submit.png"
}

# CAPTCHA Blocked
{
    "status": "captcha_blocked",
    "company": "...",
    "captcha_type": "recaptcha_v2",
    "url": "https://...",
    "screenshot": "captcha-detected.png"
}

# Error
{
    "status": "error",
    "company": "...",
    "error": "Form field not found: email",
    "screenshot": "error-state.png"
}
```

---

## Technical Stack

### Use Skyvern SDK

Skyvern provides AI-powered browser automation on top of Playwright:

```python
from skyvern import Skyvern

skyvern = Skyvern.local()
browser = await skyvern.launch_browser()
page = await browser.get_working_page()

# AI-powered actions
await page.goto(job_url)
await page.click(prompt="Click the Apply button")
await page.fill(prompt="Email address field", value="user@email.com")
await page.upload_file(prompt="Resume upload", files="resume.pdf")
```

**Why Skyvern:**
- Handles unknown page layouts via vision AI
- Natural language selectors as fallback
- Built-in retry logic

**Installation:**
```bash
pip install skyvern playwright
playwright install chromium
```

### File Locations

```
/home/ubuntu/clawd/
├── memory/
│   ├── profile.json      # User profile (READ THIS)
│   ├── resume.json       # Structured resume data
│   ├── resume.pdf        # PDF to upload
│   └── applications/     # Output directory for screenshots/logs
│       └── YYYYMMDD_HHMMSS_companyname/
│           ├── result.json
│           ├── 01-job-page.png
│           ├── 02-form-filled.png
│           └── ...
├── openclaw/             # YOUR MODULE GOES HERE
└── CODEX_TASK.md         # This file
```

### Environment

- **OS:** Ubuntu 22.04 on EC2
- **Python:** 3.12 (venv at `/home/ubuntu/clawd/.venv`)
- **Browser:** Chromium (headless)
- **AWS:** Instance role provides Bedrock access (no keys needed)

---

## CLI Interface

```bash
# Basic usage
python -m openclaw.applier "https://jobs.lever.co/company/job-id" \
    --company "Company Name" \
    --role "Software Engineer Intern"

# Dry run (don't submit)
python -m openclaw.applier "https://..." --company "..." --role "..." --dry-run

# Force submit even for top-tier
python -m openclaw.applier "https://..." --company "..." --role "..." --force

# Verbose logging
python -m openclaw.applier "https://..." --company "..." --role "..." -v
```

---

## Testing

Test against these real job URLs (use --dry-run):

```bash
# Lever
python -m openclaw.applier "https://jobs.lever.co/palantir/e27af7ab-41fc-40c9-b31d-02c6cb1c505c" \
    --company "Palantir" --role "SWE Intern" --dry-run

# Ashby
python -m openclaw.applier "https://jobs.ashbyhq.com/n1/8db1135d-40bc-4e6c-ac64-a5e11fc8d1a4" \
    --company "N1" --role "Software Engineer Intern" --dry-run

# Greenhouse
python -m openclaw.applier "https://job-boards.greenhouse.io/gusto/jobs/6442770002" \
    --company "Gusto" --role "Software Engineer" --dry-run
```

---

## Success Criteria

1. ✅ Successfully fills and screenshots a Lever application (dry-run)
2. ✅ Successfully fills and screenshots a Greenhouse application (dry-run)
3. ✅ Successfully fills and screenshots an Ashby application (dry-run)
4. ✅ Correctly detects and reports CAPTCHA when present
5. ✅ Flags Palantir as "needs_review" (top-tier)
6. ✅ Generates reasonable answers for custom questions via LLM
7. ✅ All output saved to `memory/applications/` with screenshots
8. ✅ Clean JSON output for Hex to parse

---

## Notes

- **Don't over-engineer.** Start with Lever + Greenhouse (most common), add others as needed.
- **Screenshots are crucial.** Take one before every major action.
- **Fail gracefully.** If something breaks, return error status with screenshot, don't crash.
- **Be fast.** Applications within 2 hours of posting have highest success rate.

Good luck! 🚀
