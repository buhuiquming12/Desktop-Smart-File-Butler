from sidecar import main


def test_sidecar_self_test(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["sidecar.py", "--self-test"])
    assert main() == 0
