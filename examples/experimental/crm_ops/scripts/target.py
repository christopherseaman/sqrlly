import hashlib
import json


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot(db, ids):
    contacts = {}
    for person_id in ids:
        person = db.execute(
            "SELECT id, email, name, company, domain FROM contacts WHERE id = ?", (person_id,),
        ).fetchone()
        enrichment = db.execute(
            "SELECT fields_json, source, observed_at FROM enrichment WHERE contact_id = ?", (person_id,),
        ).fetchone()
        contacts[person_id] = {
            "contact": dict(zip(("id", "email", "name", "company", "domain"), person)) if person else None,
            "enrichment": {"fields": json.loads(enrichment[0]), "source": enrichment[1],
                           "observed_at": enrichment[2]} if enrichment else None,
        }
    return contacts
