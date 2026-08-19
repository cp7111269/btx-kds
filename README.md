# BTX KDS

Kitchen Display System for **AutoCount FnB POS 5.2** — replaces kitchen
printer slips with Android kitchen screens (bump / recall / order timers)
plus a customer pickup display.

**BT xTech Sdn Bhd** · support@btxtech.my

- Bridge: .NET 8 Windows Service, reads the AutoCount FnB frontend DB **read-only**
- Station app: Kotlin shell + WebView (native sound / kiosk / boot-start)
- Deployment: in-store LAN only — the kitchen keeps running when the internet is down

> Full product spec, verified database findings and phasing: **[SPEC.md](SPEC.md)**

Status: **planning — Phase 1 not started**
