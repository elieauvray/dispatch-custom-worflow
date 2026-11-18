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


NO_VALUE = {"", "none", "n/a", "na", "no", "-", "null"}


def tag_text(value: Any) -> str:
    """Repeat/loop text, empty when the designer wrote a placeholder such as "none"."""
    text = str(value or "").strip()[:60]
    return "" if text.lower().rstrip(".") in NO_VALUE else text


def step_tags(s: dict[str, Any], with_loop: bool = True) -> list[str]:
    tags = []
    if s.get("actor") == "human":
        tags.append("GATE" + (f" · {s.get('gate_owner')}" if s.get("gate_owner") else ""))
    if s.get("done_when") in DONE_LABELS:  # the completion signal matters most: show it first
        tags.append("done when " + DONE_LABELS[s["done_when"]])
    if s.get("actor") == "dispatch" and s.get("dispatch_workflow") in DISPATCH_LABELS:
        tags.append(DISPATCH_LABELS[s["dispatch_workflow"]])
    repeat = tag_text(s.get("repeat"))
    if repeat:
        tags.append(f"↻ {repeat}")
    loop = tag_text(s.get("loop"))
    if s.get("loop_to"):
        if with_loop:
            tags.append(f"⟲ back to {s['loop_to']}" + (f" when {loop}" if loop else ""))
    elif loop:
        tags.append(f"⟲ {loop}")
    return tags


def step_info(s: dict[str, Any]) -> dict[str, Any]:
    """Full, untruncated text of a step (the diagram clips long texts)."""
    actor = s.get("actor") if s.get("actor") in ACTOR_LABELS else "maestro"
    return {
        "id": str(s.get("id", "")),
        "title": str(s.get("title", "")),
        "detail": str(s.get("detail", "")),
        "phase": str(s.get("phase", "")),
        "actor": actor,
        "actor_label": ACTOR_LABELS[actor],
        "tags": step_tags(s),
    }


def _tooltip(s: dict[str, Any]) -> str:
    info = step_info(s)
    lines = [f"{info['id']} · {info['title']}", info["detail"], " · ".join([info["actor_label"], *info["tags"]])]
    return "<title>" + escape("\n".join(line for line in lines if line)) + "</title>"


PILL_H = 22
LOOP_LANE = 16


def _pill(cx: float, cy: float, label: str, *, fill: str = SURFACE, stroke: str = BORDER, color: str = TEXT,
          weight: int = 600, size: int = 11, dashed: bool = False) -> str:
    w = max(len(label) * 6.3 + 18, 34)
    dash = ' stroke-dasharray="4 3"' if dashed else ""
    return (
        f'<rect x="{cx - w / 2:.1f}" y="{cy - PILL_H / 2:.1f}" width="{w:.1f}" height="{PILL_H}" rx="{PILL_H / 2}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1"{dash}/>'
        + _t(cx, cy, label, size=size, weight=weight, fill=color)
    )


def _blocks(steps: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """Main steps one by one, and each run of branch steps as one block of side-by-side paths."""
    blocks: list[tuple[str, Any]] = []
    for s in steps:
        label = str(s.get("branch") or "").strip()
        if not label:
            blocks.append(("step", s))
            continue
        if not blocks or blocks[-1][0] != "branches":
            blocks.append(("branches", {}))
        blocks[-1][1].setdefault(label, []).append(s)
    return blocks


NO_ROOM = 44         # room right of the steps for the "No" label of a loop
NO_RED = "#FF4A11"   # Upsun orange 600: the "No" way back, next to the lime "Yes" way forward
STEP_GAP = 46        # between two steps
YES_ROOM = 30        # extra room below a step that can loop back, for its "Yes" label
ARROW_RUN = 24       # length of the straight segment that ends in an arrowhead


def _pill_w(label: str) -> float:
    return max(len(label) * 6.3 + 18, 34)


def render_design_svg(design: dict[str, Any]) -> Markup:
    steps: list[dict[str, Any]] = design.get("steps") or []
    ids = {str(s.get("id", "")) for s in steps}
    loops = [s for s in steps if s.get("loop_to") and s.get("loop_to") in ids]
    loop_ids = {str(s.get("id")) for s in loops}
    labels = {str(s.get("id")): _clip(str(s.get("loop")), 30) for s in loops if s.get("loop")}
    label_w = max((_pill_w(t) for t in labels.values()), default=0)
    # Loop labels and lanes live in a right margin, never over the steps.
    # "No" sits right where the dashed arrow leaves the step; its condition goes in the right margin.
    gutter = (NO_ROOM + label_w + 28 + LOOP_LANE * len(loops)) if loops else 0
    width = W
    used = [a for a in ACTOR_ORDER if any(s.get("actor") == a for s in steps)] or ["maestro"]
    col_w = (width - LABEL_W) / len(used)
    box_w = col_w - 20
    col_x = {a: LABEL_W + i * col_w for i, a in enumerate(used)}

    parts: list[str] = []
    overlays: list[str] = []  # drawn last: labels sitting on arrows
    arrows: list[str] = []
    boxes: dict[str, tuple[float, float, float]] = {}  # id -> (x, y, w)

    for a in used:
        fill, op, stroke, tcolor = ACTOR_STYLE[a]
        x = col_x[a]
        parts.append(
            f'<rect x="{x + 10:.1f}" y="12" width="{box_w:.1f}" height="34" rx="6" fill="{fill}" '
            f'fill-opacity="{op}" stroke="{stroke}" stroke-width="1"/>'
        )
        parts.append(_t(x + col_w / 2, 29, ACTOR_LABELS[a], size=13, weight=700, fill=tcolor))

    def arrow(d: str, dashed: bool = False) -> None:
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        color = ACCENT if dashed else MUTED
        arrows.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.3"{dash} '
                      f'marker-end="url(#ms-arrow)"/>')

    def link(x1: float, b1: float, x2: float, t2: float, turn: float) -> None:
        if abs(x1 - x2) < 1:
            arrow(f"M{x1:.1f} {b1:.1f} L{x2:.1f} {t2 - 3:.1f}")
        else:
            arrow(f"M{x1:.1f} {b1:.1f} L{x1:.1f} {turn:.1f} L{x2:.1f} {turn:.1f} L{x2:.1f} {t2 - 3:.1f}")

    def draw_step(s: dict[str, Any], x: float, y: float, w: float, show_actor: bool) -> float:
        """Draws the step; returns where the next thing may start (below its "Yes" label if any)."""
        actor = s.get("actor") if s.get("actor") in ACTOR_STYLE else "maestro"
        fill, op, stroke, tcolor = ACTOR_STYLE[actor]
        chars = int(w / 6.9)
        cx = x + w / 2
        parts.append(f'<g class="ms-step" data-step="{escape(str(s.get("id", "")))}">' + _tooltip(s))
        parts.append(
            f'<rect class="ms-ring" x="{x - 5:.1f}" y="{y - 5:.1f}" width="{w + 10:.1f}" height="{ROW_H + 10}" '
            f'rx="11" fill="none" stroke="{ACCENT}" stroke-width="3" visibility="hidden"/>'
        )
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{ROW_H}" rx="8" fill="{fill}" '
            f'fill-opacity="{op}" stroke="{stroke}" stroke-width="1"/>'
        )
        sub = MUTED if actor != "human" else NEUTRAL_1000
        tags = step_tags({**s, "actor": actor}, with_loop=False)
        if show_actor and actor != "human":  # inside a path the column no longer says who does it
            tags = [ACTOR_LABELS[actor], *tags]
        parts.append(_t(x + 10, y + 14, _clip(f"{s.get('id', '')} " + " · ".join(tags), chars + 4), size=10,
                        fill=sub, anchor="start"))
        parts.append(_t(cx, y + 34, _clip(s.get("title", ""), chars - 2), size=13, weight=700, fill=tcolor))
        if s.get("detail"):
            parts.append(_t(cx, y + 53, _clip(s["detail"], chars), size=11, fill=sub))
        parts.append("</g>")
        sid = str(s.get("id", ""))
        boxes[sid] = (x, y, w)
        bottom = y + ROW_H
        if sid in loop_ids and sid != str(steps[-1].get("id")):
            overlays.append(_pill(cx, bottom + 8 + PILL_H / 2, "Yes", fill=LIME, stroke=LIME,
                                  color=NEUTRAL_1000, size=10))
            return bottom + YES_ROOM
        return bottom

    def band(y: float, phase: str) -> None:
        parts.append(f'<rect x="0" y="{y}" width="{width}" height="{BAND_H - 6}" rx="4" fill="{BG}" '
                     f'stroke="{BORDER}" stroke-width="1"/>')
        parts.append(_t(12, y + (BAND_H - 6) / 2, _clip(phase, 120), size=12, weight=700, fill=TEXT,
                        anchor="start"))

    cursor = float(TOP - 12)  # bottom of what was drawn last
    tails: list[tuple[float, float]] = []      # (cx, bottom) arrows leave from
    current_phase = None
    for kind, block in _blocks(steps):
        if kind == "step":
            s = block
            phase = s.get("phase") or ""
            top = cursor + STEP_GAP
            if phase and phase != current_phase:
                current_phase = phase
                band_y = cursor + 12
                band(band_y, phase)
                top = band_y + BAND_H - 6 + ARROW_RUN + 22
            actor = s.get("actor") if s.get("actor") in ACTOR_STYLE else "maestro"
            x = col_x.get(actor, col_x[used[0]]) + 10
            cx = x + box_w / 2
            for tx, tb in tails:  # one arrow, or several when paths join here
                link(tx, tb, cx, top, top - ARROW_RUN)
            cursor = draw_step(s, x, top, box_w, show_actor=False)
            tails = [(cx, top + ROW_H)]
            continue

        # Alternative paths, side by side, each under a labeled arrow from the deciding step.
        paths: dict[str, list[dict[str, Any]]] = block
        n = len(paths)
        lane = (width - 20) / n
        path_w = min(box_w, lane - 20) if n > 1 else box_w
        fork_y = cursor + 18
        pill_cy = fork_y + 34
        first_top = pill_cy + PILL_H / 2 + ARROW_RUN + 4
        if tails:
            fx = tails[0][0]
            overlays.append(
                f'<path d="M{fx:.1f} {fork_y - 7:.1f} L{fx + 7:.1f} {fork_y:.1f} L{fx:.1f} {fork_y + 7:.1f} '
                f'L{fx - 7:.1f} {fork_y:.1f} Z" fill="{SURFACE}" stroke="{ACCENT}" stroke-width="1.5"/>'
            )
        block_bottom = first_top
        new_tails: list[tuple[float, float]] = []
        frames: list[tuple[float, float]] = []
        for i, (label, path_steps) in enumerate(paths.items()):
            lx = 10 + i * lane
            x = lx + (lane - path_w) / 2
            cx = x + path_w / 2
            frames.append((lx, lane))
            for tx, tb in tails:
                link(tx, tb, cx, first_top, fork_y)
            overlays.append(_pill(cx, pill_cy, label, fill=ACCENT, stroke=ACCENT,
                                  color="var(--accent-contrast, #FFFFFF)"))
            py = end = prev_bottom = first_top
            for k, s in enumerate(path_steps):
                if k:
                    arrow(f"M{cx:.1f} {prev_bottom:.1f} L{cx:.1f} {py - 3:.1f}")
                end = draw_step(s, x, py, path_w, show_actor=True)
                prev_bottom = py + ROW_H
                if k + 1 < len(path_steps):
                    py = end + STEP_GAP
            new_tails.append((cx, py + ROW_H))
            block_bottom = max(block_bottom, end)
        if n > 1:
            frame_top, frame_h = fork_y + 12, block_bottom - fork_y + 4
            for lx, lw in frames:
                parts.append(f'<rect x="{lx + 4:.1f}" y="{frame_top:.1f}" width="{lw - 8:.1f}" '
                             f'height="{frame_h:.1f}" rx="10" fill="none" stroke="{BORDER}" stroke-dasharray="3 4"/>')
        tails = new_tails
        cursor = block_bottom + 16

    # Loops back: dashed arrow from the step to the earlier one through the right margin, labeled "No · ...".
    for i, s in enumerate(loops):
        src, dst = boxes.get(str(s.get("id"))), boxes.get(str(s.get("loop_to")))
        if not src or not dst:
            continue
        sx, sy, sw = src
        dx, dy, dw = dst
        gx = width + NO_ROOM + label_w + 24 + i * LOOP_LANE
        y1, y2 = sy + ROW_H / 2, dy + ROW_H / 2 - 8
        arrow(f"M{sx + sw:.1f} {y1:.1f} L{gx:.1f} {y1:.1f} L{gx:.1f} {y2:.1f} L{dx + dw + 3:.1f} {y2:.1f}",
              dashed=True)
        overlays.append(_pill(sx + sw + 24, y1, "No", fill=NO_RED, stroke=NO_RED, color=NEUTRAL_1000, size=10))
        if str(s.get("id")) in labels:
            overlays.append(_pill(width + NO_ROOM + 12 + label_w / 2, y1, labels[str(s.get("id"))], fill=SURFACE,
                                  stroke=ACCENT, color=TEXT, size=10, dashed=True))

    height = cursor + 16
    total_w = width + gutter
    summary = escape(design.get("summary") or "Maestro project")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w:.0f} {height:.0f}" width="100%" role="img" '
        f'aria-labelledby="ms-title ms-desc" class="maestro-diagram"><title id="ms-title">Maestro project'
        f'</title><desc id="ms-desc">{summary}</desc><defs><marker id="ms-arrow" viewBox="0 0 10 10" '
        f'refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path '
        f'd="M2 1L8 5L2 9" fill="none" stroke="context-stroke" stroke-width="1.5" stroke-linecap="round" '
        f'stroke-linejoin="round"/></marker></defs>' + "".join(parts) + "".join(arrows) + "".join(overlays)
        + "</svg>"
    )
    return Markup(svg)


# --- Compact vertical map (sticky panel on the run page) -------------------------

CW = 440
C_ROW = 46
C_GAP = 8
C_BAND = 24


def render_design_map(design: dict[str, Any]) -> Markup:
    """Narrow, readable version of the design: steps in order, colored by actor, one group per step."""
    steps: list[dict[str, Any]] = design.get("steps") or []
    parts: list[str] = []
    y = 6
    phase = None
    for s in steps:
        if s.get("phase") and s.get("phase") != phase:
            phase = s.get("phase")
            parts.append(_t(6, y + C_BAND / 2, _clip(str(phase), 60), size=11, weight=700, fill=MUTED,
                            anchor="start"))
            y += C_BAND
        actor = s.get("actor") if s.get("actor") in ACTOR_STYLE else "maestro"
        fill, op, stroke, tcolor = ACTOR_STYLE[actor]
        sub = MUTED if actor != "human" else NEUTRAL_1000
        meta = ACTOR_LABELS[actor]
        if actor == "human" and s.get("gate_owner"):
            meta = f"GATE · {s['gate_owner']}"
        elif actor == "dispatch":
            meta = " · ".join(x for x in (DISPATCH_LABELS.get(s.get("dispatch_workflow", ""), "Upsun Dispatch"),
                                          ("done when " + DONE_LABELS[s["done_when"]])
                                          if s.get("done_when") in DONE_LABELS else "") if x)
        if s.get("branch"):
            meta = f"{s['branch']} path · {meta}"
        if s.get("loop_to"):
            meta += f" · if not: back to {s['loop_to']}"
        parts.append(f'<g class="ms-step" data-step="{escape(str(s.get("id", "")))}">' + _tooltip(s))
        parts.append(
            f'<rect class="ms-ring" x="3" y="{y - 3}" width="{CW - 6}" height="{C_ROW + 6}" rx="10" fill="none" '
            f'stroke="{ACCENT}" stroke-width="3" visibility="hidden"/>'
        )
        parts.append(
            f'<rect x="8" y="{y}" width="{CW - 16}" height="{C_ROW}" rx="7" fill="{fill}" fill-opacity="{op}" '
            f'stroke="{stroke}" stroke-width="1"/>'
        )
        parts.append(_t(18, y + 16, _clip(f"{s.get('id', '')}  {s.get('title', '')}", 52), size=13, weight=700,
                        fill=tcolor, anchor="start"))
        parts.append(_t(18, y + 33, _clip(meta, 66), size=10.5, fill=sub, anchor="start"))
        parts.append("</g>")
        y += C_ROW + C_GAP
    height = y + 4
    return Markup(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CW} {height}" width="100%" role="img" '
        f'aria-label="Project steps" class="maestro-map">' + "".join(parts) + "</svg>"
    )
