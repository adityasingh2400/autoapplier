"""Simple web UI for job scoring - no external dependencies."""

from __future__ import annotations

import http.server
import json
import re
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

from openclaw.scoring import JobLedger

PORT = 5050

# Translates raw log lines from the scoring process into human-friendly messages
def _friendly_log(line: str) -> str | None:
    line = line.strip()
    if not line:
        return None

    # Loaded ledger
    m = re.search(r"Loaded (\d+) jobs from ledger", line)
    if m:
        return f"📂 Loaded {m.group(1)} previously seen jobs from ledger"

    # Found new jobs
    m = re.search(r"Found (\d+) new jobs to score \(out of (\d+) total\)", line)
    if m:
        return f"🔍 Found {m.group(1)} new jobs to score out of {m.group(2)} total fetched from Simplify"

    # Scraping JDs
    m = re.search(r"Scraping (\d+) job descriptions", line)
    if m:
        return f"🌐 Scraping job descriptions for {m.group(1)} postings in parallel..."

    # Scraped N/M
    m = re.search(r"Scraped (\d+)/(\d+) job descriptions", line)
    if m:
        return f"✅ Scraped {m.group(1)}/{m.group(2)} job descriptions successfully"

    # JD cache hit
    m = re.search(r"JD cache hit for (.+)", line)
    if m:
        return f"⚡ JD cache hit (skipping scrape)"

    # Scored a job
    m = re.search(r"Scored: ([\d.]+) (\w+) - (.+?) @ (.+)", line)
    if m:
        score, rec, role, company = m.group(1), m.group(2), m.group(3), m.group(4)
        emoji = "🟢" if rec == "high_priority" else "🟡" if rec == "medium" else "🔴"
        return f"{emoji} Scored {company} — {role[:50]}: {score} ({rec})"

    # Added job to ledger
    m = re.search(r"Added new job to ledger: (.+?) @ (.+)", line)
    if m:
        return f"➕ New job added: {m.group(1)} @ {m.group(2)}"

    # Saved ledger
    m = re.search(r"Saved (\d+) jobs to ledger", line)
    if m:
        return f"💾 Saved {m.group(1)} jobs to ledger"

    return None


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>OpenClaw Job Scorer</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0d1117;
            color: #c9d1d9;
            padding: 20px;
            max-width: 1400px;
            margin: 0 auto;
        }
        h1 { color: #58a6ff; margin-bottom: 20px; }
        .stats {
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .stat-box {
            background: #161b22;
            padding: 15px 25px;
            border-radius: 8px;
            border: 1px solid #30363d;
        }
        .stat-box .number { font-size: 28px; font-weight: bold; color: #58a6ff; }
        .stat-box .label { font-size: 12px; color: #8b949e; text-transform: uppercase; }
        .stat-box.high .number { color: #3fb950; }
        .stat-box.medium .number { color: #d29922; }
        .stat-box.low .number { color: #f85149; }

        .controls {
            margin-bottom: 15px;
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }
        button {
            background: #238636; color: white; border: none;
            padding: 10px 20px; border-radius: 6px; cursor: pointer;
            font-size: 14px; font-weight: 500;
        }
        button:hover { background: #2ea043; }
        button:disabled { background: #21262d; color: #484f58; cursor: not-allowed; }
        button.secondary { background: #21262d; border: 1px solid #30363d; }
        button.secondary:hover { background: #30363d; }

        select, input {
            background: #0d1117; color: #c9d1d9;
            border: 1px solid #30363d; padding: 8px 12px;
            border-radius: 6px; font-size: 14px;
        }

        /* Live log panel */
        #log-panel {
            display: none;
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 14px 16px;
            margin-bottom: 15px;
            max-height: 220px;
            overflow-y: auto;
            font-family: 'SF Mono', 'Fira Code', monospace;
            font-size: 12px;
        }
        #log-panel.show { display: block; }
        .log-line { padding: 2px 0; color: #8b949e; line-height: 1.6; }
        .log-line.highlight { color: #c9d1d9; }
        .log-line.green { color: #3fb950; }
        .log-line.yellow { color: #d29922; }
        .log-line.red { color: #f85149; }

        .final-status {
            padding: 10px 15px; border-radius: 6px;
            margin-bottom: 15px; display: none;
        }
        .final-status.show { display: block; }
        .final-status.success { background: #064e3b; border: 1px solid #059669; }
        .final-status.error { background: #7f1d1d; border: 1px solid #dc2626; }
        .final-status.info { background: #1f2937; border: 1px solid #374151; }

        table {
            width: 100%; border-collapse: collapse;
            background: #161b22; border-radius: 8px; overflow: hidden;
        }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #21262d; }
        th { background: #21262d; font-weight: 600; color: #8b949e; font-size: 12px; text-transform: uppercase; }
        tr:hover { background: #1c2128; }
        tr.is-applied td { opacity: 0.45; }

        .score { font-weight: bold; font-size: 18px; }
        .score.high { color: #3fb950; }
        .score.medium { color: #d29922; }
        .score.low { color: #f85149; }

        .recommendation { padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
        .recommendation.high_priority { background: #238636; color: white; }
        .recommendation.medium { background: #9e6a03; color: white; }
        .recommendation.low { background: #6e7681; color: white; }
        .recommendation.skip { background: #da3633; color: white; }

        .company { font-weight: 600; color: #58a6ff; }
        .role { color: #c9d1d9; }
        .location { color: #8b949e; font-size: 13px; }
        .age-cell { text-align: center; white-space: nowrap; }
        .age-badge {
            display: inline-block;
            padding: 5px 10px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 600;
        }
        .age-badge.fresh  { background: #0d3321; color: #3fb950; border: 1px solid #238636; }
        .age-badge.recent { background: #2d2000; color: #d29922; border: 1px solid #9e6a03; }
        .age-badge.old    { background: #1c1c1c; color: #6e7681; border: 1px solid #30363d; }
        .age-note { font-size: 10px; color: #484f58; margin-top: 3px; }
        .reasoning { font-size: 12px; color: #8b949e; max-width: 420px; line-height: 1.4; }
        a { color: #58a6ff; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .applied-badge { color: #3fb950; font-size: 12px; font-weight: 600; }
        .mark-applied-btn {
            background: #21262d; color: #8b949e; border: 1px solid #30363d;
            padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 11px;
        }
        .mark-applied-btn:hover { background: #238636; color: white; border-color: #238636; }
        .filters { display: flex; gap: 15px; margin-bottom: 15px; align-items: center; }
        .filters label { font-size: 13px; color: #8b949e; }
    </style>
</head>
<body>
    <h1>OpenClaw Job Scorer</h1>

    <div class="stats">
        <div class="stat-box">
            <div class="number" id="total-jobs">-</div>
            <div class="label">Total Jobs</div>
        </div>
        <div class="stat-box high">
            <div class="number" id="high-priority">-</div>
            <div class="label">High Priority</div>
        </div>
        <div class="stat-box medium">
            <div class="number" id="medium">-</div>
            <div class="label">Medium</div>
        </div>
        <div class="stat-box low">
            <div class="number" id="low-skip">-</div>
            <div class="label">Low/Skip</div>
        </div>
        <div class="stat-box">
            <div class="number" id="applied">-</div>
            <div class="label">Applied</div>
        </div>
    </div>

    <div class="controls">
        <button onclick="scoreNewJobs()" id="score-btn">Score New Jobs</button>
        <button onclick="scoreUnscored()" id="score-unscored-btn" class="secondary">Score Unscored (212)</button>
        <button onclick="refreshJobs()" class="secondary">Refresh List</button>
        <select id="max-jobs">
            <option value="20">20 jobs</option>
            <option value="50">50 jobs</option>
            <option value="100">100 jobs</option>
            <option value="200" selected>200 jobs</option>
        </select>
        <select id="category">
            <option value="all" selected>All Categories</option>
            <option value="software engineering">Software Engineering</option>
            <option value="robotics">Robotics</option>
            <option value="machine learning">Machine Learning</option>
            <option value="data">Data</option>
        </select>
        <select id="max-age-hours">
            <option value="6">Last 6 hours</option>
            <option value="24" selected>Last 24 hours</option>
            <option value="48">Last 48 hours</option>
            <option value="72">Last 72 hours</option>
        </select>
    </div>

    <!-- Live log panel -->
    <div id="log-panel"></div>
    <div class="final-status" id="final-status"></div>

    <div class="filters">
        <label>Min Score: <input type="number" id="min-score" value="0" min="0" max="100" style="width:60px" onchange="refreshJobs()"></label>
        <label>Posted within:
            <select id="max-post-age" onchange="refreshJobs()">
                <option value="0">All time</option>
                <option value="6">6 hours</option>
                <option value="12">12 hours</option>
                <option value="24" selected>24 hours</option>
                <option value="48">48 hours</option>
                <option value="72">3 days</option>
                <option value="168">7 days</option>
            </select>
        </label>
        <label><input type="checkbox" id="unapplied-only" checked onchange="refreshJobs()"> Unapplied only</label>
    </div>

    <table>
        <thead>
            <tr>
                <th>Score</th>
                <th>Posted</th>
                <th>Company / Role</th>
                <th>Location</th>
                <th>Reasoning</th>
                <th>Status</th>
            </tr>
        </thead>
        <tbody id="jobs-table">
            <tr><td colspan="6" style="text-align:center;padding:40px;color:#8b949e">Loading...</td></tr>
        </tbody>
    </table>

    <script>
        function addLogLine(text) {
            const panel = document.getElementById('log-panel');
            panel.classList.add('show');
            const line = document.createElement('div');
            line.className = 'log-line' +
                (text.includes('🟢') || text.includes('✅') || text.includes('💾') ? ' green' :
                 text.includes('🟡') ? ' yellow' :
                 text.includes('🔴') || text.includes('❌') ? ' red' : ' highlight');
            line.textContent = text;
            panel.appendChild(line);
            panel.scrollTop = panel.scrollHeight;
        }

        function clearLog() {
            const panel = document.getElementById('log-panel');
            panel.innerHTML = '';
            panel.classList.remove('show');
        }

        function showFinal(message, type) {
            const el = document.getElementById('final-status');
            el.textContent = message;
            el.className = 'final-status show ' + type;
        }

        function renderAge(postedAt, firstSeen) {
            // Dynamically compute how long ago the job was posted.
            // postedAt = estimated actual posting date (first_seen - age_hours_at_discovery)
            // firstSeen = when we first scraped it (fallback)
            const ref = postedAt || firstSeen;
            if (!ref) {
                return '<td class="age-cell"><span class="age-badge old">unknown</span></td>';
            }

            const hours = (Date.now() - new Date(ref).getTime()) / 3600000;
            let label, cls;

            if (hours < 1) {
                label = `${Math.max(1, Math.round(hours * 60))}m ago`;
                cls = 'fresh';
            } else if (hours < 24) {
                label = `${Math.round(hours)}h ago`;
                cls = hours < 6 ? 'fresh' : 'recent';
            } else {
                const days = Math.floor(hours / 24);
                label = `${days}d ago`;
                cls = days <= 1 ? 'fresh' : days <= 3 ? 'recent' : 'old';
            }

            const note = postedAt ? '' : '~approx';
            return `<td class="age-cell"><span class="age-badge ${cls}">${label}</span>${note ? `<div class="age-note">${note}</div>` : ''}</td>`;
        }

        async function markApplied(urlHash, btn) {
            btn.disabled = true;
            btn.textContent = 'Saving...';
            try {
                const resp = await fetch(`/api/mark-applied?hash=${urlHash}`, { method: 'POST' });
                const data = await resp.json();
                if (data.ok) {
                    const row = btn.closest('tr');
                    row.classList.add('is-applied');
                    btn.replaceWith(Object.assign(document.createElement('span'), {
                        className: 'applied-badge', textContent: '✓ Applied'
                    }));
                    document.getElementById('applied').textContent =
                        parseInt(document.getElementById('applied').textContent || '0') + 1;
                }
            } catch (e) {
                btn.disabled = false;
                btn.textContent = 'Mark Applied';
            }
        }

        async function refreshJobs() {
            const minScore = document.getElementById('min-score').value || 0;
            const unappliedOnly = document.getElementById('unapplied-only').checked;
            try {
                const resp = await fetch(`/api/jobs?min_score=${minScore}&unapplied_only=${unappliedOnly}`);
                const data = await resp.json();
                document.getElementById('total-jobs').textContent = data.stats.total_jobs;
                document.getElementById('high-priority').textContent = data.stats.high_priority;
                document.getElementById('medium').textContent = data.stats.medium;
                document.getElementById('low-skip').textContent = data.stats.low + data.stats.skip;
                document.getElementById('applied').textContent = data.stats.applied;

                const tbody = document.getElementById('jobs-table');

                const maxPostAge = parseInt(document.getElementById('max-post-age').value);
                const now = Date.now();
                let filtered = data.jobs;
                if (maxPostAge > 0) {
                    const cutoffMs = maxPostAge * 3600000;
                    filtered = filtered.filter(job => {
                        const ref = job.posted_at || job.first_seen;
                        if (!ref) return false;
                        return (now - new Date(ref).getTime()) <= cutoffMs;
                    });
                }

                if (filtered.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:40px;color:#8b949e">No jobs match filters</td></tr>';
                    return;
                }
                tbody.innerHTML = filtered.map(job => {
                    const scoreClass = job.score >= 80 ? 'high' : job.score >= 60 ? 'medium' : 'low';
                    const recClass = job.recommendation.replace('_', '-');
                    const ageCell = renderAge(job.posted_at, job.first_seen);
                    const appliedHtml = job.applied
                        ? '<span class="applied-badge">✓ Applied</span>'
                        : `<button class="mark-applied-btn" onclick="markApplied('${job.url_hash}', this)">Mark Applied</button>`;
                    return `
                        <tr class="${job.applied ? 'is-applied' : ''}">
                            <td><span class="score ${scoreClass}">${job.score}</span></td>
                            ${ageCell}
                            <td>
                                <div class="company">${job.company}</div>
                                <div class="role">${job.role}</div>
                                <a href="${job.url}" target="_blank" style="font-size:11px">Apply →</a>
                            </td>
                            <td class="location">${job.location}</td>
                            <td class="reasoning">${job.reasoning || '-'}</td>
                            <td>
                                <span class="recommendation ${recClass}">${job.recommendation}</span>
                                <div style="margin-top:6px">${appliedHtml}</div>
                            </td>
                        </tr>
                    `;
                }).join('');
            } catch (e) {
                showFinal('Failed to load jobs: ' + e.message, 'error');
            }
        }

        function scoreNewJobs() {
            const btn = document.getElementById('score-btn');
            const maxJobs = document.getElementById('max-jobs').value;
            const category = document.getElementById('category').value;
            const maxAgeHours = document.getElementById('max-age-hours').value;

            btn.disabled = true;
            btn.textContent = 'Scoring...';
            clearLog();
            document.getElementById('final-status').className = 'final-status';
            addLogLine('🚀 Starting job scoring run...');

            const url = `/api/score/stream?max_jobs=${maxJobs}&category=${encodeURIComponent(category)}&max_age_hours=${maxAgeHours}`;
            const evtSource = new EventSource(url);

            evtSource.addEventListener('log', e => {
                addLogLine(e.data);
            });

            evtSource.addEventListener('done', e => {
                evtSource.close();
                btn.disabled = false;
                btn.textContent = 'Score New Jobs';
                try {
                    const data = JSON.parse(e.data);
                    if (data.status === 'no_new_jobs') {
                        showFinal('No new jobs found — all current Simplify jobs are already in the ledger.', 'info');
                    } else if (data.status === 'scoring_complete') {
                        showFinal(`✅ Done! Scored ${data.new_jobs_scored} new jobs — ${data.summary.high_priority} high priority, ${data.summary.medium} medium.`, 'success');
                        refreshJobs();
                    } else {
                        showFinal('Error: ' + (data.error || JSON.stringify(data)), 'error');
                    }
                } catch {
                    showFinal('Scoring finished.', 'success');
                    refreshJobs();
                }
            });

            evtSource.addEventListener('error', e => {
                evtSource.close();
                btn.disabled = false;
                btn.textContent = 'Score New Jobs';
                showFinal('❌ Connection error during scoring.', 'error');
            });
        }

        // Initial load
        refreshJobs();

        async function loadUnscoredCount() {
            try {
                const resp = await fetch('/api/jobs?min_score=0&unapplied_only=false');
                const data = await resp.json();
                const total = data.stats.total_jobs;
                const scored = (data.stats.high_priority || 0) + (data.stats.medium || 0) + (data.stats.low || 0) + (data.stats.skip || 0);
                // approximate: total_jobs includes unscored
                // we expose this via stats
            } catch(e) {}
        }

        function scoreUnscored() {
            const btn = document.getElementById('score-unscored-btn');
            btn.disabled = true;
            btn.textContent = 'Scoring...';
            clearLog();
            document.getElementById('final-status').className = 'final-status';
            addLogLine('🔄 Scoring all unscored jobs already in the ledger...');

            const evtSource = new EventSource('/api/score-unscored/stream');

            evtSource.addEventListener('log', e => {
                addLogLine(e.data);
            });

            evtSource.addEventListener('done', e => {
                evtSource.close();
                btn.disabled = false;
                btn.textContent = 'Score Unscored';
                try {
                    const data = JSON.parse(e.data);
                    if (data.status === 'no_new_jobs') {
                        showFinal('✅ All jobs in the ledger are already scored!', 'info');
                    } else if (data.status === 'scoring_complete') {
                        showFinal(`✅ Done! Scored ${data.new_jobs_scored} previously unscored jobs — ${data.summary.high_priority} high priority, ${data.summary.medium} medium.`, 'success');
                        refreshJobs();
                    } else {
                        showFinal('Error: ' + (data.error || JSON.stringify(data)), 'error');
                    }
                } catch {
                    showFinal('Scoring finished.', 'success');
                    refreshJobs();
                }
            });

            evtSource.addEventListener('error', e => {
                evtSource.close();
                btn.disabled = false;
                btn.textContent = 'Score Unscored';
                showFinal('❌ Connection error during scoring.', 'error');
            });
        }
    </script>
</body>
</html>
"""


class JobScorerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default access logging

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_html(self, html):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode())

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == '/':
            self.send_html(HTML_TEMPLATE)
        elif path == '/api/jobs':
            self.handle_get_jobs(query)
        elif path == '/api/score/stream':
            self.handle_score_stream(query)
        elif path == '/api/score-unscored/stream':
            self.handle_score_unscored_stream()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == '/api/score':
            self.handle_score_stream(query)
        elif path == '/api/mark-applied':
            self.handle_mark_applied(query)
        else:
            self.send_response(404)
            self.end_headers()

    def handle_mark_applied(self, query):
        url_hash = query.get('hash', [''])[0]
        if not url_hash:
            self.send_json({'ok': False, 'error': 'missing hash'})
            return

        ledger = JobLedger()
        entry = ledger._jobs.get(url_hash)
        if entry is None:
            self.send_json({'ok': False, 'error': 'job not found'})
            return

        ledger.mark_applied(entry.url, 'manual')
        ledger.save()
        self.send_json({'ok': True})

    def handle_get_jobs(self, query):
        min_score = float(query.get('min_score', ['0'])[0])
        unapplied_only = query.get('unapplied_only', ['true'])[0].lower() == 'true'

        ledger = JobLedger()
        jobs = ledger.get_scored_jobs(
            min_score=min_score if min_score > 0 else None,
            unapplied_only=unapplied_only
        )
        jobs.sort(key=lambda j: j.score or 0, reverse=True)

        stats = {
            'total_jobs': len(ledger._jobs),
            'high_priority': sum(1 for j in ledger._jobs.values() if j.recommendation == 'high_priority'),
            'medium': sum(1 for j in ledger._jobs.values() if j.recommendation == 'medium'),
            'low': sum(1 for j in ledger._jobs.values() if j.recommendation == 'low'),
            'skip': sum(1 for j in ledger._jobs.values() if j.recommendation == 'skip'),
            'applied': sum(1 for j in ledger._jobs.values() if j.applied),
        }

        self.send_json({
            'jobs': [
                {
                    'company': j.company,
                    'role': j.role,
                    'location': j.location,
                    'url': j.url,
                    'url_hash': j.url_hash,
                    'score': j.score,
                    'posted_at': j.posted_at,
                    'reasoning': j.score_reasoning[:300] if j.score_reasoning else '',
                    'recommendation': j.recommendation,
                    'applied': j.applied,
                    'first_seen': j.first_seen,
                }
                for j in jobs[:200]
            ],
            'stats': stats,
        })

    def handle_score_unscored_stream(self):
        """Stream scoring progress for unscored ledger jobs using Server-Sent Events."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        def send_event(event_type: str, data: str):
            msg = f"event: {event_type}\ndata: {data}\n\n"
            try:
                self.wfile.write(msg.encode('utf-8'))
                self.wfile.flush()
            except Exception:
                pass

        proc = subprocess.Popen(
            [
                sys.executable, '-m', 'openclaw.applier',
                '--score-unscored',
                '-v',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=Path(__file__).parent.parent,
        )

        stdout_lines: list[str] = []

        def stream_stderr():
            for line in proc.stderr:
                friendly = _friendly_log(line)
                if friendly:
                    send_event('log', friendly)

        stderr_thread = threading.Thread(target=stream_stderr, daemon=True)
        stderr_thread.start()

        for line in proc.stdout:
            stdout_lines.append(line)

        stderr_thread.join(timeout=5)
        proc.wait()

        final_output = ''.join(stdout_lines).strip()
        try:
            result = json.loads(final_output)
            send_event('done', json.dumps(result))
        except json.JSONDecodeError:
            send_event('done', json.dumps({'error': final_output or 'No output from scorer'}))

    def handle_score_stream(self, query):
        """Stream scoring progress using Server-Sent Events."""
        max_jobs = query.get('max_jobs', ['200'])[0]
        category = query.get('category', ['all'])[0]
        max_age_hours = query.get('max_age_hours', ['24'])[0]

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        def send_event(event_type: str, data: str):
            msg = f"event: {event_type}\ndata: {data}\n\n"
            try:
                self.wfile.write(msg.encode('utf-8'))
                self.wfile.flush()
            except Exception:
                pass

        proc = subprocess.Popen(
            [
                sys.executable, '-m', 'openclaw.applier',
                '--source', 'simplify',
                '--score-jobs',
                '--category', category,
                '--max-age-hours', max_age_hours,
                '--max-jobs', max_jobs,
                '-v',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=Path(__file__).parent.parent,
        )

        stdout_lines: list[str] = []

        # Stream stderr (log lines) in a thread while capturing stdout
        def stream_stderr():
            for line in proc.stderr:
                friendly = _friendly_log(line)
                if friendly:
                    send_event('log', friendly)

        stderr_thread = threading.Thread(target=stream_stderr, daemon=True)
        stderr_thread.start()

        # Capture stdout (final JSON)
        for line in proc.stdout:
            stdout_lines.append(line)

        stderr_thread.join(timeout=5)
        proc.wait()

        final_output = ''.join(stdout_lines).strip()
        try:
            result = json.loads(final_output)
            send_event('done', json.dumps(result))
        except json.JSONDecodeError:
            send_event('done', json.dumps({'error': final_output or 'No output from scorer'}))


def main():
    print("\n" + "=" * 50)
    print("  OpenClaw Job Scorer UI")
    print(f"  Open http://localhost:{PORT} in your browser")
    print("=" * 50 + "\n")

    server = http.server.HTTPServer(('127.0.0.1', PORT), JobScorerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == '__main__':
    main()
