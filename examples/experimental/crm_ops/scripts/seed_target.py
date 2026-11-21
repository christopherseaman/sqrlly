import hashlib
import sqlite3
from pathlib import Path

path = Path("target/crm.sqlite3")
path.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(path) as db:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS contacts (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, company TEXT NOT NULL, domain TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS enrichment (contact_id TEXT PRIMARY KEY, fields_json TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS applied_changes (proposal_hash TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
    """)
    person_id = "person-" + hashlib.sha256(b"alice@example.com").hexdigest()[:16]
    db.execute("INSERT OR IGNORE INTO contacts VALUES (?, ?, ?, ?, ?)", (person_id, "alice@example.com", "Alice Smith", "Acme Inc", "acme.com"))
print('{"target":"target/crm.sqlite3","baseline":"ready"}')
