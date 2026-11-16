"""Small line icons for navigation and buttons (24x24 grid, stroke = currentColor)."""

from __future__ import annotations

from markupsafe import Markup

ICONS: dict[str, str] = {
    "create": '<path d="M12 5v14M5 12h14"/>',
    "manage": '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01" stroke-width="3"/>',
    "settings": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    "instructions": '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 13h7M9 17h5"/>',
    "users": '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c.4-3.4 3-5.5 6-5.5s5.6 2.1 6 5.5"/>'
             '<path d="M16 4.8a3.2 3.2 0 0 1 0 6.3M21 20c-.3-2.4-1.6-4.2-3.6-5"/>',
    "logout": '<path d="M10 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4"/><path d="M15 8l4 4-4 4M19 12H9"/>',
    "login": '<path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4"/><path d="M10 8l4 4-4 4M14 12H4"/>',
    "run": '<path d="M7 5l12 7-12 7z" fill="currentColor"/>',
    "runs": '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    "delete": '<path d="M4 7h16M9 7V4h6v3M6.5 7l1 13h9l1-13M10 11v6M14 11v6"/>',
    "download": '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    "refresh": '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4v5h-5"/>',
    "schedule": '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
    "pause": '<path d="M9 5v14M15 5v14"/>',
    "save": '<path d="M5 12.5l4.5 4.5L19 7"/>',
    "send": '<path d="M4 12l16-8-6 16-2.5-6.5z"/><path d="M11.5 13.5L20 4"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "test": '<path d="M13 3L5 14h6l-1 7 8-11h-6z"/>',
    "disconnect": '<path d="M9 7l-2.5-2.5M15 17l2.5 2.5M7 14l-2 2a3 3 0 0 0 4 4l2-2M17 10l2-2a3 3 0 0 0-4-4l-2 2"/>',
    "webhook": '<path d="M12 4v6M8 14H4M20 14h-4"/><circle cx="12" cy="14" r="3"/>',
    "design": '<path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M18 15l.8 2.2L21 18l-2.2.8L18 21l-.8-2.2L15 18l2.2-.8z"/>',
    "back": '<path d="M15 6l-6 6 6 6"/>',
    "allow": '<path d="M5 12.5l4.5 4.5L19 7"/>',
    "deny": '<path d="M6 6l12 12M18 6L6 18"/>',
    "reset": '<path d="M4 12a8 8 0 1 0 2.4-5.7"/><path d="M4 4v5h5"/>',
    "continue": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "history": '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
}


def icon(name: str) -> Markup:
    body = ICONS.get(name)
    if body is None:
        return Markup("")
    return Markup(
        '<svg class="icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" '
        f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">{body}</svg>'
    )
