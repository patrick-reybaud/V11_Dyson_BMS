#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Patrick Reybaud
"""Minimal GDB Remote Serial Protocol client for a Black Magic Probe.

arm-none-eabi-gdb 13 asserts (inferior_thread) right after attaching to a
SAMD20 whose security bit is set, so `monitor erase_mass` is never reached.
This sends the same packets directly, with nothing but the standard library.

    python3 -I tools/bmp_rsp.py /dev/cu.usbmodemXXXX1 \
        mon:swdp_scan attach:1 mon:erase_mass detach

Use the probe's GDB port (the first of its two serial ports). Once the chip
is unprotected, program it with GDB as usual - see doc/v15-sv22/README.md.
"""
import os
import sys
import termios
import time


def open_port(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0                                   # iflag
    attrs[1] = 0                                   # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0                                   # lflag: raw
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1                    # 100 ms read timeout
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def send(fd, payload):
    data = payload.encode()
    pkt = b"$" + data + b"#" + b"%02x" % (sum(data) & 0xFF)
    os.write(fd, pkt)


def recv_packet(fd, timeout):
    """Return the next packet payload, acknowledging it; None on timeout."""
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = os.read(fd, 4096)
        if not chunk:
            continue
        buf += chunk
        start = buf.find(b"$")
        if start < 0:
            buf = b""
            continue
        end = buf.find(b"#", start)
        if end >= 0 and len(buf) >= end + 3:
            os.write(fd, b"+")
            return buf[start + 1:end].decode(errors="replace")
    return None


def command(fd, payload, timeout=60):
    """Send a packet and collect replies until a final one. Console output is printed."""
    send(fd, payload)
    while True:
        reply = recv_packet(fd, timeout)
        if reply is None:
            print("!! timeout waiting for reply to %s" % payload.split(",")[0])
            return None
        if reply.startswith("O") and reply != "OK":
            sys.stdout.write(bytes.fromhex(reply[1:]).decode(errors="replace"))
            sys.stdout.flush()
            continue
        return reply


def main():
    port, steps = sys.argv[1], sys.argv[2:]
    fd = open_port(port)
    try:
        os.write(fd, b"+")
        command(fd, "qSupported:multiprocess+", timeout=5)
        command(fd, "!", timeout=5)               # extended-remote mode
        for step in steps:
            kind, _, arg = step.partition(":")
            if kind == "mon":
                print(">>> monitor %s" % arg)
                reply = command(fd, "qRcmd," + arg.encode().hex())
            elif kind == "attach":
                print(">>> attach %s" % arg)
                reply = command(fd, "vAttach;%x" % int(arg))
            elif kind == "detach":
                print(">>> detach")
                reply = command(fd, "D")
            else:
                sys.exit("unknown step %s" % step)
            print("<<< %s" % reply)
            if reply is None or reply.startswith("E"):
                sys.exit(1)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
