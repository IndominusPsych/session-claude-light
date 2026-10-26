#!/usr/bin/env python3
"""
Claude Code traffic light: one row per running Claude Code session.

  claude_light.py launch       -> (SessionStart) register this session, open the light
  claude_light.py set running  -> (hooks) this session's row turns GREEN (working)
  claude_light.py set waiting  -> (hooks) this session's row turns RED (needs you)
  claude_light.py quit         -> (SessionEnd) remove this session's row
  claude_light.py              -> open the light window by hand
  claude_light.py plain        -> same, as a normal titled window (debugging)

Each hook receives JSON on stdin with the session_id and cwd, so every Claude
Code window gets its own row labelled with the folder it runs in. Rows also
disappear if a Claude process dies without a SessionEnd (e.g. terminal closed),
and the window closes once no sessions are left. Right-click a row to dismiss it.
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path.home() / ".claude"
SESS_DIR = BASE / "claude_lights"           # one JSON file per session
LOG_FILE = BASE / "claude_light.log"
LOCK_PORT = 47821                             # held by the open window (single instance)
POLL_MS = 250
EXIT_AFTER_EMPTY_S = 3                        # close window after this long with no sessions
STARTUP_GRACE_S = 5

COLORS = {"running": "#22c55e", "waiting": "#ef4444"}
DIM_RED, DIM_GREEN, PULSE_RED = "#3b1111", "#0f2e1a", "#7f1d1d"
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "env", "python", "python3", "Python"}


# ----------------------------------------------------------------- hook side
def read_hook_input() -> dict:
    """Hooks pipe JSON (session_id, cwd, ...) into stdin."""
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            return json.loads(raw) if raw.strip() else {}
    except Exception:
        pass
    return {}


def find_claude_pid():
    """Walk up from the hook process past shells/python to the Claude Code process."""
    if os.name == "nt":
        return None
    pid = os.getppid()
    for _ in range(8):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=1).stdout.strip()
            ppid_s, comm = out.split(None, 1)
        except Exception:
            return None
        if os.path.basename(comm).lstrip("-") not in SHELLS:
            return pid
        pid = int(ppid_s)
    return None


def session_file(sid: str) -> Path:
    safe = "".join(c for c in sid if c.isalnum() or c in "-_") or "default"
    return SESS_DIR / f"{safe}.json"


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(path: Path, data: dict) -> None:
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)                     # atomic: the GUI never sees half a file


def update_session(state: str, hook: dict) -> None:
    sid = hook.get("session_id") or "default"
    path = session_file(sid)
    rec = load(path) or {}
    rec.setdefault("cwd", hook.get("cwd") or os.getcwd())   # keep the launch folder
    rec.setdefault("started", time.time())
    if not rec.get("pid"):
        rec["pid"] = find_claude_pid()
    rec.update(session_id=sid, state=state, updated=time.time())
    save(path, rec)


def end_session(hook: dict) -> None:
    try:
        session_file(hook.get("session_id") or "default").unlink()
    except FileNotFoundError:
        pass


def already_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=0.3):
            return True
    except OSError:
        return False


def ensure_gui() -> None:
    """Start the window detached if it isn't open. Returns immediately."""
    if already_running():
        return
    BASE.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "a", encoding="utf-8")
    script = str(Path(__file__).resolve())
    if os.name == "nt":
        exe = Path(sys.executable).with_name("pythonw.exe")
        exe = str(exe) if exe.exists() else sys.executable
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([exe, script], creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    else:
        subprocess.Popen([sys.executable, script], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log)


# ------------------------------------------------------------------ GUI side
def pid_alive(pid) -> bool:
    if not pid:
        return True                           # unknown pid: rely on SessionEnd / right-click
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False


def live_sessions() -> list:
    sessions = []
    for f in SESS_DIR.glob("*.json"):
        rec = load(f)
        if not rec:
            continue
        if not pid_alive(rec.get("pid")):
            f.unlink(missing_ok=True)         # Claude died without SessionEnd
            continue
        rec["_file"] = f
        sessions.append(rec)
    sessions.sort(key=lambda r: r.get("started", 0))
    # Disambiguate several sessions in the same folder
    seen = {}
    for r in sessions:
        name = Path(r["cwd"]).name or r["cwd"]
        seen[name] = seen.get(name, 0) + 1
        r["_title"] = name if seen[name] == 1 else f"{name} ({seen[name]})"
        p = r["cwd"].replace(str(Path.home()), "~", 1)
        r["_path"] = p if len(p) <= 34 else "…" + p[-33:]
    return sessions


def run_gui(plain: bool = False) -> None:
    import tkinter as tk

    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
        lock.listen(1)
    except OSError:
        print(f"Light already running (port {LOCK_PORT} in use).", file=sys.stderr)
        return

    W, ROW_H, D, PAD = 290, 38, 18, 8
    BG, FG, FG_DIM, SEP = "#111111", "#f2f2f2", "#8a8a8a", "#262626"

    root = tk.Tk()
    root.title("Claude Light")
    if not plain:
        if sys.platform == "darwin":
            try:
                root.tk.call("::tk::unsupported::MacWindowStyle", "style",
                             root._w, "help", "noActivates")
            except tk.TclError:
                root.overrideredirect(True)
        else:
            root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(bg=BG)
    root.geometry(f"{W}x{ROW_H}+40+40")

    canvas = tk.Canvas(root, width=W, height=ROW_H, bg=BG, highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    state = {"rows": [], "sig": None, "blink": True, "empty_since": None,
             "t0": time.time(), "drag": (0, 0)}

    def redraw(sessions):
        canvas.delete("all")
        h = max(1, len(sessions)) * ROW_H
        root.geometry(f"{W}x{h}")              # size only: keeps the window where it is
        canvas.config(height=h)
        state["rows"] = []
        if not sessions:
            canvas.create_text(W // 2, ROW_H // 2, text="no Claude sessions",
                               fill=FG_DIM, font=("Helvetica", 11))
            return
        for i, r in enumerate(sessions):
            y = i * ROW_H
            cy = y + ROW_H // 2
            if i:
                canvas.create_line(PAD, y, W - PAD, y, fill=SEP)
            red = canvas.create_oval(PAD, cy - D // 2, PAD + D, cy + D // 2,
                                     outline="#333333", width=2)
            gx = PAD + D + 6
            green = canvas.create_oval(gx, cy - D // 2, gx + D, cy + D // 2,
                                       outline="#333333", width=2)
            tx = gx + D + 12
            canvas.create_text(tx, cy - 7, text=r["_title"], anchor="w", fill=FG,
                               font=("Helvetica", 12, "bold"))
            canvas.create_text(tx, cy + 8, text=r["_path"], anchor="w", fill=FG_DIM,
                               font=("Helvetica", 10))
            state["rows"].append((red, green, r))

    def paint():
        state["blink"] = not state["blink"]
        for red, green, r in state["rows"]:
            if r.get("state") == "waiting":
                canvas.itemconfig(red, fill=COLORS["waiting"] if state["blink"] else PULSE_RED)
                canvas.itemconfig(green, fill=DIM_GREEN)
            elif r.get("state") == "running":
                canvas.itemconfig(red, fill=DIM_RED)
                canvas.itemconfig(green, fill=COLORS["running"])
            else:
                canvas.itemconfig(red, fill=DIM_RED)
                canvas.itemconfig(green, fill=DIM_GREEN)

    def tick():
        sessions = live_sessions()
        sig = tuple((r["session_id"], r["_title"]) for r in sessions)
        if sig != state["sig"]:
            state["sig"] = sig
            redraw(sessions)
        else:                                  # same rows: just refresh their states
            by_id = {r["session_id"]: r for r in sessions}
            state["rows"] = [(a, b, by_id.get(r["session_id"], r)) for a, b, r in state["rows"]]
        paint()

        now = time.time()
        if sessions:
            state["empty_since"] = None
        elif now - state["t0"] > STARTUP_GRACE_S:
            state["empty_since"] = state["empty_since"] or now
            if now - state["empty_since"] >= EXIT_AFTER_EMPTY_S:
                root.destroy()                 # last Claude window closed
                return
        root.after(POLL_MS, tick)

    # Drag anywhere to move; right-click (or Ctrl-click) a row to dismiss it
    def start(e): state["drag"] = (e.x, e.y)
    def move(e):
        dx, dy = state["drag"]
        root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")
    def dismiss(e):
        i = e.y // ROW_H
        if 0 <= i < len(state["rows"]):
            state["rows"][i][2]["_file"].unlink(missing_ok=True)
    canvas.bind("<Button-1>", start)
    canvas.bind("<B1-Motion>", move)
    for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
        canvas.bind(seq, dismiss)

    tick()
    root.mainloop()


# ----------------------------------- main----------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "launch":
        update_session("waiting", read_hook_input())
        ensure_gui()
    elif cmd == "set" and len(args) >= 2:
        update_session(args[1], read_hook_input())
        ensure_gui()                           # reopens the light if it was closed
    elif cmd in ("quit", "end"):
        end_session(read_hook_input())
    else:
        run_gui(plain="plain" in args)
