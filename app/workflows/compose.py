"""Composition du message envoyé à Claude lors de l'exécution d'un workflow."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SKILLS_INTRO = (
    "The instructions below are set by the administrator of this application. "
    "Apply them throughout the execution of the workflow that follows, at every step "
    "and up to the final result."
)


def _attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def snapshot_skills(skills: Iterable[Any]) -> list[dict[str, Any]]:
    """Fige le texte exact des skills actifs, dans leur ordre d'affichage."""
    ordered = sorted((s for s in skills if s.active), key=lambda s: (s.position, s.id))
    return [{"id": s.id, "name": s.name, "content": s.content} for s in ordered]


def compose_workflow_message(skills: list[dict[str, Any]], name: str, content: str) -> str:
    parts: list[str] = []
    if skills:
        blocks = "\n\n".join(
            f'<skill name="{_attr(str(s["name"]))}">\n{str(s["content"]).strip()}\n</skill>'
            for s in skills
        )
        parts.append(f"<instructions>\n{SKILLS_INTRO}\n\n{blocks}\n</instructions>")
    parts.append(f'<workflow name="{_attr(name)}">\n{content.strip()}\n</workflow>')
    return "\n\n".join(parts)
