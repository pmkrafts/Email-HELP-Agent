"""Design-system tokens and CSS for the Streamlit console.

Implements the project design spec: a warm paper-soft canvas over clinical white, near-black
`Inter` type with tight negative tracking at display sizes, exactly **one** structural accent
(blue, reserved for the primary action and links), and a decorative-only sticker palette that
appears as small category dots — never as a CTA or structural fill.

Tokens are plain data so `tests/test_ui_design.py` can assert the values against the spec.
"""
from __future__ import annotations

# --- colours ---------------------------------------------------------------------------------
COLORS = {
    # brand & accent (the only structural colour)
    "primary": "#0075de",
    "primary_active": "#005bab",
    "on_primary": "#ffffff",
    "secondary": "#213183",          # deep indigo: the single dark hero band
    # decorative sticker palette (dots, illustrations, tiles only)
    "accent_sky": "#62aef0",
    "accent_purple": "#d6b6f6",
    "accent_purple_deep": "#391c57",
    "accent_pink": "#ff64c8",
    "accent_orange": "#dd5b00",
    "accent_orange_deep": "#793400",
    "accent_teal": "#2a9d99",
    "accent_green": "#1aae39",
    "accent_brown": "#523410",
    # surfaces
    "canvas": "#ffffff",
    "surface": "#ffffff",
    "canvas_soft": "#f6f5f4",
    "hairline": "#e6e6e6",
    "input_border": "#dddddd",
    # text
    "ink": "#000000",
    "ink_secondary": "#31302e",
    "ink_muted": "#615d59",
    "ink_faint": "#a39e98",
}

RADII = {"xs": "4px", "sm": "5px", "md": "8px", "lg": "12px", "xl": "16px", "full": "9999px"}

# barely-there layered elevation (never a hard cast)
SHADOW_SOFT = (
    "rgba(0,0,0,0.01) 0 0.175px 1.041px, rgba(0,0,0,0.02) 0 0.8px 2.925px, "
    "rgba(0,0,0,0.027) 0 2.025px 7.847px, rgba(0,0,0,0.04) 0 4px 18px"
)
SHADOW_ELEVATED = (
    "rgba(0,0,0,0.01) 0 0.175px 1.041px, rgba(0,0,0,0.02) 0 0.8px 2.925px, "
    "rgba(0,0,0,0.027) 0 2.025px 7.847px, rgba(0,0,0,0.04) 0 6px 27px, "
    "rgba(0,0,0,0.05) 0 23px 52px"
)

FONT_STACK = 'Inter, -apple-system, system-ui, "Segoe UI", Helvetica, Arial, sans-serif'

# severity ramp for dots: calm -> urgent (decorative only, status is always also spelled out)
SEVERITY = ["accent_green", "accent_sky", "accent_orange", "accent_pink"]

PRIORITY_DOT = {"Critical": "accent_pink", "High": "accent_orange", "Medium": "accent_sky", "Low": "accent_green"}
URGENCY_DOT = {"critical": "accent_pink", "high": "accent_orange", "medium": "accent_sky", "low": "accent_green"}
SENTIMENT_DOT = {"angry": "accent_pink", "negative": "accent_orange", "neutral": "ink_faint", "positive": "accent_green"}
REVIEW_DOT = {
    "approved": "accent_green",
    "needs_revision": "accent_orange",
    "escalate": "accent_pink",
    "pending_human": "accent_teal",
    "scheduled": "accent_green",
    "junk": "accent_brown",
    "escalated": "accent_pink",
}
JUDGE_DOT = {
    "agreed": "accent_green",
    "overridden": "accent_purple",
    "adjudicated": "accent_sky",
    "error": "accent_orange",
}


def dot(kind: str, value: str | None) -> str:
    """Hex colour for a status dot, never a fill."""
    table = {
        "priority": PRIORITY_DOT,
        "urgency": URGENCY_DOT,
        "sentiment": SENTIMENT_DOT,
        "review": REVIEW_DOT,
        "judge": JUDGE_DOT,
    }.get(kind, {})
    return COLORS.get(table.get(value or "", "ink_faint"), COLORS["ink_faint"])


def chip(label: str, colour: str) -> str:
    """White pill with a decorative dot + ink text (never a coloured fill)."""
    return (
        f"<span class='chip'><span class='chip-dot' style='background:{colour}'></span>"
        f"<span class='chip-label'>{label}</span></span>"
    )


def build_css() -> str:
    c, r = COLORS, RADII
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {{
  --font: {FONT_STACK};
  --primary: {c['primary']};
  --primary-active: {c['primary_active']};
  --secondary: {c['secondary']};
  --canvas-soft: {c['canvas_soft']};
  --surface: {c['surface']};
  --hairline: {c['hairline']};
  --ink: {c['ink']};
  --ink-secondary: {c['ink_secondary']};
  --ink-muted: {c['ink_muted']};
  --ink-faint: {c['ink_faint']};
  --shadow-soft: {SHADOW_SOFT};
  --shadow-elevated: {SHADOW_ELEVATED};
}}

html, body, [class*="css"], .stApp, button, input, textarea, select {{
  font-family: {FONT_STACK};
}}
/* the design system mandates one family: beat Streamlit's own font rules, but keep icon glyphs */
.stApp *, [data-testid="stSidebar"] *, [data-testid="stAppViewContainer"] * {{
  font-family: {FONT_STACK} !important;
}}
[data-testid="stIconMaterial"], .material-symbols-rounded, [data-testid="stIcon"] {{
  font-family: "Material Symbols Rounded", "Material Icons" !important;
}}
.stApp {{ background: var(--canvas-soft); color: var(--ink); }}
.block-container {{ max-width: 1200px; padding-top: 1.25rem; padding-bottom: 4rem; }}

/* --- typography: tight, heavy headlines; calm 400 body --- */
h1 {{ font-size: 40px; font-weight: 700; line-height: 1.1; letter-spacing: -1px; color: var(--ink); }}
h2 {{ font-size: 26px; font-weight: 700; line-height: 1.23; letter-spacing: -0.625px; color: var(--ink); }}
h3 {{ font-size: 22px; font-weight: 700; line-height: 1.27; letter-spacing: -0.25px; color: var(--ink); }}
p, li, label, .stMarkdown {{ font-size: 16px; line-height: 1.5; color: var(--ink-secondary); }}
[data-testid="stCaptionContainer"] p, .stCaption, small {{
  font-size: 14px; line-height: 1.43; color: var(--ink-faint);
}}
a {{ color: var(--primary); text-decoration: none; }}

/* --- hero: the single dark indigo band --- */
.hero-band {{
  background: var(--secondary); color: #fff;
  padding: 36px 40px 32px; border-radius: {r['xl']}; margin: 4px 0 6px;
}}
.hero-band h1 {{ color: #fff; font-size: 40px; margin: 0 0 10px; }}
.hero-band p {{ color: rgba(255,255,255,0.82); font-size: 16px; margin: 0; max-width: 70ch; }}
.hero-stickers {{ margin-top: 18px; }}
.hero-stickers span {{
  display: inline-block; width: 12px; height: 12px; border-radius: 9999px; margin-right: 8px;
}}

/* --- chips: white pill, dot + ink label (decorative dot, never a fill) --- */
.chip {{
  display: inline-flex; align-items: center; gap: 6px; background: var(--surface);
  border: 1px solid var(--hairline); border-radius: 9999px; padding: 4px 12px 4px 10px;
  margin: 0 6px 6px 0; font-size: 14px; color: var(--ink); box-shadow: var(--shadow-soft);
}}
.chip-dot {{ width: 8px; height: 8px; border-radius: 9999px; display: inline-block; }}
.chip-label {{ font-size: 14px; font-weight: 500; color: var(--ink); }}

/* --- buttons: pill primary, 8px utility --- */
.stButton button, .stDownloadButton button, [data-testid="stBaseButton-secondary"] {{
  background: var(--surface); color: var(--ink); border: 1px solid var(--hairline);
  border-radius: {r['md']}; padding: 4px 14px; font-size: 16px; font-weight: 500;
}}
.stButton button[kind="primary"], [data-testid="stBaseButton-primary"] {{
  background: var(--primary); color: #fff; border: none; border-radius: {r['full']};
  padding: 10px 22px; font-weight: 500; box-shadow: var(--shadow-soft);
}}
.stButton button[kind="primary"]:active, [data-testid="stBaseButton-primary"]:active {{
  background: var(--primary-active); transform: scale(0.9);
}}

/* --- inputs: tight 4px corners, white surface, hairline border --- */
.stTextInput input, .stTextArea textarea, .stNumberInput input,
[data-baseweb="select"] > div, [data-baseweb="input"] > div {{
  background: var(--surface); color: var(--ink); border: 1px solid {c['input_border']};
  border-radius: {r['xs']}; font-size: 15px;
}}
.stTextArea textarea:disabled, .stTextInput input:disabled {{
  color: var(--ink-secondary); background: var(--surface); -webkit-text-fill-color: var(--ink-secondary);
}}
[data-testid="stFileUploaderDropzone"] {{
  background: var(--surface); border: 1px dashed {c['input_border']}; border-radius: {r['lg']};
}}

/* --- cards: white surface, 12px radius, hairline + barely-there shadow --- */
[data-testid="stMetric"] {{
  background: var(--surface); border: 1px solid var(--hairline); border-radius: {r['lg']};
  padding: 16px 20px; box-shadow: var(--shadow-soft);
}}
[data-testid="stMetricLabel"] p {{
  font-size: 12px; font-weight: 600; letter-spacing: 0.125px; text-transform: uppercase;
  color: var(--ink-muted);
}}
[data-testid="stMetricValue"] {{ font-size: 26px; font-weight: 700; letter-spacing: -0.625px; color: var(--ink); }}
[data-testid="stMetricDelta"] {{ font-size: 14px; color: var(--ink-faint); }}
[data-testid="stExpander"] {{
  background: var(--surface); border: 1px solid var(--hairline); border-radius: {r['lg']};
  box-shadow: var(--shadow-soft); margin-bottom: 10px; overflow: hidden;
}}
[data-testid="stExpander"] summary {{ font-size: 15px; font-weight: 500; color: var(--ink); }}
[data-testid="stDataFrame"], [data-testid="stTable"] {{
  border: 1px solid var(--hairline); border-radius: {r['lg']}; background: var(--surface);
  overflow: hidden; box-shadow: var(--shadow-soft);
}}
[data-testid="stTable"] thead th {{
  font-size: 12px; font-weight: 600; letter-spacing: 0.125px; text-transform: uppercase;
  color: var(--ink-muted); background: var(--surface);
}}
[data-testid="stTable"] tbody td {{ font-size: 15px; color: var(--ink-secondary); }}
[data-testid="stVerticalBlockBorderWrapper"] {{
  background: var(--surface); border: 1px solid var(--hairline) !important; border-radius: {r['lg']};
}}

/* --- tabs: 8px shape, blue active signal (the one structural accent) --- */
.stTabs [data-baseweb="tab-list"] {{ gap: 6px; border-bottom: 1px solid var(--hairline); }}
.stTabs [data-baseweb="tab"] {{
  font-size: 15px; color: var(--ink-muted); border-radius: {r['md']} {r['md']} 0 0; padding: 8px 12px;
}}
.stTabs [aria-selected="true"] {{ color: var(--ink); font-weight: 500; }}
.stTabs [data-baseweb="tab-highlight"] {{ background: var(--primary); }}

/* --- alerts: white card with a decorative hairline accent bar --- */
[data-testid="stAlert"] {{
  background: var(--surface) !important; border: 1px solid var(--hairline);
  border-left: 3px solid var(--primary); border-radius: {r['md']}; color: var(--ink-secondary);
  box-shadow: var(--shadow-soft);
}}
[data-testid="stAlert"] p {{ font-size: 15px; color: var(--ink-secondary); }}

/* --- code / reasoning blocks --- */
pre, code, .stCode {{ background: var(--canvas-soft) !important; border: 1px solid var(--hairline);
  border-radius: {r['md']}; color: var(--ink-secondary) !important; font-size: 15px; }}

/* --- sidebar: white surface, hairline divider, quiet links --- */
[data-testid="stSidebar"] {{ background: var(--surface); border-right: 1px solid var(--hairline); }}
[data-testid="stSidebar"] h1 {{ font-size: 22px; letter-spacing: -0.25px; }}
[data-testid="stSidebar"] .stMarkdown p, [data-testid="stSidebar"] label {{ font-size: 15px; color: var(--ink-secondary); }}

/* --- form controls in the one accent --- */
[data-testid="stSlider"] [role="slider"] {{ background: var(--primary); }}
[data-testid="stCheckbox"] input:checked + div, [data-testid="stRadio"] input:checked + div {{ border-color: var(--primary); }}
hr {{ border-color: var(--hairline); }}
</style>
"""
