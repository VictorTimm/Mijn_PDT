# PDT Style Guide

This guide defines the visual system for the Streamlit interface and is the source of truth for UI decisions.

## Principles

- Dark mode is the default experience.
- Information density stays high while preserving clarity.
- Visual hierarchy follows the reference layout: white top bar, dark content canvas, chart-led portfolio view.
- European numeric formatting is used throughout the interface.

## Color Tokens

### Core Dark Theme

- `--pdt-bg: #071C1B` page background
- `--pdt-ink: #F4F7F6` primary text
- `--pdt-muted: #A7B9B4` secondary text
- `--pdt-line: #1C3A36` separators and borders
- `--pdt-card: #0C2624` raised surfaces

### Core Light Theme

- `--pdt-bg: #F6F7F4` page background
- `--pdt-ink: #14221F` primary text
- `--pdt-muted: #4D5B57` secondary text
- `--pdt-line: #E3E6E1` separators and borders
- `--pdt-card: #FFFFFF` raised surfaces

### Top Bar

- `--pdt-topbar-bg: #FFFFFF`
- `--pdt-topbar-ink: #14221F`
- `--pdt-topbar-muted: #6B7280`
- Active item underline: `#111827`, 2px

### Semantic

- `--pdt-positive: #3DCE8A`
- `--pdt-negative: #E07A5F`

### Chart Palette

Use the existing app palette in sequence:

1. `#7EB6E6`
2. `#5B8DEF`
3. `#3D6FD8`
4. `#2F6B4F`
5. `#3EAA7A`
6. `#7DCEA0`
7. `#F0A07A`
8. `#E07A5F`
9. `#E8C36A`
10. `#C45C4A`
11. `#8E6BB5`
12. `#6EC4C4`

## Typography

- UI font: Source Sans 3, fallback Segoe UI, sans-serif
- Page title: 2.1rem, weight 500, tight tracking
- Data values: tabular numerals where supported

## Layout

- App is full-width with controlled content max width (1280px).
- Top bar is a white sticky header: menu button, page nav, and total gain or loss in a squared pill.
- Main content is dark-first and layered on card-like sections.
- Import controls remain in a slim sidebar.

## Navigation

- Primary nav: Portfolio, Holdings, Transactions
- Active nav item is indicated by a dark underline.
- Broker filtering appears in the sidebar as a supporting control.

## Controls

- The header menu button and gain/loss figure use a squared pill (`10px` radius). Other buttons use a pill shape (`999px` radius).
- Share and Timetravel are secondary pills in the Portfolio header.
- Breakdown tabs are text-based with underline selection (not filled chips).

## Portfolio Allocation View

- Donut chart on the left.
- Two-column legend on the right.
- Legend row pattern: 12px rounded square, label, thin 3px share bar, right-aligned percentage.
- Include a faint isometric line-grid decoration on the legend side.

## Tables And Data Views

- Table surfaces use subtle borders and rounded corners.
- Header and filter controls inherit the active theme.
- Holdings and Transactions maintain consistent spacing and font sizes.

## Number And Locale Rules

- Language: English labels.
- Locale style: European number formatting.
- Money examples: `€ 2.470,84`, `-€ 152,30`.
- Percent examples: `8,14%`.
- Dates stay day-first where displayed.

## Theme Behavior

- Dark mode is default on first load.
- User can toggle between dark and light in the sidebar.
- Top bar remains white in both themes for visual consistency.
