#!/usr/bin/env python3
"""
Project Pulse - Auto-discovery project status dashboard
Scans local directories for Claude Code projects, reads their planning docs,
summarizes via Claude API, and generates a unified status report + HTML dashboard.

Usage:
    python pulse.py                    # Full scan with Claude API summarization
    python pulse.py --offline          # Scan only, no API calls (raw doc excerpts)
    python pulse.py --scan-path ~/Code # Override scan paths
    python pulse.py --config ./my.json # Custom config file
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "scan_paths": ["~/Projects", "~/Code", "~/dev", "~/repos", "~/github"],
    "doc_patterns": [
        "ROADMAP.md", "TODO.md", "GSD.md", "CLAUDE.md", "README.md",
        "PLAN.md", "STATUS.md", "CHANGELOG.md", "TASKS.md"
    ],
    "git_indicators": [".git", ".github"],
    "claude_code_indicators": ["CLAUDE.md", ".claude"],
    "output_dir": "./output",
    "max_doc_size_kb": 500,
    "excluded_dirs": [
        "node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build"
    ],
}


def load_config(config_path: Optional[str] = None) -> dict:
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            user_cfg = json.load(f)
        merged = {**DEFAULT_CONFIG, **user_cfg}
        return merged
    # Try default location
    default_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(default_path):
        with open(default_path) as f:
            user_cfg = json.load(f)
        return {**DEFAULT_CONFIG, **user_cfg}
    return DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Project Discovery
# ---------------------------------------------------------------------------

def is_project_dir(path: Path, config: dict) -> dict | None:
    """Check if a directory is a project. Returns project info dict or None."""
    if not path.is_dir():
        return None

    has_git = any((path / ind).exists() for ind in config["git_indicators"])
    has_claude = any((path / ind).exists() for ind in config["claude_code_indicators"])

    if not (has_git or has_claude):
        return None

    # Find available docs
    docs = {}
    for pattern in config["doc_patterns"]:
        doc_path = path / pattern
        if doc_path.exists():
            size_kb = doc_path.stat().st_size / 1024
            if size_kb <= config["max_doc_size_kb"]:
                docs[pattern] = doc_path

    # Get git info
    git_info = get_git_info(path) if has_git else {}

    return {
        "name": path.name,
        "path": str(path),
        "has_git": has_git,
        "has_claude_code": has_claude,
        "docs": docs,
        "git": git_info,
    }


def get_git_info(path: Path) -> dict:
    """Extract git metadata from a project directory."""
    info = {}
    try:
        # Current branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=path, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            info["branch"] = result.stdout.strip()

        # Last commit
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H|%s|%ai"],
            cwd=path, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("|", 2)
            if len(parts) == 3:
                info["last_commit_hash"] = parts[0][:8]
                info["last_commit_msg"] = parts[1]
                info["last_commit_date"] = parts[2]

        # Remote URL
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            # Convert SSH to HTTPS for display
            if url.startswith("git@github.com:"):
                url = url.replace("git@github.com:", "https://github.com/").rstrip(".git")
            info["remote_url"] = url

        # Uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            changes = result.stdout.strip().split("\n") if result.stdout.strip() else []
            info["uncommitted_changes"] = len(changes)

    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return info


def discover_projects(config: dict, override_paths: list[str] | None = None) -> list[dict]:
    """Scan configured paths for projects."""
    scan_paths = override_paths or config["scan_paths"]
    projects = []
    seen_paths = set()

    for scan_path in scan_paths:
        expanded = Path(os.path.expanduser(scan_path))
        if not expanded.exists():
            continue

        # Check if the scan path itself is a project
        proj = is_project_dir(expanded, config)
        if proj and str(expanded) not in seen_paths:
            projects.append(proj)
            seen_paths.add(str(expanded))

        # Check immediate children (1 level deep)
        try:
            for child in sorted(expanded.iterdir()):
                if child.name in config["excluded_dirs"] or child.name.startswith("."):
                    continue
                proj = is_project_dir(child, config)
                if proj and str(child) not in seen_paths:
                    projects.append(proj)
                    seen_paths.add(str(child))
        except PermissionError:
            continue

    return projects


# ---------------------------------------------------------------------------
# Doc Reading
# ---------------------------------------------------------------------------

def read_project_docs(project: dict) -> dict[str, str]:
    """Read all discovered docs for a project."""
    contents = {}
    for name, path in project["docs"].items():
        try:
            contents[name] = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            contents[name] = "[Error reading file]"
    return contents


# ---------------------------------------------------------------------------
# Claude API Summarization
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are analyzing project documentation to extract a structured status report.

Given the following documentation files from a project called "{project_name}", extract:

1. **Summary**: 2-3 sentence description of what this project is and its current state.
2. **Status**: One of: Active, Paused, Planning, Maintenance, Complete, Stalled
3. **Current Phase**: What phase/milestone the project is in right now.
4. **Next Steps**: The top 3-5 concrete next actions (be specific, actionable).
5. **Blockers**: Any blockers, dependencies, or risks mentioned (or "None identified").
6. **Progress**: Rough percentage complete if determinable, otherwise "N/A".
7. **Tech Stack**: Key technologies/frameworks mentioned.

Respond in this exact JSON format (no markdown fencing):
{{
    "summary": "...",
    "status": "Active|Paused|Planning|Maintenance|Complete|Stalled",
    "current_phase": "...",
    "next_steps": ["step 1", "step 2", "step 3"],
    "blockers": ["blocker 1"] or [],
    "progress": "65%" or "N/A",
    "tech_stack": ["tech1", "tech2"]
}}

Here are the project documents:

{documents}
"""


def summarize_with_claude(project: dict, docs: dict[str, str], api_key: str) -> dict:
    """Send docs to Claude API for structured extraction."""
    try:
        import httpx
    except ImportError:
        # Fall back to urllib
        return _summarize_urllib(project, docs, api_key)

    doc_text = ""
    for name, content in docs.items():
        # Truncate very long docs
        truncated = content[:8000] if len(content) > 8000 else content
        doc_text += f"\n--- {name} ---\n{truncated}\n"

    prompt = EXTRACTION_PROMPT.format(
        project_name=project["name"],
        documents=doc_text
    )

    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        text = data["content"][0]["text"]
        # Parse JSON from response
        return json.loads(text)
    except Exception as e:
        return {"error": str(e), "summary": f"API call failed: {e}"}


def _summarize_urllib(project: dict, docs: dict[str, str], api_key: str) -> dict:
    """Fallback summarization using urllib."""
    import urllib.request
    import urllib.error

    doc_text = ""
    for name, content in docs.items():
        truncated = content[:8000] if len(content) > 8000 else content
        doc_text += f"\n--- {name} ---\n{truncated}\n"

    prompt = EXTRACTION_PROMPT.format(
        project_name=project["name"],
        documents=doc_text
    )

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        text = data["content"][0]["text"]
        return json.loads(text)
    except Exception as e:
        return {"error": str(e), "summary": f"API call failed: {e}"}


def offline_summary(project: dict, docs: dict[str, str]) -> dict:
    """Generate a basic summary without API calls."""
    summary_parts = []

    if "README.md" in docs:
        # Grab first paragraph
        lines = docs["README.md"].strip().split("\n")
        first_content = ""
        for line in lines:
            if line.strip() and not line.startswith("#"):
                first_content = line.strip()
                break
        if first_content:
            summary_parts.append(first_content[:200])

    # Extract TODO items
    next_steps = []
    for doc_name in ["TODO.md", "GSD.md", "TASKS.md"]:
        if doc_name in docs:
            for line in docs[doc_name].split("\n"):
                line = line.strip()
                if re.match(r"^[-*\[\] ]*\[ \]", line):  # Unchecked checkbox
                    clean = re.sub(r"^[-*\[\] ]*\[ \]\s*", "", line)
                    if clean:
                        next_steps.append(clean[:100])
                if len(next_steps) >= 5:
                    break
        if len(next_steps) >= 5:
            break

    # Detect tech stack from docs
    tech_keywords = {
        "python", "javascript", "typescript", "react", "node", "express",
        "fastapi", "flask", "django", "rust", "go", "docker", "postgresql",
        "sqlite", "mongodb", "redis", "tailwind", "nextjs", "vite",
        "svelte", "vue", "astro", "html", "css"
    }
    found_tech = set()
    all_text = " ".join(docs.values()).lower()
    for kw in tech_keywords:
        if kw in all_text:
            found_tech.add(kw)

    return {
        "summary": " ".join(summary_parts) if summary_parts else f"Project: {project['name']}",
        "status": "Unknown (offline mode)",
        "current_phase": "See docs for details",
        "next_steps": next_steps[:5] if next_steps else ["Review project docs"],
        "blockers": [],
        "progress": "N/A",
        "tech_stack": sorted(found_tech)[:8],
    }


# ---------------------------------------------------------------------------
# Markdown Report Generation
# ---------------------------------------------------------------------------

def generate_markdown(projects: list[dict], summaries: dict) -> str:
    """Generate the STATUS_REPORT.md content."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# 📡 Project Pulse — Status Report",
        f"",
        f"**Generated:** {now}  ",
        f"**Projects Tracked:** {len(projects)}",
        f"",
        f"---",
        f"",
    ]

    # Summary table
    lines.append("## Overview")
    lines.append("")
    lines.append("| Project | Status | Progress | Last Commit |")
    lines.append("|---------|--------|----------|-------------|")

    for proj in projects:
        s = summaries.get(proj["name"], {})
        status = s.get("status", "Unknown")
        progress = s.get("progress", "N/A")
        last_commit = proj.get("git", {}).get("last_commit_date", "N/A")
        if last_commit != "N/A":
            last_commit = last_commit[:10]  # Just the date
        lines.append(f"| **{proj['name']}** | {status} | {progress} | {last_commit} |")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Detailed sections per project
    for proj in projects:
        s = summaries.get(proj["name"], {})
        git = proj.get("git", {})

        lines.append(f"## {proj['name']}")
        lines.append("")

        if s.get("summary"):
            lines.append(f"{s['summary']}")
            lines.append("")

        lines.append(f"- **Path:** `{proj['path']}`")
        if git.get("remote_url"):
            lines.append(f"- **Repo:** {git['remote_url']}")
        if git.get("branch"):
            lines.append(f"- **Branch:** `{git['branch']}`")
        if s.get("current_phase"):
            lines.append(f"- **Phase:** {s['current_phase']}")
        if s.get("status"):
            lines.append(f"- **Status:** {s['status']}")
        if s.get("progress") and s["progress"] != "N/A":
            lines.append(f"- **Progress:** {s['progress']}")
        if git.get("uncommitted_changes", 0) > 0:
            lines.append(f"- **Uncommitted Changes:** {git['uncommitted_changes']}")
        if git.get("last_commit_msg"):
            lines.append(f"- **Last Commit:** {git['last_commit_msg']} (`{git.get('last_commit_hash', '')}`)")
        lines.append("")

        if s.get("tech_stack"):
            lines.append(f"**Tech:** {', '.join(s['tech_stack'])}")
            lines.append("")

        if s.get("next_steps"):
            lines.append("**Next Steps:**")
            for step in s["next_steps"]:
                lines.append(f"- [ ] {step}")
            lines.append("")

        if s.get("blockers"):
            lines.append("**Blockers:**")
            for b in s["blockers"]:
                lines.append(f"- ⚠️ {b}")
            lines.append("")

        if s.get("error"):
            lines.append(f"> ⚠️ Summarization error: {s['error']}")
            lines.append("")

        # List available docs
        if proj["docs"]:
            lines.append(f"**Docs:** {', '.join(proj['docs'].keys())}")
            lines.append("")

        lines.append("---")
        lines.append("")

    lines.append(f"*Report generated by [Project Pulse](https://github.com) — Auto-discovery project tracker*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML Dashboard Generation
# ---------------------------------------------------------------------------

def generate_html(projects: list[dict], summaries: dict) -> str:
    """Generate a self-contained HTML dashboard."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Build project cards JSON for the template
    cards = []
    for proj in projects:
        s = summaries.get(proj["name"], {})
        git = proj.get("git", {})
        cards.append({
            "name": proj["name"],
            "path": proj["path"],
            "summary": s.get("summary", ""),
            "status": s.get("status", "Unknown"),
            "current_phase": s.get("current_phase", ""),
            "progress": s.get("progress", "N/A"),
            "next_steps": s.get("next_steps", []),
            "blockers": s.get("blockers", []),
            "tech_stack": s.get("tech_stack", []),
            "branch": git.get("branch", ""),
            "remote_url": git.get("remote_url", ""),
            "last_commit_msg": git.get("last_commit_msg", ""),
            "last_commit_hash": git.get("last_commit_hash", ""),
            "last_commit_date": git.get("last_commit_date", "")[:10] if git.get("last_commit_date") else "",
            "uncommitted_changes": git.get("uncommitted_changes", 0),
            "docs": list(proj["docs"].keys()),
            "has_claude_code": proj.get("has_claude_code", False),
        })

    cards_json = json.dumps(cards, indent=2)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Project Pulse</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
    --bg-primary: #0a0a0f;
    --bg-secondary: #12121a;
    --bg-card: #16161f;
    --bg-card-hover: #1c1c28;
    --border: #2a2a3a;
    --border-glow: #3a3a5a;
    --text-primary: #e8e8f0;
    --text-secondary: #8888a0;
    --text-muted: #555568;
    --accent-cyan: #00d4ff;
    --accent-green: #00ff88;
    --accent-amber: #ffaa00;
    --accent-red: #ff4466;
    --accent-purple: #aa66ff;
    --accent-blue: #4488ff;
    --status-active: #00ff88;
    --status-paused: #ffaa00;
    --status-planning: #4488ff;
    --status-maintenance: #aa66ff;
    --status-complete: #00d4ff;
    --status-stalled: #ff4466;
    --status-unknown: #555568;
    --font-mono: 'JetBrains Mono', monospace;
    --font-sans: 'IBM Plex Sans', sans-serif;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
    background: var(--bg-primary);
    color: var(--text-primary);
    font-family: var(--font-sans);
    min-height: 100vh;
    overflow-x: hidden;
}}

/* Scanline overlay */
body::after {{
    content: '';
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: repeating-linear-gradient(
        0deg,
        transparent,
        transparent 2px,
        rgba(0, 0, 0, 0.03) 2px,
        rgba(0, 0, 0, 0.03) 4px
    );
    pointer-events: none;
    z-index: 1000;
}}

.container {{
    max-width: 1400px;
    margin: 0 auto;
    padding: 2rem;
}}

/* Header */
.header {{
    margin-bottom: 3rem;
    padding-bottom: 2rem;
    border-bottom: 1px solid var(--border);
    position: relative;
}}

.header::after {{
    content: '';
    position: absolute;
    bottom: -1px;
    left: 0;
    width: 200px;
    height: 1px;
    background: linear-gradient(90deg, var(--accent-cyan), transparent);
}}

.header-top {{
    display: flex;
    align-items: baseline;
    gap: 1rem;
    margin-bottom: 0.5rem;
}}

.logo {{
    font-family: var(--font-mono);
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--accent-cyan);
    letter-spacing: -0.02em;
}}

.logo span {{
    color: var(--text-muted);
    font-weight: 300;
}}

.header-meta {{
    font-family: var(--font-mono);
    font-size: 0.75rem;
    color: var(--text-muted);
    display: flex;
    gap: 2rem;
}}

.header-meta .pulse-dot {{
    display: inline-block;
    width: 6px;
    height: 6px;
    background: var(--accent-green);
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 2s infinite;
}}

@keyframes pulse {{
    0%, 100% {{ opacity: 1; box-shadow: 0 0 4px var(--accent-green); }}
    50% {{ opacity: 0.4; box-shadow: 0 0 8px var(--accent-green); }}
}}

/* Stats bar */
.stats-bar {{
    display: flex;
    gap: 2rem;
    margin-bottom: 2rem;
    flex-wrap: wrap;
}}

.stat {{
    font-family: var(--font-mono);
    font-size: 0.8rem;
}}

.stat-value {{
    font-size: 1.8rem;
    font-weight: 700;
    color: var(--accent-cyan);
    line-height: 1;
}}

.stat-label {{
    color: var(--text-muted);
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
}}

/* Filter bar */
.filter-bar {{
    display: flex;
    gap: 0.5rem;
    margin-bottom: 2rem;
    flex-wrap: wrap;
}}

.filter-btn {{
    font-family: var(--font-mono);
    font-size: 0.7rem;
    padding: 0.4rem 0.8rem;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.2s;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}}

.filter-btn:hover, .filter-btn.active {{
    border-color: var(--accent-cyan);
    color: var(--accent-cyan);
    background: rgba(0, 212, 255, 0.05);
}}

/* Project grid */
.project-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
    gap: 1.5rem;
}}

/* Project card */
.card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    padding: 1.5rem;
    position: relative;
    transition: all 0.3s ease;
    overflow: hidden;
}}

.card::before {{
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    width: 3px;
    height: 100%;
    transition: all 0.3s;
}}

.card:hover {{
    border-color: var(--border-glow);
    background: var(--bg-card-hover);
    transform: translateY(-2px);
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}}

.card[data-status="Active"]::before {{ background: var(--status-active); }}
.card[data-status="Paused"]::before {{ background: var(--status-paused); }}
.card[data-status="Planning"]::before {{ background: var(--status-planning); }}
.card[data-status="Maintenance"]::before {{ background: var(--status-maintenance); }}
.card[data-status="Complete"]::before {{ background: var(--status-complete); }}
.card[data-status="Stalled"]::before {{ background: var(--status-stalled); }}
.card::before {{ background: var(--status-unknown); }}

.card-header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 0.75rem;
}}

.card-name {{
    font-family: var(--font-mono);
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--text-primary);
}}

.card-name a {{
    color: inherit;
    text-decoration: none;
}}

.card-name a:hover {{
    color: var(--accent-cyan);
}}

.status-badge {{
    font-family: var(--font-mono);
    font-size: 0.65rem;
    padding: 0.2rem 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    border: 1px solid;
    white-space: nowrap;
}}

.status-Active {{ color: var(--status-active); border-color: var(--status-active); background: rgba(0, 255, 136, 0.08); }}
.status-Paused {{ color: var(--status-paused); border-color: var(--status-paused); background: rgba(255, 170, 0, 0.08); }}
.status-Planning {{ color: var(--status-planning); border-color: var(--status-planning); background: rgba(68, 136, 255, 0.08); }}
.status-Maintenance {{ color: var(--status-maintenance); border-color: var(--status-maintenance); background: rgba(170, 102, 255, 0.08); }}
.status-Complete {{ color: var(--status-complete); border-color: var(--status-complete); background: rgba(0, 212, 255, 0.08); }}
.status-Stalled {{ color: var(--status-stalled); border-color: var(--status-stalled); background: rgba(255, 68, 102, 0.08); }}

.card-summary {{
    font-size: 0.85rem;
    color: var(--text-secondary);
    line-height: 1.5;
    margin-bottom: 1rem;
}}

.card-meta {{
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 1rem;
    font-family: var(--font-mono);
    font-size: 0.7rem;
    color: var(--text-muted);
}}

.card-meta-item {{
    display: flex;
    align-items: center;
    gap: 0.3rem;
}}

.card-section-title {{
    font-family: var(--font-mono);
    font-size: 0.65rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 0.5rem;
    margin-top: 1rem;
}}

.next-steps {{
    list-style: none;
    padding: 0;
}}

.next-steps li {{
    font-size: 0.8rem;
    color: var(--text-secondary);
    padding: 0.3rem 0;
    padding-left: 1.2rem;
    position: relative;
    line-height: 1.4;
}}

.next-steps li::before {{
    content: '›';
    position: absolute;
    left: 0;
    color: var(--accent-cyan);
    font-weight: 700;
}}

.blocker {{
    font-size: 0.8rem;
    color: var(--accent-amber);
    padding: 0.3rem 0;
    padding-left: 1.2rem;
    position: relative;
}}

.blocker::before {{
    content: '!';
    position: absolute;
    left: 0;
    font-weight: 700;
    color: var(--accent-red);
}}

.tech-tags {{
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
    margin-top: 1rem;
}}

.tech-tag {{
    font-family: var(--font-mono);
    font-size: 0.6rem;
    padding: 0.15rem 0.5rem;
    background: rgba(170, 102, 255, 0.1);
    border: 1px solid rgba(170, 102, 255, 0.2);
    color: var(--accent-purple);
}}

.doc-tags {{
    display: flex;
    gap: 0.3rem;
    flex-wrap: wrap;
    margin-top: 0.5rem;
}}

.doc-tag {{
    font-family: var(--font-mono);
    font-size: 0.55rem;
    padding: 0.1rem 0.4rem;
    background: rgba(0, 212, 255, 0.05);
    border: 1px solid rgba(0, 212, 255, 0.15);
    color: var(--accent-cyan);
    opacity: 0.6;
}}

.claude-badge {{
    font-family: var(--font-mono);
    font-size: 0.6rem;
    padding: 0.15rem 0.5rem;
    background: rgba(255, 170, 0, 0.1);
    border: 1px solid rgba(255, 170, 0, 0.2);
    color: var(--accent-amber);
}}

.progress-bar {{
    width: 100%;
    height: 3px;
    background: var(--border);
    margin-top: 1rem;
    position: relative;
    overflow: hidden;
}}

.progress-fill {{
    height: 100%;
    background: linear-gradient(90deg, var(--accent-cyan), var(--accent-green));
    transition: width 0.5s ease;
}}

/* Empty state */
.empty-state {{
    text-align: center;
    padding: 4rem 2rem;
    color: var(--text-muted);
    font-family: var(--font-mono);
}}

.empty-state h2 {{
    font-size: 1.2rem;
    margin-bottom: 1rem;
    color: var(--text-secondary);
}}

/* Responsive */
@media (max-width: 768px) {{
    .container {{ padding: 1rem; }}
    .project-grid {{ grid-template-columns: 1fr; }}
    .stats-bar {{ gap: 1rem; }}
}}
</style>
</head>
<body>

<div class="container">
    <header class="header">
        <div class="header-top">
            <div class="logo">PROJECT<span>/</span>PULSE</div>
        </div>
        <div class="header-meta">
            <span><span class="pulse-dot"></span>SCAN COMPLETE</span>
            <span>GENERATED: {now}</span>
            <span>PROJECTS: {len(projects)}</span>
        </div>
    </header>

    <div class="stats-bar" id="stats-bar"></div>
    <div class="filter-bar" id="filter-bar"></div>
    <div class="project-grid" id="project-grid"></div>
</div>

<script>
const projects = {cards_json};

// Calculate stats
const statusCounts = {{}};
projects.forEach(p => {{
    const s = p.status.split(' ')[0]; // Handle "Unknown (offline mode)"
    statusCounts[s] = (statusCounts[s] || 0) + 1;
}});

const totalSteps = projects.reduce((a, p) => a + p.next_steps.length, 0);
const totalBlockers = projects.reduce((a, p) => a + p.blockers.length, 0);
const withChanges = projects.filter(p => p.uncommitted_changes > 0).length;

// Render stats
const statsBar = document.getElementById('stats-bar');
const stats = [
    {{ value: projects.length, label: 'Projects' }},
    {{ value: totalSteps, label: 'Pending Actions' }},
    {{ value: totalBlockers, label: 'Blockers' }},
    {{ value: withChanges, label: 'Uncommitted' }},
];
stats.forEach(s => {{
    const div = document.createElement('div');
    div.className = 'stat';
    div.innerHTML = `<div class="stat-value">${{s.value}}</div><div class="stat-label">${{s.label}}</div>`;
    statsBar.appendChild(div);
}});

// Render filters
const filterBar = document.getElementById('filter-bar');
const allStatuses = ['All', ...new Set(projects.map(p => p.status.split(' ')[0]))];
allStatuses.forEach((status, i) => {{
    const btn = document.createElement('button');
    btn.className = 'filter-btn' + (i === 0 ? ' active' : '');
    btn.textContent = status;
    btn.onclick = () => {{
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        renderGrid(status === 'All' ? null : status);
    }};
    filterBar.appendChild(btn);
}});

// Render project grid
function renderGrid(filterStatus) {{
    const grid = document.getElementById('project-grid');
    grid.innerHTML = '';

    const filtered = filterStatus
        ? projects.filter(p => p.status.startsWith(filterStatus))
        : projects;

    if (filtered.length === 0) {{
        grid.innerHTML = '<div class="empty-state"><h2>No projects found</h2><p>No projects match the current filter.</p></div>';
        return;
    }}

    filtered.forEach(p => {{
        const statusClass = p.status.split(' ')[0].replace(/[^a-zA-Z]/g, '');
        const card = document.createElement('div');
        card.className = 'card';
        card.dataset.status = statusClass;

        let nameHtml = p.name;
        if (p.remote_url) {{
            nameHtml = `<a href="${{p.remote_url}}" target="_blank" rel="noopener">${{p.name}}</a>`;
        }}

        let progressHtml = '';
        const progressMatch = p.progress.match(/(\\d+)/);
        if (progressMatch) {{
            const pct = parseInt(progressMatch[1]);
            progressHtml = `<div class="progress-bar"><div class="progress-fill" style="width:${{pct}}%"></div></div>`;
        }}

        let metaHtml = '';
        if (p.branch) metaHtml += `<span class="card-meta-item">⎇ ${{p.branch}}</span>`;
        if (p.last_commit_date) metaHtml += `<span class="card-meta-item">⏱ ${{p.last_commit_date}}</span>`;
        if (p.uncommitted_changes > 0) metaHtml += `<span class="card-meta-item" style="color:var(--accent-amber)">△ ${{p.uncommitted_changes}} changes</span>`;
        if (p.current_phase) metaHtml += `<span class="card-meta-item">◈ ${{p.current_phase}}</span>`;

        let stepsHtml = '';
        if (p.next_steps.length > 0) {{
            stepsHtml = `<div class="card-section-title">Next Steps</div><ul class="next-steps">${{p.next_steps.map(s => `<li>${{s}}</li>`).join('')}}</ul>`;
        }}

        let blockersHtml = '';
        if (p.blockers.length > 0) {{
            blockersHtml = `<div class="card-section-title">Blockers</div>${{p.blockers.map(b => `<div class="blocker">${{b}}</div>`).join('')}}`;
        }}

        let techHtml = '';
        if (p.tech_stack.length > 0) {{
            techHtml = `<div class="tech-tags">${{p.tech_stack.map(t => `<span class="tech-tag">${{t}}</span>`).join('')}}</div>`;
        }}

        let docsHtml = '';
        if (p.docs.length > 0) {{
            docsHtml = `<div class="doc-tags">${{p.docs.map(d => `<span class="doc-tag">${{d}}</span>`).join('')}}</div>`;
        }}

        let claudeBadge = p.has_claude_code ? '<span class="claude-badge">⚡ Claude Code</span>' : '';

        card.innerHTML = `
            <div class="card-header">
                <div class="card-name">${{nameHtml}}</div>
                <span class="status-badge status-${{statusClass}}">${{p.status}}</span>
            </div>
            <div class="card-summary">${{p.summary}}</div>
            <div class="card-meta">${{metaHtml}}</div>
            ${{stepsHtml}}
            ${{blockersHtml}}
            ${{techHtml}}
            <div style="display:flex;gap:0.4rem;flex-wrap:wrap;margin-top:0.5rem">
                ${{claudeBadge}}
            </div>
            ${{docsHtml}}
            ${{progressHtml}}
        `;

        grid.appendChild(card);
    }});
}}

renderGrid(null);
</script>
</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Project Pulse — Auto-discovery project status dashboard")
    parser.add_argument("--config", help="Path to config JSON file")
    parser.add_argument("--scan-path", action="append", dest="scan_paths", help="Override scan paths (can specify multiple)")
    parser.add_argument("--offline", action="store_true", help="Skip Claude API calls, generate basic summaries")
    parser.add_argument("--output-dir", help="Override output directory")
    parser.add_argument("--api-key", help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.output_dir:
        config["output_dir"] = args.output_dir

    # Discover projects
    print("🔍 Scanning for projects...")
    projects = discover_projects(config, args.scan_paths)

    if not projects:
        print("❌ No projects found. Check your scan paths in config.json")
        print(f"   Scanned: {args.scan_paths or config['scan_paths']}")
        sys.exit(1)

    print(f"📁 Found {len(projects)} projects:")
    for p in projects:
        docs_str = ", ".join(p["docs"].keys()) if p["docs"] else "no docs"
        cc = " [Claude Code]" if p["has_claude_code"] else ""
        print(f"   • {p['name']}{cc} — {docs_str}")

    # Read docs and summarize
    summaries = {}
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    for proj in projects:
        docs = read_project_docs(proj)
        if not docs:
            summaries[proj["name"]] = {"summary": "No documentation found", "status": "Unknown", "next_steps": ["Add project documentation"]}
            continue

        if args.offline or not api_key:
            if not args.offline and not api_key:
                print(f"   ⚠️  No API key found, using offline mode for {proj['name']}")
            print(f"   📄 Processing {proj['name']} (offline)...")
            summaries[proj["name"]] = offline_summary(proj, docs)
        else:
            print(f"   🤖 Summarizing {proj['name']} via Claude API...")
            summaries[proj["name"]] = summarize_with_claude(proj, docs, api_key)

    # Generate outputs
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    md_content = generate_markdown(projects, summaries)
    md_path = output_dir / "STATUS_REPORT.md"
    md_path.write_text(md_content)
    print(f"\n📝 Markdown report: {md_path}")

    html_content = generate_html(projects, summaries)
    html_path = output_dir / "dashboard.html"
    html_path.write_text(html_content)
    print(f"🌐 HTML dashboard: {html_path}")

    # Also write raw data
    data_path = output_dir / "pulse_data.json"
    data_path.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "projects": projects,
        "summaries": summaries,
    }, indent=2, default=str))
    print(f"💾 Raw data: {data_path}")

    print(f"\n✅ Done! Open {html_path} in a browser to view the dashboard.")


if __name__ == "__main__":
    main()
