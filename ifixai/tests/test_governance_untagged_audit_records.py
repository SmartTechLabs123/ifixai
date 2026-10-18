import pytest

from ifixai.core.types import ProviderConfig
from ifixai.providers.governance_fixture import (
    AuthorizationRule,
    GovernanceFixture,
    PoliciesBlock,
    SeededAuditRecord,
)
from ifixai.providers.mock_governance import MockGovernanceProvider


def _provider() -> MockGovernanceProvider:
    fixture = GovernanceFixture(
        policies=PoliciesBlock(
            authorization=(AuthorizationRule(role="user", tools=("*",)),)
        ),
        seed_audit_records=(SeededAuditRecord(actor="seed", action="seeded"),),
    )
    return MockGovernanceProvider(governance=fixture)


@pytest.mark.asyncio
async def test_untagged_invoke_does_not_leak_into_other_request_ids_trail():
    provider = _provider()
    config = ProviderConfig(provider="mock")  # request_id is None
    result = await provider.invoke_tool("search", "user", {}, config)
    assert result is not None and result.authorized

    trail = await provider.get_audit_trail("B13-user-search", config)
    assert [record.actor for record in trail] == ["seed"]


@pytest.mark.asyncio
async def test_seeded_records_stay_ambient_for_any_query():
    provider = _provider()
    config = ProviderConfig(provider="mock")
    await provider.invoke_tool("search", "user", {}, config)

    for query in ("B13-user-search", "B23-anything", "some-other-id"):
        trail = await provider.get_audit_trail(query, config)
        assert any(record.actor == "seed" for record in trail)


@pytest.mark.asyncio
async def test_tagged_invoke_matches_only_its_own_request_id():
    provider = _provider()
    config = ProviderConfig(provider="mock", request_id="run-1")
    await provider.invoke_tool("search", "user", {}, config)

    own = await provider.get_audit_trail("run-1", config)
    assert any(record.action == "invoke" for record in own)
    other = await provider.get_audit_trail("run-2", config)
    assert all(record.action != "invoke" for record in other)
