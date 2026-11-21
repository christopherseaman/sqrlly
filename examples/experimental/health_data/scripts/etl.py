import csv
import json
from pathlib import Path

from measures import MEASURES

raw = Path("data/raw")
metadata = json.loads((raw / "source.json").read_text())
columns = {name: name for name in ("county_fips", "county_name", *MEASURES)}
if metadata["release"] != "fixture-1":
    columns = dict(zip(columns, ("countyfips", "countyname", "diabetes_crudeprev", "mhlth_crudeprev")))
out = Path("data/staged/places.jsonl")
out.parent.mkdir(parents=True, exist_ok=True)
with (raw / "places.csv").open(encoding="utf-8-sig", newline="") as source, out.open("w") as dest:
    rows = csv.DictReader(source)
    if not set(columns.values()).issubset(rows.fieldnames or ()):
        raise SystemExit(f"CSV must contain columns {', '.join(columns.values())}")
    for row in rows:
        dest.write(json.dumps({name: row[column] for name, column in columns.items()}) + "\n")
