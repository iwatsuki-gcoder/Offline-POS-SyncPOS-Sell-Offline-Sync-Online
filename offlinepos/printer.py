"""Receipt printing for SwiftBill.

Two backends, chosen by env config:
- ``file`` (default): writes a 42-column text receipt to
  ``OFFLINEPOS_RECEIPT_DIR`` (default ``./data/receipts``). Works
  everywhere, no hardware needed — and it doubles as an audit trail.
- ``network``: sends an ESC/POS byte stream to a thermal printer at
  ``OFFLINEPOS_PRINTER_HOST:OFFLINEPOS_PRINTER_PORT`` (default 9100).
  If the printer is unreachable it falls back to a file and says so.

ESC/POS subset used: init, centered/bold text, full cut. Encoded as
CP437 with replacement so accented names don't crash the printer.
"""
from __future__ import annotations

import datetime
import os
import socket
from pathlib import Path

WIDTH = 42

# ESC/POS commands
INIT = b"\x1b@"
ALIGN_LEFT = b"\x1ba\x00"
ALIGN_CENTER = b"\x1ba\x01"
BOLD_ON = b"\x1bE\x01"
BOLD_OFF = b"\x1bE\x00"
CUT = b"\x1dV\x00"
LF = b"\n"


def _tax_label(sale: dict) -> str:
    """'TAX (18%)' when every line shares one rate, else plain 'TAX'."""
    rates = {line[3] for line in sale["items"] if len(line) > 3 and line[3]}
    if len(rates) == 1:
        return f"TAX ({next(iter(rates)):g}%)"
    return "TAX"


def format_text_receipt(sale: dict, terminal_id: str) -> str:
    """42-column plain-text receipt from a checkout ``sale`` dict."""
    subtotal = sale.get("subtotal", sale["total"])
    tax_total = sale.get("tax_total", 0.0)
    lines = ["SwiftBill".center(WIDTH), terminal_id.center(WIDTH),
             datetime.datetime.now().strftime("%Y-%m-%d %H:%M").center(WIDTH),
             "-" * WIDTH]
    for line in sale["items"]:
        pid, qty, price = line[0], line[1], line[2]
        left = f"{pid} x{qty}"
        right = f"${qty * price:.2f}"
        lines.append(f"{left:<{WIDTH - len(right)}}{right}")
        lines.append(f"  @ ${price:.2f}")
    lines += ["-" * WIDTH,
              f"{'SUBTOTAL':<{WIDTH - 8}}${subtotal:.2f}",
              f"{_tax_label(sale):<{WIDTH - 8}}${tax_total:.2f}",
              f"{'TOTAL':<{WIDTH - 8}}${sale['total']:.2f}",
              f"txn {sale['txn_id'][:8]}",
              "Stored locally - syncs when online".center(WIDTH)]
    return "\n".join(lines) + "\n"


def _escpos_bytes(sale: dict, terminal_id: str) -> bytes:
    subtotal = sale.get("subtotal", sale["total"])
    tax_total = sale.get("tax_total", 0.0)
    out = bytearray(INIT)
    out += ALIGN_CENTER + BOLD_ON
    out += "SwiftBill".encode("cp437", "replace") + LF
    out += BOLD_OFF + terminal_id.encode("cp437", "replace") + LF
    out += ALIGN_LEFT
    for line in sale["items"]:
        pid, qty, price = line[0], line[1], line[2]
        left = f"{pid} x{qty}"
        right = f"${qty * price:.2f}"
        out += f"{left:<{WIDTH - len(right)}}{right}\n".encode("cp437", "replace")
    out += ALIGN_CENTER + BOLD_ON
    out += f"SUBTOTAL ${subtotal:.2f}\n".encode("cp437", "replace")
    out += f"{_tax_label(sale)} ${tax_total:.2f}\n".encode("cp437", "replace")
    out += f"TOTAL ${sale['total']:.2f}\n".encode("cp437", "replace")
    out += BOLD_OFF + LF + LF + CUT
    return bytes(out)


class ReceiptPrinter:
    def __init__(self, mode: str | None = None,
                 receipt_dir: str | Path | None = None,
                 host: str | None = None, port: int | None = None,
                 timeout: float = 3.0):
        self.mode = mode or os.environ.get("OFFLINEPOS_PRINTER", "file")
        self.receipt_dir = Path(receipt_dir or os.environ.get(
            "OFFLINEPOS_RECEIPT_DIR", "./data/receipts"))
        self.host = host or os.environ.get("OFFLINEPOS_PRINTER_HOST")
        self.port = port or int(os.environ.get("OFFLINEPOS_PRINTER_PORT", "9100"))
        self.timeout = timeout

    def print_receipt(self, sale: dict, terminal_id: str) -> dict:
        """Print (or save) a receipt. Never raises for printer outages."""
        self.receipt_dir.mkdir(parents=True, exist_ok=True)
        fname = f"receipt_{sale['txn_id'][:8]}_{terminal_id}.txt"
        text_path = self.receipt_dir / fname

        if self.mode == "network" and self.host:
            try:
                with socket.create_connection((self.host, self.port),
                                              timeout=self.timeout) as s:
                    s.sendall(_escpos_bytes(sale, terminal_id))
                # still keep the text copy for the audit trail
                text_path.write_text(format_text_receipt(sale, terminal_id))
                return {"ok": True, "via": f"network printer {self.host}:{self.port}",
                        "path": str(text_path)}
            except OSError as e:
                text_path.write_text(format_text_receipt(sale, terminal_id))
                return {"ok": True, "via": "file (printer unreachable)",
                        "path": str(text_path), "warning": str(e)}

        text_path.write_text(format_text_receipt(sale, terminal_id))
        return {"ok": True, "via": "file", "path": str(text_path)}
