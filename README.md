# Dispatch : Claude Managed Agents + MCP + Custom Workflows

Application web (FastAPI, Jinja2, HTMX, SSE, PostgreSQL) qui permet à une équipe d'envoyer des prompts à
Claude via **Claude Managed Agents**. Claude accède à vos outils (Linear, GitHub, Jira/Confluence) par des
serveurs **MCP distants**. Les prompts peuvent être enregistrés comme **Custom Workflows** réutilisables.
L'application est prête à être déployée sur **Upsun**.

## Sommaire

1. [Architecture](#architecture)
2. [Installation locale](#installation-locale)
3. [Déploiement sur Upsun](#déploiement-sur-upsun)
4. [Configuration des connecteurs](#configuration-des-connecteurs)
5. [Sécurité](#sécurité)
6. [Tests](#tests)
7. [Limites connues](#limites-connues)

## Architecture

```
Navigateur ──HTMX/SSE──▶ web (FastAPI, uvicorn) ──▶ PostgreSQL ◀── worker (python -m app.worker)
                               │  réponses, validations,                    │  crée la session, envoie le message,
                               │  annulation                                │  lit le flux d'événements,
                               ▼                                            ▼  enregistre run_events
                         API Anthropic (Managed Agents beta) ◀───────────────┘
```

| Module | Rôle |
|---|---|
| `app/auth` | Premier lancement (`/setup`), connexion, déconnexion, limitation des tentatives |
| `app/settings` | Clé API, modèle, connecteurs MCP, OAuth (découverte, DCR, PKCE), synchronisation des ressources |
| `app/anthropic_client.py` | **Toute** la logique Managed Agents : agent, environnement, vault, credentials, sessions, événements |
| `app/workflows` | Page Prompt, CRUD des workflows, composition du message (skills puis contenu) |
| `app/runs` | Détail d'une exécution, flux SSE, réponses, validations d'outils, annulation, machine à états |
| `app/worker.py` | Worker Upsun : pilote les sessions, reprend après redémarrage (réservation + heartbeat) |
| `app/admin` | Utilisateurs (création, suppression, réinitialisation du mot de passe) et Skills |
| `app/webhooks.py` | `POST /webhooks/anthropic` (signature vérifiée) pour `vault_credential.refresh_failed` |

### Ressources Managed Agents par utilisateur

- **Agent** : modèle, system prompt, `mcp_servers` et un `mcp_toolset` par connecteur configuré. Aucun secret. Le toolset intégré (`agent_toolset_20260401`, en `auto`) reste actif, avec `web_search` et `web_fetch` désactivés.
- **Environment** : `cloud`, réseau `limited` avec `allow_mcp_servers: true`.
- **Vault** : un par utilisateur. Il contient les credentials `static_bearer` ou `mcp_oauth`, rattachés à l'URL MCP exacte.
- La synchronisation est **idempotente**. L'empreinte (SHA-256) de la configuration de l'agent est stockée, et l'agent n'est mis à jour (nouvelle version) que si elle change. Les IDs disparus côté Anthropic sont recréés.

### Cycle d'une exécution

1. Le web crée un `workflow_run` au statut `en cours`. Les prompts libres ont `workflow_id = NULL`.
2. Le worker le réserve, crée la session (`agent`, `environment_id`, `vault_ids`), ouvre le flux, relit l'historique (dédoublonnage par id d'événement), puis envoie le `user.message`.
3. Chaque événement est enregistré dans `run_events` et met à jour le statut :
   - `session.status_idle` + `requires_action` donne **en attente de validation**. Les `event_ids` bloquants sont mémorisés.
   - `session.status_idle` + `end_turn` donne **en attente de réponse** si le dernier message de Claude se termine par une question, sinon **terminé**.
   - `retries_exhausted` donne **échoué**. `session.error` est affiché ; une erreur `mcp_authentication_failed_error` passe le connecteur en « Erreur ».
4. Le navigateur suit en direct via `/runs/{id}/stream` (SSE). Le flux lit la base par polling court (1 s), après un premier affichage des événements déjà stockés.
5. Les réponses (`user.message`) et les validations (`user.tool_confirmation` allow/deny + `deny_message`) partent du web directement vers l'API. L'exécution repasse alors au worker.

### Message envoyé pour un workflow

```xml
<instructions>
Les instructions ci-dessous sont définies par l'administrateur… Applique-les pendant toute l'exécution…
<skill name="…">…</skill>
</instructions>

<workflow name="…">
…contenu du workflow…
</workflow>
```

Le texte exact des skills actifs est figé dans `workflow_runs.skills_snapshot` au lancement.

## Installation locale

Prérequis : Python 3.12 ou plus (3.14 recommandé, comme sur Upsun) et une base PostgreSQL. Pas de Docker.
Deux options pour la base :

- **Option A, recommandée** : utiliser le service PostgreSQL déclaré dans `.upsun/config.yaml`, via un tunnel
  SSH ouvert par le CLI Upsun. Le projet doit déjà avoir été déployé une fois. Utilisez de préférence un
  environnement de développement (une branche Upsun), pas la production.

  ```bash
  upsun tunnel:single --relationship database
  ```

  La commande affiche une URL `pgsql://…@127.0.0.1:<port>/main`. Copiez-la telle quelle dans `DATABASE_URL`
  (le préfixe `pgsql://` est accepté) et laissez le terminal ouvert.
- **Option B** : n'importe quel PostgreSQL 14 à 18 installé localement, par exemple
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

Remplissez `APP_SECRET_KEY`, `ENCRYPTION_KEY` et `DATABASE_URL` dans `.env` (les commandes de génération sont
indiquées dans le fichier), puis chargez les variables et lancez les migrations :

```bash
set -a; . ./.env; set +a
```

```bash
alembic upgrade head
```

Lancez le web et le worker dans deux terminaux (après `set -a; . ./.env; set +a` dans chacun) :

```bash
uvicorn app.asgi:application --reload --port 8000
```

```bash
python -m app.worker
```

Ouvrez http://localhost:8000. Au premier lancement, vous êtes redirigé vers `/setup` pour créer l'admin.

> En local, `COOKIE_SECURE=false` est nécessaire en HTTP. Sur Upsun (HTTPS), il vaut `true`.

## Déploiement sur Upsun

Upsun construit et déploie l'application à partir de **`.upsun/config.yaml`**. Il n'y a ni Dockerfile ni image : l'application, le worker et le service PostgreSQL sont tous déclarés dans ce fichier.

| Élément | Configuration |
|---|---|
| Application | `python:3.14`, dépendances installées par `pip install -r requirements.txt` (hook `build`) |
| Web | `uvicorn app.asgi:application --port $PORT` ; `/static` servi directement |
| Worker `runs` | `python -m app.worker` (même code, autre commande ; hérite de la relation BDD) |
| Migrations | `python -m alembic upgrade head` dans le hook `deploy` (exécuté sur le conteneur web uniquement) |
| PostgreSQL | service `database` de type `postgresql:18`, déclaré sous `services:`, relié à l'app par `relationships: database:` |
| `DATABASE_URL` | construit dans `.environment` à partir des variables `DATABASE_*` |
| Route | `https://{default}/` vers `app`, avec redirection de `www` |

### Étapes

```bash
upsun project:create
```

Définissez ensuite les variables **sensibles** au niveau du projet (une commande par variable), avant le
premier push :

```bash
upsun variable:create --level project --name env:APP_SECRET_KEY --value "$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" --sensitive true --visible-build false
```

```bash
upsun variable:create --level project --name env:ENCRYPTION_KEY --value "$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" --sensitive true --visible-build false
```

Optionnel, pour le webhook Anthropic :

```bash
upsun variable:create --level project --name env:ANTHROPIC_WEBHOOK_SECRET --value "whsec_..." --sensitive true --visible-build false
```

Puis déployez :

```bash
git push upsun main
```

| Variable | Obligatoire | Rôle |
|---|---|---|
| `APP_SECRET_KEY` | oui | Signature du cookie de session (32 caractères minimum) |
| `ENCRYPTION_KEY` | oui | Clé Fernet de chiffrement au repos (clé API Anthropic, secrets OAuth). **Ne la changez pas** : les données chiffrées deviendraient illisibles |
| `ANTHROPIC_WEBHOOK_SECRET` | non | Secret `whsec_…` du webhook. Sans lui, `/webhooks/anthropic` répond 404 |
| `PUBLIC_BASE_URL` | non | Déduite de la route principale Upsun. Sert à construire les URL de callback OAuth |

### Webhook Anthropic (optionnel)

Dans la Claude Console, ouvrez **Manage → Webhooks** et ajoutez `https://<votre-domaine>/webhooks/anthropic`,
abonné à `vault_credential.refresh_failed`. Le connecteur concerné passe alors en « Erreur – reconnexion
nécessaire ».

## Configuration des connecteurs

Chaque utilisateur configure ses connecteurs dans **Settings**. Les jetons sont envoyés **directement au
vault Anthropic**. La base ne garde que le type, l'URL, l'ID du credential, les 4 derniers caractères et la
date. Avant l'enregistrement, l'application vérifie le jeton par un handshake MCP `initialize`.
Le bouton « Tester » vérifie ensuite que le credential est présent dans le vault (et valide le jeton OAuth via
`mcp_oauth_validate`), et que le serveur MCP répond.

Informations vérifiées le 13/11/2026 :

| Service | URL MCP | Jeton fixe (`static_bearer`) | OAuth |
|---|---|---|---|
| **Linear** | `https://mcp.linear.app/mcp` | Clé API personnelle : Settings → Account → Security & Access → Personal API keys. Permissions conseillées : Read + Create issues + Create comments, limitées aux équipes utiles | OAuth 2.1 avec enregistrement dynamique (DCR) |
| **GitHub** | `https://api.githubcopilot.com/mcp/` | PAT fine-grained : Settings → Developer settings → Personal access tokens → Fine-grained tokens. Permissions : Contents, Issues, Pull requests (lecture, ou écriture si besoin) | Pas de DCR : l'admin crée une OAuth App GitHub et saisit son client_id / client_secret |
| **Atlassian** (Jira, Confluence) | `https://mcp.atlassian.com/v2/mcp` | Uniquement une **clé API de compte de service** (Bearer). L'admin Atlassian doit activer *Connectors → Atlassian MCP server → Authentication → API token* | **Recommandé.** OAuth 2.1 avec DCR (`offline_access` pour obtenir le refresh token) |

### Mode OAuth

1. L'admin ouvre **Settings → Applications OAuth** :
   - pour **Linear** ou **Atlassian**, il clique sur « Enregistrer automatiquement » (enregistrement dynamique RFC 7591) ;
   - pour **GitHub**, il saisit le client_id / client_secret d'une OAuth App dont l'URL de callback est celle affichée (`https://<domaine>/oauth/github/callback`).
2. Les utilisateurs voient alors « Se connecter avec … ». L'application découvre le serveur d'autorisation (RFC 9728 / RFC 8414), utilise PKCE S256 et le paramètre `resource`, puis échange le code.
3. Elle crée un credential `mcp_oauth` avec un bloc `refresh` (`token_endpoint`, `client_id`, `token_endpoint_auth`). **Anthropic rafraîchit ensuite le jeton** automatiquement.

### Permissions des outils

Pour chaque connecteur, vous choisissez entre « Toujours demander » (`always_ask`, par défaut) et
« Toujours autoriser » (`always_allow`). Ce choix est appliqué au `default_config.permission_policy` du
`mcp_toolset`. Le modifier crée une nouvelle version de l'agent.

## Sécurité

- Mots de passe hachés avec **argon2** (10 caractères minimum). Un temps de réponse constant est conservé quand le login est inconnu.
- Cookie de session signé, `HttpOnly`, `Secure` et `SameSite=Lax`, d'une durée de 12 h. Il est invalidé si le mot de passe est réinitialisé.
- **CSRF** : jeton par session, vérifié sur tous les POST (champ caché ou en-tête `X-CSRF-Token` pour HTMX).
- **Limitation des tentatives de connexion** : 5 échecs par login, ou 20 par IP, sur 15 minutes.
- La clé API Anthropic est **chiffrée au repos** (Fernet) et n'est jamais réaffichée : seuls les 4 derniers caractères sont visibles. Les secrets OAuth admin sont chiffrés de la même façon.
- Aucun secret n'est journalisé. Les loggers `httpx` et `anthropic` sont en WARNING.
- Isolation : un utilisateur n'accède qu'à ses workflows et exécutions (404 sinon). Les pages Utilisateurs et Skills sont réservées à l'admin.
- En-têtes `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` et `Referrer-Policy`.

## Tests

```bash
pytest -q
```

L'API Anthropic est remplacée par un faux client (`tests/conftest.py`). Les tests couvrent :

- le premier lancement et la création de l'admin ;
- les cookies, le CSRF, la limitation des tentatives et la déconnexion ;
- les droits (pages admin, workflows et exécutions d'autres utilisateurs) ;
- le chiffrement de la clé API et l'idempotence de la synchronisation ;
- la déclaration des serveurs MCP et les politiques de permission ;
- la composition du message (skills actifs, dans l'ordre, avant le contenu ; snapshot figé) ;
- la gestion de `requires_action` (validation et refus avec motif), de `end_turn` (question ou terminé), de l'annulation, de la reprise par le worker et du flux SSE.

Pour lancer les mêmes tests sur PostgreSQL (base dédiée, vidée à chaque test, jamais celle d'Upsun) :

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/dispatch_test pytest -q
```

## Limites connues

- **Détection des questions** : heuristique, la dernière ligne du message de Claude doit se terminer par « ? ». Dans tous les cas, l'utilisateur peut continuer la conversation.
- **Atlassian en jeton fixe** : les jetons API personnels (Basic `email:token`) ne sont pas compatibles avec `static_bearer`. Utilisez OAuth ou une clé de compte de service.
- **Le bouton « Tester »** ne peut pas refaire le handshake authentifié pour un jeton fixe, puisque le jeton n'est pas conservé. Il vérifie le credential côté vault et la joignabilité du serveur. Le handshake complet a lieu à l'enregistrement.
- **SSE par polling** de la base (1 s) plutôt que `LISTEN/NOTIFY` : c'est simple et robuste derrière le proxy Upsun, avec une légère latence.
- **Workspace Anthropic** : les vaults sont partagés à l'échelle du workspace. Utilisez une clé API par workspace dédié.
- **Recréation du vault** (clé API d'un autre workspace, vault archivé) : les jetons fixes doivent être ressaisis, et les connecteurs concernés passent en « Erreur ».
- Les fichiers produits par l'agent dans le sandbox (`/mnt/session/outputs`) ne sont pas affichés.
