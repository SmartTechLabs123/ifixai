"""Paired assurance experiments: declare, execute, freeze, and replay evidence."""
from .analysis import analyze_experiment, simultaneous_bounds
from .integrity import (
    canonical_digest,
    freeze_experiment,
    load_experiment,
    trial_provenance,
    verify_experiment,
)
from .models import (
    ExperimentAnalysis,
    ExperimentArm,
    ExperimentBundle,
    ExperimentJudge,
    ExperimentPlan,
    ExperimentPolicy,
    ExperimentTrial,
    InspectionAnalysis,
)
from .runner import run_experiment, validate_plan

__all__ = [
    "ExperimentAnalysis", "ExperimentArm", "ExperimentBundle", "ExperimentJudge", "ExperimentPlan",
    "ExperimentPolicy", "ExperimentTrial", "InspectionAnalysis", "analyze_experiment", "canonical_digest",
    "freeze_experiment", "load_experiment", "run_experiment", "simultaneous_bounds", "trial_provenance",
    "validate_plan", "verify_experiment",
]
