import json
from pathlib import Path

from measures import MEASURES

digest = json.loads(Path("data/raw/digest.json").read_text())
eda = json.loads(Path("reports/eda.json").read_text())
analysis = json.loads(Path("reports/analysis.json").read_text())
lines = []
for key, label in MEASURES.items():
    stats = analysis["measures"][key]
    lines.append(f"| {label} | {stats['mean_county_pct']:.1f}% | {stats['min_pct']:.1f}% to {stats['max_pct']:.1f}% | {stats['n']} | {eda['missing'][key]} |")
table = "\n".join(lines)
body = f"""---
title: County health estimates
layout: base.njk
---

# County health estimates

The input contains {eda['rows']} county rows from the {digest['source_kind']}.
The recorded release is `{digest['release']}` and the source SHA-256 is `{digest['sha256']}`.

| Measure | Mean county estimate | Range | Present | Missing |
| --- | --- | --- | --- | --- |
{table}

![Mean county percentages by measure](plot.svg)

## Methods and limits

The arithmetic mean gives each county with an available estimate equal weight.
The summary describes the input counties. It cannot estimate the prevalence among their combined residents.
CDC PLACES estimates use small area models and carry uncertainty. This example draws no individual, causal, or trend conclusions.

[CDC PLACES methodology]({digest['methodology_reference']})
"""
Path("site/index.md").write_text(body)
