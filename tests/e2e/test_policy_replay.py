import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from sqrlly.cli.main import cli


EXAMPLE = Path(__file__).resolve().parents[2] / "examples/experimental/policy_replay"


def run(workflow, workdir, **variables):
    workdir.mkdir()
    args = ["run", str(EXAMPLE / workflow), "--workdir", str(workdir), "--quiet"]
    for name, value in variables.items():
        args.extend(["--var", f"{name}={value}"])
    return CliRunner().invoke(cli, args)


@pytest.mark.parametrize(("candidate", "exit_code", "matches", "ship", "verdict"), [
    ("favor_standard", 1, 7, 1, "hold"),
    ("protect_access", 0, 9, 3, "go"),
])
def test_replay_pairs_same_requests_and_preserves_reasons(tmp_path, candidate, exit_code, matches, ship, verdict):
    work = tmp_path / "comparison"
    result = run("workflow.yaml", work, candidate=candidate)
    assert result.exit_code == exit_code, result.output
    report = json.loads((work / "replay-report.json").read_bytes())
    assert report["verdict"] == verdict
    assert report["review_agreement"] == {"baseline": 5, "candidate": matches, "denominator": 9}
    assert report["priority_service"]["baseline"] == 2
    assert report["priority_service"]["candidate"] == ship
    assert report["priority_service"]["denominator"] == 3
    assert report["coverage"]["unreviewed_ids"] == ["P04", "S07"]
    assert report["future_outcomes"] == "unknown"
    assert report["input_sha256"]["requests.json"] == hashlib.sha256((EXAMPLE / "requests.json").read_bytes()).hexdigest()
    changed = {case["id"]: case for case in report["changed_decisions"]}
    if verdict == "hold":
        assert set(report["hold_reasons"]) == {
            "reviewed_priority_service_declined", "unreviewed_priority_shipments_removed",
        }
        assert changed["P04"]["review"] is None
        assert (changed["P04"]["baseline"], changed["P04"]["candidate"]) == ("ship", "do_not_ship")
    else:
        assert report["hold_reasons"] == []
        assert set(changed) == {"P03", "S03", "S04", "S05", "S07"}


def test_members_control_consent_for_one_proposal_and_round(tmp_path):
    proposal = {"candidate": "protect_access", "rehearsal": "november-round"}
    receipts = {}
    for member, dataset, position in [
        ("east", EXAMPLE, "approve"),
        ("west", EXAMPLE / "west_cohort", "approve"),
        ("west", EXAMPLE / "west_cohort", "withhold"),
    ]:
        work = tmp_path / f"{member}-{position}"
        result = run("member.yaml", work, member=member, dataset=dataset, position=position, **proposal)
        assert result.exit_code == 0, result.output
        path = work / "member-decision.json"
        receipt = json.loads(path.read_bytes())
        assert set(receipt) == {
            "member", "position", "candidate", "rehearsal", "issued_at", "local_verdict",
            "baseline_sha256", "candidate_sha256", "evaluator_sha256",
        }
        assert (receipt["member"], receipt["local_verdict"], receipt["position"]) == (member, "go", position)
        receipts[(member, position)] = path

    east = receipts["east", "approve"]
    west = receipts["west", "approve"]
    withheld = receipts["west", "withhold"]
    for label, west_path, round_name, status, success in [
        ("agree", west, "november-round", "approved", True),
        ("decline", withheld, "november-round", "withheld", False),
        ("absent", "", "november-round", "missing", False),
        ("duplicate", east, "november-round", "member_mismatch", False),
        ("superseded-round", west, "later-round", "rehearsal_mismatch", False),
    ]:
        tally = tmp_path / label
        result = run("conference.yaml", tally, east_decision=east, west_decision=west_path,
                     candidate=proposal["candidate"], rehearsal=round_name)
        assert result.exit_code == (0 if success else 1), result.output
        report = json.loads((tally / "conference-report.json").read_bytes())
        assert report["members"]["west"]["status"] == status
        assert report["verdict"] == ("go" if success else "hold")

    policies = json.loads((EXAMPLE / "policies.json").read_bytes())
    policies["baseline"]["standard_max_days"] += 1
    updated = tmp_path / "edited-baseline.json"
    updated.write_text(json.dumps(policies))
    tally = tmp_path / "changed-proposal"
    result = run("conference.yaml", tally, east_decision=east, west_decision=west,
                 policies=updated, **proposal)
    assert result.exit_code == 1, result.output
    report = json.loads((tally / "conference-report.json").read_bytes())
    assert {member["status"] for member in report["members"].values()} == {"proposal_mismatch"}
    assert report["verdict"] == "hold"
