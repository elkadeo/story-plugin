"""The Story plugin's hooks: Claude Code runs this with the event's name, and the event on stdin.

- SessionStart: open this session on Story, and put its brief in front of the agent. Started by
  Story's bridge while its owner is away (STORY_WOKEN), it opens as a woken session, with what
  woke it; with a session already at work in the folder, it's told there's nothing to do.
- UserPromptSubmit, and after tools now and then: a few lines of anything new on Story.
- After TaskCreate and TaskUpdate: send the checklist, for its owner (in the background).
- After a commit or a pull request: a reminder to tell the story what happened, with the link.
- Stop: send what the turn used; and once in a while, if it committed or opened a pull request
  and Story heard nothing of it, ask it to write a note there before it stops.
- SessionEnd: close the session.

Quiet by default (Logan, 2026-11-04): it adds nothing when there's nothing to say, and never
blocks Claude Code if Story can't be reached. A folder without an agent is left alone.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from story import common  # noqa: E402
from story.relay import open_session  # noqa: E402

UPDATES_EVERY = float(os.environ.get("STORY_UPDATES_EVERY", "45"))  # seconds between checks
NUDGE_EVERY = 30 * 60.0  # at most one nudge in this long
PULL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
STORY_TOOL = re.compile(r"^mcp__plugin_story_story__")
WRITES = {  # Story's tools that write (the direct set, 2026-11-05)
    "add_story",
    "update_story",
    "move_story",
    "complete_story",
    "reopen_story",
    "write_note",
    "write_message",
    "use_shared_account",
    "post_proposal",
    "reject_proposal",
    "work_on",
    "accept_commitment",
    "decline_commitment",
    "propose_accepting",
}


def context(event: str, words: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": words}}))


def agent_and_session(given: dict[str, Any]) -> tuple:
    cwd = common.project_dir(given.get("cwd"))
    state = common.load_state()
    if not common.base_url(state):
        return cwd, None, None
    agent = common.agent_here(state, common.folder_of(cwd))
    return cwd, agent, given.get("session_id")


# ------------------------------------------------------------------ the events


def session_start(given: dict[str, Any]) -> None:
    cwd, agent, session_id = agent_and_session(given)
    if agent is None or not session_id or common.under_an_old_bridge():
        return
    brief = open_session(
        cwd, session_id, agent["key"], woken=common.woken(), woken_for=common.woken_for()
    )
    if brief:
        context("SessionStart", brief)


def updates(given: dict[str, Any], event: str, throttle: bool) -> None:
    _, agent, session_id = agent_and_session(given)
    if agent is None or not session_id or common.under_an_old_bridge():
        return
    with common.session_lock(session_id):
        kept = common.load_session(session_id)
        if throttle and common.now() - float(kept.get("checked", 0)) < UPDATES_EVERY:
            return
        kept["checked"] = common.now()
        common.save_session(session_id, kept)
    status, body = common.api(
        "GET", f"/me/sessions/{session_id}/updates", token=agent["key"], timeout=5
    )
    line = body.get("line") if status == 200 and isinstance(body, dict) else None
    if line:
        context(event, line)


def after_tool(given: dict[str, Any]) -> None:
    """After any tool: notice what matters to the story, and now and then, what's new."""
    _, agent, session_id = agent_and_session(given)
    if agent is None or not session_id:
        return
    name = str(given.get("tool_name") or "")
    with common.session_lock(session_id):
        said = _noticed(session_id, name, given)
    if said:
        context("PostToolUse", "\n".join(said))
    else:
        updates(given, "PostToolUse", throttle=True)


def _noticed(session_id: str, name: str, given: dict[str, Any]) -> list[str]:
    kept = common.load_session(session_id)
    said: list[str] = []
    if STORY_TOOL.match(name) and name.split("__")[-1] in WRITES:
        kept["told"] = common.now()  # the story heard from it
    elif name == "Bash":
        command = str((given.get("tool_input") or {}).get("command") or "")
        out = json.dumps(given.get("tool_response") or "")
        if "gh pr create" in command or "gh pr merge" in command or "git commit" in command:
            kept["moved"] = common.now()
        pulls = sorted(set(PULL.findall(out))) if "gh pr create" in command else []
        if pulls:
            said.append(
                f"Story: you opened {pulls[0]}. Write a short note on the story this session is "
                "on saying what it does, with that link attached (`links`), and point the "
                "story's link at it if that's where its work lives now."
            )
    common.save_session(session_id, kept)
    return said


def checklist(given: dict[str, Any]) -> None:
    """Claude Code's own task list, as it changes: titles and states only leave the computer."""
    _, agent, session_id = agent_and_session(given)
    if agent is None or not session_id:
        return
    with common.session_lock(session_id):
        kept = common.load_session(session_id)
        todos = _changed(kept, given)
        if todos is None or todos == kept.get("sent"):
            return
        doing = next((i.get("active") for i in todos if i["status"] == "in_progress"), None)
        status, _ = common.api(
            "PATCH",
            f"/me/sessions/{session_id}",
            {"todos": todos[:100], "doing": (doing or "")[:200]},
            token=agent["key"],
            timeout=5,
        )
        if status == 200:
            kept["sent"] = todos
        common.save_session(session_id, kept)


def _changed(kept: dict[str, Any], given: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The checklist after this task change, in the order the tasks were made. Changes can
    arrive out of order (these hooks run in the background), so an update to a task not yet seen
    makes it, and its making later only fills in its title."""
    tasks: dict[str, dict[str, Any]] = kept.get("tasks") or {}
    given_in = given.get("tool_input") or {}
    answer = given.get("tool_response") or {}
    if given.get("tool_name") == "TaskCreate":
        made = (answer.get("task") if isinstance(answer, dict) else None) or {}
        task_id = str(made.get("id") or "")
        if not task_id:
            return None
        task = tasks.setdefault(task_id, {"status": "pending"})
        task["title"] = str(given_in.get("subject") or made.get("subject") or "A step")[:300]
        if given_in.get("activeForm"):
            task["active"] = str(given_in["activeForm"])[:300]
    else:
        task_id = str(given_in.get("taskId") or "")
        if not task_id:
            return None
        status = given_in.get("status")
        if status == "deleted":
            tasks.pop(task_id, None)
        else:
            task = tasks.setdefault(task_id, {"title": "A step", "status": "pending"})
            if status in ("pending", "in_progress", "completed"):
                task["status"] = status
            if given_in.get("subject"):
                task["title"] = str(given_in["subject"])[:300]
            if given_in.get("activeForm"):
                task["active"] = str(given_in["activeForm"])[:300]
    kept["tasks"] = tasks

    def made_order(task_id: str) -> tuple[int, str]:
        return (int(task_id), "") if task_id.isdigit() else (1 << 30, task_id)

    todos = []
    for t in sorted(tasks, key=made_order):
        item = {"title": tasks[t].get("title", "A step"), "status": tasks[t]["status"]}
        if tasks[t].get("active"):
            item["active"] = tasks[t]["active"]
        todos.append(item)
    return todos


def _usage(transcript: str | None, kept: dict[str, Any]) -> dict[str, int] | None:
    """What the turn used, from Claude Code's transcript since the last report: each message
    counted once (a message's usage repeats across its parts)."""
    if not transcript or not os.path.exists(transcript):
        return None
    start = int(kept.get("usage_at", 0))
    seen = set(kept.get("usage_seen") or [])
    total = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    with open(transcript, "rb") as f:
        f.seek(start)
        chunk = f.read()
    kept["usage_at"] = start + len(chunk)
    for line in chunk.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        message = e.get("message") if isinstance(e, dict) else None
        if not isinstance(message, dict) or e.get("type") != "assistant":
            continue
        use, mid = message.get("usage"), message.get("id")
        if not isinstance(use, dict) or not mid or mid in seen:
            continue
        seen.add(mid)
        total["input_tokens"] += int(use.get("input_tokens") or 0)
        total["output_tokens"] += int(use.get("output_tokens") or 0)
        total["cache_read_tokens"] += int(use.get("cache_read_input_tokens") or 0)
        total["cache_write_tokens"] += int(use.get("cache_creation_input_tokens") or 0)
    kept["usage_seen"] = list(seen)[-200:]
    return total if any(total.values()) else None


def stop(given: dict[str, Any]) -> None:
    _, agent, session_id = agent_and_session(given)
    if agent is None or not session_id:
        return
    with common.session_lock(session_id):
        kept = common.load_session(session_id)
        used, nudge = _turn(kept, given)
        common.save_session(session_id, kept)
    if used:
        common.api("POST", "/me/usage", used, token=agent["key"], timeout=5)
    if nudge:
        print(json.dumps({"decision": "block", "reason": nudge}))


def _turn(kept: dict[str, Any], given: dict[str, Any]) -> tuple:
    """What the turn used, and the nudge, if one is due."""
    used = _usage(given.get("transcript_path"), kept)
    nudge = None
    moved, told = float(kept.get("moved", 0)), float(kept.get("told", 0))
    if (
        not given.get("stop_hook_active")
        and moved > told
        and common.now() - float(kept.get("nudged", 0)) > NUDGE_EVERY
        and kept.get("story_id")
    ):
        kept["nudged"] = common.now()
        nudge = (
            "Story: this turn committed or opened a pull request, and the story heard nothing "
            "of it. If it moved the story on, write a short note there before you stop: what "
            "was done, with links to what you made. If it didn't, just stop."
        )
    return used, nudge


def session_end(given: dict[str, Any]) -> None:
    _, agent, session_id = agent_and_session(given)
    if agent is None or not session_id:
        return
    common.api("POST", f"/me/sessions/{session_id}/close", {}, token=agent["key"], timeout=1.2)


EVENTS = {
    "SessionStart": session_start,
    "UserPromptSubmit": lambda g: updates(g, "UserPromptSubmit", throttle=False),
    "PostToolUse": after_tool,
    "Checklist": checklist,
    "Stop": stop,
    "SessionEnd": session_end,
}


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        given = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        given = {}
    run = EVENTS.get(event)
    if run is None:
        return
    try:
        run(given)
    except Exception as error:  # Story must never break Claude Code
        if os.environ.get("STORY_RELAY_LOG"):
            with open(os.environ["STORY_RELAY_LOG"], "a") as f:
                f.write(f"hook {event} failed: {error!r}\n")


if __name__ == "__main__":
    main()
