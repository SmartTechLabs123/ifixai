"""Plan-first paired experiments and offline decision replay."""

import asyncio
import json
from pathlib import Path

import click
from pydantic import ValidationError

from ifixai.experiments import (
    ExperimentPlan,
    ExperimentPolicy,
    analyze_experiment,
    load_experiment,
    run_experiment,
    validate_plan,
)
from ifixai.providers.base import ProviderError
from ifixai.reporting.experiment import write_experiment_reports


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate experiment JSON key")
        result[key] = value
    return result


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object")
    return value


def _plan(path: str) -> ExperimentPlan:
    raw = _read_json(path)
    fixture = raw.get("fixture")
    if isinstance(fixture, str):
        relative = Path(path).absolute().parent / fixture
        if relative.is_file():
            raw["fixture"] = str(relative)
    return ExperimentPlan.model_validate(raw)


def _failure(exc: Exception) -> click.ClickException:
    # ValidationError.__str__ includes rejected input values. A plan/provider
    # error may contain credentials; expose field locations, never that payload.
    if isinstance(exc, ValidationError):
        fields = sorted({'.'.join(str(x) for x in error['loc']) for error in exc.errors()})
        return click.ClickException("Invalid experiment configuration; check fields: " + ', '.join(fields))
    return click.ClickException(
        "Experiment could not complete or verify. Check the plan, installed provider extras, "
        "credential environment variables and a new writable output path. "
        "Provider error payloads are omitted to protect credentials."
    )


def _show(analysis, output: Path) -> None:
    click.echo(f"Experiment decision: {analysis.gate}")
    for reason in analysis.reasons:
        click.echo(f"  {reason}")
    click.echo(f"Evidence: {output / 'experiment.json'}")
    click.echo(f"Report:   {output / 'report.html'}")


def _exit(analysis) -> None:
    raise click.exceptions.Exit({'PASS': 0, 'BLOCK': 1, 'INCONCLUSIVE': 2}[analysis.gate])


@click.group(name="experiment")
def experiment() -> None:
    """Run repeated paired interventions; replay preserved evidence offline."""


@experiment.command(name="run")
@click.argument("plan", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--max-runs", type=click.IntRange(min=1), help="Explicit cap on total audit runs (two per pair); not a currency/token cap.")
@click.option("--output", type=click.Path(file_okay=False), default="ifixai-experiment", show_default=True)
@click.option("--dry-run", is_flag=True, help="Validate and show the plan without credentials, providers or output writes.")
def experiment_run(plan: str, max_runs: int | None, output: str, dry_run: bool) -> None:
    """Execute PLAN (strict JSON) with a bounded number of serial audit runs."""
    try:
        spec = _plan(plan)
        validate_plan(spec)
        if dry_run:
            click.echo(f"{spec.name}: {spec.pairs} paired repeats, {spec.pairs * 2} audit runs")
            click.echo(f"Inspections per run: {', '.join(spec.test_ids)}")
            click.echo(f"Evaluation: {spec.eval_mode}; one common judge configuration")
            click.echo("Order: balanced baseline/candidate; fresh serial provider instances")
            click.echo("This caps audit runs; each inspection can make multiple model calls.")
            return
        if max_runs is None:
            raise click.UsageError("Supply --max-runs after reviewing --dry-run (two audit runs per pair).")
        target = Path(output)
        if target.exists() or target.is_symlink():
            raise click.UsageError("Output exists; choose a new --output directory to preserve prior evidence.")
        if spec.pairs * 2 > max_runs:
            raise click.UsageError("The plan exceeds --max-runs; increase the cap or reduce pairs.")
        bundle = asyncio.run(run_experiment(spec, max_runs=max_runs))
        analysis = analyze_experiment(bundle)
        saved = write_experiment_reports(bundle, analysis, output)
    except (ValidationError, ValueError, TypeError, OSError, RuntimeError, ImportError, ProviderError) as exc:
        raise _failure(exc) from None
    _show(analysis, saved)
    _exit(analysis)


@experiment.command(name="replay")
@click.argument("bundle", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--policy", type=click.Path(exists=True, dir_okay=False), help="Optional strict JSON policy; the original bundle/plan remains unchanged.")
@click.option("--output", type=click.Path(file_okay=False), default="ifixai-experiment-replay", show_default=True)
def experiment_replay(bundle: str, policy: str | None, output: str) -> None:
    """Verify BUNDLE and recompute its decision without calling any provider."""
    try:
        saved_bundle = load_experiment(bundle)
        decision_policy = ExperimentPolicy.model_validate(_read_json(policy)) if policy else None
        analysis = analyze_experiment(saved_bundle, policy=decision_policy)
        saved = write_experiment_reports(saved_bundle, analysis, output)
    except (ValidationError, ValueError, TypeError, OSError, RuntimeError, ImportError) as exc:
        raise _failure(exc) from None
    _show(analysis, saved)
    _exit(analysis)
