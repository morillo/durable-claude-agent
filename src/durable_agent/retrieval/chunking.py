"""Split Markdown into heading-scoped chunks that can be cited exactly.

A chunk is everything under one heading up to the next heading of any level. Each chunk
carries its full heading path (``"Query shape rules > Read-only"``), which is the same string
``risk.py`` uses in ``SECTIONS``. That shared identifier is what lets the final answer cite a
governance section whether it came from retrieval or from the deterministic classifier.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str  # path relative to the governance folder, e.g. "policies.md"
    section: str  # full heading path, " > " separated, excluding the H1 title
    heading: str  # leaf heading text
    anchor: str  # GitHub-style slug of the leaf heading
    text: str  # body under the heading (heading line excluded)

    @property
    def embed_text(self) -> str:
        """What gets embedded: the path gives context the body alone lacks."""
        return f"{self.section}\n{self.text}".strip()


def slugify(heading: str) -> str:
    s = heading.strip().lower()
    s = re.sub(r"[`*]", "", s)  # GitHub keeps underscores in anchors
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s+", "-", s).strip("-")


def chunk_markdown(text: str, source: str) -> list[Chunk]:
    """Return one chunk per heading with non-empty body.

    The H1 document title is dropped from the section path since every chunk in a file shares
    it; the file name already identifies the document.
    """
    chunks: list[Chunk] = []
    path: list[tuple[int, str]] = []  # (level, heading)
    body: list[str] = []
    in_code = False

    def flush() -> None:
        if not path:
            body.clear()
            return
        content = "\n".join(body).strip()
        body.clear()
        if not content:
            return
        section_parts = [h for lvl, h in path if lvl > 1]
        if not section_parts:
            return  # text directly under the H1 title: preamble, not a citable rule
        section = " > ".join(section_parts)
        leaf = section_parts[-1]
        chunk_id = hashlib.sha1(f"{source}::{section}".encode()).hexdigest()[:16]  # noqa: S324
        chunks.append(Chunk(chunk_id, source, section, leaf, slugify(leaf), content))

    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            body.append(line)
            continue
        m = None if in_code else _HEADING.match(line)
        if not m:
            body.append(line)
            continue
        flush()
        level, heading = len(m.group(1)), m.group(2).strip()
        while path and path[-1][0] >= level:
            path.pop()
        path.append((level, heading))
    flush()
    return chunks


def load_corpus(governance_dir: Path) -> list[Chunk]:
    """Chunk every ``*.md`` under the governance folder, in sorted file order."""
    chunks: list[Chunk] = []
    for md in sorted(Path(governance_dir).glob("*.md")):
        chunks.extend(chunk_markdown(md.read_text(encoding="utf-8"), md.name))
    return chunks
