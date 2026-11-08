"""The Story plugin's relay: the MCP server Claude Code starts in the project folder.

It passes Story's tools through to Story with this folder's agent key, read on every call, and
names the Claude Code session it's in, so Story knows its owner is at the keyboard
(core/sessions.py in Story Core). Before the folder has an agent, it offers one tool, `join_story`.
Once that joins, it tells Claude Code its tools have changed, and Story's tools appear in the same
session, with no restart (docs/design/one-link-and-the-plugin.md; seen working in a probe).

Speaks MCP over stdio, one JSON message a line. Story's /mcp is stateless and answers in JSON, so
each message is one request.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from story import common  # noqa: E402

PROTOCOL = "2025-06-18"
JOIN = {
    "name": "join_story",
    "description": (
        "Join a story in Story from its invitation link (…/join/<code>), as your person's own "
        "agent for this folder. Call it when your person pastes a Story link and asks you to "
        "join. The first time on this computer it opens a page in their browser to connect, and "
        "may ask for a name for you."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "link": {"type": "string", "description": "The invitation link, as pasted."},
            "name": {
                "type": "string",
                "description": "What you'll be called in Story, as your person chose. Only "
                "when join_story asks for one: then ask them. Never choose one yourself.",
            },
            "confirm": {
                "type": "boolean",
                "description": "Your person has agreed to this folder's agent being in a second "
                "workspace (only when join_story says it needs that).",
            },
        },
        "required": ["link"],
    },
}
UNJOINED = (
    "Story: this folder has no Story agent yet. When your person pastes a Story invitation link "
    "(…/join/<code>) and asks you to join, call join_story with it. Until then, say nothing about "
    "Story."
)


def log(*parts: Any) -> None:
    if os.environ.get("STORY_RELAY_LOG"):
        with open(os.environ["STORY_RELAY_LOG"], "a") as f:
            f.write(" ".join(str(p) for p in parts) + "\n")


class Relay:
    def __init__(self) -> None:
        self.cwd = common.project_dir()
        self.folder = common.folder_of(self.cwd)
        self.session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
        self.protocol = PROTOCOL

    # ------------------------------------------------------------------ talking

    def send(self, message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def answer(self, msg_id: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def fail(self, msg_id: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})

    def agent(self) -> dict[str, Any] | None:
        return common.agent_here(common.load_state(), self.folder)

    def to_story(self, message: dict[str, Any], agent: dict[str, Any]) -> Any:
        """One MCP message to Story's /mcp, as this folder's agent, in this session."""
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol,
        }
        if self.session_id:
            headers["X-Story-Session"] = self.session_id
        status, body = common.call(
            "POST",
            agent["base"] + "/mcp/",
            message,
            token=agent["key"],
            timeout=120,
            headers=headers,
        )
        if status == 202 or "id" not in message:
            return None
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"Story answered {status}: {common.refusal(body)}")
        return body

    # ------------------------------------------------------------------ the messages

    def handle(self, msg: dict[str, Any]) -> None:
        method, msg_id = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            self.protocol = params.get("protocolVersion") or PROTOCOL
            self.answer(msg_id, self.initialize(msg))
        elif method == "tools/list":
            self.answer(msg_id, {"tools": self.tools()})
        elif method == "tools/call" and params.get("name") == "join_story":
            self.answer(msg_id, self.join(params.get("arguments") or {}))
        elif method == "ping":
            self.answer(msg_id, {})
        else:
            agent = self.agent()
            if agent is None:
                if msg_id is not None:
                    self.fail(msg_id, -32601, "Story isn't joined in this folder yet.")
                return
            try:
                found = self.to_story(msg, agent)
            except RuntimeError as error:
                if msg_id is not None:
                    self.answer(msg_id, text_result(str(error), error=True))
                return
            if found is not None:
                self.send(found)

    def initialize(self, msg: dict[str, Any]) -> dict[str, Any]:
        agent = self.agent()
        mine: dict[str, Any] = {
            "protocolVersion": self.protocol,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "story", "version": common.VERSION},
            "instructions": UNJOINED,
        }
        if agent is None:
            return mine
        try:
            theirs = self.to_story(msg, agent) or {}
        except RuntimeError as error:
            log("initialize failed", error)
            mine["instructions"] = "Story can't be reached just now; its tools will be back."
            return mine
        result = dict(theirs.get("result") or {})
        result["capabilities"] = dict(result.get("capabilities") or {})
        result["capabilities"]["tools"] = {"listChanged": True}
        result["protocolVersion"] = self.protocol
        return result

    def tools(self) -> list[dict[str, Any]]:
        agent = self.agent()
        if agent is None:
            return [JOIN]
        try:
            found = self.to_story({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"}, agent)
        except RuntimeError as error:
            log("tools/list failed", error)
            return [JOIN]
        return [*list(((found or {}).get("result") or {}).get("tools") or []), JOIN]

    # ------------------------------------------------------------------ joining

    def join(self, given: dict[str, Any]) -> dict[str, Any]:
        try:
            said = join(self.cwd, self.folder, self.session_id, given)
        except Exception as error:  # a join that fails says why, and the relay lives on
            log(traceback.format_exc())
            return text_result(f"Joining didn't work: {error}", error=True)
        if said.get("joined"):
            self.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        return text_result(said["text"])


def text_result(words: str, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": words}]}
    if error:
        out["isError"] = True
    return out


# ------------------------------------------------------------------ the join itself

CODE = re.compile(r"/join/([A-Za-z0-9_-]{6,})")


def _code_and_origin(link: str) -> tuple:
    m = CODE.search(link)
    if m is None:
        raise ValueError("That doesn't look like a Story invitation link (…/join/<code>).")
    origin = re.match(r"^(https?://[^/]+)", link.strip())
    return m.group(1), origin.group(1) if origin else None


def _find_story(origin: str | None, code: str) -> tuple:
    """Where Story Core answers for this link, and the invitation as it shows it."""
    if os.environ.get("STORY_URL"):
        bases = [os.environ["STORY_URL"].rstrip("/")]
    elif origin:
        bases = [origin + "/api", origin]
    else:
        raise ValueError("The link needs its whole address, starting https://.")
    for base in bases:
        status, body = common.call("GET", f"{base}/v1/invitations/{code}")
        if status == 200 and isinstance(body, dict):
            return base, body
    raise ValueError("Story didn't recognise that link. It may have been withdrawn.")


def _open_browser(url: str) -> None:
    """The person's browser: BROWSER if it's set (as Python's own webbrowser honours), else the
    system's."""
    if os.environ.get("STORY_NO_BROWSER"):
        return
    chosen = os.environ.get("BROWSER")
    if chosen:
        subprocess.run([chosen, url], capture_output=True, timeout=10)
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener):
        subprocess.run([opener, url], capture_output=True, timeout=10)


def _signed_in(state: dict[str, Any], base: str) -> str | None:
    """The person's sign-in to this Story on this computer, if it still works."""
    account = common.person_account(state, base)
    token = common.get_secret(account)
    if not token:
        return None
    status, body = common.api("GET", "/me", token=token, base=base)
    if status == 200 and isinstance(body, dict):
        card = body.get("card") or {}
        common.story_place(state, base)["person"] = {
            "card_id": card.get("id"),
            "name": card.get("display_name"),
        }
        return token
    if status in (401, 403):
        common.delete_secret(account)
    return None


def _sign_in(state: dict[str, Any], base: str, wait: float, link: str | None = None) -> tuple:
    """Connect this computer to this Story: a page opens in the person's browser, they say yes,
    and the plugin holds a sign-in as them. Returns the sign-in, or None and where to approve."""
    place = common.story_place(state, base)
    pending = place.get("device") or {}
    if not pending or pending.get("expires", 0) < common.now():
        label = f"Claude Code on {common.computer_name()}"[:80]
        # The link it's connecting for, so the page can say which story, and the name on it.
        status, body = common.api("POST", "/auth/device", {"label": label, "link": link}, base=base)
        if status != 201 or not isinstance(body, dict):
            raise RuntimeError(f"Story couldn't start connecting: {common.refusal(body)}")
        pending = {
            "device_code": body["device_code"],
            "url": body["verify_url"],
            "code": body["user_code"],
            "interval": body.get("interval", 3),
            "expires": common.now() + body.get("expires_in", 600),
        }
        place["device"] = pending
        common.save_state(state)
        _open_browser(pending["url"])
    until = common.now() + wait
    while True:
        status, body = common.api(
            "POST", "/auth/device/token", {"device_code": pending["device_code"]}, base=base
        )
        got = body.get("state") if isinstance(body, dict) else None
        if got == "approved" and body.get("token"):
            common.set_secret(common.person_account(state, base), body["token"])
            card = body.get("card") or {}
            place["person"] = {"card_id": card.get("id"), "name": card.get("display_name")}
            place.pop("device", None)
            common.save_state(state)
            return body["token"], None
        if got in ("denied", "expired", "claimed") or status == 404:
            place.pop("device", None)
            common.save_state(state)
            raise RuntimeError(
                "Connecting this computer was turned down."
                if got == "denied"
                else "That connection request ran out. Call join_story again to start over."
            )
        if common.now() >= until:
            return None, pending
        time.sleep(max(1, int(pending.get("interval", 3))))


def join(cwd: str, folder: str, session_id: str | None, given: dict[str, Any]) -> dict[str, Any]:
    link = str(given.get("link") or "")
    code, origin = _code_and_origin(link)
    state = common.load_state()
    base, preview = _find_story(origin, code)
    here = state["folders"].get(folder)
    if here and common.get_secret(common.agent_account(here["card_id"])) is None:
        here = None
    was = common.base_url(state, folder) if here else None
    if was and was != base and not os.environ.get("STORY_URL"):
        # A folder's agent is in one Story (2026-11-08). Taking it to another would leave the
        # first without it.
        raise ValueError(
            f"This folder is already {here['name']}, in the Story at "
            f"{common.story_name(was)}. To join the Story at {common.story_name(base)}, your "
            "person opens Claude Code in another folder and pastes the link there."
        )
    if not state.get("base_url"):
        state["base_url"] = base
    common.save_state(state)
    workspace = _workspace_title(preview)

    token = _signed_in(state, base)
    connected = token is None
    if token is None:
        # Said at once, not after a wait (Logan's first real try, 2026-11-05: a silent minute,
        # and a sign-in page he didn't expect), and with the code the page asks them to check.
        token, pending = _sign_in(
            state, base, float(os.environ.get("STORY_SIGN_IN_WAIT", "0")), code
        )
        if token is None:
            return {
                "text": (
                    "Tell your person: to connect this computer to Story, a page has opened in "
                    f"their browser showing the code {pending['code']}; they check it's the same "
                    "and press Connect. If it asks them to sign in first, they sign in and it "
                    f"comes back to that page. If no page opened: {pending['url']}\n\n"
                    "Once they've pressed Connect, call join_story again with the same link. "
                    "Don't choose a name for yourself meanwhile: join_story asks if one is needed."
                )
            }
    person = (common.story_place(state, base).get("person") or {}).get("name") or "your person"
    # Said once, the moment this computer is connected, whatever comes next.
    hello = (
        [
            f"Connected this computer to Story as {person}. (If that isn't who's here, they can "
            "disconnect it in Story, under their sessions.)"
        ]
        if connected
        else []
    )

    # The link is the way in (2026-11-05): one no one has taken yet, made for an agent by name,
    # names it, and Story uses that name, whatever else is given. Only this link's own name.
    named = (
        (preview or {}).get("name")
        if (preview or {}).get("state") == "open"
        and (preview or {}).get("agents")
        and not (preview or {}).get("emailed")
        else None
    )
    if here is None and not named and not (given.get("name") or "").strip():
        ask = (
            f"Ask {person} what you should be called in Story (a short name, like Fig: they "
            "can change it later in Story). Then call join_story again with the link and "
            "that name."
        )
        return {"text": "\n\n".join([*hello, ask])}
    elsewhere = [w for w in (here or {}).get("workspaces", []) if w != workspace]
    if (
        here
        and elsewhere
        and workspace not in (here.get("workspaces") or [])
        and not given.get("confirm")
    ):
        return {
            "text": "\n\n".join(hello)
            + ("\n\n" if hello else "")
            + (
                f"This folder is already {here['name']} in {', '.join(elsewhere)}. In "
                f"{workspace} too, it's the same agent: what you learn in this folder (its "
                "memory and past sessions here) is here for every session, whichever workspace "
                "it's in, though Story keeps each workspace's news and posts apart. A separate "
                "folder would keep them apart completely. If that's fine with "
                f"{person}, call join_story again with confirm set to true."
            )
        }

    status, body = common.api(
        "POST",
        f"/invitations/{code}/my-agent",
        {
            "name": None if here else (str(given.get("name") or "").strip()[:60] or None),
            "card_id": here["card_id"] if here else None,
        },
        token=token,
        base=base,
    )
    if status not in (200, 201) or not isinstance(body, dict):
        return {"text": f"Story didn't let this join: {common.refusal(body)}"}
    card_id = body["card_id"]
    if body.get("key"):
        common.set_secret(common.agent_account(card_id), body["key"])
    kept = state["folders"].get(folder) or {"card_id": card_id, "name": body.get("name")}
    kept["card_id"], kept["name"] = card_id, body.get("name") or kept.get("name")
    titles = list(kept.get("workspaces") or [])
    if body.get("workspace") and body["workspace"] not in titles:
        titles.append(body["workspace"])
    kept["workspaces"] = titles
    kept["base_url"] = base
    state["folders"][folder] = kept
    common.save_state(state)

    key = common.get_secret(common.agent_account(card_id))
    lines = list(hello)
    lines += [
        (
            f"You're already in {body.get('workspace')} as {person}'s agent, {kept['name']}."
            if body.get("already")
            else f"Joined {body.get('workspace')} as {person}'s agent, {kept['name']}"
            + (", the name on its link." if named and not here else ".")
        )
    ]
    if body.get("waiting"):
        lines.append("Your place there waits for an owner's yes; Story will say when it holds.")
    if key and session_id:
        # A session works in one workspace (decision 2): one already on a story stays there.
        before = common.load_session(session_id).get("story_id")
        brief = open_session(
            cwd, session_id, key, base=base, story_id=None if before else body.get("story_id")
        )
        if before and body.get("workspace"):
            lines.append(
                f"This session stays where it was. To work in {body['workspace']}, your person "
                "starts a new Claude Code session here."
            )
        elif brief:
            lines.append(brief)
    lines.append("Story's tools are ready now.")
    return {"joined": True, "text": "\n\n".join(lines)}


def _workspace_title(preview: dict[str, Any]) -> str:
    for path in (("workspace", "title"), ("domain", "title"), ("story", "title")):
        found: Any = preview
        for part in path:
            found = found.get(part) if isinstance(found, dict) else None
        if isinstance(found, str) and found:
            return found
    return "the workspace"


# A woken session with another already at work in its folder (one at a time).
NOTHING_TO_DO = (
    "Story: a session is already at work in this folder, and what's waiting for you reaches it "
    "there. There's nothing for this one to do: stop now, without doing anything."
)


def open_session(
    cwd: str,
    session_id: str,
    key: str,
    base: str | None = None,
    story_id: str | None = None,
    woken: bool = False,
    woken_for: str | None = None,
) -> str | None:
    """Open (or resume) this Claude Code session on Story, and its brief. `woken`: Story's
    bridge started it while its owner is away, for what's waiting (`woken_for`)."""
    branch, remote = common.git_place(cwd)
    body: dict[str, Any] = {
        "client_session_id": session_id,
        "folder": common.label_of(common.folder_of(cwd)),
        "branch": branch,
        "remote": remote,
        "pr_url": common.pull_request_of(cwd) if remote else None,
        "plugin_version": common.VERSION,
    }
    if story_id:
        body["story_id"] = story_id
    if woken:
        body["woken"] = True
        if woken_for:
            body["woken_for"] = woken_for
    status, found = common.api("POST", "/me/sessions", body, token=key, base=base)
    if status == 409 and woken:
        return NOTHING_TO_DO
    if status != 200 or not isinstance(found, dict):
        return None
    with common.session_lock(session_id):
        common.save_session(
            session_id, dict(common.load_session(session_id), story_id=found.get("story_id"))
        )
    brief = found.get("brief")
    return str(brief) if brief else None


def main() -> None:
    relay = Relay()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            relay.handle(msg)
        except Exception:  # never let one message end the relay
            log(traceback.format_exc())
            if msg.get("id") is not None:
                relay.fail(msg["id"], -32603, "The Story relay hit a problem.")


if __name__ == "__main__":
    main()
