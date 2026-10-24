"""Serial (USB-to-serial) DMX output in several framing protocols."""

import threading
import time
from dataclasses import asdict, dataclass

import serial
from serial.tools import list_ports

PROTO_ENTTEC_PRO = "Enttec DMX USB Pro"
PROTO_OPEN_DMX = "DMX512 (break + 250k 8N2)"
PROTO_RAW = "Raw bytes"
PROTOCOLS = [PROTO_ENTTEC_PRO, PROTO_OPEN_DMX, PROTO_RAW]


def list_serial_ports():
    """Return [(device, description)] for all serial ports."""
    ports = []
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        desc = p.description if p.description and p.description != "n/a" else ""
        ports.append((p.device, desc))
    return ports


@dataclass
class OutputConfig:
    universe: int = 0
    port: str = ""
    protocol: str = PROTO_ENTTEC_PRO
    baudrate: int = 250000  # used by Raw; Enttec Pro ignores it, DMX512 is fixed at 250000
    channels: int = 512
    rate_hz: float = 40.0
    enabled: bool = True

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in fields})


def enttec_pro_frame(dmx):
    """Enttec DMX USB Pro 'Output Only Send DMX Packet' (label 6)."""
    payload = b"\x00" + dmx  # DMX start code + channels
    n = len(payload)
    return b"\x7e\x06" + bytes([n & 0xFF, n >> 8]) + payload + b"\xe7"


class SerialOutput(threading.Thread):
    """Streams one universe from the store to one serial port at a fixed rate."""

    def __init__(self, cfg, store, log=print):
        super().__init__(daemon=True, name=f"serial-{cfg.port}")
        self.cfg = cfg
        self.store = store
        self.log = log
        self._stop = threading.Event()
        self.ser = None
        self.frames_sent = 0
        self.status = "starting"

    def stop(self):
        self._stop.set()

    def _open(self):
        c = self.cfg
        if c.protocol == PROTO_OPEN_DMX:
            ser = serial.Serial(c.port, 250000, bytesize=8, parity="N", stopbits=2, timeout=1, write_timeout=1)
        elif c.protocol == PROTO_ENTTEC_PRO:
            ser = serial.Serial(c.port, 57600, timeout=1, write_timeout=1)  # baud is irrelevant over USB
        else:
            ser = serial.Serial(c.port, c.baudrate, timeout=1, write_timeout=1)
        return ser

    def _send(self, dmx):
        c = self.cfg
        if c.protocol == PROTO_ENTTEC_PRO:
            self.ser.write(enttec_pro_frame(dmx))
        elif c.protocol == PROTO_OPEN_DMX:
            # Break (>=88us) and mark-after-break (>=8us), then start code + data.
            self.ser.break_condition = True
            time.sleep(0.0001)
            self.ser.break_condition = False
            time.sleep(0.000012)
            self.ser.write(b"\x00" + dmx)
            self.ser.flush()
        else:
            self.ser.write(dmx)

    def run(self):
        c = self.cfg
        period = 1.0 / max(1.0, c.rate_hz)
        channels = max(1, min(512, c.channels))
        while not self._stop.is_set():
            if self.ser is None:
                try:
                    self.ser = self._open()
                    self.status = "running"
                    self.log(f"Opened {c.port} ({c.protocol}) for universe {c.universe}")
                except (serial.SerialException, OSError, ValueError) as e:
                    self.status = f"error: {e}"
                    self.log(f"Cannot open {c.port}: {e}; retrying")
                    self._stop.wait(2.0)
                    continue
            start = time.monotonic()
            frame = self.store.get(c.universe) or bytes(512)
            try:
                self._send(frame[:channels])
                self.frames_sent += 1
            except (serial.SerialException, OSError) as e:
                self.status = f"error: {e}"
                self.log(f"Write to {c.port} failed: {e}; reopening")
                self._close()
                self._stop.wait(1.0)
                continue
            self._stop.wait(max(0.0, period - (time.monotonic() - start)))
        self._close()
        self.status = "stopped"

    def _close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
