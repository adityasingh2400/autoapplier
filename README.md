# OpenClaw Auto-Applier

Auto-applier module for job applications using ATS-specific handlers (Lever, Greenhouse, Ashby, Workday) with a generic fallback.

## Features

- ATS detection from URL and platform-specific form handling
- Profile-based standard field filling from `profile.json` and `resume.json`
- Resume upload from `resume.pdf`
- Custom question answering via AWS Bedrock Claude (with fallback responses)
- CAPTCHA detection + optional 2captcha integration
- Top-tier company review gate (`needs_review`) unless `--force`
- Screenshot + JSON result artifacts under `memory/applications/...`
- SimplifyJobs Summer 2026 source integration for high-volume job intake

## Install

```bash
pip install playwright boto3
playwright install chromium
```

## Expected Memory Layout

Create a **local** memory folder (e.g. `real_memory/` or `test_memory/`) with:

```text
real_memory/
├── profile.json    # Copy from profile.example.json and fill in your details
├── resume.json
├── resume.pdf
└── applications/   # Created automatically; screenshots and result JSON
```

**Version control & privacy:** `real_memory/`, `test_memory/`, `.env`, and credentials are in `.gitignore`. Do not commit them. Use `profile.example.json` as a template; keep your real `profile.json` and resume files only in your local memory folder. See the section below for pushing to GitHub safely.

## Answer Bank (Highly Recommended)

To make form-filling extremely consistent across ATSs, add an **answer bank** to `profile.json`.

- The applier uses it for both "standard questions" and many unlabeled fields (it matches against labels/aria/placeholder/name).
- Matching is **best-match-wins**:
  - Regex patterns (`re:<pattern>`) are weighted highest when they match.
  - Otherwise, longer/more-specific patterns win over generic ones.
- Use `__HUMAN__` (or `__ASK__` / `__SKIP__`) to force human-in-loop for sensitive/ambiguous fields.
- You can reference standard fields via placeholders like `{email}`, `{phone}`, `{linkedin}`, `{github}`.
- Placeholders also support `{company}`, `{role}`, `{first_name}`, `{last_name}`, `{school}`, `{degree}`, `{gpa}`, `{graduation}`, `{how_heard}`.

Example (see `profile.example.json` for a full template):

```json
{
  "identity": {
    "name": "Your Name",
    "email": "you@example.com",
    "linkedin": "https://linkedin.com/in/yourprofile",
    "github": "https://github.com/yourusername"
  },
  "applicationDefaults": {
    "questionBank": [
      { "patterns": ["how did you hear", "where did you hear"], "answer": "{how_heard}" },
      { "patterns": ["linkedin"], "answer": "{linkedin}" },
      { "patterns": ["github"], "answer": "{github}" },
      { "patterns": ["website", "portfolio"], "answer": "{github}" },
      { "patterns": ["work authorization", "authorized to work"], "answer": "__HUMAN__" },
      { "patterns": ["require sponsorship", "visa sponsorship"], "answer": "__HUMAN__" },
      { "patterns": ["citizenship", "security clearance"], "answer": "__HUMAN__" }
    ]
  }
}
```

## CLI

Direct URL mode:

```bash
python -m openclaw.applier "https://jobs.lever.co/company/job-id" \
  --company "Company Name" \
  --role "Software Engineer Intern" \
  --dry-run
```

## Gmail OAuth (For Email Verification Automation)

If a site requires an email verification code/link during signup, OpenClaw can optionally try to read the newest
verification email from your Gmail inbox and complete the step automatically.

1. Create a Google OAuth client (Desktop app recommended) and download the client secret JSON.
2. Put it at `~/.openclaw/gmail_client_secret.json` (or set `OPENCLAW_GMAIL_CLIENT_SECRET_PATH`).
3. Run the one-time OAuth setup for each mailbox you want OpenClaw to read:

```bash
python3 -m openclaw.gmail_oauth --client-secret ~/.openclaw/gmail_client_secret.json
# Repeat and pick the other Google account in the browser if you want both mailboxes configured.
```

The applier writes artifacts to `memory/applications/...` (screenshots, result JSON) and prints a **single JSON object to stdout**
so orchestrators can parse it. Key fields:

- `status`: `success` / `needs_review` / `captcha_blocked` / `error` / ...
- `pr_url`: compatibility alias for the “result URL” (same as `final_url`)
- `screenshot_paths`: absolute paths to the captured screenshots
- `output_dir`: absolute artifact directory for the run
- `handoff`: when `status != success`, OpenClaw also writes `handoff.json` in `output_dir` containing
  missing required fields and best-effort suggested answers (useful for handing remaining work to a slower agent).

Source mode (SimplifyJobs Summer2026 repo):

```bash
# List matching roles without applying
python -m openclaw.applier --source simplify --list-source-jobs \
  --category "software engineering" --max-jobs 20

# Apply in batch from source
python -m openclaw.applier --source simplify \
  --category "software engineering" \
  --role-keyword "intern" \
  --max-age-hours 48 \
  --max-jobs 10 \
  --dry-run
```

Harvest mode (build your answer bank by scanning real application forms):

```bash
python -m openclaw.applier --source simplify \
  --harvest-answer-bank \
  --category "software engineering" \
  --role-keyword "intern" \
  --max-jobs 25 \
  --max-form-pages 2 \
  --memory-root /path/to/memory
```

Useful flags:

- `--force`: allow auto-submit for top-tier companies
- `--exclude-unknown-ats`: source mode keeps only known ATS URLs
- `--memory-root /path/to/memory`: override default memory path
- `-v`: verbose logging
- `--human-in-loop`: pauses for manual steps (CAPTCHA/auth/missing required fields) when run interactively (TTY)

## Version control & pushing to GitHub

This repo is set up so you can use Git and push to GitHub without exposing personal data:

1. **What’s ignored (never committed)**  
   `.gitignore` excludes:
   - `real_memory/` and `test_memory/` (profile, resume, applications, screenshots)
   - `.env` and any env files (e.g. for local overrides)
   - `.venv/` and Python cache
   - Credentials under `~/.openclaw/` (those live outside the repo anyway)

2. **What gets committed**  
   - Source code: `openclaw/`, `tests/`
   - `profile.example.json` (placeholder schema; copy to `real_memory/profile.json` locally and fill in your details)
   - `README.md`, config files, etc.

3. **Before the first push**  
   - Run `git status` and confirm no `real_memory/`, `test_memory/`, or `.env` files are staged.
   - If you ever committed secrets or personal data in the past, rewrite history (e.g. `git filter-branch` or BFG Repo-Cleaner) before pushing, or create a fresh repo and push only the current tree.

4. **Initial setup**  
   ```bash
   git init
   git add .
   git status   # double-check nothing sensitive is staged
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/autoapplier.git
   git push -u origin main
   ```
