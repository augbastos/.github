# Security policy

Account-level default for public repositories under `augbastos`. A repository with a different
disclosure channel overrides this file.

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting.** On the repository, open the **Security** tab
and choose *Report a vulnerability*. That opens a private thread visible only to you and the
maintainer.

If the Security tab does not offer it on a given repository, open a normal issue that says only
*"I would like to report a security issue privately"* - with no details - and a private channel
will be opened from there.

**Please do not** put the details in a public issue, a pull request, a discussion, or a commit
message.

## What to expect

One person maintains these projects, in evenings and weekends, in Ireland (IST/GMT). Honest
numbers rather than a copied SLA:

| | |
|---|---|
| First response | usually within a week |
| Assessment | depends entirely on the finding |
| Fix | as fast as severity and my week allow |

If a week passes with no reply, send a reminder. It means the notification was missed, not that
the report was ignored.

## Scope

In scope: the code in these repositories.

Out of scope, because they are not mine to fix: GitHub itself, Cloudflare, Supabase, Stripe,
and any other third-party service these projects call. Report those to the vendor.

Also out of scope: findings from an automated scanner pasted in with no analysis. A CVE in a
transitive dependency that the code never reaches is not a vulnerability in the project, and
triaging that costs the same hour as a real report.

## Supported versions

Only the default branch. These are small projects; there are no maintained release branches and
no backports. Where a repository publishes releases, the fix ships in the next one.

## Credit

If you want credit, say so and you get it in the release notes and the advisory. If you prefer
to stay anonymous, that is respected.

There is no bug bounty. No money, no swag - just thanks and credit, offered honestly rather
than implied and then withheld.
