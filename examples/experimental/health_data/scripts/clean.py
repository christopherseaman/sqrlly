import json
import re
from pathlib import Path

from measures import MEASURES, read_rows

out = Path("data/clean/places.jsonl")
out.parent.mkdir(parents=True, exist_ok=True)
seen = set()
with out.open("w") as dest:
    for line, row in enumerate(read_rows("data/staged/places.jsonl"), start=2):
        fips = (row["county_fips"] or "").strip()
        name = (row["county_name"] or "").strip()
        if not re.fullmatch(r"[0-9]{5}", fips) or not name or fips in seen:
            raise SystemExit(f"CSV row {line}: invalid or repeated county identity")
        seen.add(fips)
        clean = {"county_fips": fips, "county_name": name}
        for key in MEASURES:
            text = (row[key] or "").strip()
            if not text:
                clean[key] = None
                continue
            try:
                value = float(text)
            except ValueError:
                raise SystemExit(f"CSV row {line}: {key} must be a percentage") from None
            if not 0 <= value <= 100:
                raise SystemExit(f"CSV row {line}: {key} must be between 0 and 100")
            clean[key] = value
        dest.write(json.dumps(clean) + "\n")
if not seen:
    raise SystemExit("CSV contains no counties")
