# BTX KDS Bridge

Reads live orders out of an AutoCount FnB frontend book and serves the kitchen
screens to the shop LAN.

## Two hard rules

1. **AutoCount is read-only.** Every statement issued against the customer's
   book is a `SELECT`. "Done" and "bumped" are the KDS's own state and live in
   `kds_state.db` next to this file, never in AutoCount. The Bridge can be
   unplugged at any moment and the shop falls straight back to kitchen printers.

2. **Kitchen state belongs to the Bridge, not the browser.** Two screens on the
   same station must agree — if one cook clears an order the other screen must
   see it go. So a screen holds no order state at all: it renders what the
   Bridge says and posts actions back.

## Running it

```
start-bridge.bat
```

Then open `http://localhost:5180/kds/index.html`, or from a tablet on the same
network, `http://<this-pc-ip>:5180/kds/index.html`.

The Bridge serves the screens itself — no separate web server needed.

### Tablets cannot reach it

Windows blocks inbound connections by default. Once, from an **administrator**
PowerShell:

```powershell
New-NetFirewallRule -DisplayName "BTX KDS Bridge 5180" -Direction Inbound -Protocol TCP -LocalPort 5180 -Action Allow -Profile Private,Domain
```

The shipped installer does this automatically; it is only manual while testing.

If it still fails, check the tablet is on the same subnet as this PC — a
separate guest SSID or an AP-isolated WiFi will not reach it.

## Checking whether the data is even there

```
python bridge.py --probe
```

Reads once and writes `probe-report.txt`: which stations exist, how many
routing rules, how many orders are in the window, how many dish lines are
already marked printed, and **every item that matches no Kitchen Printer rule,
by name**. That last list matters — an unrouted dish is a real gap, but an
unrouted *court rental / deposit / member top-up* is correctly excluded, and
only the owner can tell which is which.

Useful flags:

| Flag | Why |
|---|---|
| `--lookback-hours 9000` | see an old test book whose orders are months old |
| `--show-unrouted` | put unrouted items on the first station, to tell a routing gap apart from genuinely no orders |
| `--port 5181` | run a second instance alongside |

## Endpoints

| | |
|---|---|
| `GET /api/state` | stations, orders, per-line `doneQty`, per-station `bumped`, plus a `rev` that changes only when something changed |
| `GET /api/health` | is AutoCount reachable, and the read statistics including unrouted items |
| `GET /api/screens` | the device registry — one row per screen, with first and last seen |
| `POST /api/done` | `{docKey, dtlKey, doneQty}` |
| `POST /api/bump` | `{docKey, stationKey}` |
| `POST /api/recall` | `{docKey, stationKey}` — also clears that station's done marks |
| `POST /api/hello` | `{screenId, name, stationKey}` — a screen checking in |
| everything else | the files under `web/` |

## Configuration

All of it in `config.json`, with a note against every setting. Nothing about a
customer's setup is hardcoded. The three that matter most:

- **`require_printed`** — `true` shows only dishes AutoCount has already sent to
  the kitchen (`PosOrderDtl.Printed`), matching kitchen-printer behaviour.
  Default `false`, because a silently empty screen looks broken and this flag's
  behaviour still has to be confirmed against a live shop.
- **`unrouted_to_first_station`** — default `false`, which matches AutoCount
  exactly: an item with no Kitchen Printer rule reaches no printer today.
  Unrouted items are still named in `/api/health` and `--probe` so real gaps
  stay visible.
- **`lookback_hours`** — how far back to look, so yesterday's forgotten ticket
  never haunts the screen.

## Things worth knowing about this schema

Learned the hard way while building this:

- **`d_Boolean` columns are CHAR `'T'`/`'F'`, not SQL bit.** `bool('F')` is
  `True` in Python, so reading `Printed` / `Served` / `Cancelled` / `IsFOC` /
  `IsActive` directly inverts every one of them silently. Everything boolean
  goes through `is_true()`. Also why `ISNULL(Cancelled,0)` fails outright —
  SQL Server refuses to convert `'F'` to int.
- **`OrderNo` can be empty**, so the customer-facing number needs the fallback
  chain in `config.json` (`OrderNo` → `DocNo` → `TableNo`).
- **`DocNo` looks like `T01-HB000001`** — terminal prefix and all. The pickup
  number is the *trailing* digit group, not the leading one.
- **`DtlType`** is `"N"` for a real dish; `"M"` is a package master row and
  `"D"`/`"V"` are total-discount rows. None of the others is food.
- **Kitchen routing** resolves most-specific-first: `PosPrinterSetItem`, then
  `PosPrinterSetItemGroup`, then `PosPrinterSetItemType`. The book this was
  built against uses group level only — 15 rules, none per-item.

## Not done yet

- Per-screen licence enforcement. The `screens` registry is the anchor for it:
  a screen reports its Device ID, and the Bridge will check seat count against
  Supabase and refuse to send orders to an unlicensed screen. Enforcement has
  to live here rather than in the browser, where `localStorage` can be edited.
- Customer-facing pickup display (In Progress / Ready).
- Packaging as a Windows Service with an installer.
