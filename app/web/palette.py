"""The colours the pages actually paint, and the contrast they achieve.

Light is the export's, unchanged, including the primary button on `--color-accent` (#ec3013)
with the page ground as its label, which is what `design-system.css` specifies. Dark is
derived here, because the export defines no dark theme (design/NOTES.md 5 and 6).

Keeping the values as data lets the test suite prove the derived theme is as readable as the
one it came from, and lets the one pair that falls short say so out loud instead of being
quietly "fixed".
"""

from dataclasses import dataclass

# Resolved colours (no var() indirection): light from design-system.css, dark from theme.css.
LIGHT: dict[str, str] = {
    "bg": "#f3f2f2",
    "surface": "#eae9e9",
    "text": "#201e1d",
    "accent": "#ec3013",
    "link": "#ae1800",
    "button_bg": "#ec3013",
    "button_fg": "#f3f2f2",
    "up": "#201e1d",
    "down": "#ae1800",
}

DARK: dict[str, str] = {
    "bg": "#1a1918",
    "surface": "#242221",
    "text": "#edeae8",
    "accent": "#ec3013",
    "link": "#ff9783",
    "button_bg": "#ec3013",
    "button_fg": "#1a1918",
    "up": "#edeae8",
    "down": "#ff9783",
}

# The mockup writes muted text as ink at 70%, which is what pages use. The system's own
# `.text-muted` is 55% (3.66:1 on the page ground), left alone and used for decoration only.
MUTED_ALPHA = 0.70
# --color-body-dim: the step between muted and full ink, for text that is read and not skimmed.
DIM_ALPHA = 0.85
FAINT_ALPHA = 0.55

# WCAG 2.1: 4.5:1 for normal text, 3:1 for large text and UI components. Button labels are
# 14px, which is not "large", so they need 4.5 too.
AA_TEXT = 4.5
AA_UI = 3.0


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def blend(foreground: str, background: str, alpha: float) -> str:
    """What a translucent colour actually paints over `background`."""
    front, back = _rgb(foreground), _rgb(background)
    mixed = (round(front[i] * alpha + back[i] * (1 - alpha)) for i in range(3))
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


def _relative_luminance(color: str) -> float:
    def channel(raw: int) -> float:
        value = raw / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in _rgb(color))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground: str, background: str) -> float:
    """WCAG contrast ratio, 1.0 to 21.0."""
    first, second = _relative_luminance(foreground), _relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


@dataclass(frozen=True)
class Check:
    """One foreground/background pair, measured in both themes."""

    pair: str
    foreground: str  # a key in LIGHT / DARK, or "muted" / "faint"
    background: str
    minimum: float
    # Set where the design's own value is kept although it misses `minimum`. The ratio is
    # still reported; the test asserts it is no worse than what is recorded here.
    accepted_below_aa: bool = False
    note: str = ""

    def colors(self, theme: dict[str, str]) -> tuple[str, str]:
        background = theme[self.background]
        if self.foreground in ("muted", "faint"):
            alpha = MUTED_ALPHA if self.foreground == "muted" else FAINT_ALPHA
            return blend(theme["text"], background, alpha), background
        return theme[self.foreground], background

    def ratio(self, theme: dict[str, str]) -> float:
        foreground, background = self.colors(theme)
        return contrast(foreground, background)


CHECKS: tuple[Check, ...] = (
    Check("Body text on the page", "text", "bg", AA_TEXT),
    Check("Body text on a panel", "text", "surface", AA_TEXT),
    Check("Muted text on the page", "muted", "bg", AA_TEXT, note="ink at 70%, as the mockup"),
    Check("Link on the page", "link", "bg", AA_TEXT),
    Check("Link on a panel", "link", "surface", AA_TEXT),
    Check(
        "Primary button label on its fill",
        "button_fg",
        "button_bg",
        AA_TEXT,
        accepted_below_aa=True,
        note="the export's accent, kept",
    ),
    Check("Price up on the page", "up", "bg", AA_TEXT),
    Check("Price down on the page", "down", "bg", AA_TEXT),
    Check("Focus ring on the page", "accent", "bg", AA_UI),
)


def rows() -> list[dict[str, str]]:
    """The contrast table on the design page."""
    table = []
    for check in CHECKS:
        light, dark = check.ratio(LIGHT), check.ratio(DARK)
        if check.accepted_below_aa:
            verdict = "below AA, by design"
        else:
            verdict = "AA" if min(light, dark) >= check.minimum else "fails"
        table.append(
            {
                "pair": check.pair + (f" ({check.note})" if check.note else ""),
                "light": f"{light:.2f}:1",
                "dark": f"{dark:.2f}:1",
                "needs": f"{check.minimum:.1f}:1",
                "verdict": verdict,
            }
        )
    return table
