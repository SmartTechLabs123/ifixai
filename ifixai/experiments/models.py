"""Safe experiment declarations and retained, independently replayable evidence."""
from __future__ import annotations

from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ifixai.core.types import EvaluationMode, TestRunResult


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _known_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    from ifixai.harness.registry import ALL_SPECS

    known = {spec.test_id for spec in ALL_SPECS}
    if not values or len(set(values)) != len(values) or any(v not in known for v in values):
        raise ValueError("inspection IDs must be nonempty, unique, canonical known IDs")
    return values


class ExperimentArm(FrozenModel):
    label: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=80)
    model: str | None = Field(default=None, max_length=300)
    endpoint: str | None = Field(default=None, max_length=2048)
    system_prompt: str | None = Field(default=None, max_length=100000)
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout: int = Field(default=30, ge=1, le=3600, strict=True)
    max_retries: int = Field(default=3, ge=0, le=20, strict=True)

    @field_validator("provider")
    @classmethod
    def known_provider(cls, value: str) -> str:
        from ifixai.providers.resolver import REGISTERED_PROVIDERS

        if value not in REGISTERED_PROVIDERS:
            raise ValueError("provider must be a registered provider name")
        return value

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value: str | None) -> str | None:
        if value is not None:
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError("endpoint must not contain control characters")
            parsed = urlsplit(value)
            _ = parsed.port
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("endpoint must be an HTTP(S) URL")
            if parsed.username is not None or parsed.password is not None or parsed.fragment:
                raise ValueError("endpoint must not contain credentials or fragments")
            if any(any(word in key.lower() for word in ("key", "token", "secret", "auth", "password", "signature")) for key, _ in parse_qsl(parsed.query)):
                raise ValueError("endpoint credentials must come from the environment")
        return value

    @model_validator(mode="after")
    def provider_requirements(self) -> ExperimentArm:
        if self.model is not None and not self.model.strip():
            raise ValueError("declared model must be nonempty")
        if self.provider == "azure" and (not self.endpoint or not self.model):
            raise ValueError("azure requires explicit endpoint and deployment model")
        if self.provider == "bedrock" and (not self.model or self.api_key_env):
            raise ValueError("bedrock requires a model and its native AWS credential environment, not api_key_env")
        return self


class ExperimentJudge(FrozenModel):
    provider: str = Field(min_length=1, max_length=80)
    model: str | None = Field(default=None, max_length=300)
    endpoint: str | None = Field(default=None, max_length=2048)
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout: int = Field(default=30, ge=1, le=3600, strict=True)
    max_calls_per_run: int = Field(default=200, ge=1, le=10000, strict=True)

    @field_validator("provider")
    @classmethod
    def known_provider(cls, value: str) -> str:
        return ExperimentArm.known_provider(value)

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value: str | None) -> str | None:
        return ExperimentArm.safe_endpoint(value)

    @model_validator(mode="after")
    def provider_requirements(self) -> ExperimentJudge:
        if self.model is not None and not self.model.strip():
            raise ValueError("declared judge model must be nonempty")
        if self.provider == "azure" and (not self.endpoint or not self.model):
            raise ValueError("azure judge requires explicit endpoint and deployment model")
        if self.provider == "bedrock" and (not self.model or self.api_key_env):
            raise ValueError("bedrock judge requires a model and its native AWS credential environment, not api_key_env")
        return self


class ExperimentPolicy(FrozenModel):
    min_pairs: int = Field(default=5, ge=1, le=1000, strict=True)
    noninferiority_margin: float = Field(default=0.1, ge=0, lt=1)
    confidence: float = Field(default=0.95, gt=0, lt=1)
    required_test_ids: tuple[str, ...] = ("B01", "B08", "P01")

    @field_validator("required_test_ids")
    @classmethod
    def known_required(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _known_ids(values)


class ExperimentPlan(FrozenModel):
    name: str = Field(min_length=1, max_length=200)
    intervention: str = Field(min_length=1, max_length=10000)
    fixture: str = Field(default="default", min_length=1, max_length=4096)
    test_ids: tuple[str, ...]
    baseline: ExperimentArm
    candidate: ExperimentArm
    pairs: int = Field(default=5, ge=1, le=1000, strict=True)
    seed: int = Field(default=0, ge=0, le=2**31 - 1001, strict=True)
    judge: ExperimentJudge | None = None
    policy: ExperimentPolicy = Field(default_factory=ExperimentPolicy)
    eval_mode: EvaluationMode = EvaluationMode.DETERMINISTIC
    placebo: bool = Field(default=False, strict=True)

    @field_validator("test_ids")
    @classmethod
    def known_selected(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _known_ids(values)

    @model_validator(mode="after")
    def valid_comparison(self) -> ExperimentPlan:
        if self.baseline.label == self.candidate.label:
            raise ValueError("arms require distinct labels")
        baseline = self.baseline.model_dump(exclude={"label"})
        candidate = self.candidate.model_dump(exclude={"label"})
        if baseline == candidate and not self.placebo:
            raise ValueError("declare an effective intervention or explicitly set placebo=true")
        if self.eval_mode in {EvaluationMode.SINGLE, EvaluationMode.FULL} and self.judge is None:
            raise ValueError("single/full evaluation requires an explicit judge declaration")
        return self


class ExperimentTrial(FrozenModel):
    pair_index: int = Field(ge=0, le=999, strict=True)
    order: tuple[Literal["baseline", "candidate"], Literal["baseline", "candidate"]]
    seed: int = Field(ge=0, strict=True)
    run_nonce: str = Field(pattern=r"^[0-9a-f]{16}$")
    holdout_ids: dict[str, str]
    pipeline_config: dict[str, Any]
    baseline: TestRunResult
    candidate: TestRunResult


class InspectionAnalysis(FrozenModel):
    test_id: str
    eligible_pairs: int = 0
    paired_deltas: tuple[float, ...] = ()
    mean_delta: float | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    fixed: tuple[str, ...] = ()
    broken: tuple[str, ...] = ()
    persistent_pass: tuple[str, ...] = ()
    persistent_fail: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unscored: tuple[str, ...] = ()
    divergent: tuple[str, ...] = ()
    coverage_complete: bool = False


class ExperimentAnalysis(FrozenModel):
    gate: Literal["PASS", "BLOCK", "INCONCLUSIVE"] = "INCONCLUSIVE"
    reasons: tuple[str, ...] = ()
    inspections: tuple[InspectionAnalysis, ...] = ()
    assumptions: tuple[str, ...] = ()
    exploratory: bool = True
    planned_pairs: int = 0
    completed_pairs: int = 0
    applied_policy: ExperimentPolicy = Field(default_factory=ExperimentPolicy)
    post_capture_policy: bool = False


class ExperimentBundle(FrozenModel):
    schema_version: Literal["1"] = "1"
    plan: ExperimentPlan
    fixture_snapshot: dict[str, Any]
    fixture_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hashes: dict[str, str]
    runtime_versions: dict[str, str]
    changed_fields: tuple[str, ...]
    trials: tuple[ExperimentTrial, ...]
    analysis: ExperimentAnalysis = Field(default_factory=ExperimentAnalysis)
    redactions: tuple[str, ...] = ()
    integrity_digest: str = ""
