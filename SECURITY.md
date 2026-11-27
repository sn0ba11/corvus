# Security policy

Corvus is local-only by default. Public Internet exposure is unsupported. Non-loopback binding requires local authentication, TLS/secure cookies at a trusted reverse proxy, host firewall review and an explicit operator decision.

Report vulnerabilities privately to the repository owner. Include the affected commit, impact and minimal reproduction; remove real investigation identifiers, raw output, cookies, tokens and proxy credentials. Do not test against identifiers or services you do not control.

Security boundaries:

- Browser input is server-validated and cannot select executables or flags.
- State changes require a session-bound CSRF token; cookies are SameSite=Strict and can be Secure.
- Jinja autoescaping and CSP prevent upstream HTML from being rendered as trusted content.
- Tool and browser containers are non-root/read-only, capability-free, resource limited and have no Docker socket or host bind mounts.
- The integral browser agent is token-authenticated, isolated from collector networks, restricted to configured public HTTPS targets and exposed for manual control only on loopback through noVNC, which relies on workstation access control.
- Only collectors with an explicit session contract can receive browser sessions. Instaloader, Maigret and the optional metadata-only gallery-dl runner use independently enforced host scopes; Instaloader also receives the originating browser User-Agent and language so cookies are not replayed under a mismatched client identity. Session material is held in memory or mode-`0600` per-run temporary files, then deleted, and cookie values are never persisted or logged. A selected HTTP/SOCKS proxy is applied explicitly inside supported runners rather than inherited blindly from the host environment.
- Private runners revalidate commands and use argument arrays with `shell=False`.
- Secrets and queries are redacted from live output; `.env` and investigation data are Git-ignored.
- Automatic collector self-update, automatic CAPTCHA solving, breach-corpus ingestion and automatic protection bypass are disabled. Corvus application updates are a separate fast-forward-only, backup-and-health-gated operator/scheduled workflow against the configured canonical repository. Managed browser actions require explicit analyst approval.

Known boundary: upstream tools make Internet requests and process adversarial remote content. Treat every upstream update as supply-chain-sensitive, verify immutable commits and rebuild/retest parsers before release. Blackbird has no detected licence and must not be redistributed as a prebuilt image without permission.

Supported security fixes are applied to the current release line. Keep a database backup before updating. Never attach the Docker data volume to a public issue.
