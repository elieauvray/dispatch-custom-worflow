"""Server-side SVG rendering of a planned workflow, in Upsun brand colors.

Colors come from the page's CSS variables (light and dark themes), with hex fallbacks
so an exported .svg file renders the same outside the app.
"""

from __future__ import annotations

from html import escape
from typing import Any

from markupsafe import Markup

W = 680
MAIN_X, MAIN_W = 40, 400
SIDE_X, SIDE_W = 470, 170
BOX_H, GAP = 56, 30
FONT = "Instrument Sans, Arial, sans-serif"

TEXT = "var(--text, #0C0F10)"
MUTED = "var(--muted, #646F75)"
BORDER = "var(--border, #D8DBDC)"
SURFACE = "var(--surface, #FFFFFF)"
BG = "var(--bg, #ECEDEE)"
ACCENT = "var(--accent, #6046FF)"
LIME, BLUE, ORANGE, NEUTRAL_1000 = "#D5F800", "#00D0EC", "#FF4A11", "#0C0F10"

SERVICE_LABELS = {"linear": "Linear", "github": "GitHub", "atlassian": "Jira / Atlassian", "sandbox": "Sandbox"}
KIND_LABELS = {
    "read": "Read",
    "action": "Action",
    "reasoning": "Claude",
    "question": "Question",
    "result": "Result",
}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _text(x: float, y: float, value: str, *, size: int = 14, weight: int = 400, fill: str = TEXT,
          anchor: str = "middle") -> str:
    return (
        f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}" dominant-baseline="central">{escape(value)}</text>'
    )


def _box_style(kind: str) -> tuple[str, str, str, str]:
    """(fill, fill-opacity, stroke, text color) per step kind."""
    if kind == "read":
        return BLUE, "0.16", BLUE, TEXT
    if kind == "action":
        return ACCENT, "0.14", ACCENT, TEXT
    if kind == "result":
        return TEXT, "1", TEXT, BG
    return SURFACE, "1", BORDER, TEXT  # reasoning, question


def _side(y: int, label: str, *, fill: str, stroke: str, text_fill: str, dashed: bool = False) -> str:
    cy = y + BOX_H / 2
    dash = ' stroke-dasharray="4 3"' if dashed else ""
    return (
        f'<line x1="{MAIN_X + MAIN_W}" y1="{cy}" x2="{SIDE_X}" y2="{cy}" stroke="{stroke}" '
        f'stroke-width="1" stroke-dasharray="4 3"/>'
        f'<rect x="{SIDE_X}" y="{y + 8}" width="{SIDE_W}" height="{BOX_H - 16}" rx="6" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="1"{dash}/>'
        + _text(SIDE_X + SIDE_W / 2, cy, label, size=13, weight=600, fill=text_fill)
    )


def render_plan_svg(plan: dict[str, Any], request_label: str = "Your request") -> Markup:
    steps: list[dict[str, Any]] = plan.get("steps") or []
    parts: list[str] = []
    y = 20
    nodes = [{"title": request_label, "detail": "", "kind": "start"}] + steps
    for i, step in enumerate(nodes):
        kind = step.get("kind", "reasoning")
        if kind == "start":
            fill, opacity, stroke, tcolor = BG, "1", MUTED, TEXT
        else:
            fill, opacity, stroke, tcolor = _box_style(kind)
        parts.append(
            f'<rect x="{MAIN_X}" y="{y}" width="{MAIN_W}" height="{BOX_H}" rx="8" fill="{fill}" '
            f'fill-opacity="{opacity}" stroke="{stroke}" stroke-width="1"/>'
        )
        if kind != "start":
            tag = KIND_LABELS.get(kind, "")
            service = SERVICE_LABELS.get(step.get("service", ""), "")
            meta = f"{i}. {tag}" + (f" · {service}" if service else "")
            parts.append(_text(MAIN_X + 12, y + 13, meta, size=11, fill=tcolor if kind == "result" else MUTED,
                               anchor="start"))
        detail = step.get("detail") or ""
        title_y = y + (30 if detail or kind != "start" else BOX_H / 2)
        if not detail and kind != "start":
            title_y = y + 34
        parts.append(_text(MAIN_X + MAIN_W / 2, title_y, _clip(step.get("title", ""), 46),
                           weight=600, fill=tcolor))
        if detail:
            parts.append(_text(MAIN_X + MAIN_W / 2, y + 46, _clip(detail, 58), size=12,
                               fill=tcolor if kind == "result" else MUTED))
        # Your interventions through Dispatch, on the side
        if step.get("missing_connector"):
            parts.append(_side(y, "Connector missing", fill=SURFACE, stroke=ORANGE, text_fill=ORANGE,
                               dashed=True))
        elif step.get("needs_approval"):
            parts.append(_side(y, "Your approval", fill=LIME, stroke=LIME, text_fill=NEUTRAL_1000))
        elif kind == "question":
            parts.append(_side(y, "Your reply", fill=LIME, stroke=LIME, text_fill=NEUTRAL_1000))
        # Arrow to the next step
        if i < len(nodes) - 1:
            x = MAIN_X + MAIN_W / 2
            parts.append(
                f'<line x1="{x}" y1="{y + BOX_H}" x2="{x}" y2="{y + BOX_H + GAP - 4}" stroke="{MUTED}" '
                f'stroke-width="1.2" marker-end="url(#plan-arrow)"/>'
            )
        y += BOX_H + GAP

    # Legend
    ly = y - GAP + 24
    legend = [
        (BLUE, "0.16", BLUE, "Read"),
        (ACCENT, "0.14", ACCENT, "Action"),
        (SURFACE, "1", BORDER, "Claude step"),
        (LIME, "1", LIME, "You, via Dispatch"),
    ]
    lx = MAIN_X
    for fill, opacity, stroke, label in legend:
        parts.append(
            f'<rect x="{lx}" y="{ly - 6}" width="12" height="12" rx="2" fill="{fill}" '
            f'fill-opacity="{opacity}" stroke="{stroke}" stroke-width="1"/>'
        )
        parts.append(_text(lx + 18, ly, label, size=12, fill=MUTED, anchor="start"))
        lx += 26 + len(label) * 7 + 20
    height = ly + 20

    summary = escape(plan.get("summary") or "Planned workflow")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" width="100%" '
        f'role="img" aria-labelledby="plan-title plan-desc" class="plan-diagram">'
        f'<title id="plan-title">Planned workflow</title><desc id="plan-desc">{summary}</desc>'
        f'<defs><marker id="plan-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" '
        f'stroke="context-stroke" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        f"</marker></defs>" + "".join(parts) + "</svg>"
    )
    return Markup(svg)
