"""Product catalog helpers.

`catalog/products.csv` is the source of truth for the product database:
id, EAN-13 barcode, name, price, stock, tax_rate (percent). The app seeds
both the terminal and central databases from it on first run, so every
copy agrees on barcodes and tax rates.
"""
from __future__ import annotations

import csv
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parents[1] / "catalog" / "products.csv"


def ean13_check_digit(first12: str) -> str:
    """Compute the EAN-13 check digit for 12 digits."""
    if len(first12) != 12 or not first12.isdigit():
        raise ValueError("need exactly 12 digits")
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(first12))
    return str((10 - total % 10) % 10)


def ean13_is_valid(code: str) -> bool:
    return (len(code) == 13 and code.isdigit()
            and ean13_check_digit(code[:12]) == code[12])


def load_catalog(path: str | Path = CATALOG_PATH) -> list[dict]:
    """Load the CSV -> [{product_id, barcode, name, price, stock, tax_rate}]."""
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append({"product_id": row["product_id"].strip(),
                        "barcode": row["barcode"].strip(),
                        "name": row["name"].strip(),
                        "price": float(row["price"]),
                        "stock": int(row["stock"]),
                        "tax_rate": float(row.get("tax_rate") or 0)})
    return out


def catalog_tuples(path: str | Path = CATALOG_PATH) -> list[tuple]:
    """(product_id, name, price, stock, barcode, tax_rate) tuples for seed_catalog."""
    return [(c["product_id"], c["name"], c["price"], c["stock"], c["barcode"],
             c["tax_rate"])
            for c in load_catalog(path)]
