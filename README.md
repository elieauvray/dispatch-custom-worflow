# Maestro: meta-workflow orchestrator for Upsun Dispatch

A web application (FastAPI, Jinja2, HTMX, SSE, PostgreSQL) where a team **describes a workflow**, lets
Maestro design it, **reviews** the result, then **runs** it on Claude Managed Agents. Maestro works with your
tools (Linear, GitHub, Jira/Confluence) through **remote MCP servers** and hands coding work to
**Upsun Dispatch**. There is a single kind of workflow: the Maestro workflow, saved in *Custom workflows*.
The application is ready to deploy on **Upsun**. The interface is in American English, in line with the Upsun brand guidelines, and so is this documentation.

## Maestro at a glance

Maestro lets you describe a complete process (steps, approvals, loops, tools) and turns it into an
executable **meta-workflow** that orchestrates Upsun Dispatch:

1. **Description → design.** On the home page (*New workflow*, "Describe your workflow"), you paste the description of the
   process. Maestro (Claude, Messages API, structured output, `high` effort) generates:
   - the **orchestration prompt** followed on every run (mission, config, preflight, guardrails, ledger, steps,
     ticket and comment templates, end-of-run report);
   - the **steps and their actor**: Maestro, Upsun Dispatch (Issue to code, Code review, full review, CVE
     scan), people (gates) or Upsun Cloud;
   - the **assumptions**, what is **not possible with Dispatch** and the chosen alternative, the required
     tools, the config parameters and a suggested schedule.

   The designer relies on a Dispatch knowledge base (`app/maestro/knowledge.py`), with no built-in workflow
   template.
   **Required tools.** Before any generation, Maestro checks (with a short call) the tools it will have to use
   itself. If the description involves a connector that is not configured (for example Linear) or a tool with no
   connector in Maestro (GitLab, Slack, sending emails, and so on), **no prompt is created**: the workflow
   switches to "blocked" with the message "connect it in Settings, then try again". If a required connector
   is removed later, Run now and the schedule are disabled.
   **Clarify.** After the tool check and before the design, Maestro asks at most 6 multiple-choice questions
   (recommended option first, plus "Other") when a decision materially changes the workflow or is needed to
   fit what Dispatch can do (tracker, who validates what, one PR per issue, completion signal, first run
   scope). The workflow shows "needs your answers"; the answers are passed to the designer as final
   decisions, shown with the design, and never asked again. A clear description goes straight to the design.
2. **Visualization.** A swimlane diagram (one column per actor, phases as bands, GATE badges,
   Dispatch workflow type, repetitions and loops), exportable as SVG.
3. **Launch.** *Run now* starts a run that you follow live. *Schedule Maestro* creates a scheduled
   Claude Managed Agents deployment (cron, time zone, budget per run): each tick creates a session that the worker imports
   and follows like the other runs (transcript, approvals, questions). Pause, resume and automatic
   update when the prompt, the config or the system instructions change; deleting the workflow archives the
   deployment.

**Following a run on its diagram.** Each run keeps a frozen copy of its workflow's design. On the run page,
a compact map of the steps stays visible while you scroll the transcript (left column, or a band at the top on
small screens). Maestro starts each message that moves to a step with a marker such as `[step: S2]` (hidden
in the transcript), so every message and tool call is attached to a step. At the bottom of the transcript the
map highlights the current step (past steps are dimmed); when you scroll back through the history, it
highlights the step of the messages you are reading.

**Triggering Dispatch, hands off.** Maestro does not call any Dispatch API (there is none
for Issue to code): it creates the ticket and posts the `@upsun-dispatch implement` comment through the
MCP connectors (GitHub, Linear, Jira), with **the user's credentials** (Dispatch ignores bots).
The Dispatch workflow then runs **exactly as Dispatch intends**: its human approvals
(plan approval, PR review and approval, merge) are done by people, in the tracker and
on the PR. Maestro never touches them (no comment, no fix, no retry, no decision, no merge). On
each run, it rereads the state of the tickets it started and compares it with the step's **completion signal** (PR merged,
PR approved, issue closed or PR opened); once it is reached, it moves on to the next step (for example
another Issue to code). A failed Dispatch run or a PR closed without merge is reported, not repaired. The state
lives in a "ledger" (an edited comment) on each tracked item.

## Contents

1. [Architecture](#architecture)
2. [Local setup](#local-setup)
3. [Deploying on Upsun](#deploying-on-upsun)
4. [Connector configuration](#connector-configuration)
5. [Security](#security)
6. [Tests](#tests)
7. [Known limitations](#known-limitations)

## Architecture

```
Browser ──HTMX/SSE──▶ web (FastAPI, uvicorn) ──▶ PostgreSQL ◀── worker (python -m app.worker)
                               │  replies, approvals,                       │  creates the session, sends the message,
                               │  cancellation                              │  reads the event stream,
                               ▼                                            ▼  stores run_events
                         Anthropic API (Managed Agents beta) ◀───────────────┘
```

| Module | Role |
|---|---|
| `app/auth` | First launch (`/setup`), login, logout, rate limiting of login attempts |
| `app/settings` | API key, model, MCP connectors, OAuth (discovery, DCR, PKCE), resource synchronization |
| `app/anthropic_client.py` | **All** the Managed Agents logic: agent, environment, vault, credentials, sessions, events |
| `app/workflows` | Workflow list, edit, delete, run, history; message composition (system instructions, then content) |
| `app/runs` | Run details, SSE stream, replies, tool approvals, cancellation, state machine |
| `app/worker.py` | Upsun worker: drives the sessions, resumes after a restart (claim + heartbeat) |
| `app/admin` | Users (creation, deletion, password reset) and System instructions |
| `app/webhooks.py` | `POST /webhooks/anthropic` (verified signature) for `vault_credential.refresh_failed` |

### Managed Agents resources per user

- **Agent**: model, system prompt, `mcp_servers` and one `mcp_toolset` per configured connector. No secrets. The built-in toolset (`agent_toolset_20260401`, set to `auto`) stays enabled, with `web_search` and `web_fetch` disabled.
- **Environment**: `cloud`, `limited` network with `allow_mcp_servers: true`.
- **Vault**: one per user. It holds the `static_bearer` or `mcp_oauth` credentials, bound to the exact MCP URL.
- Synchronization is **idempotent**. The fingerprint (SHA-256) of the agent configuration is stored, and the agent is only updated (new version) when it changes. IDs that have disappeared on the Anthropic side are recreated.

### Run lifecycle

1. The web app creates a `workflow_run` with the `running` status (Run now), or the worker imports a session fired by the schedule or a GitHub wake-up.
2. The worker claims it, creates the session (`agent`, `environment_id`, `vault_ids`), opens the stream, rereads the history (deduplication by event id), then sends the `user.message`.
3. Each event is stored in `run_events` and updates the status:
   - `session.status_idle` + `requires_action` gives **waiting for approval**. The blocking `event_ids` are stored.
   - `session.status_idle` + `end_turn` gives **waiting for your reply** if Claude's last message ends with a question, otherwise **completed**.
   - `retries_exhausted` gives **failed**. `session.error` is displayed; an `mcp_authentication_failed_error` error switches the connector to "Error".
4. The browser follows live through `/runs/{id}/stream` (SSE). The stream reads the database with short polling (1 s), after first displaying the events already stored.
5. Replies (`user.message`) and approvals (`user.tool_confirmation` allow/deny + `deny_message`) go from the web app directly to the API. The run is then handed back to the worker.

### Predicted plan (diagram)

Each run and each workflow shows a **Planned workflow** diagram: the main steps that Claude
expects to carry out, inferred from the request alone, before any execution.

- **Generation**: on first display, Maestro sends the request (the active system instructions followed by the
  workflow content) and the list of connectors to Claude (Messages API, model chosen in Settings, structured
  JSON output, `low` effort). One call per plan, billed to the user's key.
- **Interventions computed by the app**, never guessed by Claude: a call to a connector set to
  "Always ask" shows "Your approval", a question shows "Your reply", and a service that is not connected
  shows "Connector missing".
- **Cache**: a run's plan is frozen. A workflow's plan is recomputed when its content, the active system instructions,
  the connectors or the model change. A run started from a workflow reuses its plan.
- **Export**: "Download SVG" button; the file keeps the Upsun colors outside the application.
- The plan is a forecast: the transcript shows what actually happened.

### Workflow diagrams

On **Describe your workflow** you can give the process as a diagram instead of, or in addition to,
text: paste an image into the description field (Ctrl+V or Cmd+V), drop a file, or choose one
(PNG, JPEG, GIF or WebP up to 3.75 MB, or a PDF up to 10 MB). The file type is checked from its
content. The diagram is stored with the workflow (`workflows.diagram_data`) and sent to Claude with
the text for the tool check, the Clarify step and the designer, including when you regenerate the
design. Only its owner can open it (`/workflows/{id}/diagram`).

### System instructions

System instructions (admin menu **System instructions**, read-only for other users at `/instructions`)
are rules that apply to every workflow, for every user. They are stored in the `skills` table
(historical name).

- **They take precedence** over a workflow description. The tool check, the Clarify step and the
  designer all receive them: Clarify never asks what they already decide, the tool check counts the
  tools they require, and the designer rewrites the workflow to comply and lists each change under
  **Adjusted to system instructions** on the design page.
- **They are sent at the start of every run**, with the same precedence rule, and frozen in
  `workflow_runs.skills_snapshot` (shown as "System instructions applied" on the run page).
- **When an admin changes them**, existing designs are not regenerated automatically. Each design
  stores a fingerprint of the instructions it was generated with (`workflows.instructions_hash`);
  when it no longer matches, the workflow shows **instructions changed** and its design page offers
  **Regenerate the design**. Runs apply the new instructions right away.
- **Hierarchy**: Maestro's built-in Dispatch rules (hands off during Dispatch runs, separate trigger
  comment, connected tools only), then the system instructions, then the workflow description.

### Message sent for a workflow

```xml
<system_instructions>
System instructions set by the administrator of this application. They apply to every workflow, ... and they take precedence over the workflow that follows ...
<instruction name="INSTRUCTION NAME">INSTRUCTION TEXT</instruction>
</system_instructions>

<workflow name="WORKFLOW NAME">
WORKFLOW CONTENT
</workflow>
```

## Local setup

Prerequisites: Python 3.12 or later (3.14 recommended, as on Upsun) and a PostgreSQL database. No Docker.
Two options for the database:

- **Option A, recommended**: use the PostgreSQL service declared in `.upsun/config.yaml`, through an SSH
  tunnel opened by the Upsun CLI. The project must already have been deployed once. Preferably use a
  development environment (an Upsun branch), not production.

  ```bash
  upsun tunnel:single --relationship database
  ```

  The command prints a `pgsql://<user>:<password>@127.0.0.1:<port>/main` URL. Copy it as is into `DATABASE_URL`
  (the `pgsql://` prefix is accepted) and leave the terminal open.
- **Option B**: any PostgreSQL 14 to 18 installed locally, for example
  `postgresql://user:pass@localhost:5432/dispatch`.

```bash
python3.14 -m venv .venv && . .venv/bin/activate
```

```bash
pip install -r requirements-dev.txt
```

```bash
cp .env.example .env
```

Fill in `APP_SECRET_KEY`, `ENCRYPTION_KEY` and `DATABASE_URL` in `.env` (the commands to generate them are
given in the file), then load the variables and run the migrations:

```bash
set -a; . ./.env; set +a
```

```bash
alembic upgrade head
```

Start the web app and the worker in two terminals (after `set -a; . ./.env; set +a` in each):

```bash
uvicorn app.asgi:application --reload --port 8000
```

```bash
python -m app.worker
```

Open http://localhost:8000. On first launch, you are redirected to `/setup` to create the admin.

> Locally, `COOKIE_SECURE=false` is required over HTTP. On Upsun (HTTPS), it is `true`.

## Deploying on Upsun

Upsun builds and deploys the application from **`.upsun/config.yaml`**. There is no Dockerfile and no image: the application, the worker and the PostgreSQL service are all declared in this file.

| Item | Configuration |
|---|---|
| Application | `python:3.14`, dependencies installed by `pip install -r requirements.txt` (`build` hook) |
| Web | `python -m uvicorn app.asgi:application --port $PORT`; `/static` served directly |
| `runs` worker | `python -m app.worker` (same code, different command; inherits the database relationship) |
| Migrations | `python -m alembic upgrade head` in the `deploy` hook (runs on the web container only) |
| PostgreSQL | `database` service of type `postgresql:18`, declared under `services:`, linked to the app by `relationships: database:` |
| Database connection | built by the app from `PLATFORM_RELATIONSHIPS` (`database` relationship), with an encoded password |
| Route | `https://{default}/` to `app`, with a redirect from `www` |

### Steps

```bash
upsun project:create
```

Then define the **sensitive** variables at the project level (one command per variable), before the
first push:

```bash
upsun variable:create --level project --name env:APP_SECRET_KEY --value "$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" --sensitive true --visible-build false
```

```bash
upsun variable:create --level project --name env:ENCRYPTION_KEY --value "$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" --sensitive true --visible-build false
```

Optional, for the Anthropic webhook:

```bash
upsun variable:create --level project --name env:ANTHROPIC_WEBHOOK_SECRET --value "whsec_..." --sensitive true --visible-build false
```

Then deploy:

```bash
git push upsun main
```

| Variable | Required | Role |
|---|---|---|
| `APP_SECRET_KEY` | yes | Session cookie signature (32 characters minimum) |
| `ENCRYPTION_KEY` | yes | Fernet key for encryption at rest (Anthropic API key, OAuth secrets). **Do not change it**: encrypted data would become unreadable |
| `ANTHROPIC_WEBHOOK_SECRET` | no | Webhook `whsec_xxxx` secret. Without it, `/webhooks/anthropic` returns 404 |
| `PUBLIC_BASE_URL` | no | Derived from the main Upsun route. Used to build the OAuth callback URLs |

### Troubleshooting: 502 Bad Gateway

A 502 means the web process is not responding. Check the logs:

```bash
upsun logs app --lines 100
```

```bash
upsun logs deploy --lines 100
```

- **Missing variables**: if `APP_SECRET_KEY` or `ENCRYPTION_KEY` is missing or invalid, the app does not crash.
  It returns **503** with a page that lists the commands to run. Create the variables, then run
  `upsun redeploy`. Variables created after a push only take effect on the next deployment.
- **Migrations**: the `deploy` hook runs `python -m alembic upgrade head`. An error at this step shows up in
  `upsun logs deploy`.

### Anthropic webhook (optional)

In the Claude Console, open **Manage → Webhooks** and add `https://<your-domain>/webhooks/anthropic`,
subscribed to `vault_credential.refresh_failed`. The affected connector then switches to "Error" with the message
"Reconnect needed".

## Connector configuration

Each user configures their connectors in **Settings**. Tokens are sent **directly to the
Anthropic vault**. The database only keeps the type, the URL, the credential ID, the last 4 characters and the
date. Before saving, the application checks the token with an MCP `initialize` handshake.
The "Test" button then checks that the credential is present in the vault (and validates the OAuth token through
`mcp_oauth_validate`), and that the MCP server responds.

Information checked on 11/13/2026:

| Service | MCP URL | Static token (`static_bearer`) | OAuth |
|---|---|---|---|
| **Linear** | `https://mcp.linear.app/mcp` | Personal API key: Settings → Account → Security & Access → Personal API keys. Recommended permissions: Read + Create issues + Create comments, limited to the relevant teams | OAuth 2.1 with dynamic client registration (DCR) |
| **GitHub** | `https://api.githubcopilot.com/mcp/` | Fine-grained PAT: Settings → Developer settings → Personal access tokens → Fine-grained tokens. Permissions: Contents, Issues, Pull requests (read, or write if needed) | No DCR: the admin creates a GitHub OAuth App and enters its client_id / client_secret |
| **Atlassian** (Jira, Confluence) | `https://mcp.atlassian.com/v2/mcp` | Only a **service account API key** (Bearer). The Atlassian admin must enable *Connectors → Atlassian MCP server → Authentication → API token* | **Recommended.** OAuth 2.1 with DCR (`offline_access` to get the refresh token) |

### OAuth mode

1. The admin opens **Settings → OAuth apps**:
   - for **Linear** or **Atlassian**, they click "Register automatically" (RFC 7591 dynamic registration);
   - for **GitHub**, they enter the client_id / client_secret of an OAuth App whose callback URL is the one shown (`https://<domain>/oauth/github/callback`).
2. Users then see "Sign in with" followed by the service name. The application discovers the authorization server (RFC 9728 / RFC 8414), uses PKCE S256 and the `resource` parameter, then exchanges the code.
3. It creates an `mcp_oauth` credential with a `refresh` block (`token_endpoint`, `client_id`, `token_endpoint_auth`). **Anthropic then refreshes the token** automatically.

### Tool permissions

For each connector, you choose between "Always ask" (`always_ask`, the default) and
"Always allow" (`always_allow`). This choice is applied to the `default_config.permission_policy` of the
`mcp_toolset`. Changing it creates a new version of the agent.

## Security

- Passwords hashed with **argon2** (10 characters minimum). A constant response time is kept when the login is unknown.
- Signed session cookie, `HttpOnly`, `Secure` and `SameSite=Lax`, valid for 12 h. It is invalidated if the password is reset.
- **CSRF**: per-session token, checked on every POST (hidden field or `X-CSRF-Token` header for HTMX).
- **Login rate limiting**: 5 failures per login, or 20 per IP, over 15 minutes.
- The Anthropic API key is **encrypted at rest** (Fernet) and never shown again: only the last 4 characters are visible. Admin OAuth secrets are encrypted the same way.
- No secret is ever logged. The `httpx` and `anthropic` loggers are set to WARNING.
- Isolation: a user can only access their own workflows and runs (404 otherwise). The Users page and editing System instructions are restricted to the admin.
- `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy` headers.

## Tests

```bash
pytest -q
```

The Anthropic API is replaced by a fake client (`tests/conftest.py`). The tests cover:

- first launch and admin creation;
- cookies, CSRF, login rate limiting and logout;
- permissions (admin pages, other users' workflows and runs);
- API key encryption and synchronization idempotence;
- MCP server declaration and permission policies;
- message composition (active system instructions, in order, before the content; frozen snapshot); system instructions passed to the tool check, Clarify and the designer; outdated designs flagged;
- handling of `requires_action` (approval and denial with a reason), `end_turn` (question or completed), cancellation, resumption by the worker and the SSE stream.

To run the same tests on PostgreSQL (dedicated database, emptied for each test, never the Upsun one):

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/dispatch_test pytest -q
```

## Known limitations

- **Question detection**: heuristic, the last line of Claude's message must end with "?". In any case, the user can continue the conversation.
- **Atlassian with a static token**: personal API tokens (Basic `email:token`) are not compatible with `static_bearer`. Use OAuth or a service account key.
- **The "Test" button** cannot redo the authenticated handshake for a static token, since the token is not kept. It checks the credential on the vault side and that the server is reachable. The full handshake happens when saving.
- **SSE through polling** of the database (1 s) rather than `LISTEN/NOTIFY`: it is simple and robust behind the Upsun proxy, with a slight latency.
- **Anthropic workspace**: vaults are shared across the workspace. Use one API key per dedicated workspace.
- **Vault recreation** (API key from another workspace, archived vault): static tokens must be entered again, and the affected connectors switch to "Error".
- Files produced by the agent in the sandbox (`/mnt/session/outputs`) are not displayed.
