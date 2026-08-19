#!/usr/bin/env python3
"""
BTX KDS Bridge
==============
Reads live orders out of an AutoCount FnB frontend book and serves them to the
kitchen screens on the shop LAN.

Two hard rules:

  1. AutoCount is READ-ONLY. Every statement issued against the customer's book
     is a SELECT. "Done" and "bumped" are the KDS's own state and live in this
     Bridge's SQLite file, never in AutoCount. Phase 1 must be unpluggable at
     any moment with the shop falling straight back to kitchen printers.

  2. Kitchen state belongs to the Bridge, not the browser. Two screens on the
     same station must see the same thing; if one cook clears an order the other
     screen must agree. So the browser holds no order state at all - it renders
     what the Bridge says and posts actions back.

Nothing about a customer's setup is hardcoded - see config.json.

Usage:
    python bridge.py
    python bridge.py --lookback-hours 9000      # to see an old test book
    python bridge.py --probe                    # read once, print a report, exit
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import pyodbc
except ImportError:
    sys.exit("pyodbc is required:  pip install pyodbc")

HERE = os.path.dirname(os.path.abspath(__file__))


def is_true(v):
    """
    AutoCount's d_Boolean columns (Printed, Served, Cancelled, IsPaid, IsFOC,
    IsActive...) are CHAR holding 'T' or 'F', not SQL bit. Python's bool('F')
    is True, so reading them directly inverts every one of these flags
    silently - a voided dish looks live, an inactive item looks sellable.
    Everything boolean out of AutoCount goes through here.
    """
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().upper() in ("T", "TRUE", "Y", "1")


DRIVER_PREFERENCE = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
]


# ─────────────────────────────────────────────────────────────
# config
# ─────────────────────────────────────────────────────────────
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    cfg = json.loads(raw)

    def strip(node):
        """Drop the _comment / _*_note keys that document the file."""
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if not k.startswith("_")}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(cfg)


# ─────────────────────────────────────────────────────────────
# KDS state - ours, not AutoCount's
# ─────────────────────────────────────────────────────────────
class StateStore:
    """
    done_qty is stored per dish line rather than as a boolean so a line of 6
    lattes can be half finished, and so it can later be written back to
    AutoCount per source line if we ever enable that.
    """

    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS dish_state(
                doc_key   INTEGER NOT NULL,
                dtl_key   INTEGER NOT NULL,
                done_qty  REAL    NOT NULL DEFAULT 0,
                updated   TEXT    NOT NULL,
                PRIMARY KEY (doc_key, dtl_key)
            );
            -- bumped per ROUND, not just per station: when add-ons are shown as
            -- their own cards, clearing the add-on must not clear the original
            -- order sitting next to it.
            CREATE TABLE IF NOT EXISTS bump_state(
                doc_key     INTEGER NOT NULL,
                station_key INTEGER NOT NULL,
                round       INTEGER NOT NULL DEFAULT 1,
                bumped_at   TEXT    NOT NULL,
                PRIMARY KEY (doc_key, station_key, round)
            );
            -- When the Bridge first laid eyes on each dish line. This is what
            -- separates an add-on from the original order, and it is our own
            -- observation rather than a guess at AutoCount's LastModified
            -- behaviour. It also makes the timer honest: a dish added at 14:30
            -- should be timed from 14:30, not from when the table sat down.
            -- Deleting a dish in the POS removes the row outright, so without a
            -- record of our own the dish would simply vanish from the screen and
            -- a cook could carry on making it. We keep enough to keep showing it,
            -- struck through and marked DELETED, until someone acknowledges it.
            CREATE TABLE IF NOT EXISTS line_seen(
                doc_key    INTEGER NOT NULL,
                dtl_key    INTEGER NOT NULL,
                first_seen TEXT    NOT NULL,
                code       TEXT,
                name       TEXT,
                qty        REAL,
                station_key INTEGER,
                note       TEXT,
                gone_at    TEXT,
                ack        INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (doc_key, dtl_key)
            );
            -- one row per physical screen. The licence will be issued per screen
            -- and keyed on screen_id, so the registry starts here.
            CREATE TABLE IF NOT EXISTS screens(
                screen_id   TEXT PRIMARY KEY,
                name        TEXT,
                station_key INTEGER,
                first_seen  TEXT,
                last_seen   TEXT
            );
        """)
        self._migrate()
        self.db.commit()

    def _migrate(self):
        """
        bump_state gained a `round` column. An older file has the two-column
        primary key, which SQLite cannot alter, so rebuild it. Losing which
        orders were cleared is harmless - they simply reappear once and get
        cleared again - whereas a wrong schema would break every bump.
        """
        # line_seen gained the columns needed to render a deleted line
        ls = [r[1] for r in self.db.execute("PRAGMA table_info(line_seen)")]
        if ls and "gone_at" not in ls:
            for col, decl in (("code", "TEXT"), ("name", "TEXT"), ("qty", "REAL"),
                             ("station_key", "INTEGER"), ("note", "TEXT"),
                             ("gone_at", "TEXT"), ("ack", "INTEGER NOT NULL DEFAULT 0")):
                if col not in ls:
                    self.db.execute("ALTER TABLE line_seen ADD COLUMN %s %s" % (col, decl))
            sys.stderr.write("[state] extended line_seen for deleted-line tracking\n")

        cols = [r[1] for r in self.db.execute("PRAGMA table_info(bump_state)")]
        if "round" in cols:
            return
        self.db.executescript("""
            DROP TABLE IF EXISTS bump_state;
            CREATE TABLE bump_state(
                doc_key     INTEGER NOT NULL,
                station_key INTEGER NOT NULL,
                round       INTEGER NOT NULL DEFAULT 1,
                bumped_at   TEXT    NOT NULL,
                PRIMARY KEY (doc_key, station_key, round)
            );
        """)
        sys.stderr.write("[state] rebuilt bump_state with per-round keys\n")

    def _now(self):
        return datetime.now().isoformat(timespec="seconds")

    def snapshot(self):
        with self.lock:
            done = {(r[0], r[1]): r[2]
                    for r in self.db.execute("SELECT doc_key,dtl_key,done_qty FROM dish_state")}
            bump = {(r[0], r[1], r[2]): r[3]
                    for r in self.db.execute(
                        "SELECT doc_key,station_key,round,bumped_at FROM bump_state")}
            seen = {(r[0], r[1]): r[2]
                    for r in self.db.execute("SELECT doc_key,dtl_key,first_seen FROM line_seen")}
        return done, bump, seen

    def see_lines(self, rows):
        """
        Record first sighting of lines we have not met before, keeping enough
        detail to render them later if the POS deletes them.
        rows: (doc_key, dtl_key, code, name, qty, station_key, note)
        """
        if not rows:
            return
        now = self._now()
        with self.lock:
            self.db.executemany(
                "INSERT OR IGNORE INTO line_seen"
                "(doc_key,dtl_key,first_seen,code,name,qty,station_key,note) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(r[0], r[1], now, r[2], r[3], r[4], r[5], r[6]) for r in rows])
            self.db.commit()

    def mark_gone(self, doc_key, present_dtl_keys):
        """
        Stamp lines we used to see on this order and no longer do. Only ever set
        once - the moment of disappearance is what the screen counts from.
        """
        now = self._now()
        with self.lock:
            rows = self.db.execute(
                "SELECT dtl_key FROM line_seen WHERE doc_key=? AND gone_at IS NULL",
                (doc_key,)).fetchall()
            missing = [r[0] for r in rows if r[0] not in present_dtl_keys]
            if missing:
                self.db.executemany(
                    "UPDATE line_seen SET gone_at=? WHERE doc_key=? AND dtl_key=?",
                    [(now, doc_key, k) for k in missing])
                self.db.commit()

    def deleted_lines(self, doc_key):
        with self.lock:
            return [dict(dtlKey=r[0], code=r[1], name=r[2], qty=r[3],
                         stationKey=r[4], note=r[5], goneAt=r[6], firstSeen=r[7])
                    for r in self.db.execute(
                        "SELECT dtl_key,code,name,qty,station_key,note,gone_at,first_seen "
                        "FROM line_seen WHERE doc_key=? AND gone_at IS NOT NULL AND ack=0",
                        (doc_key,))]

    def ack_deleted(self, doc_key, dtl_key):
        with self.lock:
            self.db.execute("UPDATE line_seen SET ack=1 WHERE doc_key=? AND dtl_key=?",
                            (doc_key, dtl_key))
            self.db.commit()

    def set_done(self, doc_key, dtl_key, qty):
        with self.lock:
            self.db.execute(
                "INSERT INTO dish_state(doc_key,dtl_key,done_qty,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(doc_key,dtl_key) DO UPDATE SET done_qty=excluded.done_qty,"
                "updated=excluded.updated",
                (doc_key, dtl_key, max(0.0, float(qty)), self._now()))
            self.db.commit()

    def bump(self, doc_key, station_key, rounds):
        """rounds: which add-on rounds this card covered. Never guessed here."""
        now = self._now()
        with self.lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO bump_state(doc_key,station_key,round,bumped_at) "
                "VALUES(?,?,?,?)", [(doc_key, station_key, int(r), now) for r in rounds])
            self.db.commit()

    def unbump(self, doc_key, station_key, rounds=None):
        """Recall. rounds=None clears every round for that station."""
        with self.lock:
            if rounds is None:
                self.db.execute("DELETE FROM bump_state WHERE doc_key=? AND station_key=?",
                                (doc_key, station_key))
            else:
                self.db.executemany(
                    "DELETE FROM bump_state WHERE doc_key=? AND station_key=? AND round=?",
                    [(doc_key, station_key, int(r)) for r in rounds])
            self.db.commit()

    def clear_done_for_doc(self, doc_key, dtl_keys):
        if not dtl_keys:
            return
        with self.lock:
            self.db.executemany("DELETE FROM dish_state WHERE doc_key=? AND dtl_key=?",
                                [(doc_key, k) for k in dtl_keys])
            self.db.commit()

    def seen_screen(self, screen_id, name, station_key):
        now = self._now()
        with self.lock:
            cur = self.db.execute("SELECT screen_id FROM screens WHERE screen_id=?", (screen_id,))
            if cur.fetchone():
                self.db.execute(
                    "UPDATE screens SET name=?, station_key=?, last_seen=? WHERE screen_id=?",
                    (name, station_key, now, screen_id))
            else:
                self.db.execute(
                    "INSERT INTO screens(screen_id,name,station_key,first_seen,last_seen) "
                    "VALUES(?,?,?,?,?)", (screen_id, name, station_key, now, now))
            self.db.commit()

    def screens(self):
        with self.lock:
            return [dict(screen_id=r[0], name=r[1], station_key=r[2],
                         first_seen=r[3], last_seen=r[4])
                    for r in self.db.execute(
                        "SELECT screen_id,name,station_key,first_seen,last_seen "
                        "FROM screens ORDER BY first_seen")]

    def prune(self, keep_doc_keys):
        """Drop state for orders that have fallen out of the lookback window."""
        with self.lock:
            keep = set(keep_doc_keys)
            olds = [r[0] for r in self.db.execute("SELECT DISTINCT doc_key FROM dish_state")]
            gone = [k for k in olds if k not in keep]
            if gone:
                self.db.executemany("DELETE FROM dish_state WHERE doc_key=?", [(k,) for k in gone])
            oldb = [r[0] for r in self.db.execute("SELECT DISTINCT doc_key FROM bump_state")]
            goneb = [k for k in oldb if k not in keep]
            if goneb:
                self.db.executemany("DELETE FROM bump_state WHERE doc_key=?", [(k,) for k in goneb])
            if gone or goneb:
                self.db.commit()


# ─────────────────────────────────────────────────────────────
# AutoCount reader - SELECT only
# ─────────────────────────────────────────────────────────────
class AutoCountReader:
    def __init__(self, sql_cfg, order_cfg, numbering):
        self.cfg = sql_cfg
        self.ocfg = order_cfg
        self.numbering = numbering or ["OrderNo", "DocNo", "TableNo"]
        self.conn = None
        self.driver = None
        self.routing = {"stations": [], "by_item": {}, "by_group": {}, "by_type": {},
                        "groups": {}, "types": {}, "item_meta": {}}
        self.routing_loaded_at = 0

    # ---- connection -------------------------------------------------
    def connect(self):
        available = set(pyodbc.drivers())
        last = None
        for drv in DRIVER_PREFERENCE:
            if drv not in available:
                continue
            parts = ["DRIVER={%s}" % drv,
                     "SERVER=%s" % self.cfg["server"],
                     "DATABASE=%s" % self.cfg["database"]]
            if self.cfg.get("trusted_connection", True):
                parts.append("Trusted_Connection=yes")
            else:
                parts.append("UID=%s" % self.cfg.get("username", ""))
                parts.append("PWD=%s" % self.cfg.get("password", ""))
            # Driver 17/18 default to Encrypt=yes and then reject the
            # self-signed certificate a local SQL Server presents.
            if "18" in drv or "17" in drv:
                parts.append("TrustServerCertificate=yes")
            try:
                self.conn = pyodbc.connect(";".join(parts), timeout=8)
                self.conn.autocommit = True      # we never write; no transactions to hold
                self.driver = drv
                return True
            except pyodbc.Error as e:
                last = e
        raise RuntimeError("no ODBC driver could connect (%s): %s" % (sorted(available), last))

    def ensure(self):
        if self.conn is None:
            self.connect()
            return
        try:
            self.conn.execute("SELECT 1").fetchone()
        except pyodbc.Error:
            # SQL Server restarted, network blipped, laptop slept - reconnect
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
            self.connect()

    def _rows(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        out = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
        return out

    # ---- Kitchen Printer Maintenance --------------------------------
    def load_routing(self):
        """
        Rebuilds the item -> station map exactly the way AutoCount narrows down
        for its kitchen printers: a rule on the item itself beats one on its
        group, which beats one on its type.
        """
        self.ensure()
        stations = self._rows(
            "SELECT PrinterSetKey, PrinterSetID, Description, IsActive "
            "FROM PosPrinterSet ORDER BY PrinterSetKey")
        by_item = {r["ItemCode"]: r["PrinterSetKey"] for r in
                   self._rows("SELECT ItemCode, PrinterSetKey FROM PosPrinterSetItem")}
        by_group = {r["ItemGroup"]: r["PrinterSetKey"] for r in
                    self._rows("SELECT ItemGroup, PrinterSetKey FROM PosPrinterSetItemGroup")}
        by_type = {r["ItemType"]: r["PrinterSetKey"] for r in
                   self._rows("SELECT ItemType, PrinterSetKey FROM PosPrinterSetItemType")}
        groups = {r["ItemGroup"]: (r["Description"] or "").strip() for r in
                  self._rows("SELECT ItemGroup, Description FROM ItemGroup")}
        types = {r["ItemType"]: (r["Description"] or "").strip() for r in
                 self._rows("SELECT ItemType, Description FROM ItemType")}
        item_meta = {r["ItemCode"]: r for r in self._rows(
            "SELECT ItemCode, Description, Desc2, ItemGroup, ItemType FROM Item")}

        self.routing = dict(stations=stations, by_item=by_item, by_group=by_group,
                            by_type=by_type, groups=groups, types=types,
                            item_meta=item_meta)
        self.routing_loaded_at = time.time()

    def active_stations(self):
        act = [s for s in self.routing["stations"] if is_true(s["IsActive"])]
        return act or self.routing["stations"]

    def route(self, item_code):
        """-> (station_key or None, how)"""
        r = self.routing
        if item_code in r["by_item"]:
            return r["by_item"][item_code], "item"
        meta = r["item_meta"].get(item_code)
        if meta:
            if meta["ItemGroup"] in r["by_group"]:
                return r["by_group"][meta["ItemGroup"]], "group"
            if meta["ItemType"] in r["by_type"]:
                return r["by_type"][meta["ItemType"]], "type"
        return None, "unrouted"

    # ---- orders -----------------------------------------------------
    def read_orders(self, lookback_hours):
        """
        Returns (orders, stats). Only SELECTs.

        One query for masters and one for their details, rather than per-order
        round trips - a busy lunch can have a few hundred open tickets and we
        re-read every 1.5s.
        """
        self.ensure()
        since = datetime.now() - timedelta(hours=float(lookback_hours))

        masters = self._rows(
            "SELECT DocKey, DocNo, OrderNo, TableNo, JoinTableNo, Guests, Remarks, "
            "       CreatedTime, LastModified, IsCompleted, IsPaid, Cancelled, "
            "       ServiceType, UserID, TerminalID "
            "FROM   PosOrder "
            "WHERE  CreatedTime >= ? "
            "ORDER  BY CreatedTime",
            (since,))

        stats = dict(masters=len(masters), lines=0, printed=0, unprinted=0,
                     unrouted=0, voided=0, unrouted_items={},
                     window_from=since.isoformat(timespec="seconds"))
        if not masters:
            return [], stats

        keys = [m["DocKey"] for m in masters]
        # chunked IN (...) keeps us under SQL Server's parameter ceiling
        details = []
        CHUNK = 500
        for i in range(0, len(keys), CHUNK):
            part = keys[i:i + CHUNK]
            marks = ",".join("?" * len(part))
            details += self._rows(
                "SELECT DtlKey, DocKey, Seq, ItemCode, Description, Qty, UOM, "
                "       Remarks, Modifier, DtlType, Printed, Served, VoidQty, "
                "       ReturnQty, IsFOC, SetMealDtlGuid, ParentDtlGuid, LastModified "
                "FROM   PosOrderDtl "
                "WHERE  DocKey IN (%s) "
                "ORDER  BY DocKey, Seq" % marks, tuple(part))

        by_doc = {}
        for d in details:
            by_doc.setdefault(d["DocKey"], []).append(d)

        require_printed = bool(self.ocfg.get("require_printed", False))
        unrouted_first = bool(self.ocfg.get("unrouted_to_first_station", True))
        skip_voided = bool(self.ocfg.get("skip_voided_lines", False))
        act = self.active_stations()
        first_key = act[0]["PrinterSetKey"] if act else None

        out = []
        for m in masters:
            if is_true(m["Cancelled"]):
                continue          # whole ticket voided in the POS
            lines = []
            for d in by_doc.get(m["DocKey"], []):
                stats["lines"] += 1
                if is_true(d["Printed"]):
                    stats["printed"] += 1
                else:
                    stats["unprinted"] += 1

                # "M" is a package master row and "D"/"V" are total-discount
                # rows - none of them is food anyone cooks.
                if (d["DtlType"] or "N") != "N":
                    continue
                if require_printed and not is_true(d["Printed"]):
                    continue

                qty = float(d["Qty"] or 0)
                void_qty = float(d["VoidQty"] or 0)
                ret_qty = float(d["ReturnQty"] or 0)
                voided = (void_qty + ret_qty) >= qty and qty > 0
                if voided:
                    stats["voided"] += 1
                    if skip_voided:
                        continue

                st_key, how = self.route(d["ItemCode"])
                if how == "unrouted":
                    stats["unrouted"] += 1
                    # Name them, because "3 unrouted lines" is not actionable.
                    # Some are genuine gaps (a dish nobody assigned a channel);
                    # others are correctly unrouted non-food - court rental,
                    # deposits, member top-ups. Only the owner can tell which.
                    nm = (d["Description"]
                          or self.routing["item_meta"].get(d["ItemCode"], {}).get("Description")
                          or "")
                    stats["unrouted_items"][d["ItemCode"]] = str(nm).strip()
                    if not unrouted_first:
                        continue
                    st_key = first_key

                meta = self.routing["item_meta"].get(d["ItemCode"], {})
                grp = meta.get("ItemGroup")
                typ = meta.get("ItemType")
                lines.append(dict(
                    dtlKey=int(d["DtlKey"]),
                    seq=int(d["Seq"] or 0),
                    code=d["ItemCode"],
                    # the line's own Description is what the cashier saw; fall
                    # back to the item master, then to the bare code
                    name=(d["Description"] or meta.get("Description") or d["ItemCode"] or "").strip(),
                    # Item.Desc2 - the second description, commonly the other
                    # language. The screen can be told to show it or not.
                    name2=(meta.get("Desc2") or "").strip(),
                    qty=qty,
                    uom=d["UOM"],
                    note=(d["Remarks"] or "").strip(),
                    modifier=(d["Modifier"] or "").strip(),
                    stationKey=st_key,
                    routedBy=how,
                    group=grp,
                    groupName=self.routing["groups"].get(grp, "") or (grp or ""),
                    type=typ,
                    typeName=self.routing["types"].get(typ, "") or (typ or ""),
                    lastModified=d["LastModified"].isoformat() if d["LastModified"] else None,
                    voided=voided,
                    isFoc=is_true(d["IsFOC"]),
                    setMeal=str(d["SetMealDtlGuid"]) if d["SetMealDtlGuid"] else None,
                ))

            if not lines:
                continue

            out.append(dict(
                docKey=int(m["DocKey"]),
                docNo=m["DocNo"],
                orderNo=m["OrderNo"],
                display=self._display_number(m),
                table=(m["TableNo"] or "").strip(),
                joinTable=(m["JoinTableNo"] or "").strip(),
                pax=int(m["Guests"] or 0),
                remarks=(m["Remarks"] or "").strip(),
                createdAt=m["CreatedTime"].isoformat() if m["CreatedTime"] else None,
                modifiedAt=m["LastModified"].isoformat() if m["LastModified"] else None,
                isPaid=is_true(m["IsPaid"]),
                isCompleted=is_true(m["IsCompleted"]),
                items=lines,
            ))
        return out, stats

    def _display_number(self, m):
        """
        The number a customer is called by. This book leaves OrderNo empty, so
        the chain matters; DocNo like T01-HB000001 is trimmed to its digits so a
        pickup screen shows 1 rather than the terminal prefix.
        """
        for field in self.numbering:
            v = m.get(field)
            if v is None:
                continue
            v = str(v).strip()
            if not v:
                continue
            if field == "DocNo":
                # T01-HB000001 -> 1 : take the trailing number group, not the
                # leading one, or the terminal prefix leaks onto the screen
                m2 = re.search(r"(\d+)\D*$", v)
                if m2:
                    return m2.group(1).lstrip("0") or "0"
                return v
            return v
        return str(m.get("DocKey", ""))


# ─────────────────────────────────────────────────────────────
# the live view: AutoCount orders merged with our own state
# ─────────────────────────────────────────────────────────────
class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = StateStore(os.path.join(HERE, "kds_state.db"))
        self.reader = AutoCountReader(cfg["sql"], cfg["orders"],
                                      cfg.get("display_numbering", {}).get("order"))
        self.lock = threading.Lock()
        # used to tell "we watched this line appear" from "it was already there
        # when we started", which decides which clock groups add-on rounds
        self.started_at = datetime.now()
        self.view = dict(ok=False, error="starting", stations=[], orders=[],
                         stats={}, rev=0, serverTime=None, source={})
        self.rev = 0
        self.stop = threading.Event()

    def refresh(self):
        now = time.time()
        if now - self.reader.routing_loaded_at > float(
                self.cfg["polling"].get("routing_seconds", 300)):
            self.reader.load_routing()

        orders, stats = self.reader.read_orders(self.cfg["orders"]["lookback_hours"])

        # Record any dish line we have not met before, then read state back so
        # brand-new lines already carry a first_seen in this same cycle.
        fresh = []
        for o in orders:
            for it in o["items"]:
                fresh.append((o["docKey"], it["dtlKey"], it["code"], it["name"],
                              it["qty"], it["stationKey"], it["note"]))
        self.state.see_lines(fresh)

        # Anything we used to see on an order and no longer do was deleted in the
        # POS. AutoCount removes the row outright, so the dish would otherwise
        # just vanish and a cook could carry on making it.
        for o in orders:
            self.state.mark_gone(o["docKey"], set(i["dtlKey"] for i in o["items"]))
            act_first = (self.reader.active_stations() or [{}])[0].get("PrinterSetKey")
            for g in self.state.deleted_lines(o["docKey"]):
                # A line recorded before we stored station_key has none. Without
                # a station it would match no screen, so it could never be shown
                # and never be acknowledged - it would sit in the table forever.
                if g["stationKey"] is None:
                    g["stationKey"] = act_first
                o["items"].append(dict(
                    dtlKey=g["dtlKey"], seq=99999, code=g["code"],
                    # A line deleted before we started recording detail leaves
                    # only its key. Still worth showing - "a line was removed" is
                    # information a cook needs - but say so honestly rather than
                    # inventing a dish name.
                    name=g["name"] or g["code"] or ("Removed line #%s" % g["dtlKey"]),
                    name2="",
                    qty=float(g["qty"] or 0), uom=None, note=g["note"] or "",
                    modifier="", stationKey=g["stationKey"], routedBy="deleted",
                    group=None, groupName="", type=None, typeName="",
                    lastModified=g["goneAt"], voided=False, isFoc=False,
                    setMeal=None, deleted=True, deletedAt=g["goneAt"],
                    firstSeen=g["firstSeen"]))

        done, bump, seen = self.state.snapshot()

        gap = float(self.cfg["orders"].get("addon_gap_seconds", 60))
        for o in orders:
            self._assign_rounds(o, seen, gap)
            o["bumped"] = {}
            for st in self.reader.active_stations():
                k = st["PrinterSetKey"]
                per_round = {str(r): ts for (dk, sk, r), ts in bump.items()
                             if dk == o["docKey"] and sk == k}
                if per_round:
                    o["bumped"][str(k)] = per_round
            for it in o["items"]:
                it.setdefault("deleted", False)
                it["doneQty"] = float(done.get((o["docKey"], it["dtlKey"]), 0.0))
                if it["doneQty"] > it["qty"]:
                    it["doneQty"] = it["qty"]      # qty was edited down in the POS

        self.state.prune([o["docKey"] for o in orders])

        with self.lock:
            self.rev += 1
            self.view = dict(
                ok=True, error=None, rev=self.rev,
                serverTime=datetime.now().isoformat(timespec="seconds"),
                stations=[dict(key=s["PrinterSetKey"], id=s["PrinterSetID"],
                               name=(s["Description"] or s["PrinterSetID"]),
                               active=is_true(s["IsActive"]))
                          for s in self.reader.active_stations()],
                orders=orders, stats=stats,
                source=dict(server=self.cfg["sql"]["server"],
                            database=self.cfg["sql"]["database"],
                            driver=self.reader.driver),
            )

    def _assign_rounds(self, o, seen, gap_seconds):
        """
        Group an order's lines into rounds: the original order, then each later
        add-on, so a table's second and third orders never blur into one wall of
        dishes.

        Grouping is by WHEN THE BRIDGE FIRST SAW each line, and this turned out
        to be far better than it first appeared. Measuring a real book: one
        ticket's first six lines carry LastModified stamps spread over 38
        seconds, yet the Bridge saw all six in the same instant - because
        AutoCount writes the lines when the cashier SENDS the order, not as each
        dish is keyed in. So one sighting == one send == exactly the round
        boundary a kitchen cares about, with no threshold to tune.

        My earlier attempt clustered LastModified with a 60s gap, which was
        wrong twice over: it merged a genuine add-on 47 seconds later into the
        original, and LastModified reflects keying-in, not sending. Corrected.

        gap_seconds survives only as a small tolerance, in case one send ever
        reaches us across two polls. LastModified is used only if a line somehow
        has no sighting recorded at all.

        Lines already present when the Bridge started share one sighting and so
        form round 1 together. That is the honest answer - we did not watch them
        arrive and cannot invent the history.
        """
        stamped = []
        for it in o["items"]:
            t = None
            ts = seen.get((o["docKey"], it["dtlKey"]))
            if ts:
                try:
                    t = datetime.fromisoformat(ts)
                except ValueError:
                    t = None
            if t is None:                       # should not happen; be safe
                lm = it.get("lastModified")
                if lm:
                    try:
                        t = datetime.fromisoformat(lm)
                    except ValueError:
                        pass
            stamped.append((t, it))

        # group in sighting order, falling back to the POS's own line order
        stamped.sort(key=lambda p: (p[0] or datetime.min, p[1]["seq"]))

        rnd, prev, base = 0, None, None
        for t, it in stamped:
            if prev is None or t is None or (t - prev).total_seconds() > gap_seconds:
                rnd += 1
                base = t
            if t is not None:
                prev = t
            it["round"] = rnd
            it["roundAt"] = (base or t).isoformat() if (base or t) else o["createdAt"]


    def run(self):
        interval = float(self.cfg["polling"].get("orders_seconds", 1.5))
        while not self.stop.is_set():
            try:
                self.refresh()
            except Exception as e:
                with self.lock:
                    self.view = dict(self.view)
                    self.view["ok"] = False
                    self.view["error"] = "%s: %s" % (type(e).__name__, e)
                    self.view["serverTime"] = datetime.now().isoformat(timespec="seconds")
                sys.stderr.write("[poll] %s\n" % traceback.format_exc())
                # a lost SQL Server must not spin the CPU or spam the log
                self.stop.wait(3.0)
                continue
            self.stop.wait(interval)

    def get_view(self):
        with self.lock:
            return self.view


# ─────────────────────────────────────────────────────────────
# HTTP: the API plus the screens themselves
# ─────────────────────────────────────────────────────────────
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
        ".ico": "image/x-icon", ".woff2": "font/woff2", ".mp3": "audio/mpeg"}


class Handler(BaseHTTPRequestHandler):
    hub = None
    web_root = None
    server_version = "BTX-KDS-Bridge"
    sys_version = ""

    def log_message(self, fmt, *args):
        # the default logs every poll; at 1.5s per screen that is pure noise
        pass

    # ---- helpers ----
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # tablets may be pointed at the Bridge from a different origin during
        # setup; the API carries no secrets and never leaves the shop LAN
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass    # a screen navigated away mid-response

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), )

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            return self._json(self.hub.get_view())
        if path == "/api/health":
            v = self.hub.get_view()
            return self._json(dict(ok=v.get("ok"), error=v.get("error"),
                                   rev=v.get("rev"), stats=v.get("stats"),
                                   source=v.get("source"),
                                   serverTime=v.get("serverTime")))
        if path == "/api/screens":
            return self._json(dict(screens=self.hub.state.screens()))
        return self._static(path)

    def _static(self, path):
        if path in ("/", ""):
            path = "/kds/index.html"
        if path.endswith("/"):
            path += "index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(self.web_root, rel))
        # never serve outside web_root, whatever the request says
        if not full.startswith(os.path.normpath(self.web_root)):
            return self._send(403, "forbidden", "text/plain; charset=utf-8")
        if not os.path.isfile(full):
            return self._send(404, "not found: " + rel, "text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            body = f.read()
        return self._send(200, body, MIME.get(ext, "application/octet-stream"))

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_json()
        st = self.hub.state
        try:
            if path == "/api/done":
                st.set_done(int(body["docKey"]), int(body["dtlKey"]), float(body["doneQty"]))
            elif path == "/api/bump":
                # The screen says which rounds its card covered - merged cards
                # cover all of them, split cards exactly one.
                rounds = body.get("rounds")
                doc, sk = int(body["docKey"]), int(body["stationKey"])
                if not rounds:
                    view = self.hub.get_view()
                    rounds = sorted({i.get("round", 1) for o in view.get("orders", [])
                                     if o["docKey"] == doc
                                     for i in o["items"] if i["stationKey"] == sk}) or [1]
                st.bump(doc, sk, rounds)
            elif path == "/api/recall":
                doc, sk = int(body["docKey"]), int(body["stationKey"])
                rounds = body.get("rounds")
                st.unbump(doc, sk, [int(r) for r in rounds] if rounds else None)
                # recall means "not done after all" for the lines coming back
                view = self.hub.get_view()
                want = set(int(r) for r in rounds) if rounds else None
                for o in view.get("orders", []):
                    if o["docKey"] == doc:
                        st.clear_done_for_doc(doc, [
                            i["dtlKey"] for i in o["items"]
                            if i["stationKey"] == sk
                            and (want is None or i.get("round", 1) in want)])
                        break
            elif path == "/api/ack-deleted":
                st.ack_deleted(int(body["docKey"]), int(body["dtlKey"]))
            elif path == "/api/hello":
                st.seen_screen(str(body.get("screenId", ""))[:32],
                               str(body.get("name", ""))[:40],
                               int(body.get("stationKey") or 0))
            else:
                return self._json(dict(ok=False, error="unknown endpoint"), 404)
        except (KeyError, TypeError, ValueError) as e:
            return self._json(dict(ok=False, error="bad request: %s" % e), 400)

        # Refresh straight away so the acting screen sees its own tap without
        # waiting out a poll interval.
        try:
            self.hub.refresh()
        except Exception:
            pass
        return self._json(dict(ok=True, rev=self.hub.get_view().get("rev")))


# ─────────────────────────────────────────────────────────────
def probe(hub):
    """One read, printed as a report. Answers 'is the data even there?'"""
    hub.reader.load_routing()
    orders, stats = hub.reader.read_orders(hub.cfg["orders"]["lookback_hours"])
    lines = []
    lines.append("connected to %s / %s via %s" % (
        hub.cfg["sql"]["server"], hub.cfg["sql"]["database"], hub.reader.driver))
    lines.append("")
    lines.append("stations: " + ", ".join(
        "%s(key %s)" % (s["PrinterSetID"], s["PrinterSetKey"])
        for s in hub.reader.active_stations()))
    lines.append("routing rules: item=%d group=%d type=%d" % (
        len(hub.reader.routing["by_item"]), len(hub.reader.routing["by_group"]),
        len(hub.reader.routing["by_type"])))
    lines.append("")
    lines.append("window starts   : %s  (lookback %s h)" % (
        stats["window_from"], hub.cfg["orders"]["lookback_hours"]))
    lines.append("orders in window: %d" % stats["masters"])
    lines.append("dish lines      : %d  (printed=%d, not yet printed=%d)" % (
        stats["lines"], stats["printed"], stats["unprinted"]))
    lines.append("unrouted lines  : %d" % stats["unrouted"])
    if stats["unrouted_items"]:
        lines.append("  these items match no Kitchen Printer rule, so today they reach")
        lines.append("  no printer either. Check each one - a dish here is a real gap,")
        lines.append("  but rental / deposit / top-up items are correctly excluded:")
        for code, nm in sorted(stats["unrouted_items"].items()):
            lines.append("    %-10s %s" % (code, nm))
    lines.append("voided lines    : %d" % stats["voided"])
    lines.append("orders shown to screens: %d" % len(orders))
    if stats["masters"] and not orders:
        lines.append("")
        lines.append("NOTE: orders exist but none reached a screen. Check require_printed")
        lines.append("      and unrouted_to_first_station in config.json.")
    for o in orders[:12]:
        lines.append("")
        lines.append("  %-16s table %-6s display#%-8s %s" % (
            o["docNo"], o["table"] or "-", o["display"], o["createdAt"]))
        for it in o["items"]:
            lines.append("      [%s] %-7s x%-5g %-28s %s%s" % (
                it["stationKey"], it["code"], it["qty"], it["name"][:28],
                ("note=" + it["note"]) if it["note"] else "",
                "  NO ROUTE" if it["routedBy"] == "unrouted" else ""))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--lookback-hours", type=float, default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--probe", action="store_true",
                    help="read once, write a report, exit")
    ap.add_argument("--show-unrouted", action="store_true",
                    help="also show items that match no Kitchen Printer rule, "
                         "on the first station. Useful for checking whether an "
                         "empty screen is a routing gap or genuinely no orders.")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.lookback_hours is not None:
        cfg["orders"]["lookback_hours"] = args.lookback_hours
    if args.port is not None:
        cfg["http"]["port"] = args.port
    if args.show_unrouted:
        cfg["orders"]["unrouted_to_first_station"] = True

    hub = Hub(cfg)

    if args.probe:
        hub.reader.connect()
        report = probe(hub)
        out = os.path.join(HERE, "probe-report.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(report.encode("ascii", "replace").decode("ascii"))
        print("\n(full report with original names: %s)" % out)
        return

    hub.reader.connect()
    print("[bridge] AutoCount: %s / %s via %s" % (
        cfg["sql"]["server"], cfg["sql"]["database"], hub.reader.driver))

    t = threading.Thread(target=hub.run, daemon=True)
    t.start()

    web_root = os.path.normpath(os.path.join(HERE, cfg["http"]["web_root"]))
    Handler.hub = hub
    Handler.web_root = web_root

    srv = ThreadingHTTPServer((cfg["http"]["bind"], int(cfg["http"]["port"])), Handler)
    print("[bridge] serving %s" % web_root)
    print("[bridge] http://localhost:%d/kds/index.html" % cfg["http"]["port"])
    print("[bridge] polling every %ss, lookback %sh, require_printed=%s" % (
        cfg["polling"]["orders_seconds"], cfg["orders"]["lookback_hours"],
        cfg["orders"]["require_printed"]))
    print("[bridge] Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] stopping")
    finally:
        hub.stop.set()
        srv.server_close()


if __name__ == "__main__":
    main()
