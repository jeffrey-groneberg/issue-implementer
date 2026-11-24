import os

import pytest
from copilot.session_events import (
    PermissionRequestMcp,
    PermissionRequestRead,
    PermissionRequestShell,
    PermissionRequestUrl,
    PermissionRequestWrite,
    PermissionWriteFileContent,
    PermissionWriteFileEdit,
)

from copilot_session import agent_definitions, available_tools, denial_reason, sandbox_config


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    return os.path.realpath(tmp_path)


def write(path, *edits):
    return PermissionRequestWrite(
        can_offer_session_approval=False, diff="", file_name=path, intention="edit",
        file_edits=[PermissionWriteFileEdit(before=PermissionWriteFileContent("", b),
                                            after=PermissionWriteFileContent("", a))
                    for b, a in edits])


def read(path, **kwargs):
    return PermissionRequestRead(intention="read", path=path, **kwargs)


def shell(**kwargs):
    return PermissionRequestShell(
        can_offer_session_approval=False, commands=[], full_command_text="python -m pytest -q",
        has_write_file_redirection=False, intention="run tests", possible_paths=[],
        possible_urls=[], **kwargs)


def test_writes_stay_in_the_repository_outside_git_and_workflows(ws):
    assert denial_reason(write(f"{ws}/src/app.py"), ws, (), True) is None
    assert denial_reason(write("src/app.py"), ws, (), True) is None
    for path in (f"{ws}/.git/config", f"{ws}/.git/hooks/pre-commit",
                 f"{ws}/.github/workflows/ci.yml", f"{ws}/../outside.py", "/etc/hosts"):
        assert denial_reason(write(path), ws, (), True), path


def test_every_path_of_a_multi_file_edit_is_checked(ws):
    rename = write(f"{ws}/a.py", (f"{ws}/a.py", f"{ws}/.github/workflows/a.yml"))
    assert denial_reason(rename, ws, (), True)


def test_symlinks_cannot_escape_the_repository(ws, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    os.symlink(outside, os.path.join(ws, "link"))
    assert denial_reason(write(f"{ws}/link/x.py"), ws, (), True)
    assert denial_reason(read(f"{ws}/link/secret.txt"), ws, (), True)


def test_reads_are_limited_to_the_repository_and_skills(ws, tmp_path_factory):
    skills = os.path.realpath(tmp_path_factory.mktemp("skills"))
    assert denial_reason(read(f"{ws}/README.md"), ws, (skills,), False) is None
    assert denial_reason(read(f"{skills}/issue-planner/SKILL.md"), ws, (skills,), False) is None
    assert denial_reason(read(os.path.expanduser("~/.ssh/id_rsa")), ws, (skills,), False)


def test_shell_needs_the_sandbox_and_cannot_leave_it(ws):
    assert denial_reason(shell(), ws, (), True) is None
    assert denial_reason(shell(), ws, (), False)
    assert denial_reason(shell(request_sandbox_bypass=True), ws, (), True)
    assert denial_reason(shell(request_sandbox_permissive=True), ws, (), True)


def test_managed_approval_urls_and_mcp_are_rejected(ws):
    assert denial_reason(read(f"{ws}/a.py", managed_approval_required=True), ws, (), True)
    assert denial_reason(PermissionRequestUrl(intention="fetch", url="https://example.com"),
                         ws, (), True)
    assert denial_reason(PermissionRequestMcp(read_only=True, server_name="s", tool_name="t",
                                              tool_title="t"), ws, (), True)


def test_agents_expose_least_privilege_tools():
    planner, implementer = agent_definitions(shell=False)
    assert available_tools(planner).to_list() == [
        "builtin:view", "builtin:glob", "builtin:grep", "custom:submit_plan"]
    assert "bash" not in implementer["tools"]
    assert "bash" in agent_definitions(shell=True)[1]["tools"]
    assert "custom:submit_pull_request" in available_tools(implementer).to_list()


def test_sandbox_has_no_network_and_a_read_only_git_dir(ws):
    config = sandbox_config(ws).to_dict()
    assert config["allowBypass"] is False
    assert config["userPolicy"]["filesystem"]["readwritePaths"] == [ws]
    assert set(config["userPolicy"]["filesystem"]["readonlyPaths"]) == {
        f"{ws}/.git", f"{ws}/.github/workflows"}
    assert config["userPolicy"]["network"]["allowOutbound"] is False
    assert config["userPolicy"]["network"]["allowLocalNetwork"] is False
