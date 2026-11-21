import json, sys
from pathlib import Path


search_path, dedup_path, decisions_path, included_path, prisma_path = map(Path, sys.argv[1:])
search = json.loads(search_path.read_text())
dedup = json.loads(dedup_path.read_text())
decisions = json.loads(decisions_path.read_text())
all_records = search["records"]
unique = dedup["records"]
duplicates = dedup["duplicate_records"]
known = {record["id"]: record for record in all_records}
if set(decisions) != set(known):
    raise ValueError("decisions must contain exactly one entry for every search record")
if len(unique) + len(duplicates) != len(all_records):
    raise ValueError("deduplication ledger does not reconcile with search records")
duplicate_ids = {record["id"] for record in duplicates}
unique_ids = {record["id"] for record in unique}
included, excluded = [], []
for record_id, decision in decisions.items():
    record = known[record_id]
    if decision.get("source_url") != record.get("source_url"):
        raise ValueError(f"{record_id} source_url does not match the search record")
    reason = decision.get("reason")
    status = decision.get("decision")
    if status not in {"include", "exclude", "duplicate"} or not reason:
        raise ValueError(f"{record_id} needs decision include, exclude, or duplicate, plus reason")
    if status == "duplicate" and record_id not in duplicate_ids:
        raise ValueError(f"{record_id} is marked duplicate but is not in the duplicate ledger")
    if status != "duplicate" and record_id not in unique_ids:
        raise ValueError(f"{record_id} is in the duplicate ledger but has decision {status}")
    if status == "include":
        included.append({**record, "review": decision})
    elif status == "exclude":
        excluded.append({"id": record_id, "reason": reason})
if not included:
    raise ValueError("screening must include at least one study")
included_path.write_text(json.dumps({"records": included}, indent=2) + "\n")
prisma = {
    "identified": len(all_records),
    "duplicates_removed": len(duplicates),
    "screened": len(unique),
    "excluded": len(excluded),
    "included_studies": len(included),
    "excluded_reasons": excluded,
}
if prisma["identified"] - prisma["duplicates_removed"] != prisma["screened"]:
    raise ValueError("PRISMA identified minus duplicates does not equal screened")
if prisma["screened"] - prisma["excluded"] != prisma["included_studies"]:
    raise ValueError("PRISMA screened minus excluded does not equal included")
prisma_path.write_text(json.dumps(prisma, indent=2) + "\n")
