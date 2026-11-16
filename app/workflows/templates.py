"""Starter workflows that hand work to Upsun Dispatch. Text in [brackets] is meant to be edited."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowTemplate:
    key: str
    name: str
    description: str
    content: str


TEMPLATES: dict[str, WorkflowTemplate] = {
    "dispatch-issue-to-code": WorkflowTemplate(
        key="dispatch-issue-to-code",
        name="Send a change to Dispatch",
        description="Write a clear issue, hand it to Upsun Dispatch Issue to Code, relay its plan to you, return the pull request.",
        content=(
            "Hand the following change to Upsun Dispatch.\n\n"
            "Change: [describe the change or bug in a few sentences]\n"
            "Where: [GitHub repository owner/name, or Linear team, or Jira project]\n\n"
            "1. Create an issue there with a clear title, the problem, the expected behavior and acceptance criteria.\n"
            "2. Hand it to Upsun Dispatch Issue to Code.\n"
            "3. Follow the issue. When Dispatch posts its plan, summarize it and ask me whether to approve, "
            "reject or revise it, then post my decision on the issue.\n"
            "4. When Dispatch opens the pull request, give me its link and a short summary."
        ),
    ),
    "dispatch-triage": WorkflowTemplate(
        key="dispatch-triage",
        name="Triage and dispatch the top bug",
        description="Find the most urgent unassigned bug, confirm it with you, then send it to Dispatch.",
        content=(
            "In Linear team [team name], find the bugs in Todo that have no assignee.\n"
            "Show me the 3 most urgent as a table (ID, title, priority, age) and ask me which one to send to "
            "Upsun Dispatch.\n"
            "Improve that issue's description if it lacks acceptance criteria, then hand it to Upsun Dispatch "
            "Issue to Code.\n"
            "Follow it, relay Dispatch's plan to me for approval, and give me the pull request link at the end."
        ),
    ),
    "dispatch-status": WorkflowTemplate(
        key="dispatch-status",
        name="Dispatch status report",
        description="Report where every issue handed to Dispatch stands, without changing anything.",
        content=(
            "In [GitHub repository owner/name, or Linear team], list the open issues that mention Upsun Dispatch "
            "or are assigned to the Dispatch app.\n"
            "For each one, read its latest comments and report as a table: issue, current stage (planning, "
            "waiting for plan approval, coding, pull request opened), pull request link if any, and what is "
            "needed from a person.\n"
            "Do not post any comment and do not change any issue."
        ),
    ),
}
