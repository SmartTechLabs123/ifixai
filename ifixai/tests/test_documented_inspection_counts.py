"""The inspection counts printed in the docs must match the registry.

Three docs quote how many inspections ship: ``README.md`` and
``docs/methodology.md`` in digits, ``docs/what-we-test.md`` in words. Those
numbers were maintained by hand and drifted — ``what-we-test.md`` still said
forty-nine after the extended series grew to twenty-nine. For an audit tool the
count is a load-bearing claim, so it is asserted here against
:data:`ifixai.harness.registry.ALL_SPECS` rather than trusted to review.

Adding an inspection is expected to fail this test. Update the prose in the
same commit; that is the point.
"""

from pathlib import Path

import pytest

from ifixai.harness.registry import ALL_SPECS

REPO_ROOT = Path(__file__).resolve().parents[2]

# Only the values these docs actually quote. Keep it small on purpose: a general
# number-to-words converter would hide a drifted count behind a passing test.
NUMBER_WORDS = {
    29: "twenty-nine",
    32: "thirty-two",
    61: "sixty-one",
}


def _counts() -> tuple[int, int, int]:
    """Total, core (``B`` series) and extended inspection counts."""
    core = sum(1 for spec in ALL_SPECS if spec.test_id.startswith("B"))
    return len(ALL_SPECS), core, len(ALL_SPECS) - core


def _read(relative: str) -> str:
    path = REPO_ROOT / relative
    if not path.is_file():
        pytest.skip(f"{relative} is not present in this checkout")
    return path.read_text(encoding="utf-8")


def test_registry_splits_into_core_and_extended() -> None:
    total, core, extended = _counts()
    assert total == core + extended
    assert core > 0 and extended > 0


def test_methodology_quotes_the_registry_counts() -> None:
    total, core, extended = _counts()
    text = _read("docs/methodology.md")
    expected = f"{total} inspections ({core} core, {extended} extended)"
    assert expected in text, (
        f"docs/methodology.md must state {expected!r}. "
        "The registry changed; update the prose in this commit."
    )


def test_readme_quotes_the_total() -> None:
    total, _, _ = _counts()
    text = _read("README.md")
    assert f"{total} inspections" in text, (
        f"README.md must state '{total} inspections'. "
        "The registry changed; update the prose in this commit."
    )


def test_what_we_test_quotes_the_counts_in_words() -> None:
    total, core, extended = _counts()
    missing = [n for n in (total, core, extended) if n not in NUMBER_WORDS]
    assert not missing, (
        f"No spelled-out form on record for {missing}. Add it to NUMBER_WORDS "
        "and update docs/what-we-test.md to match the registry."
    )

    text = _read("docs/what-we-test.md").lower()
    for count, label in (
        (total, "total"),
        (core, "core (graded) inspections"),
        (extended, "extended inspections"),
    ):
        word = NUMBER_WORDS[count]
        assert word in text, (
            f"docs/what-we-test.md must spell the {label} count as {word!r} "
            f"({count}). The registry changed; update the prose in this commit."
        )
