"""Render every ```mermaid block in the tracked Markdown files and fail on the first that breaks.

``make docs-check`` runs this; CI runs it on every pull request and push. GitHub renders these
diagrams in the browser and shows only "Unable to render rich display" when one fails to parse,
so a broken diagram is otherwise invisible until someone opens the page.

Needs Node (for ``npx``) and Playwright's Chromium (``uv run playwright install chromium``),
which mermaid-cli drives instead of downloading its own browser.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
MERMAID_CLI = "@mermaid-js/mermaid-cli@11.4.2"
BLOCK = re.compile(r"^```mermaid\n(.*?)^```", re.S | re.M)


def main() -> int:
    with sync_playwright() as pw:
        chromium = pw.chromium.executable_path
    files = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp, "puppeteer.json")
        config.write_text(json.dumps({"executablePath": chromium, "args": ["--no-sandbox"]}))
        for name in files:
            text = (ROOT / name).read_text()
            for match in BLOCK.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                src, out = Path(tmp, "d.mmd"), Path(tmp, "d.svg")
                src.write_text(match.group(1))
                cmd = ["npx", "-y", MERMAID_CLI, "-q", "-p", str(config), "-i", str(src)]
                done = subprocess.run(
                    [*cmd, "-o", str(out)],
                    capture_output=True,
                    text=True,
                    env={**os.environ, "PUPPETEER_SKIP_DOWNLOAD": "1"},
                )
                if done.returncode == 0:
                    print(f"ok    {name}:{line}")
                else:
                    failures += 1
                    error = next(
                        (ln for ln in done.stderr.splitlines() if "Error" in ln), done.stderr[-300:]
                    )
                    print(f"FAIL  {name}:{line}  {error.strip()}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
