from __future__ import annotations

from monitoring.status_file import get_git_commit, read_status_file, write_status_file


def test_get_git_commit_returns_stripped_output(mocker, tmp_path):
    mock_run = mocker.patch("monitoring.status_file.subprocess.run")
    mock_run.return_value.stdout = "abc123\n"
    mock_run.return_value.returncode = 0

    commit = get_git_commit(tmp_path)

    assert commit == "abc123"


def test_get_git_commit_returns_none_on_failure(mocker, tmp_path):
    mocker.patch("monitoring.status_file.subprocess.run", side_effect=OSError("no git"))

    assert get_git_commit(tmp_path) is None


def test_write_and_read_status_file_roundtrip(mocker, tmp_path):
    mocker.patch("monitoring.status_file.get_git_commit", return_value="abc123")
    mocker.patch("monitoring.status_file.os.getpid", return_value=4242)
    path = tmp_path / "bot_status.json"

    write_status_file(path=path, repo_dir=tmp_path)
    status = read_status_file(path=path)

    assert status["pid"] == 4242
    assert status["commit"] == "abc123"
    assert "started_at" in status


def test_write_status_file_does_not_raise_on_write_failure(mocker, tmp_path):
    mocker.patch("monitoring.status_file.get_git_commit", return_value="abc123")
    bad_path = tmp_path / "no_such_dir" / "bot_status.json"  # parent doesn't exist

    write_status_file(path=bad_path, repo_dir=tmp_path)  # must not raise


def test_read_status_file_returns_none_when_missing(tmp_path):
    assert read_status_file(path=tmp_path / "missing.json") is None
