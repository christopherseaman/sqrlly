import json
import math
import sys
from html import escape
from pathlib import Path

extraction_path, synthesis_path, chart_path = map(Path, sys.argv[1:])
studies = json.loads(extraction_path.read_text())["studies"]
weights = [1 / row["standard_error"] ** 2 for row in studies]
weight_total = sum(weights)
estimate = sum(row["mean_difference"] * weight for row, weight in zip(studies, weights)) / weight_total
se = math.sqrt(1 / weight_total)
result = {"model": "inverse_variance_fixed_effect", "studies": studies, "estimate": estimate,
          "standard_error": se, "ci95": [estimate - 1.96 * se, estimate + 1.96 * se]}
synthesis_path.write_text(json.dumps(result, indent=2) + "\n")

intervals = [(row["study_id"], row["mean_difference"], row["standard_error"]) for row in studies]
intervals.append(("Pooled", estimate, se))
left = min(0, *(value - 1.96 * error for _, value, error in intervals))
right = max(0, *(value + 1.96 * error for _, value, error in intervals))


def position(value):
    return 100 + 385 * (value - left) / (right - left)


height = 75 + 36 * len(intervals)
zero = position(0)
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 540 {height}" role="img">',
       '<title>Synthetic effects and 95% confidence intervals</title>',
       '<text x="10" y="20">Wellbeing scale points</text>',
       f'<line x1="{zero:.2f}" x2="{zero:.2f}" y1="29" y2="{height - 30}" stroke="#777" stroke-dasharray="4"/>']
for index, (label, value, error) in enumerate(intervals):
    y = 48 + 36 * index
    low, high = position(value - 1.96 * error), position(value + 1.96 * error)
    svg.extend([f'<text x="10" y="{y + 5}">{escape(label)}</text>',
                f'<line x1="{low:.2f}" x2="{high:.2f}" y1="{y}" y2="{y}" stroke="#14532d" stroke-width="3"/>',
                f'<circle cx="{position(value):.2f}" cy="{y}" r="5" fill="#14532d"/>'])
svg.extend([f'<text x="10" y="{height - 10}">Synthetic fixed effect demonstration</text>', '</svg>'])
chart_path.write_text("\n".join(svg) + "\n")
