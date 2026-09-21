# Public release checklist

## Completed in this branch

- [x] Audited the working tree and reachable Git history for credentials, private configuration, logs, databases, and build artifacts.
- [x] Removed tracked historical `.env` files, SQLite data, log files, Python bytecode, and obsolete debug/test artifacts from rewritten history.
- [x] Replaced the environment example with empty placeholders and safe mock defaults.
- [x] Expanded `.gitignore` for secrets, local data, logs, backups, caches, and delivery copies.
- [x] Removed client-specific audit and deployment material from the public tree.
- [x] Added public architecture, setup, safeguards, and limitations documentation.
- [x] Added an MIT license.

## Required before publishing

- [ ] Rotate the historical wallet private key and OpenAI API key; review any account activity and revoke the old credentials.
- [ ] Confirm no other clone, fork, CI artifact, release asset, issue attachment, or deployment log contains the removed values.
- [ ] Review the rewritten branch diff and the documentation for client/IP concerns.
- [ ] Force-push the cleaned rewritten refs only after the review; collaborators must re-clone or reset their copies.
- [ ] Configure GitHub repository visibility manually and confirm branch protection, Actions secrets, deploy keys, webhooks, and release assets are safe.
- [ ] Add a dependency-update policy and run a fresh vulnerability scan in CI with network access.

## Operating guidance

Keep production configuration in a deployment secret store. Use a separately funded execution wallet, low privilege API keys, provider-side spending limits, and a durable database. Never commit `.env`, database files, wallet exports, RPC URLs with embedded credentials, or operational logs.
