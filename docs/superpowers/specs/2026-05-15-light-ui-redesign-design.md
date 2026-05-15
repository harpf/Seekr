---
name: light-ui-redesign
description: Full light-mode UI redesign for all Seekr pages, wiki 500 fix, Swagger iframe, and graceful unauthorized state handling
metadata:
  type: project
---

# Seekr UI Redesign — Light/Minimal Theme

**Date:** 2026-05-15

## Overview

Full redesign of the Seekr web UI from the existing dark navy theme to a clean light/minimal aesthetic (Linear/Vercel style). Covers all five pages. Includes three bug/UX fixes bundled into the same pass.

## Bugs and UX Issues Fixed

1. **Wiki 500 error** — `wiki.html` contains Jinja2 template syntax (`{% if %}`, `{% for %}`, `{{ }}`) inside a Home Assistant YAML code example. Jinja2 tries to evaluate them and throws 500. Fix: wrap the block in `{% raw %}...{% endraw %}`.
2. **Unauthorized JSON leaking to UI** — Config tab API calls that return 401 before login currently let `{"detail":"unauthorized"}` reach the DOM. Fix: global `api()` handler catches 401 and shows a styled amber banner + toast instead.
3. **Default unauthorized state on config tabs** — Tab content areas show nothing useful when the user is not signed in. Fix: amber info card _"Sign in to access this section"_ shown in place of tab content until authenticated.

## Files Changed

| File | Change |
|---|---|
| `document_search/web/static/styles.css` | Full rewrite — light-first design system |
| `document_search/web/templates/index.html` | Light theme, stats grid, recent searches |
| `document_search/web/templates/search.html` | Light theme, filter panel, result cards |
| `document_search/web/templates/ingest.html` | Light theme, progress bar, dropzone |
| `document_search/web/templates/config.html` | Light theme, pill tab bar, unauthorized banner |
| `document_search/web/templates/wiki.html` | Jinja2 fix, Swagger iframe card, light theme |
| `document_search/web/static/app.js` | Graceful 401 handling in global `api()` |

No backend changes.

## Design System (Tokens)

```css
/* Backgrounds */
--bg:       #f8f9fc;   /* page */
--surface:  #ffffff;   /* cards, panels */
--overlay:  #f1f4f8;   /* inputs, code blocks, hover */

/* Borders */
--b-lo:     #e2e8f0;   /* card borders, dividers */
--b-md:     #cbd5e1;   /* input borders, separators */

/* Text */
--txt-1:    #0f172a;   /* headings */
--txt-2:    #475569;   /* body, labels */
--txt-3:    #94a3b8;   /* hints, placeholders */

/* Accent */
--blue:     #2563eb;
--blue-a:   #eff6ff;   /* soft highlight background */
--blue-dk:  #1d4ed8;   /* hover */

/* Status */
--green:    #16a34a;
--amber:    #d97706;
--red:      #dc2626;

/* Effects */
--sh:       0 1px 4px rgba(0,0,0,.06);
--r1: 4px; --r2: 8px; --r3: 10px; --r4: 12px;
```

**Typography:** Inter (existing). `h1` → 1.4rem / weight 700. `h2` → 1.05rem / weight 600. Body 0.9rem / `#475569`. Hints 0.8rem / `#94a3b8`.

**Spacing:** Card padding `1.5rem`. Card-to-card gap `1rem`. Section groups separated by styled `<hr>` (`1px solid #e2e8f0`, `1.5rem` vertical margin). Tab panels `1.25rem` top padding.

## Component Patterns

### Topbar / Navigation
- White background, `border-bottom: 1px solid #e2e8f0`, `box-shadow: 0 1px 3px rgba(0,0,0,.06)`.
- Nav links: `#475569` default, `#2563eb` active (blue chip background `#eff6ff` + blue text).
- Brand logo: `#2563eb` icon.

### Cards
- `background: #ffffff`, `border: 1px solid #e2e8f0`, `border-radius: 12px`, `box-shadow: var(--sh)`.
- Card header row: `padding: 1rem 1.5rem`, `border-bottom: 1px solid #f1f4f8`.
- Icon circle: `background: #eff6ff`, `color: #2563eb`.

### Tab Bar (config page)
- Pill-style: tabs sit on a `#f1f4f8` rounded track.
- Active tab: `background: #ffffff`, `color: #2563eb`, subtle `box-shadow`.
- Inactive: `color: #475569`, no background.

### Buttons
- Primary: `background: #2563eb`, white text, hover `#1d4ed8`.
- Secondary: white bg, `border: 1px solid #e2e8f0`, `color: #475569`.
- Danger: `background: #dc2626`, white text.

### Form Fields
- White bg, `border: 1px solid #cbd5e1`, `border-radius: 6px`.
- Focus ring: `outline: 2px solid #2563eb`, `outline-offset: 1px`.
- Hint text: `#94a3b8`, `font-size: 0.8rem`.

### Badges / Status Chips
- Rounded pill. Colour-coded with light tinted backgrounds:
  - OK: `#dcfce7` bg / `#16a34a` text.
  - Warn: `#fef9c3` bg / `#d97706` text.
  - Error: `#fee2e2` bg / `#dc2626` text.
  - Neutral: `#f1f4f8` bg / `#475569` text.

### Section Dividers
- `<hr>` styled as `border: none; border-top: 1px solid #e2e8f0; margin: 1.5rem 0`.

### Unauthorized Banner
- Amber card shown in tab content area when not authenticated.
- Lock icon + text: _"Sign in to access this section."_
- Replaces raw JSON error reaching the DOM.

### Swagger Iframe (wiki page)
- Card with heading "Interactive API Reference" and short description.
- `<iframe src="/docs" style="width:100%;height:700px;border:none;border-radius:8px;">`.
- "Open in new tab" link below the iframe.
- Positioned above the Quick Start section.

## Page-Specific Details

### Dashboard (`/`)
- Stats in a 3-column grid: documents, content blocks, total size.
- Clean number + icon + label layout.
- Recent searches as horizontal chip row.

### Search (`/search`)
- Filter panel: collapsible card (white, bordered).
- Result cards: filename bold + path muted below + snippet in `#475569` + extension badge.
- Bookmark icon right-aligned per result.
- Tag chips: `#eff6ff` bg / `#2563eb` text.

### Ingest (`/ingest`)
- Job progress bar: styled track + fill with percentage text.
- Upload dropzone: dashed-border card, centred icon + text.
- AI Reorganize results in a clean table card.

### Config (`/config`)
- Pill tab bar replaces flat tabs.
- Unauthorized state: amber "Sign in" banner per tab panel content area.
- Login card: centred white card, existing structure.

### Wiki (`/wiki`)
- `{% raw %}...{% endraw %}` wraps HA automation YAML code block (fixes 500).
- Swagger iframe card inserted above Quick Start.
- TOC sidebar: light bg `#f8f9fc`, blue active links.
- Code blocks: `#f1f4f8` bg, `#0f172a` text, monospace.

## Error Handling

- Global `api()` in `app.js` catches 401 → shows amber toast "Session expired — please sign in again."
- Config tab panels check auth state before rendering content; show unauthorized banner if not signed in.
- Wiki 500 is a pure template fix (no runtime error handling needed).

## What Is Not Changing

- Backend routes, API contracts, authentication logic.
- HTML structure and element IDs (JavaScript selectors stay valid).
- Functionality of any feature.
- The `ingest.html` and `search.html` JS interaction patterns.
