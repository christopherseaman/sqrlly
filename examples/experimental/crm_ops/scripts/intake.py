import json
import re
import sys
from pathlib import Path

rows = json.loads(Path(sys.argv[1]).read_text())
if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
    raise SystemExit("intake must be a list of objects")
ids = [row.get("id") for row in rows]
if any(not value or not isinstance(value, str) for value in ids):
    raise SystemExit("intake records require nonempty string id values")
if len(ids) != len(set(ids)):
    raise SystemExit("intake contains duplicate id values")
required = ("id", "email", "name", "company", "domain")
if any(not isinstance(row.get(key), str) or not row[key].strip() for row in rows for key in required):
    raise SystemExit("intake records require nonempty id, email, name, company, and domain strings")
if any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", row["email"].strip()) for row in rows):
    raise SystemExit("intake records require basic email addresses")
Path("artifacts").mkdir(exist_ok=True)
Path("artifacts/intake.json").write_text(json.dumps(rows, indent=2) + "\n")
print(json.dumps({"records": len(rows), "stable_ids": True}))
