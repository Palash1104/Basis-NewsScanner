# Design notes: "Basis" mockup vs SPEC.md

Compares the Claude Design export in `design/` with SPEC.md §6 (data model), §10 (Telegram
digest) and §11 (web UI). Nothing here changes the spec or the code; it records where the two
agree and where they don't, ahead of Phase 6.

**Sources read**

- `Basis - Commodity News App.dc.html` — the mockup: markup, sample data and view logic
- `_ds/modernist-…/styles.css` — design tokens and component classes
- `_ds/modernist-…/readme.md` — how the design system is meant to be used
- `_ds/modernist-…/_ds_manifest.json`, `_adherence.oxlintrc.json`, `_ds_bundle.js` — token list, lint rules, an empty component bundle
- `browser-window.jsx` — the fake Chrome window around the mockup (presentation only, not part of the app)
- `support.js` — the Claude Design runtime that renders `.dc.html` (not part of the app)
- `README.md` — handoff instructions from Claude Design

**Not available**

- `design/DESIGN-CHAT.md` does not exist, so no design-chat decisions are recorded here.
- `.thumbnail` is a blank white WebP.
- The design-system readme lists `foundations/`, `components/`, `templates/`, `theme.json` and `thumbnail.html`; none were in the export. `styles.css` alone is enough for the tokens.

**How to read the mappings**

- **stored**: a SPEC §6 column holds this value.
- **derived**: computable from stored data, but no column holds it.
- **config**: comes from `settings.yaml` (§5) or `assets.yaml` (§8), not a table.
- **GAP**: nothing in the spec provides it.

The mockup's content is sample data (the page footer says so). It is written for "ordinary
investors" following commodities, while the spec is a personal research tool covering
commodities, FX, rates, indices and stocks.

---

## 1. Screens and their SPEC §11 pages

| Design screen | Mockup URL | SPEC §11 page | Match |
|---|---|---|---|
| Header and "Moving now" ticker, shared by every screen | — | none (layout) | — |
| **Today** feed | `basis.news/today` | `/` | Partial |
| **Story detail** | `basis.news/story/{id}` | `/story/{id}` | Partial |
| **Watchlist** | `basis.news/watchlist` | none; closest is `/asset/{symbol}` | No direct match |

**States and variants in the mockup**

- Two boolean props on the Today feed: `showPlainEnglish` toggles the plain-English paragraph on each story, and `showRail` toggles the right rail. Both default to on.
- Clicking a story row (on Today or Watchlist) opens Story detail. "Today" and "Watchlist" in the header switch screens, and "← Today" returns.
- There are no loading, empty or error states, and no dark mode.

**Nav items with no screen:** "Commodities" and "Learn" are plain labels, not links. The
second browser tab, "Nickel · LME", hints at a single-commodity page that isn't designed.

**SPEC pages with no screen:** `/track-record`, `/asset/{symbol}` and `/runs`.

---

## 2. Data on each screen, mapped to SPEC §6

### 2.1 Shared header and ticker

| Design element | Mockup example | SPEC source | Status |
|---|---|---|---|
| Brand | "BASIS" | — | GAP (the spec calls the app Newsdesk) |
| Nav: Today | — | `/` | page exists |
| Nav: Watchlist | — | — | GAP |
| Nav: Commodities, Learn | — | — | GAP |
| Search box | "Search a story or a commodity" | — | GAP |
| "Get the brief" button | — | — | GAP (§10 sends the digest to a Telegram chat set in `.env`; there's no sign-up) |
| Avatar | "RK" | — | GAP (§0 and §11: single user, no auth) |
| Ticker: code | NICKEL, EUA, BDI | `assets.yaml` `symbol` / `display_name` | config; most mockup assets aren't in the universe (GAP 9) |
| Ticker: % change | +4.2% | `price_cache.close` over time | derived; the spec has no "moving now" feature or window |

### 2.2 Today feed → `/`

| Design element | Mockup example | SPEC source | Status |
|---|---|---|---|
| Page title | "What moved, and why" | — | static copy |
| Date | "Wednesday 16 September 2026" | current date in `settings.timezone` | config |
| Story count | "six stories" | number of stories shown | derived |
| Read time | "read time 9 min" | — | GAP (could be estimated from `stories.summary` length) |
| 1D / 1W / 1M control | — | — | GAP |
| Story: sector | "Metals" | `stories.category` | stored, but a different taxonomy (GAP 11) |
| Story: time | "06:20 BST" | `stories.first_seen_at` | stored; the spec displays Asia/Kolkata, not BST |
| Story: source | "Jakarta" (a city) | — | GAP; the spec has outlets (`articles.source_name`), not datelines |
| Story: headline | — | `stories.headline` | stored |
| Story: plain-English paragraph | "You probably do not trade nickel…" | `stories.summary` | closest; different content (what it means for you vs. what happened and why it matters) |
| Affected commodity: name | "Nickel" | `impacts.symbol` → `assets.yaml` `display_name` | stored + config |
| Affected commodity: unit | "LME 3M · $/t" | — | GAP |
| Affected commodity: sparkline | 16-point line | `price_cache` (`symbol`, `interval`, `ts`, `close`) | derived |
| Affected commodity: % change | "+4.2%" | `impacts.move_at_detection_pct` | stored; the mockup's time window is unstated |
| Affected commodity: colour | ink = price up, red = down | sign of the move | derived; unrelated to `impacts.direction` |
| "Earlier stories" button | — | older `stories` by `first_seen_at` | derived (pagination) |
| "Updated 07:40" | — | latest `runs.finished_at` where `kind = pipeline` | stored |
| "next brief 17:00" | — | `delivery.digest_times` | config; spec defaults are 07:30 and 19:30 |
| Rail: "Your watchlist" (name, price, sparkline, change) | — | values from `price_cache` + `display_name` | the list itself is a GAP |
| Rail: "Edit watchlist" | — | — | GAP |
| Rail: "In plain english" explainer and "How to read a sparkline" link | — | — | GAP (static learning content) |
| Rail: "Biggest movers · 24h" | "Lithium +5.6%" | `price_cache` across the universe | derived; not a spec feature |

### 2.3 Story detail → `/story/{id}`

| Design element | Mockup example | SPEC source | Status |
|---|---|---|---|
| Sector tag | "Metals" | `stories.category` | stored, different taxonomy |
| Source · time | "Jakarta · 06:20 BST" | — · `stories.first_seen_at` | dateline GAP; time stored |
| Headline | — | `stories.headline` | stored |
| Intro paragraph under the headline | "The mining ministry cut next year's ore quota…" | `stories.summary` | closest match |
| "What happened" | — | — | GAP: the spec has one 2–3 sentence summary |
| "Why it matters" | — | — | GAP (same) |
| "If you own nothing exotic" | — | — | GAP (same) |
| Related stories (sector, headline) | 3 other stories | `stories.category`, `stories.headline` | fields stored; no rule for choosing related stories (GAP) |
| "Commodities affected · 7 days" | — | — | the 7-day window is a GAP; spec horizons are 1 and 5 trading days |
| Commodity: name | "Nickel" | `impacts.symbol` → `display_name` | stored + config |
| Commodity: change | "+4.2%" | `impacts.move_at_detection_pct` | stored; the mockup labels it 7 days |
| Commodity: unit | "LME 3M · $/t" | — | GAP |
| Commodity: price | "$18,240" | latest `price_cache.close` | derived; no currency or unit to format it (GAP) |
| Commodity: sparkline | 300×64 line | `price_cache` | derived |
| Commodity: note | "Most exposed: the quota applies directly…" | `impacts.mechanism` | stored; good match |
| Order: "most exposed first" | — | §10: first-order before second-order, then by confidence | different rule |
| "Add these to watchlist" | — | — | GAP |

### 2.4 Watchlist → no spec page (closest `/asset/{symbol}`)

| Design element | Mockup example | SPEC source | Status |
|---|---|---|---|
| Subtitle: count of commodities | "Four commodities" | — | GAP (no watchlist) |
| Subtitle: stories this week | "11 stories touched them this week" | distinct `impacts.story_id` for watched symbols, `impacts.created_at` in the last 7 days | derived, once a watchlist exists |
| "Add a commodity" | — | — | GAP |
| Card: unit | "LME 3M · $/t" | — | GAP |
| Card: name | "Nickel" | `display_name` | config |
| Card: price and change | "$18,240 +4.2%" | `price_cache.close` | derived; window unstated |
| Card: sparkline | 300×72 line | `price_cache` | derived |
| Card: driver | "Indonesian export policy" | — | GAP; nearest are the latest `impacts.mechanism` or `events.channels` for that symbol |
| News row: time, headline | "06:20 BST", headline | `stories.first_seen_at`, `stories.headline` | stored |
| News row: "Moves Nickel, Cobalt, Steel HRC" | — | the story's `impacts.symbol` → `display_name` | stored + config |
| News row: lead sparkline and change | — | first impact: `price_cache` and `impacts.move_at_detection_pct` | derived + stored |
| News rows ordered newest first | — | `stories.first_seen_at` | stored |
| Exposure: note, driver/count table, "See how exposure is scored" | "Export policy 2, Mine supply 1…" | — | GAP; nearest is counting `events.channels` per watched symbol, but a different taxonomy and no scoring defined |

Design quirk: the copy says "The commodity in bold is the one you follow", but the "Moves …"
line in the mockup isn't bold.

---

## 3. GAPS: design elements with no matching data in the spec

**Features the spec doesn't have**

1. **Watchlist.** The rail list, the Watchlist screen, "Edit watchlist", "Add a commodity" and "Add these to watchlist". There's no watchlist table.
2. **User account.** The "RK" avatar. The spec is single-user with no auth (§0, §11).
3. **Search** in the header.
4. **"Get the brief" button.** There's no brief sign-up; the digest goes to a Telegram chat set in `.env`.
5. **"Commodities" and "Learn" nav**, plus the "In plain english" explainer and "How to read a sparkline" link. No such pages or content in the spec.
6. **Exposure panel.** The driver/count table, "one bet, not two" analysis and "See how exposure is scored". No exposure model; `events.channels` is the nearest data, with a different taxonomy.
7. **Per-commodity "driver" label** on watchlist cards.
8. **Related stories** on Story detail. No relatedness rule.

**Asset and price data**

9. **Assets outside the spec's universe** (§8):
   - Not in the universe at all: nickel, cobalt, lithium, aluminium, steel HRC (N. Europe), diesel (NWE), EU carbon (EUA), dry freight (Baltic index, "BDI") and LNG (JKM).
   - Different benchmark: the mockup's natural gas is TTF (€/MWh), while the spec's `NG=F` is the US NYMEX contract. Its copper is labelled LME 3M, while the spec's `HG=F` is COMEX copper.
   - Clean matches: Brent, corn, soybeans, wheat.
   - I haven't looked up tickers for the missing assets; any additions would need `validate_tickers.py`.
10. **Units, exchanges and currencies** ("LME 3M · $/t", "CBOT · ¢/bu", "€31.20"). `assets.yaml` has no unit, exchange or currency field, so prices can't be labelled or formatted.

**Editorial content**

11. **Sector taxonomy.** The mockup uses Metals, Batteries, Agriculture, Energy, Policy, Freight. `stories.category` is Politics | Geopolitics | Economy & Markets | Business | Tech | Science & Health | Other.
12. **Dateline as "source"** ("Jakarta", "Vienna"). The spec stores outlet names, not places.
13. **Multi-part story text.** The intro paragraph, "What happened", "Why it matters", "If you own nothing exotic", and the feed's plain-English line. The spec stores one 2–3 sentence `stories.summary`. The mockup's text also brings in background facts ("Indonesia supplies over half the world's mined nickel"), which the §7.5 prompt forbids: the summary may use only what the articles contain.
14. **Read time** ("read time 9 min").

**Time windows and live widgets**

15. **Time windows.** The 1D/1W/1M control, "Commodities affected · 7 days" and "Biggest movers · 24h". The spec has one move, `move_at_detection_pct` (since the pre-news reference price), and scoring horizons of 1 and 5 trading days.
16. **"Moving now" ticker and "Biggest movers".** No spec feature; could be derived from `price_cache` for universe symbols only.

**Presentation and branding**

17. **Display toggles** `showPlainEnglish` / `showRail`. No setting.
18. **Brand and positioning.** "Basis", `basis.news`, "commodity news for ordinary investors" vs. Newsdesk as personal research notes. BST times vs. the spec's Asia/Kolkata.

---

## 4. MISSING: spec requirements the design doesn't show

**On each story in the feed** (§10 digest content; §11 says `/` shows the same content)

1. **Predicted direction ▲/▼** (`impacts.direction`). The mockup colours a % by the actual price move and never shows the expected direction.
2. **Confidence** high / medium / low (`impacts.confidence`).
3. **First- vs second-order** (`impacts.order`).
4. **"Mixed signals"** when the same asset has impacts in both directions (`impacts.conflict`).
5. **"Already moved" / "moving against this call" labels** (§7.8), measured against the asset's 20-day volatility. Also the **"price unavailable"** state.
6. **Track record line**, e.g. `Rule "oil_supply_shock": right 11 of 16 times`, shown only when n ≥ `min_samples_to_show_rate` (from `impact_scores`).
7. **Source links:** up to 3 per story (`articles.source_name`, `articles.url`). The mockup shows one city and no links.
8. **Impact ordering:** first-order before second-order, then by confidence. The mockup uses "most exposed first".
9. **"Research notes, not financial advice." footer.** Absent everywhere, and the "ordinary investors" / "If you own nothing exotic" framing leans the other way.
10. **Region and category filters** on `/` (US / India / Global; the category enum). The mockup has a time-span control instead, and shows no region at all (`stories.regions`).
11. **Importance ordering.** `/` should rank stories by `stories.importance_score`; the mockup's feed is in time order.
12. **Non-commodity assets.** FX, rates, indices, stocks and ETFs (e.g. USD/INR, ^NSEI, ONGC, IndiGo in the spec's §1 example). The mockup only has commodity cards.
13. **Stories with no market impact.** Spec stories may have no impacts (`no_clear_impact`, `is_new_development = false`, or channels `[none]`). Every mockup story has three commodities.

**On Story detail** (§11 `/story/{id}`)

14. **All source articles.** `articles`: `source_name`, `title`, `snippet`, `url`, `published_at`.
15. **Event JSON.** `events`: `event_type`, `countries`, `regions`, `entities`, `companies`, `channels`, `severity`, `policy_stance`, `is_new_development`, `model`, `prompt_version`.
16. **Every impact with its scores as they come in.** `impact_scores` per horizon: `asset_return`, `benchmark_symbol`, `benchmark_return`, `excess_return`, `threshold`, `outcome` (hit / miss / no_move / unscorable). Plus the impact's own `origin` (playbook / llm / both), `rule_id`, `horizon`, `reference_time` and `reference_price`.
17. **Rule disagreements.** Where the LLM said a playbook rule didn't fit, with the reason (§7.7).
18. **"Sources disagree" note** (`stories.sources_disagree`, `disagreement_note`). §10 and §11 don't list it explicitly, but it's stored and the approved Phase 1 plan shows it in the digest.

**Whole pages**

19. **`/track-record`:** tables by rule, event type, origin, confidence, horizon and prompt version. Show a hit rate only when n ≥ min samples, and also show the no_move share (§7.9).
20. **`/asset/{symbol}`:** every impact call on one asset and its outcomes.
21. **`/runs`:** recent runs, errors and token usage (`runs`: `kind`, `started_at`, `finished_at`, `articles_fetched`, `stories_processed`, `input_tokens`, `output_tokens`, `errors`).

**Cross-cutting**

22. **Light/dark mode** (§11). `styles.css` defines a light theme only.
23. **Times in Asia/Kolkata** (§0). The mockup uses BST.

**Spec problems found while mapping** (recorded only; SPEC.md unchanged)

- §7.7 says to "store the disagreement reason" when the LLM disputes a rule, but §6 has no column or table for it. `/story/{id}` needs one to show rule disagreements.
- §11 says `/` shows "the same content as the digest", but §10's digest has no region or category, while `/` must filter by both. The page therefore needs `stories.regions` and `stories.category` beyond the digest fields.

---

## 5. Design tokens

From `styles.css` (the design system's source of truth). The mockup also hard-codes some values;
those are listed separately, with the token each one equals where there is one.

### Colors

| Token | Value | Use |
|---|---|---|
| `--color-bg` | `#f3f2f2` | page ground |
| `--color-surface` | `#eae9e9` | filled panels, inputs, sparkline backgrounds |
| `--color-text` | `#201e1d` | ink; also the mockup's "price up" colour |
| `--color-accent` | `#ec3013` | primary button, focus ring, selected segment |
| `--color-accent-2` | `#e15b47` | machine-made stand-in; the system is mono, so treat it as the accent |
| `--color-divider` | `color-mix(in srgb, #201e1d 40%, transparent)` | 2px section rules, input borders |

**Neutral ramp**

| Step | 100 | 200 | 300 | 400 | 500 | 600 | 700 | 800 | 900 |
|---|---|---|---|---|---|---|---|---|---|
| Value | `#f8f4f4` | `#eae7e7` | `#d7d3d3` | `#bab6b6` | `#9b9797` | `#7d7979` | `#605d5d` | `#444141` | `#2d2b2b` |

**Accent ramp**

| Step | 100 | 200 | 300 | 400 | 500 | 600 | 700 | 800 | 900 |
|---|---|---|---|---|---|---|---|---|---|
| Value | `#fff2ef` | `#ffe0d9` | `#ffc4b8` | `#ff9783` | `#ff563c` | `#dd2b0f` | `#ae1800` | `#7c1405` | `#4d170e` |

- 600 is the hover state; 700 is the pressed state and the colour for accent text at body size.

**Accent-2 ramp** (stand-in, mirrors the accent)

| Step | 100 | 200 | 300 | 400 | 500 | 600 | 700 | 800 | 900 |
|---|---|---|---|---|---|---|---|---|---|
| Value | `#fff2ef` | `#ffe0da` | `#ffc4b9` | `#ff9784` | `#ef6853` | `#c94b39` | `#9e3526` | `#71261b` | `#471d16` |

**Hard-coded in the mockup**

- Link `#ae1800` = `--color-accent-700`; link hover `#dd2b0f` = `--color-accent-600`.
- "Price down" `#ae1800` = `--color-accent-700`; "price up" `#201e1d` = `--color-text`.
- Muted text `rgba(32,30,29,.7)`, and `.8` / `.85` for body copy (ink at 70/80/85%). No token; the system's `.text-muted` is 55%.
- Rules: 2px `rgba(32,30,29,.4)` = `--color-divider`. 1px hairlines at `.25`, `.2` and `.15` have no token.
- Callout top border: 2px `#ec3013` = `--color-accent`.
- Presentation backdrop `#dedbd8` sits outside the app; ignore it.

### Fonts

- **Family:** Archivo for headings and body (`--font-heading`, `--font-body`), from Google Fonts at weights 400, 600 and 800. Fallback `system-ui, sans-serif`.
- **Heading weight:** `--font-heading-weight: 800`.
- **Base body:** 15px, line-height 1.55, weight 400.
- **Headings:** line-height 1.12, letter-spacing -0.015em.
- **Heading scale:** h1 42px, h2 32px, h3 25px, h4 20px, h5 16px, h6 13px uppercase with 0.08em tracking.
- **Component sizes:**
  - buttons 14px (weight 800)
  - inputs 14px
  - segmented control 13px
  - tags 11px
  - table header 11px uppercase 0.08em; table body 14px
  - card title 17px; card kicker 10px uppercase 0.1em
- **Mockup sizes:**
  - 52px story headline (line-height 1.04)
  - 44px page titles
  - 27px feed headlines
  - 24px watchlist names
  - 22px brand (tracking -0.02em)
  - 19px intro paragraph
  - 17, 16, 15, 14, 13, 12, 11 and 10px for everything else
- **Mockup weights:** 600 (nav, links) and 800 (labels, figures).
- **Uppercase label tracking:** .04, .06, .08, .10, .12 and .14em.
- **Mockup line heights:** 1.3, 1.5, 1.6, 1.65, 1.7.
- **Mockup text widths:** 24ch, 30ch, 56ch, 58ch, 62ch.

### Spacing

- **Scale:** `--space-1` 4px · `--space-2` 8px · `--space-3` 12px · `--space-4` 16px · `--space-6` 24px · `--space-8` 32px. There are no 5 or 7 steps.
- **Component padding:**
  - button 8px × 14.4px
  - input 6px × 10px, min-height 36px
  - segment option 7px × 12px
  - tag 3px × 10px
  - table cells 8px
  - card 12px
- **Mockup page padding:** 40px sides; 40px top and 64–72px bottom on screens; 32px top on Story detail.
- **Mockup gaps and inner padding:** 6, 8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 36 and 48px.
- **Mockup row padding:** 32px (feed story), 20px (watchlist news), 18px (story commodity), 12px (rail).
- **Mockup layout sizes:**
  - canvas 1280×820
  - feed right rail 320px
  - story side column 360px
  - feed meta column 104px
  - watchlist news columns 120px / fill / 150px
  - commodity chip min-width 196px
  - avatar 32×32
  - search box 260px
- **Sparklines:** 68×22 (stroke 1.6), 300×64 on Story detail and 300×72 on Watchlist (stroke 2, non-scaling).

### Radii

- `--radius-sm`, `--radius-md`, `--radius-lg` are all `0px`. No rounded corners anywhere, on purpose. The only radii in the export belong to the fake browser window, which isn't part of the app.

### Other system rules

- **Shadows** (exist but unused in the mockup):
  - `--shadow-sm` `0 1px 2px color-mix(in srgb, #2d2b2b 14%, transparent)`
  - `--shadow-md` `0 3px 10px … 16%`
  - `--shadow-lg` `0 12px 32px … 22%`
- **Rules:** `.hr` is a 2px divider. The system says to keep 2px rules between sections and not soften them into hairlines.
- **States:** focus ring `2px solid var(--color-accent)`, offset 2px. Disabled controls at 45% opacity. Selection is an accent tint at 30%.
- **Icons:** Lucide per the design-system readme, but the mockup uses none.
- **Lint:** `_adherence.oxlintrc.json` warns on raw hex, raw px and non-Archivo fonts, and the mockup itself breaks those rules. When implementing, map values to tokens (e.g. `#ae1800` → `var(--color-accent-700)`).
- **No dark theme tokens.** §11 requires one, so dark values would have to be designed.
