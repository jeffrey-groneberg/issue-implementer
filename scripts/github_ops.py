"""GitHub and git operations. They run in the action's process, outside the sandbox.

The token goes only to the gh and git subprocesses that need it. Local git commands
run with hooks and fsmonitor disabled and without credentials, so nothing the agent
left in the working tree can run with the token.
"""

import base64
import hashlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

logger = logging.getLogger("copilot-agent")

AGENT_MARKER = "<!-- copilot-issue-agent -->"
PLAN_START = "<!-- copilot:plan -->"
PLAN_END = "<!-- /copilot:plan -->"
ACTIONS_BOT = "github-actions[bot]"
SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "API_KEY", "CREDENTIAL")
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
SAFE_GIT = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
# Build and test artifacts that must never end up in the agent's pull request.
STAGE_EXCLUDES = (
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.pyc",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/node_modules/**",
)


class GitHubError(RuntimeError):
    pass


@dataclass
class PlanComment:
    comment_id: int | None
    plan: str


def extract_plan(body: str) -> str | None:
    if PLAN_START not in (body or ""):
        return None
    start = body.index(PLAN_START) + len(PLAN_START)
    end = body.find(PLAN_END, start)
    return (body[start:end] if end != -1 else body[start:]).strip()


def normalize_issue(issue: dict, comments: list[dict]) -> dict:
    """Flatten REST issue and comment payloads."""
    return {
        "number": issue.get("number"),
        "title": issue.get("title") or "",
        "body": issue.get("body") or "",
        "author": (issue.get("user") or {}).get("login", ""),
        "labels": [label.get("name", "") for label in issue.get("labels") or []],
        "comments": [
            {
                "id": c.get("id"),
                "author": (c.get("user") or {}).get("login", ""),
                "association": c.get("author_association") or "",
                "created_at": c.get("created_at") or "",
                "body": c.get("body") or "",
            }
            for c in comments
        ],
    }


class GitHub:
    def __init__(self, repo: str, server_url: str, token: str, workspace: str,
                 base_env: dict | None = None):
        self.repo, self.server_url, self.workspace = repo, server_url, workspace
        self._token = token
        host = urlparse(server_url).hostname or "github.com"
        env = dict(os.environ if base_env is None else base_env)
        self._git_env = {name: value for name, value in env.items()
                         if not any(marker in name.upper() for marker in SECRET_MARKERS)}
        self._git_env["GIT_TERMINAL_PROMPT"] = "0"
        self._gh_env = {**env, "GH_TOKEN": token, "GH_PROMPT_DISABLED": "1",
                        "GH_NO_UPDATE_NOTIFIER": "1"}
        if host != "github.com":
            self._gh_env.update({"GH_HOST": host, "GH_ENTERPRISE_TOKEN": token})

    def _run(self, args: list[str], env: dict, input_text: str | None = None,
             check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
        result = subprocess.run(args, input=input_text, capture_output=True, text=True,
                                cwd=self.workspace, env=env, timeout=180)
        if result.returncode != 0:
            message = (f"`{' '.join(args[:4])}` failed ({result.returncode}): "
                       f"{result.stderr.strip()[:400]}")
            if check:
                raise GitHubError(message)
            if not quiet:
                logger.warning(message)
        return result

    # GitHub API

    def api(self, path: str, method: str = "GET", payload: dict | None = None,
            check: bool = True):
        args = ["gh", "api", path, "--method", method]
        if payload is not None:
            args += ["--input", "-"]
        result = self._run(args, self._gh_env, json.dumps(payload) if payload else None, check)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)

    def issue(self, number: int) -> dict:
        issue = self.api(f"repos/{self.repo}/issues/{number}")
        out = self._run(["gh", "api", f"repos/{self.repo}/issues/{number}/comments?per_page=100",
                         "--paginate", "--jq", ".[]"], self._gh_env).stdout
        return normalize_issue(issue, [json.loads(line) for line in out.splitlines() if line])

    def comment(self, number: int, body: str) -> dict:
        return self.api(f"repos/{self.repo}/issues/{number}/comments", "POST", {"body": body})

    def react(self, number: int, content: str, comment_id: int | None = None) -> None:
        target = f"issues/comments/{comment_id}" if comment_id else f"issues/{number}"
        self.api(f"repos/{self.repo}/{target}/reactions", "POST", {"content": content},
                 check=False)

    def set_labels(self, number: int, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()):
        """Add and remove labels in one call; removing a label the issue lacks is fine."""
        args = ["gh", "issue", "edit", str(number), "--repo", self.repo]
        for label in add:
            args += ["--add-label", label]
        for label in remove:
            args += ["--remove-label", label]
        if add or remove:
            self._run(args, self._gh_env, check=False)

    def self_login(self) -> str | None:
        """Login behind the token; None for installation tokens such as GITHUB_TOKEN."""
        if not hasattr(self, "_login"):
            result = self._run(["gh", "api", "user", "--jq", ".login"], self._gh_env,
                               check=False, quiet=True)
            self._login = result.stdout.strip() if result.returncode == 0 else None
        return self._login

    def plan_comments(self, issue: dict) -> list[PlanComment]:
        """Plans posted by the agent's own identity, oldest first; markers in anyone
        else's comments are ignored, so a plan cannot be forged."""
        trusted = {ACTIONS_BOT, self.self_login()}
        return [PlanComment(c["id"], plan) for c in issue["comments"]
                if c["author"] in trusted and (plan := extract_plan(c["body"])) is not None]

    def noreply_email(self, login: str) -> str:
        host = urlparse(self.server_url).hostname or "github.com"
        domain = "users.noreply.github.com" if host == "github.com" else f"users.noreply.{host}"
        user_id = (self.api(f"users/{quote(login, safe='')}", check=False) or {}).get("id")
        return f"{user_id}+{login}@{domain}" if user_id else f"{login}@{domain}"

    def open_pr(self, base: str, head: str, title: str, body: str) -> tuple[str, int]:
        """Open a draft pull request, or update the body of the one already open."""
        out = self._run(["gh", "pr", "list", "--repo", self.repo, "--head", head, "--state",
                         "open", "--json", "number,url", "--limit", "1"], self._gh_env).stdout
        existing = json.loads(out or "[]")
        if existing:
            number, url = existing[0]["number"], existing[0]["url"]
            self._run(["gh", "pr", "edit", str(number), "--repo", self.repo, "--body-file", "-"],
                      self._gh_env, body)
            return url, number
        url = self._run(["gh", "pr", "create", "--repo", self.repo, "--draft", "--base", base,
                         "--head", head, "--title", title, "--body-file", "-"],
                        self._gh_env, body).stdout.strip().splitlines()[-1]
        return url, int(url.rstrip("/").rsplit("/", 1)[-1])

    # Local git

    def git(self, *args: str, input_text: str | None = None, check: bool = True) -> str:
        """Local git without credentials, hooks or fsmonitor; check=False also stays quiet."""
        return self._run(["git", *SAFE_GIT, *args], self._git_env, input_text, check,
                         quiet=not check).stdout

    def config_fingerprint(self) -> str:
        """Hash of the repository's git configuration; publishing refuses if it changed."""
        digest = hashlib.sha256()
        git_dir = self.git("rev-parse", "--absolute-git-dir").strip()
        for name in ("config", os.path.join("info", "attributes")):
            path = Path(git_dir, name)
            digest.update(path.read_bytes() if path.exists() else b"-")
        return digest.hexdigest()

    def strip_persisted_credentials(self) -> None:
        """Remove auth headers actions/checkout persisted in .git/config so the agent
        cannot read them; fail if credentials stay reachable through includes."""
        pattern = r"^http\..*\.extraheader$"
        out = self.git("config", "--local", "--name-only", "--get-regexp", pattern, check=False)
        for key in sorted(set(out.split())):
            self.git("config", "--local", "--unset-all", key)
        if self.git("config", "--includes", "--get-regexp", pattern, check=False).strip():
            raise GitHubError("Persisted git credentials found. Use actions/checkout with "
                              "persist-credentials: false.")

    def stage(self, base: str) -> list[str]:
        """Stage every change (without build artifacts); return the files changed since base."""
        self.git("add", "-A", "--", ".", *STAGE_EXCLUDES)
        out = self.git("diff", "--cached", "--name-only", "--no-renames", "-z", base)
        return [path for path in out.split("\0") if path]

    def commit(self, branch: str, base: str, message: str) -> str:
        """One commit on top of base with everything staged, on the agent's branch."""
        self.git("checkout", "-q", "-B", branch)
        self.git("reset", "-q", "--soft", base)
        self.git("-c", "user.name=github-actions[bot]", "-c", f"user.email={BOT_EMAIL}",
                 "commit", "-q", "--no-verify", "-F", "-", input_text=message)
        return self.git("rev-parse", "HEAD").strip()

    def push(self, branch: str) -> None:
        """Force-push HEAD to the agent's branch; the token travels only in this env."""
        basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        env = {**self._git_env, "GIT_CONFIG_COUNT": "1",
               "GIT_CONFIG_KEY_0": f"http.{self.server_url}/.extraheader",
               "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}
        self._run(["git", *SAFE_GIT, "push", "--force", "origin", f"HEAD:refs/heads/{branch}"],
                  env)
