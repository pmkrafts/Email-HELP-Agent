"""Design-system contract: the UI tokens must match the design spec, and the CSS must carry the
rules that matter (warm canvas, one blue accent, pill primary vs 4px inputs, hairline cards).

These tests are a guard against silent drift away from the design language.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))

from design import COLORS, FONT_STACK, RADII, REVIEW_DOT, build_css, chip, dot  # noqa: E402


class TokenTests(unittest.TestCase):
    def test_spec_hex_values(self) -> None:
        self.assertEqual(COLORS["primary"], "#0075de")
        self.assertEqual(COLORS["primary_active"], "#005bab")
        self.assertEqual(COLORS["secondary"], "#213183")
        self.assertEqual(COLORS["canvas_soft"], "#f6f5f4")
        self.assertEqual(COLORS["canvas"], "#ffffff")
        self.assertEqual(COLORS["hairline"], "#e6e6e6")
        self.assertEqual(COLORS["ink"], "#000000")
        self.assertEqual(COLORS["ink_secondary"], "#31302e")
        self.assertEqual(COLORS["ink_muted"], "#615d59")
        self.assertEqual(COLORS["ink_faint"], "#a39e98")

    def test_sticker_palette(self) -> None:
        self.assertEqual(COLORS["accent_sky"], "#62aef0")
        self.assertEqual(COLORS["accent_purple"], "#d6b6f6")
        self.assertEqual(COLORS["accent_pink"], "#ff64c8")
        self.assertEqual(COLORS["accent_orange"], "#dd5b00")
        self.assertEqual(COLORS["accent_teal"], "#2a9d99")
        self.assertEqual(COLORS["accent_green"], "#1aae39")
        self.assertEqual(COLORS["accent_brown"], "#523410")

    def test_radii_scale(self) -> None:
        self.assertEqual(RADII["xs"], "4px")
        self.assertEqual(RADII["md"], "8px")
        self.assertEqual(RADII["lg"], "12px")
        self.assertEqual(RADII["full"], "9999px")

    def test_font_stack_prefers_inter_locally(self) -> None:
        self.assertTrue(FONT_STACK.startswith("Inter"))
        for fallback in ("-apple-system", "Segoe UI", "Helvetica"):
            self.assertIn(fallback, FONT_STACK)


class BehaviourTests(unittest.TestCase):
    def test_dots_are_decorative_only(self) -> None:
        # every status resolves to a sticker/neutral colour - never the structural blue
        for kind, value in (("priority", "Critical"), ("review", "escalate"), ("judge", "agreed"),
                            ("sentiment", "angry"), ("urgency", "high")):
            colour = dot(kind, value)
            self.assertNotEqual(colour, COLORS["primary"])
            self.assertIn(colour, {v for v in COLORS.values()})
        self.assertEqual(dot("unknown-kind", "whatever"), COLORS["ink_faint"])
        self.assertEqual(REVIEW_DOT["pending_human"], "accent_teal")

    def test_chip_is_a_dot_not_a_fill(self) -> None:
        markup = chip("Critical", COLORS["accent_pink"])
        self.assertIn("chip-dot", markup)
        self.assertIn(COLORS["accent_pink"], markup)
        self.assertNotIn("background:#ff64c8;color", markup)  # no coloured label fill

    def test_css_carries_the_critical_rules(self) -> None:
        css = build_css()
        checks = {
            "warm canvas": "--canvas-soft: #f6f5f4",
            "single blue accent": "--primary: #0075de",
            "pill primary button": "border-radius: 9999px;\n  padding: 10px 22px",
            "utility button radius": "border-radius: 8px; padding: 4px 14px",
            "input radius": "border-radius: 4px",
            "card radius": "border-radius: 12px",
            "hairline borders": "1px solid var(--hairline)",
            "inter font": "family=Inter",
            "display tracking": "letter-spacing: -1px",
            "layered soft elevation": "rgba(0,0,0,0.027) 0 2.025px 7.847px",
            "reserved elevated token": "--shadow-elevated",
            "hero dark band": "background: var(--secondary)",
        }
        for label, needle in checks.items():
            self.assertIn(needle, css, label)


class ThemeConfigTests(unittest.TestCase):
    def test_streamlit_theme_maps_the_design_tokens(self) -> None:
        import tomllib

        config_path = ROOT / ".streamlit" / "config.toml"
        self.assertTrue(config_path.exists(), "Streamlit theme config is missing")
        with config_path.open("rb") as fh:
            config = tomllib.load(fh)
        theme = config["theme"]
        self.assertEqual(theme["primaryColor"].lower(), COLORS["primary"])
        self.assertEqual(theme["backgroundColor"].lower(), COLORS["canvas_soft"])
        self.assertEqual(theme["secondaryBackgroundColor"].lower(), COLORS["canvas"])
        self.assertEqual(theme["textColor"].lower(), COLORS["ink_secondary"])


if __name__ == "__main__":
    unittest.main()
