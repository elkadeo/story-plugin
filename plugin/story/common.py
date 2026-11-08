"""Shared by the Story plugin's relay and hooks: where Story is, the person's sign-in, each folder's
agent, and talking to Story.

Standard library only, and Python 3.9, so it runs on a Mac's own python3 with nothing installed
(docs/design/one-link-and-the-plugin.md, "Where the code lives").

Secrets (the person's sign-in, each agent's key) go in the Mac's Keychain, under the service
"Story". Elsewhere, or with STORY_KEYCHAIN=off (tests, the lab), they go in a file only its owner
can read, in the plugin's data folder. Nothing secret is ever written to Claude Code's settings.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

try:  # a file lock where there is one (macOS, Linux); without it, hooks may race
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

VERSION = "0.3.0"
SERVICE = "Story"
PERSON = "person"
TIMEOUT = 10.0


# ------------------------------------------------------------------ where things are kept


def data_dir() -> str:
    """The plugin's own folder for what it keeps: Claude Code gives each plugin one."""
    found = (
        os.environ.get("STORY_PLUGIN_DATA")
        or os.environ.get("CLAUDE_PLUGIN_DATA")
        or os.path.join(os.path.expanduser("~"), ".story-plugin")
    )
    os.makedirs(found, mode=0o700, exist_ok=True)
    return found


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path: str, value: Any) -> None:
    """Written whole, then moved into place, so a reader never sees half of it."""
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_state() -> dict[str, Any]:
    """Where Story is, who signed in, and which agent each folder is."""
    state = _read_json(os.path.join(data_dir(), "state.json"), {})
    state.setdefault("folders", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    _write_json(os.path.join(data_dir(), "state.json"), state)


def session_file(client_session_id: str) -> str:
    folder = os.path.join(data_dir(), "sessions")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", client_session_id)[:120] or "session"
    return os.path.join(folder, safe + ".json")


def load_session(client_session_id: str) -> dict[str, Any]:
    found: dict[str, Any] = _read_json(session_file(client_session_id), {})
    return found


def save_session(client_session_id: str, value: dict[str, Any]) -> None:
    _write_json(session_file(client_session_id), value)


@contextlib.contextmanager
def session_lock(client_session_id: str) -> Iterator[None]:
    """One hook at a time reads and writes a session's state: the checklist's run in the
    background, several at once."""
    with open(session_file(client_session_id) + ".lock", "a") as f:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_UN)


# ------------------------------------------------------------------ secrets


def _keychain() -> bool:
    return (
        sys.platform == "darwin"
        and os.environ.get("STORY_KEYCHAIN", "on") != "off"
        and shutil.which("security") is not None
    )


def _secrets_path() -> str:
    return os.path.join(data_dir(), "secrets.json")


def get_secret(account: str) -> str | None:
    if _keychain():
        done = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        found = done.stdout.strip()
        return found or None
    found = _read_json(_secrets_path(), {}).get(account)
    return str(found) if found else None


def set_secret(account: str, value: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_:.-]+", account) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", value
    ):
        raise ValueError("unexpected characters in a secret")
    if _keychain():
        # Through security's own prompt, on stdin, so the value never shows in a process list.
        subprocess.run(
            ["security", "-i"],
            input=f"add-generic-password -U -s {SERVICE} -a {account} -w {value}\n",
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return
    kept = _read_json(_secrets_path(), {})
    kept[account] = value
    _write_json(_secrets_path(), kept)


def delete_secret(account: str) -> None:
    if _keychain():
        subprocess.run(
            ["security", "delete-generic-password", "-s", SERVICE, "-a", account],
            capture_output=True,
            timeout=10,
        )
        return
    kept = _read_json(_secrets_path(), {})
    kept.pop(account, None)
    _write_json(_secrets_path(), kept)


def agent_account(card_id: str) -> str:
    return "agent:" + card_id


# ------------------------------------------------------------------ the folder and its agent


def project_dir(cwd: str | None = None) -> str:
    return os.path.realpath(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def _git(cwd: str, *args: str) -> str | None:
    if shutil.which("git") is None:
        return None
    try:
        done = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and out else None


def folder_of(cwd: str) -> str:
    """The folder an agent is: for a git repository, the repository itself, so its worktrees are
    the same agent; otherwise the folder."""
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        common = os.path.realpath(common)
        if os.path.basename(common) == ".git":
            return os.path.dirname(common)
        return common
    return os.path.realpath(cwd)


def label_of(folder: str) -> str:
    home = os.path.realpath(os.path.expanduser("~"))
    return "~" + folder[len(home) :] if folder.startswith(home + os.sep) else folder


def git_place(cwd: str) -> tuple[str | None, str | None]:
    """The branch checked out, and the repository's remote."""
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        branch = None
    return branch, _git(cwd, "remote", "get-url", "origin")


def pull_request_of(cwd: str) -> str | None:
    """The branch's pull request, if GitHub's own tool is here and quick to say."""
    if os.environ.get("STORY_NO_GH") or shutil.which("gh") is None:
        return None
    try:
        done = subprocess.run(
            ["gh", "pr", "view", "--json", "url", "-q", ".url"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and out.startswith("http") else None


def agent_here(state: dict[str, Any], folder: str) -> dict[str, Any] | None:
    """This folder's agent, its key, and the Story it's in (`base`), if it has one."""
    found = state["folders"].get(folder)
    base = base_url(state, folder)
    if not found or not base:
        return None
    key = get_secret(agent_account(found["card_id"]))
    if not key:
        return None
    return dict(found, key=key, base=base)


def computer_name() -> str:
    if sys.platform == "darwin" and shutil.which("scutil"):
        try:
            done = subprocess.run(
                ["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3
            )
            if done.returncode == 0 and done.stdout.strip():
                return done.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return platform.node() or "this computer"


def woken() -> bool:
    """Started by Story's bridge while its owner is away (Story for Mac's "answer while I'm
    away", 2026-11-05): a woken session, opened as one, with what woke it (`woken_for`)."""
    return bool(os.environ.get("STORY_WOKEN"))


def woken_for() -> str | None:
    """What woke it: the activation its bridge started it for."""
    return os.environ.get("STORY_WOKEN_FOR") or None


def under_an_old_bridge() -> bool:
    """Started by a bridge from before woken sessions (it says STORY_BRIDGE and not STORY_WOKEN):
    it has its own connection, so the plugin opens no session for it."""
    return bool(os.environ.get("STORY_BRIDGE")) and not woken()


# ------------------------------------------------------------------ talking to Story


# One computer can be in more than one Story (Logan, 2026-11-08: his Mac's own Story, and
# app.trystory.app). Each folder's agent is in one Story, kept as the folder's `base_url`. The
# first Story this computer joined is the state's own `base_url`, and folders joined before
# 2026-11-08 have none of their own, so they stay in it. The person signs in to each Story apart:
# the first one's sign-in is `person`, as before, and each other's is kept under its address.


def base_url(state: dict[str, Any] | None = None, folder: str | None = None) -> str | None:
    """Where Story is: STORY_URL if it's set; else the folder's own Story, if it has one; else
    the first Story this computer joined."""
    state = state or load_state()
    found = os.environ.get("STORY_URL")
    if not found and folder is not None:
        found = (state["folders"].get(folder) or {}).get("base_url")
    found = found or state.get("base_url")
    return found.rstrip("/") if found else None


def first_story(state: dict[str, Any], base: str) -> bool:
    """Whether `base` is the first Story this computer joined (or none is joined yet)."""
    return not state.get("base_url") or state["base_url"].rstrip("/") == base.rstrip("/")


def person_account(state: dict[str, Any], base: str) -> str:
    """Where the person's sign-in to this Story is kept."""
    if first_story(state, base):
        return PERSON
    return PERSON + ":" + re.sub(r"[^A-Za-z0-9.-]", "_", re.sub(r"^https?://", "", base))


def story_place(state: dict[str, Any], base: str) -> dict[str, Any]:
    """Where the person's name in this Story, and a connection not yet approved, are kept: the
    state itself for the first Story, as before, else the Story's own entry."""
    if first_story(state, base):
        return state
    found: dict[str, Any] = state.setdefault("storys", {}).setdefault(base.rstrip("/"), {})
    return found


def story_name(base: str) -> str:
    """A Story's address as a person reads it: app.trystory.app."""
    return re.sub(r"^https?://", "", base).split("/")[0]


# Where a Mac keeps the certificates every HTTPS site is checked against. Python from python.org
# looks only in its own folder, empty until someone runs its "Install Certificates" step, so
# without this its python3 can't reach Story over HTTPS at all (found 2026-11-05).
SYSTEM_CERTIFICATES = ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem")
_tls: ssl.SSLContext | None = None


def tls() -> ssl.SSLContext:
    """How HTTPS is checked: Python's own certificates, else the Mac's."""
    global _tls
    if _tls is None:
        context = ssl.create_default_context()
        if not context.cert_store_stats().get("x509_ca"):
            for path in SYSTEM_CERTIFICATES:
                if os.path.exists(path):
                    context.load_verify_locations(cafile=path)
                    break
        _tls = context
    return _tls


def call(
    method: str,
    url: str,
    body: Any = None,
    token: str | None = None,
    timeout: float = TIMEOUT,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """One request; the answer's status and body (JSON when it is). Never raises for a refusal."""
    data = json.dumps(body).encode() if body is not None else None
    sent = {"Accept": "application/json", "User-Agent": f"story-plugin/{VERSION}"}
    if data is not None:
        sent["Content-Type"] = "application/json"
    if token:
        sent["Authorization"] = "Bearer " + token
    sent.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=sent, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=tls()) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    except (urllib.error.URLError, OSError) as error:
        return 0, {"error": {"code": "unreachable", "message": str(error)}}
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


def api(
    method: str,
    path: str,
    body: Any = None,
    token: str | None = None,
    timeout: float = TIMEOUT,
    state: dict[str, Any] | None = None,
    base: str | None = None,
) -> tuple[int, Any]:
    """A call to Story's API: to `base`, else to where `base_url` says Story is."""
    base = base or base_url(state)
    if not base:
        return 0, {"error": {"code": "no_story", "message": "Not joined to Story yet."}}
    return call(method, base + "/v1" + path, body, token, timeout)


def refusal(answer: Any) -> str:
    """Story's words for a refusal."""
    if isinstance(answer, dict) and isinstance(answer.get("error"), dict):
        e = answer["error"]
        return " ".join(str(x) for x in (e.get("message"), e.get("hint")) if x)
    return str(answer)[:300]


def now() -> float:
    return time.time()
