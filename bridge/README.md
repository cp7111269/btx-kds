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

## Admin page

```
http://localhost:5180/admin/          (or http://<this-pc-ip>:5180/admin/)
```

Deliberately a web page rather than a separate utility: nothing extra to
install or keep updated, and the owner can open it from a phone or the office
PC instead of standing at the POS.

It shows what is actually happening - orders in the window, how many were
hidden as completed, how many reached the screens, unrouted items by name - and
says the useful thing rather than leaving someone to infer it. If every order is
completed and nothing is reaching the screens, it says so and points at the
setting to change.

From it you can:

- change the order rules, saved and applied live with no restart
- see every screen that has registered, and release a seat
- enter the licence key
- set an admin PIN

The PIN guards only the admin page; kitchen and pickup screens keep working
without it. An empty PIN is allowed - fine while testing - but the page says
loudly that anyone on the shop network can change these settings, so set one
before handover.

Settings written from here are merged back into `config.json` with every
explanatory `_note` key preserved.

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
| `GET /api/admin/state` | everything the admin page shows (PIN-guarded) |
| `POST /api/admin/config` | change settings, validated and applied live |
| `POST /api/admin/release` | free a screen's seat |
| `POST /api/admin/licence` | store the licence key |
| `POST /api/admin/pin` | set or clear the admin PIN |
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

## The three screens

| | | |
|---|---|---|
| **Kitchen Display** | `/kds/` | Kitchen or bar. Interactive - tick dishes, DONE, recall. One screen serves one station. Takes a **kitchen seat**. |
| **Order Display** | `/display/` | Counter TV facing customers. Read-only, no cursor, numbers only. Takes a **display seat**. |
| **Counter Panel** | `/counter/` | Phone or small tablet at the till. One list, one big READY button per order. **Takes no seat.** |

A licence key covers a whole shop and carries an allowance of each seat kind -
it is not one key per device.

### Why the Counter Panel exists and is free

An Order Display only *shows* what someone else declared finished; it cannot
declare it. In a shop with no Kitchen Display - a bubble tea counter where the
drinks are made two feet from the till - nothing would ever press DONE and READY
would never light up. The Counter Panel is that button.

It therefore takes no seat: an Order Display is unusable without something to
press, and charging for the button as well would be charging twice for one
working system.

It is deliberately weaker than a Kitchen Display - no per-dish ticking, no
stations, no All Day view - so a shop that needs those still needs Kitchen
Displays. `/api/ready` clears every station and every round of a ticket at once;
`/api/unready` reverses it, because staff who cannot undo a miscall stop
pressing the button at all.

## Advertising on the Order Display

The board is a television showing three numbers most of the day. Filling the
rest with the shop's own promotions is what turns "a number screen" into "a sign
that earns" - which is the argument for buying a second one.

Set an image folder in the admin page: a local path or a Windows share. Drop
JPG/PNG/GIF/WEBP files in and they appear. Nothing is uploaded into this
software, because a shop owner will do that once and never again; dropping a
file into a folder they already have is a step they will actually repeat.

Four layouts, and **orders always win the screen**:

| | |
|---|---|
| `none` | no advertising |
| `side` | orders left, advert panel on the right third |
| `bottom` | banner strip under the orders |
| `idle` | full screen whenever nothing is waiting; orders take over instantly |

`idle_only` additionally hides a side or bottom advert while any order is on the
board.

**If the Bridge runs as a Windows service, its service account needs access to
the share.** This is the likeliest thing to go wrong, so the admin page reports
it in words - "Permission denied", "Folder not found or not reachable" - rather
than showing an empty panel and leaving someone guessing.

Images are served only from the configured folder, by basename, with traversal
refused.

## Shop mode

`mode` in the admin page - `restaurant`, `counter` or `full` - only drives what
the admin page recommends and which screen addresses it lists. Every screen
keeps working whatever is set, so a wrong choice is never a broken system.

## Not done yet

- Licence enforcement. Everything it needs is in place: screens register with a
  stable ID and their kind, seats are counted per kind, and the admin page has
  the key field and a release button. What remains is validating the key against
  Supabase and refusing to serve an unlicensed screen. That check has to live in
  the Bridge rather than the browser, where `localStorage` can be edited.
- Packaging as a Windows Service with an installer.
