"""Self-contained reports for preserved paired experiments; no remote assets."""

import html
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


def _data(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_experiment_html(bundle: Any, report: Any) -> str:
    """Render only analysis metadata: raw response transcripts remain in JSON."""
    data, analysis = _data(bundle), _data(report)
    plan = data["plan"]
    gate = analysis.get("gate", "INCONCLUSIVE")
    if isinstance(gate, dict):
        gate = gate.get("status", "INCONCLUSIVE")
    gate = str(gate).upper()
    reasons = analysis.get("reasons", [])
    inspections = analysis.get("inspections", [])
    if isinstance(inspections, dict):
        inspections = list(inspections.values())
    cards = []
    for item in inspections:
        test_id = item.get("test_id", "")
        delta = item.get("mean_delta")
        delta_display = "Unscored" if delta is None else f"{delta:+.1%}"
        lower, upper = item.get("lower_bound"), item.get("upper_bound")
        interval = "Unavailable" if lower is None or upper is None else f"{lower:+.1%} to {upper:+.1%}"
        counts = {name: len(item.get(name, [])) for name in ("fixed", "broken", "persistent_fail", "missing", "unscored", "divergent")}
        counts_html = ''.join('<span class="count">' + _escape(name.replace('_', ' ')) + ' <b>' + str(count) + '</b></span>' for name, count in counts.items() if count)
        interval_plot = ''
        if lower is not None and upper is not None and delta is not None:
            interval_plot = (
                '<div class="plot" aria-label="Paired change interval"><span class="zero"></span>'
                + '<span class="range" style="left:' + str((lower + 1) * 50) + '%;width:' + str((upper - lower) * 50) + '%"></span>'
                + '<span class="point" style="left:' + str((delta + 1) * 50) + '%"></span></div>'
                + '<div class="axis"><span>−100%</span><span>No change</span><span>+100%</span></div>'
            )
        cards.append(
            '<article data-inspection="' + _escape(test_id) + '"><div class="row">'
            '<h3>' + _escape(test_id) + '</h3><b>' + _escape(delta_display) + '</b></div>'
            '<p>Paired score change · candidate minus baseline</p><dl>'
            '<dt>Simultaneous interval</dt><dd>' + _escape(interval) + '</dd>'
            '<dt>Eligible repeats</dt><dd>' + _escape(item.get("eligible_pairs", item.get("scored_pairs", 0))) + '</dd>'
            '</dl>' + interval_plot + '<div class="counts">' + counts_html + '</div><details><summary>Inspect counts and pairing diagnostics</summary><pre>'
            + _escape(json.dumps(item, indent=2, ensure_ascii=False)) + '</pre></details></article>'
        )
    reasons_html = ''.join('<li>' + _escape(x) + '</li>' for x in reasons)
    changed = data.get("changed_fields", [])
    digest = data.get("digest", data.get("integrity_digest", ""))
    overview = {key: value for key, value in analysis.items() if key not in {"inspections", "reasons"}}
    # JSON lives inside an escaped pre, never inside an executable script.
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{_escape(plan['name'])} · iFixAi experiment</title><style>
:root{{color-scheme:dark;--ink:#ecf1ef;--muted:#a9b7b1;--line:#314740;--accent:#85e7b6}}
*{{box-sizing:border-box}}body{{margin:0;background:#101b17;color:var(--ink);font:16px/1.6 system-ui,sans-serif}}
main{{max-width:1160px;margin:auto;padding:60px 28px}}header{{border-bottom:1px solid var(--line);padding-bottom:32px}}
small,.muted,dt{{color:var(--muted)}}small{{letter-spacing:.14em;text-transform:uppercase}}h1{{font-size:clamp(32px,5vw,56px);line-height:1.08;max-width:850px;margin:18px 0}}h2{{margin-top:40px}}h3,p{{margin:8px 0}}.row{{display:flex;justify-content:space-between;gap:20px}}.gate{{display:inline-block;border:1px solid var(--accent);color:var(--accent);padding:8px 16px;margin:20px 0;border-radius:6px;font-weight:700}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}}article{{background:#172720;border:1px solid var(--line);border-radius:10px;padding:22px}}article b{{font-size:26px}}.plot{{position:relative;height:22px;margin:28px 0 6px;background:#0d1511;border-radius:4px}}.zero{{position:absolute;left:50%;height:100%;width:1px;background:var(--muted)}}.range{{position:absolute;top:9px;height:4px;background:var(--accent)}}.point{{position:absolute;top:6px;width:10px;height:10px;background:var(--ink);border-radius:50%;transform:translateX(-50%)}}.axis{{display:flex;justify-content:space-between;font-size:11px;color:var(--muted)}}.counts{{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0}}.count{{font-size:13px;background:#26392f;border-radius:5px;padding:3px 8px}}.count b{{font-size:13px}}dl{{display:grid;grid-template-columns:1fr auto;gap:8px}}dd{{margin:0}}input{{background:#172720;color:var(--ink);border:1px solid var(--line);padding:12px;border-radius:6px;width:100%;max-width:380px;margin:0 0 20px}}details{{margin-top:18px}}summary{{cursor:pointer;color:var(--accent)}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace;background:#0d1511;padding:18px;border-radius:6px}}code{{overflow-wrap:anywhere}}li{{margin:10px 0}}[hidden]{{display:none!important}}footer{{border-top:1px solid var(--line);margin-top:42px;padding-top:24px;color:var(--muted)}}
</style></head><body><main><header><small>iFixAi · Paired assurance experiment</small>
<h1>{_escape(plan['name'])}</h1><p>{_escape(plan['intervention'])}</p>
<span class="gate">{_escape(gate)}</span><p>Baseline: <b>{_escape(plan['baseline']['label'])}</b> → Candidate: <b>{_escape(plan['candidate']['label'])}</b></p>
<p class="muted">Declared changes: {_escape(', '.join(changed) or 'Placebo / unchanged configuration')} · {_escape(plan['pairs'])} paired repeats</p></header>
<h2>Decision and its limits</h2><ul>{reasons_html}</ul><p class="muted">This decision concerns the selected suite and declared margin. Same seeds request comparable probes; they do not guarantee model determinism, environment stability or causality. Original scorecards retain their own grades. A hash detects alterations relative to this bundle; it does not authenticate a provider or establish truth.</p>
<h2>Where behavior changed</h2><label for="filter">Find an inspection</label><br><input id="filter" type="search" placeholder="Inspection ID" autocomplete="off"><div class="cards">{''.join(cards)}</div>
<details><summary>Decision calculation and provenance</summary><pre>{_escape(json.dumps(overview, indent=2, ensure_ascii=False))}</pre></details>
<footer>Evidence digest: <code>{_escape(digest)}</code><p>No external assets or analytics. The complete private evidence bundle accompanies this report; it can include sensitive test inputs and outputs. Offline replay recalculates the analysis without calling a model.</p></footer></main><script>
const filter=document.getElementById('filter');filter.addEventListener('input',()=>{{const query=filter.value.toLowerCase();document.querySelectorAll('[data-inspection]').forEach(card=>card.hidden=!card.dataset.inspection.toLowerCase().includes(query));}});
</script></body></html>'''


def render_experiment_markdown(bundle: Any, report: Any) -> str:
    data, analysis = _data(bundle), _data(report)
    return (
        '# iFixAi paired assurance experiment\n\n'
        + data['plan']['name'] + '\n\n'
        + 'The selected-suite decision is separate from each original audit grade.\n\n'
        + '```json\n' + json.dumps(analysis, indent=2, ensure_ascii=False) + '\n```\n\n'
        + 'Offline replay recalculates this decision from retained results; it does not rerun models. '
        + 'Paired seeds and digests do not prove deterministic behavior, causality or authenticity.\n'
    )


def write_experiment_reports(bundle: Any, report: Any, output: str | Path) -> Path:
    """Publish a complete new owner-only directory; never overwrite prior evidence."""
    target = Path(output).absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError("Experiment output already exists; choose a new directory")
    staging = Path(tempfile.mkdtemp(prefix=".ifixai-experiment-", dir=target.parent))
    try:
        payloads = {
            "experiment.json": json.dumps(_data(bundle), ensure_ascii=False, indent=2) + '\n',
            "analysis.json": json.dumps(_data(report), ensure_ascii=False, indent=2) + '\n',
            "report.html": render_experiment_html(bundle, report),
            "report.md": render_experiment_markdown(bundle, report),
        }
        for name, content in payloads.items():
            path = staging / name
            with path.open('x', encoding='utf-8') as stream:
                os.chmod(path, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        # mkdir reserves the destination without replacing an existing directory.
        target.mkdir(mode=0o700)
        try:
            for path in staging.iterdir():
                path.rename(target / path.name)
        except OSError:
            shutil.rmtree(target)
            raise
    finally:
        shutil.rmtree(staging)
    return target
