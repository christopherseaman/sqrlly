import json
from pathlib import Path

rows = json.loads(Path("artifacts/normalized.json").read_text())
directory = {"acme.com": {"industry": "software", "employee_range": "51-200"}, "beta.test": {"industry": "manufacturing", "employee_range": "11-50"}}
provenance = [{"contact_id": row["id"], "fields": directory[row["domain"]], "source": "fixture://trusted-company-directory", "observed_at": "2026-01-01"} for row in rows]
Path("artifacts/enrichment.json").write_text(json.dumps(provenance, indent=2) + "\n")
print(json.dumps({"records": len(provenance), "source": "fixture://trusted-company-directory"}))
