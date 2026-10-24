# Art-Net to USB

A small desktop tool (Python + Tkinter) that receives Art-Net DMX on the network interface you choose and sends it out through any USB-to-serial adapter.

## Features

- **Choose the network interface** to listen on, or listen on all of them. On macOS it uses `IP_BOUND_IF`, so broadcast Art-Net still arrives. On Linux it uses `SO_BINDTODEVICE`, or filters by subnet if it lacks the permission. On Windows it binds to the interface's IP.
- **Several outputs**, each sending one Art-Net universe (15-bit port address, 0–32767) to one serial port.
- **Output protocols**
  - *Enttec DMX USB Pro*: the Pro's packet framing (label 6). Use it for Enttec Pro and compatible devices (DMXKing ultraDMX, etc.).
  - *DMX512 (break + 250k 8N2)*: a plain DMX line for FTDI/RS-485 adapters such as Enttec Open DMX. The tool sends a break, then the start code and the channel data.
  - *Raw bytes*: only the channel data, at a baud rate you set. Use it for Arduino or other custom receivers.
- Answers **ArtPoll** requests, so consoles can find the node by themselves.
- Live **DMX monitor** (hover over a cell to see a channel's value), per-output packet and frame rates, and a log.
- Settings are saved to `~/.artnet_to_usb.json`.

## Run

Requires Python 3.9+ with **Tk 8.6 or newer**.

### macOS

Don't use Apple's built-in `/usr/bin/python3` (Python 3.9 from the Command Line Tools): it ships the deprecated Tk 8.5, and the window opens **empty**, with `DEPRECATION WARNING: The system version of Tk is deprecated` in the terminal. Install Python from [Homebrew](https://brew.sh) instead:

```bash
brew install python@3.13 python-tk@3.13
```

Then, from the project folder:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m artnet_to_usb          # main tool
python -m artnet_to_usb.auto     # auto-forwarder
```

If you already have a `.venv` made with Apple's Python, delete it first (`deactivate; rm -rf .venv`). Creating the new venv on top of the old one keeps `python` pointing at Python 3.9.

### Linux (Debian/Ubuntu)

```bash
sudo apt install python3 python3-venv python3-tk
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m artnet_to_usb
```

### Windows (PowerShell)

Install Python from [python.org](https://www.python.org/downloads/) (it includes Tk), then:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m artnet_to_usb
```

### Check the setup

With the venv active, this should print a Tk version of 8.6 or newer:

```bash
python -c "import sys, tkinter; print(sys.version.split()[0], 'Tk', tkinter.TkVersion)"
```

Later, only activating the venv and the `python -m …` line are needed. Run `deactivate` to leave the venv.

Then pick an interface, click **Add…** to map a universe to a serial port, and click **Start**.

## Auto-forwarder

`artnet_to_usb.auto` needs no setup. It listens for Art-Net on `127.0.0.1` and forwards one universe to the first USB-to-serial adapter it finds. Its small window shows:

- a green light that blinks while Art-Net arrives on the forwarded universe (grey when nothing arrives),
- the name and port of the USB device in use,
- a **Refresh** button that rescans the USB ports, for an adapter plugged in after the tool started.

It also checks every second whether the adapter is still connected and picks up a newly connected one by itself, so Refresh is a manual fallback.

```bash
.venv/bin/python -m artnet_to_usb.auto              # universe 0, protocol auto-detected
.venv/bin/python -m artnet_to_usb.auto -u 1 -p dmx512 -r 30
.venv/bin/python -m artnet_to_usb.auto --headless   # terminal only, no window
```

Only ports that report a USB vendor ID count as adapters, so Bluetooth and built-in ports are skipped. With `--protocol auto` (the default), adapters that identify as Enttec Pro / DMXking use the Enttec Pro framing; everything else gets DMX512. Run `--help` for all options (`--universe`, `--protocol`, `--baud`, `--rate`, `--channels`, `--ip`, `--headless`). To quit, close the window or press Ctrl+C.

## Notes

- When you choose an interface, only packets whose sender is on that interface's subnet are accepted. Senders reached through a router on another subnet are ignored.

- Art-Net uses UDP port 6454. Another program that holds this port exclusively will stop this tool from listening.
- The DMX512 mode times the break in software, so the timing depends on the OS and the adapter driver. Use an Enttec Pro–style interface if you need exact DMX timing.
- If two sources send the same universe, the most recent packet wins (no HTP/LTP merging).
