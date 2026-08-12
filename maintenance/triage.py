#!/usr/bin/env python3
"""Background maintenance triage: pick one eligible repo, find one piece of
objectively justified work, and hand exactly that to Jules.

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
  4. ONE AT A TIME. One session per run, and never while another is live for the
     same repository.

Standard library only: the runner should not need a dependency install to decide
whether to do nothing.
"""

import argparse
import json
import os
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


def pick_repo(repos, state, forced=None):
    """Least-recently-maintained wins; priority breaks ties.

    Deliberately not random: with a handful of repos, randomness produces exactly
    the starvation this is meant to avoid, and makes runs impossible to predict
    or reproduce.
    """
    enabled = [r for r in repos if r.get("enabled")]
    if forced:
        for r in repos:
            if r["repo"] == forced:
                if not r.get("enabled"):
                    log(f"{forced} is in the allowlist but disabled - refusing")
                    return None
                return r
        log(f"{forced} is not in the allowlist - refusing")
        return None
    if not enabled:
        return None
    seen = state.get("repos", {})

    def sort_key(r):
        last = seen.get(r["repo"], {}).get("last_run", "")
        return (last or "", r.get("priority", 50))

    return sorted(enabled, key=sort_key)[0]


# ----------------------------------------------------------------------- triage
def gh_paged(path, token, limit=30):
    data, status = http_json(f"{GITHUB_API}{path}", token=token)
    if status != 200 or isinstance(data, dict) and data.get("_error"):
        return None
    return data[:limit] if isinstance(data, list) else data


def already_busy(repo, token, jules_key):
    """Anything that means 'work is already in flight here'. Fail closed."""
    owner_repo = repo["repo"]

    prs = gh_paged(f"/repos/{owner_repo}/pulls?state=open&per_page=50", token)
    if prs is None:
        return "cannot list pull requests"
    for pr in prs:
        labels = [l.get("name") for l in pr.get("labels", [])]
        if PR_LABEL in labels:
            return f"open maintenance PR #{pr['number']}"
        if (pr.get("head", {}).get("ref") or "").startswith(BRANCH_PREFIX + "/"):
            return f"open {BRANCH_PREFIX}/* PR #{pr['number']}"

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="triage and print the brief; never contact Jules")
    ap.add_argument("--repo", help="force a specific allowlisted repo")
    ap.add_argument("--task", help="manual task text, used by workflow_dispatch. "
                                   "Skips discovery but still passes the sensitivity "
                                   "gate and the duplicate-work check.")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    jules_key = os.environ.get("JULES_API_KEY")

    record = {"timestamp": utc_now(), "repo": None, "triage_result": None,
              "task_created": False, "jules_session_id": None, "status": None,
              "branch": None, "pr": None, "summary": None, "tests": None,
              "outcome": None, "dry_run": bool(args.dry_run)}

    if not token:
        record.update(status="fail_closed", outcome="no GITHUB_TOKEN in environment")
        emit(record, ["### Background maintenance", "Refused: no GITHUB_TOKEN."])
        return 1

    repos = load_repos()
    state = load_state()
    repo = pick_repo(repos, state, args.repo)
    if not repo:
        record.update(status="skipped", triage_result="no eligible repo",
                      outcome="allowlist has no enabled repository")
        emit(record, ["### Background maintenance", "Skipped: no eligible repository."])
        return 0

    record["repo"] = repo["repo"]
    log(f"selected {repo['repo']} (priority {repo.get('priority')})")

    busy = already_busy(repo, token, jules_key)
    if busy:
        record.update(status="skipped", triage_result=f"already busy: {busy}",
                      outcome="no duplicate work created")
        # Still stamp the run so rotation advances past a permanently busy repo
        # instead of pinning every future run to it.
        state.setdefault("repos", {}).setdefault(repo["repo"], {})["last_run"] = utc_now()
        state.setdefault("repos", {})[repo["repo"]]["last_outcome"] = "skipped_busy"
        state["history"].append({"ts": utc_now(), "repo": repo["repo"],
                                 "outcome": "skipped_busy", "detail": busy})
        save_state(state)
        emit(record, ["### Background maintenance",
                      f"**{repo['repo']}** - skipped, {busy}."])
        return 0

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
        state.setdefault("repos", {}).setdefault(repo["repo"], {})["last_run"] = utc_now()
        state["repos"][repo["repo"]]["last_outcome"] = "skipped_no_work"
        state["history"].append({"ts": utc_now(), "repo": repo["repo"],
                                 "outcome": "skipped_no_work", "detail": evidence})
        save_state(state)
        emit(record, ["### Background maintenance",
                      f"**{repo['repo']}** - skipped: {evidence}.",
                      "", "No Jules task was created. This is a normal result."])
        return 0

    brief = build_brief(repo, kind, title, evidence)

    hits = sensitive_hits(f"{title}\n{evidence}")
    if hits:
        record.update(status="refused_sensitive", triage_result=f"{kind}: {title}",
                      outcome=f"sensitive areas {hits} - left for human review")
        state.setdefault("repos", {}).setdefault(repo["repo"], {})["last_run"] = utc_now()
        state["repos"][repo["repo"]]["last_outcome"] = "refused_sensitive"
        state["history"].append({"ts": utc_now(), "repo": repo["repo"],
                                 "outcome": "refused_sensitive", "detail": str(hits)})
        save_state(state)
        emit(record, ["### Background maintenance",
                      f"**{repo['repo']}** - candidate work touches {hits}.",
                      "Not delegated. Flagged for Claude/human review."])
        return 0

    record["triage_result"] = f"{kind}: {title}"
    record["summary"] = evidence[:500]

    if args.dry_run:
        record.update(status="dry_run", outcome="brief built, Jules not contacted")
        log("dry run - brief follows, no session created")
        print("-" * 70)
        print(brief)
        print("-" * 70)
        emit(record, ["### Background maintenance (dry run)",
                      f"**{repo['repo']}** - would delegate: {title}"])
        return 0

    if not jules_key:
        record.update(status="fail_closed", outcome="no JULES_API_KEY secret")
        emit(record, ["### Background maintenance",
                      f"**{repo['repo']}** - refused: JULES_API_KEY secret is not set.",
                      "Triage found work but nothing was delegated."])
        return 1

    session, err = create_session(repo, brief, jules_key, title)
    if err:
        record.update(status="fail_closed", outcome=err)
        state.setdefault("repos", {}).setdefault(repo["repo"], {})["last_run"] = utc_now()
        state["repos"][repo["repo"]]["last_outcome"] = err
        state["history"].append({"ts": utc_now(), "repo": repo["repo"], "outcome": err})
        save_state(state)
        emit(record, ["### Background maintenance",
                      f"**{repo['repo']}** - Jules refused or was unavailable: `{err}`.",
                      "No retry loop: the next scheduled run tries again."])
        return 1

    sid = session.get("id") or session.get("name")
    record.update(task_created=True, jules_session_id=sid, status="delegated",
                  outcome="session created; PR will appear when Jules finishes")
    state.setdefault("repos", {}).setdefault(repo["repo"], {})["last_run"] = utc_now()
    state["repos"][repo["repo"]]["last_outcome"] = "delegated"
    state["repos"][repo["repo"]]["last_session"] = sid
    state["history"].append({"ts": utc_now(), "repo": repo["repo"],
                             "outcome": "delegated", "session": sid, "task": title})
    save_state(state)
    emit(record, ["### Background maintenance",
                  f"**{repo['repo']}** - delegated to Jules.",
                  f"- task: {title}", f"- session: `{sid}`",
                  "- a pull request will appear when it finishes. Nothing merges automatically."])
    return 0


if __name__ == "__main__":
    sys.exit(main())
