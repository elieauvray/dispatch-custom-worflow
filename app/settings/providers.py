"""MCP connector registry (checked on 2026-11-13, see README)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    default_url: str
    token_label: str
    token_help_html: str
    url_help_html: str
    supports_static: bool = True
    supports_dcr: bool = False
    oauth_scopes: list[str] = field(default_factory=list)
    oauth_help_html: str = ""


PROVIDERS: dict[str, Provider] = {
    "linear": Provider(
        key="linear",
        label="Linear",
        default_url="https://mcp.linear.app/mcp",
        token_label="Linear personal API key",
        url_help_html=(
            "Official Linear MCP server (Streamable HTTP). Read-only variant: "
            "<code>https://mcp.linear.app/mcp/readonly</code>. "
            '<a href="https://linear.app/docs/mcp" target="_blank" rel="noopener">Documentation</a>.'
        ),
        token_help_html=(
            "In Linear, go to <strong>Settings → Account → Security &amp; Access → Personal API keys</strong> "
            '(<a href="https://linear.app/settings/account/security" target="_blank" rel="noopener">open</a>). '
            "If the option is missing, an admin must allow it in Settings → Administration → API. "
            "Recommended permissions: <em>Read</em>, <em>Create issues</em> and <em>Create comments</em> "
            "(or <em>Write</em> if Claude must edit issues), limited to the teams you need. "
            "The key is sent as <code>Authorization: Bearer</code>."
        ),
        supports_dcr=True,
        oauth_scopes=["read", "write"],
        oauth_help_html=(
            "Linear supports dynamic client registration (OAuth 2.1, RFC 7591): "
            "click <strong>Register automatically</strong>."
        ),
    ),
    "github": Provider(
        key="github",
        label="GitHub",
        default_url="https://api.githubcopilot.com/mcp/",
        token_label="Fine-grained personal access token",
        url_help_html=(
            "Official GitHub remote MCP server. "
            '<a href="https://github.com/github/github-mcp-server" target="_blank" rel="noopener">Documentation</a>. '
            "In a Copilot Business or Enterprise organization, the <em>MCP servers in Copilot</em> policy must be enabled."
        ),
        token_help_html=(
            "On github.com, go to <strong>Settings → Developer settings → Personal access tokens → "
            'Fine-grained tokens → Generate new token</strong> (<a href="https://github.com/settings/personal-access-tokens/new" '
            'target="_blank" rel="noopener">open</a>). Choose <em>Only select repositories</em>. '
            "Minimum permissions: <em>Contents</em>, <em>Issues</em> and <em>Pull requests</em> set to read "
            "(read and write if Claude must create issues or pull requests). "
            "<em>Metadata: Read</em> is added automatically."
        ),
        supports_dcr=False,
        oauth_scopes=["repo", "read:org"],
        oauth_help_html=(
            "GitHub does not support dynamic client registration. Create an OAuth App "
            '(<a href="https://github.com/settings/developers" target="_blank" rel="noopener">Settings → Developer settings → OAuth Apps</a>) '
            "with the callback URL shown here, then enter its client ID and client secret."
        ),
    ),
    "atlassian": Provider(
        key="atlassian",
        label="Jira / Atlassian",
        default_url="https://mcp.atlassian.com/v2/mcp",
        token_label="Atlassian service account API key",
        url_help_html=(
            "Atlassian Rovo MCP server (Jira, Confluence, Compass). "
            '<a href="https://support.atlassian.com/atlassian-rovo-mcp-server/docs/getting-started-with-the-atlassian-remote-mcp-server/" '
            'target="_blank" rel="noopener">Documentation</a>. The older <code>/v1/mcp</code> endpoint still works.'
        ),
        token_help_html=(
            "OAuth is the default and recommended mode (<strong>Sign in with Jira / Atlassian</strong> button). "
            "For a static token, only a <strong>service account API key</strong> (sent as Bearer) works: "
            "an admin creates it in <strong>admin.atlassian.com → Directory → Service accounts</strong> "
            "and must enable <strong>Connectors → Atlassian MCP server → Authentication → API token</strong>. "
            "Personal API tokens (email and token over Basic auth) are not supported by the Anthropic vault."
        ),
        supports_dcr=True,
        oauth_scopes=["offline_access"],
        oauth_help_html=(
            "Atlassian supports dynamic client registration: click <strong>Register automatically</strong>. "
            "Your Atlassian admin may need to allow this app's domain under Connectors."
        ),
    ),
}


def get_provider(key: str) -> Provider | None:
    return PROVIDERS.get(key)
