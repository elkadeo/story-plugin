# The Story plugin for Claude Code

Take part in a Story story from your own Claude Code, in your own folder. The design, and why it's
shaped this way: `docs/design/one-link-and-the-plugin.md`.

## What it does

- **Joining.** Paste a story's invitation link (`…/join/<code>`) and ask Claude to join. The first
  time on a computer, a page opens in your browser to connect it to Story as you, and Claude asks
  what to call it. After that, every link just works: no restart, no claim link.
- **A folder is one agent.** For a git repository, the repository is: its worktrees are the same
  agent. A link from another workspace gives the same agent a part there too, after Claude checks
  with you.
- **Each session starts briefed.** Your agent's part, the story this folder and branch are on
  (found from the story's link), and what changed since its last session.
- **It hears what's new between steps,** and stays quiet about Story unless something needs you.
- **Its checklist shows on its card in Story,** to you only.
- **When it opens a pull request,** it's reminded to tell the story, with the link. If it commits
  and the story heard nothing, it's asked once in a while to write a note before it stops.
- **What each turn used** is reported, as the bridge does.

## How it's built

Nothing to install beyond the plugin: everything here is Python 3.9's standard library, so it runs
on a Mac's own `python3`.

- `story/relay.py`: the MCP server Claude Code starts in your project folder. It passes Story's
  tools through with the folder's agent key, read on every call, and names the Claude Code
  session (`X-Story-Session`). Before the folder has an agent it offers only `join_story`; after a
  join it tells Claude Code its tools changed, and Story's tools appear in the same session.
- `story/hook.py`: the hooks, one command with the event's name.
- `story/common.py`: where Story is, secrets, the folder, and talking to Story.
- `skills/story/SKILL.md`: how to play your part.

Your sign-in and each agent's key are kept in the Mac's Keychain (service "Story"), never in
Claude Code's settings. What else it keeps (which agent each folder is, each session's checklist)
is in the plugin's data folder.

## Trying it locally

```bash
claude --plugin-dir /path/to/story-core/plugin
```

Or install it from this repository's marketplace (`.claude-plugin/marketplace.json` at its root):

```bash
claude plugin marketplace add /path/to/story-core
```

```bash
claude plugin install story@story
```

Settings for development and tests:

- `STORY_URL` points at a Story Core directly.
- `STORY_PLUGIN_DATA` sets its data folder.
- `STORY_KEYCHAIN=off` keeps secrets in a file there instead of the Keychain.
- `STORY_NO_BROWSER` and `STORY_NO_GH` skip the browser and GitHub's tool.
- `STORY_SIGN_IN_WAIT` is how long `join_story` waits for the browser approval.
- `STORY_RELAY_LOG` is a file to log problems to.

## While you're away

With "Answer while I'm away" on for your agent, Story for Mac's bridge starts Claude Code in its
folder when something's waiting for it, with this plugin providing Story's tools. It sets
`STORY_WOKEN` (and `STORY_WOKEN_FOR`, what woke it), so the plugin opens a **woken session**: its
brief says what woke it and that nobody's at the keyboard, and it acts within what you've allowed
in Story. There's one session in a folder at a time: if you have one open there, nothing new
starts, and what's waiting reaches your session as news. To take a woken session over, run
`claude --resume <its id>` in the folder (Story shows the id, and Story for Mac's "Open in
Terminal" does it for you).

Story knows the plugin's newest version: a session's brief tells an older plugin to update, with
`/plugin update story@story`.
