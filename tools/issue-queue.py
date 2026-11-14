#!/usr/bin/env python3
"""Issue queue automation (docs/reference/issues.md). Run by .github/workflows/issue-queue.yml
with the Actions token, or by hand with the gh CLI's login.
  issue-queue.py opened N       an issue with no status label becomes status:untriaged
  issue-queue.py closed N       open issues whose "Blocked by #N" issues are all closed become ready
  issue-queue.py commented N ID  a human comment on a status:feedback issue makes it ready
  issue-queue.py sweep          in-progress issues with no comment inside the lease become ready
  issue-queue.py board          copy each issue's status into the board's Status column (run by
                                agents: the login needs the project scope)
--dry-run prints each change and makes none. ISSUE_LEASE_HOURS sets the lease (default 4)."""
import datetime
import json
import os
import re
import subprocess
import sys

DRY = "--dry-run" in sys.argv
LEASE = datetime.timedelta(hours=float(os.environ.get("ISSUE_LEASE_HOURS", "4")))
# Agents post as the owner's account, so a comment's first word tells an agent's from a human's.
AGENT = re.compile(r"^\s*(Claim|Progress|Question|Result|Handoff|Takeover|Triage|Note):", re.I)
FOOTER = "claude.ai/code"
BOARD = ("christopherseaman", "4")  # owner and number of the sqrlly project
COLUMN = {"status:untriaged": "Untriaged", "status:ready": "Ready", "status:in-progress": "In progress",
          "status:feedback": "Feedback", "status:blocked": "Blocked", "status:parked": "Parked"}


def gh(*args):
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip().startswith(("{", "[")) else out


def blockers(body):
    """Issue numbers named by "Blocked by #N" lines."""
    return {int(n) for n in re.findall(r"(?im)^\s*[-*]?\s*blocked by #(\d+)", body or "")}


def from_agent(body, author_type="User"):
    return author_type == "Bot" or bool(AGENT.match(body or "")) or FOOTER in (body or "")


def expired(now, stamps):
    """Whether the newest of the ISO timestamps is older than the lease."""
    newest = max(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps)
    return now - newest > LEASE


def status(labels):
    return [label["name"] for label in labels if label["name"].startswith("status:")]


def move(number, old, new, note):
    print(f"#{number}: {old or 'no status'} -> {new} ({note})")
    if DRY:
        return
    remove = ["--remove-label", old] if old else []
    gh("issue", "edit", str(number), *remove, "--add-label", new)
    gh("issue", "comment", str(number), "--body", note)


def opened(number):
    if not status(gh("issue", "view", str(number), "--json", "labels")["labels"]):
        print(f"#{number}: no status -> status:untriaged")
        if not DRY:
            gh("issue", "edit", str(number), "--add-label", "status:untriaged")


def closed(number):
    for issue in gh("issue", "list", "--state", "open", "--label", "status:blocked",
                    "--json", "number,body", "--limit", "500"):
        waits = blockers(issue["body"])
        if number in waits and all(gh("issue", "view", str(n), "--json", "state")["state"] == "CLOSED"
                                   for n in waits):
            move(issue["number"], "status:blocked", "status:ready",
                 f"Unblocked: #{number} closed, and nothing else it names is open.")


def commented(number, comment_id):
    issue = gh("issue", "view", str(number), "--json", "labels,state")
    if issue["state"] != "OPEN" or "status:feedback" not in status(issue["labels"]):
        return
    comment = gh("api", f"repos/{{owner}}/{{repo}}/issues/comments/{comment_id}")
    if not from_agent(comment["body"], comment["user"]["type"]):
        move(number, "status:feedback", "status:ready", "Feedback received: ready for pickup.")


def sweep():
    now = datetime.datetime.now(datetime.timezone.utc)
    for issue in gh("issue", "list", "--state", "open", "--label", "status:in-progress",
                    "--json", "number,comments,updatedAt", "--limit", "500"):
        stamps = [c["createdAt"] for c in issue["comments"]] or [issue["updatedAt"]]
        if expired(now, stamps):
            hours = f"{LEASE.total_seconds() / 3600:g}"
            move(issue["number"], "status:in-progress", "status:ready",
                 f"Claim expired: no comment in {hours} h. Open for pickup; read the last comments first.")


def board():
    owner, number = BOARD
    project = gh("project", "view", number, "--owner", owner, "--format", "json")["id"]
    field = next(f for f in gh("project", "field-list", number, "--owner", owner, "--format", "json")["fields"]
                 if f["name"] == "Status")
    option = {o["name"]: o["id"] for o in field["options"]}
    listed = gh("project", "item-list", number, "--owner", owner, "--format", "json", "--limit", "1000")["items"]
    items = {i["content"]["number"]: i for i in listed if i.get("content", {}).get("number")}
    for issue in gh("issue", "list", "--state", "all", "--json", "number,state,labels,url", "--limit", "1000"):
        want = "Done" if issue["state"] == "CLOSED" else next(
            (COLUMN[s] for s in status(issue["labels"]) if s in COLUMN), "Untriaged")
        item = items.get(issue["number"])
        if item and item.get("status") == want:
            continue
        print(f"#{issue['number']}: board {item.get('status') if item else 'absent'} -> {want}")
        if DRY:
            continue
        item_id = item["id"] if item else gh("project", "item-add", number, "--owner", owner,
                                             "--url", issue["url"], "--format", "json")["id"]
        gh("project", "item-edit", "--id", item_id, "--project-id", project, "--field-id", field["id"],
           "--single-select-option-id", option[want])


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    commands = {"opened": opened, "closed": closed, "commented": commented, "sweep": sweep, "board": board}
    if not args or args[0] not in commands:
        sys.exit(__doc__)
    commands[args[0]](*map(int, args[1:]))
