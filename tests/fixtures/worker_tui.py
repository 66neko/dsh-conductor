"""离线 TUI：真实读取 bracketed paste，可模拟第一次 Enter 仅插入换行。"""

import json
import pathlib
import sys
import time
import tty


glyph, directory, token = sys.argv[1:4]
root = pathlib.Path(directory)
swallow_enter = "--swallow-enter" in sys.argv[4:]
tty.setraw(sys.stdin.fileno())
sys.stdout.write("\x1b[?2004h\x1b[?25h")
sys.stdout.flush()


def screen(text):
    sys.stdout.write("\x1b[2J\x1b[H" + text.replace("\n", "\r\n"))
    sys.stdout.flush()


def receive(number, prefix=""):
    global swallow_enter
    screen(prefix + glyph + " ")
    draft = ""
    while True:
        char = sys.stdin.buffer.read(1)
        if char == b"\x1b":
            if sys.stdin.buffer.read(5) != b"[200~":
                raise RuntimeError("expected bracketed paste")
            data = bytearray()
            while not data.endswith(b"\x1b[201~"):
                data.extend(sys.stdin.buffer.read(1))
            draft += data[:-6].decode().replace("\r\n", "\n").replace("\r", "\n")
            (root / f"draft-{number}.txt").write_text(draft, encoding="utf-8")
            screen(prefix + glyph + " " + draft.replace("\n", "\n  "))
        elif char == b"\r":
            if swallow_enter:
                swallow_enter = False
                draft += "\n"
                screen(prefix + glyph + " " + draft.replace("\n", "\n  "))
            else:
                (root / f"received-{number}.txt").write_text(draft, encoding="utf-8")
                return


screen("1. Continue\nUse arrows to select")
while sys.stdin.buffer.read(1) != b"\r":
    pass
receive(1)
receive(2, "Network error\n")
(root / "result.md").write_text("Complete UTF-8 report", encoding="utf-8")
(root / "receipt.json").write_text(json.dumps({
    "schema_version": 1, "token": token, "status": "ready_for_verification", "summary": "done",
}))
screen("Finished")
time.sleep(30)
