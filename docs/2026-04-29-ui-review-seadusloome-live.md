# UI Review: seadusloome.sixtyfour.ee

Date: 2026-04-29

Scope: Live deployed site at https://seadusloome.sixtyfour.ee/. Tested in headless Chrome at desktop `1440x1000` and mobile `390x844`.

Method: Click-through of unauthenticated login, authenticated explorer, dashboard, drafts, drafter, chat, admin pages, admin quick links, user menu, notification dropdown, desktop keyboard tab order, and mobile layouts. Review checklist was based on the current [Vercel Web Interface Guidelines](https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md) plus the project-specific Estonian/legal workflow expectations.

Note: The live site accepted the seeded admin account from the migrations. I used it only for read-only review and did not trigger create, delete, sync, retry, upload, logout, or other POST-style workflow actions.

## Summary

The basic authenticated application is understandable: login works, the main PageShell pages have a consistent dark layout, keyboard focus is visible, and the drafts/chat tables collapse into readable cards on mobile. The highest-risk problems are that the live deployment still accepts the default seeded admin credentials, several admin links lead to raw 500 pages, and the explorer is not integrated with the shared layout/accessibility structure.

## Findings

### P0 - Live deployment accepts seeded admin credentials

The deployed site allowed login as the seeded admin user (`admin@seadusloome.ee`) with the documented seed password from the migrations. That gives immediate access to admin dashboards, users, organizations, system health, costs, notifications, and draft/chat metadata. The migration comment says the admin "MUST change this from the UI on first login", but the UI did not force a password change after login.

Likely source: `migrations/004_admin_seed_fix.sql:11-16`, `migrations/004_admin_seed_fix.sql:23-34`.

Recommended fix: rotate the live admin password immediately, invalidate existing sessions, remove hard-coded usable production credentials, and add a forced first-login password change or deployment-time secret requirement for the initial admin account.

### P1 - Admin quick links lead to raw 500 pages

From the admin dashboard quick links, these authenticated routes returned `Internal Server Error`: `/admin/audit`, `/admin/jobs`, `/admin/analytics`, `/admin/costs`, and `/admin/performance`. The dashboard presents them as normal navigation, so an admin hits dead ends during routine monitoring.

Likely source: quick links are emitted from `app/admin/users.py:85-97`; routes are registered in `app/templates/admin_dashboard.py:280-288`.

Recommended fix: fix the failing handlers, add live-smoke coverage for every admin dashboard quick link, and return a styled `PageShell` error state if a backend dependency is unavailable.

### P1 - User menu profile link is dead

The user menu opens correctly, but `Minu profiil` links to `/profile`, which returned `404 Not Found` on the live site. The password-management plan mentions `/profile`, but the route is not registered in the current app.

Likely source: `app/ui/layout/top_bar.py:22-30`; planned but not implemented in `docs/superpowers/specs/2026-04-28-password-management-design.md`.

Recommended fix: either implement and register `/profile` and `/profile/password`, or remove the menu item until that feature ships.

### P1 - The app does not set `lang="et"`

Every tested page had an empty document language (`document.documentElement.lang === ""`). This matters because the UI is Estonian and Chrome showed native validation text in English on the login email field. Screen readers and translation tools also lack the correct language context.

Likely source: shared pages are created by `fast_app(...)` at `app/main.py:151`; the custom explorer page returns `Html(...)` at `app/explorer/pages.py:297` without a language attribute.

Recommended fix: set the root document language to Estonian for both FastHTML default pages and the custom explorer page.

### P2 - Login fields miss authentication autocomplete and rely on English native validation

The login form labels are present, but the email and password inputs have no `autocomplete` attributes. Submitting an invalid email produced the browser-native English validation bubble while the inline validation text was Estonian. This is inconsistent for an Estonian government-facing UI.

Likely source: login form fields are defined in `app/auth/routes.py:34-47`; `FormField(...)` does not expose input-level attributes like `autocomplete` to `Input(...)` because `**kwargs` are applied to the wrapper at `app/ui/forms/form_field.py:102-108`; `Input(...)` itself can accept arbitrary attributes at `app/ui/primitives/input.py:43-61`.

Recommended fix: allow `FormField` to pass input attributes through, set `autocomplete="email"` and `autocomplete="current-password"` on login, and use either localized custom submit validation or `novalidate` plus the existing Estonian inline validator.

### P2 - Explorer navigation uses buttons for page navigation

The explorer sidebar-like navigation uses `<button onclick="location.href='...'">` for `Töölaud`, `Eelnõud`, `Koostaja`, `Vestlus`, and `Admin`. These are links, not actions, so users lose expected browser behavior like open in new tab, copy link, and correct semantics.

Likely source: `app/explorer/pages.py:373-397`.

Recommended fix: render these items as anchors with real `href` values, styled like the current controls.

### P2 - Explorer lacks landmarks and labels for key controls

The explorer page has `mainCount=0` and `navCount=0`, unlike the shared PageShell pages. Its search input and timeline range input are visible but have no label or `aria-label`. The search disappears on mobile, so mobile users lose direct search access.

Likely source: custom page structure and controls in `app/explorer/pages.py:297-342`, timeline/detail controls in `app/explorer/pages.py:430-545`.

Recommended fix: add semantic landmarks (`header`, `nav`, `main`), label the search and timeline controls, and preserve a compact mobile search affordance.

### P2 - Explorer mobile layout overlaps

On `390x844`, the explorer title/badges, help banner, controls, graph, and legend overlap. The info banner floats over the graph and the top controls take most of the first viewport, making the graph hard to inspect. The PageShell pages reflow much better than the custom explorer page.

Likely source: fixed positioning and limited responsive overrides in `app/static/css/explorer.css:16-21`, `app/static/css/explorer.css:48-53`, `app/static/css/explorer.css:331-345`, and `app/static/css/explorer.css:386-395`.

Recommended fix: define a mobile explorer layout explicitly: collapsible controls, banner below the header or dismissible by default, reserved graph viewport, and a compact legend/details panel.

### P2 - Common navigation omits AI Koostaja

`/drafter` works and the explorer has a `Koostaja` navigation button, but the normal authenticated PageShell top bar and sidebar do not include it. That makes a core module discoverable from only one surface and by direct URL.

Likely source: sidebar nav list at `app/ui/layout/sidebar.py:9-15`; top nav at `app/ui/layout/top_bar.py:111-116`; explorer-only button at `app/explorer/pages.py:383-386`.

Recommended fix: add `Koostaja` to the shared authenticated navigation for roles that can use it, and keep naming consistent across pages.

### P2 - Notification dropdown copy lacks Estonian diacritics

The notification dropdown displayed `Moju analuus valmis` and `Vaata koiki`. The rest of the UI uses Estonian diacritics, so this reads as unfinished or incorrectly encoded copy.

Likely source: `app/notifications/wire.py:96-101` and `app/notifications/routes.py:364-366`.

Recommended fix: use `Mõjuanalüüs valmis`, `Eelnõu ... mõjuanalüüs on valmis`, and `Vaata kõiki`; add a regression test for localized notification labels.

### P2 - Aino font files are referenced but missing in production

Every tested PageShell page logged 404s for `/static/fonts/aino/Aino-Regular.woff2` and `/static/fonts/aino/Aino-Bold.woff2`. The repo also only contains `app/static/fonts/aino/README.md`, not the actual font files. The visual fallback works, but production should not ship expected asset 404s.

Likely source: `app/static/css/fonts.css:13-27`; missing assets under `app/static/fonts/aino/`.

Recommended fix: either deploy licensed Aino WOFF2 files or remove/disable the `@font-face` declarations until the assets are available.

### P3 - Dashboard/admin tables expose raw technical payloads

The user dashboard recent-activity table shows raw Python dict strings with UUIDs. The admin sync table shows very long SHACL warning text inline, making the admin page hard to scan on desktop and especially mobile.

Likely source: dashboard details stringification at `app/templates/dashboard.py:191-204`; sync error rendering at `app/admin/sync.py:302-329`.

Recommended fix: render audit details as human-readable summaries with expandable raw JSON, and truncate long sync errors with a `Vaata detaili` disclosure or detail page.

## What Worked

- Login redirect flow is coherent for both unauthenticated root and protected pages.
- Keyboard tab order on login is logical: skip link, brand, login link, email, password, submit.
- Visible focus rings are present on the login page.
- PageShell desktop layout is understandable and visually consistent across dashboard, drafts, chat, drafter, and admin.
- Drafts and chat tables collapse into card-like rows on mobile without horizontal overflow.
- Destructive chat actions use confirmation (`hx_confirm`) in source; draft delete uses a modal flow.

## Tested URLs

- `/auth/login`
- `/`
- `/dashboard`
- `/drafts`
- `/drafter`
- `/chat`
- `/admin`
- `/admin/users`
- `/admin/organizations`
- `/admin/audit`
- `/admin/jobs`
- `/admin/analytics`
- `/admin/costs`
- `/admin/performance`
- `/profile`
- `/api/notifications`
