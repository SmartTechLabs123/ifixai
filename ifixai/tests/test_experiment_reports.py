"""Report escaping and preservation controls, independent of experiment execution."""
import json
import os

import pytest

from ifixai.reporting.experiment import (
    render_experiment_html,
    write_experiment_reports,
)


def _bundle():
    return {
        "plan": {
            "name": '</title><script>alert(1)</script>',
            "intervention": '<img src="https://external.invalid/tracker">',
            "baseline": {"label": "current"},
            "candidate": {"label": "changed"},
            "pairs": 5,
        },
        "changed_fields": ["system_prompt"],
        "integrity_digest": "a" * 64,
        "trials": [{"actual_response": "PRIVATE-TRANSCRIPT-SENTINEL"}],
    }


def _report():
    return {
        "gate": "INCONCLUSIVE", "reasons": ["<script>untrusted</script>"],
        "inspections": [{"test_id": "B19", "eligible_pairs": 5,
                         "mean_delta": 0.5, "lower_bound": -0.1, "upper_bound": 1.0}],
    }


def test_html_escapes_untrusted_content_and_excludes_raw_transcripts():
    rendered = render_experiment_html(_bundle(), _report())
    assert '</title><script>alert(1)</script>' not in rendered
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in rendered
    assert 'PRIVATE-TRANSCRIPT-SENTINEL' not in rendered
    assert '<img src=' not in rendered
    assert '+50.0%' in rendered
    assert '-10.0% to +100.0%' in rendered
    assert "default-src 'none'" in rendered


def test_preserved_output_is_private_and_never_overwritten(tmp_path):
    destination = tmp_path / 'experiment'
    saved = write_experiment_reports(_bundle(), _report(), destination)
    assert saved == destination.absolute()
    assert json.loads((destination / 'analysis.json').read_text()) == _report()
    assert json.loads((destination / 'experiment.json').read_text()) == _bundle()
    if os.name == 'posix':
        assert destination.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in destination.iterdir())
    with pytest.raises(FileExistsError):
        write_experiment_reports(_bundle(), {}, destination)
    assert json.loads((destination / 'analysis.json').read_text()) == _report()


def test_report_write_failure_leaves_no_partial_destination(tmp_path, monkeypatch):
    import ifixai.reporting.experiment as reports
    monkeypatch.setattr(reports, 'render_experiment_html', lambda *_args: (_ for _ in ()).throw(OSError('owned write failure')))
    with pytest.raises(OSError):
        write_experiment_reports(_bundle(), _report(), tmp_path / 'failed')
    assert not (tmp_path / 'failed').exists()
    assert list(tmp_path.iterdir()) == []


def test_existing_symlink_cannot_replace_evidence(tmp_path):
    destination = tmp_path / 'target'
    destination.mkdir()
    (destination / 'sentinel').write_text('unchanged')
    link = tmp_path / 'link'
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(FileExistsError):
        write_experiment_reports(_bundle(), _report(), link)
    assert (destination / 'sentinel').read_text() == 'unchanged'


def test_cli_plan_rejects_duplicate_keys_and_hides_rejected_secrets(tmp_path):
    from click.testing import CliRunner

    from ifixai.cli.main import ifixai_cli
    path = tmp_path / 'plan.json'
    path.write_text('{"name":"first","name":"runtime-secret-SENTINEL"}')
    result = CliRunner().invoke(ifixai_cli, ['experiment', 'run', str(path), '--dry-run'])
    assert result.exit_code != 0
    assert 'runtime-secret-SENTINEL' not in result.output
    path.write_text('{"api_key":"runtime-secret-SENTINEL"}')
    result = CliRunner().invoke(ifixai_cli, ['experiment', 'run', str(path), '--dry-run'])
    assert result.exit_code != 0
    assert 'api_key' in result.output
    assert 'runtime-secret-SENTINEL' not in result.output
