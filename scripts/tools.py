"""The agent's only way to hand results back: two terminal custom tools.

define_tool(..., is_terminal=True, skip_permission=True): a successful call ends the
agent's turn, and the tool runs without a permission request because the action owns it.
The tools validate and record; the action posts the plan or opens the pull request after
the session ends, so the model never calls GitHub itself.
"""

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from copilot import define_tool
from copilot.tools import ToolResult
from github_ops import AGENT_MARKER, PLAN_END, PLAN_START

MAX_PLAN_CHARS = 60_000
CONVENTIONAL_COMMIT = re.compile(
    r"^(?:feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(?:\([^)]+\))?!?: \S"
)


@dataclass
class Submission:
    plan: str = ""
    title: str = ""
    commit_message: str = ""
    summary: str = ""
    tests: str = ""
    files: list[str] = field(default_factory=list)


class PlanParams(BaseModel):
    plan_markdown: str = Field(
        description="The complete implementation plan in Markdown, in your skill's format."
    )


class PullRequestParams(BaseModel):
    title: str = Field(description="Pull request title without a prefix, e.g. 'Add a power function'.")
    commit_message: str = Field(
        description="Conventional Commits subject line, e.g. 'feat: add power function'."
    )
    summary_markdown: str = Field(description="What changed and why, for the pull request.")
    tests: str = Field(default="", description="Tests you ran and their result, or why none ran.")


def _reject(message: str) -> ToolResult:
    return ToolResult(text_result_for_llm=message, result_type="failure", error=message)


def submit_plan_tool(out: Submission):
    @define_tool(
        "submit_plan",
        description="Submit the complete implementation plan; the action posts it on the issue. "
        "Call it once. A successful call ends your turn.",
        is_terminal=True,
        skip_permission=True,
    )
    async def submit_plan(params: PlanParams):
        plan = params.plan_markdown
        for marker in (AGENT_MARKER, PLAN_START, PLAN_END):
            plan = plan.replace(marker, "")
        plan = plan.strip()
        if len(plan) < 80:
            return _reject("The plan is too short. Submit the complete plan in your skill's format.")
        if len(plan) > MAX_PLAN_CHARS:
            return _reject(f"The plan has {len(plan)} characters; keep it under {MAX_PLAN_CHARS}.")
        out.plan = plan
        return "Plan received. The action posts it on the issue."

    return submit_plan


def submit_pull_request_tool(out: Submission, check_changes: Callable[[], list[str]]):
    """check_changes stages the working tree and returns the changed files, or raises
    ValueError with feedback the model can act on."""

    @define_tool(
        "submit_pull_request",
        description="Submit your changes; the action commits every change in the working tree "
        "to the branch copilot/issue-<number> and opens a draft pull request that closes the "
        "issue. Call it once, after implementing and testing. A successful call ends your turn.",
        is_terminal=True,
        skip_permission=True,
    )
    async def submit_pull_request(params: PullRequestParams):
        title = " ".join(params.title.split())[:200]
        message = (params.commit_message.strip().splitlines() or [""])[0][:200]
        if not title:
            return _reject("A title is required.")
        if not CONVENTIONAL_COMMIT.match(message):
            return _reject("commit_message must be a Conventional Commits subject, "
                           "e.g. 'feat: add power function'.")
        try:
            files = await asyncio.to_thread(check_changes)
        except ValueError as exc:
            return _reject(str(exc))
        out.title, out.commit_message, out.files = title, message, files
        out.summary, out.tests = params.summary_markdown.strip(), params.tests.strip()
        return (f"Accepted {len(files)} changed file(s). The action commits them and opens the "
                "draft pull request.")

    return submit_pull_request
