"""
E2E test harness for Ouroboros.

Sets up an isolated agent environment: copies repo to tmpdir,
creates fresh data dir, overrides dangerous tools, runs agent.handle_task,
and collects results.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class E2EResult:
    """Results collected from an E2E agent run."""
    final_response: str = ""
    events: list = field(default_factory=list)
    tools_called: List[str] = field(default_factory=list)
    git_log: str = ""
    cost_usd: float = 0.0
    rounds: int = 0
    repo_dir: Path = field(default_factory=Path)
    drive_root: Path = field(default_factory=Path)
    owner_messages: List[str] = field(default_factory=list)


def git_diff_from_initial(repo_dir: Path) -> str:
    """Return git diff from the initial commit to HEAD."""
    result = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=str(repo_dir), capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    initial_sha = result.stdout.strip().splitlines()[0]
    diff = subprocess.run(
        ["git", "diff", initial_sha, "HEAD"],
        cwd=str(repo_dir), capture_output=True, text=True,
    )
    return diff.stdout


class E2EHarness:
    """Sets up and runs an Ouroboros agent in an isolated environment."""

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.repo_dir = work_dir / "repo"
        self.drive_root = work_dir / "data"
        self._setup_done = False
        self._agent = None
        self._owner_messages: List[str] = []
        self._tools_called_before: int = 0  # track tool log offset between runs

    def _setup(self):
        """One-time setup: copy repo, init git, create data dirs, create agent."""
        if self._setup_done:
            return

        # 1. Copy source to isolated repo
        src = Path("/app") if Path("/app/ouroboros").exists() else Path(__file__).resolve().parent.parent.parent
        shutil.copytree(
            src, self.repo_dir,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".pytest_cache", "*.pyc", ".git", "node_modules",
            ),
        )

        # Init as git repo
        subprocess.run(["git", "init"], cwd=str(self.repo_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "ouroboros@test.local"], cwd=str(self.repo_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Ouroboros Test"], cwd=str(self.repo_dir), capture_output=True, check=True)
        subprocess.run(["git", "add", "-A"], cwd=str(self.repo_dir), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(self.repo_dir), capture_output=True, check=True,
        )

        # 2. Create data directories
        for subdir in ("state", "logs", "memory"):
            (self.drive_root / subdir).mkdir(parents=True, exist_ok=True)
        (self.drive_root / "state" / "state.json").write_text("{}")

        # 3. Set env vars
        os.environ["OUROBOROS_PRE_PUSH_TESTS"] = "0"

        # 4. Create agent
        from ouroboros.agent import make_agent
        agent = make_agent(
            repo_dir=str(self.repo_dir),
            drive_root=str(self.drive_root),
        )

        # 5. Override dangerous tools
        def _noop_handler(ctx, **kwargs):
            return "OK (e2e: skipped)"

        def _send_owner_message(ctx, **kwargs):
            text = kwargs.get("text", kwargs.get("message", ""))
            self._owner_messages.append(str(text))
            return "OK"

        def _local_commit(ctx, **kwargs):
            msg = kwargs.get("message", kwargs.get("msg", "e2e commit"))
            subprocess.run(["git", "add", "-A"], cwd=str(ctx.repo_dir), capture_output=True)
            result = subprocess.run(
                ["git", "commit", "-m", str(msg), "--allow-empty"],
                cwd=str(ctx.repo_dir), capture_output=True, text=True,
            )
            if result.returncode != 0:
                return f"Commit failed: {result.stderr}"
            return f"Committed: {msg}"

        agent.tools.override_handler("repo_commit_push", _local_commit)
        agent.tools.override_handler("send_owner_message", _send_owner_message)
        agent.tools.override_handler("request_restart", _noop_handler)
        agent.tools.override_handler("promote_to_stable", _noop_handler)
        agent.tools.override_handler("schedule_task", _noop_handler)
        agent.tools.override_handler("wait_for_task", _noop_handler)
        agent.tools.override_handler("cancel_task", _noop_handler)

        self._agent = agent
        self._setup_done = True

    def run(self, task_text: str, max_rounds: int = 30) -> E2EResult:
        """Run the agent on a task and return results.

        Can be called multiple times — setup happens once, subsequent calls
        reuse the same repo/data/agent (for testing persistence like memory).
        """
        self._setup()

        os.environ["OUROBOROS_MAX_ROUNDS"] = str(max_rounds)

        # Clear per-run owner messages
        run_owner_messages: List[str] = []
        prev_len = len(self._owner_messages)

        # Build task dict
        task = {
            "id": uuid.uuid4().hex[:8],
            "type": "task",
            "chat_id": 0,
            "text": task_text,
        }

        # Run the agent
        events = self._agent.handle_task(task)

        # Collect owner messages sent during this run
        run_owner_messages = self._owner_messages[prev_len:]

        # Parse results
        result = E2EResult(
            events=events,
            owner_messages=run_owner_messages,
            repo_dir=self.repo_dir,
            drive_root=self.drive_root,
        )

        # Extract final response from events
        for ev in events:
            if ev.get("type") == "send_message" and not ev.get("is_progress"):
                result.final_response = ev.get("text", ev.get("log_text", ""))

        # Extract cost and rounds from task_done event
        for ev in events:
            if ev.get("type") == "task_done":
                result.cost_usd = float(ev.get("cost_usd", 0))
                result.rounds = int(ev.get("total_rounds", 0))

        # Parse tools.jsonl for tool calls from THIS run only
        tools_log = self.drive_root / "logs" / "tools.jsonl"
        if tools_log.exists():
            all_entries = tools_log.read_text().strip().splitlines()
            new_entries = all_entries[self._tools_called_before:]
            for line in new_entries:
                try:
                    entry = json.loads(line)
                    if "tool" in entry:
                        result.tools_called.append(entry["tool"])
                except (json.JSONDecodeError, KeyError):
                    pass
            self._tools_called_before = len(all_entries)

        # Git log
        git_log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(self.repo_dir), capture_output=True, text=True,
        )
        result.git_log = git_log.stdout.strip()

        return result
