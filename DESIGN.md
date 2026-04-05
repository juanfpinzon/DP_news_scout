# DESIGN.md — Digital Procurement News Scout Email

Design system reference for the HTML email digest (`templates/digest_email.html`).

---

## Theme: Ocean Depths Enhanced

Based on the **Ocean Depths** theme — a professional maritime palette that reinforces trust and authority for a senior executive audience. Extended with editorial magazine typographic conventions and email-safe layout patterns.

---

## Color Palette

| Role | Hex | Usage |
|------|-----|-------|
| Deep Navy | `#1a2332` | Header bg, footer bg, section label text, numbered badges |
| Teal | `#0891b2` | Links, Top Story badge, Top Story left accent, callout borders |
| Teal Green | `#2d8b8b` | On Our Radar left accent bars, Quick Hits bullet icons, footer feedback link |
| Seafoam | `#a8dadc` | Key Developments badge text, Key Dev "Why it matters" left bar |
| Cream | `#f1faee` | Quick Hits container background |
| Teal Tint | `#f0f9fb` | Top Story card background |
| Light Teal Tint | `#ddf0f5` | Top Story "Why it matters" callout background |
| Off-White | `#ffffff` | Main container, Key Developments card backgrounds |
| Outer Background | `#cdd4db` | Page/email wrapper background |
| Body Text | `#374151` | Article summary paragraphs |
| Dark Text | `#4b5563` | Secondary body (Key Dev summaries, On Our Radar) |
| Meta Text | `#5a6b7a` | Source name labels |
| Muted Meta | `#8a9ab0` | Date text, secondary meta |
| Card Border | `#dde5ea` | Key Developments card borders |
| Divider | `#b8c2cc` | Footer top rule, container border |
| Issue Number | `#253b56` | Decorative large issue number in header (low contrast watermark) |

---

## Typography

All fonts are web-safe with no external dependencies — renders consistently in Gmail, Outlook, Apple Mail, and mobile clients.

| Element | Font | Size | Weight | Notes |
|---------|------|------|--------|-------|
| Section eyebrow | Helvetica Neue / Arial | 10px | Bold | `letter-spacing: 3px`, `text-transform: uppercase` |
| Masthead title "News Scout" | **Georgia** / Times New Roman | 28px | Bold | Serif — editorial gravitas |
| Header meta (date, sources) | Helvetica Neue / Arial | 11px | Normal | Color `#5a7d9a` |
| Top Story headline | **Georgia** / Times New Roman | 21px | Bold | Serif — feature treatment |
| Key Dev headline | **Georgia** / Times New Roman | 16px | Bold | Serif |
| On Our Radar headline | **Georgia** / Times New Roman | 15px | Bold | Serif |
| Body / summary | Helvetica Neue / Arial | 13–14px | Normal | `line-height: 1.6–1.65` |
| "Why it matters" label | Helvetica Neue / Arial | 10px | Bold | `letter-spacing: 1.5px`, uppercase, teal |
| "Why it matters" text | Helvetica Neue / Arial | 12–13px | Normal | Teal-dark color |
| Source / meta labels | Helvetica Neue / Arial | 10px | Bold | Uppercase, `letter-spacing: 0.5–0.8px` |
| Section labels | Helvetica Neue / Arial | 10px | Bold | `letter-spacing: 2.5px`, uppercase, navy |
| CTA links ("Read →") | Helvetica Neue / Arial | 11px | Bold | Teal `#0891b2` |
| Quick Hits text | Helvetica Neue / Arial | 13px | Bold | Headlines bold, source muted |
| Footer text | Helvetica Neue / Arial | 10–12px | Mixed | Centered, low-contrast navy tones |

**Why Georgia for headlines?** Georgia is universally installed (Windows, macOS, iOS, Android) and renders with editorial weight and warmth — a significant step up from Arial Bold which reads as generic corporate. It also degrades well: if somehow unavailable, Times New Roman is the next closest serif.

---

## Layout Structure

Max container width: **880px** (configured in `config/settings.yaml` → `email_max_width_px`).  
Outer background provides a 32px visual margin on all sides.

```
┌─────────────────────────────────────────────────────┐
│  ████ 4px teal accent stripe                        │
│  HEADER: Navy bg                                    │
│    PepsiCo Digital Procurement (eyebrow)            │
│    News Scout (Georgia 28px)           #00  ←issue  │
│    April 5, 2026 · 23 sources         Issue         │
├─────────────────────────────────────────────────────┤
│  [★ TOP STORY]  ← teal pill badge                  │
│  ┌──────────────────────────────────────────────┐   │
│  │▌ (5px teal bar)  Card bg #f0f9fb            │   │
│  │  Headline (Georgia 21px)                     │   │
│  │  Summary paragraph                           │   │
│  │  ▌ Why it matters callout (teal tint)        │   │
│  │  SOURCE · Date   Read more →                 │   │
│  └──────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────┤
│  KEY DEVELOPMENTS ══════════════════════════════    │
│  ┌────────────────────────────────────────────┐     │
│  │ [01]  Headline (Georgia 16px)              │     │
│  │       Summary                              │     │
│  │       ▌ Why it matters (seafoam bar)       │     │
│  │       SOURCE · Date   Read →               │     │
│  └────────────────────────────────────────────┘     │
│  ┌────────────────────────────────────────────┐     │
│  │ [02]  ...                                  │     │
│  └────────────────────────────────────────────┘     │
├─────────────────────────────────────────────────────┤
│  ON OUR RADAR ══════════════════════════════════    │
│  ▌ Headline (Georgia 15px)                          │
│    Summary · SOURCE · Date   Read →                 │
│  ─────────────────────────────────────────────      │
│  ▌ Headline                                         │
│    ...                                              │
├─────────────────────────────────────────────────────┤
│  QUICK HITS ════════════════════════════════════    │
│  ┌──────────────────────────────────────────────┐   │
│  │ ▶ One-liner headline — SOURCE               │   │
│  │ ─────────────────────────────────────────── │   │
│  │ ▶ One-liner headline — SOURCE               │   │
│  └──────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────┤
│  ── hairline rule ────────────────────────────────  │
│  ████ FOOTER: Navy bg                              │
│    DIGITAL PROCUREMENT NEWS SCOUT · PEPSICO         │
│    Questions? Share feedback                        │
│    April 5, 2026 · Issue #0                         │
└─────────────────────────────────────────────────────┘
```

---

## Email Compatibility

### Left-border accent bars
Email clients (especially Outlook) don't reliably render `border-left` on `<table>` elements. All colored left accent bars are implemented using a **narrow spacer `<td>` column**:

```html
<table role="presentation" width="100%">
  <tr>
    <td width="5" bgcolor="#0891b2" style="background-color:#0891b2;font-size:1px;">&nbsp;</td>
    <td style="padding:24px 28px;background-color:#f0f9fb;">
      <!-- content -->
    </td>
  </tr>
</table>
```

This renders correctly in all major email clients including Outlook 2016+.

### Numbered badges (Key Developments)
Implemented as a fixed-width `<td>` with `bgcolor` + `background-color` on the same element, and `line-height` equal to the cell height for vertical centering — no CSS Flexbox or Grid:

```html
<td width="30" height="30" align="center" valign="middle"
    bgcolor="#1a2332" style="background-color:#1a2332;line-height:30px;">
  01
</td>
```

### Number formatting
Key Developments badges are zero-padded via Jinja2's Python string formatting: `{{ '%02d' % loop.index }}` → `01`, `02`, `10`, etc. The header issue number is rendered separately as `#{{ issue_number }}`.

### Fonts
No Google Fonts or external font imports. Georgia and Helvetica Neue are universally available. The full fallback chain is:
- Headlines: `Georgia, 'Times New Roman', serif`
- Body / UI: `'Helvetica Neue', Helvetica, Arial, sans-serif`

### CSS inlining
`premailer` (called in `src/renderer/html_email.py`) inlines all `<style>` block declarations into element `style=""` attributes before send. `@media` queries are preserved as-is (they can't be inlined) for mobile responsiveness in clients that support them (Gmail, Apple Mail, iOS Mail).

---

## Section Visual Hierarchy

Each section is differentiated by visual weight and background treatment to create a clear reading flow for time-pressed executives:

| Section | Visual Weight | Background | Left Accent | Headline Size |
|---------|--------------|------------|-------------|---------------|
| Top Story | **Highest** | Cream-teal tint | 5px teal | Georgia 21px |
| Key Developments | High | White with border | 3px seafoam (callout only) | Georgia 16px |
| On Our Radar | Medium | White (inherited) | 3px teal-green per item | Georgia 15px |
| Quick Hits | Low | Cream container | None | Arial 13px bold |
| Footer | — | Deep Navy | — | Arial 10–12px |

---

## Jinja2 Template Variables

| Variable | Type | Source |
|----------|------|--------|
| `date` | `str` | Formatted in `html_email.py` |
| `issue_number` | `int` | Pipeline-provided issue number (currently fixed to `0` by default config via `issue_number_override`) |
| `source_count_label` | `str` | `format_source_count_label()` in `renderer/common.py` |
| `feedback_href` | `str` | `mailto:` or URL from env (`FEEDBACK_URL`, `FEEDBACK_EMAIL`) |
| `max_width_px` | `int` | `settings.email_max_width_px` (default 880) |
| `top_story` | `DigestItem` | `Digest.top_story` |
| `key_developments` | `list[DigestItem]` | `Digest.key_developments` |
| `on_our_radar` | `list[DigestItem]` | `Digest.on_our_radar` |
| `quick_hits` | `list[QuickHit]` | `Digest.quick_hits` |

`DigestItem` fields: `url`, `headline`, `summary`, `why_it_matters`, `source`, `date`  
`QuickHit` fields: `url`, `one_liner`, `source`

---

## Previewing Changes

```bash
# Render with mock data → opens /tmp/dpns_preview.html
python scripts/test_email.py --preview-path /tmp/dpns_preview.html --issue-number 7
open /tmp/dpns_preview.html
```
