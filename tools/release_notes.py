"""Extract one release from the shared changelog for GitHub publishing."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def extract_release_notes(changelog: str, version: str) -> str:
    """Return the requested release section, excluding unreleased changes."""
    heading = re.search(rf"^## {re.escape(version)}(?: —[^\n]*)?$", changelog, re.MULTILINE)
    if heading is None:
        raise ValueError(f"Missing changelog section for {version}")
    end = re.search(r"^## ", changelog[heading.end() :], re.MULTILINE)
    section_end = heading.end() + end.start() if end else len(changelog)
    return changelog[heading.start() : section_end].strip() + "\n"


def main() -> None:
    """Write release notes without maintaining a duplicate Markdown document."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=Path(".local/release-notes.md"))
    args = parser.parse_args()
    notes = extract_release_notes(Path("CHANGELOG.md").read_text(encoding="utf-8"), args.version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(notes, encoding="utf-8")


if __name__ == "__main__":
    main()
