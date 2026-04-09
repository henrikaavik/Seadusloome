"""Design tokens — Python constants for all design system values.

Colors are sourced from Estonia Brand (brand.estonia.ee/guidelines/colors).
These constants mirror the CSS variables defined in app/static/css/tokens.css.
Use them in Python code when you need to reference token values dynamically
(e.g., inline styles, SVG fills). For static styling, prefer the CSS variables.
"""

# ---------------------------------------------------------------------------
# Estonia Brand colors
# ---------------------------------------------------------------------------

# Primary
ESTONIAN_BLUE = "#0030DE"  # brand primary

# Blue family
PARNU = "#CEE2FD"  # light blue
LIIVI = "#000087"  # deep blue
PALDISKI = "#0062F5"  # mid blue
NARVA = "#00C3FF"  # cyan accent

# Warm accent
HAAPSALU = "#FCEEC8"  # warm yellow

# Neutrals (Boulders)
EHAKIVI = "#FFFFFF"  # white
PAHKLA = "#F1F5F9"  # light gray background
HELLAMAA = "#CBD5E1"  # borders
KABELIKIVI = "#64748B"  # muted text
MAJAKIVI = "#3D4B5E"  # strong muted text
MUSTKIVI = "#0F172A"  # primary text

# Semantic (derived for status states)
SUCCESS = "#15803D"
WARNING = "#CA8A04"
DANGER = "#B91C1C"
INFO = PALDISKI  # alias

# ---------------------------------------------------------------------------
# Category colors (for the D3 explorer — kept for backward compat)
# ---------------------------------------------------------------------------

CATEGORY_COLORS = {
    "Enacted Law": "#38bdf8",
    "Draft Legislation": "#a78bfa",
    "Court Decisions": "#fb923c",
    "EU Legislation": "#34d399",
    "EU Court Decisions": "#f472b6",
}

# ---------------------------------------------------------------------------
# Typography scale
# ---------------------------------------------------------------------------

TEXT_XS = "0.75rem"  # 12px
TEXT_SM = "0.875rem"  # 14px
TEXT_BASE = "1rem"  # 16px
TEXT_LG = "1.125rem"  # 18px
TEXT_XL = "1.25rem"  # 20px
TEXT_2XL = "1.5rem"  # 24px
TEXT_3XL = "1.875rem"  # 30px
TEXT_4XL = "2.25rem"  # 36px

LEADING_TIGHT = "1.25"
LEADING_NORMAL = "1.5"
LEADING_RELAXED = "1.75"

FONT_FAMILY = "'Aino', Verdana, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"

# ---------------------------------------------------------------------------
# Spacing (8pt grid)
# ---------------------------------------------------------------------------

SPACE_0 = "0"
SPACE_1 = "0.25rem"  # 4px
SPACE_2 = "0.5rem"  # 8px
SPACE_3 = "0.75rem"  # 12px
SPACE_4 = "1rem"  # 16px
SPACE_5 = "1.25rem"  # 20px
SPACE_6 = "1.5rem"  # 24px
SPACE_8 = "2rem"  # 32px
SPACE_12 = "3rem"  # 48px
SPACE_16 = "4rem"  # 64px
SPACE_24 = "6rem"  # 96px

# ---------------------------------------------------------------------------
# Radius
# ---------------------------------------------------------------------------

RADIUS_SM = "4px"
RADIUS = "8px"
RADIUS_LG = "12px"
RADIUS_FULL = "9999px"

# ---------------------------------------------------------------------------
# Shadows
# ---------------------------------------------------------------------------

SHADOW_SM = "0 1px 2px rgba(15, 23, 42, 0.05)"
SHADOW = "0 4px 6px -1px rgba(15, 23, 42, 0.08)"
SHADOW_LG = "0 10px 25px -5px rgba(15, 23, 42, 0.12)"

# ---------------------------------------------------------------------------
# Breakpoints
# ---------------------------------------------------------------------------

BREAKPOINT_SM = "640px"
BREAKPOINT_MD = "768px"
BREAKPOINT_LG = "1024px"
BREAKPOINT_XL = "1280px"
