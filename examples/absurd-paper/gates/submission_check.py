#!/usr/bin/env python3
"""Structural integrity gate for the composed paper.

Reads `output/paper.md` from the working directory and validates that reconcile produced
a well-formed journal submission:
  - an H1 title
  - an Abstract section
  - all four body sections (Introduction, Methods, Results, Discussion)
  - a Conclusion section
  - a References section with at least 3 bulleted entries

The command exits 1 when a required section or reference is missing.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REQUIRED_SECTIONS = [
    "Abstract",
    "Introduction",
    "Methods",
    "Results",
    "Discussion",
    "Conclusion",
    "References",
]


def main() -> int:
    paper = Path("output/paper.md")

    if not paper.exists():
        print(f"Missing {paper}")
        return 1

    text = paper.read_text()

    unmet: list[str] = []

    # H1 title
    if not re.search(r"^# .+", text, re.MULTILINE):
        unmet.append("missing H1 title")

    # H2 sections
    h2_headers = {m.group(1).strip() for m in re.finditer(r"^## (.+)$", text, re.MULTILINE)}
    for section in REQUIRED_SECTIONS:
        if section not in h2_headers:
            unmet.append(f"missing section: {section}")

    # References: at least 3 bulleted entries after the References header
    refs_match = re.search(r"^## References\s*\n(.*)", text, re.MULTILINE | re.DOTALL)
    if refs_match:
        refs_block = refs_match.group(1)
        bullets = re.findall(r"^[-*] ", refs_block, re.MULTILINE)
        if len(bullets) < 3:
            unmet.append(f"references has {len(bullets)} entries, need ≥3")
    # (missing-References already captured above)

    print(json.dumps({"missing": unmet}))
    return int(bool(unmet))


if __name__ == "__main__":
    sys.exit(main())
