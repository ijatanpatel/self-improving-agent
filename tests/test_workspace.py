import pytest

from loop.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "support_bot" / "kb").mkdir(parents=True)
    (tmp_path / "loop").mkdir()
    (tmp_path / "support_bot" / "app.py").write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")
    (tmp_path / "support_bot" / "kb" / "facts.md").write_text("# Facts\n", encoding="utf-8")
    (tmp_path / "loop" / "core.py").write_text("print('loop')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    return Workspace(tmp_path, editable=("support_bot/", "tests/"), protected=("support_bot/kb/",))


def test_agents_cannot_leave_the_repo_or_read_secrets(ws):
    with pytest.raises(PermissionError):
        ws.read_file("../outside.txt")
    with pytest.raises(PermissionError):
        ws.read_file(".env")
    assert "secret" not in ws.search_code("OPENAI")


def test_edits_are_limited_to_the_allowlist(ws):
    with pytest.raises(PermissionError):
        ws.replace_in_file("loop/core.py", "loop", "hacked")
    with pytest.raises(PermissionError):
        ws.create_file("support_bot/kb/new.md", "invented product facts")


def test_replace_needs_a_unique_match(ws):
    assert ws.replace_in_file("support_bot/app.py", "x = 1", "x = 3").startswith("ERROR")
    assert ws.replace_in_file("support_bot/app.py", "y = 2", "y = 5") == "Edited support_bot/app.py"
    assert "y = 5" in ws.read_file("support_bot/app.py")
    assert ws.changed == {"support_bot/app.py"}
