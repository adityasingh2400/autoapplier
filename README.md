# Autoapplier

Two tools in one repo:

1. **Job Match Ranker** -- a web dashboard that scores and ranks job postings against your resume
2. **Auto-Applier** -- a CLI bot that fills out and submits job applications automatically

---

## Job Match Ranker

A local web dashboard at **http://localhost:5050** that helps you figure out which jobs are worth applying to.

- Pulls fresh postings from SimplifyJobs
- Scrapes each job description
- Scores fit (0-100) using AWS Bedrock Claude based on experience match, recency, and location
- Ranked table you can filter by score, age, category, and applied status
- Track what you've applied to with one-click "Mark Applied"

```bash
python -m openclaw.web_ui
# Open http://localhost:5050
```

Click **Score New Jobs** to fetch and rank the latest postings. Click **Score Unscored** to process any jobs already in the ledger that haven't been scored yet.

---

## Auto-Applier

A headless browser bot that opens a job posting, fills every field, uploads your resume, answers custom questions with an LLM, and optionally submits.

Supports **Lever, Greenhouse, Ashby, Workday**, and a generic fallback for other sites.

**Apply to a single job:**

```bash
python -m openclaw.applier "https://jobs.lever.co/company/job-id" \
  --company "Acme Corp" \
  --role "Software Engineer Intern" \
  --dry-run
```

**Batch apply from SimplifyJobs:**

```bash
python -m openclaw.applier --source simplify \
  --category "software engineering" \
  --max-jobs 10 \
  --dry-run
```

**Apply to your top-ranked jobs from the scorer:**

```bash
python -m openclaw.applier --source simplify --apply-top-scored \
  --min-score 75 --max-jobs 5 --dry-run
```

Drop `--dry-run` to actually submit.

---

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

Create a `real_memory/` folder (gitignored) with your personal data:

```
real_memory/
├── profile.json   # Your info + answer bank (copy from profile.example.json)
├── resume.json    # Structured resume data
└── resume.pdf     # Your actual resume
```

The scorer and question answerer call AWS Bedrock Claude. Set up `~/.aws/credentials` or the standard `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars.

---

## CLI Flags

| Flag | What it does |
|------|-------------|
| `--dry-run` | Fill forms but don't submit |
| `--force` | Auto-submit even for top-tier companies |
| `--human-in-loop` | Pause for CAPTCHAs, auth walls, or missing fields |
| `--headful` | Show the browser window |
| `--keep-open` | Leave browser open after the run for manual review |
| `--quality` | Generate cover letters, richer answers (slower) |
| `--score-jobs` | Score new jobs from source (`--source simplify` required) |
| `--list-scored-jobs` | Print scored jobs from the ledger |
| `--ledger-stats` | Print ledger stats |
| `-v` | Verbose logging |

## Answer Bank

Add a `questionBank` array to `profile.json` to give consistent answers to common form fields (LinkedIn URL, "how did you hear about us", etc.). Matching is best-match-wins against form labels. Use `__HUMAN__` for questions you want to answer yourself. See `profile.example.json` for the full template.

## Privacy

`real_memory/`, `test_memory/`, `.env`, and all credential files are gitignored. Only source code, config templates, and this README get committed.
