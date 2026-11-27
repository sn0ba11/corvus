from pathlib import Path

import httpx
import pytest

from app.adapters.entra_tenant import EntraTenantMetadataAdapter
from app.models.schemas import QueryType


@pytest.mark.asyncio
async def test_entra_tenant_collects_public_oidc_and_federation_metadata(tmp_path: Path) -> None:
    tenant_id = "11111111-2222-3333-4444-555555555555"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/example.org/v2.0/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": f"https://login.microsoftonline.com/{tenant_id}/v2.0",
                    "authorization_endpoint": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize",
                    "token_endpoint": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
                    "jwks_uri": f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
                },
            )
        if request.url.path == "/example.org/FederationMetadata/2007-06/FederationMetadata.xml":
            return httpx.Response(
                200,
                content=f"""<EntityDescriptor xmlns='urn:oasis:names:tc:SAML:2.0:metadata'
                    xmlns:ds='http://www.w3.org/2000/09/xmldsig#'
                    xmlns:fed='http://docs.oasis-open.org/wsfed/federation/200706'
                    xmlns:wsa='http://www.w3.org/2005/08/addressing'
                    entityID='https://sts.windows.net/{tenant_id}/'>
                    <ds:KeyInfo><ds:X509Data><ds:X509Certificate>Y2VydA==</ds:X509Certificate></ds:X509Data></ds:KeyInfo>
                    <fed:PassiveRequestorEndpoint><wsa:EndpointReference><wsa:Address>https://login.microsoftonline.com/{tenant_id}/wsfed</wsa:Address></wsa:EndpointReference></fed:PassiveRequestorEndpoint>
                </EntityDescriptor>""".encode(),
                headers={"content-type": "application/xml"},
            )
        raise AssertionError(f"unexpected request {request.url}")

    adapter = EntraTenantMetadataAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("example.org", QueryType.DOMAIN, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "example.org")
    assert result.returncode == 0

    findings = adapter.normalise_results(
        adapter.parse_results(result, tmp_path),
        "example.org",
        QueryType.DOMAIN,
        "entra-tenant-metadata.json",
    )
    tenant = next(item for item in findings if item.finding_type == "microsoft_entra_tenant")
    assert tenant.metadata["tenant_id"] == tenant_id
    assert tenant.metadata["federation_entity_id"] == f"https://sts.windows.net/{tenant_id}/"
    assert sum(item.finding_type == "microsoft_entra_endpoint" for item in findings) == 3
    certificate = next(
        item for item in findings if item.finding_type == "microsoft_entra_signing_certificate"
    )
    assert len(certificate.value or "") == 64


@pytest.mark.asyncio
async def test_entra_tenant_invalid_domain_response_is_not_found(tmp_path: Path) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "invalid_tenant"})

    adapter = EntraTenantMetadataAdapter(httpx.MockTransport(handler))
    command = adapter.build_command("not-a-tenant.example", QueryType.DOMAIN, tmp_path, 30)
    result = await adapter.execute(command, tmp_path, 30, "not-a-tenant.example")
    assert result.returncode == 0
    assert calls == 1
    assert adapter.parse_results(result, tmp_path) == []
