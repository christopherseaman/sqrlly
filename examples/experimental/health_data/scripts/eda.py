import json
from pathlib import Path

from measures import MEASURES, read_rows

missing = dict.fromkeys(MEASURES, 0)
count = 0
for row in read_rows("data/staged/places.jsonl"):
    count += 1
    for key in MEASURES:
        missing[key] += not (row[key] or "").strip()
report = Path("reports/eda.json")
report.parent.mkdir(exist_ok=True)
report.write_text(json.dumps({"rows": count, "missing": missing}, indent=2) + "\n")
