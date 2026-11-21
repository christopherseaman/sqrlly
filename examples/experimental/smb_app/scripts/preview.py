#!/usr/bin/env python3
"""Render a local, static preview and its artifact handoff manifest."""

import html
import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: preview.py CUSTOMER")
    customer = sys.argv[1]
    quotes = [json.loads(p.read_text()) for p in sorted(Path("build/quotes").glob("*.json"))]
    rows = "\n".join(
        f"<tr><td>{html.escape(q['name'])}</td><td>{q['currency']} {q['total_cents'] / 100:.2f}</td></tr>"
        for q in quotes
    )
    Path("preview").mkdir(exist_ok=True)
    Path("preview/index.html").write_text(
        "<!doctype html><title>Quote</title>"
        f"<h1>Quote for {html.escape(customer)}</h1>"
        f"<table><tr><th>Item</th><th>Total</th></tr>{rows}</table>\n"
    )
    manifest = {
        "customer": customer,
        "artifacts": [
            {"path": "preview/index.html", "kind": "preview"},
            {"path": "build/plan.json", "kind": "plan"},
            {"path": "build/check.json", "kind": "verification"},
        ],
    }
    Path("artifact-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
