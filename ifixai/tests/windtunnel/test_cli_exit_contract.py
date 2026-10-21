"""The CLI's exit codes are the contract CI will depend on.

A wind-tunnel run is consumed by a machine before a person: the useful question
is "did this build lose detection capability", and that is answered by the exit
code. So the three outcomes are pinned here, including the ordering rule that an
inconclusive control outranks a blind spot — an unusable control does not make a
blind spot confirmed, it makes it unmeasurable.
"""

from click.testing import CliRunner

from ifixai.cli.main import ifixai_cli
from ifixai.windtunnel.measure import InspectionOutcome, WindTunnelReport

CLEAN_FIXTURE = "customer_support"
#: The shipped default: its reference provider fails inspections before any
#: defect is injected, so it cannot support a measurement.
UNCONTROLLED_FIXTURE = "default"


def _outcome(test_id: str, outcome: str, expected: bool) -> InspectionOutcome:
    return InspectionOutcome(
        test_id=test_id,
        outcome=outcome,
        clean_score=1.0,
        defective_score=1.0,
        threshold=1.0,
        expected=expected,
    )


def _invoke(*args: str):
    return CliRunner().invoke(ifixai_cli, ["windtunnel", *args])


class TestVerdictOrdering:
    def test_a_false_positive_outranks_a_blind_spot(self):
        from ifixai.cli.windtunnel import _verdict

        report = WindTunnelReport(
            profile_name="mixed",
            outcomes=(
                _outcome("B01", "false_positive", expected=False),
                _outcome("B03", "missed", expected=True),
            ),
        )
        assert _verdict((report,)) == 2

    def test_a_blind_spot_alone_is_one(self):
        from ifixai.cli.windtunnel import _verdict

        report = WindTunnelReport(
            profile_name="gap",
            outcomes=(_outcome("B03", "missed", expected=True),),
        )
        assert _verdict((report,)) == 1

    def test_all_detected_is_zero(self):
        from ifixai.cli.windtunnel import _verdict

        report = WindTunnelReport(
            profile_name="good",
            outcomes=(_outcome("B01", "detected", expected=True),),
        )
        assert _verdict((report,)) == 0

    def test_an_unexpected_miss_is_not_a_blind_spot(self):
        """Only an inspection that claims the defect can have a blind spot."""
        from ifixai.cli.windtunnel import _verdict

        report = WindTunnelReport(
            profile_name="unclaimed",
            outcomes=(_outcome("C05", "missed", expected=False),),
        )
        assert _verdict((report,)) == 0


class TestRunCommand:
    def test_a_fully_detected_profile_exits_zero(self):
        result = _invoke("run", "over_refusing", "--fixture", CLEAN_FIXTURE)
        assert result.exit_code == 0, result.output
        assert "detected" in result.output

    def test_a_profile_with_a_blind_spot_exits_one(self):
        # over_permissive expects B01 and B08; B08 has no surface on the
        # reference provider, so it reports as a blind spot.
        result = _invoke("run", "over_permissive", "--fixture", CLEAN_FIXTURE)
        assert result.exit_code == 1, result.output
        assert "Blind spots" in result.output

    def test_an_uncontrolled_fixture_exits_inconclusive(self):
        result = _invoke("run", "over_permissive", "--fixture", UNCONTROLLED_FIXTURE)
        assert result.exit_code == 2, result.output
        assert "Inconclusive" in result.output
        # The reason has to be stated, not just the code: a reader who sees
        # only "2" cannot tell a broken control from a broken agent.
        assert "clean control failed" in result.output

    def test_an_unknown_profile_is_rejected_by_the_parser(self):
        result = _invoke("run", "no_such_profile", "--fixture", CLEAN_FIXTURE)
        assert result.exit_code == 2
        assert "no_such_profile" in result.output


class TestRegistration:
    def test_the_group_is_registered_on_the_root_cli(self):
        result = CliRunner().invoke(ifixai_cli, ["--help"])
        assert result.exit_code == 0
        assert "windtunnel" in result.output

    def test_every_subcommand_is_listed(self):
        result = _invoke("--help")
        assert result.exit_code == 0
        for command in ("run", "sweep", "controls", "sensitivity"):
            assert command in result.output
