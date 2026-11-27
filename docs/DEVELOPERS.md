# Developing Corvus

[Operator documentation](INDEX.md) · [Developer Wiki](https://github.com/sn0ba11/corvus/wiki/Developer-Hub)

Corvus uses a fixed registry of Python adapter classes. Adding a module currently means contributing source code to Corvus and rebuilding the application or its collector service. There is no runtime plugin discovery, manifest loader or stable third-party binary interface.

## Local development

Use Git, Python 3.12 and uv.

```sh
git clone https://github.com/sn0ba11/corvus.git
cd corvus
uv sync --locked --extra dev --python 3.12
uv run --locked --extra dev python scripts/start.py
```

The local core runs without Docker; the full stack needs Docker Compose v2 and the relevant services. Keep development cases separate from operational data.

## Start with a collector

The [offline starter kit](../examples/collector-template/README.md) contains a working adapter, synthetic fixture and five regression tests. Follow its registration instructions to integrate a real collector.

| Component | Source |
| --- | --- |
| Adapter contract | [app/adapters/base.py](../app/adapters/base.py) |
| Registry | [app/adapters/__init__.py](../app/adapters/__init__.py) |
| Job scheduling | [app/services/jobs.py](../app/services/jobs.py) |
| Evidence schema | [app/models/schemas.py](../app/models/schemas.py) |
| Native HTTP example | [app/adapters/duckduckgo_instant.py](../app/adapters/duckduckgo_instant.py) |
| Offline example | [app/adapters/phone_offline.py](../app/adapters/phone_offline.py) |
| CLI runner | [tool_containers/runner.py](../tool_containers/runner.py) |
| Container build | [tool_containers/Dockerfile](../tool_containers/Dockerfile) |
| Service definitions | [docker-compose.yml](../docker-compose.yml) |

## Adapter contract

Declare a unique `tool_id`, display name, executable label, capabilities, compatible `QueryType` values, timeout, concurrency limit and selection policy. Implement `build_command`, `parse_results` and `normalise_results`. Native adapters override execution and health checks as necessary; CLI adapters can inherit the supervised runner path. Use `ExecutionResult` for structured failures. The base class handles cancellation for supervised subprocesses and remote jobs; native code must also respect cancellation and bound its own execution.

The scheduler checks health, executes the adapter and processes retained outputs. Do not write directly to the case database from a collector. Return `FindingRecord` values with source identifiers and raw references. Separate a completed run from a positive finding; unknown, blocked and rate-limited responses do not prove absence.

## Native and containerised integrations

Native HTTP integrations should follow the existing egress policy, use fixed reviewed endpoints, bounded responses and request deadlines, and inject a mock transport for tests. Do not follow unvalidated redirects or expose configuration secrets in evidence.

For CLI integrations, pin the upstream revision, review its licence, define the executable and allowed flags in the private runner, add the health probe and build recipe, and wire the service through Compose and the matching runner URL setting. Keep containers isolated, non-root and private. Use the existing service definitions as the starting point; review installer profiles and packaging before enabling a new service. See [adapter development](ADAPTER_DEVELOPMENT.md) and [external integrations](UPSTREAMS.md).

## Frontend and analytical features

FastAPI routes live in [app/api/routes.py](../app/api/routes.py), with additional feature-specific route modules. HTML templates live in [app/templates](../app/templates) and local CSS/JavaScript in [app/static](../app/static). Shared evidence projections should be implemented in services rather than independently reconstructed in templates. Preserve CSRF protection, output escaping and the content security policy. Use local assets and do not load external scripts for case views.

## Validation

```sh
uv run --locked --extra dev pytest examples/collector-template/test_adapter.py
uv run --locked --extra dev ruff check .
uv run --locked --extra dev mypy app browser_agent scripts
uv run --locked --extra dev pytest -m 'not live'
```

Use synthetic fixtures and mocked network responses for ordinary tests. Include failure, malformed response, rate-limit, timeout and cancellation tests appropriate to the source. Never commit operational evidence or credentials. See [security reporting](../SECURITY.md) and [contributing](../CONTRIBUTING.md).
