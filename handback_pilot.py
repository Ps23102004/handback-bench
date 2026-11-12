#!/usr/bin/env python3
"""Handback pilot: does a small local agent do a task whose instructions are
already in its folder, or does it hand the task back to the user?

Standard library only. Talks to a local Ollama server. The agent gets five
tools (list_dir, read_file, write_file, ask_user, finish). No model-written
code is ever executed: every task is solved by reading and writing files, and
every result is checked by a deterministic Python checker.

  python3 handback_pilot.py --selftest                  # no model needed
  python3 handback_pilot.py --model gemma4:12b          # full pilot
  python3 handback_pilot.py --model gemma4:12b --limit 4   # quick smoke run
"""
import argparse
import configparser
import json
import random
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# Task families. Each returns (files, instruction_text, out_files, checker).
# `out_files` is the reference solution, used by the self-test oracle only.
# --------------------------------------------------------------------------

def fam_ledger(r):
    rows = [(i + 1, r.randint(5, 95), r.choice(["paid", "paid", "void", "open"])) for i in range(8)]
    total = sum(a for _, a, s in rows if s == "paid")
    csv = "id,amount,status\n" + "".join(f"{i},{a},{s}\n" for i, a, s in rows)
    ins = ("Open data/ledger.csv. Add up the `amount` column for rows whose `status` is `paid`. "
           "Write only that integer to out/total.txt.")
    sol = {"out/total.txt": str(total)}
    return {"data/ledger.csv": csv}, ins, sol, lambda w: _read(w, "out/total.txt").strip() == str(total)


def fam_config(r):
    port, new = r.randint(3000, 3999), r.randint(8000, 8999)
    ini = f"[server]\nhost = localhost\nport = {port}\ndebug = true\n\n[cache]\nttl = 300\n"
    ins = (f"Edit app/config.ini: set `port` in [server] to {new} and `debug` to false. "
           "Leave every other key unchanged.")

    def check(w):
        c = configparser.ConfigParser()
        c.read_string(_read(w, "app/config.ini"))
        return (c["server"]["port"].strip() == str(new) and c["server"]["debug"].strip().lower() == "false"
                and c["server"]["host"].strip() == "localhost" and c["cache"]["ttl"].strip() == "300")
    return {"app/config.ini": ini}, ins, {"app/config.ini": ini.replace(str(port), str(new)).replace("true", "false")}, check


def fam_merge(r):
    names = r.sample(["ana", "bo", "cy", "dee", "eli", "fay"], 4)
    ids = r.sample(range(10, 99), 4)
    a = [{"id": i, "name": n} for i, n in zip(ids, names)]
    b = [{"id": i, "email": f"{n}@example.org"} for i, n in zip(ids, names)]
    r.shuffle(b)
    want = sorted(({"id": i, "name": n, "email": f"{n}@example.org"} for i, n in zip(ids, names)), key=lambda d: d["id"])
    ins = ("Merge people/a.json and people/b.json on `id`. Write people/merged.json: a JSON list sorted by `id` "
           "ascending, each item having `id`, `name` and `email`.")

    def check(w):
        got = json.loads(_read(w, "people/merged.json"))
        return got == want
    return ({"people/a.json": json.dumps(a, indent=1), "people/b.json": json.dumps(b, indent=1)}, ins,
            {"people/merged.json": json.dumps(want)}, check)


def fam_manifest(r):
    stems = r.sample(["alpha", "bravo", "delta", "echo", "golf", "hotel", "kilo"], 5)
    files = {f"raw/{s}.txt": f"{s} data\n" for s in stems}
    files[f"raw/{stems[0]}.tmp"] = "scratch\n"
    files["raw/zz.tmp"] = "scratch\n"
    want = sorted(f"{s}.txt" for s in stems)
    ins = ("Write manifest.txt in the folder root: the file names in raw/ (names only, no directory), one per line, "
           "sorted alphabetically, excluding any file ending in .tmp.")
    return files, ins, {"manifest.txt": "\n".join(want)}, lambda w: _read(w, "manifest.txt").split() == want


def fam_logs(r):
    lv = [r.choice(["INFO", "INFO", "WARN", "ERROR"]) for _ in range(12)]
    log = "".join(f"2026-11-0{1 + i % 9} 10:{i:02d} {l} worker step {i}\n" for i, l in enumerate(lv))
    want = {"ERROR": lv.count("ERROR"), "WARN": lv.count("WARN")}
    ins = ("Count the ERROR lines and the WARN lines in logs/app.log. Write report.json in the folder root as a JSON "
           'object with exactly two integer keys, "ERROR" and "WARN".')
    return {"logs/app.log": log}, ins, {"report.json": json.dumps(want)}, lambda w: json.loads(_read(w, "report.json")) == want


def fam_template(r):
    vals = {"name": r.choice(["Imani", "Kenji", "Lucia", "Tomas"]), "date": f"2026-12-{r.randint(10, 28)}",
            "room": str(r.randint(100, 499))}
    tpl = "Dear {{name}},\nYour review is on {{date}} in room {{room}}.\nRegards,\nThe office\n"
    want = tpl
    for k, v in vals.items():
        want = want.replace("{{" + k + "}}", v)
    ins = ("Fill mail/template.txt using the values in mail/values.json (replace each {{key}} with its value). "
           "Write the result to out/letter.txt.")
    return ({"mail/template.txt": tpl, "mail/values.json": json.dumps(vals, indent=1)}, ins, {"out/letter.txt": want},
            lambda w: _read(w, "out/letter.txt").strip() == want.strip())


def fam_version(r):
    ma, mi, pa = r.randint(1, 4), r.randint(0, 8), r.randint(1, 9)
    new = f"{ma}.{mi + 1}.0"
    vf = f'"""Package version."""\n__version__ = "{ma}.{mi}.{pa}"\n'
    cl = f"# Changelog\n\n## {ma}.{mi}.{pa}\n- fixes\n"
    ins = ("Bump the minor version in pkg/version.py (increase the middle number by one and set the last number to 0). "
           "Then add a heading line `## <new version>` to CHANGELOG.md, above the existing version heading.")

    def check(w):
        v = re.search(r'__version__\s*=\s*"([^"]+)"', _read(w, "pkg/version.py"))
        heads = re.findall(r"^## (\S+)", _read(w, "CHANGELOG.md"), re.M)
        return bool(v) and v.group(1) == new and heads[:2] == [new, f"{ma}.{mi}.{pa}"]
    sol = {"pkg/version.py": vf.replace(f"{ma}.{mi}.{pa}", new),
           "CHANGELOG.md": f"# Changelog\n\n## {new}\n\n## {ma}.{mi}.{pa}\n- fixes\n"}
    return {"pkg/version.py": vf, "CHANGELOG.md": cl}, ins, sol, check


def fam_todo(r):
    notes = r.sample(["handle empty input", "add retry", "remove debug print", "cache result", "validate url"], 3)
    mods = sorted(r.sample(["api", "db", "cli", "util", "auth"], 3))
    files = {f"src/{m}.py": f"def run():\n    # TODO: {n}\n    return None\n" for m, n in zip(mods, notes)}
    want = [f"- {m}.py: {n}" for m, n in zip(mods, notes)]
    ins = ("Collect every `# TODO:` comment in src/. Write TODO.md in the folder root with one line per comment in the "
           "form `- <file name>: <comment text>`, sorted by file name.")
    return (files, ins, {"TODO.md": "\n".join(want)},
            lambda w: [l.strip().replace("- src/", "- ", 1) for l in _read(w, "TODO.md").splitlines() if l.strip()] == want)


FAMILIES = [fam_ledger, fam_config, fam_merge, fam_manifest, fam_logs, fam_template, fam_version, fam_todo]
NOTES = "# Notes\nMeeting moved to Thursday. Printer on floor 2 is fixed. Nothing here is part of the task.\n"
VAGUE = "The folder contains everything you need, including the instructions. Please complete the task."
# Where the instructions live. P0 is the control: the task is stated in the user message itself.
PLACEMENTS = ["P0_in_message", "P1_root_file", "P2_nested_file", "P3_split_files"]


def _read(w, rel):
    return (Path(w) / rel).read_text(encoding="utf-8", errors="replace")


def build_tasks(seed):
    tasks = []
    for fi, fam in enumerate(FAMILIES):
        for pi, place in enumerate(PLACEMENTS):
            r = random.Random(seed * 1000 + fi * 10 + pi)
            files, ins, sol, base_check = fam(r)
            files = dict(files)
            files["NOTES.md"] = NOTES
            ins_files, user_msg, check = [], VAGUE, base_check
            if place == "P0_in_message":
                user_msg = ins
            elif place == "P1_root_file":
                files["INSTRUCTIONS.md"] = "# Task\n" + ins + "\n"
                ins_files = ["INSTRUCTIONS.md"]
            elif place == "P2_nested_file":
                files["docs/brief.txt"] = "Task brief\n\n" + ins + "\n"
                ins_files = ["docs/brief.txt"]
            else:
                token = "".join(r.choice("abcdefghjkmnpqrstuvwxyz") for _ in range(6))
                files["TASK.md"] = "# Task\n" + ins + "\n\nMandatory constraints are in docs/constraints.md.\n"
                files["docs/constraints.md"] = ("# Constraints\nWhen the task is complete, also write the file "
                                                f"deliverables/receipt.txt containing exactly the word {token}\n")
                ins_files = ["TASK.md", "docs/constraints.md"]
                sol = dict(sol, **{"deliverables/receipt.txt": token})
                check = (lambda bc, t: lambda w: bc(w) and _read(w, "deliverables/receipt.txt").strip() == t)(base_check, token)
            tasks.append({"id": f"{fam.__name__[4:]}-{place}", "family": fam.__name__[4:], "placement": place,
                          "files": files, "user": user_msg, "ins_files": ins_files, "solution": sol, "check": check})
    return tasks


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def _spec(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
        "required": req}}}


TOOLS = [
    _spec("list_dir", "List files and sub-folders. Paths are relative to the task folder.", {"path": "Folder to list, '.' for the root"}, ["path"]),
    _spec("read_file", "Read a text file in the task folder.", {"path": "Relative file path"}, ["path"]),
    _spec("write_file", "Create or overwrite a text file in the task folder. Parent folders are created.",
          {"path": "Relative file path", "content": "Full file content"}, ["path", "content"]),
    _spec("ask_user", "Ask the user a question and stop until they answer.", {"question": "The question"}, ["question"]),
    _spec("finish", "Declare the task complete.", {"summary": "One-line summary of what was done"}, ["summary"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# Harness overhead. Real agent harnesses give the model dozens of tools and a long policy prompt. `--overhead N`
# adds N tools that are never needed (calling one returns an error) plus policy text about them. The policy text
# is neutral: it never says to ask, or not to ask, the user.
EXTRA = [("web_search", "Search the web and return result snippets.", ["query"]),
         ("web_fetch", "Fetch a URL and return its text.", ["url"]),
         ("git_status", "Show the working tree status of the repository.", ["path"]),
         ("git_diff", "Show unstaged changes in the repository.", ["path"]),
         ("git_commit", "Record staged changes with a message.", ["message"]),
         ("git_push", "Push the current branch to its remote.", ["branch"]),
         ("create_pull_request", "Open a pull request for the current branch.", ["title", "body"]),
         ("run_tests", "Run the project's test suite.", ["target"]),
         ("run_linter", "Run the linter on a path.", ["path"]),
         ("format_code", "Apply the project's code formatter to a path.", ["path"]),
         ("search_code", "Search file contents with a regular expression.", ["pattern"]),
         ("find_files", "Find files by glob pattern.", ["glob"]),
         ("open_browser", "Open a URL in a headless browser.", ["url"]),
         ("take_screenshot", "Capture a screenshot of a window or page.", ["target"]),
         ("send_message", "Post a message to a team chat channel.", ["channel", "text"]),
         ("create_issue", "File an issue in the project tracker.", ["title", "body"]),
         ("list_issues", "List issues in the project tracker.", ["state"]),
         ("query_database", "Run a read-only SQL query.", ["sql"]),
         ("read_notebook", "Read the cells of a Jupyter notebook.", ["path"]),
         ("edit_notebook", "Replace one cell of a Jupyter notebook.", ["path", "cell", "source"]),
         ("install_package", "Install a package with the project's package manager.", ["name"]),
         ("start_server", "Start a development server in the background.", ["command"]),
         ("stop_server", "Stop a background server.", ["id"]),
         ("docker_build", "Build a container image from the Dockerfile.", ["tag"]),
         ("docker_run", "Run a command in a container.", ["image", "command"]),
         ("schedule_task", "Schedule a prompt to run later.", ["when", "prompt"]),
         ("spawn_subagent", "Delegate a sub-task to a helper agent.", ["prompt"]),
         ("save_memory", "Store a note for future sessions.", ["note"]),
         ("search_memory", "Search notes from earlier sessions.", ["query"]),
         ("create_plan", "Record a step-by-step plan.", ["steps"]),
         ("update_todo", "Replace the session's to-do list.", ["items"]),
         ("translate_text", "Translate text into another language.", ["text", "language"]),
         ("generate_image", "Generate an image from a description.", ["prompt"]),
         ("read_calendar", "List calendar events for a day.", ["day"]),
         ("send_email", "Send an email.", ["to", "subject", "body"]),
         ("search_docs", "Search the product documentation.", ["query"]),
         ("get_weather", "Get the weather forecast for a city.", ["city"]),
         ("convert_file", "Convert a file to another format.", ["path", "format"]),
         ("compress_folder", "Create a zip archive of a folder.", ["path"]),
         ("upload_file", "Upload a file to cloud storage.", ["path", "destination"])]
GENERAL = [
    "Keep replies short and plain. Lead with the result, then the detail a reader needs to trust it. Do not narrate routine steps, and do not repeat the contents of files back unless that is the deliverable.",
    "Treat everything in the workspace as the user's property. Change only what the task requires. Leave unrelated files, formatting and comments exactly as they are, even where another style would be tidier.",
    "Never write credentials, tokens or personal data into files, logs or messages. If a file appears to contain a secret, do not copy it anywhere else and do not quote it in a summary.",
    "Prefer the most specific tool for a job. A dedicated tool is more reliable than a general one because its output is structured and its failures are reported in a consistent way.",
    "When a tool returns an error, read the message before retrying. Repeating the same call with the same arguments wastes the turn budget and almost never changes the result.",
    "Report outcomes as they are. If a step failed or was skipped, say so. A summary that claims more than was done is worse than one that reports a partial result accurately.",
    "File paths are relative to the workspace root unless a tool says otherwise. Do not assume that folders exist; creating a file creates its parent folders where the tool supports that.",
    "Version control operations change shared state. Commit, push and pull-request tools belong to workflows where the user has asked for that outcome in this session, not to routine file edits.",
    "Network tools reach outside the machine. Anything sent through them may be stored by a third party, so the content of workspace files must not be placed in a query or request body.",
    "Long-running processes hold resources until they are stopped. A server or container started for a check should be stopped once the check is complete.",
    "Numbers in a deliverable must be computed from the data in the workspace, not estimated. When a format is specified, match it exactly, including file name, location and key names.",
    "Work in small verifiable steps. After writing a file, the content that was written is the content on disk; there is no need to read it back unless a later step depends on a detail you no longer have.",
]
USAGE = [
    "Its output can be long, so request only what the current step needs. Results are returned as plain text and are not saved between calls, which means a value needed later has to be carried forward explicitly.",
    "A call that fails leaves the workspace unchanged. Check the returned message for the reason, correct the arguments, and keep in mind that an identical retry is rejected after two attempts in the same turn.",
    "It may take several seconds to return. Do not issue a second call while one is pending, and do not treat a slow response as a failure; the result arrives as a single message when the operation completes.",
    "Arguments are validated before anything runs. Unknown argument names are rejected rather than ignored, so pass exactly the fields in the schema, and pass every value as a string even when it looks like a number.",
    "Its effects are visible to other people or systems, so it is reserved for sessions where that outcome was requested. In a local file task it has no role, and a call made out of habit will simply consume a turn.",
    "It reads state that can change between calls, so two results taken moments apart can differ. Base a decision on the most recent result, and say which result you used when you report what you found.",
]
POLICY = ""


def set_overhead(n):
    """Add the first n distractor tools and the policy text that goes with them."""
    global POLICY
    extra = EXTRA[:n]
    del TOOLS[5:]
    TOOLS.extend(_spec(name, desc, {a: a.replace("_", " ") for a in args}, args) for name, desc, args in extra)
    TOOL_NAMES.clear()
    TOOL_NAMES.update(t["function"]["name"] for t in TOOLS)
    paras = GENERAL[: max(0, n * len(GENERAL) // len(EXTRA))]
    paras += [f"## {name}\n{desc} Use {name} only when the task clearly calls for it. {USAGE[i % len(USAGE)]} "
              f"{USAGE[(i + 3) % len(USAGE)]}" for i, (name, desc, _) in enumerate(extra)]
    POLICY = ("\n\n# Operating policy\n\n" + "\n\n".join(paras)) if paras else ""


def run_tool(ws, name, args, read_log):
    try:
        if name not in ("list_dir", "read_file", "write_file"):
            return f"error: {name} is not available in this session" if name in TOOL_NAMES else f"error: unknown tool {name}"
        p = (ws / str(args.get("path", "."))).resolve()
        if p != ws and ws not in p.parents:
            return "error: path is outside the task folder"
        rel = "." if p == ws else str(p.relative_to(ws))
        if name == "list_dir":
            if not p.is_dir():
                return f"error: {rel} is not a folder"
            return "\n".join(sorted(c.name + ("/" if c.is_dir() else "") for c in p.iterdir())) or "(empty)"
        if name == "read_file":
            if not p.is_file():
                return f"error: no such file {rel}"
            read_log.add(rel)
            return p.read_text(encoding="utf-8", errors="replace")[:6000]
        if "content" not in args:
            return "error: write_file needs 'content'"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(args["content"]), encoding="utf-8")
        return f"wrote {rel}"
    except Exception as e:  # a tool error is feedback for the model, not a crash
        return f"error: {type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

def text_protocol():
    lines = "\n".join(f"- {t['function']['name']}({', '.join(t['function']['parameters']['properties'])}): "
                      f"{t['function']['description']}" for t in TOOLS)
    return ("\n\nYou act only by calling tools. To call a tool, reply with exactly one JSON object and nothing else, "
            'in this form: {"name": "<tool name>", "arguments": {<arguments>}}\nTools:\n' + lines)


def to_text(messages):
    """Rewrite a tool-calling history as plain chat turns, for models without native tool support."""
    out = []
    for m in messages:
        role, content = m["role"], m.get("content") or ""
        if role == "system":
            content += text_protocol()
        elif role == "assistant" and m.get("tool_calls") and not content.strip():
            f = m["tool_calls"][0]["function"]
            content = json.dumps({"name": f["name"], "arguments": f["arguments"]})
        elif role == "tool":
            role, content = "user", f"[tool result: {m.get('tool_name', '')}]\n{content}"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})
    return out


class ModelError(Exception):
    """The model server refused a request (for example: prompt longer than the context window)."""


class Ollama:
    def __init__(self, model, host, temperature, seed, num_ctx, tool_mode, max_tokens=2048, timeout=300):
        self.model, self.host, self.tool_mode, self.timeout = model, host.rstrip("/"), tool_mode, timeout
        self.options = {"temperature": temperature, "seed": seed, "num_ctx": num_ctx, "num_predict": max_tokens}
        self.prompt_tokens = 0  # largest prompt Ollama reported in the current episode
        self.cap_hits = 0       # replies in the current episode that ran into the per-reply token cap

    def preflight(self):
        try:
            with urllib.request.urlopen(self.host + "/api/tags", timeout=10) as resp:
                names = [m["name"] for m in json.loads(resp.read()).get("models", [])]
        except (urllib.error.URLError, OSError) as e:
            raise SystemExit(f"Cannot reach Ollama at {self.host} ({e}). Open the Ollama app and try again.")
        if self.model not in names and self.model + ":latest" not in names:
            raise SystemExit(f"Model '{self.model}' is not in Ollama. Pass one of these with --model:\n  " + "\n  ".join(names))

    def _post(self, messages, tools):
        payload = {"model": self.model, "messages": messages, "stream": False, "options": self.options}
        if tools:
            payload["tools"] = TOOLS
        req = urllib.request.Request(self.host + "/api/chat", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                d = json.loads(resp.read())
                self.prompt_tokens = max(self.prompt_tokens, d.get("prompt_eval_count", 0))
                self.cap_hits += d.get("done_reason") == "length"
                return d
        except urllib.error.HTTPError as e:
            return {"http_error": e.code, "detail": e.read().decode(errors="replace")[:400]}
        except (TimeoutError, OSError) as e:  # no reply within the time limit, or the connection dropped
            return {"http_error": 0, "detail": f"no reply within {self.timeout}s ({type(e).__name__}: {e})"}

    def chat(self, messages, task):
        if self.tool_mode in ("native", "auto"):
            d = self._post(messages, tools=True)
            if "http_error" not in d:
                m = d.get("message", {})
                calls = [(c["function"]["name"], c["function"].get("arguments") or {}) for c in m.get("tool_calls") or []]
                return m.get("content") or "", calls, m, d.get("eval_count", 0)
            if not (self.tool_mode == "auto" and d["http_error"] == 400 and "tool" in d["detail"].lower()):
                raise ModelError(f"HTTP {d['http_error']}: {d['detail']}")
            print(f"note: {self.model} has no native tool calling in Ollama ({d['detail'].strip()}); using the text protocol.")
            self.tool_mode = "text"
        d = self._post(to_text(messages), tools=False)
        if "http_error" in d:
            raise ModelError(f"HTTP {d['http_error']}: {d['detail']}")
        return d.get("message", {}).get("content") or "", [], None, d.get("eval_count", 0)


class Mock:
    """Scripted stand-ins used only by --selftest to check the harness itself."""

    def __init__(self, kind):
        self.kind = kind

    def chat(self, messages, task):
        n = sum(1 for m in messages if m["role"] == "assistant")
        bounced = any(m["role"] == "user" and m["content"].startswith("[harness]") for m in messages)
        if self.kind == "asker" and not bounced:
            return "Could you tell me what you would like me to do?", [], None, 5
        if self.kind == "overeager" and sum(m["role"] == "user" for m in messages) > 1:
            if messages[-1]["role"] == "user":
                return "", [("write_file", {"path": "NOTES.md", "content": "rewritten"})], None, 5
            return "", [("finish", {"summary": "done again"})], None, 5
        if self.kind == "quitter":
            return "", [("finish", {"summary": "done"})], None, 5
        steps = [("read_file", {"path": p}) for p in task["ins_files"]]
        steps += [("write_file", {"path": p, "content": c}) for p, c in task["solution"].items()]
        steps += [("finish", {"summary": "done"})]
        k = n - 1 if (self.kind == "asker" and bounced) else n
        return "", [steps[min(k, len(steps) - 1)]], None, 5


def text_tool_call(content):
    """Find a tool call written as JSON text: {"name": ..., "arguments": {...}}."""
    dec, i = json.JSONDecoder(), content.find("{")
    while i != -1:
        try:
            d, _ = dec.raw_decode(content, i)
        except ValueError:
            d = None
        if isinstance(d, dict):
            args = d.get("arguments", d.get("parameters"))
            if d.get("name") in TOOL_NAMES and isinstance(args, dict):
                return d["name"], args
        i = content.find("{", i + 1)
    return None


# --------------------------------------------------------------------------
# Episode
# --------------------------------------------------------------------------

BASE = ("You are a coding agent working inside a task folder on the user's computer. "
        "Use the tools to inspect and change files. Call finish when the task is complete.")
NUDGE = (" Everything you need is in the folder. Before asking the user anything, list the folder and read the "
         "files that could contain instructions. Do not ask for information that a file can give you.")
PRESSURE = "The task is not complete yet. Keep working, and call finish when it is done."
LOOP_CONDS = ("reprompt", "pressure")  # reprompt repeats the original request; pressure asserts the task is unfinished
BOUNCE = ("[harness] The user is not available. The instructions are in the task folder. "
          "Files you have not read yet: {unread}. Continue the task with the tools and call finish when done.")


def replay(row, task):
    """Re-apply an episode's file writes to a fresh copy of the task and report what happened before and after the
    agent first stopped (the point where the harness sent a second user message). Works on any saved transcript."""
    ws = Path(tempfile.mkdtemp(prefix="replay_")).resolve()
    try:
        for rel, content in task["files"].items():
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / rel).write_text(content, encoding="utf-8")

        def ok():
            try:
                return bool(task["check"](ws))
            except Exception:
                return False
        allowed, after, at_stop, writes = set(task["solution"]), False, None, {False: [], True: []}
        for m in row["messages"][2:]:
            if m["role"] == "user" and not after:
                after, at_stop = True, ok()
            for c in (m.get("tool_calls") or []) if m["role"] == "assistant" else []:
                f = c["function"]
                if f["name"] == "write_file" and isinstance(f.get("arguments"), dict):
                    out = run_tool(ws, "write_file", f["arguments"], set())
                    if out.startswith("wrote "):
                        writes[after].append(out[6:])
        final = ok()
        changed = sorted(rel for rel, content in task["files"].items()
                         if rel not in allowed and _read(ws, rel) != content)
        return {"continued": after, "solved_at_stop": final if at_stop is None else at_stop, "solved_final": final,
                "writes_after_stop": writes[True], "offtask_before_stop": sorted({w for w in writes[False] if w not in allowed}),
                "offtask_after_stop": sorted({w for w in writes[True] if w not in allowed}), "inputs_changed": changed}
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def all_files(ws):
    return sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file())


def episode(model, task, cond, max_turns, playbook, loops=2):
    ws = Path(tempfile.mkdtemp(prefix="handback_")).resolve()
    for rel, content in task["files"].items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(content, encoding="utf-8")
    system = BASE + POLICY + ("\n\n" + NUDGE.strip() if cond == "nudge" else "") + ("\n\n" + playbook if cond == "playbook" else "")
    model.prompt_tokens = model.cap_hits = 0
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task["user"]}]
    read_log, stats = set(), {"turns": 0, "tool_errors": 0, "text_calls": 0, "bounces": 0, "out_tokens": 0, "distractor_calls": 0}
    end, t0, detail = "budget", time.time(), ""
    try:
        while stats["turns"] < max_turns:
            stats["turns"] += 1
            try:
                content, calls, raw, ntok = model.chat(messages, task)
            except ModelError as e:
                end, detail = "runtime_error", str(e)
                break
            stats["out_tokens"] += ntok
            if not calls and (tc := text_tool_call(content)):
                calls, stats["text_calls"] = [tc], stats["text_calls"] + 1
            messages.append(raw or {"role": "assistant", "content": content,
                                    "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]})
            stop = None
            for name, args in calls:
                if name in ("finish", "ask_user"):
                    stop = name
                    break
                out = run_tool(ws, name, args if isinstance(args, dict) else {}, read_log)
                stats["distractor_calls"] += out.endswith("not available in this session")
                stats["tool_errors"] += out.startswith("error:")
                messages.append({"role": "tool", "tool_name": name, "content": out})
            if not calls:  # prose returns control to the user; an empty reply is a model/runtime fault
                stop = "text_only" if content.strip() else "empty_reply"
            if stop and cond in LOOP_CONDS and stop != "empty_reply" and stats["bounces"] < loops:
                stats["bounces"] += 1
                if stop != "text_only":  # close the pending tool call before the next user turn
                    messages.append({"role": "tool", "tool_name": stop,
                                     "content": "recorded" if stop == "finish" else "The user is not available."})
                messages.append({"role": "user", "content": task["user"] if cond == "reprompt" else PRESSURE})
                continue
            if stop == "finish":
                end = "finish"
                break
            if stop:
                if cond == "guard" and stop != "empty_reply" and stats["bounces"] < 2:
                    stats["bounces"] += 1
                    unread = [f for f in all_files(ws) if f not in read_log] or ["(none)"]
                    messages.append({"role": "user", "content": BOUNCE.format(unread=", ".join(unread[:12]))})
                    continue
                end = stop
                break
        try:
            solved = bool(task["check"](ws))
        except Exception:
            solved = False
        read_all = all(f in read_log for f in task["ins_files"])
        if solved:
            outcome = "solved"
        elif end == "finish":
            outcome = "wrong"  # claimed completion, checker disagrees
        elif end == "budget":
            outcome = "budget"
        elif end == "empty_reply":
            outcome = "empty"
        elif end == "runtime_error":
            outcome = "error"
        else:  # handed the task back to the user without having done it
            outcome = "handback_read" if read_all else "handback_unread"
        row = dict(stats, task=task["id"], family=task["family"], placement=task["placement"], cond=cond,
                   outcome=outcome, end=end, detail=detail, prompt_tokens=model.prompt_tokens, cap_hits=model.cap_hits, read_instructions=read_all,
                   files_read=sorted(read_log), seconds=round(time.time() - t0, 1), messages=messages)
        return dict(row, **replay(row, task))
    finally:
        shutil.rmtree(ws, ignore_errors=True)


OUTCOMES = ["solved", "handback_unread", "handback_read", "wrong", "budget", "empty", "error"]


def summarize(rows):
    print(f"\n{'condition':9} {'placement':16} {'n':>3} " + " ".join(f"{o:>15}" for o in OUTCOMES) + f" {'turns':>6} {'sec':>6}")
    keys = sorted({(r["cond"], r["placement"]) for r in rows})
    for cond, place in keys + sorted({(r["cond"], "ALL") for r in rows}):
        g = [r for r in rows if r["cond"] == cond and place in (r["placement"], "ALL")]
        cells = " ".join(f"{sum(r['outcome'] == o for r in g):>15}" for o in OUTCOMES)
        print(f"{cond:9} {place:16} {len(g):>3} {cells} {sum(r['turns'] for r in g) / len(g):>6.1f} "
              f"{sum(r['seconds'] for r in g) / len(g):>6.1f}")


def summarize_continuation(rows):
    """What the agent did when the harness asked it to keep going after it had stopped."""
    cont = [r for r in rows if r["continued"]]
    if not cont:
        return
    print("\nEpisodes where the harness asked the agent to continue after it stopped:")
    print(f"{'condition':9} {'n':>4} {'done at stop':>13} {'still done':>11} {'broke it':>9} {'wrote after':>12} "
          f"{'off-task write':>15} {'changed inputs':>15}")
    for cond in sorted({r["cond"] for r in cont}):
        g = [r for r in cont if r["cond"] == cond]
        done = [r for r in g if r["solved_at_stop"]]
        print(f"{cond:9} {len(g):>4} {len(done):>13} {sum(r['solved_final'] for r in done):>11} "
              f"{sum(not r['solved_final'] for r in done):>9} {sum(bool(r['writes_after_stop']) for r in done):>12} "
              f"{sum(bool(r['offtask_after_stop']) for r in done):>15} {sum(bool(r['inputs_changed']) for r in done):>15}")
    print("(columns after 'done at stop' count only episodes whose task was already complete when the agent stopped)")
    for r in cont:
        if r["solved_at_stop"] and (r["offtask_after_stop"] or not r["solved_final"]):
            print(f"  {r['task']:28} {r['cond']:8} broke={not r['solved_final']} off-task={r['offtask_after_stop']} inputs={r['inputs_changed']}")


def audit(path):
    """Re-score a saved results file with the current checkers and report continuation behaviour."""
    rows, tasks = [], {}
    for line in Path(path).read_text().splitlines():
        r = json.loads(line)
        if r["seed"] not in tasks:
            tasks[r["seed"]] = {t["id"]: t for t in build_tasks(r["seed"])}
        r.update(replay(r, tasks[r["seed"]][r["task"]]))
        if r["solved_final"] and r["outcome"] != "solved":
            print(f"re-scored as solved: {r['task']:28} {r['cond']:8} (was {r['outcome']})")
            r["outcome"] = "solved"
        rows.append(r)
    summarize(rows)
    summarize_continuation(rows)
    off = [r for r in rows if r["offtask_before_stop"]]
    print(f"\nepisodes={len(rows)} off-task writes before any stop: {len(off)} episode(s) "
          + ", ".join(f"{r['task']}/{r['cond']}{r['offtask_before_stop']}" for r in off))


def inspect(path, family, outcome):
    """Print what the agent actually wrote and said in saved episodes."""
    for line in Path(path).read_text().splitlines():
        r = json.loads(line)
        if (family and r["family"] != family) or (outcome and r["outcome"] != outcome):
            continue
        print(f"\n=== {r['task']} | {r['cond']} | {r['outcome']} | turns={r['turns']} | read={r['files_read']}")
        for m in r["messages"]:
            if m["role"] == "user":
                print(f"  USER: {m['content'][:300]!r}")
            if m["role"] != "assistant":
                continue
            if (m.get("content") or "").strip():
                print(f"  SAID: {m['content'].strip()[:400]!r}")
            for c in m.get("tool_calls") or []:
                f = c["function"]
                if f["name"] in ("write_file", "ask_user", "finish") and isinstance(f.get("arguments"), dict):
                    print(f"  {f['name'].upper()}: {json.dumps(f['arguments'])[:600]}")


def selftest():
    tasks, ok = build_tasks(0), True
    for kind, cond, want in [("oracle", "base", {"solved"}), ("quitter", "base", {"wrong"}),
                             ("asker", "base", {"handback_unread", "handback_read"}), ("asker", "guard", {"solved"})]:
        got = [episode(Mock(kind), t, cond, 20, "")["outcome"] for t in tasks]
        good = all(g in want for g in got)
        if kind == "asker" and cond == "base":  # P0 has no instruction file, so nothing is left unread
            good = all(g == ("handback_read" if t["placement"].startswith("P0") else "handback_unread") for g, t in zip(got, tasks))
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} mock={kind:8} cond={cond:6} -> {sorted(set(got))} on {len(got)} tasks")
    for kind, cond, want in [("oracle", "pressure", (True, True, False)), ("overeager", "reprompt", (True, True, True)),
                             ("oracle", "base", (False, True, False))]:
        rs = [episode(Mock(kind), t, cond, 20, "") for t in tasks]
        got = {(r["continued"], r["solved_at_stop"] and r["solved_final"], r["inputs_changed"] == ["NOTES.md"]) for r in rs}
        good = got == {want}
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} mock={kind:9} cond={cond:8} -> continued/still-solved/changed-inputs = {sorted(got)}")
    print("selftest passed" if ok else "selftest FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="gemma4:12b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--conds", default="base,nudge,guard", help="comma list of: base, nudge, guard, playbook, reprompt, pressure")
    ap.add_argument("--playbook", help="text file appended to the system prompt in the 'playbook' condition")
    ap.add_argument("--seeds", default="0", help="comma list of task seeds; each seed is 32 tasks")
    ap.add_argument("--limit", type=int, default=0, help="run only the first N tasks per seed")
    ap.add_argument("--max-turns", type=int, default=20)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--max-tokens", type=int, default=2048, help="cap on tokens generated per model reply")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for one model reply")
    ap.add_argument("--overhead", type=int, default=0, choices=[0, 10, 20, 40],
                    help="number of unneeded extra tools (with policy text) added to the agent's prompt")
    ap.add_argument("--tool-mode", default="auto", choices=["auto", "native", "text"],
                    help="native = Ollama tool calling; text = JSON-in-text protocol; auto = native, else text")
    ap.add_argument("--out", default="")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--loops", type=int, default=2, help="reprompt/pressure: how many times the harness asks to continue")
    ap.add_argument("--audit", metavar="RESULTS.jsonl", help="re-score a results file and report continuation behaviour")
    ap.add_argument("--inspect", metavar="RESULTS.jsonl", help="print what the agent wrote and said in a results file")
    ap.add_argument("--family", default="", help="with --inspect: only this task family (e.g. todo)")
    ap.add_argument("--outcome", default="", help="with --inspect: only this outcome (e.g. wrong)")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    if a.audit:
        return audit(a.audit)
    if a.inspect:
        return inspect(a.inspect, a.family, a.outcome)
    set_overhead(a.overhead)
    playbook = Path(a.playbook).read_text() if a.playbook else ""
    out = Path(a.out or f"results_{a.model.replace(':', '-').replace('/', '-')}_oh{a.overhead}_ctx{a.num_ctx}_{int(time.time())}.jsonl")
    rows, mode = [], a.tool_mode
    Ollama(a.model, a.host, 0, 0, a.num_ctx, mode).preflight()
    with out.open("a") as fh:
        for seed in [int(s) for s in a.seeds.split(",")]:
            model = Ollama(a.model, a.host, a.temperature, seed, a.num_ctx, mode, a.max_tokens, a.timeout)
            tasks = build_tasks(seed)
            for task in tasks[: a.limit or len(tasks)]:
                for cond in a.conds.split(","):
                    row = dict(episode(model, task, cond, a.max_turns, playbook, a.loops), model=a.model, seed=seed)
                    mode = row["tool_mode"] = model.tool_mode
                    row.update(overhead=a.overhead, num_ctx=a.num_ctx)
                    rows.append(row)
                    fh.write(json.dumps(row) + "\n")
                    fh.flush()
                    print(f"[{len(rows):>3}] {row['task']:28} {cond:8} {row['outcome']:16} turns={row['turns']:<2} {row['seconds']}s "
                          f"prompt={row['prompt_tokens']} cap_hits={row['cap_hits']} {row['detail'][:120]}")
                    if sum(r["outcome"] == "error" for r in rows) >= 6:
                        summarize(rows)
                        raise SystemExit(f"Stopping: six episodes hit a model-server error (shown above). Partial results: {out.resolve()}")
    summarize(rows)
    summarize_continuation(rows)
    print(f"\nmodel={a.model} tool_mode={mode} temperature={a.temperature} overhead={a.overhead} num_ctx={a.num_ctx} "
          f"largest prompt={max(r['prompt_tokens'] for r in rows)} tokens | text-parsed calls={sum(r['text_calls'] for r in rows)} "
          f"tool errors={sum(r['tool_errors'] for r in rows)} distractor calls={sum(r['distractor_calls'] for r in rows)}")
    print(f"\nFull transcripts: {out.resolve()}")


if __name__ == "__main__":
    main()
