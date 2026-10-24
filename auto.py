"""Art-Net on 127.0.0.1 -> first available USB-to-serial adapter, with a minimal window.

Run with --headless for a terminal-only version.
"""

import argparse
import signal
import sys
import threading
import time

from serial.tools import list_ports

from .artnet import ArtNetReceiver, UniverseStore, format_universe, list_interfaces
from .serial_out import PROTO_ENTTEC_PRO, PROTO_OPEN_DMX, PROTO_RAW, OutputConfig, SerialOutput

PROTOCOL_ARGS = {"enttec-pro": PROTO_ENTTEC_PRO, "dmx512": PROTO_OPEN_DMX, "raw": PROTO_RAW}
ENTTEC_PRO_HINTS = ("dmx usb pro", "enttec", "dmxking", "ultradmx")


def usb_serial_ports():
    """USB serial ports (those reporting a USB vendor ID), in a stable order."""
    ports = [p for p in list_ports.comports() if p.vid is not None]
    return sorted(ports, key=lambda p: p.device)


def detect_protocol(port):
    info = " ".join(filter(None, [port.description, port.manufacturer, port.product])).lower()
    return PROTO_ENTTEC_PRO if any(h in info for h in ENTTEC_PRO_HINTS) else PROTO_OPEN_DMX


def device_name(port):
    parts = [port.manufacturer, port.product or port.description]
    return " ".join(p for p in parts if p and p != "n/a") or port.device


def describe(port):
    return f"{port.device} ({device_name(port)}, {port.vid:04x}:{port.pid:04x})"


def log(msg):
    sys.stdout.write("\r\033[K" + time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stdout.flush()


class Bridge:
    """Receives Art-Net and keeps one SerialOutput attached to the first USB adapter."""

    def __init__(self, args, log=log):
        self.args = args
        self.log = log
        self.store = UniverseStore()
        self.worker = None
        self.port = None  # serial.tools ListPortInfo of the adapter in use
        self._warned = set()
        self._lock = threading.Lock()

        iface = next((i for i in list_interfaces() if i.ip == args.ip), None)
        if iface is None:
            raise OSError(f"No network interface has the address {args.ip}")
        self.receiver = ArtNetReceiver(self.store, iface, universes_fn=lambda: [args.universe], log=log)
        self.receiver.start()

    def packet_count(self):
        return self.store.stats().get(self.args.universe, (0,))[0]

    def refresh(self, force=False):
        """Drop the adapter if it was unplugged, and attach the first one if none is in use.

        With force=True the current adapter is released and the ports are rescanned.
        """
        with self._lock:
            ports = usb_serial_ports()
            if self.worker is not None and (force or self.worker.cfg.port not in [p.device for p in ports]):
                if not force:
                    self.log(f"{self.worker.cfg.port} disconnected")
                self._stop_worker()
            if self.worker is None and ports:
                port = ports[0]
                a = self.args
                proto = PROTOCOL_ARGS.get(a.protocol) or detect_protocol(port)
                cfg = OutputConfig(universe=a.universe, port=port.device, protocol=proto,
                                   baudrate=a.baud, channels=a.channels, rate_hz=a.rate)
                self.log(f"Using {describe(port)} as {proto}")
                self.port = port
                self.worker = SerialOutput(cfg, self.store, log=self.log)
                self.worker.start()
            elif self.worker is None and force:
                self.log("No USB-to-serial adapter found")

        # Point out data arriving on universes we're not forwarding.
        for u in self.store.stats():
            if u != self.args.universe and u not in self._warned:
                self._warned.add(u)
                self.log(f"Note: receiving universe {u} ({format_universe(u)}), but forwarding only {self.args.universe}")

    def _stop_worker(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker.join(timeout=2)
        self.worker = None
        self.port = None

    def close(self):
        with self._lock:
            self._stop_worker()
        self.receiver.stop()
        self.receiver.join(timeout=2)


def run_headless(bridge):
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    a = bridge.args
    log(f"Forwarding universe {a.universe} ({format_universe(a.universe)}); Ctrl+C to quit")
    waiting_logged = False
    last_count, last_frames, last_worker, last_t = 0, 0, None, time.monotonic()
    while not stop.is_set():
        bridge.refresh()
        worker = bridge.worker
        if worker is None and not waiting_logged:
            log("Waiting for a USB-to-serial adapter to be plugged in…")
        waiting_logged = worker is None

        now = time.monotonic()
        dt = now - last_t
        count = bridge.packet_count()
        frames = worker.frames_sent if worker else 0
        if worker is not last_worker:
            last_frames, last_worker = 0, worker
        in_rate = (count - last_count) / dt
        out_rate = (frames - last_frames) / dt
        last_count, last_frames, last_t = count, frames, now
        state = worker.status if worker else "no adapter"
        sys.stdout.write(f"\r\033[Kin {in_rate:4.0f} pkt/s   out {out_rate:4.0f} fps   [{state}]")
        sys.stdout.flush()
        stop.wait(1.0)
    sys.stdout.write("\n")
    bridge.close()


def run_window(bridge):
    import tkinter as tk
    from tkinter import ttk

    LED_ON, LED_DIM, LED_OFF = "#2ecc40", "#145a1c", "#555"

    root = tk.Tk()
    root.title("Art-Net → USB")
    root.resizable(False, False)

    frm = ttk.Frame(root, padding=14)
    frm.pack(fill="both", expand=True)

    led = tk.Canvas(frm, width=22, height=22, highlightthickness=0, background=root.cget("background"))
    led_dot = led.create_oval(3, 3, 19, 19, fill=LED_OFF, outline="")
    led.grid(row=0, column=0, padx=(0, 8))
    artnet_lbl = ttk.Label(frm, text="Art-Net: waiting", width=28)
    artnet_lbl.grid(row=0, column=1, sticky="w")

    ttk.Label(frm, text="USB device").grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))
    device_lbl = ttk.Label(frm, text="", font=("TkDefaultFont", 13, "bold"))
    device_lbl.grid(row=2, column=0, columnspan=2, sticky="w")
    detail_lbl = ttk.Label(frm, text="", foreground="gray")
    detail_lbl.grid(row=3, column=0, columnspan=2, sticky="w")

    ttk.Button(frm, text="Refresh", command=lambda: (bridge.refresh(force=True), update_device())).grid(
        row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
    a = bridge.args
    ttk.Label(frm, text=f"Universe {a.universe} ({format_universe(a.universe)}) on {a.ip}", foreground="gray").grid(
        row=4, column=1, sticky="e", pady=(12, 0))

    state = {"count": 0, "last_rx": 0.0, "lit": False, "rates": [], "scan": 0.0}

    def update_device():
        w, port = bridge.worker, bridge.port
        if w is None or port is None:
            device_lbl.config(text="No adapter connected")
            detail_lbl.config(text="Plug one in and press Refresh")
            return
        status = "" if w.status == "running" else f" · {w.status}"
        device_lbl.config(text=device_name(port))
        detail_lbl.config(text=f"{port.device} · {w.cfg.protocol}{status}")

    def tick():
        now = time.monotonic()
        # Watch for unplugged adapters once a second.
        if now - state["scan"] >= 1.0:
            state["scan"] = now
            bridge.refresh()
            update_device()

        count = bridge.packet_count()
        new = count - state["count"]
        state["count"] = count
        state["rates"] = (state["rates"] + [new])[-10:]  # last second at 100 ms ticks
        if new:
            state["last_rx"] = now
            state["lit"] = not state["lit"]
        if now - state["last_rx"] < 0.5:
            led.itemconfig(led_dot, fill=LED_ON if state["lit"] else LED_DIM)
            artnet_lbl.config(text=f"Art-Net: receiving ({sum(state['rates'])} pkt/s)")
        else:
            led.itemconfig(led_dot, fill=LED_OFF)
            artnet_lbl.config(text="Art-Net: waiting" if not count else "Art-Net: no data")
        root.after(100, tick)

    def on_close():
        bridge.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    # The 100 ms tick keeps Python running, so Ctrl+C in the terminal is noticed.
    signal.signal(signal.SIGINT, lambda *_: root.after(0, on_close))
    update_device()
    tick()
    root.mainloop()


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m artnet_to_usb.auto",
        description="Forward Art-Net received on 127.0.0.1 to the first available USB-to-serial adapter.",
    )
    ap.add_argument("-u", "--universe", type=int, default=0, help="Art-Net universe / port address to forward (default 0)")
    ap.add_argument("-p", "--protocol", choices=["auto", *PROTOCOL_ARGS], default="auto",
                    help="serial framing; auto = Enttec Pro if the adapter identifies as one, else DMX512")
    ap.add_argument("-b", "--baud", type=int, default=250000, help="baud rate for --protocol raw (default 250000)")
    ap.add_argument("-r", "--rate", type=float, default=40.0, help="output refresh rate in Hz (default 40)")
    ap.add_argument("-c", "--channels", type=int, default=512, help="number of channels to send (default 512)")
    ap.add_argument("--ip", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    ap.add_argument("--headless", action="store_true", help="run in the terminal without a window")
    args = ap.parse_args(argv)
    if not 0 <= args.universe <= 32767:
        ap.error("universe must be 0-32767")

    try:
        bridge = Bridge(args)
    except OSError as e:
        sys.exit(f"Could not listen for Art-Net: {e}")
    bridge.refresh()
    if args.headless:
        run_headless(bridge)
    else:
        run_window(bridge)


if __name__ == "__main__":
    main()
