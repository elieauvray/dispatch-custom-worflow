"""What the Maestro designer knows about Upsun Dispatch and the Maestro runtime.

Kept generic on purpose: no customer-specific workflow lives here. Update it when Dispatch evolves.
"""

from __future__ import annotations

DISPATCH_KNOWLEDGE = """# Upsun Dispatch: capabilities and limits (as of November 2026)

## Built-in workflows only
- Issue to code: a tracker issue becomes a pull/merge request. Steps: gather context, plan, optional plan
  approval, code, a single critic pass (no revision after the critic), push, open the PR/MR, link-back
  comment on the issue. Branch name: upsun-dispatch/issue-<number>. Each run starts from a fresh clone
  of the default branch, so a run only sees what is merged.
- Code review: one agent, runs automatically on every PR (capped at 10 automatic reviews per PR);
  "@upsun-dispatch full review" requests a manual review and bypasses the cap. Review rules can be
  steered with REVIEW.md files in the repository.
- CVE scan: dependency vulnerability scan, manual or daily.
- There are NO custom workflows, no workflow builder and no public API to start Issue to code or to
  answer its gates. Everything else (triage, specs, multi-step chains, loops, tests on environments,
  reports, emails) must be orchestrated by Maestro.

## Triggers and protocol (driven through the tracker, like a person)
- Trackers: GitHub Issues, Jira, Linear (GitLab issues where the instance is reachable). On Jira the
  repository goes in the issue description as owner/repo. The Linear issue to repository mapping is
  not documented: flag it as a risk.
- Trigger: a separate comment whose whole content is exactly "@upsun-dispatch implement". Never in a
  title or body, never by editing a comment. Triggers posted by bot accounts are ignored. A trigger
  posted while a run is active is dropped: wait for the link-back, blocked or failed comment first.
- Corrections: Dispatch does not iterate on its own. To fix findings or failing tests, post a comment
  starting with CORRECTION listing every finding, then a fresh trigger. Cap the attempts (usually 3)
  and escalate to a person after that.
- Plan approval gate (Jira, documented inconsistently for other trackers): "plan approve",
  "plan reject", "plan revise: <feedback>" as comments. It is set per organization or repository, not
  per issue. Prefer pull request approval as the human gate when in doubt.
- Code hosts: GitHub. GitLab publishing is not guaranteed (Premium/Ultimate and an instance reachable
  from Dispatch's SaaS): plan a go/no-go test first. Dispatch is SaaS only.
- Context is bounded: keep each issue focused (about 8 files or less to change) and give it explicit
  scope, context files and acceptance criteria. Dispatch probably does not read git submodules or
  .claude/skills: copy what it needs into the repository and point to it from AGENTS.md.
- Steering files: AGENTS.md / CLAUDE.md (instructions for every run), REVIEW.md (review rules).

## Upsun Cloud
- One preview environment per pull/merge request (via the GitHub/GitLab integration, or pushed by CI
  when the code host is not reachable), cloned from the parent environment's data. Good for tests,
  end-to-end checks and human acceptance on a real URL. Production deploys on merge to the default branch.

## Maestro (the orchestrator this design is for)
- Maestro is Claude running in Claude Managed Agents. Each run gets a fresh session with: MCP connectors
  configured by the user (Linear, GitHub, Jira/Confluence), a sandbox with bash and files, and the
  orchestration prompt you write. It can be started by hand or on a schedule (cron), for example hourly.
- Runs are short-lived: a run must never wait for days. State lives in the trackers: a "ledger" comment
  edited in place on each tracked item (and/or labels), so every run can resume where the last one stopped.
- Human gates happen in the trackers and code host (PR approval, issue comments). Maestro detects them on
  its next run. Maestro can also ask the person who launched it a question in the Maestro app.
- Maestro cannot run long waits: after a trigger it checks back on its next run; within one run it may
  wait a few minutes between checks, never more than 30 minutes in total.
"""

DESIGNER_SYSTEM = """You design meta-workflows for Maestro, an orchestrator that drives Upsun Dispatch.

The user describes a process (often with stages, agents, validations, loops and tools). You turn it into
an executable design that respects what Dispatch can and cannot do today.

For every step, decide WHO does it:
- "dispatch": only what a built-in Dispatch workflow does (Issue to code, Code review, full review, CVE scan).
- "maestro": orchestration and everything Dispatch cannot do (triage, tracker updates, issue writing,
  triggers, corrections, checks, reports, relaying questions).
- "human": a decision or validation by a named role (a gate). Say where it happens (PR approval,
  issue comment, plan approval).
- "upsun": Upsun Cloud work (preview/acceptance environments, deploys).
- "external": other systems (CI, email sent by a person, a third-party tool).

Then write the orchestration prompt that Maestro will follow on every run. It must be self-contained,
in English (agents read it), precise and operational:
1. Mission and definition of done.
2. Config table: every value the user must provide, as KEY | meaning (use <PLACEHOLDER> values).
3. Tools and preflight checks (connectors, permissions, accounts; never guess, report failures).
4. Guardrails: the only human gates and who owns them; escalation rules; the exact Dispatch trigger and
   CORRECTION protocol; attempt caps; things never to do.
5. State and resumption: the ledger format (comment marker, table of items, event log) and the rule to
   read every ledger first and advance each active item as far as possible, then stop.
6. The stages, in order, each with: who does it, inputs, the exact issue/comment templates to post, the
   expected outputs, the checks, and the gate.
7. Per-run budget: stop after about 30 minutes of waiting in a run; the next scheduled run continues.
8. The end-of-run status report: a Markdown table of items with their current stage, state
   (working | waiting for Dispatch | waiting for a person | escalated | done), owner and links.

Also return: a short summary, the assumptions you made, the needs that are not possible with Dispatch
today with the alternative you chose, the tools/connectors required, the config keys, and a suggested
schedule (cron) with its reason. Keep step titles short (2 to 7 words) and details to one line.
Never invent Dispatch features beyond the knowledge below; when the description asks for one, say so in
"not_possible" and design the alternative. Write titles, summary and lists in the language of the
user's description; write the orchestration prompt in English."""

DESIGN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "phase": {"type": "string"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "actor": {"type": "string", "enum": ["maestro", "dispatch", "human", "upsun", "external"]},
                    "dispatch_workflow": {
                        "type": "string",
                        "enum": ["none", "issue_to_code", "code_review", "full_review", "cve_scan"],
                    },
                    "gate_owner": {"type": "string"},
                    "repeat": {"type": "string"},
                    "loop": {"type": "string"},
                },
                "required": ["id", "phase", "title", "detail", "actor", "dispatch_workflow",
                             "gate_owner", "repeat", "loop"],
                "additionalProperties": False,
            },
        },
        "config": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "description": {"type": "string"}},
                "required": ["key", "description"],
                "additionalProperties": False,
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "not_possible": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"need": {"type": "string"}, "alternative": {"type": "string"}},
                "required": ["need", "alternative"],
                "additionalProperties": False,
            },
        },
        "tools": {"type": "array", "items": {"type": "string"}},
        "schedule": {
            "type": "object",
            "properties": {"cron": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["cron", "reason"],
            "additionalProperties": False,
        },
        "orchestration_prompt": {"type": "string"},
    },
    "required": ["summary", "steps", "config", "assumptions", "not_possible", "tools", "schedule",
                 "orchestration_prompt"],
    "additionalProperties": False,
}
