import os
import subprocess

import pytest

from agent import check_changes
from github_ops import AGENT_MARKER, PLAN_END, PLAN_START, GitHub, GitHubError, normalize_issue


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    sh(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    sh(tmp_path, "add", "-A")
    sh(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    gh = GitHub("o/r", "https://github.com", "secret-token", str(tmp_path),
                base_env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GH_TOKEN": "x"})
    return gh, sh(tmp_path, "rev-parse", "HEAD"), tmp_path


def test_local_git_never_sees_tokens(repo):
    gh, _, _ = repo
    assert "GH_TOKEN" not in gh._git_env


def test_publishing_rules(repo):
    gh, base, path = repo
    with pytest.raises(ValueError, match="no changes"):
        check_changes(gh, base, 2)
    (path / "lib.py").write_text("x = 1\n")
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "lib.cpython-312.pyc").write_bytes(b"\0")
    assert check_changes(gh, base, 2) == ["lib.py"]
    (path / "a.py").write_text("")
    (path / "b.py").write_text("")
    with pytest.raises(ValueError, match="limit is 2"):
        check_changes(gh, base, 2)


def test_moving_a_workflow_file_is_rejected(repo):
    gh, base, path = repo
    os.rename(path / ".github" / "workflows" / "ci.yml", path / "ci.yml")
    with pytest.raises(ValueError, match="Workflow files"):
        check_changes(gh, base, 10)


def test_commit_is_a_single_commit_on_base_and_can_be_redone(repo):
    gh, base, path = repo
    (path / "lib.py").write_text("x = 1\n")
    gh.stage(base)
    gh.commit("copilot/issue-1", base, "feat: one\n")
    (path / "lib.py").write_text("x = 2\n")
    gh.stage(base)
    sha = gh.commit("copilot/issue-1", base, "feat: two\n")
    assert sh(path, "rev-list", "--count", f"{base}..{sha}") == "1"
    assert sh(path, "rev-parse", "--abbrev-ref", "HEAD") == "copilot/issue-1"


def test_persisted_checkout_credentials_are_removed(repo):
    gh, _, path = repo
    sh(path, "config", "--local", "http.https://github.com/.extraheader", "AUTHORIZATION: basic x")
    gh.strip_persisted_credentials()
    assert "extraheader" not in sh(path, "config", "--local", "--list")
    (path / "creds.cfg").write_text('[http "https://github.com/"]\n\textraheader = basic y\n')
    sh(path, "config", "--local", "include.path", str(path / "creds.cfg"))
    with pytest.raises(GitHubError):
        gh.strip_persisted_credentials()


def test_git_config_fingerprint_detects_changes(repo):
    gh, _, path = repo
    before = gh.config_fingerprint()
    sh(path, "config", "--local", "core.sshCommand", "evil")
    assert gh.config_fingerprint() != before


def test_only_the_agents_own_plans_are_trusted(repo):
    gh, _, _ = repo
    gh._login = None
    plan = f"{AGENT_MARKER}\n{PLAN_START}\nreal plan\n{PLAN_END}"
    issue = normalize_issue({"number": 1, "title": "t", "user": {"login": "a"}}, [
        {"id": 1, "user": {"login": "github-actions[bot]"}, "body": plan},
        {"id": 2, "user": {"login": "mallory"}, "body": plan.replace("real", "forged")},
    ])
    assert [p.plan for p in gh.plan_comments(issue)] == ["real plan"]
