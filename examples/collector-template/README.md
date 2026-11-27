# Collector starter kit

A deliberately offline example using a synthetic catalogue. It exercises the real Corvus adapter contract without sending any data to external services. It is not registered or shipped as a production collector.

From the repository root:

```sh
uv sync --locked --extra dev --python 3.12
uv run --locked --extra dev pytest examples/collector-template/test_adapter.py
uv run --locked --extra dev ruff check examples/collector-template
uv run --locked --extra dev mypy examples/collector-template/adapter.py
```

The example implements command construction, bounded local execution, parsing and normalisation. The fixture contains two invented records; a matching name produces a source-attributed lead, not an identity assertion.

## Turn it into a collector

1. Choose a unique tool ID and supported query types.
2. Copy the adapter into `app/adapters/` under a unique module name. Replace the fixture lookup with your own documented source; do not register the synthetic example. For a native HTTP collector, follow [DuckDuckGo Instant Answer](../../app/adapters/duckduckgo_instant.py) and the [egress policy](../../app/services/egress_policy.py).
3. Import your adapter in `app/adapters/__init__.py` and register its class as `REGISTRY[YourAdapter.tool_id] = YourAdapter`. Keep specialist collectors opt-in.
4. Copy the tests to `tests/adapters/`, update their import, and use synthetic fixtures or mocked responses. Verify the adapter appears only for compatible queries.
5. For a CLI integration, use the existing private runner rather than wrapping commands in a shell. See the [developer guide](../../docs/DEVELOPERS.md).
6. Update the collector catalogue, applicable privacy documentation, and upstream notices.

The example is intentionally not a Docker service. A containerised integration requires changes to the existing runner, image and Compose configuration; copying a standalone Dockerfile would not register a collector.

See [developer documentation](../../docs/DEVELOPERS.md).
