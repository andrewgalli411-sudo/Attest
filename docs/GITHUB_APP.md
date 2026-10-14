# Registering the Attest GitHub App

Attest signs people in with GitHub, reads their repositories, and opens pull requests through **one GitHub App** that you
register once for your server. It asks for the least access that makes the product work, and it never needs to be an
administrator of anything.

## 1. Create the App
GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**.

| Field | Value |
|---|---|
| GitHub App name | anything unique, for example `attest-yourcompany` (it becomes the URL slug) |
| Homepage URL | your server's public address, `https://attest.example.com` |
| Callback URL | `https://attest.example.com/api/auth/github/callback` |
| Expire user authorization tokens | leave **on** (Attest asks people to sign in again when theirs expires) |
| Request user authorization (OAuth) during installation | **off** |
| Webhook → Active | **on** |
| Webhook URL | `https://attest.example.com/api/github/webhook` |
| Webhook secret | a long random string; keep it, you need it below (`openssl rand -hex 32`) |

## 2. Permissions (and nothing else)
**Repository permissions**
- **Contents: Read and write.** Reads the source to find where a problem lives, and writes the fix commits to a new branch.
- **Pull requests: Read and write.** Opens the pull request and comments the re-test result on it.
- **Metadata: Read-only.** Required by GitHub.
- **Deployments: Read-only.** Lets Attest see that a pull request's preview deployment is ready.

No account or organisation permissions. Attest never merges, never pushes to your default branch, and never changes
settings; every change is a pull request that a person reviews.

**Subscribe to events:** `Deployment status` and `Pull request`. (`Installation` events are always delivered.)

**Where can this App be installed?** "Any account" if people outside your organisation will use your server, otherwise "Only on this account".

## 3. Collect the credentials
After creating the App:
1. **App ID** (top of the App's page) → `GITHUB_APP_ID`
2. **Client ID** → `GITHUB_CLIENT_ID`
3. **Generate a new client secret** → `GITHUB_CLIENT_SECRET`
4. **Generate a private key**: a `.pem` file downloads. Its contents → `GITHUB_APP_PRIVATE_KEY` (a literal `\n` in place of each
   line break is accepted, which makes it easy to keep on one line)
5. The webhook secret from step 1 → `GITHUB_WEBHOOK_SECRET`
6. The App's URL name (`github.com/apps/<this part>`) → `GITHUB_APP_SLUG` (so the "install on your repositories" link works)

Treat the private key and the client secret like passwords. They belong in your secret manager, never in the repository.

## 4. Point the server at it
See `docs/MULTIUSER.md` for the full list. The GitHub-related settings are:

```
AUTH_MODE=accounts
PUBLIC_URL=https://attest.example.com
SECRETS_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
GITHUB_APP_ID=...        GITHUB_APP_SLUG=...
GITHUB_CLIENT_ID=...     GITHUB_CLIENT_SECRET=...
GITHUB_APP_PRIVATE_KEY=...   GITHUB_WEBHOOK_SECRET=...
```

## 5. Check it works
1. Open your server and choose **Continue with GitHub**. You should land back signed in.
2. On the New project screen, **install the App** on a test repository, then pick it.
3. In the App's **Advanced** tab, open **Recent Deliveries** and press **Redeliver** on a `ping`. The response should be `200`.
   A `401` means the webhook secret differs; a `404` means `GITHUB_WEBHOOK_SECRET` is not set on the server.
4. Run an audit, accept a fix, and publish it. A pull request appears on a branch named `attest/...`.
5. When the pull request's preview deployment finishes, Attest comments the re-test result on it.

## What Attest can and cannot do with this access
- It **can** read the repository's files, create branches named `attest/...`, make commits on them, open pull requests, and comment on them.
- It **cannot** touch your default branch, merge, delete, change settings, or read anything outside the repositories you installed it on.
- If you uninstall the App, Attest immediately stops trying to write to those repositories.
