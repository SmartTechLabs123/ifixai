"""Declarative plans cannot smuggle executable providers or credentials."""

import copy

import pytest
from pydantic import ValidationError

from ifixai.experiments import ExperimentPlan, validate_plan

PLAN = {
    "name": "Test plan", "intervention": "Controlled system prompt change",
    "fixture": "customer_support", "test_ids": ["B19"],
    "baseline": {"label": "a", "provider": "http", "endpoint": "http://127.0.0.1:1"},
    "candidate": {"label": "b", "provider": "http", "endpoint": "http://127.0.0.1:1", "system_prompt": "Use supplied facts"},
}


@pytest.mark.parametrize("target,key,value", [
    ("baseline", "api_key", "plaintext-secret"),
    ("candidate", "provider", "python:custom.plugin"),
    ("baseline", "endpoint", "https://user:password@example.org"),
    ("candidate", "endpoint", "https://example.org?api_key=private"),
    ("candidate", "endpoint", "file:///private/model"),
    ("candidate", "temperature", float("nan")),
    ("candidate", "temperature", float("inf")),
    ("candidate", "temperature", -1),
    ("candidate", "timeout", 0),
    ("candidate", "max_retries", -1),
    ("candidate", "timeout", "30"),
    ("candidate", "api_key_env", "bad-env-name"),
    ("candidate", "endpoint", "http://127.0.0.1:not-a-port"),
    ("candidate", "endpoint", "http://@example.org"),
    ("candidate", "endpoint", "http://example.org/\npath"),
    ("candidate", "model", "   "),
])
def test_arm_rejects_unsafe_or_ambiguous_fields(target, key, value):
    data = copy.deepcopy(PLAN)
    data[target][key] = value
    with pytest.raises(ValidationError):
        ExperimentPlan.model_validate(data)


@pytest.mark.parametrize("key,value", [
    ("test_ids", []), ("test_ids", ["B19", "B19"]),
    ("test_ids", ["b19"]), ("test_ids", ["UNKNOWN"]),
    ("pairs", 0), ("pairs", 1001), ("pairs", True),
    ("seed", -1), ("executable", "run.sh"), ("placebo", "false"),
    ("policy", {"confidence": 1}), ("policy", {"noninferiority_margin": 1}),
])
def test_invalid_plan_and_policy_rejected(key, value):
    data = copy.deepcopy(PLAN)
    data[key] = value
    with pytest.raises(ValidationError):
        ExperimentPlan.model_validate(data)


def test_placebo_must_be_explicit_and_dry_validation_has_no_provider(monkeypatch):
    data = copy.deepcopy(PLAN)
    data["candidate"].pop("system_prompt")
    with pytest.raises(ValidationError):
        ExperimentPlan.model_validate(data)
    data["placebo"] = True
    data["baseline"]["api_key_env"] = "UNSET_TEST_SECRET"
    data["candidate"]["api_key_env"] = "UNSET_TEST_SECRET"
    plan = ExperimentPlan.model_validate(data)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("declarative validation reached provider")
    monkeypatch.setattr("ifixai.providers.resolver.resolve_provider", forbidden)
    monkeypatch.delenv("UNSET_TEST_SECRET", raising=False)
    assert validate_plan(plan).metadata.name == "Customer Support Assistant (illustrative)"


def test_frozen_plan_needs_deliberate_reconstruction():
    plan = ExperimentPlan.model_validate(PLAN)
    with pytest.raises(ValidationError):
        plan.pairs = 999
    with pytest.raises(ValidationError):
        plan.candidate.system_prompt = "replacement"


@pytest.mark.parametrize("provider,fields", [
    ("azure", {"endpoint": None, "model": "deployment"}),
    ("azure", {"endpoint": "https://example.org", "model": None}),
    ("bedrock", {"model": None}),
    ("bedrock", {"model": "native-model", "api_key_env": "CUSTOM_IGNORED_KEY"}),
])
def test_provider_native_requirements_fail_during_declaration(provider, fields):
    data = copy.deepcopy(PLAN)
    data["candidate"].update(provider=provider, **fields)
    with pytest.raises(ValidationError):
        ExperimentPlan.model_validate(data)


@pytest.mark.parametrize("key,value", [("max_retries", 20), ("system_prompt", "ignored judge prompt"), ("label", "unused")])
def test_judge_does_not_accept_settings_ignored_by_native_judge_api(key, value):
    data = copy.deepcopy(PLAN)
    data["judge"] = {"provider": "http", "model": "judge", key: value}
    with pytest.raises(ValidationError):
        ExperimentPlan.model_validate(data)
