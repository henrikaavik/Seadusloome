# Accessibility Checklist

Every new UI component and page must pass this checklist before being
considered done. Baseline: **WCAG 2.1 AA**, per [`docs/nfr-baseline.md`](nfr-baseline.md) §10.

---

## 1. Keyboard navigation

- [ ] Every interactive element is reachable via Tab
- [ ] Tab order matches visual order
- [ ] Custom components (Tabs, Modal, DataTable) expose expected keyboard patterns:
  - Tabs: arrow keys navigate, Home/End jump, Enter/Space activate
  - Modal: Escape closes, Tab cycles inside, Shift-Tab reverses, focus returns to trigger on close
  - Menu dropdowns: Escape closes, arrow keys navigate
- [ ] No keyboard trap (user can always escape an area)
- [ ] Skip-to-content link is the first focusable element and becomes visible on focus

## 2. Focus management

- [ ] Every interactive element has a visible focus ring (`:focus-visible`)
- [ ] Focus ring uses `--color-focus-ring` (2px solid outline)
- [ ] Focus is moved to the modal on open, returned to trigger on close
- [ ] Focus does not get lost after HTMX swaps (use `hx-focus-scroll` if needed)

## 3. Semantic HTML

- [ ] Headings nest correctly (h1 → h2 → h3, no skipping)
- [ ] Lists use `<ul>` / `<ol>` / `<dl>`
- [ ] Tables use `<table>` with `<thead>` and `<tbody>`
- [ ] Forms use `<form>`, `<label>`, `<fieldset>`, `<legend>` where appropriate
- [ ] Navigation uses `<nav>` with `aria-label`
- [ ] Main content is wrapped in `<main>` with an id for skip link target
- [ ] Landmarks: `<header>`, `<nav>`, `<main>`, `<aside>`, `<footer>`

## 4. ARIA attributes

- [ ] Icon-only buttons have `aria-label`
- [ ] Decorative icons have `aria-hidden="true"`
- [ ] Error states on inputs have `aria-invalid="true"`
- [ ] Help text and error messages are linked via `aria-describedby`
- [ ] Modal dialogs have `role="dialog"` + `aria-modal="true"` + `aria-labelledby`
- [ ] Tabs have `role="tablist"` / `role="tab"` / `role="tabpanel"` with `aria-selected`, `aria-controls`, `aria-labelledby`
- [ ] Toasts use `role="status"` + `aria-live="polite"`
- [ ] Loading spinners use `role="status"` + screen-reader-only text
- [ ] DataTable sortable columns expose `aria-sort="ascending|descending|none"`
- [ ] Breadcrumbs: current item has `aria-current="page"`
- [ ] Pagination: current page has `aria-current="page"`
- [ ] Alerts use `role="alert"` for errors, `role="status"` for info
- [ ] Skip link is the first element in the page

## 5. Color contrast

- [ ] Body text: 4.5:1 minimum (AA)
- [ ] Large text (18pt+ or 14pt bold): 3:1 minimum
- [ ] UI chrome (borders, focus rings, icons): 3:1 minimum
- [ ] No colour-only information (also use icons, text, or patterns)

Verified contrast pairs (light theme):

| Foreground | Background | Ratio | AA? |
|------------|-----------|-------|-----|
| Mustkivi (#0F172A) | Pahkla (#F1F5F9) | 16:1 | ✓ |
| Mustkivi (#0F172A) | Ehakivi (#FFFFFF) | 18:1 | ✓ |
| White on Estonian Blue (#0030DE) | | 8.6:1 | ✓ |
| Kabelikivi (#64748B) | Pahkla (#F1F5F9) | 4.8:1 | ✓ |
| White on Danger (#B91C1C) | | 5.9:1 | ✓ |

Verified contrast pairs (dark theme):

| Foreground | Background | Ratio | AA? |
|------------|-----------|-------|-----|
| Ehakivi (#FFFFFF) | Mustkivi (#0F172A) | 18:1 | ✓ |
| Mustkivi (#0F172A) | Narva (#00C3FF) | 9.3:1 | ✓ |
| Hellamaa (#CBD5E1) | Mustkivi (#0F172A) | 12:1 | ✓ |

## 6. Form accessibility

- [ ] Every input has a visible `<label>`
- [ ] Labels linked via `for`/`id` (FastHTML: use `fr=` attribute)
- [ ] Required fields marked with text or asterisk (not colour alone)
- [ ] Error messages linked via `aria-describedby` and use `role="alert"`
- [ ] Placeholder text is not used as the sole label
- [ ] Field groups use `<fieldset>` + `<legend>`
- [ ] Form submission errors are announced

## 7. Media and motion

- [ ] `@media (prefers-reduced-motion: reduce)` disables animations
- [ ] No auto-playing audio or video
- [ ] Animated content (toasts, spinners) respects reduced motion
- [ ] Decorative images use empty `alt=""`
- [ ] Content images use descriptive `alt` text
- [ ] Complex graphics have text alternatives

## 8. Internationalization

- [ ] `<html lang="et">` set correctly
- [ ] No text baked into images (use SVG with `<text>` or CSS)
- [ ] Content handles Estonian characters (ä, ö, ü, õ, š, ž) correctly

## 9. Screen reader smoke test

Run a manual screen reader test for every new page using VoiceOver (macOS)
or NVDA (Windows):

1. Navigate the page using Tab only — do you understand the page structure?
2. Use landmark navigation (VO+U) — are `<main>`, `<nav>`, `<header>` all present?
3. Use heading navigation (VO+Cmd+H) — does the outline make sense?
4. Try submitting a form with invalid data — are errors announced?
5. Open a modal — does focus move correctly?

## 10. Automated tests

- Axe-core smoke tests (manual for now, will be automated in CI per #363)
- `tests/test_ui_*.py` verify semantic markup in rendered HTML
- Component library smoke tests check for correct aria attributes

## Component status

| Component | Keyboard | ARIA | Contrast | Status |
|-----------|:--------:|:----:|:--------:|:------:|
| Button | ✓ | ✓ | ✓ | ✓ |
| IconButton | ✓ | ✓ (aria-label required) | ✓ | ✓ |
| Input / Textarea / Select | ✓ | ✓ (aria-invalid) | ✓ | ✓ |
| Checkbox / Radio | ✓ | ✓ | ✓ | ✓ |
| FormField | ✓ | ✓ (aria-describedby) | ✓ | ✓ |
| Card | n/a | n/a | ✓ | ✓ |
| Alert | n/a | ✓ (role=alert) | ✓ | ✓ |
| Badge / StatusBadge | n/a | ✓ | ✓ | ✓ |
| Toast | n/a | ✓ (role=status) | ✓ | ✓ |
| LoadingSpinner | n/a | ✓ (role=status) | ✓ | ✓ |
| Skeleton | n/a | ✓ (aria-busy) | ✓ | ✓ |
| EmptyState | ✓ | n/a | ✓ | ✓ |
| Modal | ✓ (focus trap, Escape) | ✓ | ✓ | ✓ |
| DataTable | ✓ | ✓ (aria-sort) | ✓ | ✓ |
| Pagination | ✓ | ✓ (aria-current) | ✓ | ✓ |
| Icon | n/a | ✓ (aria-hidden default) | inherits | ✓ |
| Breadcrumb | ✓ | ✓ (aria-current) | ✓ | ✓ |
| Tabs | ✓ (arrow keys) | ✓ (full ARIA) | ✓ | ✓ |
| TopBar | ✓ | ✓ | ✓ | ✓ |
| Sidebar | ✓ | ✓ (aria-current) | ✓ | ✓ |
| PageShell (skip link) | ✓ | ✓ | ✓ | ✓ |

## Known issues

- Aino font fallback to Verdana may slightly change visual rhythm but
  contrast ratios remain compliant
- Automated axe-core tests in CI are tracked under #363 (not yet
  implemented — manual checklist used)
- Screen reader testing is currently manual; recurring smoke tests will
  be added when pilot users start
