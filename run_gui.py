from __future__ import annotations

import sys
import traceback
from pathlib import Path


# Always prefer the source tree shipped beside this launcher.  Without this
# bootstrap, double-clicking run_gui.py could import an older globally installed
# copy of snes_cheat_patcher and display an outdated interface.
_PROJECT_ROOT = Path(__file__).resolve().parent
_SRC_DIR = _PROJECT_ROOT / "src"
if _SRC_DIR.is_dir():
    src_text = str(_SRC_DIR)
    if src_text in sys.path:
        sys.path.remove(src_text)
    sys.path.insert(0, src_text)


def _write_crash_log(text: str) -> Path:
    target = Path.cwd() / "SNES-Cheat-Patcher-crash.log"
    try:
        target.write_text(text, encoding="utf-8")
    except OSError:
        target = Path.home() / "SNES-Cheat-Patcher-crash.log"
        target.write_text(text, encoding="utf-8")
    return target


try:
    from snes_cheat_patcher.gui import main
    main()
except Exception:
    detail = traceback.format_exc()
    log_path = _write_crash_log(detail)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("SNES Cheat Patcher Studio could not start",
                             f"An error occurred while starting.\n\nLog: {log_path}\n\n{detail[-2000:]}")
        root.destroy()
    except Exception:
        print(detail, file=sys.stderr)
        print(f"Log saved to: {log_path}", file=sys.stderr)
        if sys.stdin and sys.stdin.isatty():
            input("Press Enter to close...")
    raise SystemExit(1)
