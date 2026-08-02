#!/usr/bin/env python3
"""Celestron NexStar telescope control through an INDI server.

Works both on the Raspberry Pi that runs the INDI server and from a remote
PC (only the network is needed: the serial port stays with the INDI server,
so there is no conflict with KStars/Stellarium).

Requires:  pip install indipyclient

Configuration (optional): /etc/celestron-bridge.conf, overridable with the
CELESTRON_CONF environment variable.  Recognized keys: INDI_HOST, INDI_PORT,
INDI_DEVICE.  Command-line arguments take precedence.

Library usage:
    from celestron_indi import CelestronINDI
    with CelestronINDI() as scope:            # default host: from config
        print(scope.get_radec())              # (hours, degrees)
        scope.set_rate(3)                     # manual slew speed
        scope.move("e")                       # east; "n"/"s"/"w" likewise
        scope.stop()                          # stop everything
        scope.goto(5.6, -5.4)                 # GoTo RA 5.6h, DEC -5.4°
        scope.sync(5.6, -5.4)                 # sync on a known position

Command-line usage:
    python celestron_indi.py                  # status and position
    python celestron_indi.py goto 5.6 -5.4 [--wait]
    python celestron_indi.py sync 5.6 -5.4
    python celestron_indi.py move e
    python celestron_indi.py stop
    python celestron_indi.py --host raspberrypi.local status
"""

import argparse
import os
import queue
import threading
import time

from indipyclient.queclient import runqueclient

CONFIG_FILE = os.environ.get("CELESTRON_CONF", "/etc/celestron-bridge.conf")


def load_config(path=CONFIG_FILE):
    """Parse a simple KEY=value file; a missing file yields the defaults."""
    conf = {"INDI_HOST": "localhost", "INDI_PORT": "7624",
            "INDI_DEVICE": "Celestron GPS"}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    conf[key.strip()] = value.strip()
    except OSError:
        pass
    return conf


_CONF = load_config()
DEFAULT_HOST = _CONF["INDI_HOST"]
DEFAULT_PORT = int(_CONF["INDI_PORT"])
DEVICE = _CONF["INDI_DEVICE"]

_DIRECTIONS = {
    "n": ("TELESCOPE_MOTION_NS", "MOTION_NORTH"),
    "s": ("TELESCOPE_MOTION_NS", "MOTION_SOUTH"),
    "w": ("TELESCOPE_MOTION_WE", "MOTION_WEST"),
    "e": ("TELESCOPE_MOTION_WE", "MOTION_EAST"),
}


class CelestronINDI:
    """Synchronous INDI client for the telescope.

    Every event received from the server carries a full snapshot of the
    client state: a reader thread keeps the latest one cached, so reads
    (position, status, rates) are instantaneous with no round-trip, and
    waits (connection, end of slew) react to events instead of polling
    the server."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 device=DEVICE, timeout=15):
        self.device = device
        self._tx = queue.Queue()
        self._rx = queue.Queue()
        self._snap = None
        self._closing = False
        self._cond = threading.Condition()
        self._client = threading.Thread(
            target=runqueclient, args=(self._tx, self._rx, host, port),
            daemon=True)
        self._reader = threading.Thread(target=self._read_events, daemon=True)
        self._client.start()
        self._reader.start()
        self._tx.put((None, None, "snapshot"))  # seed the cache
        deadline = time.monotonic() + timeout
        if not self._wait_for(
                lambda snap: self.device in snap
                and "CONNECTION" in snap[self.device],
                timeout):
            self.close()
            raise TimeoutError("INDI server unreachable or device "
                               f"{self.device!r} not present")
        self.connect(timeout=max(1, deadline - time.monotonic()))

    # --- plumbing --------------------------------------------------------

    def _read_events(self):
        """Consume the event queue and keep the state cache up to date."""
        while not self._closing:
            try:
                item = self._rx.get(timeout=0.5)
            except queue.Empty:
                continue
            if item.eventtype == "snapshot" and item.devicename is not None:
                continue  # partial (device/vector) snapshot: not needed
            if item.snapshot is None:
                continue
            with self._cond:
                self._snap = item.snapshot
                self._cond.notify_all()

    def _wait_for(self, predicate, timeout=None):
        """Wait until the cache satisfies predicate(snapshot).  Returns True
        if it happens within timeout (None = wait forever, Ctrl+C works)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self._closing:
                if self._snap is not None and predicate(self._snap):
                    return True
                if deadline is None:
                    self._cond.wait(0.5)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(min(remaining, 0.5))
            return False

    def _send(self, vector, members):
        self._tx.put((self.device, vector, members))

    def _device(self):
        snap = self._snap
        if snap is None or self.device not in snap:
            raise ConnectionError("no data from the INDI server")
        return snap[self.device]

    def _vector(self, name):
        dev = self._device()
        if name not in dev:
            raise KeyError(f"property {name!r} not defined (yet): is the "
                           "telescope connected?")
        return dev[name]

    @staticmethod
    def _number(vector, member):
        try:
            return vector.getfloatvalue(member)
        except AttributeError:
            return float(vector[member])

    def _coord_state(self, snap):
        dev = snap.get(self.device)
        if dev and "EQUATORIAL_EOD_COORD" in dev:
            return dev["EQUATORIAL_EOD_COORD"].state
        return None

    # --- connection ------------------------------------------------------

    def connected(self):
        try:
            return self._vector("CONNECTION")["CONNECT"] == "On"
        except (KeyError, ConnectionError):
            return False

    _REQUIRED = ("EQUATORIAL_EOD_COORD", "TELESCOPE_MOTION_NS",
                 "TELESCOPE_MOTION_WE", "TELESCOPE_SLEW_RATE",
                 "TELESCOPE_ABORT_MOTION")

    def connect(self, timeout=20):
        """Ensure the driver is connected and its properties are ready.

        After a (re)connection the server redefines the vectors a few at a
        time: commands sent before they are active (enable=False) would be
        discarded, so wait until they are all present."""
        if not self.connected():
            self._send("CONNECTION", {"CONNECT": "On"})

        def ready(snap):
            if self.device not in snap:
                return False
            dev = snap[self.device]
            return all(n in dev and getattr(dev[n], "enable", True)
                       for n in self._REQUIRED)

        if not self._wait_for(ready, timeout):
            raise TimeoutError("the driver does not connect to the telescope")

    def disconnect(self):
        """Detach the driver from the serial port (frees it for other uses).

        Note: DISCONNECT=On is required; setting CONNECT=Off alone is
        ignored by the server (one-of-many switch)."""
        self._send("CONNECTION", {"DISCONNECT": "On"})

    def close(self):
        self._closing = True
        self._tx.put(None)
        with self._cond:
            self._cond.notify_all()   # release any pending _wait_for
        self._client.join(timeout=5)
        self._reader.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.close()
        except Exception:
            pass

    # --- state reads -----------------------------------------------------

    def get_radec(self):
        """(RA in hours, DEC in degrees)."""
        v = self._vector("EQUATORIAL_EOD_COORD")
        return self._number(v, "RA"), self._number(v, "DEC")

    def slewing(self):
        """True while a GoTo is in progress."""
        return self._vector("EQUATORIAL_EOD_COORD").state == "Busy"

    def rates(self):
        """Names of the available slew rates, slowest to fastest."""
        return list(self._vector("TELESCOPE_SLEW_RATE").keys())

    def status(self):
        dev = self._device()  # one consistent read of the cache
        coord = dev["EQUATORIAL_EOD_COORD"]
        ra, dec = self._number(coord, "RA"), self._number(coord, "DEC")
        connected = ("CONNECTION" in dev
                     and dev["CONNECTION"]["CONNECT"] == "On")
        lines = [f"device      : {self.device}",
                 f"connected   : {connected}",
                 f"RA          : {ra:.4f} h",
                 f"DEC         : {dec:+.4f}°",
                 f"GoTo active : {coord.state == 'Busy'}"]
        if "TELESCOPE_SLEW_RATE" in dev:
            v = dev["TELESCOPE_SLEW_RATE"]
            current = [m for m in v if v[m] == "On"]
            lines.append(f"slew rate   : {current[0] if current else '?'} "
                         f"(available: {', '.join(v)})")
        return "\n".join(lines)

    # --- motion ----------------------------------------------------------

    def set_rate(self, index_or_name):
        """Manual slew rate: index 0..N-1 or a name (see rates())."""
        names = self.rates()
        name = (names[index_or_name] if isinstance(index_or_name, int)
                else index_or_name)
        if name not in names:
            raise ValueError(f"unknown rate {index_or_name!r}, "
                             f"pick one of: {names}")
        self._send("TELESCOPE_SLEW_RATE", {name: "On"})

    def move(self, direction):
        """Move towards 'n', 's', 'e' or 'w' until you call stop()."""
        key = direction.lower()[:1]
        if key not in _DIRECTIONS:
            raise ValueError(f"unknown direction {direction!r}, "
                             "use one of: n, s, e, w")
        vector, member = _DIRECTIONS[key]
        self._send(vector, {member: "On"})

    def stop(self):
        """Stop manual motion and any GoTo."""
        self._send("TELESCOPE_MOTION_NS",
                   {"MOTION_NORTH": "Off", "MOTION_SOUTH": "Off"})
        self._send("TELESCOPE_MOTION_WE",
                   {"MOTION_WEST": "Off", "MOTION_EAST": "Off"})
        self._send("TELESCOPE_ABORT_MOTION", {"ABORT": "On"})

    def track(self, enable=True):
        """Enable/disable sidereal tracking."""
        member = "TRACK_ON" if enable else "TRACK_OFF"
        self._send("TELESCOPE_TRACK_STATE", {member: "On"})

    # --- goto / sync -----------------------------------------------------

    def _coord_action(self, action, ra_hours, dec_deg):
        """Set ON_COORD_SET, wait for the server to confirm it, then send
        the coordinates (both commands travel in order, but this makes
        sure the mode is active before the coordinates arrive)."""
        self._send("ON_COORD_SET", {action: "On"})
        self._wait_for(
            lambda snap: self.device in snap
            and "ON_COORD_SET" in snap[self.device]
            and snap[self.device]["ON_COORD_SET"][action] == "On"
            and snap[self.device]["ON_COORD_SET"].state != "Busy",
            timeout=3)
        self._send("EQUATORIAL_EOD_COORD",
                   {"RA": float(ra_hours), "DEC": float(dec_deg)})

    def goto(self, ra_hours, dec_deg, wait=False):
        """GoTo the given coordinates (RA in hours, DEC in degrees).

        With wait=True, block until the slew is finished."""
        self._coord_action("TRACK", ra_hours, dec_deg)
        if wait:
            # sending immediately produces a "State" event with the vector
            # Busy: wait to see it, then wait for it to leave Busy
            self._wait_for(lambda s: self._coord_state(s) == "Busy",
                           timeout=3)
            self._wait_for(lambda s: self._coord_state(s)
                           not in (None, "Busy"))

    def sync(self, ra_hours, dec_deg):
        """Tell the telescope it is currently pointing at these coordinates."""
        self._coord_action("SYNC", ra_hours, dec_deg)
        self._wait_for(lambda s: self._coord_state(s) not in (None, "Busy"),
                       timeout=3)
        self._send("ON_COORD_SET", {"TRACK": "On"})


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Celestron NexStar control through an INDI server.")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"INDI server host (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"INDI server port (default {DEFAULT_PORT})")
    parser.add_argument("--device", default=DEVICE,
                        help=f"INDI device name (default {DEVICE!r})")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="status and position (default)")
    p = sub.add_parser("goto", help="GoTo the given coordinates")
    p.add_argument("ra", type=float, help="right ascension in hours")
    p.add_argument("dec", type=float, help="declination in degrees")
    p.add_argument("--wait", action="store_true",
                   help="wait until the slew is finished")
    p = sub.add_parser("sync", help="sync on a known position")
    p.add_argument("ra", type=float, help="right ascension in hours")
    p.add_argument("dec", type=float, help="declination in degrees")
    sub.add_parser("stop", help="stop all motion")
    p = sub.add_parser("move", help="continuous manual motion")
    p.add_argument("direction", choices=sorted(_DIRECTIONS),
                   help="direction: n, s, e, w")
    args = parser.parse_args(argv)

    with CelestronINDI(host=args.host, port=args.port,
                       device=args.device) as scope:
        cmd = args.cmd or "status"
        if cmd == "status":
            print(scope.status())
        elif cmd == "goto":
            scope.goto(args.ra, args.dec, wait=args.wait)
            done = "finished at" if args.wait else "started towards"
            print(f"GoTo {done} RA {args.ra}h DEC {args.dec}°")
        elif cmd == "sync":
            scope.sync(args.ra, args.dec)
            print(f"Synced on RA {args.ra}h DEC {args.dec}°")
        elif cmd == "stop":
            scope.stop()
            print("Stopped.")
        elif cmd == "move":
            scope.move(args.direction)
            print(f"Moving '{args.direction}' (use 'stop' to halt)")


if __name__ == "__main__":
    main()
