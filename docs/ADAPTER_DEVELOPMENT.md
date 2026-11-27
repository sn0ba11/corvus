# Adapter development

Every adapter subclasses `Adapter` and declares `tool_id`, `display_name`, `capabilities`, `supported_query_types`, `healthcheck()`, `get_version()`, `build_command()`, `execute()`, `parse_results()`, `normalise_results()` and `cancel()`. The base class supplies supervised local/runner execution, timeout, cancellation, rate-limit detection, redaction and structured errors.

Rules:

1. Use a documented CLI machine format (JSON/CSV) when available. Console parsing needs a pinned regression fixture and an explicit upstream-version test.
2. Return an argument list beginning with the fixed executable. Never interpolate a shell command or set `shell=True`.
3. Add every allowed option to the runner's tool-specific allowlist; never accept flags from a request.
4. Disable upstream self-update, AI, browser-opening and interactive modes. Authentication-dependent adapters must fail health checks until locally configured.
5. Preserve upstream statuses. Do not invent confidence values. Use `None` when upstream confidence is not meaningful.
6. Store parsed raw metadata but never render it as HTML. Add known secret formats to redaction tests.
7. Detect 429/rate-limit language and stop according to policy. Do not rotate proxies or evade protections.
8. Add sanitised found/not-found, malformed, changed-version, timeout, cancellation and rate-limit fixtures. Default tests must not use the network.

Update workflow:

```bash
# Inspect release notes, licence and automation changes first.
git ls-remote https://github.com/OWNER/REPO.git HEAD
# Edit upstreams.lock.json and the matching docker-compose build ARG.
docker compose build TOOL
pytest tests/adapters -q
ruff check . && mypy app && pytest -m 'not live'
```

Run a manual search only after fixture tests, using an identifier you control. Record the exact smoke result in `docs/ACCEPTANCE.md`. Roll back both hashes if the parser or health check regresses.

## Compare username collectors

The application image includes a CLI audit that runs the username collectors against the same term and retains each stage separately: upstream files, stdout/stderr, parsed records, normalised findings and cross-tool overlap.

```bash
docker compose exec app python scripts/audit_username_collectors.py johnsmith \
  --output /data/collector-audits/johnsmith \
  --timeout 300 \
  --replace
```

The audit is deliberately diagnostic rather than an investigation import. Review `comparison.json` first, then each collector's `parsed.json` and `normalised.json`. The `presentation/` directory contains merged account cards, grouped entities, a combined timeline and a terminal-readable summary.

After parser changes, re-render the retained audit without repeating the network collection:

```bash
docker compose exec app python scripts/render_username_audit.py \
  /data/collector-audits/johnsmith \
  --reparse \
  --stdout
```

`--reparse` writes `normalised-current.json` beside each retained `parsed.json`, leaving the original normalised output intact for comparison. Do not commit live audit output or identifiers to the repository.
