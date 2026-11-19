# Issue queue

Read before picking up, claiming, triaging, filing or closing work. Work lives in
[GitHub issues](https://github.com/christopherseaman/sqrlly/issues); the
[sqrlly board](https://github.com/users/christopherseaman/projects/4) shows them. History is in `git log` and
[CHANGELOG.md](../../CHANGELOG.md). Labels are the source of truth: any agent on any host can read
and set them with the `gh` CLI or the GitHub API.

## States

Every open issue carries exactly one `status:` label.

| Label | Meaning | Leaves it when |
| --- | --- | --- |
| `status:untriaged` | New, not yet classified or sized. Set automatically on a new issue. | Triage finishes |
| `status:ready` | Sized and unblocked. Open to any agent that meets its `needs:` labels. | An agent claims it |
| `status:in-progress` | Claimed. The latest `Claim:` or `Takeover:` comment names the agent. | It is closed, handed off, or the claim goes stale |
| `status:feedback` | Waits for a human answer. The last `Question:` comment says what is asked. | A human comments (automatic), or the asker withdraws the question |
| `status:blocked` | Waits on other issues. The body has one `Blocked by #N` line per blocker. | Every blocker is closed (automatic) |
| `status:parked` | Kept for later, not queued. A comment names what would unpark it. | The owner or a triage pass unparks it |

Closed means done, a duplicate, or not planned; say which in the closing comment.

## Other labels

- `needs:owner` (only the owner can do or decide it: a minor or major release, a schema removal,
  a direction call) and `needs:live` (logged-in `codex` and `claude` CLIs, for `tests/cli` and
  live runs). They state what a task needs, not who takes it. An issue with no `needs:` label is
  open to any agent, cloud sessions included.
- `kind:defect`, `kind:feature`, `kind:chore`, `kind:test`, `kind:decision`, `kind:investigation`.
- Size: `effort:S` (half a day or less), `effort:M` (up to two days), `effort:L` (more);
  `impact:low|medium|high` for users or upkeep; `risk:low|medium|high` for the chance the work
  breaks something or cannot finish as scoped.

## Comments

Agents post through the owner's account, so the first word of a comment tells an agent's comment
from a human's. Start every agent comment with one of: `Claim:`, `Progress:`, `Question:`,
`Result:`, `Handoff:`, `Takeover:`, `Triage:`, `Note:`. A comment that starts with none of them
counts as the owner's.

## Picking up work

1. List `status:ready` issues whose `needs:` labels you meet. Read each candidate's comments
   and drop any with a live claim: a `Claim:` or `Takeover:` whose issue has an agent comment
   inside the lease (4 hours), the claim itself included. A label can lag its comments, so
   `status:ready` alone proves nothing. Take the highest impact, then the lowest effort, then the
   oldest of what is left. Do not claim a `needs:owner` issue; add evidence to it.
2. Leave a claimed issue to its claimant: do not offer it as next work, and do no part of it.
   Say once in a `Note:` what your host has (logged-in CLIs, say), then wait for the claimant's
   `Handoff:` or the owner's word.
3. Comment `Claim: <agent or host name>` with the branch you will push, then set
   `status:in-progress`.
4. Read the comments again. If another agent's claim is earlier and still live, comment
   `Note: withdrawn, #<their comment> came first`, and pick another issue.
5. While working, comment `Progress:` at least once per lease (4 hours). One line is enough.
6. A claim with no comment inside the lease is stale. Any agent may take it with
   `Takeover: <agent>`, after reading the earlier comments and the pushed branch. A sweep
   (every 6 hours) also returns stale claims to `status:ready`.

## Stopping, asking, finishing

- Hand off: comment `Handoff:` with what is done, the pushed branch and what remains, then set
  `status:ready`.
- Ask the owner: comment `Question:` with one question and the options, then set
  `status:feedback`. The owner's answer returns the issue to `status:ready`; the asker or any
  agent claims it again.
- Blocked mid-work: add a `Blocked by #N` line to the body, set `status:blocked`, and comment
  why. File the blocker first if no issue covers it.
- Finish: comment `Result:` with the commit, the test and its result, and the branch. Close the
  issue. Closing readies the issues it blocked.
- A finding outside the issue's scope becomes a new issue.

## Triage

A triage pass turns `status:untriaged` into one of ready, blocked, feedback, parked, or closed:

1. Reproduce a defect, or confirm the entry points a feature names still exist.
2. Set one `kind:` label and the `needs:` labels.
3. Size it: `effort:`, `impact:`, `risk:`.
4. Add `Blocked by #N` lines for issues that must close first.
5. Write "Done when": a named test or an exact observable output.
6. Comment `Triage:` with what was checked, then set the new status.

## Filing

Title: imperative, under 70 characters. Body: one paragraph on what is wrong or wanted, then
`Known`, `Where` (paths, tests, docs) and `Done when`. One issue per piece of work that can be
claimed alone. New issues start `status:untriaged` unless the filer already triaged them.

An issue makes an artifact or builds something. A spec or design is a file under
`docs/reference/`, committed to main; issues cite the file and its section. An issue body is
never the spec for another issue. A decision issue is done when its answer is written into the
file it changes.

## Automation

`.github/workflows/issue-queue.yml` runs `tools/issue-queue.py` on issue open, issue close, new
comments and a 6-hour schedule. The script also runs by hand: `tools/issue-queue.py sweep
--dry-run`. `tools/test-issue-queue.py` checks its three decisions.

The project board's Status column mirrors the labels. `tools/issue-queue.py board` copies them,
adding issues the board lacks; closed issues show as Done. An agent runs it when it picks up work
and after it changes a status, if its login has the `project` scope. Nothing depends on the
board: pickup reads labels. The automatic moves (unblock, feedback, stale claim, new issue)
change the label at once and reach the board at the next agent's sync.
