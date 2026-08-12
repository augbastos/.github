"""Is this task too sensitive to hand to a remote vendor?

Faithful port of ~/.claude/scripts/lib/sensitive-task.ps1, which is the gate the
local Jules launcher already enforces. The runner cannot reach ~/.claude, so the
rules live here too - and tests/test_gate.py pins them to the same cases.

Why "production" is contextual and not a bare word match
--------------------------------------------------------
In software English the word carries two unrelated senses:

  1. OPERATIONAL - the live system. "deploy to production", "production
     database", "rotate production credentials". This is what the gate exists to
     catch.
  2. STRUCTURAL - source that is not test code. "do not change production code",
     "production-ready". Benign, and exactly the phrasing a careful task brief
     uses when telling a worker to stay inside the test tree.

A measured false positive (2026-08-12): a brief scoped to "re-anchor dates in
backend/tests/, do not change production code" was refused. It was denied for
containing the word describing the files it was forbidding itself to touch.

The fix is not to drop the rule. It is to require the operational sense.
Fail-closed stays the design: where the two senses are genuinely ambiguous,
prefer the hit. A refusal costs one rewrite; a miss sends live-system work to a
third party.
"""

import re

_PROD_OPERATIONAL_NOUN = (
    r"deploy(?:ment)?s?|environments?|envs?|servers?|clusters?|databases?|dbs?|"
    r"credentials?|secrets?|keys?|tokens?|data|datasets?|traffic|"
    r"infra(?:structure)?|instances?|buckets?|tenants?|accounts?|"
    r"rollouts?|releases?|incidents?|outages?|endpoints?|domains?|dns|"
    r"apis?|hosts?|nodes?|queues?|webhooks?|"
    r"backups?|migrations?|workloads?|pipelines?"
)

_PROD_STRUCTURAL_NOUN = (
    r"code|codebase|source|sources|build|builds|bundle|artifact|artifacts|"
    r"dependency|dependencies|deps|ready|readiness|grade|quality|path|paths|"
    r"behaviour|behavior|semantics|defaults?"
)

SENSITIVE_PATTERNS = [
    # data-layer authorization
    (r"\bRLS\b", "rls"),
    (r"row.level.security", "rls"),
    (r"service_role", "service_role"),
    # money
    (r"\bstripe\b", "payments"),
    (r"\bpayments?\b", "payments"),
    (r"\bbilling\b", "billing"),
    # credentials
    (r"\bcredentials?\b", "credentials"),
    (r"\bapi[ _-]?keys?\b", "credentials"),
    (r"\bsecrets?\b", "credentials"),
    (r"\boauth\b", "auth"),
    (r"\bauth\b", "auth"),
    (r"\bauthentication\b", "auth"),
    (r"\bauthorization\b", "auth"),
    # shipping - bare on purpose: unlike "production" these have no benign
    # structural sense.
    (r"\bdeploy(ment)?\b", "deploy"),
    (r"\brelease\b", "release"),
    # the live system, contextual
    (rf"\b(?:production|prod)[-\s]+(?:{_PROD_OPERATIONAL_NOUN})\b", "production_system"),
    (
        rf"\b(?:to|in|on|against|from|onto|towards?)\s+(?:the\s+)?prod(?:uction)?\b"
        rf"(?!\s*[-\s]\s*(?:{_PROD_STRUCTURAL_NOUN})\b)",
        "production_system",
    ),
    (r"\bprod(?:uction)?\s+(?:is|was|went)\s+(?:down|broken|failing|degraded)\b",
     "production_system"),
    # schema surgery
    (r"\bdb migration\b", "db_migration"),
    (r"\bdatabase migration\b", "db_migration"),
    # the last line of defence. A broken deploy is rolled back; a broken backup
    # is discovered on the day it is needed and cannot be undone, so this is not
    # unattended work no matter how much it looks like ordinary CI. The noun is
    # bare, like deploy and release; the verb form is required to name real data
    # so that "back up the old fixture before rewriting it" stays ordinary work.
    (r"\bbackups?\b", "backup_restore"),
    (r"\bback\s+up\s+(?:the\s+)?(?:database|db|data|volume|bucket|storage)\b",
     "backup_restore"),
    (r"\bdisaster[-\s]?recovery\b", "backup_restore"),
    (r"\bpg_?(?:dump|restore)\b", "backup_restore"),
    (r"\brestor(?:e|ing|ation)\s+(?:the\s+)?(?:backup|database|db|dump|snapshot|data)\b",
     "backup_restore"),
    (r"\b(?:database|db|data|snapshot|dump)\s+restor(?:e|ation)\b", "backup_restore"),
    # destructive repository operations - background work never does these
    (r"\bforce[- ]push\b", "destructive_repo"),
    (r"\bgit\s+push\s+--force\b", "destructive_repo"),
    (r"\brewrite\s+(?:the\s+)?(?:git\s+)?history\b", "destructive_repo"),
    (r"\bdelete\s+(?:the\s+)?(?:repo|repository|branch\s+protection)\b", "destructive_repo"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in SENSITIVE_PATTERNS]


def sensitive_hits(text):
    """Return the distinct category labels this text trips. Empty means clean."""
    if not text or not text.strip():
        return []
    hits = []
    for rx, label in _COMPILED:
        if rx.search(text) and label not in hits:
            hits.append(label)
    return hits


def is_sensitive(text):
    return bool(sensitive_hits(text))
