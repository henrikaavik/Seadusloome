# 2026-04-30 — Preventing password-management from disappearing again

## What happened

The password-management feature was developed on `feature/password-management`
(15 commits, complete with tests). It was never merged into `main`. Instead,
during the 2026-04-29 UI review follow-ups (commit `7c786ce`) a *partial*
reimplementation was added directly to `main`: just `validate_password`,
`change_password`, `/profile/password`, the must_change_password middleware
redirect, and migration `024_must_change_password.sql`.

Six weeks later it surfaced as "I wanted admin password reset, where is it?" —
because the visible entry points (login forgot link, admin reset button) were
on the orphaned feature branch.

## Why a UI smoke test is the most cost-effective guardrail

The test suite at the time had thorough unit coverage of `change_password`,
`issue_reset_token`, `claim_reset_token`, the rate limiter, and the email
provider — all on the feature branch. None of it ran on `main` because the
*entry points* into those flows were missing. A reviewer reading a 89-file
"UI follow-ups" diff cannot reasonably be expected to notice that one button
is gone. A regression test can.

## The rules going forward

1. **Every user-visible feature has a UI smoke test** that fetches a real
   page and asserts the entry-point HTML is present. See
   `tests/test_password_ui_smoke.py` for the canonical pattern.

2. **Long-lived feature branches must be merged via PR, not cherry-picked
   piecemeal.** If `feature/password-management` had been merged via a single
   `git merge` then `7c786ce` would have been a *sibling* commit and the
   conflict would have surfaced visibly.

3. **"Follow-ups" / "review fixes" / "polish" commits that touch >20 files
   require a code-review agent pass.** The 7c786ce commit message lists 12
   intentional changes; any other change in that diff is suspect by default.

4. **Maintain `docs/feature-inventory.md` (TODO).** A one-line-per-flow list
   of "Feature → Entry-point file:line → smoke test" makes orphaned features
   findable by `grep` rather than by user complaints.
