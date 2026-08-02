#!/usr/bin/env python3
"""SkyPortal WiFi bridge for Celestron NexStar telescopes.

Emulates the Celestron WiFi module: SkyPortal connects to 1.2.3.4:2000 and
speaks the AUX protocol; this bridge encapsulates the AUX commands addressed
to the motor controllers inside the hand controller's 'P' (passthrough)
command over the USB serial link.

AUX packets have the format: 0x3B len src dst cmd [data...] checksum
where len = 3 + n_data and checksum is the two's complement of the sum
of len..data.

Configuration: /etc/celestron-bridge.conf (KEY=value lines), overridable
with the CELESTRON_CONF environment variable.  Recognized keys:
SERIAL_PORT (or "auto"), SERIAL_BAUD, INDI_DEVICE, LOG_LEVEL, LOG_FILE.
"""

import asyncio
import glob
import logging
import os
import subprocess
import time

import serial

TCP_PORT = 2000  # fixed: SkyPortal always connects to 1.2.3.4:2000

CONFIG_FILE = os.environ.get("CELESTRON_CONF", "/etc/celestron-bridge.conf")

DEFAULTS = {
    "SERIAL_PORT": "auto",
    "SERIAL_BAUD": "9600",
    "INDI_DEVICE": "Celestron GPS",
    "LOG_LEVEL": "INFO",
    "LOG_FILE": "",
}

DEV_AZM = 0x10
DEV_ALT = 0x11
DEV_WIFI = 0xB5          # the "WiFi module": we answer in its place
WIFI_VERSION = (1, 0)

# Expected response length for the MC commands used by SkyPortal
# (the hand controller needs it to know how many bytes to read
# from the AUX bus).
RESP_LEN = {
    0x01: 3,  # MC_GET_POSITION
    0x02: 0,  # MC_GOTO_FAST
    0x04: 0,  # MC_SET_POSITION
    0x05: 2,  # MC_GET_MODEL
    0x06: 0,  # MC_SET_POS_GUIDERATE
    0x07: 0,  # MC_SET_NEG_GUIDERATE
    0x0B: 0,  # MC_LEVEL_START
    0x10: 0,  # MC_SET_POS_BACKLASH
    0x11: 0,  # MC_SET_NEG_BACKLASH
    0x12: 1,  # MC_LEVEL_DONE
    0x13: 1,  # MC_SLEW_DONE
    0x17: 0,  # MC_GOTO_SLOW
    0x18: 1,  # MC_AT_INDEX
    0x19: 0,  # MC_SEEK_INDEX
    0x24: 0,  # MC_MOVE_POS
    0x25: 0,  # MC_MOVE_NEG
    0x38: 0,  # MC_ENABLE_CORDWRAP
    0x39: 0,  # MC_DISABLE_CORDWRAP
    0x3A: 0,  # MC_SET_CORDWRAP_POS
    0x3B: 1,  # MC_POLL_CORDWRAP
    0x3C: 3,  # MC_GET_CORDWRAP_POS
    0x40: 1,  # MC_GET_POS_BACKLASH
    0x41: 1,  # MC_GET_NEG_BACKLASH
    0x46: 0,  # MC_SET_AUTOGUIDE_RATE
    0x47: 1,  # MC_GET_AUTOGUIDE_RATE
    0xFE: 2,  # GET_VER
}

log = logging.getLogger("bridge")


def load_config(path=CONFIG_FILE):
    """Parse a simple KEY=value file; a missing file yields the defaults."""
    conf = dict(DEFAULTS)
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


def find_serial_port(configured):
    """Return the serial port path, auto-detecting a USB adapter if asked."""
    if configured and configured.lower() != "auto":
        return configured
    candidates = sorted(glob.glob("/dev/serial/by-id/*"))
    if not candidates:
        raise IOError("no USB serial adapter in /dev/serial/by-id/ "
                      "(set SERIAL_PORT in the config file)")
    if len(candidates) > 1:
        log.warning("multiple serial adapters found, using %s", candidates[0])
    return candidates[0]


def aux_frame(src, dst, cmd, data=b""):
    body = bytes([3 + len(data), src, dst, cmd]) + data
    return b";" + body + bytes([(-sum(body)) & 0xFF])


class HandControl:
    """Exclusive access to the hand controller, with the 'P' tunnel for
    AUX commands."""

    def __init__(self, port_setting, baud, indi_device):
        self.port_setting = port_setting
        self.baud = baud
        self.indi_device = indi_device
        self.ser = None
        self.lock = asyncio.Lock()

    def open(self):
        if self.ser and self.ser.is_open:
            return
        # free the port if the INDI driver is holding it
        subprocess.run(["indi_setprop",
                        f"{self.indi_device}.CONNECTION.DISCONNECT=On"],
                       capture_output=True, timeout=10)
        port = find_serial_port(self.port_setting)
        last_error = None
        for _ in range(15):
            try:
                self.ser = serial.Serial(port, self.baud, timeout=1.0)
                break
            except serial.SerialException as exc:
                last_error = exc
                time.sleep(1)
        else:
            raise IOError(f"serial port busy: {last_error}")
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.write(b"Kx")
        if self.ser.read(2) != b"x#":
            log.warning("echo test failed, continuing anyway")
        log.info("hand controller ready on %s", port)

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            finally:
                self.ser = None

    def tunnel(self, dst, cmd, data, resp_len):
        """Run an AUX command through the hand controller's P command."""
        pkt = (bytes([ord("P"), 1 + len(data), dst, cmd])
               + (data + b"\x00\x00\x00")[:3] + bytes([resp_len]))
        self.ser.reset_input_buffer()
        self.ser.write(pkt)
        raw = self.ser.read(resp_len + 1)
        if not raw.endswith(b"#"):
            raise TimeoutError(f"no response from device {dst:02x}")
        return raw[:-1]


class AuxParser:
    """Reassembles AUX packets from the TCP stream."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf.extend(data)
        frames = []
        while True:
            start = self.buf.find(b";")
            if start < 0:
                self.buf.clear()
                break
            if start:
                del self.buf[:start]
            if len(self.buf) < 2:
                break
            length = self.buf[1]
            total = 2 + length + 1
            if not 3 <= length <= 40:
                del self.buf[0]           # absurd length: resynchronize
                continue
            if len(self.buf) < total:
                break
            frame = bytes(self.buf[:total])
            del self.buf[:total]
            if sum(frame[1:]) & 0xFF:
                log.warning("bad checksum, dropping: %s", frame.hex(" "))
                continue
            frames.append(frame)
        return frames


async def handle_frame(hc, frame):
    """Return the response packet (or None)."""
    src, dst, cmd = frame[2], frame[3], frame[4]
    data = frame[5:-1]

    if dst == DEV_WIFI:
        payload = bytes(WIFI_VERSION) if cmd == 0xFE else b""
        return aux_frame(dst, src, cmd, payload)

    if dst in (DEV_AZM, DEV_ALT):
        resp_len = RESP_LEN.get(cmd, 0)
        if len(data) > 3:
            log.warning("cmd %02x with %d data bytes: cannot encapsulate",
                        cmd, len(data))
            return None
        async with hc.lock:
            loop = asyncio.get_running_loop()
            try:
                payload = await loop.run_in_executor(
                    None, hc.tunnel, dst, cmd, data, resp_len)
            except TimeoutError as exc:
                log.warning("timeout %s (cmd %02x)", exc, cmd)
                return None
        return aux_frame(dst, src, cmd, payload)

    log.debug("device %02x not handled (cmd %02x): staying silent", dst, cmd)
    return None


async def handle_client(hc, reader, writer):
    peer = writer.get_extra_info("peername")
    log.info("client connected: %s", peer)
    parser = AuxParser()
    try:
        hc.open()
        while True:
            data = await reader.read(256)
            if not data:
                break
            for frame in parser.feed(data):
                resp = await handle_frame(hc, frame)
                if resp:
                    writer.write(resp)
                    await writer.drain()
    except Exception as exc:
        log.error("client %s: %s", peer, exc)
    finally:
        log.info("client disconnected: %s", peer)
        writer.close()
        # stop the motors for safety when the app disconnects
        try:
            async with hc.lock:
                for dev in (DEV_AZM, DEV_ALT):
                    hc.tunnel(dev, 0x24, b"\x00", 0)   # MOVE_POS rate 0
        except Exception:
            pass


async def main():
    conf = load_config()
    logging.basicConfig(
        level=getattr(logging, conf["LOG_LEVEL"].upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        filename=conf["LOG_FILE"] or None)
    hc = HandControl(conf["SERIAL_PORT"], int(conf["SERIAL_BAUD"]),
                     conf["INDI_DEVICE"])
    server = await asyncio.start_server(
        lambda r, w: handle_client(hc, r, w), "0.0.0.0", TCP_PORT)
    log.info("SkyPortal bridge listening on port %d", TCP_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
