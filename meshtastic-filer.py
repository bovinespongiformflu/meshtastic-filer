#!/usr/bin/env python3
"""
meshtastic-filer.py – Lightweight file‑transfer helper 
-------------------------------------------------------------------------------
This script lets you **push** or **pull** arbitrary files through an existing
Meshtastic network link that you have already set up with `meshtastic‑commander`.
It intentionally mirrors the packet structure and helper utilities already used
inside the commander project, so you can drop it into the same working folder
and reuse the existing `config.py` (for mesh node IP / channel choices) if you
like.

### Packet format (all ASCII so it fits inside Meshtastic text packets)
Each packet is a single line in the form:

    <verb>|<filename>|<total>|<index>|<data>

* **verb** – `RTS`, `DATA`, `NAK`, or `ACK`.
* **filename** – base filename (no path) so both sides know which transfer the
  packet belongs to.
* **total** – integer count of chunks in this transfer (only meaningful in `RTS`
  and repeated for convenience in `DATA`).
* **index** – 0‑based chunk number (`-1` for `RTS`).
* **data** – present only in `DATA` packets – one chunk of the original file
  already Base64‑encoded.

All other metadata (channel, port number, etc.) comes from commander’s globals
(`CHANNEL_SLOT`, `CHUNK_SIZE`, …).

### Typical usage
Send a file:
    python meshtastic-filer.py send /path/to/brochure.pdf

Receive files (blocks until interrupted):
    python meshtastic-filer.py recv

You can run **multiple receivers**; only the one that replies with an `ACK` will
trigger the sender to clean up its state.
"""

import argparse
import base64
import logging
import os
import sys
import time
import threading
from pathlib import Path
from typing import Dict, List

from pubsub import pub  # Meshtastic-python requirement
from meshtastic.tcp_interface import TCPInterface
from meshtastic import portnums_pb2

TEXT_PORT_VALUES = {portnums_pb2.TEXT_MESSAGE_APP, "TEXT_MESSAGE_APP"}

# ───────── CONFIGURABLE CONSTANTS ─────────────────────────────────────────
MESH_NODE_IP = "192.168.0.127"  # default TCP host
CHANNEL_SLOT = 0                 # channel index 0-7
CHUNK_SIZE = 180                 # ≤228 bytes recommended
RTS_INTERVAL_SEC = 10            # resend RTS until CTS
TX_DELAY_SEC = 1.00              # inter-DATA delay
NAK_INTERVAL_SEC = 20             # gap check interval
MAX_CTRL_DATA_LEN = 180         #max NAK size
# -------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ───────── MeshSession helper ─────────────────────────────────────────────
class MeshSession:
    """Light wrapper around TCPInterface plus helpers for control packets."""

    def __init__(self, host: str, channel: int):
        self.iface = TCPInterface(hostname=host, portNumber=4403, connectNow=True)
        self.ch = channel

    def send_text(self, txt: str):
        self.iface.sendText(txt, channelIndex=self.ch)

    def send_ctrl(self, verb: str, fname: str, total: int, idx: int = -1, data: str = ""):
        self.send_text(f"{verb}|{fname}|{total}|{idx}|{data}")

    @staticmethod
    def parse(pkt_text: str):
        parts = pkt_text.split("|", 4) + [""] * 5
        return parts[:5]

    @staticmethod
    def extract_text(pkt: dict) -> str:
        try:
            dec = pkt.get("decoded", {})
            if dec.get("portnum") not in TEXT_PORT_VALUES:
                return ""
            txt = dec.get("text")
            if txt is not None:
                return txt
            hex_payload = dec.get("payload", "")
            return bytes.fromhex(hex_payload).decode("utf-8", "ignore")
        except Exception:
            return ""

# ───────── Sender ----------------------------------------------------------
class Sender:
    def __init__(self, sess: MeshSession, src: Path):
        self.sess = sess
        self.fname = src.name
        self.data_b64 = base64.b64encode(src.read_bytes()).decode()
        self.chunks: List[str] = [
            self.data_b64[i : i + CHUNK_SIZE] for i in range(0, len(self.data_b64), CHUNK_SIZE)
        ]
        self.total = len(self.chunks)
        self.cts_event = threading.Event()
        pub.subscribe(self._on_rx, "meshtastic.receive")

    def _on_rx(self, packet=None, interface=None):
        dec = (packet or {}).get("decoded", {})
        if dec.get("channelIndex", 0) != self.sess.ch:
            return
        text = self.sess.extract_text(packet or {})
        if not text:
            return
        verb, fname, _tot, _idx, data = self.sess.parse(text)
        if fname != self.fname:
            return
        if verb == "CTS":
            self.cts_event.set()
        elif verb == "NAK":
            for m in (int(i) for i in data.split(",") if i):
                if 0 <= m < self.total:
                    self._send_chunk(m)
        elif verb == "ACK":
            logging.info("Transfer complete – receiver saved file.")
            os._exit(0)

    def _send_chunk(self, idx: int):
        self.sess.send_ctrl("DATA", self.fname, self.total, idx, self.chunks[idx])
        time.sleep(TX_DELAY_SEC)

    def run(self):
        logging.info("Offering %s (%d chunks) – waiting for CTS", self.fname, self.total)
        try:
            while not self.cts_event.is_set():
                self.sess.send_ctrl("RTS", self.fname, self.total)
                if self.cts_event.wait(RTS_INTERVAL_SEC):
                    break
            if not self.cts_event.is_set():
                logging.warning("No CTS – aborting.")
                return
            logging.info("CTS received – streaming DATA")
            for i in range(self.total):
                self._send_chunk(i)
            logging.info("All DATA sent – awaiting ACK/NAK…")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logging.info("Sender interrupted.")

# ───────── Receiver --------------------------------------------------------
class Receiver:
    class Transfer:
        def __init__(self, total: int):
            self.total = total
            self.chunks: Dict[int, str] = {}
            self.last_rx = time.time()
            self.cts_sent = False

    def __init__(self, sess: MeshSession, out_dir: Path):
        self.sess = sess
        self.out_dir = out_dir
        self.transfers: Dict[str, Receiver.Transfer] = {}
        pub.subscribe(self._on_rx, "meshtastic.receive")
        threading.Thread(target=self._gap_check, daemon=True).start()

    def _on_rx(self, packet=None, interface=None):
        dec = (packet or {}).get("decoded", {})
        if dec.get("channelIndex", 0) != self.sess.ch:
            return
        txt = self.sess.extract_text(packet or {})
        if not txt:
            return
        verb, fname, tot_str, idx_str, data = self.sess.parse(txt)
        total = int(tot_str) if tot_str.isdigit() else 0
        idx = int(idx_str)
        if verb == "RTS":
            tf = self.transfers.setdefault(fname, Receiver.Transfer(total))
            if not tf.cts_sent:
                logging.info("Incoming %s (%d chunks) – sending CTS", fname, total)
                self.sess.send_ctrl("CTS", fname, total)
                tf.cts_sent = True
            return
        if fname not in self.transfers:
            return
        tf = self.transfers[fname]
        tf.last_rx = time.time()
        if verb == "DATA" and 0 <= idx < tf.total:
            tf.chunks[idx] = data
            if len(tf.chunks) == tf.total:
                self._assemble(fname)

    def _gap_check(self):
        while True:
            time.sleep(1)
            now = time.time()
            for fname, tf in list(self.transfers.items()):
                if len(tf.chunks) == tf.total:
                    continue
                if now - tf.last_rx >= NAK_INTERVAL_SEC:
                    missing = [str(i) for i in range(tf.total) if i not in tf.chunks]
                    for payload in self._chunk_nak(missing):
                        self.sess.send_ctrl("NAK", fname, tf.total, -1, payload)
                    if missing:
                        logging.info("NAK %s – %d chunks", fname, len(missing))
                    tf.last_rx = now
    
    def _chunk_nak(missing_list):
        """Yield comma-joined chunks whose UTF-8 length ≤ MAX_CTRL_DATA_LEN."""
        batch = []
        length = 0
        for idx in missing_list:
            part_len = len(idx) + (1 if batch else 0)   # +1 for comma
            if length + part_len > MAX_CTRL_DATA_LEN:
                yield ",".join(batch)
                batch, length = [], 0
            batch.append(idx)
            length += part_len
        if batch:
            yield ",".join(batch)

    def _assemble(self, fname: str):
        tf = self.transfers[fname]
        payload = "".join(tf.chunks[i] for i in range(tf.total))
        data = base64.b64decode(payload)
        path = self.out_dir / fname
        path.write_bytes(data)
        logging.info("Saved %s (%d bytes)", fname, len(data))
        self.sess.send_ctrl("ACK", fname, tf.total)
        del self.transfers[fname]

    def run(self):
        logging.info("Receiver ready – waiting for files")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logging.info("Receiver stopped.")

# ───────── CLI -------------------------------------------------------------
def main():
    global CHUNK_SIZE

    parser = argparse.ArgumentParser(description="Send or receive files over Meshtastic")
    sub = parser.add_subparsers(dest="mode", required=True)

    send_p = sub.add_parser("send", help="Send a file")
    send_p.add_argument("path", type=Path, help="File to send")

    recv_p = sub.add_parser("recv", help="Receive files")

    for cmd in (send_p, recv_p):
        cmd.add_argument("--host", default=MESH_NODE_IP, help="meshtastic-daemon host")
        cmd.add_argument("--channel", type=int, default=CHANNEL_SLOT, help="Channel index 0-7")
        cmd.add_argument("--chunk", type=int, default=CHUNK_SIZE, help="Bytes per DATA chunk")

    args = parser.parse_args()
    CHUNK_SIZE = args.chunk

    session = MeshSession(args.host, args.channel)

    if args.mode == "send":
        if not args.path.exists():
            sys.exit(f"File not found: {args.path}")
        Sender(session, args.path).run()
    else:
        Receiver(session, Path.cwd()).run()

if __name__ == "__main__":
    main()