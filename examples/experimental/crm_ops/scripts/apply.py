import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from target import digest, snapshot

proposal = json.loads(Path("artifacts/proposal.json").read_text())
approval_path = Path("approval.json")
if not approval_path.is_file():
    raise SystemExit("approval.json is required after operator inspection")
approval = json.loads(approval_path.read_text())
expected = proposal.pop("proposal_hash")
if expected != digest(proposal) or approval.get("approved") is not True or approval.get("proposal_hash") != expected:
    raise SystemExit("approval.json does not approve this proposal hash")
contacts = proposal["contacts"]
enrichment = proposal["enrichment"]
ids = [row["id"] for row in contacts]
if len(ids) != len(set(ids)) or sorted(ids) != sorted(row["contact_id"] for row in enrichment):
    raise SystemExit("proposal must contain one contact and enrichment record per ID")

with sqlite3.connect("target/crm.sqlite3") as db:
    db.execute("BEGIN IMMEDIATE")
    if db.execute("SELECT 1 FROM applied_changes WHERE proposal_hash = ?", (expected,)).fetchone():
        status = "already_applied"
    else:
        if snapshot(db, ids) != proposal["before"]:
            raise SystemExit("target baseline changed after proposal; refusing to mutate")
        for row in contacts:
            db.execute("""
                INSERT INTO contacts VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET email=excluded.email, name=excluded.name,
                    company=excluded.company, domain=excluded.domain
                """, (row["id"], row["email"], row["name"], row["company"], row["domain"]))
        for row in enrichment:
            db.execute("""
                INSERT INTO enrichment VALUES (?, ?, ?, ?)
                ON CONFLICT(contact_id) DO UPDATE SET fields_json=excluded.fields_json,
                    source=excluded.source, observed_at=excluded.observed_at
                """, (row["contact_id"], json.dumps(row["fields"], sort_keys=True), row["source"], row["observed_at"]))
        db.execute("INSERT INTO applied_changes VALUES (?, ?)", (expected, datetime.now(timezone.utc).isoformat()))
        status = "applied"

result = {"proposal_hash": expected, "status": status, "contacts": len(contacts), "enrichment": len(enrichment)}
Path("artifacts/apply-report.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
