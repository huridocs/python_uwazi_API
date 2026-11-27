"""One Uwazi V2 design layer for every admin-agent page.

The review page grew a hand-rolled stylesheet while the rest of the app kept
NiceGUI's defaults. This module is the single source of truth for that look:
the warm Uwazi palette expressed as CSS variables, the page/card/table chrome,
and the small set of class names every page shares (``uw-page``, ``uw-card``,
``uw-table``, ``uw-modal-header``, …).

There is no Tailwind build here, so the Uwazi token names are kept as CSS
variables (``--uw-paper``, ``--uw-ink``, …) and the reusable selectors are plain
classes. ``install_theme`` sets the Quasar palette to the same values, which
keeps ``color="primary"`` buttons/badges/progress bars in step with the rest.
"""

from __future__ import annotations

from nicegui import ui

# The Uwazi V2 palette (light theme). The dark values are the documented
# counterparts; NiceGUI's body--dark class switches them.
THEME_CSS = """
:root {
  --uw-font-sans: Inter, "Noto Sans", ui-sans-serif, system-ui, sans-serif;
  --uw-font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;

  --uw-parchment: #f5f0e8;
  --uw-paper: #ffffff;
  --uw-warm: #fcfaf8;
  --uw-vellum: #f5eed7;
  --uw-selected: #f0eded;

  --uw-ink: #1a1a1a;
  --uw-ink-secondary: #333333;
  --uw-ink-tertiary: #555555;
  --uw-ink-muted: #777777;

  --uw-border: #ede7db;
  --uw-border-soft: #e2dbc9;

  --uw-carbon: #00b4f0;
  --uw-carbon-tint: #e7f7fd;
  --uw-emphasis: #e8432a;
  --uw-emphasis-tint: #fcece9;
  --uw-success: #059669;
  --uw-success-light: #d1fae5;
  --uw-warning: #f59e0b;
  --uw-warning-light: #fef3c7;

  --uw-radius-sm: 6px;
  --uw-radius: 8px;
  --uw-radius-lg: 12px;
  --uw-shadow-sm: 0 1px 2px rgb(0 0 0 / 0.08);
  --uw-shadow-md: 0 1px 3px rgb(0 0 0 / 0.1), 0 1px 2px rgb(0 0 0 / 0.06);
  --uw-shadow-lg: 0 10px 15px -3px rgb(0 0 0 / 0.08), 0 4px 6px -4px rgb(0 0 0 / 0.08);
  --uw-ring: color-mix(in srgb, #00b4f0 25%, transparent);

  --q-primary: #1a1a1a;
  --q-secondary: #00b4f0;
  --q-accent: #e8432a;
  --q-positive: #059669;
  --q-negative: #e8432a;
  --q-warning: #f59e0b;
  --q-info: #00b4f0;
}

body.body--dark {
  --uw-parchment: #1a1a1a;
  --uw-paper: #242424;
  --uw-warm: #2a2a2a;
  --uw-vellum: #333333;
  --uw-selected: #333333;
  --uw-ink: #f5f0e8;
  --uw-ink-secondary: #d4cdb8;
  --uw-ink-tertiary: #ada79e;
  --uw-ink-muted: #8a857c;
  --uw-border: #514e48;
  --uw-border-soft: #5e5a53;
  --uw-carbon-tint: #12323c;
  --uw-emphasis-tint: #3a1d18;
  --uw-shadow-sm: 0 1px 2px rgb(0 0 0 / 0.25);
  --uw-shadow-md: 0 1px 3px rgb(0 0 0 / 0.25), 0 1px 2px rgb(0 0 0 / 0.15);
  --uw-shadow-lg: 0 10px 15px -3px rgb(0 0 0 / 0.4);
}

html, body {
  background: var(--uw-parchment);
  color: var(--uw-ink);
  font-family: var(--uw-font-sans);
}

body { overflow-x: hidden; }
.nicegui-content { padding: 0; gap: 0; }
.q-layout, .q-page-container, .q-page { background: transparent; }
.q-drawer, .q-menu { background: var(--uw-paper); color: var(--uw-ink); }
.q-menu {
  border: 1px solid var(--uw-border);
  border-radius: var(--uw-radius);
  box-shadow: var(--uw-shadow-lg);
  min-width: 210px;
}
.q-item { color: var(--uw-ink-secondary); border-radius: var(--uw-radius-sm); }
.q-item.q-router-link--active, .q-item--active { color: var(--uw-ink); background: var(--uw-warm); }
.q-separator { background: var(--uw-border-soft); }

/* --- text and the Quasar palette ------------------------------------------ */
.text-grey-5, .text-grey-6 { color: var(--uw-ink-muted) !important; }
.text-grey-7 { color: var(--uw-ink-tertiary) !important; }
.text-grey-8, .text-grey-9, .text-grey-10 { color: var(--uw-ink-secondary) !important; }
.text-primary { color: var(--uw-ink) !important; }
.text-secondary { color: var(--uw-carbon) !important; }
.text-negative { color: var(--uw-emphasis) !important; }
.text-positive { color: var(--uw-success) !important; }
.text-warning { color: var(--uw-warning) !important; }
.text-teal, .text-teal-9 { color: var(--uw-success) !important; }
.text-red-10 { color: var(--uw-emphasis) !important; }
.text-orange-9 { color: var(--uw-warning) !important; }
.bg-grey-2, .bg-grey-3 { background: var(--uw-warm) !important; }
.bg-grey-1, .bg-grey-2 { background: var(--uw-warm) !important; }
.bg-blue-1 { background: var(--uw-carbon-tint) !important; }
.bg-orange-1 { background: var(--uw-warning-light) !important; }

/* --- buttons -------------------------------------------------------------- */
.q-btn {
  text-transform: none;
  font-weight: 600;
  letter-spacing: 0;
  box-shadow: none;
}
.q-btn:not(.q-btn--round) { border-radius: var(--uw-radius-sm); }
.q-btn:not(.q-btn--round) .q-btn__content { gap: 4px; }
.q-btn--flat { color: inherit; }
.q-btn--flat:hover { background: var(--uw-warm); }
.q-btn--outline:before { border-color: var(--uw-border) !important; }
.q-btn--outline:hover:before { border-color: var(--uw-border-soft) !important; }
.q-btn.bg-primary { background: var(--uw-ink) !important; color: var(--uw-parchment) !important; }
.q-btn.bg-primary:hover { opacity: 0.9; }
.q-btn.bg-negative { background: var(--uw-emphasis) !important; color: #fff !important; }
.q-btn.bg-positive { background: var(--uw-success) !important; color: #fff !important; }
.q-btn.bg-secondary, .q-btn.bg-info { background: var(--uw-carbon) !important; color: #fff !important; }
.q-btn.bg-warning { background: var(--uw-warning) !important; color: #1a1a1a !important; }
.q-btn.bg-grey-8 { background: var(--uw-ink-secondary) !important; color: #fff !important; }
.q-btn.bg-purple, .q-btn.bg-deep-orange { background: var(--uw-ink-secondary) !important; color: #fff !important; }
.uw-btn-ghost { color: var(--uw-ink-tertiary) !important; }
.uw-btn-ghost:hover { color: var(--uw-ink) !important; background: var(--uw-warm) !important; }
.uw-btn-danger { color: var(--uw-emphasis) !important; }
.uw-btn-danger:hover { background: var(--uw-emphasis-tint) !important; }
.uw-btn-compact {
  border: 1px solid var(--uw-border-soft) !important;
  background: var(--uw-warm) !important;
  color: var(--uw-ink-secondary) !important;
}
.uw-btn-compact:hover { background: var(--uw-vellum) !important; border-color: var(--uw-border) !important; }

/* --- inputs --------------------------------------------------------------- */
.q-field--outlined .q-field__control {
  border-radius: var(--uw-radius) !important;
  background: var(--uw-paper);
}
.q-field--outlined .q-field__control:before { border-color: var(--uw-border-soft) !important; }
.q-field--outlined.q-field--focused .q-field__control:after {
  border-color: var(--uw-carbon) !important;
}
.q-field--outlined.q-field--focused .q-field__control {
  box-shadow: 0 0 0 4px var(--uw-ring);
}
.q-field__native, .q-field__input, .q-field__prefix, .q-field__suffix {
  color: var(--uw-ink) !important;
  font-size: 14px;
}
.q-field__label { color: var(--uw-ink-tertiary) !important; font-size: 13px; }
.q-field--dense .q-field__control, .q-field--dense .q-field__marginal { min-height: 36px; }
.q-field__bottom { color: var(--uw-ink-muted); }
.q-field--error .q-field__control:before, .q-field--error .q-field__control:after {
  border-color: var(--uw-emphasis) !important;
}
.q-checkbox__inner--truthy, .q-radio__inner--truthy { color: var(--uw-ink) !important; }
.q-toggle__inner--truthy { color: var(--uw-ink) !important; }
.q-chip {
  background: var(--uw-warm) !important;
  color: var(--uw-ink-secondary) !important;
  border-radius: var(--uw-radius-sm) !important;
}

/* --- page chrome ---------------------------------------------------------- */
.uw-header {
  background: var(--uw-paper) !important;
  color: var(--uw-ink) !important;
  border-bottom: 1px solid var(--uw-border);
  box-shadow: none !important;
  padding: 0 8px;
  min-height: 56px;
}
.uw-header .q-link { color: var(--uw-ink-tertiary); text-decoration: none; font-size: 13px; }
.uw-header .q-link:hover { color: var(--uw-ink); }
.uw-header .q-btn { color: var(--uw-ink-secondary); }
.uw-header .q-btn:hover { background: var(--uw-warm); }

.brand-mark {
  width: 28px;
  height: 28px;
  border-radius: var(--uw-radius);
  background: var(--uw-emphasis);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  font-size: 15px;
  flex: 0 0 auto;
}
.brand-name { font-size: 15px; font-weight: 700; color: var(--uw-ink); letter-spacing: -0.01em; }
.brand-sub { font-size: 11px; color: var(--uw-ink-muted); }
.header-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: var(--uw-radius-sm);
  padding: 6px 10px;
  color: var(--uw-ink-tertiary);
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
}
.header-tab:hover { background: var(--uw-warm); color: var(--uw-ink); }
.header-tab-active { background: var(--uw-warm); color: var(--uw-ink); }
.header-separator { width: 1px; height: 24px; background: var(--uw-border); flex: 0 0 auto; }

.uw-page {
  width: 100%;
  max-width: 1360px;
  margin: 0 auto;
  padding: 24px 24px 96px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.uw-page-narrow { max-width: 760px; }
.uw-page-heading { display: flex; flex-direction: column; gap: 2px; }
.uw-page-title { font-size: 22px; line-height: 1.2; font-weight: 700; color: var(--uw-ink); letter-spacing: -0.02em; }
.uw-page-subtitle { font-size: 13px; color: var(--uw-ink-tertiary); }
.uw-eyebrow {
  font-size: 10px;
  line-height: 1.2;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--uw-ink-tertiary);
}

.uw-card {
  background: var(--uw-paper);
  border: 1px solid var(--uw-border);
  border-radius: var(--uw-radius);
  box-shadow: var(--uw-shadow-md);
  overflow: hidden;
}
.uw-card-header {
  padding: 16px 20px;
  border-bottom: 1px solid var(--uw-border-soft);
  background: var(--uw-paper);
}
.uw-card-title { font-size: 20px; line-height: 1.2; font-weight: 700; color: var(--uw-ink); }
.uw-card-subtitle { font-size: 13px; color: var(--uw-ink-tertiary); }
.uw-card-body { padding: 16px 20px; }
.uw-card-footer {
  padding: 12px 20px;
  border-top: 1px solid var(--uw-border-soft);
  background: var(--uw-paper);
}
.uw-subcard {
  background: var(--uw-warm) !important;
  border: 1px solid var(--uw-border-soft) !important;
  border-radius: var(--uw-radius) !important;
  box-shadow: none !important;
}
.uw-callout {
  border: 1px solid var(--uw-border-soft);
  background: var(--uw-warm);
  border-radius: var(--uw-radius);
  padding: 12px 14px;
}
.uw-callout-warning {
  border: 1px solid color-mix(in srgb, var(--uw-warning) 30%, var(--uw-border));
  background: var(--uw-warning-light);
  border-radius: var(--uw-radius);
  padding: 12px 14px;
}

/* Quasar cards compose the same paper surface everywhere. */
.q-card {
  background: var(--uw-paper);
  border: 1px solid var(--uw-border);
  border-radius: var(--uw-radius);
  box-shadow: var(--uw-shadow-sm);
}
.uw-dialog .q-card, .q-dialog .q-card {
  border-radius: var(--uw-radius-lg);
  border: 1px solid var(--uw-border);
  box-shadow: var(--uw-shadow-lg);
  background: var(--uw-paper);
}
.uw-dialog-wide { width: 900px; max-width: 95vw; }

/* --- review page: fill the viewport, scroll only the table ---------------- */
.uw-review-shell {
  width: 100%;
  height: calc(100dvh - 56px);
  min-height: 0;
  overflow: hidden;
}
.uw-page.uw-review-page {
  height: 100%;
  max-height: 100%;
  min-height: 0;
  overflow: hidden;
  box-sizing: border-box;
  /* Room for the always-visible sticky bulk-action bar. */
  padding-bottom: 76px;
}
.uw-review-page > .uw-info-banner { flex: 0 0 auto; }
.uw-review-card {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.uw-review-card > .uw-card-header,
.uw-review-card > .uw-search-row,
.uw-review-card > .uw-filter-row,
.uw-review-card > .uw-card-footer { flex: 0 0 auto; }
.uw-review-card > .uw-table {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.uw-review-card .q-table__middle {
  max-height: none !important;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}
.uw-review-card .q-table__bottom { flex: 0 0 auto; }

/* --- banners and pills ---------------------------------------------------- */
.uw-info-banner {
  background: var(--uw-warning-light);
  border: 1px solid color-mix(in srgb, var(--uw-warning) 30%, var(--uw-border));
  border-radius: var(--uw-radius);
  padding: 12px 16px;
  display: flex;
  align-items: center;
}
.uw-banner-title { font-weight: 700; font-size: 13px; color: var(--uw-ink); }
.uw-banner-text { font-size: 13px; color: var(--uw-ink-secondary); }
.uw-banner-value { color: var(--uw-ink); }
.op-pill { border-radius: var(--uw-radius-sm); font-size: 11px; font-weight: 600; padding: 2px 8px; }
.status-pill { border-radius: 999px; font-size: 11px; font-weight: 600; padding: 3px 9px; line-height: 1.3; }
.status-pending { background: var(--uw-warning-light); color: #92400e; }
.status-applied { background: var(--uw-success-light); color: #166534; }
.status-discarded, .status-neutral { background: var(--uw-vellum); color: var(--uw-ink-tertiary); }
.status-negative { background: var(--uw-emphasis-tint); color: var(--uw-emphasis); }
.status-progress { width: 120px; }
.lang-pill { border-radius: 4px; font-size: 10px; padding: 1px 6px; }
.sync-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  display: inline-block;
  background: var(--uw-ink-muted);
  flex: 0 0 auto;
}
.sync-dot-pulse { animation: syncpulse 1.1s ease-in-out infinite; }
@keyframes syncpulse { 0%, 100% { opacity: 0.35; } 50% { opacity: 1; } }
.sync-label { font-size: 12px; color: var(--uw-ink-tertiary); }
.sync-button { font-size: 12px; font-weight: 600; padding: 4px 14px; }
.sync-button .q-icon { font-size: 18px; }

/* --- tables --------------------------------------------------------------- */
.uw-table { background: var(--uw-paper); }
.uw-table .q-table__middle { max-height: 70vh; overflow-y: auto; overflow-x: hidden; }
.uw-table .q-table { width: 100%; background: var(--uw-paper); }
.uw-table thead tr th {
  position: sticky;
  top: 0;
  z-index: 3;
  background: var(--uw-warm);
  text-transform: uppercase;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: var(--uw-ink-tertiary);
  padding: 10px 16px;
  border-bottom: 1px solid var(--uw-border);
}
.uw-table tbody td {
  padding: 10px 16px;
  border-bottom: 1px solid color-mix(in srgb, var(--uw-border) 60%, transparent);
  vertical-align: middle;
  overflow: hidden;
  font-size: 13px;
  color: var(--uw-ink-secondary);
}
.uw-table tbody tr:hover { background: var(--uw-warm); }
.uw-table .q-table__sort-icon { font-size: 14px; opacity: 0.7; }
.uw-table .q-checkbox { margin: 0; }
.uw-table-select thead tr th:first-child,
.uw-table-select tbody td:first-child {
  width: 52px;
  min-width: 52px;
  max-width: 52px;
  padding-left: 16px;
  padding-right: 4px;
}
.uw-table .q-table__bottom { border-top: 1px solid var(--uw-border-soft); color: var(--uw-ink-tertiary); }
.uw-table .q-table__control { color: var(--uw-ink-tertiary); }
.uw-table .q-table__separator { background: var(--uw-border-soft); }
.uw-table .q-table__middle::-webkit-scrollbar, .uw-scroll::-webkit-scrollbar { width: 10px; height: 10px; }
.uw-table .q-table__middle::-webkit-scrollbar-thumb, .uw-scroll::-webkit-scrollbar-thumb {
  background: var(--uw-border-soft);
  border-radius: 999px;
  border: 2px solid var(--uw-paper);
}
.uw-table .q-table__middle::-webkit-scrollbar-track, .uw-scroll::-webkit-scrollbar-track { background: transparent; }

.entity-link { color: var(--uw-carbon); font-weight: 500; text-decoration: none; cursor: pointer; }
.entity-link:hover { text-decoration: underline; }
.truncate { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wide-tooltip { max-width: 480px; white-space: pre-wrap; }

/* --- task cards and the tasks table --------------------------------------- */
.uw-task-card { border-left: 3px solid var(--uw-ink); }
.uw-task-card .uw-card-body { padding: 16px 18px; }
.uw-step-title { font-size: 15px; font-weight: 700; color: var(--uw-ink); }
.uw-step-description { font-size: 13px; color: var(--uw-ink-tertiary); }

/* --- auth / landing ------------------------------------------------------- */
.uw-auth-page {
  width: 100%;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  background:
    radial-gradient(circle at 15% 0%, color-mix(in srgb, var(--uw-carbon) 8%, transparent), transparent 35%),
    radial-gradient(circle at 100% 100%, color-mix(in srgb, var(--uw-emphasis) 6%, transparent), transparent 40%),
    var(--uw-parchment);
}
.uw-auth-card {
  width: 100%;
  max-width: 420px;
  background: var(--uw-paper);
  border: 1px solid var(--uw-border);
  border-radius: var(--uw-radius-lg);
  box-shadow: var(--uw-shadow-lg);
  padding: 28px;
}
.uw-auth-brand { display: flex; align-items: center; gap: 10px; margin-bottom: 18px; }
.uw-auth-title { font-size: 24px; line-height: 1.2; font-weight: 700; color: var(--uw-ink); letter-spacing: -0.02em; }
.uw-auth-subtitle { font-size: 13px; color: var(--uw-ink-tertiary); margin-top: 6px; line-height: 1.5; }
.uw-instance-box {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--uw-warm);
  border: 1px solid var(--uw-border-soft);
  border-radius: var(--uw-radius);
  padding: 10px 12px;
  margin-top: 16px;
}
.uw-instance-box .q-icon { color: var(--uw-ink-tertiary); }
.uw-instance-url { font-size: 13px; font-weight: 600; color: var(--uw-ink); overflow-wrap: anywhere; }
.uw-instance-user { font-size: 11px; color: var(--uw-ink-tertiary); }
.uw-auth-error { color: var(--uw-emphasis); font-size: 13px; min-height: 20px; }

/* --- wizard and guided form ---------------------------------------------- */
.uw-wizard {
  background: transparent !important;
  box-shadow: none !important;
  border: 0 !important;
  border-radius: 0 !important;
}
.uw-wizard .q-stepper__header { display: none !important; }
.uw-wizard .q-stepper__step-inner { padding: 16px 0 0; }
.uw-wizard .q-stepper__content { padding: 0; }
.uw-step-label { font-size: 12px; color: var(--uw-ink-tertiary); }
.uw-step-progress { border-radius: 999px; }
.wizard-options { gap: 10px; }
.wizard-options .q-radio {
  width: 100%;
  margin: 0;
  padding: 14px 16px;
  align-items: flex-start;
  border: 1px solid var(--uw-border);
  border-radius: var(--uw-radius);
  background: var(--uw-paper);
  transition: border-color 0.15s, background 0.15s, box-shadow 0.15s;
}
.wizard-options .q-radio:hover { border-color: var(--uw-border-soft); background: var(--uw-warm); }
.wizard-options .q-radio[aria-checked="true"] {
  border-color: var(--uw-ink);
  background: var(--uw-warm);
  box-shadow: 0 0 0 1px var(--uw-ink);
}
.wizard-options .q-radio__inner { margin-top: 1px; }
.wizard-options .q-radio__label { line-height: 1.5; }
.wizard-goal-options .q-radio { min-height: 70px; }
.wizard-choice {
  border: 1px solid var(--uw-border);
  border-radius: var(--uw-radius);
  background: var(--uw-paper);
  padding: 10px 12px;
  margin: 0;
}
.wizard-choice:hover { background: var(--uw-warm); }
.uw-question-title { font-size: 18px; font-weight: 700; color: var(--uw-ink); }
.uw-question-counter { font-size: 12px; color: var(--uw-ink-tertiary); }

/* --- review-specific structure (shared with the review page) ------------- */
.filter-box {
  border: 1px solid var(--uw-border);
  border-radius: var(--uw-radius);
  background: var(--uw-paper);
  padding: 6px 10px 5px;
  min-height: 52px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  position: relative;
}
.filter-placeholder {
  position: absolute;
  left: 10px;
  top: 29px;
  font-size: 13px;
  color: var(--uw-ink);
  pointer-events: none;
}
.filter-caption {
  font-size: 10px;
  line-height: 1.2;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--uw-ink-tertiary);
}
.filter-select .q-field__control { min-height: 26px; height: auto; padding: 0; }
.filter-select .q-field__native,
.filter-select .q-field__input {
  padding: 0;
  min-height: 24px;
  color: var(--uw-ink);
  font-size: 13px;
  flex-wrap: wrap;
  align-content: center;
  row-gap: 2px;
  max-height: 72px;
  overflow-y: auto;
}
.filter-select .q-field__append { height: auto; }
.filter-select .q-field__bottom { display: none; }
.filter-select .q-chip { font-size: 11px; max-width: 100%; }
.filter-select .q-chip .q-chip__content { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.filter-select .q-field__control:before,
.filter-select .q-field__control:after { display: none; }
.reset-link { color: var(--uw-ink-tertiary) !important; font-size: 13px; }
.rec-cell { color: var(--uw-ink-secondary); font-size: 13px; }
.rec-pair { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.rec-line { display: flex; align-items: baseline; gap: 6px; min-width: 0; }
.rec-tag {
  flex: 0 0 auto;
  width: 74px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--uw-ink-tertiary);
}
.rec-value { font-size: 13px; color: var(--uw-ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rec-current .rec-line { align-items: flex-start; }
.rec-current .rec-value {
  color: var(--uw-ink-tertiary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  text-overflow: clip;
  max-height: 12em;
  overflow-y: auto;
  overflow-x: hidden;
}
.rec-values { display: flex; flex-wrap: wrap; gap: 4px; min-width: 0; }
.rec-values .q-chip { height: auto; min-height: 20px; padding: 1px 8px; font-size: 11px; }
.rec-extra { font-size: 12px; color: var(--uw-ink-secondary); }
.subs-context {
  font-size: 12px;
  line-height: 1.45;
  color: var(--uw-ink-secondary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  border-left: 3px solid var(--uw-border-soft);
  padding-left: 8px;
  max-height: 15em;
  overflow-y: auto;
  user-select: text;
}
.context-row td { background: var(--uw-warm); padding-top: 4px; padding-bottom: 8px; }
.actions-cell { gap: 6px; }
.apply-btn { border-radius: var(--uw-radius-sm); font-weight: 600; font-size: 12px; padding: 3px 12px; }
.row-actions { visibility: hidden; }
tr:hover .row-actions { visibility: visible; }
.child-row { background: var(--uw-warm); }
.child-row td {
  background: var(--uw-warm);
  border-bottom: 1px solid var(--uw-border);
  padding-top: 8px;
  padding-bottom: 8px;
}
.child-row td:first-child { border-left: 2px solid var(--uw-ink); }
.child-target { padding-left: 32px; font-size: 13px; color: var(--uw-ink-secondary); }
.uw-page-label { font-size: 12px; color: var(--uw-ink-tertiary); }
.uw-card-footer .page-btn {
  min-width: 30px;
  border-radius: var(--uw-radius-sm);
  color: var(--uw-ink-tertiary) !important;
  font-size: 13px;
}
.uw-card-footer .page-btn-active,
.uw-card-footer .page-btn-active * { background: var(--uw-ink) !important; color: var(--uw-parchment) !important; }
.page-input { width: 64px; }
.page-input .q-field__control { height: 30px; min-height: 30px; }
.uw-bulk-bar {
  background: var(--uw-paper);
  border-top: 1px solid var(--uw-border);
  padding: 10px 24px;
  box-shadow: 0 -2px 8px rgb(0 0 0 / 0.04);
}
.uw-selection-label { font-size: 13px; font-weight: 700; color: var(--uw-ink); }
.uw-run-button { background: var(--uw-ink) !important; color: var(--uw-parchment) !important; }
.uw-select-all { color: var(--uw-carbon) !important; font-size: 13px; }
.uw-discard-button {
  color: var(--uw-emphasis) !important;
  border-color: color-mix(in srgb, var(--uw-emphasis) 40%, transparent) !important;
}
.uw-bulk-sticky { z-index: 1000; }
.breadcrumb-link { color: var(--uw-carbon); font-size: 13px; text-decoration: none; }
.breadcrumb-chevron { color: var(--uw-ink-muted); font-size: 16px; }
.breadcrumb-current { color: var(--uw-ink); font-size: 13px; font-weight: 600; }
.settings-btn { color: var(--uw-ink) !important; }

/* --- modal chrome --------------------------------------------------------- */
.uw-modal-header {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: nowrap;
  flex-shrink: 0;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--uw-border-soft);
}
.uw-modal-title { font-size: 17px; font-weight: 700; color: var(--uw-ink); }
.uw-modal-close { color: var(--uw-ink-tertiary) !important; }
.uw-dialog-body { min-height: 0; }
.uw-dialog-footer {
  display: flex;
  width: 100%;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 10px;
  border-top: 1px solid var(--uw-border-soft);
}

/* --- chat ----------------------------------------------------------------- */
.q-dialog__backdrop {
  background: color-mix(in srgb, #000000 55%, transparent) !important;
}
.q-scrollarea__thumb {
  background: var(--uw-border-soft) !important;
  opacity: 0.85;
}
.q-message-name { font-size: 12px; color: var(--uw-ink-tertiary); }
.q-message-text {
  border-radius: var(--uw-radius) !important;
  font-size: 13px;
  line-height: 1.5;
  box-shadow: none !important;
}
.q-message-text--sent { color: var(--uw-warm) !important; }
.q-message-text--received { color: var(--uw-warm) !important; }
.prompt-bubble .q-message-text--received {
  color: var(--uw-carbon-tint) !important;
  border: 1px solid color-mix(in srgb, var(--uw-carbon) 22%, var(--uw-border)) !important;
}
.uw-chat-panel {
  border-top: 1px solid var(--uw-border-soft);
  background: var(--uw-warm);
  border-radius: var(--uw-radius);
  padding: 10px;
}

/* --- misc ----------------------------------------------------------------- */
.uw-empty { font-size: 13px; font-style: italic; color: var(--uw-ink-secondary); }
.uw-mono { font-family: var(--uw-font-mono); }
.uw-code, code, pre { font-family: var(--uw-font-mono); }
.q-notification { border-radius: var(--uw-radius) !important; font-family: var(--uw-font-sans); }
.q-tooltip { background: var(--uw-ink) !important; color: var(--uw-parchment) !important; font-size: 12px; }
"""

FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">'
)


def install_theme() -> None:
    """Apply the Uwazi palette and the shared stylesheet to the current page."""
    ui.colors(
        primary="#1a1a1a",
        secondary="#00b4f0",
        accent="#e8432a",
        positive="#059669",
        negative="#e8432a",
        warning="#f59e0b",
        info="#00b4f0",
    )
    ui.add_head_html(FONT_LINKS)
    ui.add_css(THEME_CSS)


__all__ = ["FONT_LINKS", "THEME_CSS", "install_theme"]
