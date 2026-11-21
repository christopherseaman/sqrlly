import json, sys
from pathlib import Path

source, destination = map(Path, sys.argv[1:])
data = json.loads(source.read_text())
seen, records, duplicates = set(), [], []
for record in data["records"]:
    if record["doi"] not in seen:
        seen.add(record["doi"])
        records.append(record)
    else:
        duplicates.append(record)
destination.write_text(json.dumps({"records": records, "duplicate_records": duplicates}, indent=2) + "\n")
