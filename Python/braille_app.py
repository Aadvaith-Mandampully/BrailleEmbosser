#!/usr/bin/env python3
"""
Braille Plotter — Desktop Application
=======================================

Requirements:  pip install pyserial
               braille_plotter.py in the same folder
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import queue
import time
import math
import serial
import serial.tools.list_ports
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
try:
    import braille_plotter as bp
except ImportError:
    messagebox.showerror("Missing file", "braille_plotter.py must be in the same folder.")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────
#  MULTI-PAGE SPLITTER
# ─────────────────────────────────────────────────────────────────

def split_into_pages(braille_text: str,
                     rows_per_page: int = 25) -> list[str]:
    """
    Split a braille string into page-sized chunks.
    Each chunk is a string of braille characters with embedded newlines.
    rows_per_page: standard A4 = 25 rows at 10 mm row spacing with 20 mm margins.
    """
    pages = []
    current_page_rows = []
    current_row_cells = 0
    current_row_chars = []
    in_number = False

    for ch in braille_text:
        if ch == '\n':
            current_page_rows.append(''.join(current_row_chars) + '\n')
            current_row_chars = []
            current_row_cells = 0
            if len(current_page_rows) >= rows_per_page:
                pages.append(''.join(current_page_rows))
                current_page_rows = []
            continue

        current_row_chars.append(ch)
        current_row_cells += 1

        if current_row_cells >= bp.CELLS_PER_LINE:
            current_page_rows.append(''.join(current_row_chars) + '\n')
            current_row_chars = []
            current_row_cells = 0
            if len(current_page_rows) >= rows_per_page:
                pages.append(''.join(current_page_rows))
                current_page_rows = []

    # Flush remainder
    if current_row_chars:
        current_page_rows.append(''.join(current_row_chars))
    if current_page_rows:
        pages.append(''.join(current_page_rows))
    if not pages:
        pages = ['']

    return pages


def dots_for_page(page_braille: str,
                  boustrophedon: bool = True,
                  mirror: bool = True) -> list[tuple[float, float]]:
    """Generate dot jobs for one page of braille text."""
    return bp.braille_to_dot_jobs(page_braille,
                                  boustrophedon=boustrophedon,
                                  mirror=mirror)


# ─────────────────────────────────────────────────────────────────
#  SERIAL WORKER
# ─────────────────────────────────────────────────────────────────

class SerialWorker(threading.Thread):
    def __init__(self, log_queue):
        super().__init__(daemon=True)
        self.log_q  = log_queue
        self.cmd_q  = queue.Queue()
        self.conn   = None
        self.running = True
        self.status  = "disconnected"
        self.pos_x   = 0.0
        self.pos_y   = 0.0
        self._abort  = threading.Event()

    def connect(self, port, baud=115200):
        try:
            self.conn = serial.Serial(port, baudrate=baud, timeout=30)
            self.log(f"Opened {port}")
            deadline = time.time() + 8.0
            while time.time() < deadline:
                raw = self.conn.readline()
                if b"READY" in raw:
                    self.status = "idle"
                    self.log("Arduino ready ✓")
                    return True
            self.log("WARNING: no READY signal — proceeding anyway")
            self.status = "idle"
            return True
        except Exception as e:
            self.log(f"Connection failed: {e}")
            self.status = "disconnected"
            return False

    def disconnect(self):
        if self.conn and self.conn.is_open:
            try: self._send_raw("DONE")
            except Exception: pass
            self.conn.close()
        self.status = "disconnected"
        self.log("Disconnected")

    def _send_raw(self, cmd):
        if not self.conn or not self.conn.is_open:
            raise IOError("Not connected")
        self.conn.write((cmd + "\n").encode("ascii"))
        self.conn.flush()
        while True:
            raw = self.conn.readline()
            if not raw:
                raise IOError(f"Timeout on: {cmd}")
            reply = raw.decode("ascii", errors="ignore").strip()
            if reply == "OK": return "OK"
            if reply.startswith("ERR"): raise RuntimeError(reply)

    def send(self, cmd):
        self.log(f"→ {cmd}")
        result = self._send_raw(cmd)
        self.log(f"← {result}")
        return result

    def home(self):
        self.status = "busy"
        self.log("Homing (X endstop + paper sensor if enabled)…")
        self.send("HOME")
        self.pos_x = 0.0
        self.pos_y = 0.0
        self.status = "idle"
        self.log("Homed ✓  position reset to (0, 0)")

    def move_to(self, x, y):
        self.send(f"MOVE {x:.4f} {y:.4f}")
        self.pos_x = x
        self.pos_y = y

    def jog(self, dx, dy):
        self.move_to(self.pos_x + dx, self.pos_y + dy)

    def fire_dot(self):
        self.send("DOT")

    def feed(self, mm):
        self.send(f"FEED {mm:.4f}")
        self.pos_y += mm

    def set_origin(self):
        bp.PAPER_ORIGIN_X = self.pos_x
        bp.PAPER_ORIGIN_Y = self.pos_y
        self.log(f"Paper origin set: X={bp.PAPER_ORIGIN_X:.3f}  Y={bp.PAPER_ORIGIN_Y:.3f}")

    def paper_ready(self):
        self.send("PAPERREADY")
        self.pos_y = 0.0

    def motors_off(self):
        self.send("DONE")
        self.status = "idle"

    def print_page(self, dot_jobs, progress_cb=None):
        self.status = "printing"
        self._abort.clear()
        total = len(dot_jobs)
        for i, (x_mm, y_mm) in enumerate(dot_jobs, 1):
            if self._abort.is_set():
                self.status = "idle"
                return False
            self.move_to(x_mm, y_mm)
            self.fire_dot()
            if progress_cb:
                progress_cb(i, total)
        self.status = "idle"
        return True

    def abort(self):
        self._abort.set()
        self.log("Abort requested…")

    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log_q.put(f"[{ts}]  {msg}")

    def enqueue(self, fn):
        self.cmd_q.put(fn)

    def run(self):
        while self.running:
            try:
                fn = self.cmd_q.get(timeout=0.1)
                fn()
            except queue.Empty:
                pass
            except Exception as e:
                self.log(f"Error: {e}")
                self.status = "idle"

    def stop(self):
        self.running = False


# ─────────────────────────────────────────────────────────────────
#  PAGE PREVIEW CANVAS  (standalone widget, reused in tab)
# ─────────────────────────────────────────────────────────────────

class PageCanvas(tk.Canvas):
    """
    A canvas that renders one page of dot jobs at accurate scale.

    Coordinate system matches the machine exactly:
      - X axis: left to right, origin at left margin
      - Y axis: top to bottom, origin at top margin
      - All dimensions in mm, rendered at a configurable px/mm scale

    Features:
      - Accurate ruler / grid overlay
      - Margin boundary lines
      - Individual dot positions with correct physical spacing
      - Live print cursor (orange dot)
      - Hover tooltip showing exact mm coordinates
      - Alignment overlay (crosshair at origin)
    """

    PX_PER_MM  = 3.2     # default scale: 3.2 px = 1 mm at 100% zoom
    PAGE_W_MM  = 210.0
    PAGE_H_MM  = 297.0
    MARGIN_PX  = 30      # canvas margin around the page rect for rulers

    def __init__(self, parent, C: dict, **kw):
        super().__init__(parent, bg=C["bg"], bd=0, highlightthickness=0, **kw)
        self.C          = C
        self._dot_jobs  = []
        self._cursor_idx = -1
        self._zoom      = 1.0
        self._show_grid = tk.BooleanVar(value=True)
        self._show_margins = tk.BooleanVar(value=True)
        self._show_coords  = tk.BooleanVar(value=True)
        self._hover_x   = None
        self._hover_y   = None

        self.bind("<Configure>",    self._on_resize)
        self.bind("<Motion>",       self._on_mouse_move)
        self.bind("<Leave>",        self._on_mouse_leave)

    # ── Public API ────────────────────────────────────────────────

    def set_jobs(self, dot_jobs: list):
        self._dot_jobs = dot_jobs
        self._cursor_idx = -1
        self.redraw()

    def set_cursor(self, idx: int):
        """Highlight the dot currently being printed (0-based index)."""
        self._cursor_idx = idx
        self._draw_cursor_only()

    def set_zoom(self, z: float):
        self._zoom = max(0.5, min(4.0, z))
        self.redraw()

    # ── Scale helpers ─────────────────────────────────────────────

    def _scale(self) -> float:
        return self.PX_PER_MM * self._zoom

    def _mm_to_px(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        s = self._scale()
        return (self.MARGIN_PX + x_mm * s,
                self.MARGIN_PX + y_mm * s)

    def _px_to_mm(self, px: float, py: float) -> tuple[float, float]:
        s = self._scale()
        return ((px - self.MARGIN_PX) / s,
                (py - self.MARGIN_PX) / s)

    # ── Draw ─────────────────────────────────────────────────────

    def redraw(self, event=None):
        self.delete("all")
        self._draw_page_rect()
        if self._show_grid.get():
            self._draw_grid()
        if self._show_margins.get():
            self._draw_margins()
        self._draw_rulers()
        self._draw_dots()
        self._draw_cursor_only()
        if self._show_coords.get() and self._hover_x is not None:
            self._draw_coord_tooltip()

    def _on_resize(self, event=None):
        self.redraw()

    def _draw_page_rect(self):
        C = self.C
        s = self._scale()
        x0, y0 = self.MARGIN_PX, self.MARGIN_PX
        x1 = x0 + self.PAGE_W_MM * s
        y1 = y0 + self.PAGE_H_MM * s
        self.create_rectangle(x0, y0, x1, y1,
                               fill=C["surface2"], outline=C["border2"],
                               width=1, tags="page")

    def _draw_grid(self):
        """10 mm grid lines over the page."""
        C  = self.C
        s  = self._scale()
        x0 = self.MARGIN_PX
        y0 = self.MARGIN_PX
        col = C["border"]

        # Vertical lines every 10 mm
        x = 0.0
        while x <= self.PAGE_W_MM + 0.1:
            px = x0 + x * s
            py1 = y0
            py2 = y0 + self.PAGE_H_MM * s
            self.create_line(px, py1, px, py2, fill=col, width=1,
                             dash=(2, 6) if x % 50 != 0 else None,
                             tags="grid")
            x += 10.0

        # Horizontal lines every 10 mm
        y = 0.0
        while y <= self.PAGE_H_MM + 0.1:
            py = y0 + y * s
            px1 = x0
            px2 = x0 + self.PAGE_W_MM * s
            self.create_line(px1, py, px2, py, fill=col, width=1,
                             dash=(2, 6) if y % 50 != 0 else None,
                             tags="grid")
            y += 10.0

    def _draw_margins(self):
        """Show the printable area boundary."""
        C  = self.C
        lm = bp.PAPER_ORIGIN_X + bp.LEFT_MARGIN
        tm = bp.PAPER_ORIGIN_Y + bp.TOP_MARGIN
        rm = lm + bp.PAGE_WIDTH_MM
        bm = tm + (25 * bp.ROW_SPACING)   # 25 rows × 10 mm

        ax0, ay0 = self._mm_to_px(lm, tm)
        ax1, ay1 = self._mm_to_px(rm, bm)

        self.create_rectangle(ax0, ay0, ax1, ay1,
                               outline=self.C["accent2"],
                               fill="", width=1,
                               dash=(6, 3), tags="margin")

        # Label
        self.create_text(ax0 + 3, ay0 + 3,
                         text=f"printable  {bp.PAGE_WIDTH_MM:.0f}×{25*bp.ROW_SPACING:.0f} mm",
                         fill=self.C["accent2"],
                         font=("Courier New", 8), anchor="nw", tags="margin")

    def _draw_rulers(self):
        """Rulers along left and top edges with mm tick marks."""
        C   = self.C
        s   = self._scale()
        m   = self.MARGIN_PX
        col = C["muted"]
        font = ("Courier New", 7)

        # Top ruler — X axis (mm)
        self.create_line(m, 0, m + self.PAGE_W_MM * s, 0,
                         fill=col, width=1, tags="ruler")
        x = 0.0
        while x <= self.PAGE_W_MM + 0.1:
            px = m + x * s
            major = (x % 10 == 0)
            tick_h = 10 if major else 5
            self.create_line(px, m - tick_h, px, m, fill=col, width=1, tags="ruler")
            if major and x > 0:
                self.create_text(px, m - 12, text=f"{x:.0f}",
                                 fill=col, font=font, anchor="s", tags="ruler")
            x += 5.0

        # X axis label
        self.create_text(m + self.PAGE_W_MM * s / 2, 8,
                         text="X (mm)", fill=col, font=font, anchor="n", tags="ruler")

        # Left ruler — Y axis (mm)
        self.create_line(0, m, 0, m + self.PAGE_H_MM * s,
                         fill=col, width=1, tags="ruler")
        y = 0.0
        while y <= self.PAGE_H_MM + 0.1:
            py = m + y * s
            major = (y % 10 == 0)
            tick_h = 10 if major else 5
            self.create_line(m - tick_h, py, m, py, fill=col, width=1, tags="ruler")
            if major and y > 0:
                self.create_text(m - 12, py, text=f"{y:.0f}",
                                 fill=col, font=font, anchor="e", tags="ruler")
            y += 5.0

        # Y axis label (rotated via offset text)
        self.create_text(8, m + self.PAGE_H_MM * s / 2,
                         text="Y (mm)", fill=col, font=font, anchor="center",
                         tags="ruler")

        # Origin label
        self.create_text(m - 2, m - 2, text="0",
                         fill=col, font=font, anchor="se", tags="ruler")

    def _draw_dots(self):
        """Draw all raised dot positions."""
        if not self._dot_jobs:
            W = self.winfo_width() or 400
            H = self.winfo_height() or 300
            self.create_text(W // 2, H // 2,
                             text="No job loaded — translate text in the Print tab",
                             fill=self.C["muted"], font=("Courier New", 10),
                             tags="nodot")
            return

        s       = self._scale()
        dot_r   = max(1.5, bp.DOT_PITCH * s * 0.38)
        col     = self.C["dot"]
        m       = self.MARGIN_PX

        for x_mm, y_mm in self._dot_jobs:
            px = m + x_mm * s
            py = m + y_mm * s
            self.create_oval(px - dot_r, py - dot_r,
                             px + dot_r, py + dot_r,
                             fill=col, outline="", tags="dot")

    def _draw_cursor_only(self):
        """Draw/update the orange cursor dot without full redraw."""
        self.delete("cursor")
        if self._cursor_idx < 0 or self._cursor_idx >= len(self._dot_jobs):
            return
        x_mm, y_mm = self._dot_jobs[self._cursor_idx]
        s = self._scale()
        m = self.MARGIN_PX
        px = m + x_mm * s
        py = m + y_mm * s
        r = max(3, bp.DOT_PITCH * s * 0.7)
        C = self.C
        self.create_oval(px - r, py - r, px + r, py + r,
                         fill=C["warning"], outline=C["danger"], width=1.5,
                         tags="cursor")
        # Crosshair lines through the dot
        self.create_line(px - r*2, py, px + r*2, py,
                         fill=C["warning"], width=1, tags="cursor")
        self.create_line(px, py - r*2, px, py + r*2,
                         fill=C["warning"], width=1, tags="cursor")

    # ── Hover coordinate tooltip ──────────────────────────────────

    def _on_mouse_move(self, event):
        x_mm, y_mm = self._px_to_mm(event.x, event.y)
        # Clamp to page bounds for display
        if 0 <= x_mm <= self.PAGE_W_MM and 0 <= y_mm <= self.PAGE_H_MM:
            self._hover_x = x_mm
            self._hover_y = y_mm
        else:
            self._hover_x = None
            self._hover_y = None
        self.delete("tooltip")
        if self._hover_x is not None and self._show_coords.get():
            self._draw_coord_tooltip(event.x, event.y)

    def _on_mouse_leave(self, event):
        self._hover_x = None
        self._hover_y = None
        self.delete("tooltip")

    def _draw_coord_tooltip(self, px=None, py=None):
        self.delete("tooltip")
        if self._hover_x is None:
            return
        if px is None:
            s = self._scale()
            px = self.MARGIN_PX + self._hover_x * s
            py = self.MARGIN_PX + self._hover_y * s

        # Find nearest dot
        nearest_dist = float('inf')
        nearest_mm   = None
        for x_mm, y_mm in self._dot_jobs:
            d = math.hypot(x_mm - self._hover_x, y_mm - self._hover_y)
            if d < nearest_dist:
                nearest_dist = d
                nearest_mm   = (x_mm, y_mm)

        lines = [f"cursor  X={self._hover_x:6.2f}  Y={self._hover_y:6.2f} mm"]
        if nearest_mm and nearest_dist < 5.0:
            lines.append(f"nearest X={nearest_mm[0]:6.2f}  Y={nearest_mm[1]:6.2f} mm  (Δ{nearest_dist:.2f})")

        C   = self.C
        tx  = px + 12
        ty  = py - 10
        pad = 4
        font = ("Courier New", 8)
        line_h = 14
        box_w  = max(len(l) for l in lines) * 6 + pad * 2
        box_h  = len(lines) * line_h + pad * 2

        # Keep tooltip inside canvas
        W = self.winfo_width() or 600
        H = self.winfo_height() or 400
        if tx + box_w > W:
            tx = px - box_w - 4
        if ty + box_h > H:
            ty = py - box_h - 4

        self.create_rectangle(tx, ty, tx + box_w, ty + box_h,
                               fill=C["surface"], outline=C["border2"],
                               width=1, tags="tooltip")
        for i, line in enumerate(lines):
            self.create_text(tx + pad, ty + pad + i * line_h,
                             text=line, fill=C["text"],
                             font=font, anchor="nw", tags="tooltip")

    def _on_resize(self, event=None):
        self.redraw()


# ─────────────────────────────────────────────────────────────────
#  MAIN APPLICATION
# ─────────────────────────────────────────────────────────────────

class BrailleApp(tk.Tk):

    C = {
        "bg":       "#0f0f0f",
        "surface":  "#1a1a1a",
        "surface2": "#242424",
        "border":   "#2e2e2e",
        "border2":  "#3a3a3a",
        "text":     "#e8e8e8",
        "muted":    "#888888",
        "accent":   "#00d4aa",
        "accent2":  "#007a63",
        "danger":   "#ff4d4d",
        "warning":  "#ffaa00",
        "success":  "#00c878",
        "dot":      "#00d4aa",
    }

    JOG_SIZES = [0.1, 0.5, 1.0, 2.34, 5.0, 10.0]
    ROWS_PER_PAGE = 24     # A4 at 10.5 mm row spacing: floor(257/10.5) = 24

    def __init__(self):
        super().__init__()
        self.title("Braille Plotter")
        self.geometry("1380x860")
        self.minsize(1100, 700)
        self.configure(bg=self.C["bg"])

        # State
        self.log_queue   = queue.Queue()
        self.worker      = SerialWorker(self.log_queue)
        self.worker.start()
        self._pages      = []        # list of braille page strings
        self._page_jobs  = []        # list of dot-job lists, one per page
        self._cur_page   = 0        # which page is shown in preview
        self._print_page = 0        # which page is currently printing
        self._braille_str = ""

        # Tkinter vars
        self.port_var    = tk.StringVar()
        self.grade_var   = tk.IntVar(value=1)
        self.mirror_var  = tk.BooleanVar(value=True)
        self.bous_var    = tk.BooleanVar(value=True)
        self.manual_var  = tk.BooleanVar(value=False)
        self.solenoid_ms = tk.IntVar(value=60)
        self.jog_size    = tk.DoubleVar(value=1.0)
        self.zoom_var    = tk.DoubleVar(value=1.0)

        self._build_styles()
        self._build_ui()
        self._refresh_ports()
        self._poll_log()
        self._poll_status()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Styles ────────────────────────────────────────────────────

    def _build_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        C = self.C
        s.configure(".",
            background=C["surface"], foreground=C["text"],
            fieldbackground=C["surface2"], bordercolor=C["border"],
            darkcolor=C["border"], lightcolor=C["border"],
            troughcolor=C["surface"], selectbackground=C["accent2"],
            selectforeground=C["bg"], font=("Courier New", 10))
        s.configure("TFrame",   background=C["surface"])
        s.configure("TLabel",   background=C["surface"],  foreground=C["text"],  font=("Courier New", 10))
        s.configure("TButton",  background=C["surface2"], foreground=C["text"],  font=("Courier New", 10), padding=(8,4))
        s.map("TButton", background=[("active", C["border2"]), ("disabled", C["surface"])])
        s.configure("Accent.TButton",  background=C["accent"],  foreground=C["bg"],  font=("Courier New", 10, "bold"))
        s.map("Accent.TButton", background=[("active", C["accent2"]), ("disabled", C["surface"])])
        s.configure("Danger.TButton",  background=C["danger"],  foreground=C["bg"],  font=("Courier New", 10, "bold"))
        s.map("Danger.TButton", background=[("active", "#cc3333")])
        s.configure("TNotebook",      background=C["bg"], tabmargins=[2,4,0,0])
        s.configure("TNotebook.Tab",  background=C["surface2"], foreground=C["muted"], padding=[14,6], font=("Courier New", 10))
        s.map("TNotebook.Tab",
              background=[("selected", C["surface"])],
              foreground=[("selected", C["accent"])])
        s.configure("TCombobox",   fieldbackground=C["surface2"], foreground=C["text"])
        s.configure("TCheckbutton", background=C["surface"], foreground=C["text"])
        s.configure("Horizontal.TProgressbar",
                    background=C["accent"], troughcolor=C["surface2"], bordercolor=C["border"])
        s.configure("TEntry",    fieldbackground=C["surface2"], foreground=C["text"],
                    insertcolor=C["accent"], bordercolor=C["border"])
        s.configure("TSpinbox",  fieldbackground=C["surface2"], foreground=C["text"])
        s.configure("TScale",    background=C["surface"], troughcolor=C["surface2"])

    # ── UI Layout ─────────────────────────────────────────────────

    def _build_ui(self):
        C = self.C

        # Top bar
        top = tk.Frame(self, bg=C["bg"], height=52)
        top.pack(fill="x")
        top.pack_propagate(False)

        tk.Label(top, text="⠃⠗⠁⠊⠇⠇⠑", font=("Courier New", 18, "bold"),
                 bg=C["bg"], fg=C["accent"]).pack(side="left", padx=20, pady=10)
        tk.Label(top, text="PLOTTER", font=("Courier New", 11),
                 bg=C["bg"], fg=C["muted"]).pack(side="left", pady=10)

        conn_frame = tk.Frame(top, bg=C["bg"])
        conn_frame.pack(side="right", padx=16)
        tk.Label(conn_frame, text="PORT", font=("Courier New", 9),
                 bg=C["bg"], fg=C["muted"]).pack(side="left", padx=(0,4))
        self.port_combo = ttk.Combobox(conn_frame, textvariable=self.port_var,
                                        width=14, state="readonly")
        self.port_combo.pack(side="left", padx=4)
        ttk.Button(conn_frame, text="↻", width=3,
                   command=self._refresh_ports).pack(side="left", padx=2)
        self.conn_btn = ttk.Button(conn_frame, text="CONNECT",
                                    style="Accent.TButton", command=self._toggle_connect)
        self.conn_btn.pack(side="left", padx=(6,0))

        self.status_lbl = tk.Label(top, text="● OFFLINE", font=("Courier New", 9, "bold"),
                                    bg=C["bg"], fg=C["danger"])
        self.status_lbl.pack(side="right", padx=16)

        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        # Main paned window
        main = tk.PanedWindow(self, orient="horizontal", bg=C["bg"],
                               sashwidth=4, sashpad=0, relief="flat",
                               sashrelief="flat", bd=0)
        main.pack(fill="both", expand=True)

        # Left: tabs
        left = tk.Frame(main, bg=C["surface"])
        main.add(left, minsize=520)

        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True)

        self._build_print_tab(nb)
        self._build_preview_tab(nb)
        self._build_calibrate_tab(nb)
        self._build_settings_tab(nb)

        # Right: console
        right = tk.Frame(main, bg=C["surface"], width=320)
        main.add(right, minsize=260)
        self._build_console(right)

    # ── PRINT TAB ─────────────────────────────────────────────────

    def _build_print_tab(self, nb):
        C = self.C
        tab = tk.Frame(nb, bg=C["surface"])
        nb.add(tab, text="  PRINT  ")

        # Text input
        tk.Label(tab, text="Input text", font=("Courier New", 9),
                 fg=C["muted"], bg=C["surface"]).pack(anchor="w", padx=16, pady=(14,2))
        self.text_input = scrolledtext.ScrolledText(
            tab, height=7, wrap="word",
            bg=C["surface2"], fg=C["text"], insertbackground=C["accent"],
            font=("Courier New", 11), relief="flat", bd=0,
            selectbackground=C["accent2"], padx=8, pady=6)
        self.text_input.pack(fill="x", padx=16, pady=(0,8))

        # Options
        opts = tk.Frame(tab, bg=C["surface"])
        opts.pack(fill="x", padx=16, pady=(0,8))
        tk.Label(opts, text="Grade", fg=C["muted"], bg=C["surface"],
                 font=("Courier New", 9)).pack(side="left")
        ttk.Radiobutton(opts, text="1", variable=self.grade_var, value=1).pack(side="left", padx=(4,0))
        ttk.Radiobutton(opts, text="2", variable=self.grade_var, value=2).pack(side="left", padx=(2,12))
        ttk.Checkbutton(opts, text="Mirror", variable=self.mirror_var).pack(side="left", padx=(0,10))
        ttk.Checkbutton(opts, text="Boustrophedon", variable=self.bous_var).pack(side="left", padx=(0,10))
        ttk.Checkbutton(opts, text="Manual paper", variable=self.manual_var).pack(side="left")

        # File load
        frow = tk.Frame(tab, bg=C["surface"])
        frow.pack(fill="x", padx=16, pady=(0,8))
        ttk.Button(frow, text="Load .txt", command=self._load_file).pack(side="left")
        self.file_lbl = tk.Label(frow, text="", fg=C["muted"], bg=C["surface"],
                                  font=("Courier New", 9))
        self.file_lbl.pack(side="left", padx=10)

        # Translate button + stats
        trans_row = tk.Frame(tab, bg=C["surface"])
        trans_row.pack(fill="x", padx=16, pady=(0,4))
        ttk.Button(trans_row, text="TRANSLATE",
                   command=self._translate).pack(side="left", padx=(0,12))
        self.job_stats_lbl = tk.Label(trans_row, text="—",
                                       fg=C["muted"], bg=C["surface"],
                                       font=("Courier New", 9))
        self.job_stats_lbl.pack(side="left")

        # Braille preview
        tk.Label(tab, text="Braille preview", font=("Courier New", 9),
                 fg=C["muted"], bg=C["surface"]).pack(anchor="w", padx=16, pady=(6,2))
        self.preview_lbl = tk.Label(tab, text="—", fg=C["accent"],
                                     bg=C["surface2"], font=("Courier New", 12),
                                     anchor="w", padx=8, pady=5, relief="flat")
        self.preview_lbl.pack(fill="x", padx=16, pady=(0,10))

        tk.Frame(tab, bg=C["border"], height=1).pack(fill="x", padx=16, pady=8)

        # Multi-page controls
        tk.Label(tab, text="Pages", font=("Courier New", 9),
                 fg=C["muted"], bg=C["surface"]).pack(anchor="w", padx=16, pady=(0,4))

        page_ctrl = tk.Frame(tab, bg=C["surface"])
        page_ctrl.pack(fill="x", padx=16, pady=(0,8))

        ttk.Button(page_ctrl, text="◀", width=3,
                   command=self._prev_page).pack(side="left", padx=(0,4))
        self.page_lbl = tk.Label(page_ctrl, text="Page — / —",
                                  fg=C["text"], bg=C["surface2"],
                                  font=("Courier New", 10), padx=10, pady=4)
        self.page_lbl.pack(side="left", padx=4)
        ttk.Button(page_ctrl, text="▶", width=3,
                   command=self._next_page).pack(side="left", padx=(4,12))
        ttk.Button(page_ctrl, text="Preview page",
                   command=self._show_current_preview).pack(side="left")

        # Progress
        self.progress_var = tk.DoubleVar(value=0)
        self.prog_bar = ttk.Progressbar(tab, variable=self.progress_var,
                                         maximum=100, style="Horizontal.TProgressbar")
        self.prog_bar.pack(fill="x", padx=16, pady=(0,4))
        self.prog_lbl = tk.Label(tab, text="Ready", fg=C["muted"],
                                  bg=C["surface"], font=("Courier New", 9))
        self.prog_lbl.pack(anchor="w", padx=16)

        # Print buttons
        btn_row = tk.Frame(tab, bg=C["surface"])
        btn_row.pack(fill="x", padx=16, pady=10)

        self.print_btn = ttk.Button(btn_row, text="▶  PRINT ALL",
                                     style="Accent.TButton",
                                     command=self._start_print_all, state="disabled")
        self.print_btn.pack(side="left", padx=(0,6))

        self.print_page_btn = ttk.Button(btn_row, text="▶  THIS PAGE",
                                          command=self._start_print_page, state="disabled")
        self.print_page_btn.pack(side="left", padx=(0,6))

        self.abort_btn = ttk.Button(btn_row, text="■  ABORT",
                                     style="Danger.TButton",
                                     command=self._abort_print, state="disabled")
        self.abort_btn.pack(side="left")

    # ── PAGE PREVIEW TAB ──────────────────────────────────────────

    def _build_preview_tab(self, nb):
        C = self.C
        tab = tk.Frame(nb, bg=C["surface"])
        nb.add(tab, text="  PAGE PREVIEW  ")

        # Toolbar
        toolbar = tk.Frame(tab, bg=C["surface2"])
        toolbar.pack(fill="x", padx=0, pady=0)

        # Page nav
        tk.Label(toolbar, text="Page:", fg=C["muted"], bg=C["surface2"],
                 font=("Courier New", 9)).pack(side="left", padx=(10,4), pady=6)
        ttk.Button(toolbar, text="◀", width=2,
                   command=self._prev_page).pack(side="left", padx=2, pady=4)
        self.preview_page_lbl = tk.Label(toolbar, text="—/—",
                                          fg=C["accent"], bg=C["surface2"],
                                          font=("Courier New", 10, "bold"),
                                          width=8, anchor="center")
        self.preview_page_lbl.pack(side="left", padx=4)
        ttk.Button(toolbar, text="▶", width=2,
                   command=self._next_page).pack(side="left", padx=2, pady=4)

        tk.Frame(toolbar, bg=C["border"], width=1).pack(side="left",
                 fill="y", padx=8, pady=4)

        # Zoom
        tk.Label(toolbar, text="Zoom:", fg=C["muted"], bg=C["surface2"],
                 font=("Courier New", 9)).pack(side="left", padx=(0,4))
        for z, lbl in [(0.5,"50%"), (1.0,"100%"), (1.5,"150%"), (2.0,"200%")]:
            ttk.Button(toolbar, text=lbl, width=5,
                       command=lambda z=z: self._set_zoom(z)).pack(side="left", padx=2, pady=4)

        tk.Frame(toolbar, bg=C["border"], width=1).pack(side="left",
                 fill="y", padx=8, pady=4)

        # Overlay toggles
        self._show_grid    = tk.BooleanVar(value=True)
        self._show_margins = tk.BooleanVar(value=True)
        self._show_coords  = tk.BooleanVar(value=True)

        for text, var in [("Grid", self._show_grid),
                           ("Margins", self._show_margins),
                           ("Coords", self._show_coords)]:
            ttk.Checkbutton(toolbar, text=text, variable=var,
                            command=self._refresh_preview).pack(side="left", padx=4)

        tk.Frame(toolbar, bg=C["border"], width=1).pack(side="left",
                 fill="y", padx=8, pady=4)

        # Alignment check button
        ttk.Button(toolbar, text="⊕ Alignment check",
                   command=self._alignment_check).pack(side="left", padx=4)

        tk.Frame(toolbar, bg=C["border"], width=1).pack(side="left",
                 fill="y", padx=8, pady=4)

        # Export dots button
        ttk.Button(toolbar, text="Export coords…",
                   command=self._export_coords).pack(side="left", padx=4)

        # Info bar below toolbar
        self.preview_info = tk.Label(tab, text="",
                                      fg=C["muted"], bg=C["surface"],
                                      font=("Courier New", 9), anchor="w")
        self.preview_info.pack(fill="x", padx=8, pady=(4,0))

        # Scrollable canvas area
        canvas_frame = tk.Frame(tab, bg=C["bg"])
        canvas_frame.pack(fill="both", expand=True, padx=0, pady=4)

        hbar = ttk.Scrollbar(canvas_frame, orient="horizontal")
        vbar = ttk.Scrollbar(canvas_frame, orient="vertical")

        self.preview_canvas = PageCanvas(canvas_frame, C,
                                          xscrollcommand=hbar.set,
                                          yscrollcommand=vbar.set)
        # Sync overlay vars
        self.preview_canvas._show_grid    = self._show_grid
        self.preview_canvas._show_margins = self._show_margins
        self.preview_canvas._show_coords  = self._show_coords

        hbar.configure(command=self.preview_canvas.xview)
        vbar.configure(command=self.preview_canvas.yview)

        vbar.pack(side="right", fill="y")
        hbar.pack(side="bottom", fill="x")
        self.preview_canvas.pack(side="left", fill="both", expand=True)
        self.preview_canvas.configure(scrollregion=(0, 0, 900, 1100))

        # Coordinate readout bar
        coord_bar = tk.Frame(tab, bg=C["surface2"])
        coord_bar.pack(fill="x")
        tk.Label(coord_bar, text="Cursor:", fg=C["muted"], bg=C["surface2"],
                 font=("Courier New", 9)).pack(side="left", padx=(8,4), pady=3)
        self.coord_x_lbl = tk.Label(coord_bar, text="X —",
                                     fg=C["accent"], bg=C["surface2"],
                                     font=("Courier New", 9, "bold"), width=12)
        self.coord_x_lbl.pack(side="left")
        self.coord_y_lbl = tk.Label(coord_bar, text="Y —",
                                     fg=C["accent"], bg=C["surface2"],
                                     font=("Courier New", 9, "bold"), width=12)
        self.coord_y_lbl.pack(side="left")
        tk.Label(coord_bar, text="  Nearest dot:", fg=C["muted"], bg=C["surface2"],
                 font=("Courier New", 9)).pack(side="left", padx=(12,4))
        self.nearest_lbl = tk.Label(coord_bar, text="—",
                                     fg=C["text"], bg=C["surface2"],
                                     font=("Courier New", 9))
        self.nearest_lbl.pack(side="left")

        self.preview_canvas.bind("<Motion>", self._on_preview_mouse_move)
        self.preview_canvas.bind("<Leave>",  self._on_preview_mouse_leave)

    # ── CALIBRATE TAB ─────────────────────────────────────────────

    def _build_calibrate_tab(self, nb):
        C = self.C
        tab = tk.Frame(nb, bg=C["surface"])
        nb.add(tab, text="  CALIBRATE  ")

        # Position readout
        pos_frame = tk.Frame(tab, bg=C["surface2"])
        pos_frame.pack(fill="x", padx=16, pady=(14,8))
        tk.Label(pos_frame, text="POSITION", font=("Courier New", 8),
                 fg=C["muted"], bg=C["surface2"]).pack(anchor="w", padx=10, pady=(6,0))
        pos_inner = tk.Frame(pos_frame, bg=C["surface2"])
        pos_inner.pack(fill="x", padx=10, pady=(4,8))
        self.pos_x_lbl = tk.Label(pos_inner, text="X  0.000 mm",
                                    font=("Courier New", 14, "bold"),
                                    fg=C["accent"], bg=C["surface2"])
        self.pos_x_lbl.pack(side="left", padx=(0,20))
        self.pos_y_lbl = tk.Label(pos_inner, text="Y  0.000 mm",
                                    font=("Courier New", 14, "bold"),
                                    fg=C["accent"], bg=C["surface2"])
        self.pos_y_lbl.pack(side="left")

        # Quick commands
        cmd_row = tk.Frame(tab, bg=C["surface"])
        cmd_row.pack(fill="x", padx=16, pady=(0,8))
        for text, fn in [("HOME", self._home),
                          ("MOTORS OFF", self._motors_off),
                          ("FIRE DOT", self._fire_dot),
                          ("SET ORIGIN", self._set_origin)]:
            ttk.Button(cmd_row, text=text, command=fn).pack(side="left", padx=(0,6))

        tk.Frame(tab, bg=C["border"], height=1).pack(fill="x", padx=16, pady=6)

        # Jog controls
        tk.Label(tab, text="JOG", font=("Courier New", 9),
                 fg=C["muted"], bg=C["surface"]).pack(anchor="w", padx=16, pady=(0,4))

        size_row = tk.Frame(tab, bg=C["surface"])
        size_row.pack(fill="x", padx=16, pady=(0,6))
        tk.Label(size_row, text="Step mm:", fg=C["muted"], bg=C["surface"],
                 font=("Courier New", 9)).pack(side="left", padx=(0,6))
        for sz in self.JOG_SIZES:
            ttk.Radiobutton(size_row, text=f"{sz:g}", variable=self.jog_size,
                            value=sz).pack(side="left", padx=2)

        arrow_grid = tk.Frame(tab, bg=C["surface"])
        arrow_grid.pack(anchor="w", padx=16, pady=(0,8))
        for r, c, text, dx, dy in [
            (0,0,"↖",-1,1),(0,1,"↑",0,1),(0,2,"↗",1,1),
            (1,0,"←",-1,0),(1,2,"→",1,0),
            (2,0,"↙",-1,-1),(2,1,"↓",0,-1),(2,2,"↘",1,-1)]:
            tk.Button(arrow_grid, text=text, width=4, height=2,
                      bg=C["surface2"], fg=C["text"], relief="flat",
                      activebackground=C["border2"], activeforeground=C["accent"],
                      font=("Courier New", 12, "bold"), bd=0,
                      command=lambda dx=dx,dy=dy: self._jog(dx,dy)
                      ).grid(row=r, column=c, padx=2, pady=2)
        tk.Label(arrow_grid, text="●", fg=C["accent"], bg=C["surface"],
                 font=("Courier New", 16)).grid(row=1, column=1, padx=2, pady=2)

        tk.Frame(tab, bg=C["border"], height=1).pack(fill="x", padx=16, pady=6)

        # Paper feed
        feed_row = tk.Frame(tab, bg=C["surface"])
        feed_row.pack(fill="x", padx=16, pady=(0,6))
        tk.Label(feed_row, text="Feed (mm):", fg=C["muted"], bg=C["surface"],
                 font=("Courier New", 9)).pack(side="left", padx=(0,4))
        self.feed_entry = ttk.Entry(feed_row, width=8)
        self.feed_entry.insert(0, "10.0")
        self.feed_entry.pack(side="left", padx=(0,6))
        ttk.Button(feed_row, text="FEED ↓", command=self._feed_paper).pack(side="left", padx=(0,6))
        ttk.Button(feed_row, text="PAPER READY", command=self._paper_ready).pack(side="left")

        tk.Frame(tab, bg=C["border"], height=1).pack(fill="x", padx=16, pady=6)

        # Cal tests
        tk.Label(tab, text="CALIBRATION TESTS", font=("Courier New", 9),
                 fg=C["muted"], bg=C["surface"]).pack(anchor="w", padx=16, pady=(0,4))
        test_row = tk.Frame(tab, bg=C["surface"])
        test_row.pack(fill="x", padx=16, pady=(0,8))
        for text, fn in [("Dot test", self._dot_test),
                          ("Row test", self._row_test),
                          ("X +100 mm", self._x_cal_move),
                          ("Feed 100 mm", self._y_cal_move)]:
            ttk.Button(test_row, text=text, command=fn).pack(side="left", padx=(0,6))

        # Origin display
        orig_frame = tk.Frame(tab, bg=C["surface2"])
        orig_frame.pack(fill="x", padx=16, pady=(4,0))
        tk.Label(orig_frame, text="PAPER ORIGIN", font=("Courier New", 8),
                 fg=C["muted"], bg=C["surface2"]).pack(anchor="w", padx=10, pady=(6,0))
        orig_inner = tk.Frame(orig_frame, bg=C["surface2"])
        orig_inner.pack(fill="x", padx=10, pady=(4,8))
        self.orig_x_lbl = tk.Label(orig_inner,
                                    text=f"X  {bp.PAPER_ORIGIN_X:.3f} mm",
                                    font=("Courier New", 12, "bold"),
                                    fg=C["warning"], bg=C["surface2"])
        self.orig_x_lbl.pack(side="left", padx=(0,20))
        self.orig_y_lbl = tk.Label(orig_inner,
                                    text=f"Y  {bp.PAPER_ORIGIN_Y:.3f} mm",
                                    font=("Courier New", 12, "bold"),
                                    fg=C["warning"], bg=C["surface2"])
        self.orig_y_lbl.pack(side="left")

    # ── SETTINGS TAB ──────────────────────────────────────────────

    def _build_settings_tab(self, nb):
        C = self.C
        tab = tk.Frame(nb, bg=C["surface"])
        nb.add(tab, text="  SETTINGS  ")

        def row(parent, label, attr, default):
            r = tk.Frame(parent, bg=C["surface"])
            r.pack(fill="x", padx=16, pady=3)
            tk.Label(r, text=label, width=24, anchor="w", fg=C["muted"],
                     bg=C["surface"], font=("Courier New", 9)).pack(side="left")
            e = ttk.Entry(r, width=10)
            e.insert(0, default)
            setattr(self, attr, e)
            e.pack(side="left")

        tk.Label(tab, text="MARGINS (mm)", font=("Courier New", 9),
                 fg=C["muted"], bg=C["surface"]).pack(anchor="w", padx=16, pady=(14,4))
        row(tab, "Left margin",  "_lm_entry", str(bp.LEFT_MARGIN))
        row(tab, "Top margin",   "_tm_entry", str(bp.TOP_MARGIN))
        row(tab, "Page width",   "_pw_entry", str(bp.PAGE_WIDTH_MM))

        tk.Label(tab, text="PAGING", font=("Courier New", 9),
                 fg=C["muted"], bg=C["surface"]).pack(anchor="w", padx=16, pady=(12,4))
        row(tab, "Rows per page", "_rpp_entry", str(self.ROWS_PER_PAGE))

        tk.Label(tab, text="SOLENOID", font=("Courier New", 9),
                 fg=C["muted"], bg=C["surface"]).pack(anchor="w", padx=16, pady=(12,4))
        sol_row = tk.Frame(tab, bg=C["surface"])
        sol_row.pack(fill="x", padx=16, pady=3)
        tk.Label(sol_row, text="Dwell (ms)", width=24, anchor="w",
                 fg=C["muted"], bg=C["surface"], font=("Courier New", 9)).pack(side="left")
        ttk.Spinbox(sol_row, from_=10, to=200, increment=5,
                    textvariable=self.solenoid_ms, width=6).pack(side="left")

        tk.Frame(tab, bg=C["border"], height=1).pack(fill="x", padx=16, pady=10)
        ttk.Button(tab, text="APPLY", style="Accent.TButton",
                   command=self._apply_settings).pack(anchor="w", padx=16)
        tk.Label(tab, text="Margin changes apply to next translation.",
                 fg=C["muted"], bg=C["surface"],
                 font=("Courier New", 8)).pack(anchor="w", padx=16, pady=(6,0))

    # ── CONSOLE (right panel) ─────────────────────────────────────

    def _build_console(self, parent):
        C = self.C
        tk.Label(parent, text="CONSOLE", font=("Courier New", 9),
                 fg=C["muted"], bg=C["surface"]).pack(anchor="w", padx=10, pady=(10,3))
        self.log_box = scrolledtext.ScrolledText(
            parent, wrap="word",
            bg=C["bg"], fg=C["text"], insertbackground=C["accent"],
            font=("Courier New", 9), relief="flat", bd=0,
            state="disabled", padx=6, pady=4)
        self.log_box.pack(fill="both", expand=True, padx=6, pady=(0,4))
        self.log_box.tag_configure("err",  foreground=C["danger"])
        self.log_box.tag_configure("ok",   foreground=C["success"])
        self.log_box.tag_configure("sent", foreground=C["muted"])
        self.log_box.tag_configure("info", foreground=C["accent"])
        ttk.Button(parent, text="Clear",
                   command=self._clear_log).pack(anchor="e", padx=6, pady=(0,6))

    # ── Page navigation ───────────────────────────────────────────

    def _prev_page(self):
        if self._cur_page > 0:
            self._cur_page -= 1
            self._refresh_preview()
            self._update_page_labels()

    def _next_page(self):
        if self._cur_page < len(self._pages) - 1:
            self._cur_page += 1
            self._refresh_preview()
            self._update_page_labels()

    def _update_page_labels(self):
        total = max(1, len(self._pages))
        cur   = self._cur_page + 1
        self.page_lbl.configure(text=f"Page {cur} / {total}")
        self.preview_page_lbl.configure(text=f"{cur}/{total}")
        if self._page_jobs:
            jobs = self._page_jobs[self._cur_page]
            self.preview_info.configure(
                text=f"Page {cur}/{total}  ·  {len(jobs)} dots  "
                     f"·  {len(self._pages[self._cur_page])} cells  "
                     f"·  X {bp.LEFT_MARGIN:.1f}–{bp.LEFT_MARGIN+bp.PAGE_WIDTH_MM:.1f} mm  "
                     f"·  Y {bp.TOP_MARGIN:.1f}–{bp.TOP_MARGIN+self.ROWS_PER_PAGE*bp.ROW_SPACING:.1f} mm")

    def _show_current_preview(self):
        """Switch to preview tab and show current page."""
        for i, tab in enumerate(self.nametowidget(".").tk.call(
                "ttk::notebook", str(self._nb), "tabs")):
            pass
        # Navigate directly to preview tab (index 1)
        self._refresh_preview()

    def _refresh_preview(self):
        if not self._page_jobs:
            self.preview_canvas.set_jobs([])
            return
        jobs = self._page_jobs[self._cur_page]
        self.preview_canvas._show_grid    = self._show_grid
        self.preview_canvas._show_margins = self._show_margins
        self.preview_canvas._show_coords  = self._show_coords
        self.preview_canvas.set_jobs(jobs)
        self._update_page_labels()

    def _set_zoom(self, z: float):
        self.preview_canvas.set_zoom(z)

    # ── Preview mouse events ──────────────────────────────────────

    def _on_preview_mouse_move(self, event):
        x_mm, y_mm = self.preview_canvas._px_to_mm(event.x, event.y)
        if 0 <= x_mm <= 210 and 0 <= y_mm <= 297:
            self.coord_x_lbl.configure(text=f"X  {x_mm:6.2f} mm")
            self.coord_y_lbl.configure(text=f"Y  {y_mm:6.2f} mm")
            # Find nearest dot
            if self._page_jobs and self._cur_page < len(self._page_jobs):
                jobs = self._page_jobs[self._cur_page]
                best_d = float('inf')
                best   = None
                for xd, yd in jobs:
                    d = math.hypot(xd - x_mm, yd - y_mm)
                    if d < best_d:
                        best_d = d
                        best   = (xd, yd)
                if best and best_d < 5.0:
                    self.nearest_lbl.configure(
                        text=f"X={best[0]:.3f}  Y={best[1]:.3f}  (Δ{best_d:.3f} mm)")
                else:
                    self.nearest_lbl.configure(text="—")
        else:
            self.coord_x_lbl.configure(text="X —")
            self.coord_y_lbl.configure(text="Y —")
            self.nearest_lbl.configure(text="—")

    def _on_preview_mouse_leave(self, event):
        self.coord_x_lbl.configure(text="X —")
        self.coord_y_lbl.configure(text="Y —")
        self.nearest_lbl.configure(text="—")

    # ── Alignment check ───────────────────────────────────────────

    def _alignment_check(self):
        """
        Print the four corner dots of the printable area only — no text.
        Used to verify paper is loaded correctly and origin is right.
        After printing, user measures corner dot positions with calipers
        and compares to expected coordinates.
        """
        if not self._require_connected():
            return

        lm = bp.PAPER_ORIGIN_X + bp.LEFT_MARGIN
        tm = bp.PAPER_ORIGIN_Y + bp.TOP_MARGIN
        rm = lm + bp.PAGE_WIDTH_MM - bp.DOT_PITCH
        bm = tm + (self.ROWS_PER_PAGE - 1) * bp.ROW_SPACING

        corners = [
            (lm, tm, "top-left"),
            (rm, tm, "top-right"),
            (lm, bm, "bottom-left"),
            (rm, bm, "bottom-right"),
        ]

        msg = "Alignment check will fire 4 dots at the corners of the printable area:\n\n"
        for x, y, name in corners:
            msg += f"  {name:15s}  X={x:.2f}  Y={y:.2f} mm\n"
        msg += "\nMeasure these positions with calipers after printing."

        if not messagebox.askokcancel("Alignment check", msg):
            return

        dot_jobs = [(x, y) for x, y, _ in corners]

        def run():
            try:
                self.worker.home()
                self.worker.print_page(dot_jobs)
                self.worker.motors_off()
                self.worker.log("Alignment check complete ✓")
                self.after(0, lambda: messagebox.showinfo(
                    "Alignment check",
                    "4 corner dots printed.\n"
                    "Measure each dot position and compare to expected values.\n\n"
                    + "\n".join(f"  {n}: X={x:.2f}  Y={y:.2f}"
                                for x, y, n in corners)))
            except Exception as e:
                self.worker.log(f"Alignment check error: {e}")

        threading.Thread(target=run, daemon=True).start()

    # ── Export coordinates ────────────────────────────────────────

    def _export_coords(self):
        """Export dot coordinates for current page to a .csv file."""
        if not self._page_jobs or self._cur_page >= len(self._page_jobs):
            messagebox.showwarning("No data", "Translate text first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
            initialfile=f"braille_page_{self._cur_page+1}_dots.csv")
        if not path:
            return
        jobs = self._page_jobs[self._cur_page]
        with open(path, "w") as f:
            f.write("dot_index,x_mm,y_mm\n")
            for i, (x, y) in enumerate(jobs, 1):
                f.write(f"{i},{x:.4f},{y:.4f}\n")
        self.worker.log(f"Exported {len(jobs)} dot coords → {os.path.basename(path)}")

    # ── Connection ────────────────────────────────────────────────

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _toggle_connect(self):
        if self.worker.status == "disconnected":
            port = self.port_var.get()
            if not port:
                messagebox.showerror("No port", "Select a serial port first.")
                return
            self.conn_btn.configure(state="disabled", text="CONNECTING…")
            def do_connect():
                ok = self.worker.connect(port)
                self.after(0, lambda: self.conn_btn.configure(
                    text="DISCONNECT" if ok else "CONNECT", state="normal"))
            threading.Thread(target=do_connect, daemon=True).start()
        else:
            self.worker.enqueue(self.worker.disconnect)
            self.conn_btn.configure(text="CONNECT")

    # ── Calibration ───────────────────────────────────────────────

    def _home(self):
        if not self._require_connected(): return
        self.worker.enqueue(self.worker.home)

    def _motors_off(self):
        if not self._require_connected(): return
        self.worker.enqueue(self.worker.motors_off)

    def _fire_dot(self):
        if not self._require_connected(): return
        self.worker.enqueue(self.worker.fire_dot)

    def _jog(self, dx_sign, dy_sign):
        if not self._require_connected(): return
        sz = self.jog_size.get()
        self.worker.enqueue(lambda: self.worker.jog(dx_sign*sz, dy_sign*sz))

    def _feed_paper(self):
        if not self._require_connected(): return
        try:
            mm = float(self.feed_entry.get())
        except ValueError:
            messagebox.showerror("Invalid", "Enter a number.")
            return
        self.worker.enqueue(lambda: self.worker.feed(mm))

    def _paper_ready(self):
        if not self._require_connected(): return
        self.worker.enqueue(self.worker.paper_ready)

    def _set_origin(self):
        if not self._require_connected(): return
        self.worker.enqueue(self.worker.set_origin)

    def _dot_test(self):
        if not self._require_connected(): return
        def run():
            ox, oy = self.worker.pos_x, self.worker.pos_y
            for dx, dy in [(0.0,0.0),(bp.DOT_PITCH,0.0)]:
                self.worker.move_to(ox+dx, oy+dy)
                self.worker.fire_dot()
            self.worker.move_to(ox, oy)
            self.worker.log("Dot test done — measure 2.34 mm between dots")
        self.worker.enqueue(run)

    def _row_test(self):
        if not self._require_connected(): return
        def run():
            ox, oy = self.worker.pos_x, self.worker.pos_y
            for cell in range(5):
                cx = ox + cell * bp.CELL_SPACING
                for col in range(2):
                    self.worker.move_to(cx + col * bp.DOT_PITCH, oy)
                    self.worker.fire_dot()
            self.worker.move_to(ox, oy)
            self.worker.log("Row test done — measure 6.2 mm cell-to-cell")
        self.worker.enqueue(run)

    def _x_cal_move(self):
        if not self._require_connected(): return
        self.worker.enqueue(lambda: self.worker.move_to(
            self.worker.pos_x + 100.0, self.worker.pos_y))

    def _y_cal_move(self):
        if not self._require_connected(): return
        self.worker.enqueue(lambda: self.worker.feed(100.0))

    # ── Translation ───────────────────────────────────────────────

    def _load_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("Text files","*.txt"),("All","*.*")])
        if path:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            self.text_input.delete("1.0", "end")
            self.text_input.insert("1.0", text)
            self.file_lbl.configure(text=os.path.basename(path))

    def _translate(self):
        text = self.text_input.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("Empty", "Enter text first.")
            return
        try:
            self._braille_str = bp.text_to_braille(text, grade=self.grade_var.get())
        except Exception as e:
            messagebox.showerror("Translation error", str(e))
            return

        # Split into pages
        self._pages = split_into_pages(self._braille_str,
                                        rows_per_page=self.ROWS_PER_PAGE)
        self._page_jobs = [
            dots_for_page(p, boustrophedon=self.bous_var.get(),
                          mirror=self.mirror_var.get())
            for p in self._pages
        ]
        self._cur_page = 0

        total_dots  = sum(len(j) for j in self._page_jobs)
        total_cells = len(self._braille_str)
        n_pages     = len(self._pages)

        self.preview_lbl.configure(
            text=self._braille_str[:60] + ("…" if len(self._braille_str)>60 else ""))
        self.job_stats_lbl.configure(
            text=f"{n_pages} page{'s' if n_pages!=1 else ''}  ·  "
                 f"{total_cells} cells  ·  {total_dots} dots  ·  "
                 f"grade {self.grade_var.get()}")

        self._update_page_labels()
        self._refresh_preview()

        enabled = "normal" if self._page_jobs else "disabled"
        self.print_btn.configure(state=enabled)
        self.print_page_btn.configure(state=enabled)
        self.worker.log(
            f"Translated: {n_pages} pages, {total_dots} dots total")

    # ── Printing ──────────────────────────────────────────────────

    def _start_print_all(self):
        if not self._page_jobs:
            messagebox.showwarning("No job", "Translate first.")
            return
        if not self._require_connected(): return
        if self.worker.status == "printing": return
        self._run_print(list(range(len(self._page_jobs))))

    def _start_print_page(self):
        if not self._page_jobs:
            messagebox.showwarning("No job", "Translate first.")
            return
        if not self._require_connected(): return
        if self.worker.status == "printing": return
        self._run_print([self._cur_page])

    def _run_print(self, page_indices: list[int]):
        self.print_btn.configure(state="disabled")
        self.print_page_btn.configure(state="disabled")
        self.abort_btn.configure(state="normal")
        self.progress_var.set(0)

        all_jobs   = [(pi, self._page_jobs[pi]) for pi in page_indices]
        total_dots = sum(len(j) for _, j in all_jobs)
        done_dots  = 0
        manual     = self.manual_var.get()

        def progress_cb(done_this_page, total_this_page):
            nonlocal done_dots
            done_dots += 1
            pct = done_dots / total_dots * 100
            self.after(0, lambda: self.progress_var.set(pct))
            self.after(0, lambda: self.prog_lbl.configure(
                text=f"Page {self._print_page+1}/{len(self._page_jobs)}  "
                     f"dot {done_this_page}/{total_this_page}  "
                     f"({pct:.0f}%)"))
            self.after(0, lambda: self.preview_canvas.set_cursor(done_this_page - 1))

        def wait_for_paper(page_num: int) -> bool:
            """
            Show a blocking dialog on the UI thread and wait for the user
            to click OK or Cancel before the worker thread continues.
            Returns True if user confirmed, False if they cancelled.

            Uses threading.Event to bridge the worker thread (which calls
            this function) and the UI thread (which must show the dialog).
            The worker thread blocks on event.wait() — no moves are sent
            until the user has physically loaded the paper and clicked OK.
            """
            confirmed = threading.Event()
            user_ok   = [False]   # mutable container accessible in closure

            def show_dialog():
                result = messagebox.askokcancel(
                    "Load paper",
                    f"Page {page_num} of {len(all_jobs)}\n\n"
                    f"Insert paper to the alignment mark (or load next sheet),\n"
                    f"then click OK to begin printing this page.\n\n"
                    f"Click Cancel to abort the job.")
                user_ok[0] = bool(result)
                confirmed.set()   # unblock the worker thread

            # Schedule dialog on UI thread — returns immediately on worker thread
            self.after(0, show_dialog)
            # Worker thread blocks here until user clicks OK or Cancel
            confirmed.wait()
            return user_ok[0]

        def run():
            try:
                for page_num, (page_idx, jobs) in enumerate(all_jobs, 1):
                    if self.worker._abort.is_set():
                        break

                    self._print_page = page_idx
                    self.after(0, lambda pi=page_idx: self._switch_to_page(pi))
                    self.worker.log(f"Page {page_idx+1}: {len(jobs)} dots")

                    # ── Always home X axis ────────────────────────────────
                    # X endstop gives the only physical reference — home it
                    # regardless of whether paper loading is manual or automatic.
                    # This resets X=0 and confirms the motor is responding.
                    self.worker.log("Homing X axis…")
                    self.worker.send("HOME")
                    self.worker.pos_x = 0.0
                    # Note: HOME also runs homePaper() in firmware.
                    # In sensor mode that feeds paper to the sensor — correct.
                    # In manual mode firmware's homePaper() returns immediately
                    # (USE_PAPER_SENSOR=0), so we handle paper loading below.
                    self.worker.pos_y = 0.0
                    self.worker.log("X homed ✓")

                    # ── Paper loading ─────────────────────────────────────
                    if manual:
                        # Block worker thread — no commands sent until confirmed
                        self.after(0, lambda: self.prog_lbl.configure(
                            text="Waiting for paper…"))
                        ok = wait_for_paper(page_num)
                        if not ok:
                            self.worker.log("User cancelled — aborting job")
                            break
                        # User confirmed paper is loaded — tell firmware
                        self.worker.send("PAPERREADY")
                        self.worker.pos_y = 0.0
                        self.worker.log("Paper confirmed — starting print")
                    # (Sensor mode: HOME already fed paper to sensor + top margin)

                    self.worker.print_page(jobs, progress_cb=progress_cb)

                self.worker.motors_off()
                self.worker.log("All pages complete ✓")
            except Exception as e:
                self.worker.log(f"Print error: {e}")
            finally:
                self.after(0, self._print_finished)

        threading.Thread(target=run, daemon=True).start()

    def _switch_to_page(self, page_idx: int):
        self._cur_page = page_idx
        self._refresh_preview()
        self._update_page_labels()

    def _abort_print(self):
        self.worker.abort()

    def _print_finished(self):
        enabled = "normal" if self._page_jobs else "disabled"
        self.print_btn.configure(state=enabled)
        self.print_page_btn.configure(state=enabled)
        self.abort_btn.configure(state="disabled")
        self.prog_lbl.configure(text="Complete")
        self.preview_canvas.set_cursor(-1)

    # ── Settings ──────────────────────────────────────────────────

    def _apply_settings(self):
        try:
            bp.LEFT_MARGIN    = float(self._lm_entry.get())
            bp.TOP_MARGIN     = float(self._tm_entry.get())
            bp.PAGE_WIDTH_MM  = float(self._pw_entry.get())
            bp.CELLS_PER_LINE = int(bp.PAGE_WIDTH_MM / bp.CELL_SPACING)
            self.ROWS_PER_PAGE = int(self._rpp_entry.get())
            self.worker.log("Settings applied ✓")
        except ValueError as e:
            messagebox.showerror("Invalid value", str(e))

    # ── Status polling ────────────────────────────────────────────

    def _poll_status(self):
        C = self.C
        labels = {
            "disconnected": ("● OFFLINE",  C["danger"]),
            "idle":         ("● IDLE",     C["success"]),
            "busy":         ("● BUSY",     C["warning"]),
            "printing":     ("● PRINTING", C["accent"]),
        }
        text, colour = labels.get(self.worker.status, ("● UNKNOWN", C["muted"]))
        self.status_lbl.configure(text=text, fg=colour)
        self.pos_x_lbl.configure(text=f"X  {self.worker.pos_x:7.3f} mm")
        self.pos_y_lbl.configure(text=f"Y  {self.worker.pos_y:7.3f} mm")
        self.orig_x_lbl.configure(text=f"X  {bp.PAPER_ORIGIN_X:.3f} mm")
        self.orig_y_lbl.configure(text=f"Y  {bp.PAPER_ORIGIN_Y:.3f} mm")
        self.after(200, self._poll_status)

    def _poll_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.after(80, self._poll_log)

    def _append_log(self, msg):
        self.log_box.configure(state="normal")
        tag = "info"
        if "ERR" in msg or "error" in msg.lower() or "failed" in msg.lower():
            tag = "err"
        elif "✓" in msg or "complete" in msg.lower():
            tag = "ok"
        elif "→" in msg:
            tag = "sent"
        self.log_box.insert("end", msg + "\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ── Helpers ───────────────────────────────────────────────────

    def _require_connected(self) -> bool:
        if self.worker.status == "disconnected":
            messagebox.showwarning("Not connected", "Connect to Arduino first.")
            return False
        return True

    def _on_close(self):
        self.worker.enqueue(self.worker.disconnect)
        self.after(400, self.destroy)


# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = BrailleApp()
    app.mainloop()
