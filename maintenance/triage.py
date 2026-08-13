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
  3b. REACTIVE SOURCES ALONE STARVE THE LOOP. Failing CI and open issues are the
     only two signals that arrive on their own, and a healthy repository emits
     neither. Every triage run recorded so far ended in "no failing CI and no
     actionable open issue" on five of the six repositories it reached, because
     the CI was green and the owner does not file issues against himself - and a
     later count found zero open issues across the whole allowlist. So the loop
     was quiet for the one reason nobody wants it quiet: everything was fine,
     and being fine is not something it knows how to work on. The proactive
     sources below (MAINTENANCE.md backlog, stale action pins) exist to give the
     loop something objectively justified to do when nothing is broken. They are
     still evidence-backed; they simply do not wait for a failure.
  4. ONE PER REPOSITORY, AND A BOUNDED QUEUE. Never a second task while one is
     live for the same repository, and nothing new at all once the review
     backlog reaches MAX_OPEN_MAINTENANCE_PRS. Review capacity is the scarce
     resource, not Jules quota - a queue nobody can read gets rubber-stamped.
  5. UNTRUSTED TEXT IS NOT A WORK ORDER. Most maintained repositories are public,
     so an issue body is input from the internet, not an instruction. Only
     authors with write-level trust can become a brief (TRUSTED_ASSOCIATIONS).
     The rule stays on for private repositories too: trust is about who wrote the
     text, not about who can read the repository.
  6. VISIBILITY IS NOT SCOPE. A private repository is reachable only with an
     explicit read token, and what may be worked on there is narrowed further by
     its scope profile. Lucky Cat lives in one repository together with payments,
     auth and RLS - "the loop may read it" must never imply "the loop may touch
     all of it".

Standard library only: the runner should not need a dependency install to decide
whether to do nothing.
"""

import argparse
import base64
import hashlib
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

# How many repositories one run may work through. Paired with the twice-daily
# schedule this is deliberate arithmetic, not a round number: 4 per run x 2 runs
# = 8 repository-visits a day, which is exactly the size of the enabled
# allowlist. Every repository gets looked at every day, and no single run has to
# walk the whole list (a long run is a run that can be cut off mid-way by the
# job timeout, and the rotation state it never wrote is the part that is lost).
MAX_REPOS_PER_RUN = 4

# Global brake. `already_busy` caps each repo at one open maintenance PR, which
# bounds nothing across eight repos running twice a day. Review capacity is the
# real scarce resource here, not Jules quota (100 tasks/24h, we use a handful a
# week): a queue nobody can read gets rubber-stamped, and rubber-stamping is
# how an unreviewed agent patch reaches master. Above this many open
# maintenance PRs the run does nothing at all until the backlog is cleared.
#
# Raised 5 -> 8 on 2026-08-13, deliberately and with the trade-off understood.
# The first scheduled run in the loop's life delegated nothing: seven Jules pull
# requests were open on wavr, the cap was five, and the run stopped before
# triaging anything. That is the brake working as designed - but a cap that a
# single repository's batch can exceed on its own stops being a brake on the
# queue and becomes a stop on the loop. Eight leaves room for one repository to
# hold a small batch while the rotation still reaches the others. It is still a
# hard stop, and it is still the number to lower first if the queue ever
# outruns the reading.
MAX_OPEN_MAINTENANCE_PRS = 8

# Where held work is parked for a human decision. This repository is the
# orchestrator, so the queue lives with the thing that produced it.
ORCHESTRATOR_REPO = "augbastos/.github"
AMBER_LABEL = "needs-augusto"

# Issue authors whose text may become an agent brief. Most repositories are
# public, so anyone on the internet can open an issue, and `find_work` feeds the
# issue title and body straight into the prompt Jules executes - a stranger's
# text would be agent instructions. Only people who already have write-level
# trust on the repository qualify. GitHub returns this as `author_association`.
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

# PROACTIVE SOURCE 1 - a maintenance backlog the owner writes into the repository
# itself. This exists because the trusted-issue source has a practical hole: the
# rule is right (only write-level trust may become a brief) but Augusto does not
# open issues on his own repositories, so in practice the source is always empty
# and the loop starves on repositories that are simply healthy.
#
# A file in the repository carries exactly the same trust as a trusted issue, and
# arguably more: changing it requires a commit, which requires write access, and
# the change is reviewable in history. It is NOT a second inbox from the
# internet - a fork's copy is never read, only the default branch of the repo
# itself.
BACKLOG_FILES = (".github/MAINTENANCE.md", "MAINTENANCE.md")
BACKLOG_ITEM_RE = re.compile(r"^\s*[-*]\s+\[ \]\s+(.+?)\s*$")
# A backlog line becomes the title and the brief of an autonomous task. A very
# long line is either an essay (not a scoped task) or an attempt to smuggle a
# wall of instructions through; either way it is not what this source is for.
MAX_BACKLOG_ITEM_CHARS = 300

# PROACTIVE SOURCE 2 - action reference hygiene, in two flavours:
#
#   UNPINNED  `uses: actions/checkout@v4` - a moving ref. Whoever publishes that
#             action can change what runs in this repository's CI at any moment,
#             with no review and no notification. This workflow file pins its own
#             actions by sha for exactly that reason, and says so in a comment;
#             a survey on 2026-08-13 found that discipline applied in this
#             repository and in NO other - all thirteen distinct action
#             references across the maintained repositories were moving tags,
#             including one branch-like `@stable`. The fix is mechanical and
#             behaviour-preserving: pin to the sha the ref resolves to TODAY, so
#             nothing changes version, only the guarantee that it cannot change
#             underneath the repository.
#   STALE     `uses: actions/checkout@<sha>` behind the publisher's latest
#             release. Once a repository is pinned this is what accumulates, so
#             the source stays useful after the first sweep instead of going
#             quiet again.
#
# Both are verifiable entirely from the API - the ref in the file, and the sha it
# or the latest release tag resolves to. Nothing is inferred. This is the work
# Dependabot would do if it were available: it is not, Dependabot alerts answer
# 403 (disabled) and code scanning answers 404 (no analysis) on these
# repositories, which is why this source reads the workflow files directly.
#
# Group 1 is owner/repo (what the API is asked about), group 2 the optional
# subdirectory of a nested action (github/codeql-action/init), group 3 the ref.
ACTION_REF_RE = re.compile(
    r"uses:\s*([A-Za-z0-9][\w.-]*/[\w.-]+)((?:/[\w.-]+)*)@([^\s#'\"]+)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
WORKFLOW_DIR = ".github/workflows"
# Bounds on how much of a repository one triage pass will read looking for pins.
# Without them a repository with fifty workflows turns a "decide whether to do
# anything" run into a hundred API calls against someone else's rate limit.
MAX_WORKFLOW_FILES = 12
MAX_ACTIONS_CHECKED = 15

# Work kinds that do not go away on their own, and therefore need a memory.
# A failing build stops failing and a closed issue stops being open - the world
# forgets those for us. A line in MAINTENANCE.md and a stale pin stay exactly as
# they are until a pull request lands, so without a record of what has already
# been offered, a proposal Augusto declined would be re-delegated on every
# rotation forever. That is how a maintenance loop becomes a nuisance.
PROACTIVE_KINDS = {"backlog", "action_pin"}

# Repositories that carry this GitHub topic are the Lucky Cat product family and
# join the rotation on their own, enabled, as soon as they exist. That is a
# deliberate exception to "automation never adds a repository": Augusto asked for
# every new Lucky Cat service to be maintained without him wiring it up first.
#
# The exception is bounded three ways, and all three matter:
#   - the topic is set by a human on the repository, so a repo opts IN by being
#     tagged; nothing is discovered by name-guessing.
#   - discovery may only ADD. An entry already in repos.json is never rewritten,
#     so a repository Augusto turned off stays off forever.
#   - auto-added entries land on the narrow scope profile, never on 'full'.
FAMILY_TOPIC = "lucky-cat"
FAMILY_SCOPE_PROFILE = "maintenance_lite"

# What a repository's scope profile allows. 'full' is the historical behaviour:
# any non-sensitive work the evidence justifies. 'maintenance_lite' exists for
# repositories where product logic and money live in the same tree as the tests -
# Lucky Cat is one repository containing Tillr, Ownly and PitchPilot alongside
# Stripe, auth and RLS. There, the sensitivity gate alone is not enough: it reads
# the brief, and a brief can be innocent while the change is not.
SCOPE_PROFILES = {"full", "maintenance_lite"}


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


def save_repos(repos):
    """Write the allowlist back, preserving the header comment that explains it."""
    with open(REPOS_FILE, encoding="utf-8") as f:
        doc = json.load(f)
    doc["repos"] = repos
    with open(REPOS_FILE, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")


def is_private(repo):
    """Unknown visibility counts as private.

    Not symmetry - fail-closed. The two ways to be wrong are not equal: guessing
    'public' for a private repository sends it through triage to collect 404s,
    which `find_work` reports as "cannot read workflow runs" and is exactly the
    illegible failure this feature set out to delete. Guessing 'private' for a
    public one costs it a run and says so, by name, in the summary. Take the
    failure that explains itself.
    """
    return (repo.get("visibility") or "private").lower() == "private"


def scope_profile(repo):
    """Never trust the file blindly: anything that is not an explicit, known
    profile is the narrow one.

    A typo in repos.json must not silently widen what an unattended agent may
    touch, and 'full' is the wide setting. Unknown -> maintenance_lite.

    ABSENT counts as unknown, and that is the whole point of this line. The first
    version defaulted a MISSING key to 'full', which covered the typo but not the
    likelier human error: adding a repository by hand and forgetting the field.
    Widening is a decision, so it has to be written down - 'full' is now reachable
    only by typing it. Costs nothing today (every enabled entry states its
    profile) and stops the trap the moment someone adds the next Lucky Cat
    service in a hurry, which this very feature invites them to do.
    """
    profile = (repo.get("scope_profile") or "").lower()
    return profile if profile in SCOPE_PROFILES else FAMILY_SCOPE_PROFILE


def drop_unreadable_private(repos, has_private_reader):
    """Remove private repositories when there is no token that can read them.

    Returns (kept, dropped_names). Reading a private repository with a token that
    cannot see it returns 404 on every endpoint, and `find_work` reads a 404 as
    "cannot read workflow runs" - which stops that repo but says nothing about
    why. Dropping them by name up front turns a confusing per-repo failure into
    one legible line in the run summary.
    """
    if has_private_reader:
        return repos, []
    dropped = [r["repo"] for r in repos if r.get("enabled") and is_private(r)]
    if not dropped:
        return repos, []
    return [r for r in repos if not is_private(r)], dropped


def discover_family_repos(repos, token):
    """Add repositories carrying FAMILY_TOPIC that are not in the allowlist yet.

    Returns (new_entries, note). Discovery only ever appends: an existing entry
    keeps its own `enabled`, priority and scope, so turning a repository off is
    permanent and this cannot undo a human decision.

    A search failure is not fatal. Discovery is a convenience; failing it stops
    the new repository from being picked up this run, which is a delay, not a
    hazard - so it must not take the whole maintenance run down with it.
    """
    known = {r["repo"] for r in repos}
    q = urllib.parse.quote(
        f"user:augbastos topic:{FAMILY_TOPIC} fork:false archived:false")
    data, status = http_json(
        f"{GITHUB_API}/search/repositories?q={q}&per_page=50", token=token)
    if status != 200 or not isinstance(data.get("items"), list):
        return [], f"family discovery skipped: {data.get('_error', status)}"

    next_priority = max([r.get("priority", 50) for r in repos] + [0])
    new_entries = []
    for item in data["items"]:
        full = item.get("full_name")
        if not full or full in known or full == ORCHESTRATOR_REPO:
            continue
        next_priority += 1
        new_entries.append({
            "repo": full,
            "enabled": True,
            "priority": next_priority,
            "default_branch": item.get("default_branch") or "main",
            "visibility": "private" if item.get("private") else "public",
            "scope_profile": FAMILY_SCOPE_PROFILE,
            "family": FAMILY_TOPIC,
            "why": (f"AUTO-ADDED: carries the '{FAMILY_TOPIC}' topic. Every Lucky Cat "
                    f"service is maintained from birth. Narrow scope by default - "
                    f"widen it only by editing this entry by hand."),
        })
    if not new_entries:
        return [], None
    return new_entries, f"family discovery added {[e['repo'] for e in new_entries]}"


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


def gh_file(owner_repo, path, token, ref=None):
    """Read one text file from a repository. None means 'not usable', never ''.

    Kept separate from gh_paged for two reasons. The contents API answers with an
    object rather than a list, and - more importantly - a 404 here is ordinary
    information ("this repository has no MAINTENANCE.md"), not the failure that
    gh_paged's None is meant to signal. Callers must therefore treat None as
    "nothing to read", and must not mistake it for "the repository is
    unreachable"; the reachability question is already answered earlier by
    find_work's workflow-runs call, which fails closed on its own.
    """
    url = f"{GITHUB_API}/repos/{owner_repo}/contents/{urllib.parse.quote(path)}"
    if ref:
        url += f"?ref={urllib.parse.quote(ref)}"
    data, status = http_json(url, token=token)
    if status != 200 or not isinstance(data, dict):
        return None
    # A directory answers as a list, and an oversized blob answers with an empty
    # `content` and a download_url instead. Neither is a file we can read here.
    if data.get("encoding") != "base64" or not data.get("content"):
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return None


def work_fingerprint(owner_repo, kind, title):
    """Stable identity for one piece of proactive work - see PROACTIVE_KINDS.

    Derived from the title rather than carried alongside it on purpose: the title
    is what already encodes the specific item (the backlog line, the action and
    the release it should move to), so there is exactly one thing to keep stable
    and no second value to forget to thread through a call.

    Whitespace is normalised so that reflowing a line in MAINTENANCE.md does not
    read as a brand-new task. Editing the actual words does - which is the
    documented way to re-offer something that was declined.
    """
    raw = f"{owner_repo}|{kind}|{' '.join((title or '').split())}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def done_work_keys(state):
    """Fingerprints of proactive work already handed to Jules at least once."""
    return frozenset((state.get("done_work") or {}).keys())


def is_maintenance_pr(pr, jules_prs=frozenset()):
    """Does this open pull request come from the maintenance loop?

    Four markers, in order of how much they can be trusted.

    The first three read the pull request itself, and all three are guesses about
    naming. Jules names its branch after the WORK, not after itself: the first
    real delivery arrived as `fix/test-occupancy-log-5738273773848449738`, with no
    label and no `jules/` prefix - the session id in it was the only tell. Then a
    delivery arrived on `ci/scpe-reusable`: no label, no prefix, no session id,
    invisible to all three. Every naming heuristic is one branch name away from
    being wrong.

    The fourth marker is not a guess. Jules reports the pull request it opened in
    the session's own outputs (`outputs[].pullRequest.url`), so `jules_prs` is
    the loop asking the agent what it actually did instead of inferring it from a
    string. When the Jules key is missing the set is empty and the heuristics are
    all that is left - degraded, still fail-closed elsewhere.

    Deliberately shared with count_open_maintenance_prs: two definitions of
    "is this ours" drift apart, and the drift is silent.
    """
    key = pr_key(pr.get("html_url"))
    if key and key in jules_prs:
        return "Jules reports having opened it"
    labels = [l.get("name") for l in pr.get("labels", [])]
    if PR_LABEL in labels:
        return f"labelled {PR_LABEL}"
    head = pr.get("head", {}).get("ref") or ""
    if head.startswith(BRANCH_PREFIX + "/"):
        return f"{BRANCH_PREFIX}/* branch"
    if re.search(r"\d{15,}", head):
        return "branch carries a Jules session id"
    return None


def pr_key(url):
    """Normalise any GitHub pull request URL to 'owner/repo#number', or None.

    The two sides being compared come from different APIs and do not agree on
    shape: GitHub hands us `https://github.com/o/r/pull/7`, while Jules reports
    whatever its own record holds - the browser URL for some sessions, the REST
    form `https://api.github.com/repos/o/r/pulls/7` for others. Comparing raw
    strings looks like it works right up until the day Jules returns the other
    one, and then the brake reads zero again with no error anywhere. Compare
    identities, not spellings.
    """
    if not url:
        return None
    m = re.search(r"(?:github\.com/(?:repos/)?)([^/]+)/([^/]+)/pulls?/(\d+)", url)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}#{m.group(3)}"


def jules_pull_requests(jules_key):
    """Pull requests Jules itself reports having opened, as 'owner/repo#number'.

    Returns (keys, error). An error is not empty-set: the caller has to tell
    "Jules says none" apart from "Jules could not be asked", because the second
    one silently unblocks a brake that exists to stop duplicate work.
    """
    if not jules_key:
        return frozenset(), None
    data, status = http_json(f"{JULES_API}/sessions?pageSize=50", api_key=jules_key)
    if status != 200 or data.get("_error"):
        return frozenset(), f"cannot list Jules sessions ({data.get('_error', status)})"
    keys = set()
    for session in data.get("sessions", []):
        for output in session.get("outputs", []) or []:
            key = pr_key((output.get("pullRequest") or {}).get("url"))
            if key:
                keys.add(key)
    return frozenset(keys), None


def already_busy(repo, token, jules_key, jules_prs=frozenset()):
    """Anything that means 'work is already in flight here'. Fail closed."""
    owner_repo = repo["repo"]

    prs = gh_paged(f"/repos/{owner_repo}/pulls?state=open&per_page=50", token)
    if prs is None:
        return "cannot list pull requests"
    for pr in prs:
        why = is_maintenance_pr(pr, jules_prs)
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


def count_open_maintenance_prs(repos, token, jules_prs=frozenset()):
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
            if is_maintenance_pr(pr, jules_prs):
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


def find_backlog_work(repo, token, done):
    """First unchecked line of the repository's own MAINTENANCE.md, or None.

    Read from the DEFAULT BRANCH only. That is the whole trust argument: putting
    a line there took a commit to the branch, which took write access. A pull
    request's copy, a fork's copy and any other ref are never consulted, so an
    outside contributor cannot open a PR that adds a task and have it executed
    before anyone merges it.
    """
    owner_repo = repo["repo"]
    branch = repo.get("default_branch") or "main"
    for path in BACKLOG_FILES:
        text = gh_file(owner_repo, path, token, branch)
        if text is None:
            continue
        for line in text.splitlines():
            m = BACKLOG_ITEM_RE.match(line)
            if not m:
                continue
            item = " ".join(m.group(1).split())
            if not item or len(item) > MAX_BACKLOG_ITEM_CHARS:
                continue
            title = f"Backlog item: {item}"
            if work_fingerprint(owner_repo, "backlog", title) in done:
                continue
            return ("backlog", title[:200],
                    f"unchecked item in {path} on the {branch} branch of "
                    f"{owner_repo}:\n\n- [ ] {item}\n\n"
                    f"That file is part of the repository, so this line was put "
                    f"there by a commit from somebody with write access.")
        # The file existed and had nothing left to do. Checking the second
        # candidate path would only find a stale duplicate, so stop here.
        return None
    return None


def _resolve_ref(action, ref, token):
    """The 40-char commit sha a tag/branch of `action` points at, or None.

    None means "could not be established", and every caller treats that as a
    reason to skip the action entirely. An action reference that cannot be
    resolved must never become a proposed edit: a pin to a sha nobody verified
    is indistinguishable, in a review queue, from a pin to the right one.
    """
    commit = gh_paged(f"/repos/{action}/commits/{urllib.parse.quote(ref)}", token)
    sha = (commit or {}).get("sha") if isinstance(commit, dict) else None
    return sha if sha and SHA_RE.match(sha) else None


def find_action_pin_work(repo, token, done):
    """An action reference that is unpinned, or pinned behind its latest release.

    Evidence, not opinion: the reference is read out of the workflow file, and
    the sha it is compared against comes from resolving a real ref through the
    API. When either side cannot be established the action is skipped rather
    than guessed at.

    The unpinned case deliberately proposes the sha of the ref ALREADY IN USE,
    not the newest release. Pinning and upgrading are two different changes and
    mixing them produces a pull request that is both a security improvement and
    a version bump, which is the kind nobody can review confidently. Pin first,
    at today's behaviour; the stale-pin branch below handles upgrades later, on
    its own, with its own evidence.
    """
    owner_repo = repo["repo"]
    branch = repo.get("default_branch") or "main"

    listing = gh_paged(f"/repos/{owner_repo}/contents/{WORKFLOW_DIR}"
                       f"?ref={urllib.parse.quote(branch)}", token)
    if not isinstance(listing, list):
        return None  # no workflows directory, or unreadable - not our problem here

    checked, seen = 0, set()
    for entry in listing[:MAX_WORKFLOW_FILES]:
        name = entry.get("name") or ""
        if entry.get("type") != "file" or not name.endswith((".yml", ".yaml")):
            continue
        text = gh_file(owner_repo, f"{WORKFLOW_DIR}/{name}", token, branch)
        if not text:
            continue
        for action, subpath, ref in ACTION_REF_RE.findall(text):
            used = f"{action}{subpath}"
            if used in seen or checked >= MAX_ACTIONS_CHECKED:
                continue
            # A REUSABLE WORKFLOW is not an action, even though `uses:` takes
            # both. Caught by the first live run of this source, which proposed
            # pinning `augbastos/.github/.github/workflows/scpe-seal-reusable.yml`
            # - and that would have been actively wrong. A reusable workflow
            # exists so a fix lands in one place and every caller gets it; sha-
            # pinning the callers deletes that property and turns one edit into
            # N. The supply-chain argument does not carry over either: this one
            # lives in the repository that runs the maintenance loop itself.
            if subpath.endswith((".yml", ".yaml")):
                continue
            seen.add(used)
            checked += 1

            if not SHA_RE.match(ref):
                # UNPINNED. Resolve the ref currently in use; that sha is the
                # whole proposal, so a failure to resolve it is a skip.
                target = _resolve_ref(action, ref, token)
                if not target:
                    continue
                title = f"Pin the {used} action to a commit sha"
                if work_fingerprint(owner_repo, "action_pin", title) in done:
                    continue
                return ("action_pin", title[:200],
                        f"{WORKFLOW_DIR}/{name} on the {branch} branch of "
                        f"{owner_repo} uses `{used}@{ref}`.\n"
                        f"`{ref}` is a moving reference: whoever publishes "
                        f"{action} can change what it runs at any time, without "
                        f"a commit to this repository and without review.\n"
                        f"Today it resolves to commit `{target}` - pinning to "
                        f"that sha keeps the current behaviour exactly and "
                        f"removes the ability to change it silently.")

            # STALE PIN. Compare against the publisher's latest release.
            release = gh_paged(f"/repos/{action}/releases/latest", token)
            tag = (release or {}).get("tag_name") if isinstance(release, dict) else None
            if not tag:
                continue  # no releases - nothing to compare against
            target = _resolve_ref(action, tag, token)
            if not target or target == ref:
                continue  # unresolvable, or already on the latest release

            title = f"Update the pinned {used} action to {tag}"
            if work_fingerprint(owner_repo, "action_pin", title) in done:
                continue
            return ("action_pin", title[:200],
                    f"{WORKFLOW_DIR}/{name} on the {branch} branch of "
                    f"{owner_repo} pins `{used}` at `{ref}`.\n"
                    f"The publisher's latest release is `{tag}`, which resolves "
                    f"to commit `{target}`.\n"
                    f"The pin is therefore behind by at least one release.")
    return None


def find_work(repo, token, done=frozenset()):
    """Return (kind, title, evidence) for the highest-priority justified task.

    Priority order mirrors the maintenance policy: broken CI, then open issues,
    then the repository's own backlog file, then a stale action pin, then
    nothing. Sources that cannot be verified from the API are not guessed at.

    The first two are REACTIVE - something already went wrong or somebody already
    asked. They rank highest because a regression outranks tidiness. The last two
    are PROACTIVE and exist because the reactive pair is silent on a healthy
    repository (see design rule 3b); `done` carries the fingerprints of proactive
    work already offered, so nothing is proposed twice on its own.
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

    # 3. the repository's own maintenance backlog - the owner's stated work,
    #    without requiring him to use an issue tracker he does not use.
    backlog = find_backlog_work(repo, token, done)
    if backlog:
        return backlog

    # 4. an unpinned or stale action reference - drift that accumulates
    #    precisely while nothing is breaking, and the only one of these four
    #    sources that needs nobody to have written anything down first.
    pin = find_action_pin_work(repo, token, done)
    if pin:
        return pin

    return (None, None, "no failing CI, no actionable open issue, no unchecked "
                        "backlog item and no unpinned or stale action reference")


LITE_SCOPE_BLOCK = """
ALLOWED WORK - this repository is on a narrow profile, everything else is out of scope
- tests: add, repair, un-skip, de-flake, extend coverage
- CI and tooling configuration: workflows, linters, formatters, type checks
- documentation, comments, README, changelog
- user-facing strings and translations, without changing what a price, claim or
  legal text means

NOT ALLOWED HERE, even when it looks small and even when the evidence points at it
- product or business logic, pricing, order flow, menu or tenant behaviour
- database schema, queries, or anything under a migrations directory
- API contracts, request/response shapes, webhooks
- UI behaviour beyond the literal text of a string
If the fix requires any of those, say so and change nothing. A correct refusal is
a successful run here.
"""

PIN_SCOPE_BLOCK = """
HOW TO CHANGE AN ACTION REFERENCE - the sha pin is a security control, not clutter
- Use EXACTLY the 40-character commit sha named in the evidence above. Do not
  look up a different one, and do not resolve the ref yourself.
- Leave a comment beside it recording the human-readable version the sha came
  from, e.g. `uses: actions/checkout@<sha>  # v4`. The comment is how the next
  reader knows what the sha is without querying the API.
- NEVER use a tag or a branch as the reference (`@v5`, `@main`, `@stable`). A
  moving ref hands whoever publishes that action the ability to change what runs
  in this repository's CI at any time, with no review - which is the entire risk
  the pin removes. Leaving or introducing a moving ref is a failed task, not a
  shortcut.
- When the task is to PIN, the version must not change. The sha given is the one
  the ref already resolves to, so CI behaviour is identical before and after; if
  you find yourself upgrading anything, you are doing a different task.
- Change the ONE action named above, in every workflow file where that exact
  reference appears. Every other action stays exactly as it is.
- If the change would alter the action's inputs or behaviour, say so and change
  nothing: a bump that silently breaks a workflow is worse than a stale pin.
"""


def build_brief(repo, kind, title, evidence):
    """A bounded contract. Never 'improve this repository'."""
    branch = repo.get("default_branch") or "main"
    scope_block = LITE_SCOPE_BLOCK if scope_profile(repo) == "maintenance_lite" else ""
    # A pin bump has one specific way to go wrong that no general instruction
    # covers: "update the action" reads as an invitation to use the friendly tag.
    scope_block += PIN_SCOPE_BLOCK if kind == "action_pin" else ""

    # The default instructions assume a defect: something broke, find why, and
    # leave behind the test that would have caught it. Two of those lines are
    # nonsense for the proactive kinds - a moving action ref has no root cause
    # and no test that would have caught it - and an instruction that cannot be
    # followed is not harmless: it invites the agent to invent work to satisfy
    # it, which is exactly the scope creep every other rule here is fighting.
    if kind == "action_pin":
        how = ("- Make exactly the edit described above and nothing else.\n"
               "- Do not add tests: this changes a reference, not behaviour.\n"
               "- Check that the workflows still parse, and say what you checked.")
    elif kind == "backlog":
        how = ("- Do only what the backlog line asks. If it is ambiguous, say so\n"
               "  and change nothing - there is nobody to ask.\n"
               "- Implement the smallest correct change. Do not refactor around it.\n"
               "- Add or update tests when the change is behavioural.\n"
               "- Tick that line in the backlog file in the same pull request.")
    else:
        how = ("- Investigate and confirm the root cause before changing anything.\n"
               "- Implement the smallest correct fix. Do not refactor around it.\n"
               "- Add or update the tests that would have caught this.")

    forbidden = repo.get("forbidden_paths") or []
    forbidden_block = ""
    if forbidden:
        listed = "\n".join(f"- {p}" for p in forbidden)
        forbidden_block = (
            "\nPATHS THAT ARE FROZEN - do not read from, write to, or move files here\n"
            f"{listed}\n"
            "Touching a frozen path is a failed task even if everything else is right.\n")

    return f"""{title}

WHY THIS TASK EXISTS (evidence)
{evidence}

WHAT TO DO
{how}
- Do not change unrelated code, formatting, or dependencies.
- Base your work on the {branch} branch.
- Name your branch `{BRANCH_PREFIX}/<short-slug>` so the maintenance loop can
  recognise its own work and not stack a second task on top of it.
{scope_block}{forbidden_block}
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


def run_one(repo, token, jules_key, args, state, orch_token=None, jules_prs=frozenset()):
    """Triage and possibly delegate ONE repository.

    Returns (record, summary_lines, delegated). Every early return still stamps
    rotation state so a permanently-busy or permanently-quiet repository cannot
    pin every future run to itself.

    `token` reads the target repository; `orch_token` writes the approval queue
    here. They are different credentials on purpose: reading a private product
    repository must not carry the right to open issues, and the queue must not
    depend on a token scoped to somebody else's repository.
    """
    orch_token = orch_token or token
    record = {"timestamp": utc_now(), "repo": repo["repo"], "triage_result": None,
              "task_created": False, "jules_session_id": None, "status": None,
              "branch": None, "pr": None, "summary": None, "tests": None,
              "scope_profile": scope_profile(repo),
              "visibility": "private" if is_private(repo) else "public",
              "outcome": None, "dry_run": bool(args.dry_run)}

    def stamp(outcome, detail=None):
        seen = state.setdefault("repos", {}).setdefault(repo["repo"], {})
        seen["last_run"] = utc_now()
        seen["last_outcome"] = outcome
        entry = {"ts": utc_now(), "repo": repo["repo"], "outcome": outcome}
        if detail:
            entry["detail"] = detail
        state.setdefault("history", []).append(entry)

    log(f"selected {repo['repo']} (priority {repo.get('priority')}, "
        f"{record['visibility']}, scope={record['scope_profile']})")

    busy = already_busy(repo, token, jules_key, jules_prs)
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
        kind, title, evidence = find_work(repo, token, done_work_keys(state))

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
        issue_no, why = open_amber_issue(repo, kind, title, evidence, hits, brief,
                                         orch_token)
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
    if kind in PROACTIVE_KINDS:
        # Recorded on DELEGATION, not on merge. The loop cannot see whether a
        # pull request was eventually accepted, and waiting for an outcome it
        # cannot observe would mean re-proposing the same thing every rotation
        # while the first proposal is still being decided. The cost is that
        # declining a proposal retires it silently: to offer it again, edit the
        # backlog line (any wording change is a new fingerprint) or drop the
        # entry from `done_work` in state.json.
        fp = work_fingerprint(repo["repo"], kind, title)
        state.setdefault("done_work", {})[fp] = {
            "ts": utc_now(), "repo": repo["repo"], "kind": kind, "title": title}
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

    # Two credentials with different jobs. GITHUB_TOKEN is the workflow's own
    # token: scoped to this repository, it can open the approval queue here and
    # read public repositories, and nothing else. MAINTENANCE_READ_TOKEN is a
    # read-only fine-grained PAT covering the private product repositories -
    # without it they are simply not maintained, which is a smaller failure than
    # running the loop on a credential wide enough to write to them.
    orch_token = os.environ.get("GITHUB_TOKEN")
    read_token = os.environ.get("MAINTENANCE_READ_TOKEN") or orch_token
    jules_key = os.environ.get("JULES_API_KEY")
    has_private_reader = bool(os.environ.get("MAINTENANCE_READ_TOKEN"))

    run = {"timestamp": utc_now(), "status": None, "outcome": None,
           "dry_run": bool(args.dry_run), "repos": [], "delegated": 0}

    if not orch_token:
        run.update(status="fail_closed", outcome="no GITHUB_TOKEN in environment")
        emit(run, ["### Background maintenance", "Refused: no GITHUB_TOKEN."])
        return 1

    repos = load_repos()
    state = load_state()
    notes = []

    # New Lucky Cat services join on their own. Only ever appends - see
    # discover_family_repos.
    discovered, note = discover_family_repos(repos, read_token)
    if note:
        notes.append(note)
        log(note)
    if discovered:
        repos = repos + discovered
        # A dry run is allowed to look, never to leave anything behind. The
        # manual dispatch defaults to dry_run=true, so writing here would let a
        # "just show me what it would do" click quietly edit the allowlist.
        if args.dry_run:
            log("dry run - allowlist NOT written")
        else:
            save_repos(repos)

    # A private repository is invisible to the workflow's own token. Reading it
    # with a token that cannot see it produces 404s that look exactly like "no
    # work here" - a silent no-op is the worst possible outcome for a loop whose
    # whole job is to notice things, so drop them loudly instead.
    repos, blocked = drop_unreadable_private(repos, has_private_reader)
    if blocked:
        msg = (f"private repositories skipped, MAINTENANCE_READ_TOKEN is not set: "
               f"{', '.join(blocked)}")
        notes.append(msg)
        log(msg)

    # Ask Jules which pull requests it opened before counting them. Branch-name
    # heuristics have already been wrong twice; this is the authoritative answer.
    jules_prs, jules_err = jules_pull_requests(jules_key)
    if jules_err:
        notes.append(f"{jules_err} - falling back to branch heuristics for PR ownership")
        log(notes[-1])

    # Review capacity is the brake, not quota. Checked once, before any triage:
    # if the backlog is already at the cap there is no point discovering more.
    enabled = [r for r in repos if r.get("enabled")]
    open_prs, detail = count_open_maintenance_prs(enabled, read_token, jules_prs)
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
    lines.extend(f"_{n}_" for n in notes)
    if notes:
        lines.append("")
    run["notes"] = notes
    exit_code = 0
    for repo in selected:
        # Re-check the cap between repositories: this run may have just filled it.
        if run["delegated"] + open_prs >= MAX_OPEN_MAINTENANCE_PRS and not args.task:
            lines.append(f"**{repo['repo']}** - not reached, backlog cap hit during this run.")
            break
        record, repo_lines, delegated = run_one(repo, read_token, jules_key, args, state,
                                                orch_token=orch_token, jules_prs=jules_prs)
        run["repos"].append(record)
        lines.extend(repo_lines)
        if delegated:
            run["delegated"] += 1
        if record.get("status") == "fail_closed":
            exit_code = 1

    # A dry run looks and leaves nothing behind - the same rule the allowlist
    # write above already followed, applied to rotation state, which it was not.
    # It matters more than it sounds: `workflow_dispatch` defaults dry_run to
    # TRUE, so every "just show me what it would do" click was stamping
    # last_run on the repositories it inspected and appending history entries
    # for runs that never happened, and the workflow's next step commits that
    # file. The rotation then skipped those repositories on the real run,
    # because as far as the state file knew, they had just been visited.
    if args.dry_run:
        log("dry run - rotation state NOT written")
    else:
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
