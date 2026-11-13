from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from app.security import get_or_create_csrf_token

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
_TZ = ZoneInfo("Europe/Paris")


def markdown(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))  # raw HTML disabled: content is escaped


def format_dt(value: datetime | None) -> str:
    if not value:
        return "Never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_TZ).strftime("%b %d, %Y %H:%M")


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "Unknown"
    if seconds < 60:
        return f"{seconds} s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {sec:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def excerpt(text: str | None, length: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[: length - 1] + "…"


templates.env.filters["md"] = markdown
templates.env.filters["dt"] = format_dt
templates.env.filters["duration"] = format_duration
templates.env.filters["pretty_json"] = pretty_json
templates.env.filters["excerpt"] = excerpt


def flash(request: Request, message: str, category: str = "info", section: str | None = None) -> None:
    """Queue a message. `section` displays it inside a given card of the next page (e.g. Settings)."""
    messages = list(request.session.get("flash", []))
    messages.append({"message": message, "category": category, "section": section})
    request.session["flash"] = messages


def pop_flashes(request: Request) -> list[dict[str, str]]:
    return list(request.session.pop("flash", []))


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> HTMLResponse:
    ctx: dict[str, Any] = {
        "request": request,
        "csrf_token": get_or_create_csrf_token(request.session),
        "current_user": getattr(request.state, "user", None),
        "user_configured": getattr(request.state, "user_configured", False),
        "flashes": pop_flashes(request),
    }
    if context:
        ctx.update(context)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
