"""USB 继电器控制台 —— LCUS / CUS 型（CH340）图形界面。

一个窗口同时显示所有继电器面板：每个串口一张卡片，连接、通道、开/关/点动、
实时状态、最后发送帧都直接可见，不需要切换“当前设备”。

也支持多窗口：每个窗口有独立的 settings-*.json 配置和设备子集。
同一个串口同一时间只会被一个窗口连接，避免互相抢端口。

运行： python relay_gui.py
依赖： 无（tkinter + ctypes 均为 Python 自带）
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from relay_core import (DEFAULT_BAUD, SerialError, SerialPort, build_frame,
                        list_ports)

APP_TITLE = "USB 继电器控制台"
APP_SUB = "多设备 · 多窗口 · CH340 串口 · 9600 8N1"
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
MUTEX_PREFIX = "Local\\UsbRelayConsole_"
MAX_WINDOWS = 20

# ---------------------------------------------------------------- 配色 / 尺寸 #
BG = "#0e1116"
PANEL = "#161a22"
PANEL2 = "#1b2029"
LINE = "#252b36"
FG = "#e8ecf3"
MUT = "#8a93a6"
GREEN = "#22c55e"
GREEN_DARK = "#0b3d22"
OFF_CLR = "#39424f"
RED = "#ef4444"
RED_BG = "#3a1616"
AMBER = "#f59e0b"
AMBER_BG = "#3a2a10"
BLUE = "#3b82f6"
BLUE_BG = "#14324f"
LIGHT_BLUE = "#9dc4ff"

FONT_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "SimHei")
BAUD_CHOICES = ["9600", "19200", "38400", "57600", "115200"]


def pick_font() -> str:
    try:
        families = set(tkfont.families())
    except Exception:
        return "TkDefaultFont"
    for name in FONT_CANDIDATES:
        if name in families:
            return name
    return "TkDefaultFont"


def mix(c1: str, c2: str, t: float) -> str:
    """颜色线性插值，t=0 取 c1，t=1 取 c2。"""
    t = max(0.0, min(1.0, t))
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def read_json_file(path: str) -> tuple[dict, bool]:
    """读一个 JSON 对象文件，返回 (数据, 是否成功读到有效对象)。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            return loaded, True
    except (OSError, ValueError):
        pass
    return {}, False


# -------------------------------------------------------------------- 设备对象 #
class Device:
    """一台继电器 = 一个串口 + 独立状态/锁，互不干扰。"""

    def __init__(self, port: str, baud=DEFAULT_BAUD, addr: int = 1):
        self.port = str(port).strip().upper()
        self.baud = str(baud or DEFAULT_BAUD)
        try:
            self.addr = max(1, int(addr))
        except (TypeError, ValueError):
            self.addr = 1
        self.ser: SerialPort | None = None
        self.state = "unknown"        # unknown | on | off
        self.tx_count = 0
        self.last_frame = ""
        self.busy = False
        self.opening = False
        self.error = ""
        self.lock = threading.Lock()   # 保护 ser 与写串口

    def to_dict(self) -> dict:
        return {"port": self.port, "baud": self.baud, "addr": self.addr}


# ---------------------------------------------------------------- 单设备面板 #
class DeviceCard:
    """一台继电器的完整操作面板：连接、串口/波特率/通道、开/关/点动、状态。"""

    def __init__(self, master, app: "App", dev: Device):
        self.app = app
        self.dev = dev
        self.raw_dialog: tk.Toplevel | None = None

        ff = app.ff
        self.card = tk.Frame(master, bg=PANEL, highlightthickness=1,
                             highlightbackground=LINE)
        self.card.pack(fill="x", pady=(0, 8))

        self.accent = tk.Frame(self.card, width=4, bg=OFF_CLR)
        self.accent.pack(side="left", fill="y")

        body = tk.Frame(self.card, bg=PANEL)
        body.pack(side="left", fill="both", expand=True, padx=10, pady=8)

        # ---- 第一行：串口设置 + 连接/移除 ----
        top = tk.Frame(body, bg=PANEL)
        top.pack(fill="x")

        self.remove_btn = app._small_button(top, "移除", lambda: app.remove_device(dev),
                                           bg=PANEL2, fg=MUT, size=8, width=4)
        self.remove_btn.pack(side="right")
        self.connect_btn = app._small_button(top, "连接", lambda: app.toggle_connect(dev),
                                            bg=GREEN_DARK, fg="#8ef0b4", size=9, width=7)
        self.connect_btn.pack(side="right", padx=(0, 6))

        tk.Label(top, text="串口", bg=PANEL, fg=MUT,
                 font=(ff, 8)).pack(side="left")
        self.port_var = tk.StringVar(value=dev.port)
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var,
                                    values=app._port_values(dev), width=8,
                                    state="readonly", font=(ff, 9))
        self.port_cb.pack(side="left", padx=(5, 10))
        self.port_cb.bind("<<ComboboxSelected>>", self._on_port)

        tk.Label(top, text="波特率", bg=PANEL, fg=MUT,
                 font=(ff, 8)).pack(side="left")
        self.baud_var = tk.StringVar(value=dev.baud)
        self.baud_cb = ttk.Combobox(top, textvariable=self.baud_var,
                                    values=self._baud_values(), width=7,
                                    state="readonly", font=(ff, 9))
        self.baud_cb.pack(side="left", padx=(5, 10))
        self.baud_cb.bind("<<ComboboxSelected>>", self._on_baud)

        tk.Label(top, text="通道", bg=PANEL, fg=MUT,
                 font=(ff, 8)).pack(side="left")
        self.addr_var = tk.IntVar(value=dev.addr)
        self.addr_sp = ttk.Spinbox(top, from_=1, to=8, textvariable=self.addr_var,
                                   width=3, font=(ff, 9))
        self.addr_sp.pack(side="left", padx=(5, 0))
        self.addr_var.trace_add("write", self._on_addr)

        # ---- 第二行：状态 + 操作按钮 ----
        bottom = tk.Frame(body, bg=PANEL)
        bottom.pack(fill="x", pady=(7, 0))

        self.hex_btn = app._small_button(bottom, "HEX", lambda: app.open_raw_dialog(dev),
                                         bg=PANEL, fg=MUT, size=8, width=4)
        self.hex_btn.pack(side="right")
        self.pulse_btn = app._small_button(bottom, "点 动", lambda: app.on_pulse(dev),
                                           bg=PANEL2, fg=LIGHT_BLUE, size=9, width=5)
        self.pulse_btn.pack(side="right", padx=(0, 6))
        self.off_btn = app._small_button(bottom, "关  OFF", lambda: app.set_state(dev, False),
                                         bg=PANEL2, fg=FG, size=9, width=6)
        self.off_btn.pack(side="right", padx=(0, 6))
        self.on_btn = app._small_button(bottom, "开  ON", lambda: app.set_state(dev, True),
                                        bg=GREEN_DARK, fg="#8ef0b4", size=9,
                                        width=6, bold=True)
        self.on_btn.pack(side="right", padx=(0, 6))

        self.badge = tk.Label(bottom, text="未连接", bg=PANEL2, fg=MUT,
                              font=(ff, 9, "bold"), width=7, pady=2)
        self.badge.pack(side="left")
        self.detail = tk.Label(bottom, text="", bg=PANEL, fg=MUT,
                               font=(ff, 8), anchor="w")
        self.detail.pack(side="left", fill="x", expand=True, padx=(8, 8))

        self.refresh()

    # -- 数据同步 ------------------------------------------------------- #
    def _baud_values(self) -> list[str]:
        values = list(BAUD_CHOICES)
        if self.dev.baud not in values:
            values.insert(0, self.dev.baud)
        return values

    def refresh_ports_list(self, ports: list[str]):
        values = list(ports)
        if self.dev.port and self.dev.port not in values:
            values.insert(0, self.dev.port)
        self.port_cb.configure(values=values)

    def _on_port(self, _evt=None):
        new = self.port_var.get().strip().upper()
        if not new or new == self.dev.port:
            return
        if self.dev.ser is not None or self.dev.opening:
            self.port_var.set(self.dev.port)
            self.app.log("err", "请先断开，再切换端口", self.dev)
            return
        self.dev.port = new
        self.dev.error = ""
        self.app._save_settings()
        self.refresh()

    def _on_baud(self, _evt=None):
        new = self.baud_var.get().strip()
        if not new or new == self.dev.baud:
            return
        if self.dev.ser is not None or self.dev.opening:
            self.baud_var.set(self.dev.baud)
            self.app.log("err", "请先断开，再修改波特率", self.dev)
            return
        self.dev.baud = new
        self.app._save_settings()
        self.refresh()

    def _on_addr(self, *_args):
        try:
            self.dev.addr = int(self.addr_var.get())
        except (tk.TclError, ValueError):
            pass

    # -- 状态刷新 ------------------------------------------------------- #
    def refresh(self):
        dev = self.dev
        if dev.busy:
            badge, badge_bg, badge_fg, accent = "执行中", AMBER_BG, AMBER, AMBER
        elif dev.opening:
            badge, badge_bg, badge_fg, accent = "连接中…", AMBER_BG, AMBER, AMBER
        elif dev.error:
            if dev.ser is None:
                badge = "打开失败"
            else:
                badge = "通信异常"
            badge_bg, badge_fg, accent = RED_BG, "#fca5a5", RED
        elif dev.ser is not None:
            accent = GREEN if dev.state == "on" else BLUE
            if dev.state == "on":
                badge, badge_bg, badge_fg = "已开启", GREEN_DARK, "#8ef0b4"
            elif dev.state == "off":
                badge, badge_bg, badge_fg = "已关闭", PANEL2, LIGHT_BLUE
            else:
                badge, badge_bg, badge_fg = "已连接", BLUE_BG, LIGHT_BLUE
        else:
            badge, badge_bg, badge_fg, accent = "未连接", PANEL2, MUT, OFF_CLR

        self.badge.configure(text=badge, bg=badge_bg, fg=badge_fg)
        self.accent.configure(bg=accent)

        if dev.error:
            detail = ("点“连接”重试（详情见日志）" if dev.ser is None
                      else "最近一次操作失败（详情见日志）")
        elif dev.busy:
            detail = "正在发送指令…"
        elif dev.opening:
            detail = "正在打开串口…"
        elif dev.ser is not None:
            detail = f"累计 {dev.tx_count} 帧"
            if dev.last_frame and dev.state in ("on", "off"):
                detail += f" · {dev.last_frame.replace(' ', '')}"
        else:
            detail = "点“连接”打开串口"
        self.detail.configure(text=detail, fg=RED if dev.error else MUT)

        if dev.opening:
            self.connect_btn.configure(text="连接中…", state="disabled",
                                       bg=PANEL2, fg=AMBER)
        elif dev.ser is not None:
            self.connect_btn.configure(text="断开", bg=PANEL2, fg=MUT,
                                       state="disabled" if dev.busy else "normal")
        else:
            self.connect_btn.configure(text="连接", bg=GREEN_DARK, fg="#8ef0b4",
                                       state="disabled" if dev.busy else "normal")

        busy = dev.busy or dev.opening
        for btn in (self.on_btn, self.off_btn, self.pulse_btn, self.hex_btn):
            btn.configure(state="disabled" if busy else "normal")
        self.remove_btn.configure(
            state="normal" if len(self.app.devices) > 1 and not busy else "disabled")
        editable = dev.ser is None and not dev.opening
        self.port_cb.configure(state="readonly" if editable else "disabled")
        self.baud_cb.configure(state="readonly" if editable else "disabled")
        self.addr_sp.configure(state="disabled" if busy else "normal")


# ------------------------------------------------------------------------ 应用 #
class App:
    def __init__(self, root: tk.Tk, profile: str = "default",
                 settings_path: str = SETTINGS_PATH,
                 window_title: str = APP_TITLE,
                 auto_connect: bool = True):
        self.root = root
        self.ff = pick_font()
        self.profile = profile
        self.settings_path = settings_path
        self.window_title = window_title
        self.auto_connect_enabled = bool(auto_connect)

        self.devices, self.pulse_ms = self._load_settings()
        self.cards: dict[Device, DeviceCard] = {}
        self.events: queue.Queue = queue.Queue()
        self.closing = False
        self._sb_visible = False

        self._setup_window()
        self._setup_style()
        self._build_ui()
        self._bind_keys()

        for dev in self.devices:
            self._add_card(dev)
        self._fit_dev_area()
        self._refresh_header()

        self.root.after(40, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.report_callback_exception = self._on_tk_error

        self.log("info", f"端口列表：{', '.join(list_ports()) or '未发现串口'}")
        if self.profile != "default":
            self.log("info", f"本窗口：{self.window_title} · 配置 "
                             f"{os.path.basename(self.settings_path)}")
        if self.auto_connect_enabled:
            self.root.after(350, self.auto_connect)
        else:
            self.log("info", "多窗口模式：本窗口不自动连接；"
                             "同一串口同时只能被一个窗口占用，请在每个面板点“连接”。")
            self.set_status("多窗口模式 · 在每个面板点“连接”", MUT)

    def _on_tk_error(self, exc, val, tb):
        """把界面回调里的异常也写进日志，避免界面变哑。"""
        import traceback
        self.log("err", f"界面异常：{val}")
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                fh.write("".join(traceback.format_exception(exc, val, tb)))
        except OSError:
            pass

    # ---------------------------------------------------------------- 设置 #
    def _load_settings(self):
        """读当前窗口的配置；新窗口首次启动时从主窗口复制设备列表。"""
        data, ok = read_json_file(self.settings_path)
        if not ok and self.profile != "default":
            base, _ = read_json_file(SETTINGS_PATH)
            data = dict(base)

        devices: list[Device] = []
        seen: set[str] = set()
        raw = data.get("devices")
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                port = str(item.get("port") or "").strip()
                if not port:
                    continue
                dev = Device(port, item.get("baud", DEFAULT_BAUD), item.get("addr", 1))
                if dev.port in seen:
                    continue
                seen.add(dev.port)
                devices.append(dev)

        if not devices:                     # 旧版单设备配置 / 空配置
            port = str(data.get("port") or "COM5").strip() or "COM5"
            devices.append(Device(port, data.get("baud", DEFAULT_BAUD), data.get("addr", 1)))

        try:
            pulse = int(data.get("pulse_ms", 500))
        except (TypeError, ValueError):
            pulse = 500
        pulse = max(50, min(10000, pulse))
        return devices, pulse

    def _save_settings(self):
        data = {
            "devices": [d.to_dict() for d in self.devices],
            "pulse_ms": self._pulse_value(),
        }
        tmp = self.settings_path + ".tmp"
        try:
            # 先写临时文件再原子替换：别的窗口读取时不会读到半截 JSON
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.settings_path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            try:                            # 极端情况下退化为直接覆盖
                with open(self.settings_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
            except OSError:
                pass

    # --------------------------------------------------------------- 窗口 #
    def _setup_window(self):
        self.root.title(self.window_title)
        self.root.configure(bg=BG)
        self.root.geometry("580x720")
        self.root.minsize(540, 650)
        try:
            self.root.iconname(self.window_title)
        except tk.TclError:
            pass
        # 居中；多窗口时依次错开，避免完全重叠
        self.root.update_idletasks()
        w, h = 580, 720
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2 - 20
        if self.profile != "default":
            m = re.fullmatch(r"win(\d+)", self.profile)
            step = ((int(m.group(1)) - 1) % 8) if m else 1
            x += step * 30
            y += step * 30
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h - 80))
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _setup_style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                     foreground=FG, arrowcolor=MUT, bordercolor=PANEL2,
                     lightcolor=PANEL2, darkcolor=PANEL2, padding=4,
                     selectbackground=PANEL2, selectforeground=FG)
        st.map("TCombobox",
               fieldbackground=[("readonly", PANEL2), ("disabled", PANEL)],
               foreground=[("readonly", FG), ("disabled", MUT)],
               arrowcolor=[("disabled", LINE)],
               selectbackground=[("readonly", PANEL2)],
               selectforeground=[("readonly", FG)])
        st.configure("TSpinbox", fieldbackground=PANEL2, background=PANEL2,
                     foreground=FG, arrowcolor=MUT, bordercolor=PANEL2,
                     lightcolor=PANEL2, darkcolor=PANEL2, padding=3,
                     selectbackground=PANEL2, selectforeground=FG)
        st.configure("Dark.Vertical.TScrollbar", background=PANEL2,
                     troughcolor="#0f1319", bordercolor="#0f1319",
                     arrowcolor=MUT, darkcolor=PANEL2, lightcolor=PANEL2,
                     relief="flat", arrowsize=11, width=9)
        st.map("Dark.Vertical.TScrollbar",
               background=[("active", "#2c3340"), ("pressed", "#343c4b")])
        self.root.option_add("*TCombobox*Listbox.background", PANEL2)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", BLUE)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", (self.ff, 10))

    def _small_button(self, master, text, command, bg=PANEL2, fg=FG,
                      size=9, width=None, bold=False):
        btn = tk.Button(master, text=text, command=command, bg=bg, fg=fg,
                        activebackground=mix(bg, "#ffffff", 0.12),
                        activeforeground=fg, relief="flat", bd=0, cursor="hand2",
                        font=(self.ff, size, "bold" if bold else "normal"),
                        padx=9, pady=4, highlightthickness=0)
        if width:
            btn.configure(width=width)
        return btn

    # ----------------------------------------------------------------- UI #
    def _build_ui(self):
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=16, pady=14)

        # ---- 标题栏 ----
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        left = tk.Frame(head, bg=BG)
        left.pack(side="left", anchor="w")
        title_row = tk.Frame(left, bg=BG)
        title_row.pack(anchor="w")
        tk.Label(title_row, text=APP_TITLE, bg=BG, fg=FG,
                 font=(self.ff, 17, "bold")).pack(side="left")
        if self.profile != "default":
            m = re.fullmatch(r"win(\d+)", self.profile)
            tag = f"窗口{int(m.group(1))}" if m else self.profile
            tk.Label(title_row, text=tag, bg=BLUE_BG, fg=LIGHT_BLUE,
                     font=(self.ff, 9, "bold"), padx=6, pady=1).pack(
                         side="left", padx=(8, 0), pady=(1, 0))
        tk.Label(left, text=APP_SUB, bg=BG, fg=MUT,
                 font=(self.ff, 9)).pack(anchor="w", pady=(2, 0))

        right = tk.Frame(head, bg=BG)
        right.pack(side="right", anchor="e")
        self.dot = tk.Canvas(right, width=10, height=10, bg=BG, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 6), pady=(6, 0))
        self._dot_item = self.dot.create_oval(1, 1, 9, 9, fill=OFF_CLR, outline="")
        self.conn_lbl = tk.Label(right, text="", bg=BG, fg=MUT,
                                 font=(self.ff, 10))
        self.conn_lbl.pack(side="left")

        # ---- 工具栏 ----
        tools = tk.Frame(outer, bg=BG)
        tools.pack(fill="x", pady=(12, 0))
        tk.Label(tools, text="设备面板", bg=BG, fg=FG,
                 font=(self.ff, 11, "bold")).pack(side="left")
        tk.Label(tools, text="点动", bg=BG, fg=MUT,
                 font=(self.ff, 9)).pack(side="left", padx=(14, 4))
        self.pulse_var = tk.IntVar(value=self.pulse_ms)
        ttk.Spinbox(tools, from_=50, to=10000, increment=50,
                    textvariable=self.pulse_var, width=5,
                    font=(self.ff, 9)).pack(side="left")
        tk.Label(tools, text="毫秒", bg=BG, fg=MUT,
                 font=(self.ff, 9)).pack(side="left", padx=(4, 0))

        self.new_win_btn = self._small_button(tools, "新窗口", self.open_new_window,
                                              bg=PANEL2, fg=FG, size=8)
        self.new_win_btn.pack(side="right")
        self.refresh_btn = self._small_button(tools, "刷新端口", self.refresh_ports,
                                              bg=PANEL2, fg=MUT, size=8)
        self.refresh_btn.pack(side="right", padx=(0, 6))
        self.add_btn = self._small_button(tools, "+ 设备", self.add_device,
                                          bg=PANEL2, fg=FG, size=8)
        self.add_btn.pack(side="right", padx=(0, 6))

        # ---- 所有设备面板（可滚动）----
        devwrap = tk.Frame(outer, bg=BG)
        devwrap.pack(fill="x", pady=(8, 0))
        self.dev_canvas = tk.Canvas(devwrap, bg=BG, highlightthickness=0, height=120)
        self.dev_sb = ttk.Scrollbar(devwrap, orient="vertical",
                                    command=self.dev_canvas.yview,
                                    style="Dark.Vertical.TScrollbar")
        self.dev_inner = tk.Frame(self.dev_canvas, bg=BG)
        self._dev_window = self.dev_canvas.create_window((0, 0), window=self.dev_inner,
                                                         anchor="nw")
        self.dev_canvas.configure(yscrollcommand=self.dev_sb.set)
        self.dev_canvas.pack(side="left", fill="both", expand=True)
        self.dev_inner.bind(
            "<Configure>",
            lambda _e: self.dev_canvas.configure(scrollregion=self.dev_canvas.bbox("all")))
        self.dev_canvas.bind(
            "<Configure>",
            lambda e: self.dev_canvas.itemconfigure(self._dev_window, width=e.width))

        # ---- 状态栏（先占位，保证窗口压缩时不被挤掉）----
        self.status = tk.Label(outer, text="就绪", bg=BG, fg="#5c6675", anchor="w",
                               font=(self.ff, 9))
        self.status.pack(side="bottom", fill="x", pady=(8, 0))

        # ---- 日志 ----
        logcard = tk.Frame(outer, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        logcard.pack(fill="both", expand=True, pady=(10, 0))
        lhead = tk.Frame(logcard, bg=PANEL)
        lhead.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(lhead, text="通信日志", bg=PANEL, fg=MUT,
                 font=(self.ff, 9)).pack(side="left")
        self._small_button(lhead, "保存", self.save_log, bg=PANEL, fg=MUT,
                           size=8).pack(side="right")
        self._small_button(lhead, "清空", self.clear_log, bg=PANEL, fg=MUT,
                           size=8).pack(side="right", padx=(0, 4))

        body = tk.Frame(logcard, bg=PANEL)
        body.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.logbox = tk.Text(body, bg="#101419", fg="#c7cedb", relief="flat",
                              font=("Consolas", 9), wrap="char", height=7,
                              insertbackground=FG, padx=8, pady=6,
                              highlightthickness=0)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.logbox.yview,
                           style="Dark.Vertical.TScrollbar")
        self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(side="left", fill="both", expand=True)
        self.logbox.tag_configure("time", foreground="#5c6675")
        self.logbox.tag_configure("info", foreground="#c7cedb")
        self.logbox.tag_configure("ok", foreground=GREEN)
        self.logbox.tag_configure("tx", foreground="#7fb4ff")
        self.logbox.tag_configure("rx", foreground=AMBER)
        self.logbox.tag_configure("err", foreground=RED)
        self.logbox.configure(state="disabled")

    def _bind_keys(self):
        self.root.bind("<F5>", lambda _e: self.refresh_ports())
        self.root.bind("<Control-n>", lambda _e: self.open_new_window())

    # --------------------------------------------------------- 设备面板区 #
    def _port_values(self, dev: Device | None = None) -> list[str]:
        ports = list_ports()
        if dev is not None and dev.port and dev.port not in ports:
            ports.insert(0, dev.port)
        return ports

    def _add_card(self, dev: Device):
        card = DeviceCard(self.dev_inner, self, dev)
        self.cards[dev] = card
        self._refresh_cards()
        self._fit_dev_area()
        self._refresh_header()

    def _refresh_cards(self):
        for card in self.cards.values():
            card.refresh()

    def _fit_dev_area(self):
        self.dev_inner.update_idletasks()
        req = self.dev_inner.winfo_reqheight()
        height = max(90, min(340, req))
        self.dev_canvas.configure(height=height)
        bbox = self.dev_canvas.bbox("all")
        if bbox:
            self.dev_canvas.configure(scrollregion=bbox)
        if req > height:
            if not self._sb_visible:
                self.dev_sb.pack(side="right", fill="y")
                self._sb_visible = True
        else:
            if self._sb_visible:
                self.dev_sb.pack_forget()
                self._sb_visible = False
            self.dev_canvas.yview_moveto(0)
        self._refresh_header()

    def _refresh_header(self):
        total = len(self.devices)
        connected = sum(1 for d in self.devices if d.ser is not None)
        opening = any(d.opening for d in self.devices)
        if total and connected == total:
            color = GREEN
        elif connected or opening:
            color = AMBER
        else:
            color = OFF_CLR
        self.dot.itemconfigure(self._dot_item, fill=color)
        self.conn_lbl.configure(text=f"{connected}/{total} 已连接",
                                fg=color if (connected or opening) else MUT)

    def add_device(self):
        used = {d.port for d in self.devices}
        free = [p for p in list_ports() if p not in used]
        if not free:
            messagebox.showinfo(APP_TITLE,
                                "没有发现其它可用串口。\n\n"
                                "请先插好继电器，再点“刷新端口”。",
                                parent=self.root)
            return
        port = self._ask_port(free)
        if not port:
            return
        dev = Device(port, DEFAULT_BAUD, 1)
        self.devices.append(dev)
        self._add_card(dev)
        self._save_settings()
        self.log("info", "已添加，正在连接…", dev)
        self.connect(dev)

    def _ask_port(self, ports: list[str]) -> str | None:
        """让用户选择要添加的串口（避免自动选中 COM1 这类非继电器口）。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("添加设备")
        dlg.configure(bg=PANEL)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        w, h = 330, 152
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - w) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - h) // 3)
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        tk.Label(dlg, text="选择要添加的串口：", bg=PANEL, fg=FG,
                 font=(self.ff, 10)).pack(anchor="w", padx=16, pady=(16, 6))
        var = tk.StringVar(value=ports[0])
        cb = ttk.Combobox(dlg, textvariable=var, values=ports, state="readonly",
                          font=(self.ff, 10))
        cb.pack(fill="x", padx=16)
        cb.focus_set()

        result = {"port": None}

        def ok(_evt=None):
            result["port"] = var.get().strip().upper() or None
            dlg.destroy()

        def cancel(_evt=None):
            dlg.destroy()

        btns = tk.Frame(dlg, bg=PANEL)
        btns.pack(fill="x", padx=16, pady=(14, 0))
        self._small_button(btns, "取消", cancel, bg=PANEL2, fg=MUT,
                           size=9).pack(side="right")
        self._small_button(btns, "添加", ok, bg=GREEN_DARK, fg="#8ef0b4",
                           size=9, bold=True).pack(side="right", padx=(0, 8))
        cb.bind("<Return>", ok)
        dlg.bind("<Escape>", cancel)
        dlg.protocol("WM_DELETE_WINDOW", cancel)
        self.root.wait_window(dlg)
        return result["port"]

    def remove_device(self, dev: Device):
        if len(self.devices) <= 1:
            messagebox.showinfo(APP_TITLE, "至少保留一台设备。", parent=self.root)
            return
        if not messagebox.askyesno(APP_TITLE, f"移除 {dev.port}？", parent=self.root):
            return
        self.close_device(dev)
        card = self.cards.pop(dev, None)
        if card is not None:
            if card.raw_dialog is not None:
                try:
                    card.raw_dialog.destroy()
                except tk.TclError:
                    pass
            card.card.destroy()
        self.devices.remove(dev)
        self._refresh_cards()
        self._fit_dev_area()
        self._refresh_header()
        self._save_settings()
        self.log("info", f"已移除 {dev.port}")

    def close_device(self, dev: Device):
        with dev.lock:
            if dev.ser is not None:
                try:
                    dev.ser.close()
                finally:
                    dev.ser = None
        dev.state = "unknown"
        dev.busy = False
        dev.opening = False
        dev.error = ""

    # ------------------------------------------------------------- 日志等 #
    def log(self, kind: str, msg: str, dev: Device | None = None):
        if dev is not None:
            msg = f"[{dev.port}] {msg}"
        ts = time.strftime("%H:%M:%S")
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{ts} ", "time")
        self.logbox.insert("end", f"{msg}\n", kind)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    def save_log(self):
        path = filedialog.asksaveasfilename(defaultextension=".log",
                                            filetypes=[("日志", "*.log"), ("全部", "*.*")],
                                            initialfile=time.strftime("relay-%Y%m%d-%H%M%S.log"))
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.logbox.get("1.0", "end"))
            self.log("ok", f"日志已保存：{path}")

    def set_status(self, text: str, color: str = "#5c6675"):
        self.status.configure(text=text, fg=color)

    # -------------------------------------------------------------- 线程 #
    def _submit(self, fn, dev: Device):
        """每个任务一个短线程：一台设备点动/读回传时不会卡住另一台。"""
        def run():
            try:
                fn()
            except SerialError as exc:
                self._post(("error", (dev, str(exc))))
            except Exception as exc:                       # noqa: BLE001
                self._post(("error", (dev, f"{type(exc).__name__}: {exc}")))

        threading.Thread(target=run, daemon=True, name=f"relay-{dev.port}").start()

    def _post(self, item):
        if not self.closing:
            self.events.put(item)

    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                try:
                    self._handle(kind, payload)
                except Exception as exc:                   # noqa: BLE001
                    self.log("err", f"事件处理失败（{kind}）：{exc}")
        except queue.Empty:
            pass
        self.root.after(40, self._poll_events)

    def _handle(self, kind, payload):
        if kind == "log":
            dev, kind2, msg = payload
            if dev in self.devices:
                self.log(kind2, msg, dev)
            return
        dev, *rest = payload
        if dev not in self.devices:
            return
        if kind == "connected":
            self._on_connected(dev, *rest)
        elif kind == "disconnected":
            self._on_disconnected(dev)
        elif kind == "state":
            on, frame = rest
            dev.state = "on" if on else "off"
            dev.tx_count += 1
            dev.last_frame = frame
            dev.error = ""
            card = self.cards.get(dev)
            if card is not None:
                card.refresh()
            self.set_status(f"{dev.port} · 已发送 {frame} · 累计 {dev.tx_count} 帧",
                            GREEN if on else MUT)
        elif kind == "busy":
            (busy,) = rest
            if not busy:
                dev.error = ""
            self._set_busy(dev, bool(busy))
        elif kind == "error":
            (msg,) = rest
            dev.busy = False
            dev.opening = False
            dev.error = msg
            card = self.cards.get(dev)
            if card is not None:
                card.refresh()
            self._refresh_header()
            self.log("err", f"错误：{msg}", dev)
            self.set_status(f"{dev.port} 错误：{msg}", RED)

    def _set_busy(self, dev: Device, busy: bool):
        dev.busy = busy
        card = self.cards.get(dev)
        if card is not None:
            card.refresh()
        if busy:
            self.set_status(f"{dev.port} · 正在执行…", AMBER)
        elif not any(d.busy for d in self.devices):
            self.set_status(f"{dev.port} · 累计 {dev.tx_count} 帧",
                            GREEN if dev.ser is not None else MUT)

    # ------------------------------------------------------------ 连接逻辑 #
    def toggle_connect(self, dev: Device):
        if dev.ser is not None:
            self.disconnect(dev)
        elif not dev.opening:
            self.connect(dev)

    def auto_connect(self):
        for dev in list(self.devices):
            if dev.ser is None and not dev.opening:
                self.connect(dev)

    def connect(self, dev: Device):
        if dev.ser is not None or dev.opening:
            return
        dev.opening = True
        dev.error = ""
        self.log("info", f"正在打开（{dev.baud} 8N1）…", dev)
        self.set_status(f"{dev.port} 正在连接…", AMBER)
        card = self.cards.get(dev)
        if card is not None:
            card.refresh()
        self._refresh_header()

        def job():
            try:
                with dev.lock:
                    if dev.ser is None:
                        ser = SerialPort(dev.port, int(dev.baud))
                        ser.open()
                        dev.ser = ser
            finally:
                dev.opening = False
            self._post(("connected", (dev, dev.port, int(dev.baud))))

        self._submit(job, dev)

    def _on_connected(self, dev: Device, port: str, baud: int):
        dev.state = "unknown"
        dev.error = ""
        dev.last_frame = ""
        card = self.cards.get(dev)
        if card is not None:
            card.refresh()
        self._refresh_header()
        self.log("ok", f"已连接（{baud} 8N1），可以开始控制", dev)
        self.set_status(f"{port} 已连接", GREEN)
        self._save_settings()

    def disconnect(self, dev: Device):
        if dev.ser is None:
            return

        def job():
            with dev.lock:
                if dev.ser is not None:
                    dev.ser.close()
                    dev.ser = None
                dev.state = "unknown"
            self._post(("disconnected", (dev,)))

        self._submit(job, dev)

    def _on_disconnected(self, dev: Device):
        self.close_device(dev)
        card = self.cards.get(dev)
        if card is not None:
            card.refresh()
        self._refresh_header()
        self.log("info", "串口已关闭", dev)
        self.set_status(f"{dev.port} · 已断开")

    # ------------------------------------------------------------ 控制逻辑 #
    def _ensure_open_in_job(self, dev: Device):
        """在工作线程内确保该设备串口已打开；需要时自动连接。"""
        if dev.ser is not None:
            return
        self._post(("log", (dev, "info", "未打开，自动连接…")))
        with dev.lock:
            if dev.ser is None:
                ser = SerialPort(dev.port, int(dev.baud))
                ser.open()
                dev.ser = ser
        self._post(("connected", (dev, dev.port, int(dev.baud))))

    def set_state(self, dev: Device, on: bool):
        if dev not in self.devices or dev.busy or dev.opening:
            return
        self._set_busy(dev, True)
        frame = build_frame(dev.addr, on)

        def job():
            self._ensure_open_in_job(dev)
            with dev.lock:
                ser = dev.ser
                if ser is None:
                    raise SerialError(0, "串口不可用")
                ser.write(frame)
                ser.flush()
            self._post(("log", (dev, "tx",
                                f"TX → {frame.hex(' ').upper()}   "
                                f"({'吸合' if on else '释放'})")))
            self._post(("state", (dev, on, frame.hex(" ").upper())))
            self._post(("busy", (dev, False)))

        self._submit(job, dev)

    def on_pulse(self, dev: Device):
        if dev not in self.devices or dev.busy or dev.opening:
            return
        self._set_busy(dev, True)
        ms = self._pulse_value()
        on_f = build_frame(dev.addr, True)
        off_f = build_frame(dev.addr, False)

        def job():
            self._ensure_open_in_job(dev)
            with dev.lock:
                ser = dev.ser
                if ser is None:
                    raise SerialError(0, "串口不可用")
                ser.write(on_f)
                ser.flush()
            self._post(("state", (dev, True, on_f.hex(" ").upper())))
            self._post(("log", (dev, "tx", f"TX → {on_f.hex(' ').upper()}   (点动开始)")))
            time.sleep(ms / 1000.0)
            with dev.lock:
                if dev.ser is None:
                    raise SerialError(0, "串口已关闭")
                dev.ser.write(off_f)
                dev.ser.flush()
            self._post(("state", (dev, False, off_f.hex(" ").upper())))
            self._post(("log", (dev, "tx",
                                f"TX → {off_f.hex(' ').upper()}   (点动结束 {ms} ms)")))
            self._post(("busy", (dev, False)))

        self._submit(job, dev)

    def send_raw(self, dev: Device, data: bytes) -> bool:
        if dev not in self.devices or dev.busy or dev.opening:
            return False
        if not data:
            return False
        self._set_busy(dev, True)

        def job():
            self._ensure_open_in_job(dev)
            with dev.lock:
                ser = dev.ser
                if ser is None:
                    raise SerialError(0, "串口不可用")
                ser.reset_input_buffer()
                ser.write(data)
                ser.flush()
                reply = ser.read(0.30)
            self._post(("log", (dev, "tx", f"TX → {data.hex(' ').upper()}")))
            if reply:
                self._post(("log", (dev, "rx", f"RX ← {reply.hex(' ').upper()}")))
                if len(reply) >= 4:
                    self._post(("log", (dev, "info", "收到 4 字节回传，可能是状态应答")))
            else:
                self._post(("log", (dev, "info", "无回传（LCUS-1 等型号单向发送，正常）")))
            self._post(("busy", (dev, False)))

        self._submit(job, dev)
        return True

    def open_raw_dialog(self, dev: Device):
        card = self.cards.get(dev)
        if card is None:
            return
        if card.raw_dialog is not None:
            try:
                card.raw_dialog.lift()
                card.raw_dialog.focus_force()
                return
            except tk.TclError:
                card.raw_dialog = None

        dlg = tk.Toplevel(self.root)
        dlg.title(f"原始 HEX 指令 · {dev.port}")
        dlg.configure(bg=PANEL)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        w, h = 400, 164
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - w) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - h) // 2)
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        card.raw_dialog = dlg

        tk.Label(dlg, text=f"目标：{dev.port} · {dev.baud} 8N1 · 通道 {dev.addr}",
                 bg=PANEL, fg=MUT, font=(self.ff, 9)).pack(anchor="w", padx=16,
                                                           pady=(14, 6))
        row = tk.Frame(dlg, bg=PANEL)
        row.pack(fill="x", padx=16)
        var = tk.StringVar(value=build_frame(dev.addr, True).hex(" ").upper())
        ent = tk.Entry(row, textvariable=var, bg=PANEL2, fg=FG,
                       insertbackground=FG, relief="flat", font=("Consolas", 10),
                       highlightthickness=0)
        ent.pack(side="left", fill="x", expand=True, ipady=5)
        note = tk.Label(dlg, text="", bg=PANEL, fg=MUT, font=(self.ff, 8), anchor="w")
        note.pack(fill="x", padx=16, pady=(8, 0))

        def close():
            card.raw_dialog = None
            dlg.destroy()

        def send(_evt=None):
            text = var.get().strip()
            try:
                data = bytes.fromhex(
                    text.replace(",", " ").replace("0x", " ").replace("0X", " "))
            except ValueError:
                note.configure(text="HEX 格式错误", fg=RED)
                return
            if not data:
                return
            if self.send_raw(dev, data):
                note.configure(text=f"已提交：{data.hex(' ').upper()}", fg=GREEN)
            else:
                note.configure(text="设备正忙，请稍候", fg=AMBER)

        send_btn = self._small_button(row, "发送", send, bg=GREEN_DARK,
                                      fg="#8ef0b4", size=9, bold=True)
        send_btn.pack(side="left", padx=(8, 0))
        ent.bind("<Return>", send)
        dlg.protocol("WM_DELETE_WINDOW", close)
        ent.focus_set()

    # --------------------------------------------------------- 端口 / 配置 #
    def refresh_ports(self):
        ports = list_ports()
        for card in self.cards.values():
            card.refresh_ports_list(ports)
        self.log("info", f"端口列表：{', '.join(ports) or '未发现串口'}")
        for dev in self.devices:
            if dev.port not in ports:
                self.log("info", "当前不在端口列表里（USB 未插好？）", dev)

    def _pulse_value(self) -> int:
        try:
            v = int(self.pulse_var.get())
        except (tk.TclError, ValueError):
            v = 500
        return max(50, min(10000, v))

    # ----------------------------------------------------------- 新窗口 #
    def open_new_window(self):
        """启动一个独立进程的新窗口（独立配置 + 独立设备子集）。"""
        self._save_settings()
        exe = sys.executable
        if sys.platform == "win32":
            pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(pyw):
                exe = pyw
        script = os.path.abspath(__file__)
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) if sys.platform == "win32" else 0
        try:
            subprocess.Popen([exe, script, "--new-window"],
                             cwd=os.path.dirname(script),
                             close_fds=True, creationflags=flags)
        except OSError as exc:
            self.log("err", f"打开新窗口失败：{exc}")
            return
        self.log("ok", "已打开新窗口（独立配置，请在新窗口里选择设备后点“连接”）")

    # --------------------------------------------------------------- 退出 #
    def on_close(self):
        self._save_settings()
        self.closing = True
        for dev in list(self.devices):
            try:
                self.close_device(dev)
            except Exception:
                pass
        self.root.destroy()


# ---------------------------------------------------------------- 多窗口配置 #
def _normalize_profile(name: str) -> str:
    name = re.sub(r"[^\w-]+", "_", str(name).strip()).strip("_").lower()
    return name or "window"


def _profile_title(profile: str) -> str:
    if profile == "default":
        return APP_TITLE
    m = re.fullmatch(r"win(\d+)", profile)
    if m:
        return f"{APP_TITLE} · 窗口{int(m.group(1))}"
    return f"{APP_TITLE} · {profile}"


def _profile_settings_path(profile: str) -> str:
    if profile == "default":
        return SETTINGS_PATH
    return os.path.join(os.path.dirname(SETTINGS_PATH), f"settings-{profile}.json")


def _close_handle(handle) -> None:
    if not handle:
        return
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int
        k32.CloseHandle(handle)
    except Exception:
        pass


def _try_acquire_profile(profile: str):
    """每个窗口配置一把命名互斥体：同一配置只允许一个窗口，不同配置可同时开。"""
    if sys.platform != "win32":
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k32.CreateMutexW.restype = ctypes.c_void_p
        handle = k32.CreateMutexW(None, 0, MUTEX_PREFIX + profile)
        if not handle:
            return None
        if ctypes.get_last_error() == 183:          # ERROR_ALREADY_EXISTS
            _close_handle(handle)
            return None
        return handle
    except Exception:
        return None


def _focus_existing_window(title: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _show_message(kind: str, text: str) -> None:
    root = tk.Tk()
    root.withdraw()
    func = {"warning": messagebox.showwarning,
            "info": messagebox.showinfo,
            "error": messagebox.showerror}.get(kind, messagebox.showinfo)
    try:
        func(APP_TITLE, text)
    finally:
        root.destroy()


def _choose_profile(requested: str | None, force_new: bool):
    """选择窗口配置，返回 (profile, mutex, auto_connect)；失败返回 None。

    - 双击启动：主配置空闲就用主配置，否则自动开“窗口2/3/…”；
    - 新窗口：跳过主配置，直接找下一个空闲窗口编号；
    - 只有主配置（default）会在启动时自动连接；其它配置都手动连接，
      避免抢已连接的端口。
    """
    if requested:
        profile = _normalize_profile(requested)
        handle = _try_acquire_profile(profile)
        if handle is None:
            _focus_existing_window(_profile_title(profile))
            _show_message("warning",
                          f"配置“{profile}”的窗口已经在运行。\n\n"
                          "如需再开一个窗口，请点程序里的“新窗口”，"
                          "或直接双击启动图标。")
            return None
        return profile, handle, profile == "default"

    if not force_new:
        handle = _try_acquire_profile("default")
        if handle is not None:
            return "default", handle, True

    for i in range(2, MAX_WINDOWS + 1):
        profile = f"win{i}"
        handle = _try_acquire_profile(profile)
        if handle is not None:
            return profile, handle, False

    _show_message("warning", f"窗口数量已达到上限（{MAX_WINDOWS} 个）。")
    return None


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="USB 继电器控制台")
    ap.add_argument("--profile", metavar="名称", help="使用指定的窗口配置")
    ap.add_argument("--new-window", action="store_true", help="新开一个独立窗口")
    args = ap.parse_args(argv)

    chosen = _choose_profile(args.profile, args.new_window)
    if chosen is None:
        return 0
    profile, mutex, auto_connect = chosen
    try:
        if sys.platform == "win32":
            try:                                   # 高分屏不糊
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        root = tk.Tk()
        App(root,
            profile=profile,
            settings_path=_profile_settings_path(profile),
            window_title=_profile_title(profile),
            auto_connect=auto_connect)
        root.mainloop()
    except Exception:
        # pythonw 下看不到控制台，出错时留一份日志便于排查
        import traceback
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                fh.write(traceback.format_exc())
        except Exception:
            pass
        raise
    finally:
        _close_handle(mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
