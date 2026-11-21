import json
import sqlite3
from pathlib import Path

from target import digest, snapshot

contacts = json.loads(Path("artifacts/normalized.json").read_text())
enrichment = json.loads(Path("artifacts/enrichment.json").read_text())
with sqlite3.connect("target/crm.sqlite3") as db:
    db.execute("BEGIN")
    baseline = snapshot(db, [row["id"] for row in contacts])
proposal = {"contacts": contacts, "enrichment": enrichment, "before": baseline}
proposal_hash = digest(proposal)
Path("artifacts/proposal.json").write_text(json.dumps({"proposal_hash": proposal_hash, **proposal}, indent=2) + "\n")
print(json.dumps({"proposal_hash": proposal_hash, "contacts": len(contacts)}))
