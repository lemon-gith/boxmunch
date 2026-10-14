"""Tests for boxmunge add-git-project command."""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from boxmunge.commands.add_git_project_cmd import (
    cmd_add_git_project, run_add_git_project, validate_ref, validate_repo_url,
)
from boxmunge.fileutil import project_lock
from boxmunge.log import _reset_logger
from boxmunge.paths import BoxPaths
from boxmunge.state import read_state

VALID_MANIFEST = """\
id: 01TESTULID0000000000000000
project: testapp
source: git
repo: git@github.com:org/testapp.git
ref: main
hosts:
  - testapp.example.com
services:
  web:
    port: 8080
    routes:
      - path: /
"""

class TestRunAddGitProject:
    @patch("boxmunge.commands.add_git_project_cmd.subprocess.run")
    def test_creates_project_dir(self, mock_run: MagicMock,
                                  paths: BoxPaths, tmp_path: Path) -> None:
        def fake_clone(cmd, **kwargs):
            if cmd[0] == "git" and cmd[1] == "clone":
                repo_dir = Path(cmd[-1])
                repo_dir.mkdir(parents=True, exist_ok=True)
                (repo_dir / "manifest.yml").write_text(VALID_MANIFEST)
                (repo_dir / "compose.yml").write_text(
                    "services:\n  web:\n    image: nginx\n"
                )
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = fake_clone

        result = run_add_git_project(
            "testapp", "git@github.com:org/testapp.git", paths, ref="main"
        )
        assert result == 0
        assert paths.project_dir("testapp").exists()

    @patch("boxmunge.commands.add_git_project_cmd.subprocess.run")
    def test_records_deploy_state(self, mock_run: MagicMock,
                                   paths: BoxPaths, tmp_path: Path) -> None:
        def fake_clone(cmd, **kwargs):
            if cmd[0] == "git" and cmd[1] == "clone":
                repo_dir = Path(cmd[-1])
                repo_dir.mkdir(parents=True, exist_ok=True)
                (repo_dir / "manifest.yml").write_text(VALID_MANIFEST)
                (repo_dir / "compose.yml").write_text(
                    "services:\n  web:\n    image: nginx\n"
                )
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = fake_clone

        run_add_git_project(
            "testapp", "git@github.com:org/testapp.git", paths, ref="main"
        )
        state = read_state(paths.project_deploy_state("testapp"))
        assert state.get("project_id") == "01TESTULID0000000000000000"

    def test_rejects_existing_project(self, paths: BoxPaths) -> None:
        pdir = paths.project_dir("testapp")
        pdir.mkdir(parents=True)
        (pdir / "manifest.yml").write_text("project: testapp\n")

        result = run_add_git_project(
            "testapp", "git@github.com:org/testapp.git", paths
        )
        assert result == 1


class TestRepoUrlValidation:
    @pytest.mark.parametrize("url", [
        "https://github.com/org/repo.git",
        "https://gitlab.com/group/sub/repo",
        "ssh://git@github.com/org/repo.git",
        "git@github.com:org/repo.git",
    ])
    def test_accepts_remote_urls(self, url: str) -> None:
        assert validate_repo_url(url) is None

    @pytest.mark.parametrize("url", [
        "",
        "-u",
        "--upload-pack=touch /tmp/x",
        "file:///opt/boxmunge/projects/other/repo",
        "/opt/boxmunge/projects/other/repo",
        "../other/repo",
        "~/repo",
        "ext::sh -c 'id'",
        "fd::17/foo",
        "git://github.com/org/repo.git",
        "http://github.com/org/repo.git",
        "github.com:org/repo.git",
        "https://github.com/org/repo .git",
        "https://github.com/org/repo\n",
    ])
    def test_rejects_unsafe_urls(self, url: str) -> None:
        assert validate_repo_url(url) is not None

    @pytest.mark.parametrize("ref", ["-x", "--upload-pack=x", "", "a b"])
    def test_rejects_bad_refs(self, ref: str) -> None:
        assert validate_ref(ref) is not None

    @pytest.mark.parametrize("ref", ["main", "v1.2.3", "feature/x"])
    def test_accepts_good_refs(self, ref: str) -> None:
        assert validate_ref(ref) is None


class TestRunAddGitProjectHardening:
    @pytest.mark.parametrize("name", ["../evil", "Evil", "a/b", "", "-x"])
    @patch("boxmunge.commands.add_git_project_cmd.subprocess.run")
    def test_rejects_invalid_project_name(
        self, mock_run: MagicMock, name: str, paths: BoxPaths,
    ) -> None:
        result = run_add_git_project(name, "https://github.com/o/r.git", paths)
        assert result == 2
        mock_run.assert_not_called()

    @patch("boxmunge.commands.add_git_project_cmd.subprocess.run")
    def test_rejects_option_like_url_without_cloning(
        self, mock_run: MagicMock, paths: BoxPaths,
    ) -> None:
        assert run_add_git_project("testapp", "-u", paths) == 2
        assert run_add_git_project(
            "testapp", "file:///etc", paths) == 2
        mock_run.assert_not_called()
        assert not paths.project_dir("testapp").exists()

    @patch("boxmunge.commands.add_git_project_cmd.subprocess.run")
    def test_rejects_option_like_ref(
        self, mock_run: MagicMock, paths: BoxPaths,
    ) -> None:
        result = run_add_git_project(
            "testapp", "https://github.com/o/r.git", paths, ref="--upload-pack=x")
        assert result == 2
        mock_run.assert_not_called()

    @patch("boxmunge.commands.add_git_project_cmd.subprocess.run")
    def test_clone_uses_double_dash_and_protocol_allowlist(
        self, mock_run: MagicMock, paths: BoxPaths,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        # Clone "succeeds" but writes no manifest, so the run fails afterwards;
        # we only care how git was invoked.
        run_add_git_project("testapp", "https://github.com/o/r.git", paths)
        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["git", "clone"]
        assert cmd[-3] == "--"
        assert cmd[-2] == "https://github.com/o/r.git"
        assert mock_run.call_args.kwargs["env"]["GIT_ALLOW_PROTOCOL"] == "https:ssh"

    @patch("boxmunge.commands.add_git_project_cmd.subprocess.run")
    def test_fails_fast_when_project_lock_held(
        self, mock_run: MagicMock, paths: BoxPaths,
    ) -> None:
        with project_lock("testapp", paths):
            result = run_add_git_project(
                "testapp", "https://github.com/o/r.git", paths)
        assert result == 1
        mock_run.assert_not_called()


class TestAddGitProjectLogging:
    def setup_method(self):
        _reset_logger()

    def teardown_method(self):
        _reset_logger()

    @patch("boxmunge.commands.add_git_project_cmd.subprocess.run")
    def test_uses_add_git_project_component(
        self, mock_run: MagicMock, paths: BoxPaths,
    ) -> None:
        """Component must match the CLI verb 'add-git-project' (not 'add-project')."""
        def fake_clone(cmd, **kwargs):
            if cmd[0] == "git" and cmd[1] == "clone":
                repo_dir = Path(cmd[-1])
                repo_dir.mkdir(parents=True, exist_ok=True)
                (repo_dir / "manifest.yml").write_text(VALID_MANIFEST)
                (repo_dir / "compose.yml").write_text(
                    "services:\n  web:\n    image: nginx\n"
                )
            return MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = fake_clone

        run_add_git_project(
            "testapp", "git@github.com:org/testapp.git", paths, ref="main"
        )
        entries = [
            json.loads(line)
            for line in paths.log_file.read_text().strip().splitlines()
            if line
        ]
        components = {e.get("component") for e in entries}
        assert "add-git-project" in components
        assert "add-project" not in components


class TestErrorOutputGoesToStderr:
    """Audit H-4: ERROR prints in commands/*.py must go to stderr.

    Single integration check covering the sweep — exhaustive per-line
    coverage would be mechanical noise. add-git-project with no args is
    a representative example and exercises ``cmd_add_git_project``.
    """

    def test_missing_args_writes_to_stderr(self, capfd) -> None:
        # Empty args path exits before touching any boxmunge state, so no
        # monkeypatch fixture is needed — only the usage error itself.
        with pytest.raises(SystemExit) as exc:
            cmd_add_git_project([])
        assert exc.value.code == 2
        captured = capfd.readouterr()
        # Usage prints to stderr
        assert "Usage" in captured.err
        assert captured.out == ""
