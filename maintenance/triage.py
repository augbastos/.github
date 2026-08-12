#!/usr/bin/env python3
"""Background maintenance triage: work through the least-recently-maintained
eligible repositories, find one piece of objectively justified work in each, and
hand exactly that to Jules.

    python maintenance/triage.py --dry-run          # triage only, never calls Jules
    python maintenance/triage.py                    # triage, then create a session
    python maintenance/triage.py --repo augbastos/scpe --dry-run

Design rules, in priority order:

  1. SKIP IS A RESULT. "No convincing work found" is a successful run. The system
     exists to maintain repositories, not to look busy.
  2. FAIL CLOSED. Anything we cannot determine - auth, triage, repo state,
     sensitivity - stops before Jules is called, never after.
  3. EVIDENCE BEFORE WORK. Every brief cites what made it necessary: a failing
     run, an issue number, a warning. No evidence, no task.
  4. ONE PER REPOSITORY, AND A BOUNDED QUEUE. Never a second task while one is
     live for the same repository, and nothing new at all once the review
     backlog reaches MAX_OPEN_MAINTENANCE_PRS. Review capacity is the scarce
     resource, not Jules quota - a queue nobody can read gets rubber-stamped.
  5. UNTRUSTED TEXT IS NOT A WORK ORDER. The maintained repositories are public,
     so an issue body is input from the internet, not an instruction. Only
     authors with write-level trust can become a brief (TRUSTED_ASSOCIATIONS).

Standard library only: the runner should not need a dependency install to decide
whether to do nothing.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sensitive_task import sensitive_hits  # noqa: E402

GITHUB_API = "https://api.github.com"
JULES_API = "https://jules.googleapis.com/v1alpha"
HERE = os.path.dirname(os.path.abspath(__file__))
REPOS_FILE = os.path.join(HERE, "repos.json")
STATE_FILE = os.path.join(HERE, "state.json")

PR_LABEL = "jules-maintenance"
BRANCH_PREFIX = "jules"
JULES_LIVE_STATES = {"IN_PROGRESS", "PENDING", "QUEUED", "PLANNING",
                     "AWAITING_PLAN_APPROVAL", "AWAITING_USER_FEEDBACK", "PAUSED"}

# How many repositories one run may work through. The loop went daily, so a
# single repo per run would take a week and a half to touch the allowlist once.
MAX_REPOS_PER_RUN = 3

# Global brake. `already_busy` caps each repo at one open maintenance PR, which
# bounds nothing across six repos running daily. Review capacity is the real
# scarce resource here, not Jules quota (100 tasks/24h, we use a handful a
# week): a queue nobody can read gets rubber-stamped, and rubber-stamping is
# how an unreviewed agent patch reaches master. Above this many open
# maintenance PRs the run does nothing at all until the backlog is cleared.
MAX_OPEN_MAINTENANCE_PRS = 5

# Where held work is parked for a human decision. This repository is the
# orchestrator, so the queue lives with the thing that produced it.
ORCHESTRATOR_REPO = "augbastos/.github"
AMBER_LABEL = "needs-augusto"

# Issue authors whose text may become an agent brief. The repositories are
# public, so anyone on the internet can open an issue, and `find_work` feeds the
# issue title and body straight into the prompt Jules executes - a stranger's
# text would be agent instructions. Only people who already have write-level
# trust on the repository qualify. GitHub returns this as `author_association`.
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    print(f"[triage] {msg}", flush=True)


# --------------------------------------------------------------------------- http
def http_json(url, token=None, method="GET", body=None, api_key=None, timeout=45):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "augbastos-background-maintenance")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if api_key:
        req.add_header("X-Goog-Api-Key", api_key)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            raw = r.read().decode() or "{}"
            return json.loads(raw), r.status
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        # Never echo an auth header; the body of a 401/403 can quote the request.
        if e.code in (401, 403):
            detail = "<withheld: auth error body may quote the credential>"
        return {"_error": f"HTTP {e.code}", "_detail": detail}, e.code
    except Exception as e:  # network, DNS, timeout
        return {"_error": type(e).__name__, "_detail": str(e)[:200]}, 0


# --------------------------------------------------------------------- allowlist
def load_repos():
    with open(REPOS_FILE, encoding="utf-8") as f:
        return json.load(f)["repos"]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"schema_version": 1, "repos": {}, "history": []}


def save_state(state):
    state["history"] = state.get("history", [])[-50:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


def pick_repos(repos, state, limit, forced=None):
    """Least-recently-maintained first; priority breaks ties. Returns a list.

    Deliberately not random: with a handful of repos, randomness produces exactly
    the starvation this is meant to avoid, and makes runs impossible to predict
    or reproduce. `limit` is how many this run may work through - the rotation
    order is unchanged, it just takes a longer prefix of it.
    """
    enabled = [r for r in repos if r.get("enabled")]
    if forced:
        for r in repos:
            if r["repo"] == forced:
                if not r.get("enabled"):
                    log(f"{forced} is in the allowlist but disabled - refusing")
                    return []
                return [r]
        log(f"{forced} is not in the allowlist - refusing")
        return []
    if not enabled:
        return []
    seen = state.get("repos", {})

    def sort_key(r):
        last = seen.get(r["repo"], {}).get("last_run", "")
        return (last or "", r.get("priority", 50))

    return sorted(enabled, key=sort_key)[:max(1, limit)]


# ----------------------------------------------------------------------- triage
def gh_paged(path, token, limit=30):
    data, status = http_json(f"{GITHUB_API}{path}", token=token)
    if status != 200 or isinstance(data, dict) and data.get("_error"):
        return None
    return data[:limit] if isinstance(data, list) else data


def is_maintenance_pr(pr):
    """Does this open pull request come from the maintenance loop?

    Three markers, and the third is the one that matters. Jules names its branch
    after the WORK, not after itself: the first real delivery arrived as
    `fix/test-occupancy-log-5738273773848449738`, with no label and no `jules/`
    prefix. Matching only the first two markers left `already_busy` blind to a
    maintenance PR the loop had itself caused - the repository looked free and
    would have collected a second one on top. The embedded session id (a long
    run of digits) is the reliable marker.

    Deliberately shared with count_open_maintenance_prs: two definitions of
    "is this ours" drift apart, and the drift is silent.
    """
    labels = [l.get("name") for l in pr.get("labels", [])]
    if PR_LABEL in labels:
        return f"labelled {PR_LABEL}"
    head = pr.get("head", {}).get("ref") or ""
    if head.startswith(BRANCH_PREFIX + "/"):
        return f"{BRANCH_PREFIX}/* branch"
    if re.search(r"\d{15,}", head):
        return "branch carries a Jules session id"
    return None


def already_busy(repo, token, jules_key):
    """Anything that means 'work is already in flight here'. Fail closed."""
    owner_repo = repo["repo"]

    prs = gh_paged(f"/repos/{owner_repo}/pulls?state=open&per_page=50", token)
    if prs is None:
        return "cannot list pull requests"
    for pr in prs:
        why = is_maintenance_pr(pr)
        if why:
            return f"open maintenance PR #{pr['number']} ({why})"

    branches = gh_paged(f"/repos/{owner_repo}/branches?per_page=100", token, limit=100)
    if branches is None:
        return "cannot list branches"
    for b in branches:
        if b["name"].startswith(BRANCH_PREFIX + "/"):
            return f"existing branch {b['name']}"

    if jules_key:
        data, status = http_json(f"{JULES_API}/sessions?pageSize=50", api_key=jules_key)
        if status != 200:
            return f"cannot list Jules sessions ({data.get('_error')})"
        for s in data.get("sessions", []):
            src = (s.get("sourceContext", {}) or {}).get("source", "")
            if src.endswith("/" + owner_repo) and s.get("state") in JULES_LIVE_STATES:
                return f"live Jules session {s.get('id')} ({s.get('state')})"
    return None


def count_open_maintenance_prs(repos, token):
    """How many maintenance pull requests are open across the whole allowlist.

    Returns (count, details) or (None, reason) when it cannot be determined -
    and an unknown backlog stops the run, same fail-closed rule as everywhere else.
    """
    total, details = 0, []
    for r in repos:
        prs = gh_paged(f"/repos/{r['repo']}/pulls?state=open&per_page=50", token)
        if prs is None:
            return None, f"cannot list pull requests for {r['repo']}"
        for pr in prs:
            if is_maintenance_pr(pr):
                total += 1
                details.append(f"{r['repo']}#{pr['number']}")
    return total, details


def amber_issue_exists(fingerprint, token):
    """True when this held item is already queued. Without this the same
    refusal would open a fresh issue on every run, which is how an approval
    queue becomes noise nobody reads."""
    q = urllib.parse.quote(
        f'repo:{ORCHESTRATOR_REPO} is:issue is:open label:"{AMBER_LABEL}" "{fingerprint}"')
    data, status = http_json(f"{GITHUB_API}/search/issues?q={q}", token=token)
    if status != 200:
        return None  # unknown -> caller must not create a possible duplicate
    return (data.get("total_count") or 0) > 0


def open_amber_issue(repo, kind, title, evidence, hits, brief, token):
    """Park work that the gate refused into a human-approval queue.

    Previously a sensitive refusal was recorded in state.json and nowhere else,
    so the only person who could act on it never learned it existed. The issue
    carries the evidence and the exact brief, so approving is a copy-paste into
    the workflow_dispatch `task` box rather than rewriting the analysis.
    """
    fingerprint = f"maint:{repo['repo']}:{kind}:{title[:80]}"
    exists = amber_issue_exists(fingerprint, token)
    if exists is None:
        return None, "cannot search the approval queue"
    if exists:
        return None, "already queued"

    body = f"""Background maintenance found work on **{repo['repo']}** and did **not** delegate it.

**Why it was held:** the sensitivity gate matched {hits}. Work touching those
areas is never handed to an unattended remote agent.

**Evidence**
{evidence[:1500]}

---

### If you want this done

Run the `background maintenance` workflow manually with this text in the `task`
box (Actions -> background maintenance -> Run workflow), and untick `dry_run`:

```
{brief[:2500]}
```

It still passes every gate on the way through - dispatching is a decision to
proceed, not a bypass.

### If you do not

Close this issue. Nothing else references it.

<!-- {fingerprint} -->
"""
    data, status = http_json(
        f"{GITHUB_API}/repos/{ORCHESTRATOR_REPO}/issues", token=token, method="POST",
        body={"title": f"[approval] {repo['repo']}: {title[:90]}",
              "body": body, "labels": [AMBER_LABEL]})
    if status not in (200, 201):
        return None, f"could not open issue: {data.get('_error')} {data.get('_detail', '')[:120]}"
    return data.get("number"), None


def find_work(repo, token):
    """Return (kind, title, evidence) for the highest-priority justified task.

    Priority order mirrors the maintenance policy: broken CI, then open issues,
    then nothing. Sources that cannot be verified from the API are not guessed at.
    """
    owner_repo = repo["repo"]
    branch = repo.get("default_branch") or "main"

    # 1. broken CI on the default branch - the only signal that is a regression
    #    by definition rather than by opinion.
    runs = gh_paged(
        f"/repos/{owner_repo}/actions/runs?branch={urllib.parse.quote(branch)}"
        f"&status=completed&per_page=10", token)
    if runs is None:
        return None, None, "cannot read workflow runs"
    for run in (runs or {}).get("workflow_runs", [])[:5]:
        if run.get("conclusion") == "failure":
            return ("ci_failure",
                    f"Fix the failing {run.get('name')} workflow on {branch}",
                    f"workflow run #{run.get('run_number')} ({run.get('html_url')}) "
                    f"concluded 'failure' on {branch}")

    # 2. open issues - human-stated, already-scoped work. Oldest first: the
    #    backlog tail is what never gets touched otherwise.
    issues = gh_paged(f"/repos/{owner_repo}/issues?state=open&sort=created"
                      f"&direction=asc&per_page=30", token)
    if issues is None:
        return None, None, "cannot read issues"
    for issue in issues:
        if "pull_request" in issue:
            continue  # the issues endpoint returns PRs too
        # The repositories are public: anyone can open an issue, and the body
        # below becomes part of the brief an autonomous agent executes. Text from
        # a stranger is untrusted input, not a work order. Only write-level
        # trust qualifies; everything else is a signal for a human to read.
        assoc = (issue.get("author_association") or "").upper()
        if assoc not in TRUSTED_ASSOCIATIONS:
            log(f"issue #{issue.get('number')} skipped: author_association={assoc or 'NONE'}")
            continue
        body = (issue.get("body") or "")[:2000]
        text = f"{issue.get('title')} {body}"
        if sensitive_hits(text):
            continue  # handled by the caller's gate as a human-review item
        return ("issue",
                f"Resolve issue #{issue['number']}: {issue['title']}",
                f"open issue #{issue['number']} ({issue['html_url']}): "
                f"{(issue.get('title') or '').strip()}\n\n{body}")

    return None, None, "no failing CI and no actionable open issue"


def build_brief(repo, kind, title, evidence):
    """A bounded contract. Never 'improve this repository'."""
    branch = repo.get("default_branch") or "main"
    return f"""{title}

WHY THIS TASK EXISTS (evidence)
{evidence}

WHAT TO DO
- Investigate and confirm the root cause before changing anything.
- Implement the smallest correct fix. Do not refactor around it.
- Add or update the tests that would have caught this.
- Do not change unrelated code, formatting, or dependencies.
- Base your work on the {branch} branch.

OUT OF SCOPE - stop and report instead of doing any of these
- authentication, login or session security
- payments, Stripe, billing
- secrets, credentials, API keys, tokens, CI secrets
- deployment, release, publishing, production environments
- destructive database migrations or row-level-security policies
- rewriting git history, force pushes, branch protection
- anything that speaks publicly on the owner's behalf

WHAT TO RETURN
A pull request containing only the change described above, plus evidence that
the tests ran and what they printed. If the task turns out to be wrong, too
large, or to require any out-of-scope area, say so and change nothing.

This task was created by scheduled background maintenance, not by a human
watching. There is nobody to ask. If the scope is unclear, do less.
"""


# ------------------------------------------------------------------------ jules
def create_session(repo, brief, jules_key, title):
    source = f"sources/github/{repo['repo']}"
    body = {
        "prompt": brief,
        "title": title[:120],
        "sourceContext": {
            "source": source,
            "githubRepoContext": {"startingBranch": repo.get("default_branch") or "main"},
        },
        # Background work must land somewhere a human can find it later; the
        # local launcher's patch-return default assumes Claude is running.
        # AUTO_CREATE_PR creates the branch and the PR - and nothing merges it.
        "automationMode": "AUTO_CREATE_PR",
    }
    data, status = http_json(f"{JULES_API}/sessions", method="POST",
                             body=body, api_key=jules_key)
    if status == 429:
        return None, "quota_or_rate_limited"
    if status in (401, 403):
        return None, "jules_auth_failed"
    if status != 200 or data.get("_error"):
        return None, f"jules_error:{data.get('_error', status)}"
    return data, None


# ------------------------------------------------------------------------- main
def emit(record, summary_lines):
    out = os.environ.get("MAINTENANCE_RESULT", os.path.join(HERE, "last-run.json"))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("\n".join(summary_lines) + "\n")
    print(json.dumps(record, indent=2, ensure_ascii=False))


def run_one(repo, token, jules_key, args, state):
    """Triage and possibly delegate ONE repository.

    Returns (record, summary_lines, delegated). Every early return still stamps
    rotation state so a permanently-busy or permanently-quiet repository cannot
    pin every future run to itself.
    """
    record = {"timestamp": utc_now(), "repo": repo["repo"], "triage_result": None,
              "task_created": False, "jules_session_id": None, "status": None,
              "branch": None, "pr": None, "summary": None, "tests": None,
              "outcome": None, "dry_run": bool(args.dry_run)}

    def stamp(outcome, detail=None):
        seen = state.setdefault("repos", {}).setdefault(repo["repo"], {})
        seen["last_run"] = utc_now()
        seen["last_outcome"] = outcome
        entry = {"ts": utc_now(), "repo": repo["repo"], "outcome": outcome}
        if detail:
            entry["detail"] = detail
        state.setdefault("history", []).append(entry)

    log(f"selected {repo['repo']} (priority {repo.get('priority')})")

    busy = already_busy(repo, token, jules_key)
    if busy:
        record.update(status="skipped", triage_result=f"already busy: {busy}",
                      outcome="no duplicate work created")
        stamp("skipped_busy", busy)
        return record, [f"**{repo['repo']}** - skipped, {busy}."], False

    if args.task:
        # A human typed this into workflow_dispatch. It still has to clear the
        # same gates - "Augusto asked for it" is not a reason to let an
        # unattended remote agent near a sensitive area.
        kind, title = "manual", args.task.strip().splitlines()[0][:120]
        evidence = f"manual dispatch by the repository owner:\n\n{args.task.strip()}"
    else:
        kind, title, evidence = find_work(repo, token)

    if not kind:
        record.update(status="skipped", triage_result=evidence,
                      outcome="nothing worth a Jules task")
        stamp("skipped_no_work", evidence)
        return record, [f"**{repo['repo']}** - skipped: {evidence}."], False

    brief = build_brief(repo, kind, title, evidence)

    hits = sensitive_hits(f"{title}\n{evidence}")
    if hits:
        # AMBER. Not delegated - but no longer silent either. Held work used to
        # be written to state.json and nowhere a human would ever look, so the
        # only person who could approve it never learned it existed.
        issue_no, why = open_amber_issue(repo, kind, title, evidence, hits, brief, token)
        record.update(status="refused_sensitive", triage_result=f"{kind}: {title}",
                      outcome=f"sensitive areas {hits} - queued for approval")
        if issue_no:
            record["approval_issue"] = issue_no
            line = (f"**{repo['repo']}** - touches {hits}. Not delegated; "
                    f"queued for approval as {ORCHESTRATOR_REPO}#{issue_no}.")
        else:
            line = (f"**{repo['repo']}** - touches {hits}. Not delegated. "
                    f"Approval queue not updated: {why}.")
        stamp("refused_sensitive", str(hits))
        return record, [line], False

    record["triage_result"] = f"{kind}: {title}"
    record["summary"] = evidence[:500]

    if args.dry_run:
        record.update(status="dry_run", outcome="brief built, Jules not contacted")
        log("dry run - brief follows, no session created")
        print("-" * 70)
        print(brief)
        print("-" * 70)
        return record, [f"**{repo['repo']}** - would delegate: {title}"], False

    if not jules_key:
        record.update(status="fail_closed", outcome="no JULES_API_KEY secret")
        return record, [f"**{repo['repo']}** - refused: JULES_API_KEY secret is not set."], False

    session, err = create_session(repo, brief, jules_key, title)
    if err:
        record.update(status="fail_closed", outcome=err)
        stamp(err)
        return record, [f"**{repo['repo']}** - Jules refused or was unavailable: `{err}`."], False

    sid = session.get("id") or session.get("name")
    record.update(task_created=True, jules_session_id=sid, status="delegated",
                  outcome="session created; PR will appear when Jules finishes")
    seen = state.setdefault("repos", {}).setdefault(repo["repo"], {})
    seen["last_session"] = sid
    stamp("delegated")
    state["history"][-1]["session"] = sid
    state["history"][-1]["task"] = title
    return record, [f"**{repo['repo']}** - delegated to Jules.",
                    f"  - task: {title}", f"  - session: `{sid}`"], True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="triage and print the brief; never contact Jules")
    ap.add_argument("--repo", help="force a specific allowlisted repo")
    ap.add_argument("--task", help="manual task text, used by workflow_dispatch. "
                                   "Skips discovery but still passes the sensitivity "
                                   "gate and the duplicate-work check.")
    ap.add_argument("--max-repos", type=int, default=MAX_REPOS_PER_RUN,
                    help=f"repositories to work through this run (default {MAX_REPOS_PER_RUN})")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    jules_key = os.environ.get("JULES_API_KEY")

    run = {"timestamp": utc_now(), "status": None, "outcome": None,
           "dry_run": bool(args.dry_run), "repos": [], "delegated": 0}

    if not token:
        run.update(status="fail_closed", outcome="no GITHUB_TOKEN in environment")
        emit(run, ["### Background maintenance", "Refused: no GITHUB_TOKEN."])
        return 1

    repos = load_repos()
    state = load_state()

    # Review capacity is the brake, not quota. Checked once, before any triage:
    # if the backlog is already at the cap there is no point discovering more.
    enabled = [r for r in repos if r.get("enabled")]
    open_prs, detail = count_open_maintenance_prs(enabled, token)
    if open_prs is None:
        run.update(status="fail_closed", outcome=f"backlog unknown: {detail}")
        emit(run, ["### Background maintenance",
                   f"Refused: could not measure the review backlog ({detail})."])
        return 1
    run["open_maintenance_prs"] = open_prs
    if open_prs >= MAX_OPEN_MAINTENANCE_PRS and not args.task:
        run.update(status="skipped",
                   outcome=f"{open_prs} maintenance PRs already open (cap {MAX_OPEN_MAINTENANCE_PRS})")
        emit(run, ["### Background maintenance",
                   f"Skipped: {open_prs} maintenance pull requests are already waiting "
                   f"for review (cap {MAX_OPEN_MAINTENANCE_PRS}).",
                   "", "Open: " + ", ".join(detail),
                   "", "Nothing new is created until the queue is cleared. "
                       "An unreadable queue gets rubber-stamped, and that is the "
                       "failure this brake exists to prevent."])
        return 0

    # A forced/manual dispatch is one repository by definition.
    limit = 1 if (args.repo or args.task) else max(1, args.max_repos)
    selected = pick_repos(repos, state, limit, args.repo)
    if not selected:
        run.update(status="skipped", outcome="allowlist has no enabled repository")
        emit(run, ["### Background maintenance", "Skipped: no eligible repository."])
        return 0

    lines = ["### Background maintenance",
             f"Backlog: {open_prs}/{MAX_OPEN_MAINTENANCE_PRS} maintenance PRs open. "
             f"Working through {len(selected)} repo(s).", ""]
    exit_code = 0
    for repo in selected:
        # Re-check the cap between repositories: this run may have just filled it.
        if run["delegated"] + open_prs >= MAX_OPEN_MAINTENANCE_PRS and not args.task:
            lines.append(f"**{repo['repo']}** - not reached, backlog cap hit during this run.")
            break
        record, repo_lines, delegated = run_one(repo, token, jules_key, args, state)
        run["repos"].append(record)
        lines.extend(repo_lines)
        if delegated:
            run["delegated"] += 1
        if record.get("status") == "fail_closed":
            exit_code = 1

    save_state(state)

    statuses = [r.get("status") for r in run["repos"]]
    run["status"] = ("delegated" if run["delegated"] else
                     "fail_closed" if "fail_closed" in statuses else
                     "dry_run" if "dry_run" in statuses else "skipped")
    run["outcome"] = (f"{run['delegated']} session(s) created across "
                      f"{len(run['repos'])} repo(s); nothing merges automatically")
    lines.append("")
    lines.append("Nothing is merged automatically. Every pull request waits for review.")
    emit(run, lines)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
