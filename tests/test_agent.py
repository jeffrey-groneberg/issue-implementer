import asyncio

import pytest
from copilot.tools import ToolInvocation

from agent import format_issue, load_settings
from github_ops import AGENT_MARKER, PLAN_START
from tools import Submission, submit_plan_tool, submit_pull_request_tool


@pytest.fixture
def env(monkeypatch):
    for name, value in {"ISSUE_NUMBER": "7", "GH_TOKEN": "gh-token", "GITHUB_REPOSITORY": "o/r"}.items():
        monkeypatch.setenv(name, value)
    for name in ("COPILOT_TOKEN", "MODEL", "MAX_AI_CREDITS", "REASONING_EFFORT", "COMMENT_AUTHOR"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_settings_defaults_and_validation(env):
    env.setenv("MAX_AI_CREDITS", "5")
    env.setenv("REASONING_EFFORT", "extreme")
    s = load_settings()
    assert (s.model, s.copilot_token, s.max_ai_credits, s.reasoning_effort) == (
        "auto", "gh-token", 30.0, "")
    assert (s.session_id, s.branch) == ("copilot-issue-7", "copilot/issue-7")


def test_settings_reject_unsafe_input(env):
    env.setenv("COMMENT_AUTHOR", "x; rm -rf /")
    with pytest.raises(SystemExit):
        load_settings()


def test_issue_context_is_fenced_and_skips_agent_comments():
    text = format_issue({"number": 3, "title": "Add pow", "author": "a", "labels": [],
                         "body": "please", "comments": [
                             {"author": "b", "body": "also docs"},
                             {"author": "github-actions[bot]", "body": f"{AGENT_MARKER}\nplan"}]})
    assert text.index("<<<ISSUE") < text.index("Add pow") < text.index("ISSUE>>>")
    assert "also docs" in text and "plan" not in text.split("also docs")[1]


def call(tool, **arguments):
    return asyncio.run(tool.handler(ToolInvocation(arguments=arguments)))


def test_submit_plan_validates_and_strips_markers():
    out = Submission()
    tool = submit_plan_tool(out)
    assert tool.is_terminal and tool.skip_permission
    assert call(tool, plan_markdown="too short").result_type == "failure"
    call(tool, plan_markdown=f"{PLAN_START}\n## 🤖 Implementation Plan\n" + "step\n" * 30)
    assert out.plan.startswith("## 🤖 Implementation Plan") and PLAN_START not in out.plan


def test_submit_pull_request_relays_publishing_feedback():
    out = Submission()

    def no_changes():
        raise ValueError("There are no changes to publish.")

    tool = submit_pull_request_tool(out, no_changes)
    bad = call(tool, title="Add pow", commit_message="added pow", summary_markdown="s")
    assert bad.result_type == "failure" and "Conventional Commits" in bad.text_result_for_llm
    rejected = call(tool, title="Add pow", commit_message="feat: add pow", summary_markdown="s")
    assert "no changes" in rejected.text_result_for_llm and not out.files
    ok = call(submit_pull_request_tool(out, lambda: ["src/calc.py"]), title="Add pow",
              commit_message="feat: add pow", summary_markdown="s", tests="3 passed")
    assert out.files == ["src/calc.py"] and out.tests == "3 passed" and "Accepted" in str(ok)
