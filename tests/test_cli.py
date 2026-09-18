from typer.testing import CliRunner

from process_doctor.cli import app


def test_default_invocation_exits_clean():
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0
    assert "not yet implemented" in result.output
