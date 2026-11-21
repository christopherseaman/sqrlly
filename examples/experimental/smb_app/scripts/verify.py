#!/usr/bin/env python3
"""Check every quote line and write a machine-readable integration result."""

import json
from pathlib import Path


def main() -> None:
    plan = json.loads(Path("build/plan.json").read_text())
    files = sorted(Path("build/quotes").glob("*.json"))
    expected = {item["id"] for item in plan["items"]}
    errors = []
    if {file.stem for file in files} != expected:
        errors.append("quote files do not match the plan items")
    for path in files:
        quote = json.loads(path.read_text())
        if quote["subtotal_cents"] + quote["tax_cents"] != quote["total_cents"]:
            errors.append(f"{path}: total mismatch")
    result = {"quote_count": len(files), "errors": errors, "passed": not errors and bool(files)}
    Path("build/check.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    if errors or not files:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
