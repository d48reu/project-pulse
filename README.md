# Project Pulse

Auto-discovery project status dashboard. Scans your local directories for Claude Code projects, reads their planning docs (ROADMAP.md, GSD.md, TODO.md, etc.), and generates a unified status report + HTML dashboard.

## Quick Start

```bash
# Basic scan (offline mode, no API key needed)
python pulse.py --offline

# With Claude API summarization
export ANTHROPIC_API_KEY=your-key-here
python pulse.py

# Custom scan paths
python pulse.py --scan-path ~/Projects --scan-path ~/Code

# Full options
python pulse.py --config ./config.json --output-dir ./reports --offline
```

## How It Works

1. **Auto-discovers** projects by scanning configured directories for git repos and Claude Code indicators (`CLAUDE.md`, `.claude/`)
2. **Reads documentation** — looks for ROADMAP.md, TODO.md, GSD.md, CLAUDE.md, README.md, PLAN.md, STATUS.md, CHANGELOG.md, TASKS.md
3. **Summarizes** each project using Claude API (or basic extraction in offline mode)
4. **Generates outputs:**
   - `STATUS_REPORT.md` — Markdown report with overview table + detailed sections
   - `dashboard.html` — Self-contained HTML dashboard (dark theme, filterable)
   - `pulse_data.json` — Raw structured data for further processing

## Configuration

Edit `config.json` to customize:

```json
{
  "scan_paths": ["~/Projects", "~/Code"],
  "doc_patterns": ["ROADMAP.md", "TODO.md", "GSD.md", "CLAUDE.md"],
  "output_dir": "./output",
  "max_doc_size_kb": 500
}
```

### Scan Paths

By default, Pulse scans these directories (and their immediate children):
- `~/Projects`
- `~/Code`
- `~/dev`
- `~/repos`
- `~/github`

A directory is considered a project if it contains `.git` or `CLAUDE.md`/`.claude/`.

## Outputs

### Markdown Report
Clean, readable status report with:
- Overview table (all projects at a glance)
- Per-project details: summary, status, phase, next steps, blockers, tech stack

### HTML Dashboard
Self-contained single HTML file with:
- Project cards with status indicators
- Filter by status (Active, Paused, Planning, etc.)
- Git metadata (branch, last commit, uncommitted changes)
- Next steps and blockers per project
- Tech stack tags
- Progress bars where available

## Automation

### Cron (run daily at 8am)
```bash
0 8 * * * cd /path/to/project-pulse && python pulse.py --offline >> /tmp/pulse.log 2>&1
```

### Git hook (post-commit)
```bash
#!/bin/sh
cd /path/to/project-pulse && python pulse.py --offline --scan-path "$(git rev-parse --show-toplevel)/.."
```

### Claude Code alias
Add to your shell config:
```bash
alias pulse="python ~/project-pulse/pulse.py"
alias pulse-full="ANTHROPIC_API_KEY=your-key python ~/project-pulse/pulse.py"
```

## Requirements

- Python 3.10+
- `git` (for git metadata extraction)
- `httpx` (optional, for Claude API calls — falls back to urllib)
- Anthropic API key (optional, for AI-powered summarization)
