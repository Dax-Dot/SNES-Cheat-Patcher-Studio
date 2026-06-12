from __future__ import annotations

import json
import logging
import platform
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .catalog import CatalogCheat, CatalogGame, identify_rom, validate_game_cheats
from .core import (
    Candidate,
    PatchError,
    PatchPlan,
    RomInfo,
    apply_plans,
    classify_region,
    decode_snes_game_genie,
    inspect_rom,
    make_patch_plan,
    patch_candidates,
)

APP_VERSION = __version__
ASSET_DIR = Path(__file__).resolve().parent / "assets"
APP_ICON_PNG = ASSET_DIR / "app_icon.png"
HEADER_LOGO_PNG = ASSET_DIR / "header_logo.png"
ABOUT_LOGO_PNG = ASSET_DIR / "about_logo.png"
LOG_MAX_LINES = 2000

# Interface palette (light, flat, consistent theme across Windows/macOS/Linux).
PALETTE = {
    "bg": "#eef1f6",
    "surface": "#ffffff",
    "border": "#d3d9e3",
    "text": "#1f2533",
    "muted": "#5d6678",
    "accent": "#3b6fe0",
    "accent_hover": "#2f59bd",
    "accent_text": "#ffffff",
    "good": "#1a7f37",
    "bad": "#c0392b",
    "tree_alt": "#f3f6fb",
    "heading": "#e6eaf2",
    "select": "#d6e2ff",
    "select_text": "#13265e",
    "trough": "#dde2ec",
}

# Preferred families per platform (all scalable/antialiased; no bitmap fonts).
if sys.platform == "darwin":
    _UI_FAMILIES = ["SF Pro Text", "Helvetica Neue", "Lucida Grande", "DejaVu Sans"]
    _MONO_FAMILIES = ["SF Mono", "Menlo", "Monaco", "DejaVu Sans Mono"]
elif sys.platform.startswith("win"):
    _UI_FAMILIES = ["Segoe UI", "Tahoma", "DejaVu Sans"]
    _MONO_FAMILIES = ["Cascadia Mono", "Consolas", "Cascadia Code", "DejaVu Sans Mono"]
else:
    _UI_FAMILIES = ["Inter", "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans", "Liberation Sans"]
    _MONO_FAMILIES = ["JetBrains Mono", "Noto Sans Mono", "DejaVu Sans Mono",
                      "Liberation Mono", "Ubuntu Mono"]


def _enable_dpi_awareness() -> None:
    """On Windows, prevents the OS from blurry-rescaling the window on HiDPI displays.

    Must be called BEFORE creating the Tk root.
    """
    if sys.platform.startswith("win"):
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # System DPI aware
            except (AttributeError, OSError):
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _resolve_family(root: tk.Misc, candidates: list[str], fallback: str) -> str:
    try:
        import tkinter.font as tkfont
        available = {name.lower() for name in tkfont.families(root)}
    except Exception:
        available = set()
    for name in candidates:
        if name.lower() in available:
            return name
    return fallback


class TkTextHandler(logging.Handler):
    def __init__(self, widget: tk.Text, *, include_exception: bool = False) -> None:
        super().__init__()
        self.widget = widget
        self.include_exception = include_exception

    def emit(self, record: logging.LogRecord) -> None:
        # Keep the on-screen activity panel concise. Full tracebacks still go
        # to the rotating diagnostic file handler.
        display_record = record
        if not self.include_exception and (record.exc_info or record.stack_info):
            display_record = logging.makeLogRecord(record.__dict__.copy())
            display_record.exc_info = None
            display_record.exc_text = None
            display_record.stack_info = None
        message = self.format(display_record)
        try:
            self.widget.after(0, self._append, message)
        except (tk.TclError, RuntimeError):
            pass

    def _append(self, message: str) -> None:
        try:
            self.widget.configure(state="normal")
            self.widget.insert("end", message + "\n")
            # Truncate so it does not grow indefinitely in memory.
            line_count = int(self.widget.index("end-1c").split(".")[0])
            if line_count > LOG_MAX_LINES:
                self.widget.delete("1.0", f"{line_count - LOG_MAX_LINES}.0")
            self.widget.see("end")
            self.widget.configure(state="disabled")
        except tk.TclError:
            pass


class CandidateDialog(tk.Toplevel):
    """Dialog to choose between candidate physical offsets."""

    def __init__(self, parent: tk.Misc, code: str, candidates: list[Candidate]) -> None:
        super().__init__(parent)
        self.title("Choose offset")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: int | None = None
        self._var = tk.IntVar(value=0)

        ttk.Label(self, text=f"Code {code} can map to several offsets.\nChoose the correct one:",
                  padding=12).pack(anchor="w")
        frame = ttk.Frame(self, padding=(12, 0))
        frame.pack(fill="x")
        for i, cand in enumerate(candidates):
            tag = "primary" if cand.primary else "alternative"
            ttk.Radiobutton(frame, variable=self._var, value=i,
                            text=f"0x{cand.file_offset:06X} — {cand.note} [{tag}]").pack(anchor="w", pady=2)
        btns = ttk.Frame(self, padding=12)
        btns.pack(fill="x")
        ttk.Button(btns, text="OK", command=self._ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self._cancel).pack(side="right", padx=8)
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.update_idletasks()

    def _ok(self) -> None:
        self.result = self._var.get()
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class App(tk.Tk):
    def __init__(self) -> None:
        _enable_dpi_awareness()
        super().__init__()
        self.title(f"SNES Cheat Patcher Studio {APP_VERSION}")

        self.scaling = self._setup_scaling()
        self._setup_fonts()
        self.configure(background=PALETTE["bg"])
        self.report_callback_exception = self._report_callback_exception

        ratio = max(1.0, self.scaling / 1.333)
        width, height = int(1220 * ratio), int(760 * ratio)
        self.geometry(f"{width}x{height}")
        self.minsize(int(940 * ratio), int(640 * ratio))

        self.plans: list[PatchPlan] = []
        self.rom_info: RomInfo | None = None
        self.catalog_game: CatalogGame | None = None
        self.catalog_rows: dict[str, tuple[CatalogCheat, list[PatchPlan], tk.BooleanVar]] = {}
        self.plan_catalog_indices: dict[str, int] = {}
        self.catalog_checkbuttons: list[ttk.Checkbutton] = []
        self.busy_widgets: list[tk.Misc] = []

        self.rom_var = tk.StringVar()
        self.code_var = tk.StringVar()
        self.preview_var = tk.StringVar(value="Type a code to see the live log.")
        self.romdetail_var = tk.StringVar(value="No ROM loaded.")
        self.status_var = tk.StringVar(value="Select a SNES ROM and add one or more codes.")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.catalog_selected_var = tk.StringVar(value="0 cheats selected")
        self.live_event = "Ready."

        self._busy = False
        self._configure_style()
        self._load_assets()
        self._build()
        self._configure_logging()
        self._refresh_live_log()
        self.code_var.trace_add("write", lambda *_: self._update_preview())
        self.logger.debug("Starting SNES Cheat Patcher Studio %s", APP_VERSION)
        self.logger.debug("Python %s | %s | Tk %s | scaling %.2f",
                         platform.python_version(), platform.platform(), tk.TkVersion, self.scaling)

    # ----------------------------------------------------------------- hidpi
    def _setup_scaling(self) -> float:
        try:
            dpi = float(self.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        scaling = min(max(dpi / 72.0, 1.0), 3.0)
        try:
            self.tk.call("tk", "scaling", scaling)
        except tk.TclError:
            scaling = 1.333
        return scaling

    def _setup_fonts(self) -> None:
        import tkinter.font as tkfont
        ui_family = _resolve_family(self, _UI_FAMILIES, "TkDefaultFont")
        mono_family = _resolve_family(self, _MONO_FAMILIES, "TkFixedFont")
        self.font_ui = tkfont.Font(family=ui_family, size=11)
        self.font_small = tkfont.Font(family=ui_family, size=10)
        self.font_bold = tkfont.Font(family=ui_family, size=11, weight="bold")
        self.font_title = tkfont.Font(family=ui_family, size=18, weight="bold")
        self.font_mono = tkfont.Font(family=mono_family, size=10)
        # Reassign Tk named fonts so EVERYTHING uses the crisp family.
        for named in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(named).configure(family=ui_family, size=11)
            except tk.TclError:
                pass
        try:
            tkfont.nametofont("TkFixedFont").configure(family=mono_family, size=10)
        except tk.TclError:
            pass
        self.option_add("*Font", self.font_ui)

    # ----------------------------------------------------------------- style
    def _configure_style(self) -> None:
        p = PALETTE
        style = ttk.Style(self)
        # 'clam' is fully re-styleable and identical across the three systems.
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        row_h = int(self.font_mono.metrics("linespace") * 1.7)

        style.configure(".", background=p["bg"], foreground=p["text"],
                        font=self.font_ui, focuscolor=p["bg"])
        style.configure("TFrame", background=p["bg"])
        style.configure("Card.TFrame", background=p["surface"])
        style.configure("CatalogEven.TFrame", background=p["surface"])
        style.configure("CatalogOdd.TFrame", background=p["tree_alt"])
        style.configure(
            "Catalog.TCheckbutton", background=p["surface"], foreground=p["text"],
            font=self.font_ui, padding=(4, 4),
            indicatorsize=max(18, int(18 * (self.scaling / 1.333))),
            indicatormargin=max(4, int(4 * (self.scaling / 1.333))),
        )
        style.map("Catalog.TCheckbutton", background=[("active", p["select"])])
        style.configure("CatalogCode.TLabel", background=p["surface"], foreground=p["muted"], font=self.font_mono)
        style.configure("TLabel", background=p["bg"], foreground=p["text"])
        style.configure("Title.TLabel", font=self.font_title, foreground=p["text"])
        style.configure("Subtitle.TLabel", font=self.font_small, foreground=p["muted"])
        style.configure("Status.TLabel", font=self.font_small, foreground=p["muted"])
        style.configure("Good.TLabel", font=self.font_small, foreground=p["good"])
        style.configure("Bad.TLabel", font=self.font_small, foreground=p["bad"])
        style.configure("Detail.TLabel", font=self.font_small, foreground=p["text"])

        style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"],
                        relief="solid", borderwidth=1, padding=12)
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["muted"],
                        font=self.font_bold)

        style.configure("TButton", background=p["surface"], foreground=p["text"],
                        bordercolor=p["border"], relief="flat", borderwidth=1,
                        padding=(12, 7), font=self.font_ui)
        style.map("TButton",
                  background=[("active", "#eaeef6"), ("disabled", "#f0f1f4")],
                  bordercolor=[("active", p["accent"])],
                  foreground=[("disabled", "#a3a9b5")])

        style.configure("Accent.TButton", background=p["accent"], foreground=p["accent_text"],
                        bordercolor=p["accent"], font=self.font_bold, padding=(16, 8))
        style.map("Accent.TButton",
                  background=[("active", p["accent_hover"]), ("disabled", "#aebde0")],
                  foreground=[("disabled", "#eef1f6")])

        style.configure("TEntry", fieldbackground=p["surface"], background=p["surface"],
                        bordercolor=p["border"], foreground=p["text"], relief="flat",
                        borderwidth=1, padding=6, insertcolor=p["text"])
        style.map("TEntry", bordercolor=[("focus", p["accent"])])

        style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
        style.configure("TRadiobutton", background=p["bg"], foreground=p["text"])
        style.map("TCheckbutton", background=[("active", p["bg"])])
        style.map("TRadiobutton", background=[("active", p["bg"])])

        style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                        foreground=p["text"], bordercolor=p["border"], borderwidth=1,
                        rowheight=row_h, font=self.font_mono)
        style.map("Treeview",
                  background=[("selected", p["select"])],
                  foreground=[("selected", p["select_text"])])
        style.configure("Treeview.Heading", background=p["heading"], foreground=p["text"],
                        font=self.font_bold, relief="flat", padding=(8, 6))
        style.map("Treeview.Heading", background=[("active", p["heading"])])

        style.configure("Vertical.TScrollbar", background=p["heading"], troughcolor=p["bg"],
                        bordercolor=p["bg"], arrowcolor=p["muted"], relief="flat")
        style.configure("TProgressbar", background=p["accent"], troughcolor=p["trough"],
                        bordercolor=p["trough"], thickness=int(10 * (self.scaling / 1.333)))

    def _load_assets(self) -> None:
        """Load bundled branding assets for source and PyInstaller builds."""
        self.app_icon_image = None
        self.header_logo_image = None
        self.about_logo_image = None
        try:
            if APP_ICON_PNG.exists():
                self.app_icon_image = tk.PhotoImage(file=str(APP_ICON_PNG))
                self.iconphoto(True, self.app_icon_image)
        except (tk.TclError, OSError):
            self.app_icon_image = None
        try:
            if HEADER_LOGO_PNG.exists():
                self.header_logo_image = tk.PhotoImage(file=str(HEADER_LOGO_PNG))
        except (tk.TclError, OSError):
            self.header_logo_image = None
        try:
            if ABOUT_LOGO_PNG.exists():
                self.about_logo_image = tk.PhotoImage(file=str(ABOUT_LOGO_PNG))
        except (tk.TclError, OSError):
            self.about_logo_image = None

    def _show_about(self) -> None:
        """Show project information, credits and legal-use notes."""
        dialog = tk.Toplevel(self)
        dialog.title("About SNES Cheat Patcher Studio")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.configure(background=PALETTE["bg"])
        try:
            if self.app_icon_image is not None:
                dialog.iconphoto(True, self.app_icon_image)
        except tk.TclError:
            pass

        container = ttk.Frame(dialog, padding=18)
        container.pack(fill="both", expand=True)
        top = ttk.Frame(container)
        top.pack(fill="x")
        if self.about_logo_image is not None:
            ttk.Label(top, image=self.about_logo_image).pack(side="left", padx=(0, 14))
        title_area = ttk.Frame(top)
        title_area.pack(side="left", fill="both", expand=True)
        ttk.Label(title_area, text="SNES Cheat Patcher Studio", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_area, text=f"Version {APP_VERSION}", style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))

        body = (
            "A desktop tool for applying supported SNES Game Genie cheats "
            "directly to legally obtained ROM backups.\n\n"
            "Cheat data: GameHacking.org\n"
            "ROM metadata: No-Intro.org\n\n"
            "Based on Mte90/Game-Genie-Good-Guy.\n"
            "Maintainer / Creator: Dax-Dot\n"
            "© 2026 Dax-Dot\n\n"
            "License: GPL-3.0-or-later. See LICENSE.\n\n"
            "This application does not include ROMs, BIOS files or copyrighted game data.\n"
            "Use it only with backups you are legally entitled to modify."
        )
        ttk.Label(container, text=body, justify="left", wraplength=540).pack(anchor="w", pady=(16, 12))
        ttk.Button(container, text="Close", command=dialog.destroy).pack(anchor="e")

        dialog.update_idletasks()
        x = self.winfo_rootx() + max(20, (self.winfo_width() - dialog.winfo_reqwidth()) // 2)
        y = self.winfo_rooty() + max(20, (self.winfo_height() - dialog.winfo_reqheight()) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        dialog.focus_set()

    # ----------------------------------------------------------------- build
    def _build(self) -> None:
        root = ttk.Frame(self, padding=(14, 10, 14, 8))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1, minsize=int(290 * (self.scaling / 1.333)))
        root.rowconfigure(9, minsize=max(24, int(24 * (self.scaling / 1.333))))

        # --- Header
        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        header.columnconfigure(1, weight=1)
        if self.header_logo_image is not None:
            ttk.Label(header, image=self.header_logo_image).grid(
                row=0, column=0, rowspan=2, sticky="w", padx=(0, 10)
            )
        ttk.Label(header, text="SNES Cheat Patcher Studio", style="Title.TLabel").grid(
            row=0, column=1, sticky="sw"
        )
        ttk.Label(header, text=f"Game Genie cheats → patched SNES ROM · version {APP_VERSION}",
                  style="Subtitle.TLabel").grid(row=1, column=1, sticky="nw", pady=(2, 0))
        ttk.Button(header, text="About", command=self._show_about).grid(
            row=0, column=2, rowspan=2, sticky="e", padx=(12, 0)
        )

        # --- ROM row
        romrow = ttk.Frame(root)
        romrow.grid(row=1, column=0, sticky="ew")
        romrow.columnconfigure(1, weight=1)
        ttk.Label(romrow, text="SNES ROM:").grid(row=0, column=0, padx=(0, 10))
        ttk.Entry(romrow, textvariable=self.rom_var, state="readonly").grid(row=0, column=1, sticky="ew")
        self.browse_btn = ttk.Button(romrow, text="Browse…", command=self._browse_rom)
        self.browse_btn.grid(row=0, column=2, padx=(10, 0))
        self.busy_widgets.append(self.browse_btn)
        ttk.Label(root, textvariable=self.romdetail_var, style="Detail.TLabel").grid(
            row=2, column=0, sticky="ew", pady=(7, 0)
        )

        # --- Main horizontal workspace
        # A real PanedWindow lets the user resize both areas and avoids the
        # vertical competition that previously clipped the bottom controls.
        self.main_paned = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        self.main_paned.grid(row=3, column=0, sticky="nsew", pady=(8, 6))
        left_panel = ttk.Frame(self.main_paned)
        right_panel = ttk.Frame(self.main_paned)
        self.main_paned.add(left_panel, weight=3)
        self.main_paned.add(right_panel, weight=2)
        left_panel.columnconfigure(0, weight=1)
        left_panel.rowconfigure(0, weight=1)
        right_panel.columnconfigure(0, weight=1)
        right_panel.rowconfigure(0, weight=1)

        # --- Integrated catalog (left)
        catalog_frame = ttk.LabelFrame(left_panel, text="Patchable cheats for this ROM revision")
        catalog_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        catalog_frame.columnconfigure(0, weight=1)
        catalog_frame.rowconfigure(1, weight=1)
        self.catalog_status_var = tk.StringVar(value="Load a recognized ROM to show its cheats.")
        ttk.Label(catalog_frame, textvariable=self.catalog_status_var, style="Status.TLabel",
                  wraplength=650).grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))

        list_shell = ttk.Frame(catalog_frame, style="Card.TFrame")
        list_shell.grid(row=1, column=0, columnspan=2, sticky="nsew")
        list_shell.columnconfigure(0, weight=1)
        list_shell.rowconfigure(0, weight=1)
        self.catalog_canvas = tk.Canvas(
            list_shell, highlightthickness=1, highlightbackground=PALETTE["border"],
            background=PALETTE["surface"], borderwidth=0,
        )
        self.catalog_scroll = ttk.Scrollbar(list_shell, orient="vertical", command=self.catalog_canvas.yview)
        self.catalog_inner = ttk.Frame(self.catalog_canvas, style="Card.TFrame")
        self.catalog_canvas_window = self.catalog_canvas.create_window((0, 0), window=self.catalog_inner, anchor="nw")
        self.catalog_inner.bind("<Configure>", self._catalog_inner_configured)
        self.catalog_canvas.bind("<Configure>", self._catalog_canvas_configured)
        self.catalog_canvas.configure(yscrollcommand=self.catalog_scroll.set)
        self.catalog_canvas.grid(row=0, column=0, sticky="nsew")
        self.catalog_scroll.grid(row=0, column=1, sticky="ns")
        self._enable_catalog_mousewheel()

        catalog_buttons = ttk.Frame(catalog_frame)
        catalog_buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.deselect_all_btn = ttk.Button(
            catalog_buttons,
            text="Deselect all",
            command=lambda: self._set_all_catalog_checks(False),
        )
        self.deselect_all_btn.pack(side="left")
        self.busy_widgets.append(self.deselect_all_btn)
        ttk.Label(catalog_buttons, textvariable=self.catalog_selected_var, style="Status.TLabel").pack(side="left", padx=(6, 0))
        ttk.Label(
            catalog_buttons, text="Checked cheats will be applied to the saved ROM.",
            style="Status.TLabel",
        ).pack(side="right")

        # --- Live log (right), always visible
        self.live_frame = ttk.LabelFrame(right_panel, text="Live Log")
        self.live_frame.grid(row=0, column=0, sticky="nsew", padx=(5, 0))
        self.live_frame.columnconfigure(0, weight=1)
        self.live_frame.rowconfigure(2, weight=1)

        ttk.Label(self.live_frame, text="Current code analysis", style="Status.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.preview_label = ttk.Label(
            self.live_frame, textvariable=self.preview_var, style="Status.TLabel",
            wraplength=470, justify="left", anchor="nw",
        )
        self.preview_label.grid(row=1, column=0, sticky="ew", pady=(0, 9))

        log_shell = ttk.Frame(self.live_frame)
        log_shell.grid(row=2, column=0, sticky="nsew")
        log_shell.columnconfigure(0, weight=1)
        log_shell.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_shell, height=12, wrap="word", state="disabled", font=self.font_mono,
            relief="flat", borderwidth=0, background=PALETTE["surface"],
            foreground=PALETTE["text"], insertbackground=PALETTE["text"],
            highlightthickness=1, highlightbackground=PALETTE["border"],
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_shell, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_buttons = ttk.Frame(self.live_frame)
        log_buttons.grid(row=3, column=0, sticky="ew", pady=(7, 0))
        ttk.Button(log_buttons, text="Save log…", command=self._save_log).pack(side="left")
        ttk.Button(log_buttons, text="Copy log", command=self._copy_log).pack(side="left", padx=6)
        ttk.Button(log_buttons, text="Clear log", command=self._clear_log).pack(side="left")

        # --- Optional manual tools / patch queue
        advanced_bar = ttk.Frame(root)
        advanced_bar.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        self.advanced_toggle_btn = ttk.Button(
            advanced_bar, text="Show manual codes & patch queue", command=self._toggle_advanced,
        )
        self.advanced_toggle_btn.pack(side="left")

        self.input_frame = ttk.LabelFrame(root, text="Game Genie codes")
        self.input_frame.grid(row=5, column=0, sticky="ew", pady=(0, 7))
        self.input_frame.columnconfigure(1, weight=1)
        ttk.Label(self.input_frame, text="Code(s):").grid(row=0, column=0, sticky="w")
        self.code_entry = ttk.Entry(self.input_frame, textvariable=self.code_var)
        self.code_entry.grid(row=0, column=1, sticky="ew", padx=10)
        self.code_entry.bind("<Return>", lambda _e: self._add_codes())
        self.add_codes_btn = ttk.Button(
            self.input_frame, text="Analyze and add", command=self._add_codes
        )
        self.add_codes_btn.grid(row=0, column=2)
        self.busy_widgets.extend((self.code_entry, self.add_codes_btn))
        ttk.Label(self.input_frame, text="Accepts several codes separated by space, comma or newline.",
                  style="Status.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(7, 0))

        queue_shell = ttk.Frame(root)
        queue_shell.grid(row=6, column=0, sticky="nsew", pady=(0, 6))
        queue_shell.columnconfigure(0, weight=1)
        queue_shell.rowconfigure(0, weight=1)
        columns = ("code", "cpu", "offset", "old", "new", "mapper", "note")
        self.tree = ttk.Treeview(queue_shell, columns=columns, show="headings", selectmode="extended", height=5)
        headings = {"code": "Code", "cpu": "CPU", "offset": "ROM offset", "old": "Original",
                    "new": "New", "mapper": "Mapper", "note": "Mapping"}
        widths = {"code": 110, "cpu": 100, "offset": 110, "old": 80, "new": 80, "mapper": 160, "note": 320}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")
        self.tree.tag_configure("even", background=PALETTE["surface"])
        self.tree.tag_configure("odd", background=PALETTE["tree_alt"])
        self.tree.tag_configure("noop", foreground=PALETTE["muted"])
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.plan_scroll = ttk.Scrollbar(queue_shell, orient="vertical", command=self.tree.yview)
        self.plan_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=self.plan_scroll.set)
        self.queue_shell = queue_shell

        self.plan_buttons = ttk.Frame(root)
        self.plan_buttons.grid(row=7, column=0, sticky="ew", pady=(0, 7))
        self.remove_selected_btn = ttk.Button(
            self.plan_buttons, text="Remove selected", command=self._remove_selected
        )
        self.remove_selected_btn.pack(side="left")
        self.clear_plans_btn = ttk.Button(
            self.plan_buttons, text="Clear", command=self._clear_plans
        )
        self.clear_plans_btn.pack(side="left", padx=8)
        self.save_list_btn = ttk.Button(
            self.plan_buttons, text="Save list…", command=self._save_list
        )
        self.save_list_btn.pack(side="left")
        self.load_list_btn = ttk.Button(
            self.plan_buttons, text="Load list…", command=self._load_list
        )
        self.load_list_btn.pack(side="left", padx=8)
        self.busy_widgets.extend((
            self.remove_selected_btn,
            self.clear_plans_btn,
            self.save_list_btn,
            self.load_list_btn,
            self.tree,
        ))

        # --- Output: compact and always visible
        out_frame = ttk.LabelFrame(root, text="Output")
        out_frame.grid(row=8, column=0, sticky="ew", pady=(0, 7))
        out_frame.columnconfigure(0, weight=1)
        self.generate_btn = ttk.Button(
            out_frame, text="Apply cheats and save ROM…", command=self._generate, style="Accent.TButton"
        )
        self.generate_btn.grid(row=0, column=0, sticky="e")
        self.progress = ttk.Progressbar(out_frame, variable=self.progress_var, maximum=1.0)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        status_bar = ttk.Frame(root)
        status_bar.grid(row=9, column=0, sticky="ew")
        status_bar.columnconfigure(0, weight=1)
        ttk.Label(
            status_bar, textvariable=self.status_var, wraplength=1160,
            style="Status.TLabel", anchor="w", padding=(2, 3),
        ).grid(row=0, column=0, sticky="ew")

        # Advanced tools start collapsed so the central workspace and output
        # fit at the minimum supported window size.
        self.input_frame.grid_remove()
        self.queue_shell.grid_remove()
        self.plan_buttons.grid_remove()

        # Set a sensible first split after Tk has calculated real dimensions.
        self.after_idle(self._set_initial_pane_position)

    def _set_initial_pane_position(self) -> None:
        try:
            width = self.main_paned.winfo_width()
            if width > 100:
                self.main_paned.sashpos(0, max(520, int(width * 0.60)))
        except (tk.TclError, AttributeError):
            pass

    def _toggle_advanced(self) -> None:
        if self.input_frame.winfo_ismapped():
            self.input_frame.grid_remove()
            self.queue_shell.grid_remove()
            self.plan_buttons.grid_remove()
            self.advanced_toggle_btn.configure(text="Show manual codes & patch queue")
            self.update_idletasks()
        else:
            self.input_frame.grid()
            self.queue_shell.grid()
            self.plan_buttons.grid()
            self.advanced_toggle_btn.configure(text="Hide manual codes & patch queue")
            self.update_idletasks()

    def _toggle_log(self) -> None:
        """Compatibility hook: Live analysis is now permanently visible."""
        try:
            self.log_text.focus_set()
        except tk.TclError:
            pass

    def _configure_logging(self) -> None:
        self.logger = logging.getLogger("snes_cheat_patcher")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()
        try:
            log_dir = Path.home() / "SNES Cheat Patcher" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(log_dir / "snes-cheat-patcher.log",
                                               maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
            self.logger.addHandler(file_handler)
            self.logger.debug("Persistent log: %s", log_dir / "snes-cheat-patcher.log")
        except OSError:
            pass

    def _report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        self.logger.exception("Unhandled error in the interface", exc_info=(exc_type, exc_value, exc_traceback))
        messagebox.showerror("Unexpected error",
                             f"{exc_value}\n\nThe details were recorded in the diagnostic panel.", parent=self)

    # ----------------------------------------------------------------- ROM
    def _browse_rom(self) -> None:
        path = filedialog.askopenfilename(
            title="Select SNES ROM",
            filetypes=[("SNES ROM", "*.sfc *.smc *.fig *.swc"), ("All files", "*.*")],
        )
        if not path:
            return
        self._clear_log()
        self.rom_var.set(path)
        self._clear_plans()
        self.logger.info("Analyzing %s…", Path(path).name)
        try:
            info = inspect_rom(path)
            self.rom_info = info
            self.logger.debug("ROM loaded: %s", info.path)
            self.logger.debug("Title=%r size=%d copier=%d header=0x%X",
                             info.title, info.size, info.copier_header, info.header_offset)
            self.logger.debug("Topology=%s chip=%s map_mode=0x%02X chipset=0x%02X confidence=%d",
                             info.topology.value, info.chip.value or "none", info.map_mode,
                             info.chipset_byte, info.confidence)
            self.logger.debug(
                "File SHA-256=%s file CRC32=%08X | ROM SHA-256=%s ROM CRC32=%08X",
                info.sha256, info.crc32, info.payload_sha256, info.payload_crc32,
            )
            self.romdetail_var.set(
                f"{info.title or '(untitled)'} · {info.mapper_label} · {info.size:,} bytes · "
                f"map mode 0x{info.map_mode:02X} · CRC32 ROM {info.payload_crc32:08X}"
            )
            self.status_var.set("ROM analyzed. Select cheats to apply.")
            self.live_event = "ROM loaded successfully."
            self._refresh_live_log()
            self._load_catalog_for_rom(info)
        except Exception as exc:  # noqa: BLE001
            self.rom_info = None
            self._clear_catalog()
            self.romdetail_var.set("No ROM loaded.")
            self.logger.exception("Could not analyze the ROM")
            messagebox.showerror("Invalid ROM", str(exc), parent=self)
        self._update_preview()

    # ------------------------------------------------------------- catalog
    def _catalog_inner_configured(self, _event=None) -> None:
        self.catalog_canvas.configure(scrollregion=self.catalog_canvas.bbox("all"))

    def _catalog_canvas_configured(self, event) -> None:
        # Force every row to use the visible canvas width, avoiding horizontal
        # clipping and making the complete label clickable.
        self.catalog_canvas.itemconfigure(self.catalog_canvas_window, width=event.width)

    def _reset_catalog_view(self) -> None:
        """Place the embedded cheat list at its true origin and show row one."""
        try:
            self.catalog_canvas.coords(self.catalog_canvas_window, 0, 0)
            self.catalog_inner.update_idletasks()
            bbox = self.catalog_canvas.bbox("all")
            self.catalog_canvas.configure(scrollregion=bbox or (0, 0, 0, 0))
            self.catalog_canvas.yview_moveto(0.0)
            # Some Windows/Tk builds update the viewport one idle cycle late.
            self.after(20, lambda: self.catalog_canvas.yview_moveto(0.0))
        except tk.TclError:
            pass

    def _enable_catalog_mousewheel(self) -> None:
        def scroll(event):
            if getattr(event, "num", None) == 4:
                amount = -2
            elif getattr(event, "num", None) == 5:
                amount = 2
            else:
                delta = getattr(event, "delta", 0)
                if not delta:
                    return "break"
                amount = -(delta // 120) * 2 if abs(delta) >= 120 else (-1 if delta > 0 else 1)
            self.catalog_canvas.yview_scroll(amount, "units")
            return "break"

        self._catalog_wheel_handler = scroll
        for widget in (self.catalog_canvas, self.catalog_inner):
            widget.bind("<MouseWheel>", scroll)
            widget.bind("<Button-4>", scroll)
            widget.bind("<Button-5>", scroll)

    def _bind_catalog_mousewheel(self, widget: tk.Misc) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, self._catalog_wheel_handler)

    def _clear_catalog(self) -> None:
        self.catalog_game = None
        self.catalog_rows.clear()
        self.catalog_checkbuttons.clear()
        self.catalog_selected_var.set("0 cheats selected")
        if hasattr(self, "catalog_inner"):
            for child in self.catalog_inner.winfo_children():
                child.destroy()
            self.catalog_canvas.coords(self.catalog_canvas_window, 0, 0)
            self.catalog_canvas.configure(scrollregion=(0, 0, 0, 0))
            self.catalog_canvas.yview_moveto(0.0)
        if hasattr(self, "catalog_status_var"):
            self.catalog_status_var.set("Load a recognized ROM to show its cheats.")

    def _load_catalog_for_rom(self, info: RomInfo) -> None:
        self._clear_catalog()
        match = identify_rom(info)
        if not match.matched or match.game is None:
            self.catalog_status_var.set(
                f"ROM not identified in the built-in database. {match.reason}"
            )
            ttk.Label(
                self.catalog_inner,
                text="No matching cheat catalog was found for this ROM revision.",
                style="Status.TLabel",
            ).pack(anchor="w", padx=10, pady=10)
            self.logger.info("ROM analyzed, but this revision has no matching built-in cheat catalog.")
            self.live_event = "No compatible built-in cheats were found for this ROM revision."
            self._refresh_live_log()
            return

        self.catalog_game = match.game
        valid, rejected = validate_game_cheats(info, match.game)
        for row_no, (cheat, plans) in enumerate(valid):
            key = f"cheat-{cheat.index}"
            var = tk.BooleanVar(value=False)
            self.catalog_rows[key] = (cheat, plans, var)
            row = ttk.Frame(
                self.catalog_inner,
                style="CatalogEven.TFrame" if row_no % 2 == 0 else "CatalogOdd.TFrame",
                padding=(8, 7),
            )
            row.pack(fill="x", expand=True)
            row.columnconfigure(0, weight=1)
            label = f"#{cheat.index}  {cheat.name}"
            cb = ttk.Checkbutton(
                row, text=label, variable=var,
                command=lambda row_key=key: self._on_catalog_toggle(row_key),
                style="Catalog.TCheckbutton",
            )
            cb.grid(row=0, column=0, sticky="ew")
            self.catalog_checkbuttons.append(cb)
            codes = ttk.Label(row, text=" + ".join(cheat.codes), style="CatalogCode.TLabel")
            codes.grid(row=0, column=1, sticky="e", padx=(12, 2))
            self._bind_catalog_mousewheel(row)
            self._bind_catalog_mousewheel(cb)
            self._bind_catalog_mousewheel(codes)

        if not valid:
            ttk.Label(
                self.catalog_inner, text="This ROM matched, but no cheats passed validation.",
                style="Status.TLabel",
            ).pack(anchor="w", padx=10, pady=10)

        # Reset only after Tk has recalculated both the embedded window and
        # the scrollregion. This avoids the blank gap that could remain above
        # the first row after switching ROMs.
        self.after_idle(self._reset_catalog_view)
        self.catalog_status_var.set(
            f"{match.game.name} [{match.game.region}] · "
            f"{len(valid)} statically patchable cheats"
        )
        self.logger.info(
            "%s ready: %d patchable cheats%s.",
            match.game.name, len(valid),
            f"; {len(rejected)} unavailable" if rejected else "",
        )
        for cheat, reason in rejected:
            self.logger.error(
                "Catalog integrity warning: #%d %s failed runtime validation: %s",
                cheat.index, cheat.name, reason,
            )
        self.live_event = "Cheat catalog ready."
        self._refresh_live_log()

    def _set_all_catalog_checks(self, selected: bool) -> None:
        if selected:
            # Activate cheats through the same path as an individual click so
            # conflicts are never silently accepted.
            for key, (_cheat, _plans, var) in self.catalog_rows.items():
                if not var.get():
                    var.set(True)
                    self._on_catalog_toggle(key)
        else:
            # Programmatic BooleanVar changes do not call the checkbutton
            # command, so remove each active catalog cheat explicitly.
            active_indices = {
                cheat.index
                for cheat, _plans, var in self.catalog_rows.values()
                if var.get()
            }
            for _cheat, _plans, var in self.catalog_rows.values():
                var.set(False)
            if active_indices:
                self._remove_catalog_indices(active_indices, log_action=False)
            self._update_catalog_selected_count()
            self.status_var.set("No catalog cheats selected.")

    def _update_catalog_selected_count(self) -> None:
        count = sum(1 for _cheat, _plans, var in self.catalog_rows.values() if var.get())
        label = "cheat selected" if count == 1 else "cheats selected"
        self.catalog_selected_var.set(f"{count} {label}")
        self._refresh_live_log()

    def _catalog_row_for_index(self, index: int):
        for cheat, plans, var in self.catalog_rows.values():
            if cheat.index == index:
                return cheat, plans, var
        return None

    def _rebuild_plan_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for plan in self.plans:
            noop = " [NO EFFECT: byte already has this value]" if plan.is_noop else ""
            row_tags = ["even" if len(self.tree.get_children()) % 2 == 0 else "odd"]
            if plan.is_noop:
                row_tags.append("noop")
            self.tree.insert("", "end", tags=row_tags, values=(
                plan.code.text, f"${plan.code.cpu_address:06X}", f"0x{plan.file_offset:06X}",
                f"${plan.old_value:02X}", f"${plan.new_value:02X}",
                plan.rom.mapper_label, plan.mapping_note + noop))

    def _remove_catalog_indices(self, indices: set[int], *, log_action: bool = True) -> None:
        if not indices:
            return
        removed = [
            plan for plan in self.plans
            if self.plan_catalog_indices.get(plan.code.text) in indices
        ]
        self.plans = [
            plan for plan in self.plans
            if self.plan_catalog_indices.get(plan.code.text) not in indices
        ]
        for plan in removed:
            self.plan_catalog_indices.pop(plan.code.text, None)
        for index in indices:
            row = self._catalog_row_for_index(index)
            if row is not None:
                row[2].set(False)
        if removed:
            self._rebuild_plan_tree()
            if log_action:
                labels = []
                for index in sorted(indices):
                    row = self._catalog_row_for_index(index)
                    labels.append(f"#{index} {row[0].name}" if row else f"#{index}")
                self.logger.info("Disabled catalog cheat(s): %s", ", ".join(labels))

    def _remove_manual_plan_codes(self, codes: set[str]) -> None:
        if not codes:
            return
        self.plans = [plan for plan in self.plans if plan.code.text not in codes]
        for code in codes:
            self.plan_catalog_indices.pop(code, None)
        self._rebuild_plan_tree()

    def _on_catalog_toggle(self, key: str) -> None:
        if self._busy:
            return
        row = self.catalog_rows.get(key)
        if row is None:
            return
        cheat, plans, var = row

        if not var.get():
            self._remove_catalog_indices({cheat.index})
            self._update_catalog_selected_count()
            self.status_var.set(f"Cheat #{cheat.index} disabled.")
            self.live_event = f"Cheat #{cheat.index} disabled."
            self._refresh_live_log()
            return

        # A catalog cheat is atomic: all of its codes are enabled together.
        if len({plan.file_offset for plan in plans}) != len(plans):
            var.set(False)
            self._update_catalog_selected_count()
            messagebox.showerror(
                "Invalid catalog cheat",
                f"Cheat #{cheat.index} contains multiple codes for the same ROM offset and cannot be enabled.",
                parent=self,
            )
            return

        new_offsets = {plan.file_offset for plan in plans}
        new_codes = {plan.code.text for plan in plans}
        conflicting_catalog: set[int] = set()
        conflicting_manual: set[str] = set()
        conflict_names: list[str] = []

        for existing in self.plans:
            if existing.file_offset not in new_offsets and existing.code.text not in new_codes:
                continue
            catalog_index = self.plan_catalog_indices.get(existing.code.text)
            if catalog_index is not None and catalog_index != cheat.index:
                if catalog_index not in conflicting_catalog:
                    conflicting_catalog.add(catalog_index)
                    old_row = self._catalog_row_for_index(catalog_index)
                    conflict_names.append(
                        f"#{catalog_index} {old_row[0].name}" if old_row else f"#{catalog_index}"
                    )
            elif catalog_index is None:
                conflicting_manual.add(existing.code.text)
                conflict_names.append(f"manual code {existing.code.text}")

        previous_plans = list(self.plans)
        previous_indices = dict(self.plan_catalog_indices)
        previous_checks = {
            row_key: row_data[2].get()
            for row_key, row_data in self.catalog_rows.items()
        }
        previous_checks[key] = False

        if conflicting_catalog or conflicting_manual:
            details = "\n".join(f"• {name}" for name in conflict_names)
            replace = messagebox.askyesno(
                "Conflicting cheats",
                f"#{cheat.index} {cheat.name} conflicts with:\n\n{details}\n\n"
                "Replace the conflicting selection with this cheat?",
                parent=self,
            )
            if not replace:
                var.set(False)
                self._update_catalog_selected_count()
                self.status_var.set(f"Cheat #{cheat.index} was not enabled.")
                self.live_event = f"Conflict canceled: cheat #{cheat.index} was not enabled."
                self._refresh_live_log()
                return
            self._remove_catalog_indices(conflicting_catalog, log_action=False)
            self._remove_manual_plan_codes(conflicting_manual)

        try:
            for plan in plans:
                self._append_plan(plan)
                self.plan_catalog_indices[plan.code.text] = cheat.index
        except PatchError as exc:
            self.plans = previous_plans
            self.plan_catalog_indices = previous_indices
            for row_key, checked in previous_checks.items():
                current = self.catalog_rows.get(row_key)
                if current is not None:
                    current[2].set(checked)
            self._rebuild_plan_tree()
            self._update_catalog_selected_count()
            self.logger.error("Catalog cheat rejected #%d %s: %s", cheat.index, cheat.name, exc)
            messagebox.showerror("Could not enable cheat", str(exc), parent=self)
            return

        self._update_catalog_selected_count()
        if conflict_names:
            self.logger.info(
                "Enabled #%d %s, replacing %s", cheat.index, cheat.name, ", ".join(conflict_names)
            )
            self.status_var.set(f"Cheat #{cheat.index} enabled; conflicting cheat replaced.")
            self.live_event = f"Conflict resolved: cheat #{cheat.index} replaced {', '.join(conflict_names)}."
        else:
            self.logger.info("Enabled catalog cheat #%d %s", cheat.index, cheat.name)
            self.status_var.set(f"Cheat #{cheat.index} enabled.")
            self.live_event = f"Cheat #{cheat.index} enabled."
        self._refresh_live_log()

    # ----------------------------------------------------------------- preview
    def _update_preview(self) -> None:
        raw = self.code_var.get().strip()
        if not raw:
            self.preview_var.set("Type a code to see the live log.")
            self.preview_label.configure(style="Status.TLabel")
            return
        # Only preview the first token.
        first = raw.replace(",", " ").split()[0]
        try:
            code = decode_snes_game_genie(first)
        except PatchError as exc:
            self.preview_var.set(f"⚠ {exc}")
            self.preview_label.configure(style="Bad.TLabel")
            return
        region = classify_region(code.bank, code.address)
        base = f"CPU ${code.cpu_address:06X} · value ${code.value:02X} · region {region.value}"
        if self.rom_info is None:
            self.preview_var.set(base + " · (load a ROM to see the offset)")
            self.preview_label.configure(style="Status.TLabel")
            return
        try:
            cands = patch_candidates(self.rom_var.get(), first, rom_info=self.rom_info)
            if not cands:
                self.preview_var.set(base + " · ✗ does not map to ROM in this topology")
                self.preview_label.configure(style="Bad.TLabel")
                return
            c = cands[0]
            with self.rom_info.path.open("rb") as f:
                f.seek(c.file_offset)
                old = f.read(1)[0]
            extra = f" · {len(cands)} candidates" if len(cands) > 1 else ""
            self.preview_var.set(
                base + f" · ✓ offset 0x{c.file_offset:06X} · original ${old:02X} → new ${code.value:02X}{extra}"
            )
            self.preview_label.configure(style="Good.TLabel")
        except PatchError as exc:
            self.preview_var.set(base + f" · ✗ {exc}")
            self.preview_label.configure(style="Bad.TLabel")

    # ----------------------------------------------------------------- add
    def _make_plan_for_token(
        self,
        token: str,
        *,
        expected: int | None = None,
        preferred_offset: int | None = None,
        prompt_candidates: bool = True,
    ) -> PatchPlan | None:
        assert self.rom_info is not None
        cands = patch_candidates(self.rom_info.path, token, rom_info=self.rom_info)
        if not cands:
            raise PatchError("the address does not map to ROM with the detected topology")

        index = 0
        if preferred_offset is not None:
            matches = [i for i, cand in enumerate(cands) if cand.file_offset == preferred_offset]
            if not matches:
                raise PatchError(
                    f"the saved offset 0x{preferred_offset:06X} is no longer a candidate"
                )
            index = matches[0]
        elif len(cands) > 1 and prompt_candidates:
            dlg = CandidateDialog(self, decode_snes_game_genie(token).text, cands)
            self.wait_window(dlg)
            if dlg.result is None:
                self.logger.info("Offset selection cancelled for %s", token)
                return None
            index = dlg.result

        return make_patch_plan(
            self.rom_info.path,
            token,
            expected=expected,
            candidate_index=index,
            rom_info=self.rom_info,
        )

    def _append_plan(self, plan: PatchPlan) -> None:
        if any(p.code.text == plan.code.text for p in self.plans):
            raise PatchError("it is already in the list")
        if any(p.file_offset == plan.file_offset for p in self.plans):
            raise PatchError("another code already modifies that same offset")

        self.plans.append(plan)
        noop = " [NO EFFECT: byte already has this value]" if plan.is_noop else ""
        row_tags = ["even" if len(self.tree.get_children()) % 2 == 0 else "odd"]
        if plan.is_noop:
            row_tags.append("noop")
        self.tree.insert("", "end", tags=row_tags, values=(
            plan.code.text, f"${plan.code.cpu_address:06X}", f"0x{plan.file_offset:06X}",
            f"${plan.old_value:02X}", f"${plan.new_value:02X}",
            plan.rom.mapper_label, plan.mapping_note + noop))
        self.logger.info("OK %s -> offset 0x%06X $%02X→$%02X (%s)",
                         plan.code.text, plan.file_offset, plan.old_value,
                         plan.new_value, plan.mapping_note)
        if plan.is_noop:
            self.logger.warning("%s will have no effect: the byte is already $%02X",
                                plan.code.text, plan.new_value)

    def _add_codes(self) -> None:
        if self.rom_info is None:
            messagebox.showwarning("No ROM", "Load a valid ROM first.", parent=self)
            return
        raw = self.code_var.get().replace(",", " ").split()
        if not raw:
            return
        added, skipped = 0, 0
        for token in raw:
            try:
                plan = self._make_plan_for_token(
                    token, prompt_candidates=len(raw) == 1
                )
                if plan is None:
                    continue
                self._append_plan(plan)
                added += 1
            except (PatchError, OSError) as exc:
                skipped += 1
                self.logger.error("REJECTED %s: %s", token, exc)

        self.status_var.set(f"Added {added}, rejected {skipped}. Check the log for details.")
        if added and not skipped:
            self.code_var.set("")
        elif skipped and not added:
            messagebox.showwarning("Codes rejected",
                                   f"No code was added ({skipped} rejected). Check the log.", parent=self)

    def _remove_selected(self) -> None:
        selected = list(self.tree.selection())
        selected_plans = [self.plans[self.tree.index(item)] for item in selected]
        catalog_indices = {
            self.plan_catalog_indices[plan.code.text]
            for plan in selected_plans
            if plan.code.text in self.plan_catalog_indices
        }
        manual_codes = {
            plan.code.text for plan in selected_plans
            if plan.code.text not in self.plan_catalog_indices
        }
        self._remove_catalog_indices(catalog_indices)
        self._remove_manual_plan_codes(manual_codes)
        self._update_catalog_selected_count()
        self.status_var.set(f"{len(self.plans)} active code(s).")

    def _clear_plans(self) -> None:
        self.plans.clear()
        self.plan_catalog_indices.clear()
        for _cheat, _plans, var in self.catalog_rows.values():
            var.set(False)
        self._update_catalog_selected_count()
        for item in self.tree.get_children():
            self.tree.delete(item)

    # ----------------------------------------------------------------- list IO
    def _save_list(self) -> None:
        if not self.plans:
            messagebox.showwarning("Empty list", "There are no codes to save.", parent=self)
            return
        output = filedialog.asksaveasfilename(title="Save cheat list", defaultextension=".json",
                                              filetypes=[("Cheat list", "*.json")])
        if not output:
            return
        data = {
            "version": APP_VERSION,
            "rom_sha256": self.rom_info.sha256 if self.rom_info else None,
            "rom_crc32": f"{self.rom_info.crc32:08X}" if self.rom_info else None,
            "rom_payload_sha256": self.rom_info.payload_sha256 if self.rom_info else None,
            "rom_payload_crc32": (
                f"{self.rom_info.payload_crc32:08X}" if self.rom_info else None
            ),
            "codes": [
                {
                    "code": p.code.text,
                    "expected": f"{p.old_value:02X}",
                    "file_offset": f"{p.file_offset:06X}",
                }
                for p in self.plans
            ],
        }
        Path(output).write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.logger.info("List saved to %s (%d codes)", output, len(self.plans))

    def _load_list(self) -> None:
        if self.rom_info is None:
            messagebox.showwarning("No ROM", "Load the matching ROM first.", parent=self)
            return
        path = filedialog.askopenfilename(title="Load cheat list",
                                          filetypes=[("Cheat list", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("Could not read the list", str(exc), parent=self)
            return
        expected_payload_hash = data.get("rom_payload_sha256")
        expected_file_hash = data.get("rom_sha256")
        hash_mismatch = (
            expected_payload_hash
            and expected_payload_hash != self.rom_info.payload_sha256
        ) or (
            not expected_payload_hash
            and expected_file_hash
            and expected_file_hash != self.rom_info.sha256
        )
        if hash_mismatch:
            if not messagebox.askyesno("Different ROM",
                                       "The list was saved for a different ROM (different SHA-256). Continue anyway?",
                                       parent=self):
                return
        codes = data.get("codes", [])
        if not isinstance(codes, list):
            messagebox.showerror("Invalid list", "The 'codes' field must be a list.", parent=self)
            return

        added = skipped = 0
        for item in codes:
            try:
                if not isinstance(item, dict) or not isinstance(item.get("code"), str):
                    raise PatchError("invalid code record")
                expected_text = item.get("expected")
                expected = int(expected_text, 16) if expected_text is not None else None
                if expected is not None and not 0 <= expected <= 0xFF:
                    raise ValueError
                offset_text = item.get("file_offset")
                preferred = int(offset_text, 16) if offset_text is not None else None
                plan = self._make_plan_for_token(
                    item["code"],
                    expected=expected,
                    preferred_offset=preferred,
                    prompt_candidates=True,
                )
                if plan is None:
                    continue
                self._append_plan(plan)
                added += 1
            except (PatchError, OSError, TypeError, ValueError) as exc:
                skipped += 1
                self.logger.error("REJECTED while loading %r: %s", item, exc)

        self.code_var.set("")
        self.logger.info("List loaded from %s: %d added, %d rejected.",
                         path, added, skipped)
        self.status_var.set(f"List loaded: {added} added, {skipped} rejected.")
        if skipped:
            messagebox.showwarning(
                "List loaded with warnings",
                f"Added {added} codes and rejected {skipped}. Check the log.",
                parent=self,
            )

    # ----------------------------------------------------------------- generate
    def _generate(self) -> None:
        if self._busy:
            return
        if not self.plans:
            messagebox.showwarning("No cheats", "Select at least one cheat first.", parent=self)
            return
        source = Path(self.rom_var.get())
        cheat_indices = sorted({
            self.plan_catalog_indices[p.code.text]
            for p in self.plans
            if p.code.text in self.plan_catalog_indices
        })
        patch_label = "patched"
        if cheat_indices:
            patch_label += " " + ",".join(str(index) for index in cheat_indices)
        output = filedialog.asksaveasfilename(
            title="Save patched ROM",
            initialfile=f"{source.stem} ({patch_label}){source.suffix or '.sfc'}",
            defaultextension=source.suffix or ".sfc",
            filetypes=[("SNES ROM", "*.sfc *.smc"), ("All files", "*.*")],
        )
        if not output:
            return

        self._set_busy(True)
        self.progress_var.set(0.0)
        plans = list(self.plans)
        # Always repair the internal SNES checksum after patching. Keeping
        # this mandatory prevents accidentally producing an inconsistent ROM.
        fix = True
        self.logger.info("Generating ROM: %s (%d codes, checksum=%s)", output, len(plans), fix)

        # Thread-safe queue: the worker never touches Tk; the main thread polls.
        self._gen_queue: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                out = apply_plans(plans, output, overwrite=True, fix_checksum=fix,
                                  progress=lambda p, m: self._gen_queue.put(("progress", p, m)))
                self._gen_queue.put(("done", str(out), None))
            except Exception as exc:
                self.logger.exception("Unexpected generation failure")
                self._gen_queue.put(("done", output, exc))

        threading.Thread(target=worker, daemon=True).start()
        self.after(50, self._drain_gen_queue)

    def _drain_gen_queue(self) -> None:
        try:
            while True:
                item = self._gen_queue.get_nowait()
                if item[0] == "progress":
                    self.progress_var.set(item[1])
                    self.status_var.set(item[2])
                else:  # done
                    self._generate_done(item[1], item[2])
                    return
        except queue.Empty:
            pass
        if self._busy:
            self.after(50, self._drain_gen_queue)

    def _generate_done(self, output: str, error: Exception | None) -> None:
        self._set_busy(False)
        if error is not None:
            self.progress_var.set(0.0)
            self.logger.error("Generation failed: %s", error)
            self.live_event = f"Could not create patched ROM: {error}"
            self._refresh_live_log()
            messagebox.showerror("Could not generate ROM", str(error), parent=self)
            return
        self.progress_var.set(1.0)
        self.logger.info("Created successfully: %s", output)
        self.live_event = f"Patched ROM created: {Path(output).name}"
        self._refresh_live_log()
        try:
            out_info = inspect_rom(output)
            self.logger.info("Output SHA-256=%s CRC32=%08X", out_info.sha256, out_info.crc32)
        except PatchError:
            pass
        messagebox.showinfo("Done", f"Patched ROM created:\n{output}", parent=self)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for widget in (*self.busy_widgets, *self.catalog_checkbuttons, self.generate_btn):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass

    # ----------------------------------------------------------------- log IO
    def _log_contents(self) -> str:
        return self.log_text.get("1.0", "end-1c")

    def _save_log(self) -> None:
        output = filedialog.asksaveasfilename(title="Save log", defaultextension=".log",
                                              initialfile=f"snes-cheat-patcher-{datetime.now():%Y%m%d-%H%M%S}.log",
                                              filetypes=[("Log file", "*.log *.txt")])
        if output:
            Path(output).write_text(self._log_contents(), encoding="utf-8")
            self.logger.info("Log copy saved to %s", output)

    def _copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self._log_contents())
        self.update_idletasks()
        self.logger.info("Log copied to clipboard")

    def _refresh_live_log(self) -> None:
        if not hasattr(self, "log_text"):
            return
        lines: list[str] = []
        if self.rom_info is None:
            lines.append("No ROM loaded.")
        else:
            game_name = self.catalog_game.name if self.catalog_game else self.rom_info.title
            lines.append(f"Game: {game_name}")
            selected = sorted(
                (cheat.index, cheat.name)
                for cheat, _plans, var in self.catalog_rows.values()
                if var.get()
            )
            if selected:
                lines.append("Active cheats:")
                lines.extend(f"  #{index} {name}" for index, name in selected)
            else:
                lines.append("Active cheats: none")
            manual_count = sum(
                1 for plan in self.plans
                if self.plan_catalog_indices.get(plan.code.text) is None
            )
            if manual_count:
                lines.append(f"Manual codes: {manual_count}")
        if self.live_event:
            lines.extend(("", f"Status: {self.live_event}"))
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", "\n".join(lines))
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.live_event = "Ready."
        self._refresh_live_log()


def main() -> None:
    App().mainloop()
