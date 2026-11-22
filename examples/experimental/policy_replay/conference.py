import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from replay import EXAMPLE_DIR, load_inputs, parse_json, replay, select_policies, write_report


ROSTER = ("east", "west")
MAX_DECISION_AGE = timedelta(hours=24)
POSITIONS = ("approve", "withhold")


def rule_digest(policy):
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def proposal(policies, candidate):
    return {
        "candidate": candidate,
        "baseline_sha256": rule_digest(policies["baseline"]),
        "candidate_sha256": rule_digest(policies[candidate]),
        "evaluator_sha256": hashlib.sha256((EXAMPLE_DIR / "replay.py").read_bytes()).hexdigest(),
    }


def issue_decision(args):
    if not args.dataset:
        raise ValueError("a member dataset directory is required")
    inputs, digests = load_inputs(Path(args.dataset), args.policies)
    report = replay(inputs, digests, args.candidate)
    write_report("replay-report.json", report)
    if args.position == "approve" and report["verdict"] != "go":
        raise ValueError("a local hold permits only a withhold decision")
    write_report("member-decision.json", {
        "member": args.member,
        "rehearsal": args.rehearsal,
        **proposal(report["policies"], args.candidate),
        "local_verdict": report["verdict"],
        "position": args.position,
        "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def inspect_decision(path, member, rehearsal, expected, now):
    if not path:
        return {"status": "missing"}
    try:
        receipt = parse_json(Path(path).read_bytes())
    except FileNotFoundError:
        return {"status": "missing"}
    except (OSError, ValueError):
        return {"status": "invalid"}
    fields = {
        "member", "rehearsal", *expected, "local_verdict", "position", "issued_at",
    }
    if not isinstance(receipt, dict) or receipt.keys() != fields:
        return {"status": "invalid"}
    if any(not isinstance(value, str) for value in receipt.values()):
        return {"status": "invalid"}
    if receipt["member"] != member:
        return {"status": "member_mismatch"}
    if receipt["rehearsal"] != rehearsal:
        return {"status": "rehearsal_mismatch"}
    if any(receipt[key] != value for key, value in expected.items()):
        return {"status": "proposal_mismatch"}
    try:
        issued_at = datetime.fromisoformat(receipt["issued_at"])
    except ValueError:
        return {"status": "invalid"}
    if issued_at.utcoffset() != timedelta(0) or issued_at > now:
        return {"status": "invalid"}
    if now - issued_at > MAX_DECISION_AGE:
        return {"status": "stale"}
    local = receipt["local_verdict"]
    position = receipt["position"]
    if local not in ("go", "hold") or position not in POSITIONS:
        return {"status": "invalid"}
    if local == "hold" and position == "approve":
        return {"status": "invalid"}
    status = "approved" if position == "approve" else "withheld"
    return {"status": status, "local_verdict": local, "position": position}


def tally(args):
    policies = select_policies(parse_json(args.policies.read_bytes()), args.candidate)
    expected = proposal(policies, args.candidate)
    now = datetime.now(timezone.utc)
    members = {
        member: inspect_decision(
            getattr(args, f"{member}_decision"), member, args.rehearsal, expected, now
        )
        for member in ROSTER
    }
    unanimous = all(decision["status"] == "approved" for decision in members.values())
    write_report("conference-report.json", {
        "rehearsal": args.rehearsal,
        **expected,
        "members": members,
        "verdict": "go" if unanimous else "hold",
    })


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    member = commands.add_parser("member")
    member.add_argument("--member", choices=ROSTER, required=True)
    member.add_argument("--dataset", required=True)
    member.add_argument("--position", choices=POSITIONS, required=True)
    conference = commands.add_parser("conference")
    for name in ROSTER:
        conference.add_argument(f"--{name}-decision", default="")
    for command in (member, conference):
        command.add_argument("--policies", required=True, type=Path)
        command.add_argument("--candidate", required=True)
        command.add_argument("--rehearsal", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.rehearsal):
        parser.error("--rehearsal must be a 1 to 64 character identifier")
    try:
        if args.command == "member":
            issue_decision(args)
        else:
            tally(args)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Library rehearsal failed: {exc}\n")


if __name__ == "__main__":
    main()
