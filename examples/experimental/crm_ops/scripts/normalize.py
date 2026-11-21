import hashlib
import json
import re
from pathlib import Path

rows = json.loads(Path("artifacts/intake.json").read_text())
seen = {}
duplicates = 0
for row in rows:
    email = row["email"].strip().lower()
    domain = re.sub(r"^https?://", "", row["domain"].strip().lower()).removeprefix("www.").rstrip("/")
    person_id = "person-" + hashlib.sha256(email.encode()).hexdigest()[:16]
    value = {"id": person_id, "email": email, "name": row["name"].strip(), "company": re.sub(r",?\s+(incorporated|inc)\.?$", " Inc", row["company"].strip(), flags=re.I), "domain": domain, "source_ids": [row["id"]]}
    if email in seen:
        seen[email]["source_ids"].append(row["id"])
        duplicates += 1
    else:
        seen[email] = value
normalized = list(seen.values())
Path("artifacts/normalized.json").write_text(json.dumps(normalized, indent=2) + "\n")
print(json.dumps({"input": len(rows), "output": len(normalized), "duplicates": duplicates}))
