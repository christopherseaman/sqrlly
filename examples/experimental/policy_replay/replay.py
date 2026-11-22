import argparse
import hashlib
import json
from pathlib import Path


EXAMPLE_DIR = Path(__file__).resolve().parent


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def parse_json(content):
    return json.loads(content, object_pairs_hook=unique_object)


def load_inputs(dataset=EXAMPLE_DIR, policies_path=EXAMPLE_DIR / "policies.json"):
    inputs = {}
    digests = {}
    sources = {
        "requests": dataset / "requests.json",
        "reviews": dataset / "reviews.json",
        "policies": policies_path,
    }
    for name, source in sources.items():
        content = source.read_bytes()
        inputs[name] = parse_json(content)
        digests[f"{name}.json"] = hashlib.sha256(content).hexdigest()
    return inputs, digests


def index_requests(requests):
    by_id = {}
    if not isinstance(requests, list) or not requests:
        raise ValueError("requests.json must contain a nonempty request list")
    for request in requests:
        if not isinstance(request, dict):
            raise ValueError("each request must be an object")
        case_id = request.get("id")
        days = request.get("estimated_transfer_days")
        if not isinstance(case_id, str) or not case_id or case_id in by_id:
            raise ValueError(f"missing or duplicate case ID: {case_id!r}")
        if type(days) is not int or days < 0:
            raise ValueError(f"{case_id}: estimated_transfer_days must be a nonnegative integer")
        for field in ("limited_local_access", "source_copy_available"):
            if type(request.get(field)) is not bool:
                raise ValueError(f"{case_id}: {field} must be a boolean")
        by_id[case_id] = request
    return by_id


def validate_policy(name, policy):
    fields = {"standard_max_days", "limited_access_max_days"}
    if not isinstance(policy, dict) or policy.keys() != fields:
        raise ValueError(f"{name}: expected {', '.join(sorted(fields))}")
    for field, value in policy.items():
        if type(value) is not int or value < 0:
            raise ValueError(f"{name}: {field} must be a nonnegative integer")


def decide(request, policy):
    limit = "limited_access_max_days" if request["limited_local_access"] else "standard_max_days"
    if request["source_copy_available"] and request["estimated_transfer_days"] <= policy[limit]:
        return "ship"
    return "do_not_ship"


def select_policies(policies, candidate):
    if not isinstance(policies, dict) or candidate == "baseline" or candidate not in policies:
        raise ValueError(f"expected a named candidate in policies.json, got {candidate!r}")
    selected = {name: policies.get(name) for name in ("baseline", candidate)}
    for name, policy in selected.items():
        validate_policy(name, policy)
    return selected


def replay(inputs, digests, candidate):
    requests = index_requests(inputs["requests"])
    reviews = inputs["reviews"]
    if not isinstance(reviews, dict) or any(
        case_id not in requests or decision not in ("ship", "do_not_ship")
        for case_id, decision in reviews.items()
    ):
        raise ValueError("reviews.json must map known case IDs to ship or do_not_ship")
    policies = select_policies(inputs["policies"], candidate)

    cases = []
    for case_id, request in requests.items():
        cases.append({
            "id": case_id,
            "limited_local_access": request["limited_local_access"],
            "estimated_transfer_days": request["estimated_transfer_days"],
            "source_copy_available": request["source_copy_available"],
            "review": reviews.get(case_id),
            "baseline": decide(request, policies["baseline"]),
            "candidate": decide(request, policies[candidate]),
        })

    scored = [case for case in cases if case["review"] is not None]
    reviewed_priority_ship = [
        case for case in scored if case["limited_local_access"] and case["review"] == "ship"
    ]
    unreviewed = [case for case in cases if case["review"] is None]
    changed = [case for case in cases if case["baseline"] != case["candidate"]]
    unreviewed_priority_losses = [
        case["id"] for case in unreviewed if case["limited_local_access"]
        and case["baseline"] == "ship" and case["candidate"] == "do_not_ship"
    ]
    agreement = {
        side: sum(case[side] == case["review"] for case in scored)
        for side in ("baseline", "candidate")
    }
    priority_ship = {
        side: sum(case[side] == "ship" for case in reviewed_priority_ship)
        for side in ("baseline", "candidate")
    }

    reasons = []
    if not scored:
        reasons.append("no_reviewed_cases")
    elif agreement["candidate"] <= agreement["baseline"]:
        reasons.append("agreement_did_not_improve")
    if not reviewed_priority_ship:
        reasons.append("no_reviewed_priority_ship_cases")
    elif priority_ship["candidate"] < priority_ship["baseline"]:
        reasons.append("reviewed_priority_service_declined")
    if unreviewed_priority_losses:
        reasons.append("unreviewed_priority_shipments_removed")

    return {
        "candidate": candidate,
        "verdict": "hold" if reasons else "go",
        "hold_reasons": reasons,
        "input_sha256": digests,
        "policies": policies,
        "coverage": {
            "requests": len(cases),
            "reviewed": len(scored),
            "unreviewed_ids": [case["id"] for case in unreviewed],
            "changed_reviewed": sum(case["review"] is not None for case in changed),
            "changed_unreviewed_ids": [case["id"] for case in changed if case["review"] is None],
        },
        "review_agreement": {"denominator": len(scored), **agreement},
        "priority_service": {
            "denominator": len(reviewed_priority_ship),
            **priority_ship,
            "unreviewed_shipment_removal_ids": unreviewed_priority_losses,
        },
        "changed_decisions": changed,
        "future_outcomes": "unknown",
    }


def write_report(path, report):
    with Path(path).open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate")
    args = parser.parse_args()
    try:
        inputs, digests = load_inputs()
        report = replay(inputs, digests, args.candidate)
        write_report("replay-report.json", report)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Replay failed: {exc}\n")
    print(report["verdict"])


if __name__ == "__main__":
    main()
