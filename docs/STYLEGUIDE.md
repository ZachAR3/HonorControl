# Honor Control UI Style Guide

This is the visual source of truth for the Honor Control frontend. The product
should feel like a small, native laptop utility: quick to scan, quiet, and direct.
It is not an enterprise dashboard, a marketing site, or a decorative AI concept.

## Product posture

- Optimize for a user who opens the app to make one change in a few seconds.
- Show live hardware state before advanced configuration.
- Prefer plain language, short labels, and values aligned on a shared grid.
- Keep the visual system consistent between light and dark themes; dark mode is
  a palette swap, not a different composition.

## Window and layout

- Desktop utility window, straight-on, no browser chrome and no perspective.
- Default working size is 980 × 640 logical pixels; the supported compact
  minimum is 700 × 500.
- Persistent top bar: `Honor Control`, device model, service status, window actions.
- Persistent text-only sidebar with these labels, in this order:
  `Overview`, `Battery`, `Power`, `Touchpad`, `Fan`, `Diagnostics`, `Settings`.
- Sidebar is narrow and quiet. Active navigation uses a 3px burnt-orange vertical
  rule and orange text. Do not use filled navigation pills.
- Main content is one vertical flow of flat sections separated by 1px rules.
- Bottom utility links may contain `Diagnostics`, `Settings`, and `Quit` when useful;
  do not duplicate them as decorative tiles.
- Use spacing tokens of 8, 12, 16, 24, and 32px. Give sections room to breathe.
- Use 2–4px corner radii only where a control needs them. Square edges are preferred.
- Never use a grid of floating cards, oversized hero panels, glass surfaces, or
  decorative shadows.
- The header is 52px high. The sidebar is 176px wide at normal sizes and
  contracts to 136px below an 820px window width.
- Main content uses 32px horizontal and 24px vertical insets. At narrow sizes,
  preserve control usability with horizontal reflow and vertical scrolling;
  hide secondary header metadata before removing navigation or primary actions.

## Type

- Primary font: native system sans-serif; use `Inter`, `Noto Sans`, or `Segoe UI`
  when available.
- Telemetry and technical values may use `JetBrains Mono`, `Iosevka`, or another
  restrained monospace face.
- Default interface text is 14px. Page titles are 20px semibold; section titles
  are 15px medium.
- Section label: 18–20px, medium weight.
- Primary value: 30–36px, semibold; do not use display-sized marketing type.
- Supporting copy: 14–16px, secondary color.
- Navigation: 16px, regular; active state uses color, not bold decorative treatment.
- Sentence case only. Avoid all-caps labels except compact metadata where necessary.

## Palette

### Light theme

| Token | Hex | Use |
| --- | --- | --- |
| `canvas` | `#F2F0EA` | Window and page background |
| `surface` | `#FAF9F5` | Main content surface |
| `text` | `#24231F` | Primary text and values |
| `muted` | `#6D6A63` | Supporting text and metadata |
| `rule` | `#D8D4CC` | Dividers and control borders |
| `accent` | `#C85A38` | Active navigation, selected mode, primary action |
| `success` | `#63B239` | Healthy, running, enabled |
| `warning` | `#D8A52B` | Unavailable or caution |

### Dark theme

| Token | Hex | Use |
| --- | --- | --- |
| `canvas` | `#1D1E1C` | Window and page background |
| `surface` | `#242522` | Main content surface |
| `text` | `#ECE8DE` | Primary text and values |
| `muted` | `#A5A39B` | Supporting text and metadata |
| `rule` | `#3B3C38` | Dividers and control borders |
| `accent` | `#D45B37` | Active navigation, selected mode, primary action |
| `success` | `#7CC243` | Healthy, running, enabled |
| `warning` | `#E0B33C` | Unavailable or caution |

## Controls

- Performance modes use one restrained three-way segmented control:
  `Silent`, `Balanced`, `Performance`.
- Battery charge presets use one exclusive segmented control. Selecting `Off`,
  `Home`, `Travel`, or `Storage` always clears the previous selection.
- Selected controls use the accent fill with high-contrast text. Unselected controls
  are transparent or surface-colored with a thin rule.
- Sliders are thin, flat, and labeled with the current value. Avoid giant tracks.
- Toggles are compact and reserved for real on/off state, not decoration.
- Secondary actions are quiet text buttons such as `Adjust`, `Configure`, or `Reset`.
- Primary actions are rare and use the accent color.
- Do not use emoji, clip-art, generated illustrations, circular icon badges, or
  arbitrary pictograms.
- Prefer no icon at all. If an icon is necessary, use a single 16px monochrome
  platform/system glyph with no surrounding circle or colored container.
- Standard controls are at least 28px high. Segmented controls have 0px gaps so
  adjacent borders form one visual group.
- Text actions use the accent color with no border or fill. Reserve filled
  accent buttons for selected states and the rare primary action.
- Form-style configuration rows use a shared label column and a value control
  with a visible surface and border so editability is explicit.

## Component patterns

- A **section** is transparent and ends with a 1px rule. It is not a raised card.
- A **status row** places a muted label on the left and the value on the right.
  Status dots precede short state text when color alone would be ambiguous.
- A **primary value** is 30–32px semibold and appears only once in a section.
- A **quiet action** is an orange text button such as `Adjust`, `Configure`,
  `Reset profile`, or `Apply changes`.
- Touchpad firmware controls use two columns when space permits: touch response
  on the left and multitouch, edge, and knuckle groups on the right. The page
  scrolls vertically rather than squeezing controls at compact widths.
- The fan editor keeps its graph as the visual focus. Mode selection is a
  three-way segmented control, with point editing as a secondary row.

## Interaction states

- Hover changes a control border or text to the theme accent without moving it.
- Keyboard focus uses a 1px accent outline/border and must remain visible in
  both themes.
- Selected segmented controls use the accent fill and white text.
- Disabled controls retain their geometry and use muted text with a rule-color
  border; pending hardware actions disable only the affected control.
- Mouse-wheel and trackpad-scroll gestures over combo boxes, numeric inputs,
  and sliders scroll the page and never change the hovered value. Values are
  changed only through an intentional click, drag, or keyboard action.
- Status is always communicated with text as well as color.
- Custom animation is limited to the compact toggle transition. It is disabled
  without changing state when `HONOR_CONTROL_REDUCE_MOTION=1` is set.

## Theme behavior

- Resolve the palette once at application startup from the desktop light/dark
  preference. Do not mix light and dark surfaces in one window.
- All shared components consume palette tokens rather than page-local color
  overrides. Page-local styles may set typography, but not theme colors.
- Light and dark modes keep the same measurements, order, and wrapping rules.

## Overview information hierarchy

The overview should fit in one glance and contain no more than five content sections:

1. **Performance mode** — selected mode, CPU temperature, fan RPM, mode selector.
2. **Battery** — charge percentage, charging/source state, charge-limit slider.
3. **Cooling** — current mode, firmware-curve state, `Adjust` action.
4. **Touchpad gestures** — running state, report/shortcut counts, `Configure` action.
5. **Utility links** — `Diagnostics`, `Settings`, `Quit` as plain text actions.

Do not add new telemetry, charts, decorative summaries, or feature tiles to this
screen. Advanced editing belongs in the relevant sidebar section.

## Required product behavior

- Keep the sidebar geometry and main-content alignment identical in both themes.
- Preserve exact labels and values from the product state; do not invent capabilities.
- Make unavailable hardware explicit with text and the warning color; never imply support.
- Show service health in the top bar with a small status dot and text.
- Keep the UI believable as a Qt desktop app at compact laptop-window sizes.
