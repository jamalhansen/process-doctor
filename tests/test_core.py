from process_doctor.core import run


def test_run_returns_a_string():
    assert isinstance(run(), str)
