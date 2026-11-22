#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""aises - find and resume past coding-agent sessions (claude, claude_lab, pi, agy).

Standard library only (plus the external `fzf` binary), so this single file can be
copied to ~/.bin/. Because a child process cannot change the directory of your
shell, install the small zsh wrapper printed by `aises.py --init`.

Foreign session stores (agent files, agent databases) are only ever READ.
Pins/tags/notes/hidden flags live in ~/.local/share/aises/state.db, and the
rebuildable search index lives in ~/.cache/aises/index.db.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

HOME = Path.home()
SCRIPT = os.path.abspath(__file__)
CACHE_DIR = Path(os.environ.get("AISES_CACHE", HOME / ".cache" / "aises"))
STATE_DIR = Path(os.environ.get("AISES_STATE", HOME / ".local" / "share" / "aises"))
MAX_BODY = 1_500_000  # chars of conversation text indexed per session

# ---------------------------------------------------------------- helpers


def eprint(*a):
    print(*a, file=sys.stderr)


def to_epoch(s) -> float:
    if not s:
        return 0.0
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def ro_connect(path: str) -> sqlite3.Connection:
    """Open a foreign SQLite DB strictly read-only; never creates a file."""
    uri = Path(path).as_uri()
    try:
        con = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=10)
        con.execute("select 1 from sqlite_master limit 1")
        return con
    except sqlite3.OperationalError:
        return sqlite3.connect(uri + "?immutable=1", uri=True, timeout=10)


def text_of(content) -> str:
    """Plain text from a message content (string or list of blocks); ignores tools/thinking."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


NOISE = ("<command-", "<local-command", "<system-reminder", "[Request interrupted",
         "Caveat:", "<ide_", "<user-prompt-submit-hook")


def is_noise(text: str) -> bool:
    return text.startswith(NOISE)


def one_line(s: str, n: int = 100) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def fts_expr(q: str) -> str:
    toks = re.findall(r"\w+", q, re.UNICODE)
    return " ".join(f'"{t}"*' for t in toks)


# ---------------------------------------------------------------- plugins
# A plugin exposes:  name, file_based, and
#   file-based : files() -> paths ; parse(path) -> (meta dict, body text)
#   agy-like   : records() -> list of meta dicts ; search(expr) -> [sid]
#   all        : resume(meta) -> (cwd, shell command string)
#                messages(meta) -> [(role, text)]  (for the preview)
# meta keys: source sid cwd title prompt ts start n path


class ClaudeLike:
    """Claude Code (any config dir). One instance per account."""
    file_based = True

    def __init__(self, name: str, root: str, launcher: str):
        self.name, self.root, self.launcher = name, str(Path(root).expanduser()), launcher

    def files(self):
        return glob.glob(os.path.join(self.root, "projects", "*", "*.jsonl"))

    def _iter(self, path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    def parse(self, path):
        cwd = ai = custom = first = None
        n, t0, t1, body = 0, 0.0, 0.0, []
        for d in self._iter(path):
            t = d.get("type")
            if t == "ai-title":
                ai = d.get("aiTitle") or ai
                continue
            if t == "custom-title":
                custom = d.get("customTitle") or custom
                continue
            if d.get("isSidechain"):
                continue
            if cwd is None and d.get("cwd"):
                cwd = d["cwd"]
            ts = to_epoch(d.get("timestamp"))
            if ts:
                t0 = t0 or ts
                t1 = max(t1, ts)
            if t not in ("user", "assistant"):
                continue
            text = text_of((d.get("message") or {}).get("content"))
            if not text or (t == "user" and (d.get("isMeta") or is_noise(text))):
                continue
            n += 1
            if t == "user" and first is None:
                first = text
            body.append(text)
        meta = dict(source=self.name, sid=Path(path).stem, cwd=cwd or "", path=path,
                    title=custom or ai or one_line(first or "", 90), prompt=one_line(first or "", 300),
                    ts=t1 or os.path.getmtime(path), start=t0, n=n)
        return meta, "\n".join(body)[:MAX_BODY]

    def resume(self, m):
        return m["cwd"], f"{self.launcher} --resume {shlex.quote(m['sid'])}"

    def messages(self, m):
        out = []
        for d in self._iter(m["path"]):
            if d.get("type") in ("user", "assistant") and not d.get("isSidechain"):
                t = text_of((d.get("message") or {}).get("content"))
                if t and not (d["type"] == "user" and (d.get("isMeta") or is_noise(t))):
                    out.append((d["type"], t))
        return out


class Pi:
    name, file_based = "pi", True

    def __init__(self, root: str = "~/.pi/agent/sessions", name: str = "pi"):
        self.name, self.root = name, Path(root).expanduser()

    def files(self):
        return glob.glob(str(self.root / "*" / "*.jsonl"))

    def _iter(self, path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    @staticmethod
    def _msg(d):
        m = d.get("message") or {}
        return m.get("role"), text_of(m.get("content"))

    def parse(self, path):
        sid, cwd, first, name = Path(path).stem, "", None, None
        n, t0, t1, body = 0, 0.0, 0.0, []
        for d in self._iter(path):
            t = d.get("type")
            ts = to_epoch(d.get("timestamp"))
            if ts:
                t0 = t0 or ts
                t1 = max(t1, ts)
            if t == "session":
                sid, cwd = d.get("id") or sid, d.get("cwd") or ""
            elif t == "session_info" and d.get("name"):
                name = d["name"]
            elif t == "message":
                role, text = self._msg(d)
                if role in ("user", "assistant") and text:
                    n += 1
                    if role == "user" and first is None:
                        first = text
                    body.append(text)
        meta = dict(source="pi", sid=sid, cwd=cwd, path=path,
                    title=name or one_line(first or "", 90), prompt=one_line(first or "", 300),
                    ts=t1 or os.path.getmtime(path), start=t0, n=n)
        return meta, "\n".join(body)[:MAX_BODY]

    def resume(self, m):
        return m["cwd"], f"pi --session {shlex.quote(m['path'])}"

    def messages(self, m):
        out = []
        for d in self._iter(m["path"]):
            if d.get("type") == "message":
                role, text = self._msg(d)
                if role in ("user", "assistant") and text:
                    out.append((role, text))
        return out


class Agy:
    """Antigravity CLI. Reads agy's own summary DB (and its FTS table) read-only."""
    name, file_based = "agy", False

    def __init__(self, db: str = "~/.gemini/antigravity-cli/conversation_summaries.db", name: str = "agy"):
        self.name, self.db = name, str(Path(db).expanduser())

    def records(self):
        if not os.path.exists(self.db):
            return []
        con = ro_connect(self.db)
        rows = con.execute(
            "select conversation_id, title, preview, step_count, last_modified_time, workspace_uris "
            "from conversation_summaries where nesting_depth = 0").fetchall()
        out = []
        for sid, title, preview, steps, lm, ws in rows:
            try:
                uris = json.loads(ws or "[]")
            except ValueError:
                uris = []
            cwd = urllib.parse.unquote(urllib.parse.urlparse(uris[0]).path) if uris else ""
            out.append(dict(source="agy", sid=sid, cwd=cwd, path="",
                            title=one_line(title or preview, 90), prompt=one_line(preview, 300),
                            ts=to_epoch(lm), start=0.0, n=steps or 0))
        return out

    def search(self, expr):
        if not expr or not os.path.exists(self.db):
            return []
        try:
            con = ro_connect(self.db)
            rows = con.execute(
                "select conversation_id from conversations_fts where conversations_fts match ? "
                "order by bm25(conversations_fts) limit 400", (expr,)).fetchall()
        except sqlite3.Error:
            return []
        seen, out = set(), []
        for (sid,) in rows:
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
        return out

    def resume(self, m):
        return m["cwd"], f"agy --conversation {shlex.quote(m['sid'])}"

    def messages(self, m):
        try:
            con = ro_connect(self.db)
            rows = con.execute(
                "select user_body from conversations_fts where conversation_id = ? "
                "order by chunk_index limit 3", (m["sid"],)).fetchall()
        except sqlite3.Error:
            return []
        return [("user", r[0]) for r in rows if r[0]]


# ---------------------------------------------------------------- configuration
# Agents are AUTO-DISCOVERED, then adjusted by an optional TOML file
# (~/.config/aises/config.toml, or $AISES_CONFIG). Write a starter with `aises --init-config`.
# To support a new kind of agent, add a plugin class above and a branch in build_plugins().

CONFIG_PATH = Path(os.environ.get("AISES_CONFIG", HOME / ".config" / "aises" / "config.toml"))
RC_FILES = [".zshrc", ".zshenv", ".zprofile", ".aliases", ".bashrc", ".bash_profile", ".config/aliases"]
ALIAS_RE = re.compile(r"""^\s*(?:alias\s+)?([\w.-]+)=(['"])(.*)\2\s*$""")
ALIAS_CACHE = CACHE_DIR / "aliases.json"
CFGDIR_RE = re.compile(r"""CLAUDE_CONFIG_DIR=(["']?)([^"'\s]+)\1""")


def expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def _parse_aliases(lines, found):
    for line in lines:
        m = ALIAS_RE.match(line)
        if not m or "CLAUDE_CONFIG_DIR" not in m[3] or "claude" not in m[3]:
            continue
        d = CFGDIR_RE.search(m[3])
        if d:
            found[os.path.realpath(expand(d[2]))] = (m[1], m[3])


def shell_aliases() -> dict[str, tuple[str, str]]:
    """Ask the interactive shell for its aliases once (they may live in sourced dotfiles); cached a week."""
    try:
        cached = json.loads(ALIAS_CACHE.read_text())
        if time.time() - cached["time"] < 7 * 86400:
            return {k: tuple(v) for k, v in cached["found"].items()}
    except (OSError, ValueError, KeyError):
        pass
    found: dict = {}
    try:
        out = subprocess.run([os.environ.get("SHELL", "/bin/zsh"), "-ic", "alias"], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=20).stdout
        _parse_aliases(out.splitlines(), found)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        ALIAS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ALIAS_CACHE.write_text(json.dumps({"time": time.time(), "found": found}))
    except OSError:
        pass
    return found


def claude_aliases(need_more: bool = True) -> dict[str, tuple[str, str]]:
    """Aliases that run claude with another CLAUDE_CONFIG_DIR -> {real dir: (alias, command)}."""
    found: dict = {}
    for rc in RC_FILES + [".config/zsh/.zshrc"]:
        try:
            _parse_aliases((HOME / rc).read_text(errors="replace").splitlines(), found)
        except OSError:
            pass
    return found


def discover() -> list[dict]:
    """Return plugin specs found on this machine."""
    specs = []
    aliases = claude_aliases()
    dirs = [d for d in sorted(HOME.glob(".claude*")) if (d / "projects").is_dir()]
    if any(d.name != ".claude" and os.path.realpath(d) not in aliases for d in dirs):
        aliases = {**shell_aliases(), **aliases}  # only pay for the shell lookup when needed
    for d in dirs:
        real = os.path.realpath(d)
        if d.name == ".claude":
            specs.append(dict(type="claude", name="claude", dir=str(d), launcher="claude"))
        elif real in aliases:  # reuse the alias name and its exact command (auth flags etc.)
            specs.append(dict(type="claude", name=aliases[real][0], dir=str(d), launcher=aliases[real][1]))
        else:
            nm = "claude_" + re.sub(r"[^\w]+", "_", d.name[len(".claude"):].strip("-_.")) if d.name != ".claude" else "claude"
            specs.append(dict(type="claude", name=nm, dir=str(d),
                              launcher=f"env CLAUDE_CONFIG_DIR={shlex.quote(str(d))} claude"))
    if (HOME / ".pi" / "agent" / "sessions").is_dir():
        specs.append(dict(type="pi", name="pi", dir="~/.pi/agent/sessions"))
    agy_db = HOME / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
    if agy_db.exists():
        specs.append(dict(type="agy", name="agy", db=str(agy_db)))
    return specs


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        import tomllib
        return tomllib.loads(CONFIG_PATH.read_text())
    except Exception as e:  # bad config must not break the tool
        eprint(f"aises: ignoring unreadable config {CONFIG_PATH}: {e}")
        return {}


def effective_specs(cfg: dict) -> list[dict]:
    specs = discover() if cfg.get("discover", True) else []
    for entry in cfg.get("agent", []):  # [[agent]] tables: add, or override a discovered one by name
        if "name" not in entry or "type" not in entry:
            eprint(f"aises: config [[agent]] needs name and type: {entry}")
            continue
        specs = [x for x in specs if x["name"] != entry["name"]]
        specs.append(dict(entry))
    off = set(cfg.get("disable", []))
    return [x for x in specs if x["name"] not in off]


def build_plugins(specs: list[dict]) -> list:
    out = []
    for x in specs:
        t = x["type"]
        if t == "claude":
            d = expand(x["dir"])
            out.append(ClaudeLike(x["name"], d, x.get("launcher") or "claude"))
        elif t == "pi":
            out.append(Pi(expand(x.get("dir", "~/.pi/agent/sessions")), x["name"]))
        elif t == "agy":
            out.append(Agy(expand(x.get("db", "~/.gemini/antigravity-cli/conversation_summaries.db")), x["name"]))
        else:
            eprint(f"aises: unknown agent type {t!r} for {x['name']}")
    return out


CONFIG = load_config()
SPECS = effective_specs(CONFIG)
PLUGINS = build_plugins(SPECS)
BY_NAME = {p.name: p for p in PLUGINS}

# ---------------------------------------------------------------- storage


def open_db(path: Path, schema: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.executescript(schema)
    return con


INDEX_SCHEMA = """
create table if not exists sessions(path text primary key, source text, sid text, cwd text,
  title text, prompt text, ts real, start real, n int, mtime real, size int);
create virtual table if not exists fts using fts5(path unindexed, body,
  tokenize='unicode61 remove_diacritics 2');
"""
STATE_SCHEMA = """
create table if not exists flags(source text, sid text, pinned int default 0,
  hidden int default 0, tag text default '', note text default '', primary key(source, sid));
"""


def update_index(con: sqlite3.Connection) -> None:
    known = {p: (m, s) for p, m, s in con.execute("select path, mtime, size from sessions")}
    seen = set()
    for plugin in PLUGINS:
        if not plugin.file_based:
            continue
        for path in plugin.files():
            seen.add(path)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if known.get(path) == (st.st_mtime, st.st_size):
                continue
            meta, body = plugin.parse(path)
            con.execute("delete from fts where path = ?", (path,))
            con.execute("insert or replace into sessions values (?,?,?,?,?,?,?,?,?,?,?)",
                        (path, meta["source"], meta["sid"], meta["cwd"], meta["title"], meta["prompt"],
                         meta["ts"], meta["start"], meta["n"], st.st_mtime, st.st_size))
            con.execute("insert into fts(path, body) values (?,?)", (path, body))
    for path in set(known) - seen:
        con.execute("delete from sessions where path = ?", (path,))
        con.execute("delete from fts where path = ?", (path,))
    con.commit()


COLS = ("source", "sid", "cwd", "title", "prompt", "ts", "start", "n", "path")


def load_records(index: sqlite3.Connection) -> list[dict]:
    recs = [dict(zip(COLS, r)) for r in index.execute(
        "select source, sid, cwd, title, prompt, ts, start, n, path from sessions")]
    for p in PLUGINS:
        if not p.file_based:
            try:
                recs += p.records()
            except sqlite3.Error as e:
                eprint(f"aises: cannot read {p.name}: {e}")
    return recs


def load_flags(state: sqlite3.Connection) -> dict:
    return {(s, i): dict(pinned=p, hidden=h, tag=t, note=n)
            for s, i, p, h, t, n in state.execute("select source, sid, pinned, hidden, tag, note from flags")}


# ---------------------------------------------------------------- display

NOFOLDER = "(no folder)"
_PALETTE = ["38;5;208", "38;5;75", "38;5;114", "38;5;173", "38;5;141", "38;5;220", "38;5;204", "38;5;80"]
_FIXED = {"claude": "38;5;208", "pi": "38;5;75", "agy": "38;5;114"}


class _Color(dict):
    def get(self, name, default="37"):
        if name in _FIXED:
            return _FIXED[name]
        return _PALETTE[sum(map(ord, name)) % len(_PALETTE)]


COLOR = _Color()


def c(code, s):
    return f"\x1b[{code}m{s}\x1b[0m"


def short_dir(p: str, width: int = 42) -> str:
    h = str(HOME)
    if not p or p == NOFOLDER:
        return NOFOLDER
    if p == h:
        p = "~"
    elif p.startswith(h + "/"):
        p = "~" + p[len(h):]
    return p if len(p) <= width else "…" + p[-(width - 1):]


class Env:
    def __init__(self):
        self.cwd_real = os.path.realpath(os.getcwd())
        self._real: dict[str, str] = {}
        self._exists: dict[str, bool] = {}

    def real(self, p):
        if p not in self._real:
            self._real[p] = os.path.realpath(p) if p else ""
        return self._real[p]

    def exists(self, p):
        if p not in self._exists:
            self._exists[p] = (not p) or os.path.isdir(p)  # no folder recorded: not "missing"
        return self._exists[p]


def key_of(r):
    return f"{r['source']}:{r['sid']}"


def fmt_line(r, fl, env, extra=""):
    f = fl.get((r["source"], r["sid"]), {})
    when = dt.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M") if r["ts"] else "?" * 16
    badge = c(COLOR.get(r["source"], "37"), f"{r['source']:<10}")
    mark = c("31", "✗") if not env.exists(r["cwd"]) else " "
    folder = f"{short_dir(r['cwd']):<42}"
    if mark != " ":
        folder = c("2", folder)
    star = c("33", "★") if f.get("pinned") else " "
    hid = c("2;31", "[hidden] ") if f.get("hidden") else ""
    tag = c("36", f" #{f['tag']}") if f.get("tag") else ""
    note = c("35", f" ✎ {one_line(f['note'], 40)}") if f.get("note") else ""
    n = f"{r['n']:>4}"
    text = one_line(r["title"] or r["prompt"], 90)
    line = f"{star}{when} {badge} {mark}{folder} {n}  {hid}{text}{tag}{note}"
    if extra:
        line += "  " + c("2", extra)
    return f"{key_of(r)}\t{line}"


def ordered(recs, fl, env, show_hidden=False, agents=None, folder=None):
    out = []
    ref = env.real(folder) if folder else None
    for r in recs:
        f = fl.get((r["source"], r["sid"]), {})
        if f.get("hidden") and not show_hidden:
            continue
        if agents and r["source"] not in agents:
            continue
        if folder == NOFOLDER:
            if r["cwd"]:
                continue
        elif ref is not None and env.real(r["cwd"]) != ref:
            continue
        here = bool(r["cwd"]) and env.real(r["cwd"]) == env.cwd_real
        out.append((0 if here else 1 if f.get("pinned") else 2, -r["ts"], r))
    out.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in out]


# ---------------------------------------------------------------- commands


def get_conns():
    return (open_db(CACHE_DIR / "index.db", INDEX_SCHEMA), open_db(STATE_DIR / "state.db", STATE_SCHEMA))


def cmd_list(a):
    index, state = get_conns()
    update_index(index)
    env, fl = Env(), load_flags(state)
    recs = ordered(load_records(index), fl, env, a.show_hidden, a.agent, a.folder)
    sys.stdout.write("\n".join(fmt_line(r, fl, env) for r in recs) + ("\n" if recs else ""))


def cmd_fts(a):
    query = a.fts
    if not fts_expr(query):
        return cmd_list(a)
    index, state = get_conns()
    update_index(index)
    env, fl = Env(), load_flags(state)
    expr = fts_expr(query)
    snippets: dict[tuple, str] = {}
    hits: set[tuple] = set()
    try:
        for path, snip in index.execute(
                "select path, snippet(fts, 1, '[', ']', '…', 10) from fts where fts match ? "
                "order by rank limit 400", (expr,)):
            hits.add(path)
            snippets[path] = one_line(snip, 90)
    except sqlite3.OperationalError:
        pass
    agy_ids = set(BY_NAME["agy"].search(expr))
    recs = []
    for r in load_records(index):
        if (r["path"] and r["path"] in hits) or (r["source"] == "agy" and r["sid"] in agy_ids):
            recs.append(r)
    recs = ordered(recs, fl, env, a.show_hidden, a.agent, a.folder)
    sys.stdout.write("\n".join(fmt_line(r, fl, env, snippets.get(r["path"], "")) for r in recs)
                     + ("\n" if recs else ""))


def find(index, key):
    src, _, sid = key.partition(":")
    for r in load_records(index):
        if r["source"] == src and r["sid"] == sid:
            return r
    return None


def cmd_preview(a):
    index, state = get_conns()
    r = find(index, a.preview)
    if not r:  # projects view passes a folder path: summarise it
        env = Env()
        rs = [x for x in load_records(index) if x["cwd"] and env.real(x["cwd"]) == env.real(a.preview)]
        print(a.preview + ("" if env.exists(a.preview) else c("31", "   (folder no longer exists)")))
        for x in sorted(rs, key=lambda x: -x["ts"])[:15]:
            print(f"  {dt.datetime.fromtimestamp(x['ts']):%Y-%m-%d %H:%M} {x['source']:<10} {one_line(x['title'], 60)}")
        return
    env, fl = Env(), load_flags(state)
    f = fl.get((r["source"], r["sid"]), {})
    plugin = BY_NAME[r["source"]]
    cwd, cmd = plugin.resume(r)
    fmt = lambda t: dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "?"
    print(c(COLOR.get(r["source"], "37"), f"{r['source']}") + f"  {r['sid']}")
    print(f"folder : {cwd or '?'}" + ("" if env.exists(cwd) else c("31", "   (folder no longer exists)")))
    print(f"active : {fmt(r['ts'])}" + (f"   started: {fmt(r['start'])}" if r["start"] else "")
          + f"   msgs: {r['n']}")
    if f.get("pinned") or f.get("hidden") or f.get("tag") or f.get("note"):
        print(f"flags  : pinned={bool(f.get('pinned'))} hidden={bool(f.get('hidden'))} "
              f"tag={f.get('tag','')!r} note={f.get('note','')!r}")
    print(f"resume : cd {shlex.quote(cwd)} && {cmd}\n")
    msgs = plugin.messages(r)
    shown = msgs[:4] + ([("…", "…")] if len(msgs) > 8 else []) + (msgs[4:][-4:] if len(msgs) > 8 else msgs[4:])
    for role, text in shown:
        if role == "…":
            print(c("2", f"  … {len(msgs) - 8} more messages …\n"))
            continue
        label = c("1;32", "you") if role == "user" else c("1;34", "agent")
        print(f"{label}: {one_line(text, 600)}\n")


def cmd_act(a):
    action, key = a.act
    index, state = get_conns()
    src, _, sid = key.partition(":")
    state.execute("insert or ignore into flags(source, sid) values (?,?)", (src, sid))
    row = state.execute("select pinned, hidden, tag, note from flags where source=? and sid=?",
                        (src, sid)).fetchone()
    pinned, hidden, tag, note = row
    if action == "pin":
        pinned = 0 if pinned else 1
    elif action == "hide":
        hidden = 0 if hidden else 1
    elif action in ("tag", "note"):
        cur = tag if action == "tag" else note
        try:
            with open("/dev/tty", "r+") as tty:
                tty.write(f"{action} (current: {cur!r}; empty clears): ")
                tty.flush()
                val = tty.readline().rstrip("\n")
        except OSError:
            val = cur
        tag, note = (val.strip().lstrip("#"), note) if action == "tag" else (tag, val.strip())
    state.execute("update flags set pinned=?, hidden=?, tag=?, note=? where source=? and sid=?",
                  (pinned, hidden, tag, note, src, sid))
    state.commit()


def cmd_stats(a):
    index, state = get_conns()
    update_index(index)
    env = Env()
    recs = load_records(index)
    for name in BY_NAME:
        rs = [r for r in recs if r["source"] == name]
        missing = sum(1 for r in rs if not env.exists(r["cwd"]))
        print(f"{name:<11} sessions={len(rs):<4} folder-missing={missing}")
    print(f"total       {len(recs)}")


def cmd_config(a):
    print(f"# config file: {CONFIG_PATH} ({'found' if CONFIG_PATH.exists() else 'not present, using discovery only'})")
    for x in SPECS:
        where = x.get("dir") or x.get("db")
        extra = f"   launcher: {x['launcher']}" if x.get("launcher") else ""
        print(f"{x['name']:<12} type={x['type']:<7} {where}{extra}")


STARTER_CONFIG = """# aises configuration. Everything here is optional: agents are auto-discovered
# (~/.claude*, ~/.pi, the agy database). Run `aises --config` to see what was found.

# discover = true            # set false to use only the [[agent]] entries below
# disable  = ["pi"]          # hide discovered agents by name

# [ui]
# splash = true

# Add an agent, or override a discovered one with the same name.
# type is one of: claude, pi, agy
#
# [[agent]]
# name     = "claude_work"
# type     = "claude"
# dir      = "~/.claude-work"
# launcher = 'env -u ANTHROPIC_API_KEY CLAUDE_CONFIG_DIR="$HOME/.claude-work" claude'
#
# [[agent]]
# name = "pi"
# type = "pi"
# dir  = "~/.pi/agent/sessions"
#
# [[agent]]
# name = "agy"
# type = "agy"
# db   = "~/.gemini/antigravity-cli/conversation_summaries.db"
"""


def cmd_init_config(a):
    if CONFIG_PATH.exists():
        eprint(f"aises: {CONFIG_PATH} already exists; not overwriting")
        return 1
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(STARTER_CONFIG)
    eprint(f"aises: wrote {CONFIG_PATH}")


def cmd_init(a):
    print(f"""# add this to ~/.zshrc
aises() {{
  local out
  out="$({shlex.quote(SCRIPT)} "$@")" || return
  [[ -n "$out" ]] && eval "$out"
}}""")



def chip(key, label):
    return f"\x1b[1;30;48;5;208m {key} \x1b[0m \x1b[1m{label}\x1b[0m"


KEYS_HEADER = "\n".join("   ".join(chip(k, l) for k, l in row) for row in (
    [("ENTER", "resume"), ("^O", "print cmd"), ("^Y", "copy cmd"), ("ESC", "quit")],
    [("^P", "pin"), ("^X", "hide/unhide"), ("^T", "tag"), ("^N", "note"), ("^R", "show hidden")],
    [("^F", "search conversation text"), ("^G", "back to fuzzy")],
))

VERSION = "0.2"
LOGO = ("▄▀█ █ █▀ █▀▀ █▀", "█▀█ █ ▄█ ██▄ ▄█")


def splash(recs, env, t_start, min_seconds=1.1):
    """Boxed start-up banner on the terminal (stderr); erased before the picker opens."""
    if not sys.stderr.isatty():
        return
    counts = {}
    for r in recs:
        counts[r["source"]] = counts.get(r["source"], 0) + 1
    here = sum(1 for r in recs if r["cwd"] and env.real(r["cwd"]) == env.cwd_real)
    plain = [
        ("", None),
        (f"  {LOGO[0]}    aises v{VERSION}", "logo1"),
        (f"  {LOGO[1]}    find and resume your agent sessions", "logo2"),
        ("", None),
        (f"  {len(recs)} sessions   " + "  ".join(f"{k} {v}" for k, v in sorted(counts.items())), "dim"),
        (f"  here: {short_dir(os.getcwd(), 50)}  ({here} session{'s' if here != 1 else ''})", "dim"),
        ("", None),
        ("  tip: ^F searches the full text of every conversation", "tip"),
        ("", None),
    ]
    width = max(len(t) for t, _ in plain) + 2
    style = {"logo1": "1;38;5;208", "logo2": "1;38;5;208", "dim": "2", "tip": "38;5;114", None: "0"}
    out = ["\x1b[38;5;208m╭" + "─" * width + "╮\x1b[0m"]
    for text, kind in plain:
        body = f"\x1b[{style[kind]}m{text.ljust(width)}\x1b[0m"
        out.append(f"\x1b[38;5;208m│\x1b[0m{body}\x1b[38;5;208m│\x1b[0m")
    out.append("\x1b[38;5;208m╰" + "─" * width + "╯\x1b[0m")
    sys.stderr.write("\n".join(out) + "\n")
    sys.stderr.flush()
    time.sleep(max(0.0, min_seconds - (time.time() - t_start)))
    sys.stderr.write(f"\x1b[{len(out)}A\x1b[J")  # erase the banner
    sys.stderr.flush()


def cmd_view(a):
    """Called by fzf `transform`: print the next fzf actions for the current view."""
    mode = a.view_action
    prompt = os.environ.get("FZF_PROMPT", "> ")
    me = f"{shlex.quote(sys.executable)} {shlex.quote(SCRIPT)}"
    flags = ""
    if a.agent:
        flags += f" --agent {shlex.quote(','.join(a.agent))}"
    if a.folder:
        flags += f" --folder {shlex.quote(a.folder)}"
    lst, lst_h = f"{me} --list{flags}", f"{me} --list --show-hidden{flags}"
    fts = f'{me} --fts="$FZF_QUERY"{flags}'
    fuzzy = "enable-search+unbind(change)"
    if mode == "refresh":
        act = (f"reload({fts})" if prompt.startswith("text") else
               f"reload({lst_h})" if prompt.startswith("all") else f"reload({lst})")
    elif mode == "toggle-hidden":
        act = (f"{fuzzy}+change-prompt(> )+reload({lst})" if prompt.startswith("all")
               else f"{fuzzy}+change-prompt(all> )+reload({lst_h})")
    elif mode == "text":
        act = f"disable-search+change-prompt(text> )+rebind(change)+reload({fts})"
    elif mode == "change":
        act = f"reload({fts})"
    else:  # fuzzy
        act = f"{fuzzy}+change-prompt(> )+reload({lst})"
    print(act)


def fzf_pick(lines, header, prompt, extra=None, query=""):
    """Run fzf over TSV lines (key<TAB>display). Returns (pressed_key, selected_key) or None."""
    py = shlex.quote(sys.executable)
    me = f"{py} {shlex.quote(SCRIPT)}"
    cmd = ["fzf", "--ansi", "--delimiter", "\t", "--with-nth", "2..", "--tiebreak=index",
           "--layout=reverse", "--height=90%", "--prompt", prompt, "--header", header,
           "--query", query, "--expect", "ctrl-o,ctrl-y",
           "--preview", f"{me} --preview {{1}}", "--preview-window", "right:45%:wrap"]
    if extra:
        cmd += extra
    res = subprocess.run(cmd, input="\n".join(lines), text=True, stdout=subprocess.PIPE)
    if res.returncode != 0:
        return None
    out = res.stdout.split("\n")
    if len(out) < 2 or not out[1]:
        return None
    return out[0], out[1].split("\t")[0]


def interactive(a):
    py = shlex.quote(sys.executable)
    me = f"{py} {shlex.quote(SCRIPT)}"
    flags = ""
    if a.agent:
        flags += f" --agent {shlex.quote(','.join(a.agent))}"
    if a.folder:
        flags += f" --folder {shlex.quote(a.folder)}"
    view = f"transform({me} --view-action {{mode}}{flags})"
    binds = [
        "start:unbind(change)",
        f"ctrl-p:execute-silent({me} --act pin {{1}})+" + view.format(mode="refresh"),
        f"ctrl-x:execute-silent({me} --act hide {{1}})+" + view.format(mode="refresh"),
        f"ctrl-t:execute({me} --act tag {{1}})+" + view.format(mode="refresh"),
        f"ctrl-n:execute({me} --act note {{1}})+" + view.format(mode="refresh"),
        "ctrl-r:" + view.format(mode="toggle-hidden"),
        "ctrl-f:" + view.format(mode="text"),
        "change:" + view.format(mode="change"),
        "ctrl-g:" + view.format(mode="fuzzy"),
    ]
    extra = ["--header-border=rounded", "--header-label= keys "]
    for b in binds:
        extra += ["--bind", b]
    header = KEYS_HEADER
    t_start = time.time()
    index, state = get_conns()
    update_index(index)
    env, fl = Env(), load_flags(state)
    all_recs = load_records(index)
    recs = ordered(all_recs, fl, env, False, a.agent, a.folder)
    if not recs:
        eprint("aises: no sessions found")
        return 1
    if not a.no_splash and not a.query and CONFIG.get("ui", {}).get("splash", True):
        splash(all_recs, env, t_start)
    picked = fzf_pick([fmt_line(r, fl, env) for r in recs], header, "> ", extra, a.query or "")
    if not picked:
        return 1
    pressed, key = picked
    r = find(index, key)
    cwd, cmd = BY_NAME[r["source"]].resume(r)
    full = f"cd {shlex.quote(cwd)} && {cmd}" if cwd else cmd
    if pressed == "ctrl-y":
        subprocess.run(["pbcopy"], input=full, text=True)
        eprint(f"aises: copied: {full}")
        return 0
    if pressed == "ctrl-o":
        print(f"print -z -- {shlex.quote(full)}")
        return 0
    if cwd and not os.path.isdir(cwd):
        eprint(f"aises: folder no longer exists: {cwd}\naises: command placed on your prompt instead.")
        print(f"print -z -- {shlex.quote(full)}")
        return 0
    if sys.stdout.isatty():  # not wrapped by the shell function: cannot cd for the user
        eprint("aises: (run `aises.py --init` for the zsh wrapper that can cd for you)")
    print(full)
    return 0


def cmd_projects(a):
    index, state = get_conns()
    update_index(index)
    env, fl = Env(), load_flags(state)
    groups: dict[str, list] = {}
    for r in load_records(index):
        if fl.get((r["source"], r["sid"]), {}).get("hidden") and not a.show_hidden:
            continue
        if a.agent and r["source"] not in a.agent:
            continue
        groups.setdefault(env.real(r["cwd"]) or NOFOLDER, []).append(r)
    rows = sorted(groups.items(), key=lambda kv: -max(r["ts"] for r in kv[1]))
    lines = []
    for folder, rs in rows:
        last = dt.datetime.fromtimestamp(max(r["ts"] for r in rs)).strftime("%Y-%m-%d %H:%M")
        agents = {}
        for r in rs:
            agents[r["source"]] = agents.get(r["source"], 0) + 1
        ag = " ".join(c(COLOR.get(k, "37"), f"{k}×{v}") for k, v in sorted(agents.items()))
        mark = c("31", "✗") if folder != NOFOLDER and not env.exists(folder) else " "
        lines.append(f"{folder}\t{last} {mark}{short_dir(folder, 60):<60} {len(rs):>3}  {ag}")
    picked = fzf_pick(lines, "   ".join(chip(k, l) for k, l in [("ENTER", "sessions in that folder"), ("ESC", "quit")]), "project> ",
                      ["--expect", "ctrl-o,ctrl-y"], a.query or "")
    if not picked:
        return 1
    a.folder = picked[1]
    a.query = ""
    a.no_splash = True
    return interactive(a)


def main():
    ap = argparse.ArgumentParser(prog="aises", description="Find and resume past agent sessions.")
    ap.add_argument("query", nargs="?", help="initial fuzzy query")
    ap.add_argument("-p", "--projects", action="store_true", help="group by folder first")
    ap.add_argument("-a", "--agent", type=lambda s: s.split(","), help="comma list of agent names (see --config)")
    ap.add_argument("--folder", help="only sessions of this folder")
    ap.add_argument("--show-hidden", action="store_true")
    ap.add_argument("--stats", action="store_true", help="per-agent counts")
    ap.add_argument("--init", action="store_true", help="print the zsh wrapper function")
    ap.add_argument("--config", action="store_true", help="show the agents found and the config in use")
    ap.add_argument("--init-config", action="store_true", help="write a commented starter config file")
    for internal in ("--list", "--reindex"):
        ap.add_argument(internal, action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-splash", action="store_true", help="skip the start-up banner")
    ap.add_argument("--view-action", help=argparse.SUPPRESS)
    ap.add_argument("--fts", help=argparse.SUPPRESS)
    ap.add_argument("--preview", help=argparse.SUPPRESS)
    ap.add_argument("--act", nargs=2, help=argparse.SUPPRESS)
    a = ap.parse_args()
    try:
        if a.init:
            return cmd_init(a)
        if a.config:
            return cmd_config(a)
        if a.init_config:
            return cmd_init_config(a)
        if a.stats:
            return cmd_stats(a)
        if a.reindex:
            (CACHE_DIR / "index.db").unlink(missing_ok=True)
            ALIAS_CACHE.unlink(missing_ok=True)
            return cmd_stats(a)
        if a.view_action:
            return cmd_view(a)
        if a.list:
            return cmd_list(a)
        if a.fts is not None:
            return cmd_fts(a)
        if a.preview:
            return cmd_preview(a)
        if a.act:
            return cmd_act(a)
        if subprocess.run(["which", "fzf"], capture_output=True).returncode:
            eprint("aises: fzf is required (brew install fzf)")
            return 2
        return cmd_projects(a) if a.projects else interactive(a)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
