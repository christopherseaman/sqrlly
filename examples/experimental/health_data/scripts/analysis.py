import json
from pathlib import Path

from measures import MEASURES, read_rows

stats = {key: {"n": 0, "sum": 0.0, "min_pct": 100.0, "max_pct": 0.0} for key in MEASURES}
count = 0
for row in read_rows("data/clean/places.jsonl"):
    count += 1
    for key, summary in stats.items():
        rate = row[key]
        if rate is not None:
            summary["n"] += 1
            summary["sum"] += rate
            summary["min_pct"] = min(rate, summary["min_pct"])
            summary["max_pct"] = max(rate, summary["max_pct"])
for key, summary in stats.items():
    if summary["n"] == 0:
        raise SystemExit(f"no county estimate present for {key}")
    summary["mean_county_pct"] = summary.pop("sum") / summary["n"]

report = Path("reports")
report.mkdir(exist_ok=True)
(report / "analysis.json").write_text(json.dumps({"county_rows": count, "measures": stats}, indent=2) + "\n")
svg = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 520 160" role="img">',
       '<title>Mean county percentages by measure</title>']
for y, (key, label) in enumerate(MEASURES.items()):
    value = stats[key]["mean_county_pct"]
    top = y * 70 + 28
    svg.append(f'<text x="10" y="{top}">{label} {value:.1f}%</text>')
    svg.append(f'<rect x="10" y="{top + 8}" width="{value * 5:.2f}" height="18" fill="#1e6b55"/>')
svg.append('</svg>')
chart = "\n".join(svg) + "\n"
(report / "prevalence.svg").write_text(chart)
Path("site").mkdir(exist_ok=True)
Path("site/plot.svg").write_text(chart)
