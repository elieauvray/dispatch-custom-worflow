"""Swimlane SVG of a Maestro design: one column per actor, steps in order, phases as bands.

Colors follow the Upsun brand and the page's CSS variables (light and dark), with hex fallbacks
so the exported file renders the same outside the app.
"""

from __future__ import annotations

from html import escape
from typing import Any

from markupsafe import Markup

W = 960
LABEL_W = 0
TOP = 64
ROW_H = 70
GAP = 22
BAND_H = 30
FONT = "Instrument Sans, Arial, sans-serif"

TEXT = "var(--text, #0C0F10)"
MUTED = "var(--muted, #646F75)"
BORDER = "var(--border, #D8DBDC)"
SURFACE = "var(--surface, #FFFFFF)"
BG = "var(--bg, #ECEDEE)"
ACCENT = "var(--accent, #6046FF)"
LIME, BLUE, PINK, NEUTRAL_1000 = "#D5F800", "#00D0EC", "#E81CEC", "#0C0F10"

ACTOR_ORDER = ["human", "maestro", "dispatch", "upsun", "external"]
ACTOR_LABELS = {
    "human": "People (gates)",
    "maestro": "Maestro",
    "dispatch": "Upsun Dispatch",
    "upsun": "Upsun Cloud",
    "external": "Other systems",
}
# fill, fill-opacity, stroke, text
ACTOR_STYLE = {
    "human": (LIME, "1", LIME, NEUTRAL_1000),
    "maestro": (ACCENT, "0.14", ACCENT, TEXT),
    "dispatch": (PINK, "0.12", PINK, TEXT),
    "upsun": (BLUE, "0.16", BLUE, TEXT),
    "external": (SURFACE, "1", BORDER, TEXT),
}
DISPATCH_LABELS = {
    "issue_to_code": "Issue to code",
    "code_review": "Code review",
    "full_review": "Full review",
    "cve_scan": "CVE scan",
}


DONE_LABELS = {
    "pr_merged": "PR merged",
    "pr_approved": "PR approved",
    "issue_closed": "issue closed",
    "pr_opened": "PR opened",
}


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _t(x: float, y: float, value: str, *, size: int = 12, weight: int = 400, fill: str = TEXT,
       anchor: str = "middle") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}" text-anchor="{anchor}" dominant-baseline="central">{escape(value)}</text>'
    )


def render_design_svg(design: dict[str, Any]) -> Markup:
    steps: list[dict[str, Any]] = design.get("steps") or []
    used = [a for a in ACTOR_ORDER if any(s.get("actor") == a for s in steps)] or ["maestro"]
    col_w = (W - LABEL_W) / len(used)
    box_w = col_w - 20
    chars = int(box_w / 6.9)  # ~6.9px per character at 11-13px, with padding
    col_x = {a: LABEL_W + i * col_w for i, a in enumerate(used)}

    parts: list[str] = []
    # Column headers and lane separators
    for a in used:
        fill, op, stroke, tcolor = ACTOR_STYLE[a]
        x = col_x[a]
        parts.append(
            f'<rect x="{x + 10:.1f}" y="12" width="{box_w:.1f}" height="34" rx="6" fill="{fill}" '
            f'fill-opacity="{op}" stroke="{stroke}" stroke-width="1"/>'
        )
        parts.append(_t(x + col_w / 2, 29, ACTOR_LABELS[a], size=13, weight=700, fill=tcolor))

    y = TOP
    centers: list[tuple[float, float, float]] = []  # (cx, top, bottom)
    current_phase = None
    for s in steps:
        phase = s.get("phase") or ""
        if phase and phase != current_phase:
            current_phase = phase
            parts.append(
                f'<rect x="0" y="{y}" width="{W}" height="{BAND_H - 6}" rx="4" fill="{BG}" stroke="{BORDER}" '
                f'stroke-width="1"/>'
            )
            parts.append(_t(12, y + (BAND_H - 6) / 2, _clip(phase, 120), size=12, weight=700, fill=TEXT,
                            anchor="start"))
            y += BAND_H
        actor = s.get("actor") if s.get("actor") in ACTOR_STYLE else "maestro"
        fill, op, stroke, tcolor = ACTOR_STYLE[actor]
        x = col_x.get(actor, col_x[used[0]]) + 10
        cx = x + box_w / 2
        parts.append(
            f'<rect x="{x:.1f}" y="{y}" width="{box_w:.1f}" height="{ROW_H}" rx="8" fill="{fill}" '
            f'fill-opacity="{op}" stroke="{stroke}" stroke-width="1"/>'
        )
        sub = MUTED if actor != "human" else NEUTRAL_1000
        tags = []
        if actor == "human":
            tags.append("GATE" + (f" · {s.get('gate_owner')}" if s.get("gate_owner") else ""))
        if s.get("done_when") in DONE_LABELS:  # the completion signal matters most: show it first
            tags.append("done when " + DONE_LABELS[s["done_when"]])
        if actor == "dispatch" and s.get("dispatch_workflow") in DISPATCH_LABELS:
            tags.append(DISPATCH_LABELS[s["dispatch_workflow"]])
        if s.get("repeat"):
            tags.append("↻ " + s["repeat"])
        if s.get("loop"):
            tags.append("⟲ " + s["loop"])
        parts.append(_t(x + 10, y + 14, _clip(f"{s.get('id', '')} " + " · ".join(tags), chars + 4), size=10,
                        fill=sub, anchor="start"))
        parts.append(_t(cx, y + 34, _clip(s.get("title", ""), chars - 2), size=13, weight=700, fill=tcolor))
        if s.get("detail"):
            parts.append(_t(cx, y + 53, _clip(s["detail"], chars), size=11, fill=sub))
        centers.append((cx, y, y + ROW_H))
        y += ROW_H + GAP

    # Arrows between consecutive steps (drawn on top so phase bands never hide them)
    arrows = []
    for (cx1, _t1, b1), (cx2, t2, _b2) in zip(centers, centers[1:]):
        mid = b1 + (t2 - b1) / 2
        if abs(cx1 - cx2) < 1:
            d = f"M{cx1:.1f} {b1} L{cx2:.1f} {t2 - 3}"
        else:
            d = f"M{cx1:.1f} {b1} L{cx1:.1f} {mid:.1f} L{cx2:.1f} {mid:.1f} L{cx2:.1f} {t2 - 3}"
        arrows.append(
            f'<path d="{d}" fill="none" stroke="{MUTED}" stroke-width="1.2" marker-end="url(#ms-arrow)"/>'
        )
    height = y + 10
    summary = escape(design.get("summary") or "Maestro workflow")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" width="100%" role="img" '
        f'aria-labelledby="ms-title ms-desc" class="maestro-diagram"><title id="ms-title">Maestro workflow'
        f'</title><desc id="ms-desc">{summary}</desc><defs><marker id="ms-arrow" viewBox="0 0 10 10" '
        f'refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path '
        f'd="M2 1L8 5L2 9" fill="none" stroke="context-stroke" stroke-width="1.5" stroke-linecap="round" '
        f'stroke-linejoin="round"/></marker></defs>' + "".join(parts) + "".join(arrows) + "</svg>"
    )
    return Markup(svg)
