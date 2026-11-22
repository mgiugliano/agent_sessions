# aises

**Find and resume your past coding-agent sessions from one place.**

If you use several coding agents (and maybe several accounts of the same agent), you
quickly lose track of *where* you worked on something and *with which tool*. `aises`
reads the session stores of all of them, lets you fuzzy-search them (including the
full text of every conversation), and resumes the one you pick: it changes to the
right folder and starts the right agent with the right flags.

Supported out of the box:

| Agent | Notes |
|---|---|
| Claude Code (`claude`) | One entry per config directory, so a second account such as `~/.claude-lab` shows up as its own agent. |
| `pi` | Reads `~/.pi/agent/sessions`. |
| `agy` (Antigravity CLI) | Reads the CLI's own summary database, read-only. |

It is a single Python file that uses only the standard library, plus the `fzf` program.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Adding another agent](#adding-another-agent)
- [Privacy and safety](#privacy-and-safety)
- [Limitations](#limitations)

## Features

- One list for all agents and accounts, newest first, with the agent shown as a colored badge.
- Sessions from the **current folder come first**, then pinned sessions, then the rest.
- Fuzzy search on the list (date, agent, folder, title, tag, note).
- **Full-text search** inside the conversations (press `Ctrl-F`).
- A preview pane with the folder, dates, message count, the exact resume command, and the first and last messages.
- Resume with one key: `aises` changes to the session folder and starts the agent. Or only print or copy the command.
- A **projects view** that groups sessions by folder.
- Folders that no longer exist are flagged with a red `✗`.
- Pin, tag and add notes to sessions. Hide sessions you no longer want to see (and unhide them later). Nothing is ever deleted.
- Agents are discovered automatically. An optional config file adjusts them.
- A short start-up banner (can be turned off).

## Requirements

- macOS or Linux. The author uses macOS.
- **Python 3.11 or newer** (for `tomllib` and SQLite FTS5, which ship with normal Python builds).
- **[fzf](https://github.com/junegunn/fzf)**, a recent version (developed with 0.75; it uses the `transform`, `rebind` and `--header-border` features).
- **zsh**, if you want `aises` to change directory for you (see the wrapper below).
- [`uv`](https://docs.astral.sh/uv/) is optional. It is used for the shebang line and for development.
- `pbcopy` (macOS) is used by the "copy command" key. On Linux, the key reports an error and the rest still works.

## Installation

### 1. Get the file

```sh
git clone <your-repository-url> aises
cd aises
```

There is nothing to build and there are no Python packages to install.

### 2. Make it executable and put it on your PATH

```sh
chmod +x aises.py
mkdir -p ~/.bin
cp aises.py ~/.bin/aises.py        # or: ln -s "$PWD/aises.py" ~/.bin/aises.py
```

Make sure `~/.bin` is on your `PATH`.

The first line of the file is `#!/usr/bin/env -S uv run --script`, so it needs `uv`. Because the
code uses only the standard library, you can instead change that line to
`#!/usr/bin/env python3` and `uv` is not needed at all.

### 3. Add the shell function

A program cannot change the directory of the shell that started it, so `aises` prints
the command to run and a small zsh function runs it. Print the function with:

```sh
aises.py --init
```

and paste the output into `~/.zshrc`. It looks like this:

```zsh
aises() {
  local out
  out="$(/path/to/aises.py "$@")" || return
  [[ -n "$out" ]] && eval "$out"
}
```

Open a new terminal. From now on, `aises` is the command to use.

Without the function you can still run `aises.py`: it prints the `cd … && agent …`
line and you copy it yourself.

### 4. Check what was found

```sh
aises.py --config     # agents discovered on this machine
aises.py --stats      # number of sessions per agent
```

### Development with uv

```sh
uv run aises.py --stats
```

`pyproject.toml` declares no dependencies.

## Usage

```sh
aises                 # pick a session (current folder first)
aises "stim gen"      # same, with an initial fuzzy query
aises -p              # projects view: pick a folder, then one of its sessions
aises -a claude,pi    # only these agents (names as shown by --config)
aises --folder ~/dev/myproject
aises --show-hidden   # include sessions you hid
aises --no-splash     # skip the start-up banner
```

Other commands:

| Command | What it does |
|---|---|
| `aises.py --init` | Print the zsh wrapper function. |
| `aises.py --config` | Show the agents found and which config file is used. |
| `aises.py --init-config` | Write a commented starter config file. |
| `aises.py --stats` | Count sessions per agent and folders that no longer exist. |
| `aises.py --reindex` | Delete the search index (and the cached alias lookup) and rebuild it. |

### Typical workflows

- **"Where did I work on X?"** Run `aises`, press `Ctrl-F`, type a word that appeared in the conversation. The list now shows every conversation, from every agent, that mentions it.
- **"What did I do in this folder?"** Go there and run `aises`. Its sessions are at the top.
- **"I only want to see the command."** Press `Ctrl-O`. The command is placed on your prompt so you can edit it before running it.
- **"Which folders have I used agents in?"** Run `aises -p`.

## Keyboard shortcuts

Everything that is not Enter or Esc is on a Ctrl key. The shortcuts are also shown in the box at the top of the picker.

| Key | Action |
|---|---|
| `Enter` | Resume the session: change folder and start the agent. |
| `Ctrl-O` | Put the command on your prompt without running it. |
| `Ctrl-Y` | Copy the command to the clipboard. |
| `Ctrl-P` | Pin or unpin the session (pinned sessions stay near the top). |
| `Ctrl-X` | Hide or unhide the session. |
| `Ctrl-T` | Set a tag. |
| `Ctrl-N` | Set a note. |
| `Ctrl-R` | Show or hide the hidden sessions (the prompt shows `all>`). |
| `Ctrl-F` | Search the full conversation text (the prompt shows `text>`). |
| `Ctrl-G` | Go back to fuzzy search. |
| `Esc` | Quit. |

`Ctrl-P` and `Ctrl-N` replace fzf's usual "up" and "down". The arrow keys, `Ctrl-J` and `Ctrl-K` still move the selection.

If the folder of a session no longer exists, `aises` does not try to change to it. It puts the command on your prompt instead and says so.

## Configuration

Agents are **discovered automatically**:

- every `~/.claude*` folder that has a `projects/` subfolder becomes a Claude Code agent;
- if a shell alias runs `claude` with another `CLAUDE_CONFIG_DIR`, the alias name and its exact command are used as the name and launcher (this keeps flags such as `env -u ANTHROPIC_API_KEY`);
- `~/.pi/agent/sessions` becomes `pi`;
- `~/.gemini/antigravity-cli/conversation_summaries.db` becomes `agy`.

The alias lookup asks your shell once (`$SHELL -ic alias`) and caches the answer for a week in `~/.cache/aises/aliases.json`.

You never need a config file. If you want one, run `aises.py --init-config`. The file is `~/.config/aises/config.toml` (or the path in `$AISES_CONFIG`):

```toml
discover = true              # false: use only the [[agent]] entries below
disable  = ["pi"]            # hide discovered agents by name

[ui]
splash = true

[[agent]]                    # add an agent, or override a discovered one with the same name
name     = "claude_work"
type     = "claude"          # claude | pi | agy
dir      = "~/.claude-work"
launcher = 'env -u ANTHROPIC_API_KEY CLAUDE_CONFIG_DIR="$HOME/.claude-work" claude'

[[agent]]
name = "agy"
type = "agy"
db   = "~/.gemini/antigravity-cli/conversation_summaries.db"
```

A config file that cannot be read is ignored with a warning. It never stops the tool.

Where `aises` keeps its own data (you can delete both folders at any time):

| Path | Content |
|---|---|
| `~/.cache/aises/index.db` | Search index. Can be rebuilt. |
| `~/.cache/aises/aliases.json` | Cached alias lookup. |
| `~/.local/share/aises/state.db` | Your pins, tags, notes and hidden flags. |

The environment variables `AISES_CACHE`, `AISES_STATE` and `AISES_CONFIG` change these locations.

## How it works

**One plugin per agent.** Each agent is a small class that knows how to (1) list its sessions, (2) read the metadata of a session (id, folder, title, first prompt, dates, number of messages), (3) build the resume command, and (4) return the messages for the preview.

- **Claude Code** keeps one JSONL file per session in `<config dir>/projects/<folder>/<id>.jsonl`. The folder name is an encoding of the path and is not reversible (`/`, `.` and `_` all become `-`), so `aises` reads the real folder from the `cwd` field inside the file. It ignores sub-agent (sidechain) messages, tool calls and system noise, and takes the title from the session's own title record. Resume: `claude --resume <id>`, run from the session's folder.
- **pi** also writes JSONL. The first line holds the id and folder. Resume: `pi --session <file>`.
- **agy** stores conversation summaries in SQLite, with its own FTS5 full-text table. `aises` opens that database read-only (`mode=ro`) and reuses agy's own search index instead of building another one. Resume: `agy --conversation <id>`.

**The index.** For the JSONL agents, `aises` builds a SQLite database with an FTS5 full-text table. Each file is identified by `(path, modification time, size)`, so only new or changed sessions are parsed again. Only the text typed by you and written by the agent is indexed. Tool inputs, tool outputs and "thinking" blocks are left out, which keeps the index small and the results relevant.

**Full-text search.** fzf can only match the lines it displays, so full-text mode switches fzf's own search off and reloads the list from SQLite on every keystroke. Queries become a list of prefix terms (`"word"*`), so partial words match.

**The picker.** The interface is [fzf](https://github.com/junegunn/fzf), driven from Python. The list is tab-separated: a hidden key (`agent:id`) and the display text. The shortcuts use fzf's `execute`, `reload`, `transform` and `rebind` actions. The `transform` action calls `aises.py --view-action …`, which looks at fzf's environment (`FZF_PROMPT`, `FZF_QUERY`) and prints the next fzf actions. This is how pin/hide/tag keep you in the same view (fuzzy, full-text or "all").

**Ordering.** Sessions whose folder equals the current folder (compared with `realpath`, so symlinks and Dropbox paths work) come first, then pinned sessions, then everything by last activity.

**Resuming.** The picker's stdout carries only the final command, built with `shlex.quote`. The shell function evaluates it. `Ctrl-O` emits `print -z -- '<command>'`, which is a zsh feature that places text in the next prompt's edit buffer.

**State is separate from the cache.** Pins, tags, notes and hidden flags are in their own database, keyed by `(agent, session id)`. Deleting the cache can never lose them.

**Technology summary:** Python 3.11+ standard library (`sqlite3` with FTS5, `json`, `tomllib`, `argparse`, `subprocess`, `shlex`), fzf, zsh.

## Adding another agent

1. Write a class like `Pi` in `aises.py`. File-based agents provide `files()`, `parse(path)`, `resume(meta)` and `messages(meta)`. Database-backed agents provide `records()`, `search(expr)`, `resume(meta)` and `messages(meta)`. The metadata is a dict with `source`, `sid`, `cwd`, `path`, `title`, `prompt`, `ts`, `start` and `n`.
2. Add a branch for it in `discover()` (auto-discovery) and `build_plugins()` (config file).

The comments above the configuration section of `aises.py` show where.

## Privacy and safety

- `aises` runs entirely on your machine and makes no network calls.
- It **only reads** the session files and databases of the agents. It opens foreign SQLite databases read-only and never writes to them.
- **The search index contains the text of your conversations.** `~/.cache/aises/index.db` is a plain SQLite file in your home folder. Treat it like the session files themselves. `aises.py --reindex` deletes it.
- "Hide" only sets a flag in `aises`. The session stays in the agent.
- Resuming a session starts the agent in that folder. Check the folder shown in the preview first.

## Limitations

- Claude Desktop (Cowork), Claude Science and the Antigravity IDE are not supported yet. Their stores are either in a binary format or not documented.
- The "print command" key needs zsh. The shell wrapper is written for zsh (bash is not supported yet).
- The "copy" key uses `pbcopy`, which is macOS only.
- Session formats are internal to each agent and can change. If an agent updates and sessions disappear from the list, please open an issue.
- Pins, tags, notes and hidden flags are tied to the session id, so they do not follow a session if an agent re-creates it with a new id.

## License

MIT. See the [LICENSE](LICENSE) file.
