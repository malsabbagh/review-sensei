# GitHub App Auth

ReviewSensei can authenticate to GitHub as a GitHub App for optional
App-identity comments or reviews. This path is intentionally narrow: it signs
short-lived App JWTs and exchanges them for installation access tokens. The
Python auth module is not a hosted review service, queue, webhook receiver, or
durable delivery store. Installation-time setup bootstrap is documented
separately in
[`docs/github-app-registration.md`](github-app-registration.md) and
[`docs/adr/0014-github-app-setup-bootstrap.md`](adr/0014-github-app-setup-bootstrap.md).
For a deployable webhook ingress, use
[`deploy/cloudflare`](../deploy/cloudflare/README.md), which implements the
same narrow setup boundary in the Worker runtime without a Python Container.

## Registration

Create a GitHub App for the ReviewSensei name and icon you want to publish.
GitHub App identity is registration/branding plus optional App-identity
publication. The open-source workflow remains the primary delivery path; users
run ReviewSensei in their own repositories, usually through GitHub Actions.

Recommended setup:

- App name: **ReviewSensei**.
- App description:

  ```text
  ReviewSensei opens reviewable setup pull requests for repository-owned GitHub Actions code reviews. You control the runner, provider credentials, and merge decisions.
  ```

- Homepage: `https://reviewsensei.dev`.
- Support: [`SUPPORT.md`](https://github.com/malsabbagh/review-sensei/blob/main/SUPPORT.md).
- Privacy and data handling: [`docs/data-handling.md`](https://github.com/malsabbagh/review-sensei/blob/main/docs/data-handling.md).
- Setup URL: use the public ReviewSensei repository or documentation at
  `https://github.com/malsabbagh/review-sensei`.
- Webhook: disable unless you implement a separate webhook ingress. If you
  enable webhooks for setup bootstrap, use the
  [Cloudflare package](../deploy/cloudflare/README.md) or an equivalent
  operator-owned runtime that verifies `X-Hub-Signature-256` with the webhook
  secret before accepting any installation event.
- Permissions: request only what the deployed feature needs. GitHub App
  registration requires implicit `Metadata: read`. For the setup bootstrap,
  request `Contents: write`, `Pull requests: write`, `Variables: write`, and
  `Workflows: write`.
  Opt-in top-level pull-request conversation replies also require
  `Issues: write`; review and inline-reply capability tokens do not receive it.
  Do not request organization, administration, checks, or unrelated
  permissions without a separate design.
- Repository access: selected repositories, so installation scope stays
  explicit.

## Secrets

Never commit the private key. ReviewSensei loads the PEM through an injectable
`PrivateKeySource`:

- `FilePrivateKeySource` reads a PEM file with a bounded read.
- `EnvPrivateKeySource` reads `GITHUB_APP_PRIVATE_KEY` by default.

Set the app id and private key in the process environment or inject the key
source when embedding ReviewSensei in an application. The auth adapter redacts
errors and does not echo private keys, JWTs, installation tokens, authorization
headers, or API bodies.

## Usage

```python
from review_sensei.hosting.github import (
    EnvPrivateKeySource,
    GitHubAppAuth,
    RequestedPermissions,
)

auth = GitHubAppAuth(
    app_id=123456,
    private_key_source=EnvPrivateKeySource("GITHUB_APP_PRIVATE_KEY"),
)

token = auth.installation_token(
    installation_id=789,
    repository="owner/repo",
    requested_permissions=RequestedPermissions({"pull_requests"}),
)

# Use token.token as the Bearer token for that installation/repository scope.
```

The adapter:

- creates short-lived App JWTs with a testable clock and bounded clock skew;
- requests installation access tokens with a narrow repository and permission
  payload;
- verifies the returned token grants every requested permission as write;
- caches tokens keyed by installation, repository, and permission scope;
- refreshes cached tokens before expiry;
- discards cached tokens on suspension/removal/auth failures;
- treats installation tokens as opaque variable-length values;
- maps unavailable installation, insufficient permission, configuration, and
  transient GitHub failures to distinct error classes.

## Key rotation and emergency revocation

Rotate GitHub App private keys through GitHub App settings and redeploy the new
PEM to the runtime's secret store. Delete the old PEM after the new key has
been verified end to end. The adapter does not persist keys or tokens.

For emergency revocation:

1. Suspend or delete the installation in GitHub App settings, or rotate the App
   private key immediately.
2. Clear the runtime token cache and stop new publication attempts.
3. Revoke any externally stored installation tokens from your application or
   secrets store.
4. Treat installation deletion, suspension, and permission downgrade as an
   authorization failure before publishing anything.

## Error classes

Public errors expose stable `error_category` values:

| Class | Category |
| --- | --- |
| `GitHubAuthConfigurationError` | `github_auth_configuration` |
| `GitHubAuthUnavailableInstallationError` | `github_auth_unavailable_installation` |
| `GitHubAuthInsufficientPermissionError` | `github_auth_insufficient_permission` |
| `GitHubAuthTransientError` | `github_auth_transient` |
| `GitHubAuthError` | `github_auth` |

Transient failures should be retried with a bounded delay only after checking
rate-limit and service-status guidance. Configuration and permission failures
should not be retried until an operator fixes the App, key, or installation.

The setup and webhook boundaries add stable error categories documented in
`docs/public-contracts.md`.

## OIDC installation-token broker

The optional broker is a hosted issuance-only backend for workflows that need
an installation token without placing the ReviewSensei App private key in a
customer repository. Configure `BrokerPolicy` with the expected issuer and
audience, an explicit `approved_workflows` set, and the least-privileged
`requested_permissions` needed by the caller. The default broker audience is
`sts.reviewsensei.dev`.

The calling workflow must grant the OIDC permission and request the configured
audience:

```yaml
permissions:
  id-token: write
```

The token request uses `permissions: id-token: write` and
`audience: sts.reviewsensei.dev`; the broker verifies issuer, audience, JWKS,
RS256 signature, time claims, repository identity, workflow identity, fork
status, installation mapping, rate limits, and requested permissions before
issuing a repository-scoped token.

Key rotation follows the App key-rotation procedure above. For emergency
revocation, revoke the installation, rotate the App key, and clear the broker
and `GitHubAppAuth` token caches. Configure the in-memory rate limiter for the
operator's availability budget; GitHub transport failures fail closed rather
than issuing a token.

Customer-visible broker failures are stable `BrokerRejectionError` and
`BrokerRateLimitError` categories. A future HTTP frontend can map them to 401
and 429 respectively. Broker errors and audit records never include tokens,
keys, JWTs, authorization headers, or assertion content.

### Issue-64 Worker capability broker

The deployed Worker endpoint is `POST /github/token` and accepts a bounded
JSON body containing an OIDC assertion plus one of the fixed capability names:

| Capability | GitHub installation permission |
| --- | --- |
| `review_publish` | `pull_requests: write` |
| `inline_reply` | `pull_requests: write` |
| `issue_reply` | `issues: write` |
| `learning_write` | `contents: write`, `pull_requests: write` |

The signed assertion must use issuer
`https://token.actions.githubusercontent.com`, audience
`sts.reviewsensei.dev`, and include `iss`, `aud`, `exp`, `iat`, `sub`, `jti`,
`repository`, `repository_owner`, `repository_id`, `event_name`,
`workflow_ref`, `workflow_sha`, `job_workflow_ref`, `job_workflow_sha`,
`run_id`, `run_attempt`, and `runner_environment` (plus the documented actor
and ref claims). `installation_id` is not trusted or accepted as a caller
claim. The broker requires the configured immutable workflow SHA (and only
explicitly retained older pinned SHA pairs during migration),
checks repository and fork state server-side, resolves the installation
server-side, claims replay/rate state in the SQLite ledger, and then requests a
repository-scoped token for only the selected capability. The response is
`Cache-Control: no-store`; there is no browser CORS contract.
