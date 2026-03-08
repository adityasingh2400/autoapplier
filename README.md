# OpenClaw

Two tools in one repo: a **Job Match Ranker** (web dashboard) and an **Auto-Applier** (CLI bot that fills and submits job applications).

---

## 1. Job Match Ranker (Web Dashboard)

A local web UI that scores and ranks job postings against your resume using an LLM. Runs at **http://localhost:5050**.

**What it does:**
- Pulls fresh internship/job postings from SimplifyJobs
- Scrapes each job description
- Uses AWS Bedrock Claude to score fit (0-100) based on experience match, recency, and location
- Displays a ranked dashboard: filter by score, age, category, and applied status
- One-click "Mark Applied" to track what you've already submitted

**Run it:**

```bash
python -m openclaw.web_ui
# Open http://localhost:5050
```

From the dashboard you can click "Score New Jobs" to fetch and rank the latest postings, or "Score Unscored" to process jobs already in the ledger that haven't been scored yet.

---

## 2. Auto-Applier (CLI)

A headless browser bot that navigates to a job posting, fills out the entire application form, uploads your resume, answers custom questions via LLM, and optionally submits.

**Supported ATS platforms:** Lever, Greenhouse, Ashby, Workday, plus a generic fallback.

**Single application:**

```bash
python -m openclaw.applier "https://jobs.lever.co/company/job-id" \
  --company "Acme Corp" \
  --role "Software Engineer Intern" \
  --dry-run
```

**Batch apply from SimplifyJobs source:**

```bash
python -m openclaw.applier --source simplify \
  --category "software engineering" \
  --max-jobs 10 \
  --dry-run
```

**Apply to your top-ranked jobs (from the scorer):**

```bash
python -m openclaw.applier --source simplify --apply-top-scored \
  --min-score 75 --max-jobs 5 --dry-run
```

Remove `--dry-run` to actually submit.

---

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

**Memory folder:** Create a local `real_memory/` directory (gitignored) with your personal data:

```
real_memory/
├── profile.json   # Your details + answer bank (copy from profile.example.json)
├── resume.json    # Structured resume data
└── resume.pdf     # Your resume file
```

**AWS credentials:** The scorer and question answerer use AWS Bedrock Claude. Configure `~/.aws/credentials` or set the usual `AWS_*` env vars.

---

## Key CLI Flags

| Flag | What it does |
|------|-------------|
| `--dry-run` | Fill forms but don't submit |
| `--force` | Allow auto-submit for top-tier companies |
| `--human-in-loop` | Pause for CAPTCHAs, auth, or missing fields |
| `--headful` | Show the browser window |
| `--keep-open` | Leave browser open after run for manual review |
| `--quality` | Generate cover letter text, richer answers (slower) |
| `--score-jobs` | Score new jobs from source (requires `--source simplify`) |
| `--list-scored-jobs` | Print scored jobs from the ledger |
| `--ledger-stats` | Print ledger statistics |
| `-v` | Verbose logging |

## Answer Bank

Add a `questionBank` array to `profile.json` to pre-fill common form fields consistently. Patterns are matched best-match-wins against form labels. Use `__HUMAN__` to force manual input for sensitive questions. See `profile.example.json` for the full template.

## Privacy

`real_memory/`, `test_memory/`, `.env`, and credentials are all gitignored. Only source code, config templates, and this README are committed. Never commit your personal `profile.json` or resume.
