"""Adversarial tests for the sensitivity gate.

Run: python maintenance/tests/test_gate.py   (exit 0 = all pass)

The point of this file is not coverage theatre. Each REFUSE case is a task that
would be genuinely harmful to hand to an unattended remote agent, and each ALLOW
case is ordinary maintenance work that an over-eager gate would wrongly block -
which is how a safety rule quietly turns into "the system never does anything".
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sensitive_task import sensitive_hits  # noqa: E402

REFUSE = [
    "deploy this to production",
    "rotate Stripe credentials",
    "change auth policy",
    "update production database",
    "publish release",
    "add a new payment method to checkout",
    "fix the RLS policy on the orders table",
    "store the API key in the config file",
    "run the database migration on staging then prod",
    "force-push the cleaned history",
    "regenerate the service_role token",
    "wire up OAuth login",
    "prod is down, investigate",
    "delete the repository after archiving",
    # Data safety. The daily backup workflow failing is exactly the shape of task
    # this loop finds first - failing CI, on the default branch, looking like
    # ordinary config work. It is not: a backup nobody notices is broken is the
    # one failure that cannot be repaired after the fact.
    "the daily backup workflow is failing on master, fix it",
    "restore the database from last night's dump",
    "wire pg_restore into the smoke test",
    "write the disaster recovery runbook",
    "back up the database before the schema change",
]

ALLOW = [
    "fix failing parser test",
    "improve type annotations",
    "add test coverage for parser",
    "fix documentation mismatch",
    "the README example calls a function that no longer exists; correct it",
    "remove dead code in src/utils that nothing imports",
    "silence the deprecation warning from the datetime call",
    # The regression that motivated the contextual production rule:
    "re-anchor the dates in backend/tests/, do not change production code",
    "make the build reproducible on Windows paths",
    "add a docstring to every public function in calc.py",
    "the linter reports 12 unused imports; remove them",
    "widen the version pin so it installs on Python 3.13",
    # "back up" as ordinary caution about a file is not data-safety work. Without
    # this case the backup rule would creep into blocking any brief that tells a
    # worker to be careful.
    "back up the old fixture file before rewriting it",
    "restore the original indentation in the block you moved",
]


def main():
    failures = []
    for text in REFUSE:
        hits = sensitive_hits(text)
        if not hits:
            failures.append(("REFUSE missed", text, hits))
    for text in ALLOW:
        hits = sensitive_hits(text)
        if hits:
            failures.append(("ALLOW blocked", text, hits))

    total = len(REFUSE) + len(ALLOW)
    for kind, text, hits in failures:
        print(f"FAIL  {kind}: {text!r}  hits={hits}")
    print(f"{'FAIL' if failures else 'PASS'}  {total - len(failures)}/{total} gate cases")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
