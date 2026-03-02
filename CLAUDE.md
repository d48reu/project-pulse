# project-pulse

Auto-discovery project status dashboard. Scans the filesystem for Claude Code projects and generates a consolidated status report with optional AI summarization.

**Status:** Working prototype, not version controlled

## How It Works

Single Python script (`pulse.py`) that:
1. Scans configured directories for projects (identified by `.git`, `CLAUDE.md`, or `.claude/`)
2. Reads project docs (ROADMAP.md, TODO.md, CLAUDE.md, README.md, etc.)
3. Summarizes status via Claude API (online) or pattern matching (offline)
4. Outputs: `STATUS_REPORT.md` + `dashboard.html` + `pulse_data.json`

## Tech Stack

**Python 3.10+**, standard library only. Optional: `httpx` for Claude API calls (falls back to urllib).
**No database, no build process, no deployment** — local CLI tool.

## Key Commands

```bash
python pulse.py --offline                     # No API key needed
python pulse.py                               # With Claude API summarization
python pulse.py --scan-path ~/Projects        # Custom scan path
python pulse.py --config ./config.json        # Custom config
python pulse.py --output-dir ./reports        # Custom output directory
```

## Environment Variables

**Optional:**
- `ANTHROPIC_API_KEY` — For AI-powered summarization (gracefully falls back to offline mode)

## Configuration

`config.json` defines scan paths (default: ~/Projects, ~/Code, ~/dev, ~/repos, ~/github), doc file patterns, and excluded directories.

## Dashboard Output

Self-contained HTML dashboard with dark cyberpunk theme. Shows project cards with:
- Status indicators (Active, Paused, Planning, Maintenance, Complete, Stalled)
- Git metadata (branch, last commit, uncommitted changes)
- Tech stack tags, next steps, blockers
- Filter by status

## TODO

- Initialize git repo and add `.gitignore`
- Remove unrelated files (`CommonInstaller.exe`, `SteamSetup.exe`, `files.zip`)
- Clean up duplicate `files/` directory
