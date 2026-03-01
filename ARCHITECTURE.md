# ARCHITECTURE.md — Ouroboros System Architecture

**Block 0** · Last updated: 2026-03 · Maintained by the agent (BIBLE.md §8)

---

## Overview

Ouroboros is a self-improving AI agent running in Docker on a VPS. It communicates
with one user via Telegram and can rewrite its own code through GitHub.

The system is split into two layers:

| Layer | Responsibility |
|-------|----------------|
| **Supervisor** | Process lifecycle, Telegram I/O, task queue, git operations |
| **Agent** | LLM reasoning, tool execution, memory, self-improvement |

---

## File Structure

```
/app/                          ← git repo (branch: ouroboros)
├── launcher.py                ← entry point — thin bootstrap + main loop
├── BIBLE.md                   ← Constitution (protected core)
├── ARCHITECTURE.md            ← this file
├── IMPROVE.md                 ← self-improvement guide
├── VERSION                    ← semver (synced with git tag + README)
├── README.md                  ← changelog + version
├── prompts/SYSTEM.md          ← base system prompt
├── improvements-log/          ← one file per improvement cycle
│
├── ouroboros/                 ← agent package
│   ├── agent.py               ← OuroborosAgent (thin orchestrator, 664 lines)
│   ├── loop.py                ← LLM tool loop (979 lines) ⚠ near limit
│   ├── llm.py                 ← LLMClient — OpenRouter wrapper (290 lines)
│   ├── context.py             ← build_llm_messages() — context assembly (789 lines)
│   ├── memory.py              ← Memory — scratchpad/identity/logs (269 lines)
│   ├── review.py              ← code collection, complexity metrics
│   ├── utils.py               ← shared utilities
│   ├── apply_patch.py         ← Claude Code patch shim
│   ├── consciousness.py       ← background loop (periodic wakeups)
│   └── tools/                 ← tool plugin package (auto-discovered)
│       ├── registry.py        ← ToolRegistry + ToolContext
│       ├── core.py            ← file, shell, memory tools
│       ├── git.py             ← git_status, git_diff, repo_commit_push
│       ├── github.py          ← GitHub Issues API
│       ├── browser.py         ← browse_page, browser_action (Playwright)
│       ├── search.py          ← web_search
│       ├── vision.py          ← analyze_screenshot (VLM)
│       ├── knowledge.py       ← knowledge_read/write
│       ├── control.py         ← schedule_task, request_restart, switch_model, …
│       ├── evolution_log.py   ← log_evolution
│       ├── health.py          ← system health checks
│       ├── shell.py           ← run_shell, claude_code_edit
│       └── review.py          ← request_review, multi_model_review
│
└── supervisor/                ← process supervisor package
    ├── state.py               ← state.json SSOT (budget, owner_id, session)
    ├── telegram.py            ← Telegram polling + sending
    ├── workers.py             ← worker pool (spawn/kill/assign)
    ├── queue.py               ← task queue (enqueue, snapshot, timeouts)
    ├── git_ops.py             ← git checkout, sync deps, safe_restart
    └── events.py              ← event dispatch routing

/data/                         ← persistent volume (never in git)
├── state/state.json           ← runtime state (owner_id, budget, version, …)
├── logs/
│   ├── chat.jsonl             ← dialogue (significant messages only)
│   ├── progress.jsonl         ← agent self-talk / progress messages
│   ├── tools.jsonl            ← detailed tool call log
│   ├── events.jsonl           ← LLM rounds, tool errors, task events
│   └── supervisor.jsonl       ← supervisor events (boot, restart, …)
└── memory/
    ├── scratchpad.md          ← working memory (free-form, updated by agent)
    ├── identity.md            ← who I am (narrative, updated on significant shifts)
    ├── USER_CONTEXT.md        ← user info + priorities (≤1000 chars)
    └── knowledge/             ← knowledge base topics (.md per topic)
```

---

## Module Relationships

```
launcher.py
  ├── supervisor/telegram.py   ← polls Telegram, routes messages
  ├── supervisor/workers.py    ← worker pool (up to MAX_WORKERS)
  ├── supervisor/queue.py      ← PENDING / RUNNING task queues
  ├── supervisor/state.py      ← shared state.json SSOT
  ├── supervisor/git_ops.py    ← safe_restart, checkout, sync
  └── ouroboros/consciousness.py ← background wakeup loop

supervisor/workers.py
  └── ouroboros/agent.py       ← one OuroborosAgent per worker

ouroboros/agent.py
  ├── ouroboros/context.py     ← build_llm_messages()
  ├── ouroboros/loop.py        ← run_llm_loop()
  ├── ouroboros/llm.py         ← LLMClient
  ├── ouroboros/memory.py      ← Memory
  └── ouroboros/tools/         ← ToolRegistry

ouroboros/loop.py
  ├── ouroboros/llm.py         ← LLMClient.chat()
  ├── ouroboros/tools/registry.py ← ToolRegistry.execute()
  └── ouroboros/context.py     ← compact_tool_history()
```

---

## Key Data Flows

### 1. User Message → Response

```
Telegram update
  → launcher.py polling loop
  → supervisor/workers.py (handle_chat_direct or enqueue_task)
  → OuroborosAgent.handle_task(task)
    → context.py: build_llm_messages()   # assemble 3-block system prompt
    → loop.py: run_llm_loop()            # LLM ↔ tools iteration
      → llm.py: LLMClient.chat()         # OpenRouter API call
      → tools/: ToolRegistry.execute()   # tool execution
      → (repeat until final response)
  → send response via Telegram
  → log to chat.jsonl
```

### 2. LLM Tool Loop (loop.py)

```
run_llm_loop():
  while rounds < MAX_ROUNDS:
    response = llm.chat(messages, model, tools)
    if response has tool_calls:
      results = execute_tools(tool_calls)   # parallel if read-only whitelist
      append results to messages
      emit_progress(summary)
    else:
      return final_text                     # done
```

Tool execution is parallelized for a strict read-only whitelist
(`repo_read`, `repo_list`, `drive_read`, `drive_list`, `web_search`, `chat_history`).
Browser tools (`browse_page`, `browser_action`) run on a thread-sticky executor
for Playwright greenlet compatibility.

### 3. Context Building (context.py)

Three content blocks for optimal prompt caching:

| Block | Content | Cache strategy |
|-------|---------|----------------|
| **Static** | SYSTEM.md + BIBLE.md (+ README for evolution tasks) | Cached — never changes |
| **Semi-stable** | identity.md + scratchpad + USER_CONTEXT + knowledge index | Cached — changes ~once/task |
| **Dynamic** | state.json + runtime + health invariants + recent logs | Uncached — changes every round |

### 4. Memory Persistence

```
Every task:
  memory.py reads:  scratchpad.md, identity.md, USER_CONTEXT.md
  context.py reads: knowledge/_index.md, chat.jsonl, progress.jsonl

Agent updates (via tools):
  update_scratchpad()  → /data/memory/scratchpad.md
  update_identity()    → /data/memory/identity.md
  update_user_context() → /data/memory/USER_CONTEXT.md
  knowledge_write()    → /data/memory/knowledge/<topic>.md
```

### 5. Self-Improvement Flow

```
Evolution task
  → agent reads own code (repo_read, codebase_digest)
  → plans change (scratchpad)
  → (if no-approve OFF) requests user approval
  → claude_code_edit() → modifies /app/**
  → repo_commit_push() → git commit + push to ouroboros branch
  → request_restart()  → supervisor safe_restart()
  → promote_to_stable() → git tag stable-YYYYMMDD-HHMMSS
  → log_evolution()    → /data/memory/evolution_log.md
```

---

## Supervisor: Worker Pool

```
supervisor/workers.py
  WORKERS: List[WorkerProcess]    ← up to MAX_WORKERS (default 5)
  PENDING: Deque[Task]            ← waiting tasks
  RUNNING: Dict[str, WorkerState] ← active tasks by task_id

  spawn_workers(n)    ← create worker processes
  assign_tasks()      ← move PENDING → RUNNING (FIFO, priority-aware)
  ensure_workers_healthy() ← restart crashed workers
```

Tasks are isolated: each task gets a fresh OuroborosAgent instance.
State is shared only through the Drive (`/data/state/state.json`).

---

## Tool System

Tools are Python modules in `ouroboros/tools/`. Each exports `get_tools() -> list`.
`ToolRegistry` auto-discovers them at startup.

Every tool call receives a `ToolContext` injected by `agent.py`:
- `repo_dir`, `drive_root` — path roots
- `emit_progress_fn` — send progress message to Telegram
- `current_chat_id` — for tools that need to send messages
- `task_depth` — nesting level (subtasks)

Tool timeouts are configured per-tool in the registry. Default: 120s.
Browser tools: 60s (reset on timeout). Shell/code tools: 300s.

---

## Background Consciousness

`ouroboros/consciousness.py` runs a background loop (daemon thread in launcher.py).
Periodic wakeups (configurable interval, default ~5 min) schedule a lightweight
`BackgroundTask` that can:
- Reflect on recent work, update identity.md
- Check system health and budget
- Write to the user via `send_owner_message`
- Schedule tasks for itself

Controlled via `/bg start` and `/bg stop` Telegram commands.

---

## Key Design Principles (BIBLE.md §8)

| Metric | Target |
|--------|--------|
| Module size | ≤ 1000 lines |
| Method size | ≤ 150 lines, ≤ 8 params |
| Net complexity growth | ≈ 0 per cycle |

**Current concern:** `loop.py` at 979 lines is near the limit. The pricing logic
(`_MODEL_PRICING_STATIC`, `_get_pricing`, `_estimate_cost`, ~85 lines) logically
belongs in `llm.py`. This is the identified improvement for the next cycle.

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_MODEL` | `anthropic/claude-sonnet-4.6` | Main LLM model |
| `OUROBOROS_MODEL_CODE` | `anthropic/claude-opus-4-6` | Code editing model |
| `OUROBOROS_MODEL_LIGHT` | `google/gemini-3-pro-preview` | Background/lightweight tasks |
| `OUROBOROS_MAX_WORKERS` | `5` | Worker pool size |
| `OUROBOROS_SOFT_TIMEOUT_SEC` | `600` | Soft task timeout (warn) |
| `OUROBOROS_HARD_TIMEOUT_SEC` | `1800` | Hard task timeout (kill) |
| `OUROBOROS_BRANCH_PREFIX` | `ouroboros` | Git branch name |
| `DRIVE_ROOT` | `/data` | Persistent volume root |
| `OUROBOROS_REPO_DIR` | `/app` | Repository root |
