"""Tkinter UI for the Art-Net to USB-serial bridge."""

import json
import queue
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .artnet import ANY_INTERFACE, ArtNetReceiver, UniverseStore, format_universe, list_interfaces
from .serial_out import PROTO_RAW, PROTOCOLS, OutputConfig, SerialOutput, list_serial_ports

CONFIG_PATH = Path.home() / ".artnet_to_usb.json"
MONITOR_COLS = 32
MONITOR_ROWS = 16
CELL = 18


class OutputDialog(tk.Toplevel):
    """Add/edit one universe -> serial port mapping."""

    def __init__(self, parent, cfg=None):
        super().__init__(parent)
        self.title("Output")
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        cfg = cfg or OutputConfig()

        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")

        self.universe = tk.StringVar(value=str(cfg.universe))
        self.port = tk.StringVar(value=cfg.port)
        self.protocol = tk.StringVar(value=cfg.protocol)
        self.baud = tk.StringVar(value=str(cfg.baudrate))
        self.channels = tk.StringVar(value=str(cfg.channels))
        self.rate = tk.StringVar(value=str(cfg.rate_hz))
        self.enabled = tk.BooleanVar(value=cfg.enabled)

        r = 0
        ttk.Label(frm, text="Art-Net universe (0-32767)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Spinbox(frm, from_=0, to=32767, textvariable=self.universe, width=10).grid(row=r, column=1, sticky="w")
        self.uni_label = ttk.Label(frm, foreground="gray")
        self.uni_label.grid(row=r, column=2, sticky="w", padx=6)
        self.universe.trace_add("write", lambda *_: self._update_uni_label())
        self._update_uni_label()

        r += 1
        ttk.Label(frm, text="Serial port").grid(row=r, column=0, sticky="w", pady=3)
        self.port_combo = ttk.Combobox(frm, textvariable=self.port, width=34)
        self.port_combo.grid(row=r, column=1, sticky="w")
        ttk.Button(frm, text="Refresh", command=self._refresh_ports).grid(row=r, column=2, padx=6)
        self._refresh_ports()

        r += 1
        ttk.Label(frm, text="Protocol").grid(row=r, column=0, sticky="w", pady=3)
        proto = ttk.Combobox(frm, textvariable=self.protocol, values=PROTOCOLS, state="readonly", width=34)
        proto.grid(row=r, column=1, sticky="w", columnspan=2)
        proto.bind("<<ComboboxSelected>>", lambda e: self._update_baud_state())

        r += 1
        ttk.Label(frm, text="Baud rate (Raw only)").grid(row=r, column=0, sticky="w", pady=3)
        self.baud_combo = ttk.Combobox(
            frm, textvariable=self.baud, width=10,
            values=["9600", "19200", "38400", "57600", "115200", "230400", "250000", "460800", "500000", "921600", "1000000"],
        )
        self.baud_combo.grid(row=r, column=1, sticky="w")

        r += 1
        ttk.Label(frm, text="Channels (1-512)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Spinbox(frm, from_=1, to=512, textvariable=self.channels, width=10).grid(row=r, column=1, sticky="w")

        r += 1
        ttk.Label(frm, text="Refresh rate (Hz)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Spinbox(frm, from_=1, to=200, textvariable=self.rate, width=10).grid(row=r, column=1, sticky="w")

        r += 1
        ttk.Checkbutton(frm, text="Enabled", variable=self.enabled).grid(row=r, column=0, sticky="w", pady=3)

        r += 1
        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="OK", command=self._ok).pack(side="right", padx=6)

        self._update_baud_state()
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window()

    def _update_uni_label(self):
        try:
            u = int(self.universe.get())
            self.uni_label.config(text=f"Net:Sub:Uni {format_universe(u)}" if 0 <= u <= 32767 else "")
        except ValueError:
            self.uni_label.config(text="")

    def _update_baud_state(self):
        self.baud_combo.config(state="normal" if self.protocol.get() == PROTO_RAW else "disabled")

    def _refresh_ports(self):
        ports = list_serial_ports()
        self.port_combo["values"] = [p for p, _ in ports]
        if not self.port.get() and ports:
            usb = [p for p, d in ports if "usb" in (p + d).lower()]
            self.port.set(usb[0] if usb else ports[0][0])

    def _ok(self):
        try:
            cfg = OutputConfig(
                universe=int(self.universe.get()),
                port=self.port.get().strip(),
                protocol=self.protocol.get(),
                baudrate=int(self.baud.get()),
                channels=int(self.channels.get()),
                rate_hz=float(self.rate.get()),
                enabled=self.enabled.get(),
            )
        except ValueError:
            messagebox.showerror("Invalid value", "Please enter numeric values.", parent=self)
            return
        if not 0 <= cfg.universe <= 32767:
            messagebox.showerror("Invalid value", "Universe must be 0-32767.", parent=self)
            return
        if not cfg.port:
            messagebox.showerror("Invalid value", "Select a serial port.", parent=self)
            return
        if not 1 <= cfg.channels <= 512 or not 1 <= cfg.rate_hz <= 200:
            messagebox.showerror("Invalid value", "Channels must be 1-512 and rate 1-200 Hz.", parent=self)
            return
        self.result = cfg
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Art-Net to USB")
        self.minsize(720, 640)

        self.store = UniverseStore()
        self.receiver = None
        self.outputs = []  # list[OutputConfig]
        self.workers = {}  # index -> SerialOutput
        self.log_queue = queue.Queue()
        self._last_counts = {}
        self._last_frames = {}
        self._last_tick = time.monotonic()
        self.interfaces = []

        self.iface_var = tk.StringVar()
        self.monitor_var = tk.StringVar(value="0")
        self.hover_var = tk.StringVar(value="")

        self._build()
        self._load_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(250, self._tick)

    # ---------- layout ----------

    def _build(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        top = ttk.LabelFrame(root, text="Art-Net input", padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Network interface").pack(side="left")
        self.iface_combo = ttk.Combobox(top, textvariable=self.iface_var, state="readonly", width=36)
        self.iface_combo.pack(side="left", padx=6)
        ttk.Button(top, text="Refresh", command=self._refresh_interfaces).pack(side="left")
        self.start_btn = ttk.Button(top, text="Start", command=self._toggle)
        self.start_btn.pack(side="right")
        self.status_lbl = ttk.Label(top, text="Stopped", foreground="gray")
        self.status_lbl.pack(side="right", padx=10)

        outs = ttk.LabelFrame(root, text="Outputs (universe → serial port)", padding=8)
        outs.pack(fill="x", pady=(10, 0))
        cols = ("en", "universe", "port", "protocol", "rate", "rx", "tx", "status")
        self.tree = ttk.Treeview(outs, columns=cols, show="headings", height=5, selectmode="browse")
        for col, text, w in [
            ("en", "On", 36), ("universe", "Universe", 90), ("port", "Serial port", 170),
            ("protocol", "Protocol", 170), ("rate", "Hz", 40), ("rx", "In pkt/s", 70),
            ("tx", "Out fps", 64), ("status", "Status", 160),
        ]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor="w", stretch=col in ("port", "status"))
        self.tree.pack(fill="x")
        self.tree.bind("<Double-1>", lambda e: self._edit_output())
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        bar = ttk.Frame(outs)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Button(bar, text="Add…", command=self._add_output).pack(side="left")
        ttk.Button(bar, text="Edit…", command=self._edit_output).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove", command=self._remove_output).pack(side="left")
        ttk.Label(bar, text="Changes apply immediately while running.", foreground="gray").pack(side="right")

        mon = ttk.LabelFrame(root, text="DMX monitor", padding=8)
        mon.pack(fill="both", expand=True, pady=(10, 0))
        mbar = ttk.Frame(mon)
        mbar.pack(fill="x")
        ttk.Label(mbar, text="Universe").pack(side="left")
        ttk.Spinbox(mbar, from_=0, to=32767, textvariable=self.monitor_var, width=8).pack(side="left", padx=6)
        self.mon_info = ttk.Label(mbar, text="", foreground="gray")
        self.mon_info.pack(side="left", padx=6)
        ttk.Label(mbar, textvariable=self.hover_var).pack(side="right")
        self.canvas = tk.Canvas(
            mon, width=MONITOR_COLS * CELL + 1, height=MONITOR_ROWS * CELL + 1,
            background="#111", highlightthickness=0,
        )
        self.canvas.pack(pady=(6, 0))
        self.cells = []
        for i in range(512):
            x, y = (i % MONITOR_COLS) * CELL, (i // MONITOR_COLS) * CELL
            self.cells.append(self.canvas.create_rectangle(x + 1, y + 1, x + CELL, y + CELL, fill="#000", outline="#222"))
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Leave>", lambda e: self.hover_var.set(""))
        self._monitor_frame = bytes(512)

        logf = ttk.LabelFrame(root, text="Log", padding=8)
        logf.pack(fill="x", pady=(10, 0))
        self.log_text = tk.Text(logf, height=6, state="disabled", wrap="word", font=("Menlo", 11))
        self.log_text.pack(fill="x")

    # ---------- interfaces ----------

    def _refresh_interfaces(self, select=None):
        self.interfaces = list_interfaces()
        labels = [ANY_INTERFACE] + [i.label for i in self.interfaces]
        self.iface_combo["values"] = labels
        current = select or self.iface_var.get()
        if current not in labels:
            # Match by interface name if its IP changed.
            name = current.split(" (")[0] if current else ""
            current = next((i.label for i in self.interfaces if i.name == name), ANY_INTERFACE)
        self.iface_var.set(current)

    def _selected_interface(self):
        label = self.iface_var.get()
        return next((i for i in self.interfaces if i.label == label), None)

    # ---------- outputs ----------

    def _refresh_tree(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for idx, cfg in enumerate(self.outputs):
            self.tree.insert("", "end", iid=str(idx), values=(
                "✓" if cfg.enabled else "", f"{cfg.universe} ({format_universe(cfg.universe)})",
                cfg.port, cfg.protocol + (f" @{cfg.baudrate}" if cfg.protocol == PROTO_RAW else ""),
                f"{cfg.rate_hz:g}", "", "", "",
            ))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel)

    def _check_port_conflict(self, cfg, skip_index=None):
        for i, other in enumerate(self.outputs):
            if i != skip_index and other.enabled and cfg.enabled and other.port == cfg.port:
                messagebox.showerror("Port in use", f"{cfg.port} is already used by another output.")
                return False
        return True

    def _add_output(self):
        dlg = OutputDialog(self)
        if dlg.result and self._check_port_conflict(dlg.result):
            self.outputs.append(dlg.result)
            self._outputs_changed()

    def _edit_output(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        dlg = OutputDialog(self, self.outputs[idx])
        if dlg.result and self._check_port_conflict(dlg.result, idx):
            self.outputs[idx] = dlg.result
            self._outputs_changed()

    def _remove_output(self):
        sel = self.tree.selection()
        if not sel:
            return
        del self.outputs[int(sel[0])]
        self._outputs_changed()

    def _outputs_changed(self):
        self._refresh_tree()
        self._save_config()
        if self.receiver is not None:
            self._restart_workers()

    def _on_select(self, _event):
        sel = self.tree.selection()
        if sel:
            self.monitor_var.set(str(self.outputs[int(sel[0])].universe))

    # ---------- run control ----------

    def _toggle(self):
        if self.receiver is None:
            self._start()
        else:
            self._stop()

    def _start(self):
        iface = self._selected_interface()
        try:
            self.receiver = ArtNetReceiver(
                self.store, iface,
                universes_fn=lambda: [o.universe for o in self.outputs if o.enabled],
                log=self.log,
            )
        except OSError as e:
            messagebox.showerror("Cannot listen", f"Could not open UDP port 6454:\n{e}")
            self.receiver = None
            return
        self.store.clear()
        self.receiver.start()
        self._restart_workers()
        self.start_btn.config(text="Stop")
        self.iface_combo.config(state="disabled")
        self.status_lbl.config(text=f"Running on {iface.label if iface else 'all interfaces'}", foreground="green")
        self._save_config()

    def _stop(self):
        self._stop_workers()
        if self.receiver is not None:
            self.receiver.stop()
            self.receiver = None
        self.start_btn.config(text="Start")
        self.iface_combo.config(state="readonly")
        self.status_lbl.config(text="Stopped", foreground="gray")
        self._refresh_tree()

    def _stop_workers(self):
        for w in self.workers.values():
            w.stop()
        for w in self.workers.values():
            w.join(timeout=2)
        self.workers.clear()
        self._last_frames.clear()

    def _restart_workers(self):
        self._stop_workers()
        for idx, cfg in enumerate(self.outputs):
            if cfg.enabled:
                w = SerialOutput(cfg, self.store, log=self.log)
                self.workers[idx] = w
                w.start()

    # ---------- periodic UI update ----------

    def _tick(self):
        now = time.monotonic()
        dt = max(1e-3, now - self._last_tick)
        self._last_tick = now

        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.delete("1.0", "end-200l")
            self.log_text.see("end")
            self.log_text.config(state="disabled")

        stats = self.store.stats()
        rates = {}
        for u, (count, _, _) in stats.items():
            rates[u] = (count - self._last_counts.get(u, 0)) / dt
            self._last_counts[u] = count

        if self.receiver is not None:
            for idx, cfg in enumerate(self.outputs):
                iid = str(idx)
                if not self.tree.exists(iid):
                    continue
                w = self.workers.get(idx)
                tx = ""
                status = "disabled"
                if w is not None:
                    tx = f"{(w.frames_sent - self._last_frames.get(idx, 0)) / dt:.0f}"
                    self._last_frames[idx] = w.frames_sent
                    status = w.status
                    st = stats.get(cfg.universe)
                    if status == "running" and (st is None or now - st[1] > 3):
                        status = "running (no Art-Net)"
                self.tree.set(iid, "rx", f"{rates.get(cfg.universe, 0):.0f}")
                self.tree.set(iid, "tx", tx)
                self.tree.set(iid, "status", status)

        self._update_monitor(stats, rates, now)
        self.after(250, self._tick)

    def _update_monitor(self, stats, rates, now):
        try:
            u = int(self.monitor_var.get())
        except ValueError:
            return
        frame = self.store.get(u) or bytes(512)
        st = stats.get(u)
        if st:
            age = now - st[1]
            self.mon_info.config(text=f"{format_universe(u)} · from {st[2]} · {rates.get(u, 0):.0f} pkt/s"
                                 + (f" · last {age:.0f}s ago" if age > 2 else ""))
        else:
            self.mon_info.config(text=f"{format_universe(u)} · no data")
        old = self._monitor_frame
        for i in range(512):
            if frame[i] != old[i]:
                v = frame[i]
                self.canvas.itemconfig(self.cells[i], fill=f"#{v // 2:02x}{v:02x}{v // 2 + 64 if v else 0:02x}")
        self._monitor_frame = frame

    def _on_hover(self, event):
        col, row = event.x // CELL, event.y // CELL
        if 0 <= col < MONITOR_COLS and 0 <= row < MONITOR_ROWS:
            ch = row * MONITOR_COLS + col
            v = self._monitor_frame[ch]
            self.hover_var.set(f"Ch {ch + 1}: {v} ({v * 100 // 255}%)")

    def log(self, msg):
        # Called from worker threads; the UI thread drains the queue.
        self.log_queue.put(time.strftime("%H:%M:%S ") + msg)

    # ---------- config ----------

    def _load_config(self):
        data = {}
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
            except (OSError, ValueError) as e:
                self.log(f"Could not read {CONFIG_PATH}: {e}")
        self.outputs = [OutputConfig.from_dict(d) for d in data.get("outputs", [])]
        self._refresh_interfaces(select=data.get("interface"))
        self._refresh_tree()

    def _save_config(self):
        data = {"interface": self.iface_var.get(), "outputs": [o.to_dict() for o in self.outputs]}
        try:
            CONFIG_PATH.write_text(json.dumps(data, indent=2))
        except OSError as e:
            self.log(f"Could not save config: {e}")

    def _on_close(self):
        self._save_config()
        self._stop()
        self.destroy()


def main():
    App().mainloop()
