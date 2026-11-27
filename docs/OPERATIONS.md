# Local operation

Keep the application and managed browser ports bound to loopback. The application supports HTTP Basic authentication using a password hash created with `python scripts/generate_password_hash.py`. Use TLS and secure session cookies before exposing the application beyond the workstation. The managed browser desktop is a separate service and should stay on loopback.

The Compose volumes retain the case database, browser profiles and browser captures. Back up these volumes and the local configuration securely. Stop writing to the database or use a SQLite-consistent backup. Exports and backups remain sensitive after a case is deleted.

Use `docker compose ps` to inspect service status and `docker compose logs app` to troubleshoot startup from the installed checkout. Remove subject values, credentials, file paths and screenshots before sharing diagnostics. See [the operator manual](MANUAL.md) and [privacy notes](PRIVACY.md).
