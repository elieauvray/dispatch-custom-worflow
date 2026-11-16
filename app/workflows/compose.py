"""Composition of the message sent to Claude when a workflow runs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

# System instructions are stored in the `skills` table (historical name). They are set by the admin,
# apply to every workflow, and take precedence over what a workflow description or prompt says.
INSTRUCTIONS_INTRO = (
    "System instructions set by the administrator of this application. They apply to every workflow, "
    "at every step and up to the final result, and they take precedence over the workflow that follows: "
    "where the workflow contradicts them, follow the system instructions. They never override Maestro's "
    "built-in rules for driving Upsun Dispatch (hands off during Dispatch runs, separate trigger comment, "
    "connected tools only)."
)


def _attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def snapshot_skills(skills: Iterable[Any]) -> list[dict[str, Any]]:
    """Freezes the exact text of the active system instructions, in their display order."""
    ordered = sorted((s for s in skills if s.active), key=lambda s: (s.position, s.id))
    return [{"id": s.id, "name": s.name, "content": s.content} for s in ordered]


def instructions_hash(skills: list[dict[str, Any]]) -> str:
    """Fingerprint of the active system instructions ("" when there are none)."""
    if not skills:
        return ""
    raw = json.dumps([[s["name"], s["content"]] for s in skills], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def instructions_block(skills: list[dict[str, Any]]) -> str:
    if not skills:
        return ""
    blocks = "\n\n".join(
        f'<instruction name="{_attr(str(s["name"]))}">\n{str(s["content"]).strip()}\n</instruction>'
        for s in skills
    )
    return f"<system_instructions>\n{INSTRUCTIONS_INTRO}\n\n{blocks}\n</system_instructions>"


def compose_workflow_message(skills: list[dict[str, Any]], name: str, content: str) -> str:
    parts: list[str] = []
    if skills:
        parts.append(instructions_block(skills))
    parts.append(f'<workflow name="{_attr(name)}">\n{content.strip()}\n</workflow>')
    return "\n\n".join(parts)
