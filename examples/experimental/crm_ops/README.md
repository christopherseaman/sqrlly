# Experimental CRM operations

This local-only example turns synthetic contacts into a reviewable, idempotent SQLite change.
It demonstrates stable intake IDs, normalization, duplicate collapse, separate enrichment provenance, before/after target rows, a proposal hash, manual approval, and transactional reapplication.

The example has no network adapter and does not represent live CRM support.

## Fresh run

From the repository root:

```bash
project=$PWD
scratch=$(mktemp -d)
cp -R examples/experimental/crm_ops/. "$scratch/"
uv run --project "$project" sqrlly validate "$scratch/workflow.yaml"
uv run --project "$project" sqrlly run "$scratch/workflow.yaml" --workdir "$scratch" --quiet --log "$scratch/first.jsonl" || test $? -eq 1
cat "$scratch/artifacts/proposal.json"
```

The first run stops at `apply` because no approval exists. Inspect `artifacts/proposal.json`, then create `approval.json` with its exact `proposal_hash`:

```json
{"approved": true, "proposal_hash": "PASTE_THE_EXACT_VALUE_FROM_artifacts/proposal.json", "approved_by": "operator", "approved_at": "2026-01-01T00:00:00Z"}
```

Resume the checkpoint:

```bash
uv run --project "$project" sqrlly run "$scratch/workflow.yaml" --workdir "$scratch" --quiet --resume --log "$scratch/approved.jsonl"
cat "$scratch/artifacts/apply-report.json"
```

Expected report shape:

```json
{"contacts": 2, "enrichment": 2, "proposal_hash": "<64 hex characters>", "status": "applied"}
```

The intake has 3 rows, normalization produces 2 contacts, and one duplicate source ID remains attached to Alice. The enrichment file keeps source and observation time separate from contact fields.

## Reapply

```bash
uv run --project "$project" sqrlly run "$scratch/workflow.yaml" --workdir "$scratch" --quiet --resume --rerun-all --log "$scratch/reapply.jsonl"
cat "$scratch/artifacts/apply-report.json"
```

The intentional replay command is `--resume-from apply`, which keeps the approved proposal artifact and produces the same hash with `"status": "already_applied"`; SQLite's idempotency ledger prevents duplicate application.

An approval with the wrong hash fails before the SQLite transaction writes. The proposal's `before` map contains the contact and enrichment rows for each target. To exercise the baseline check, edit a contact in `target/crm.sqlite3` after proposal creation and before approval, then resume. Apply compares those rows under the write transaction and reports `target baseline changed after proposal; refusing to mutate`.

## Extension path

An external CRM adapter should replace only the local SQLite apply boundary. It must preserve proposal hashes, approval checks, idempotency keys, unique-field upserts, relationship ordering, pagination and rate-limit backoff, a before/after ledger, and secret handling. Credentials belong in environment or a secret manager, never in YAML, fixtures, prompts, or approval files.
