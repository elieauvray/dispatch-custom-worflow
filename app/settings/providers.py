"""Registre des connecteurs MCP (vérifié le 2026-11-13, voir README)."""

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
        token_label="Clé API personnelle Linear",
        url_help_html=(
            "Serveur MCP officiel de Linear (Streamable HTTP). Variante lecture seule : "
            "<code>https://mcp.linear.app/mcp/readonly</code>. "
            '<a href="https://linear.app/docs/mcp" target="_blank" rel="noopener">Documentation</a>.'
        ),
        token_help_html=(
            "Linear → <strong>Settings → Account → Security &amp; Access → Personal API keys</strong> "
            '(<a href="https://linear.app/settings/account/security" target="_blank" rel="noopener">ouvrir</a>). '
            "Si l'option n'apparaît pas, un admin doit l'autoriser dans Settings → Administration → API. "
            "Permissions conseillées : <em>Read</em> + <em>Create issues</em> + <em>Create comments</em> "
            "(ou <em>Write</em> si Claude doit modifier des tickets), limitées aux équipes utiles. "
            "La clé est envoyée en <code>Authorization: Bearer</code>."
        ),
        supports_dcr=True,
        oauth_scopes=["read", "write"],
        oauth_help_html=(
            "Linear accepte l'enregistrement dynamique de client (OAuth 2.1, RFC 7591) : "
            "cliquez sur « Enregistrer automatiquement »."
        ),
    ),
    "github": Provider(
        key="github",
        label="GitHub",
        default_url="https://api.githubcopilot.com/mcp/",
        token_label="Personal Access Token (fine-grained)",
        url_help_html=(
            "Serveur MCP distant officiel de GitHub. "
            '<a href="https://github.com/github/github-mcp-server" target="_blank" rel="noopener">Documentation</a>. '
            "Dans une organisation Copilot Business/Enterprise, la politique « MCP servers in Copilot » doit être activée."
        ),
        token_help_html=(
            "github.com → <strong>Settings → Developer settings → Personal access tokens → Fine-grained tokens → "
            'Generate new token</strong> (<a href="https://github.com/settings/personal-access-tokens/new" '
            'target="_blank" rel="noopener">ouvrir</a>). Choisissez « Only select repositories ». '
            "Permissions minimales : <em>Contents</em>, <em>Issues</em>, <em>Pull requests</em> en lecture "
            "(passez en lecture/écriture si Claude doit créer des issues ou des PR). "
            "<em>Metadata: Read</em> est ajouté automatiquement."
        ),
        supports_dcr=False,
        oauth_scopes=["repo", "read:org"],
        oauth_help_html=(
            "GitHub ne propose pas l'enregistrement dynamique : créez une OAuth App "
            '(<a href="https://github.com/settings/developers" target="_blank" rel="noopener">Settings → Developer settings → OAuth Apps</a>) '
            "avec l'URL de callback indiquée, puis saisissez son client_id / client_secret."
        ),
    ),
    "atlassian": Provider(
        key="atlassian",
        label="Jira / Atlassian",
        default_url="https://mcp.atlassian.com/v2/mcp",
        token_label="Clé API de compte de service Atlassian",
        url_help_html=(
            "Serveur MCP Atlassian Rovo (Jira, Confluence, Compass). "
            '<a href="https://support.atlassian.com/atlassian-rovo-mcp-server/docs/getting-started-with-the-atlassian-remote-mcp-server/" '
            'target="_blank" rel="noopener">Documentation</a>. L\'ancien endpoint <code>/v1/mcp</code> fonctionne encore.'
        ),
        token_help_html=(
            "OAuth est le mode par défaut et recommandé (bouton « Se connecter avec Jira / Atlassian »). "
            "En mode jeton fixe, seule une <strong>clé API de compte de service</strong> (envoyée en Bearer) est "
            "compatible : un admin la crée dans <strong>admin.atlassian.com → Directory → Service accounts</strong> "
            "et doit activer <strong>Connectors → Atlassian MCP server → Authentication → API token</strong>. "
            "Les jetons API personnels (email + jeton en Basic) ne sont pas supportés par le vault Anthropic."
        ),
        supports_dcr=True,
        oauth_scopes=["offline_access"],
        oauth_help_html=(
            "Atlassian accepte l'enregistrement dynamique de client : cliquez sur « Enregistrer automatiquement ». "
            "L'admin Atlassian peut devoir autoriser le domaine de cette application dans Connectors."
        ),
    ),
}


def get_provider(key: str) -> Provider | None:
    return PROVIDERS.get(key)
