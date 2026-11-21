import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from helpers import init_git_repo
from sqrlly.cli.main import cli


EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "experimental"


def run_example(name, workdir, *args):
    return CliRunner().invoke(cli, ["run", str(EXAMPLES / name / "workflow.yaml"),
                                    "--workdir", str(workdir), "--quiet", *args])


def read_json(path):
    return json.loads(path.read_text())


def test_smb_app_promotes_workers_and_hands_off_preview(tmp_path):
    init_git_repo(tmp_path, files={"README.md": "Quote demo"})
    log = tmp_path / "run.jsonl"
    run = run_example("smb_app", tmp_path, "--var", "customer=Northwind & Bikes", "--var", "currency=USD",
                      "--log", str(log))
    assert run.exit_code == 0, run.output
    assert read_json(tmp_path / "build/check.json") == {"quote_count": 2, "errors": [], "passed": True}
    assert read_json(tmp_path / "build/quotes/setup.json")["subtotal_cents"] == 12000
    assert read_json(tmp_path / "build/quotes/support.json")["subtotal_cents"] == 4500
    assert "Northwind &amp; Bikes" in (tmp_path / "preview/index.html").read_text()
    manifest = read_json(tmp_path / "artifact-manifest.json")["artifacts"]
    assert {artifact["path"] for artifact in manifest} == {"preview/index.html", "build/plan.json", "build/check.json"}
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert {event["node"] for event in events if event["event"] == "node_completed"} >= {
        "quotes::setup", "quotes::support", "verify", "preview",
    }


def test_crm_rejects_wrong_review_and_reapplies_idempotently(tmp_path):
    pending = run_example("crm_ops", tmp_path)
    assert pending.exit_code == 1 and "approval.json is required" in pending.output
    db_path = tmp_path / "target/crm.sqlite3"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM contacts").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM applied_changes").fetchone() == (0,)

    approval = tmp_path / "approval.json"
    approval.write_text('{"approved": true, "proposal_hash": "wrong"}')
    wrong = run_example("crm_ops", tmp_path, "--resume")
    assert wrong.exit_code == 1 and "does not approve this proposal hash" in wrong.output

    proposal = read_json(tmp_path / "artifacts/proposal.json")
    approval.write_text(json.dumps({"approved": True, "proposal_hash": proposal["proposal_hash"]}))
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE contacts SET name = 'Edited after review'")
    stale = run_example("crm_ops", tmp_path, "--resume")
    assert stale.exit_code == 1 and "target baseline changed" in stale.output
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT name FROM contacts").fetchall() == [("Edited after review",)]
        db.execute("UPDATE contacts SET name = 'Alice Smith'")

    approved = run_example("crm_ops", tmp_path, "--resume")
    assert approved.exit_code == 0, approved.output
    report = tmp_path / "artifacts/apply-report.json"
    assert read_json(report) == {"proposal_hash": proposal["proposal_hash"], "status": "applied",
                                 "contacts": 2, "enrichment": 2}
    replay = run_example("crm_ops", tmp_path, "--resume-from", "apply")
    assert replay.exit_code == 0, replay.output
    assert read_json(report)["status"] == "already_applied"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM contacts").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM enrichment").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM applied_changes").fetchone() == (1,)


def test_health_stages_external_bytes_and_reports_missingness(tmp_path):
    source = tmp_path / "county CSV.csv"
    source.write_text("countyfips,countyname,diabetes_crudeprev,mhlth_crudeprev\n"
                      "01001,Sample A,12,20\n01003,Sample B,8,\n01005,Sample C,10,10\n")
    output = tmp_path / "work"
    output.mkdir()
    run = run_example("health_data", output, "--var", f"input={source}", "--var", "source_query=sample")
    assert run.exit_code == 0, run.output
    digest = read_json(output / "data/raw/digest.json")
    assert digest["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert digest["source_query"] == "sample"
    assert read_json(output / "reports/analysis.json")["measures"] == {
        "diabetes_pct": {"n": 3, "min_pct": 8.0, "max_pct": 12.0, "mean_county_pct": 10.0},
        "mental_health_not_good_pct": {"n": 2, "min_pct": 10.0, "max_pct": 20.0, "mean_county_pct": 15.0},
    }
    assert "| Frequent mental distress | 15.0% | 10.0% to 20.0% | 2 | 1 |" in (output / "site/index.md").read_text()
    assert (output / "site/plot.svg").read_bytes() == (output / "reports/prevalence.svg").read_bytes()

    source.write_text("countyfips,countyname,diabetes_crudeprev,mhlth_crudeprev\n01001,Sample A,101,20\n")
    malformed = tmp_path / "bad"
    malformed.mkdir()
    bad_run = run_example("health_data", malformed, "--var", f"input={source}")
    assert bad_run.exit_code == 1 and "must be between 0 and 100" in bad_run.output
    assert not (malformed / "site/index.md").exists()


def test_research_resumes_with_reviewed_evidence(tmp_path):
    pending = run_example("research_synthesis", tmp_path)
    assert pending.exit_code == 1 and "decisions.reviewed.json" in pending.output
    assert read_json(tmp_path / "output/scout.json")["candidate_count"] == 4
    frozen = (tmp_path / "output/search.json").read_bytes()
    provenance = read_json(tmp_path / "output/provenance.json")
    assert provenance["search_sha256"] == hashlib.sha256(frozen).hexdigest()
    reviewed = tmp_path / "decisions.reviewed.json"
    decisions = read_json(EXAMPLES / "research_synthesis/decisions.fixture.json")
    excluded = decisions.pop("r4")
    reviewed.write_text(json.dumps(decisions))
    incomplete = run_example("research_synthesis", tmp_path, "--resume")
    assert incomplete.exit_code == 1 and "exactly one entry for every search record" in incomplete.output
    assert not (tmp_path / "output/manuscript.md").exists()

    decisions["r4"] = excluded
    reviewed.write_text(json.dumps(decisions))
    finished = run_example("research_synthesis", tmp_path, "--resume")
    assert finished.exit_code == 0, finished.output
    counts = read_json(tmp_path / "output/prisma.json")
    assert {k: counts[k] for k in ("identified", "duplicates_removed", "screened", "excluded", "included_studies")} == {
        "identified": 4, "duplicates_removed": 1, "screened": 3, "excluded": 1, "included_studies": 2,
    }
    assert counts["excluded_reasons"] == [{"id": "r4", "reason": excluded["reason"]}]
    result = read_json(tmp_path / "output/synthesis.json")
    assert result["estimate"] == 3.0 and result["ci95"] == pytest.approx([1.61407, 4.38593])
    figure = (tmp_path / "output/forest.svg").read_text()
    assert all(f">{label}</text>" in figure for label in ("r1", "r3", "Pooled"))
