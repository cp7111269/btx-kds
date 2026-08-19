#!/usr/bin/env python3
"""
BTX KDS - export the AutoCount FnB menu and its kitchen-station routing.

READ-ONLY. Issues nothing but SELECT statements against the AutoCount FnB
frontend book, and answers the question "which dish belongs on which kitchen
screen" the same way AutoCount answers it for its kitchen printers.

Routing precedence (most specific wins, mirroring how AutoCount narrows down
from a blanket rule to a single product):
    1. PosPrinterSetItem       - this exact ItemCode
    2. PosPrinterSetItemGroup  - the item's ItemGroup
    3. PosPrinterSetItemType   - the item's ItemType
An item matched by nothing is unrouted: today its ticket simply never reaches
a kitchen printer, so the KDS must surface it rather than silently drop it.

Output: web/kds/menu.json - consumed by the KDS station UI as realistic mock
data until the Bridge is live, and by the Bridge itself afterwards.

Usage:
    python tools/export-menu.py                       # defaults below
    python tools/export-menu.py --db AED_SHOP_FNB_FE
    python tools/export-menu.py --server MYPC\\SQLEXPRESS --db AED_X_FNB_FE
"""

import argparse
import io
import json
import os
import sys

try:
    import pyodbc
except ImportError:
    sys.exit("pyodbc is required:  pip install pyodbc")

DEFAULT_SERVER = r".\SQL2019"
DEFAULT_DB = "AED_BT_FNB_FE"

# Newest driver first; the older ones stay as fallbacks for customer machines.
DRIVER_PREFERENCE = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
]


def connect(server, db):
    available = set(pyodbc.drivers())
    for drv in DRIVER_PREFERENCE:
        if drv not in available:
            continue
        parts = [
            "DRIVER={%s}" % drv,
            "SERVER=%s" % server,
            "DATABASE=%s" % db,
            "Trusted_Connection=yes",
        ]
        # Driver 18 defaults to Encrypt=yes and then rejects the self-signed
        # certificate a local SQL Server instance presents.
        if "18" in drv or "17" in drv:
            parts.append("TrustServerCertificate=yes")
        try:
            return pyodbc.connect(";".join(parts), timeout=8), drv
        except pyodbc.Error:
            continue
    sys.exit("Could not connect with any available ODBC driver: %s" % sorted(available))


def rows(cur, sql):
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--db", default=DEFAULT_DB, help="the AutoCount FnB *_FE book")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    out_path = args.out or os.path.join(here, "..", "web", "kds", "menu.json")
    out_path = os.path.abspath(out_path)

    conn, driver = connect(args.server, args.db)
    print("connected: %s / %s   (%s)" % (args.server, args.db, driver))
    cur = conn.cursor()

    # ---- stations (what the user calls "kitchen channels") ----
    stations = rows(cur, """
        SELECT PrinterSetKey, PrinterSetID, Description, IsActive
        FROM   PosPrinterSet
        ORDER  BY PrinterSetKey
    """)

    # ---- the three routing tables ----
    by_item = {r["ItemCode"]: r["PrinterSetKey"]
               for r in rows(cur, "SELECT ItemCode, PrinterSetKey FROM PosPrinterSetItem")}
    by_group = {r["ItemGroup"]: r["PrinterSetKey"]
                for r in rows(cur, "SELECT ItemGroup, PrinterSetKey FROM PosPrinterSetItemGroup")}
    by_type = {r["ItemType"]: r["PrinterSetKey"]
               for r in rows(cur, "SELECT ItemType, PrinterSetKey FROM PosPrinterSetItemType")}

    groups = {r["ItemGroup"]: r["Description"]
              for r in rows(cur, "SELECT ItemGroup, Description FROM ItemGroup")}

    items = rows(cur, """
        SELECT ItemCode, Description, Desc2, ItemGroup, ItemType, BaseUOM, IsActive
        FROM   Item
        ORDER  BY ItemGroup, ItemCode
    """)
    conn.close()

    st_by_key = {s["PrinterSetKey"]: s for s in stations}

    def route(it):
        """Return (PrinterSetKey, how) or (None, 'unrouted')."""
        if it["ItemCode"] in by_item:
            return by_item[it["ItemCode"]], "item"
        if it["ItemGroup"] in by_group:
            return by_group[it["ItemGroup"]], "group"
        if it["ItemType"] in by_type:
            return by_type[it["ItemType"]], "type"
        return None, "unrouted"

    out_items, unrouted, tally = [], [], {}
    for it in items:
        if it["IsActive"] is not None and not it["IsActive"]:
            continue
        key, how = route(it)
        st = st_by_key.get(key)
        rec = {
            "code": it["ItemCode"],
            "name": (it["Description"] or "").strip(),
            "name2": (it["Desc2"] or "").strip(),
            "group": it["ItemGroup"],
            "groupName": (groups.get(it["ItemGroup"]) or "").strip(),
            "uom": it["BaseUOM"],
            "stationKey": key,
            "station": st["PrinterSetID"] if st else None,
            "routedBy": how,
        }
        out_items.append(rec)
        label = rec["station"] or "(unrouted)"
        tally[label] = tally.get(label, 0) + 1
        if key is None:
            unrouted.append(rec["code"])

    payload = {
        "source": {"server": args.server, "database": args.db},
        "stations": [
            {"key": s["PrinterSetKey"], "id": s["PrinterSetID"],
             "name": s["Description"], "active": bool(s["IsActive"])}
            for s in stations
        ],
        "routingUsed": {
            "byItem": len(by_item),
            "byGroup": len(by_group),
            "byType": len(by_type),
        },
        "items": out_items,
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with io.open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    # Also emit a plain <script> version. A page opened straight off the disk
    # (file://) is blocked from fetch()-ing a sibling .json by CORS, but a
    # script tag loads fine - so double-clicking the KDS page still shows the
    # real AutoCount menu with no web server involved.
    js_path = os.path.splitext(out_path)[0] + ".js"
    with io.open(js_path, "w", encoding="utf-8") as f:
        f.write("/* Generated by tools/export-menu.py - do not edit by hand. */\n")
        f.write("window.KDS_MENU = ")
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write(";\n")

    # ---- report (write to a file too: Windows consoles mangle CJK) ----
    lines = []
    lines.append("stations: " + ", ".join(
        "%s (key %s)" % (s["PrinterSetID"], s["PrinterSetKey"]) for s in stations))
    lines.append("routing rules: item=%d group=%d type=%d"
                 % (len(by_item), len(by_group), len(by_type)))
    lines.append("active items: %d" % len(out_items))
    for k in sorted(tally):
        lines.append("  %-14s %d" % (k, tally[k]))
    if unrouted:
        lines.append("UNROUTED (%d) - these dishes reach no kitchen screen: %s"
                     % (len(unrouted), ", ".join(unrouted[:20])))
    lines.append("")
    lines.append("menu by station:")
    for s in stations:
        lines.append("  [%s]" % s["PrinterSetID"])
        for g in sorted({i["group"] for i in out_items
                         if i["stationKey"] == s["PrinterSetKey"]}):
            gi = [i for i in out_items
                  if i["stationKey"] == s["PrinterSetKey"] and i["group"] == g]
            lines.append("    %-8s %-22s %d items" % (g, gi[0]["groupName"], len(gi)))
            for i in gi[:6]:
                lines.append("        %-10s %s" % (i["code"], i["name"]))
            if len(gi) > 6:
                lines.append("        ... +%d more" % (len(gi) - 6))

    report = os.path.join(os.path.dirname(out_path), "..", "..", "docs", "menu-report.txt")
    report = os.path.abspath(report)
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with io.open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("wrote %s (%d items)" % (out_path, len(out_items)))
    print("wrote %s" % js_path)
    print("wrote %s" % report)


if __name__ == "__main__":
    main()
