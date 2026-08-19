# BTX KDS — AutoCount F&B Kitchen Display System

> 產品規格書 · BT xTech Sdn Bhd
> 建立日期：2026-08-19
> 狀態：**規劃期（Phase 1 未開工）**

---

## 1. 產品定位

把 AutoCount FnB 5.2 現有的「廚房單靠 kitchen printer 打紙條」流程，
替換成 **Android 廚房顯示屏（KDS）**：訂單即時推到對應廚房站點的螢幕，
廚師做好按「完成」、取消按「取消」，另有一台顧客取餐叫號屏。

- **定位：BT xTech 正式商品**，賣給裝了 AutoCount FnB POS 的餐飲客戶
- 第一天就具備：Supabase license 控制、多店配置化、安裝包
- **Phase 1 對 AutoCount 完全 read-only**，不寫入任何一個 AutoCount 欄位

### 為什麼值得做
- 省紙條、省打印機耗材與卡紙維修
- 廚房不會漏單（紙條會掉、會被油弄濕）
- 出餐計時 → 可量化廚房效率，這是紙條做不到的
- AutoCount 原生沒有 KDS → 差異化賣點

---

## 2. 實地偵查結果（2026-08-19 於用戶本機驗證）

> ⚠️ 這一節是本專案最重要的技術資產。以下為**實際查詢用戶機器所得**，非文件推測。

### 2.1 環境
| 項目 | 值 |
|---|---|
| 安裝路徑 | `C:\Program Files (x86)\AutoCount\FnB 5.2` |
| 架構 | 32-bit host（plugin 需 net48 AnyCPU） |
| DevExpress | v22.2 |
| Backend DB | `AED_BT_FNB` |
| Frontend DB | `AED_BT_FNB_FE` ← **門店實際跑單的庫，KDS 的資料來源** |
| SQL Server | `.\SQL2019`（Windows auth 可連） |

### 2.2 廚房「頻道」= PosPrinterSet

用戶口中的「Kitchen 頻道」在 DB 就是 `PosPrinterSet`。測試庫現有兩個：

| PrinterSetKey | PrinterSetID | Description |
|---|---|---|
| 236 | FOOD | FOOD |
| 249 | DRINK | DRINK |

`PosPrinterSet` 欄位：
`PrinterSetKey (bigint)`, `PrinterSetID`, `Description`, `IsActive`,
`IsSeparateQty`, `LastUpdate`, `Guid`, `IsEachRecordPerReceipt`

**item → 頻道 的路由映射表**（三種粒度，AutoCount 自己算好，我們不用重算）：
- `PosPrinterSetItem`（逐個 item）
- `PosPrinterSetItemGroup`（按 item group）
- `PosPrinterSetItemType`（按 item type）

> 這三張表在 backend `AED_BT_FNB` 和 frontend `AED_BT_FNB_FE` **都存在**。

### 2.3 KOT 隊列表 `FNBKitchenReceipt`（最佳掛載點）

| 欄位 | 型別 | 說明 |
|---|---|---|
| `AutoKey` | bigint | 自增，可當水位線 |
| `PrinterKey` | bigint | → `PosPrinterSet.PrinterSetKey`，即頻道 |
| `Value` | nvarchar | 打印內容（**格式待驗證**） |
| `DocNo` | d_DocNo | → `PosOrder.DocNo` |
| `PrintDate` | datetime | |
| `IsPrinted` | d_Boolean | |
| `UserID` | d_PosUserID | 下單員工 |
| `TableNo` | nvarchar | 桌號 |
| `FromTableNo` | nvarchar | 轉桌來源 |
| `ComputerName` | nvarchar | 哪台 POS 前端下的單 |
| `IsOnlyPrintToAlternatePrinter` | d_Boolean | |
| `IsFromReservation` | d_Boolean | |

> ⚠️ 測試庫此表 **0 筆**。必須在真實門店庫確認它是「持久紀錄」還是「打完即清的瞬時隊列」。
> 這一條決定主觸發源是它，還是退回 2.4 的方案。

### 2.4 訂單表

**`PosOrder`（單頭）** 關鍵欄位：
`DocKey`, `DocNo`, `OrderNo`, `TableNo`, `JoinTableNo`, `Guests`,
`Remarks`, `CreatedTime`, `LastModified`, `UserID`, `TerminalID`,
`IsCompleted`, `IsPaid`, `Cancelled`, `Printed`, `ServiceType`, `Guid`

**`PosOrderDtl`（單身）** 關鍵欄位：

| 欄位 | KDS 用途 |
|---|---|
| `DtlKey`, `DocKey`, `Seq` | 主鍵 / 排序 |
| `ItemCode`, `Description`, `Qty`, `UOM` | 菜名數量 |
| **`Printed` (d_Boolean)** | **★ 是否已送廚房 → 推送觸發信號** |
| **`Served` (d_Boolean)** | **★ AutoCount 原生「已上菜」欄位 → Phase 3 回寫目標** |
| `Remarks` | 單行備註（「少辣」「走冰」） |
| `Modifier`, `ModifierAmt`, `ModifierKeyGuid` | 加料 |
| `VoidQty`, `ReturnQty`, `ReturnedQty` | 退菜 |
| `IsFOC` | 招待 |
| `DtlType` | "N"=一般行 |
| `SetMealDtlGuid`, `PackageGuid`, `ParentDtlGuid` | 套餐/組合結構 |
| `LastModified`, `LastModifiedUserID` | 增量抓取水位線 |
| `TableNo` | 單身也帶桌號 |
| `Guid` | 穩定識別碼 |

**其他相關表**：
`PosOrderDtlModifier`（加料明細）、`PosOrderSession`、`PosOrderKIV`（掛單）、
`PosTable`、`PosTableStatusColor`、`PosVoidOrder`（退單）、
`PosWebOrder*`（線上單）、`FNBeWaiterOrderSlipPrinter`

### 2.5 已排除的方案

| 方案 | 為何不採用 |
|---|---|
| 虛擬打印機驅動攔截 | 要裝驅動，脆弱 |
| 假裝成 TCP 9100 網路打印機 | 拿到的是 ESC/POS 純文字，無 ItemCode、無結構、報表格式一改就爛 |
| **直接讀 DB（採用）** | AutoCount 已把頻道路由算好寫進庫，拿到完整結構化資料 |

### 2.6 順帶發現（與 KDS 無關但有商業價值）

`AutoCount.FnbSelfOrder.IntegrationSDK.dll` — 官方**入向**自助點餐 SDK
（顧客掃碼點餐 → 推單進 POS）。含 `OrderService`、`MenuService`、
`ApproveOrderInputModel`、`GenerateQRCodeInputModel` 等。
→ 對 KDS 無用（方向相反），但**未來做掃碼點餐產品可直接用官方 SDK**，記錄備查。

---

## 3. 系統架構

```
┌─────────────┐  下單 / 加菜 / 退菜
│ FnB POS 5.2 │──────────┐
└─────────────┘          ▼
                 AED_<x>_FNB_FE   (READ-ONLY)
                         │  輪詢 ~1s，水位線 AutoKey / LastModified
                         ▼
              ┌──────────────────────────┐
              │   BTX KDS Bridge          │  Windows Service
              │   · 讀單 + 依頻道分派      │  裝店內 POS 主機
              │   · 自有狀態庫(SQLite)     │  不碰 AutoCount
              │   · REST + WebSocket      │
              └──────────────────────────┘
                    │ WebSocket (店內區網)
     ┌──────────┬───┴──────┬──────────────┐
     ▼          ▼          ▼              ▼
 Android盒子  Android盒子 Android盒子   電視/顯示屏
 [FOOD 站]   [DRINK 站]  [出餐站]      [顧客叫號屏]
```

**核心原則：Bridge 裝店內，不上雲。** 斷網廚房照跑。

---

## 4. 組件規格

### 4.1 BTX KDS Bridge（Windows Service，.NET 8）

職責：
1. 以 read-only 連線輪詢 AutoCount FnB frontend DB
2. 依 `PosPrinterSet` 把菜品分派到對應站點
3. 維護 KDS 自有狀態（pending / cooking / done / recalled）於本地 SQLite
4. 對外提供 REST（設定、歷史）+ WebSocket（即時推送）
5. Supabase license 驗證

**觸發源策略（依 §2.3 驗證結果二選一）**
- 主案：輪詢 `FNBKitchenReceipt` 新增列（AutoCount 已算好頻道）
- 備案：輪詢 `PosOrderDtl` where `LastModified > 水位` 且 `Printed = 1`，
  自行 join `PosPrinterSetItem/ItemGroup/ItemType` 推導頻道
- 加速（Phase 2 選配）：FnB POS plugin 於下單後發本地 ping，Bridge 立即抓取

**必須處理的訂單變更**
| 情境 | 行為 |
|---|---|
| 新單 / 加菜 | 新卡片 + 響鈴 |
| 改數量 | 卡片更新 + 黃色高亮 |
| 退菜 / Void | **紅色高亮 + 警示音**，不可靜默消失 |
| 轉桌 | 卡片桌號更新（`FromTableNo`） |
| 掛單 KIV | 卡片標記暫停 |
| 套餐 | 依 `SetMealDtlGuid` 群組顯示，不拆散 |

### 4.2 KDS Station App（Android：Kotlin 殼 + WebView）

**殼負責**（原生，做好基本不動）：
- 開機自啟（BOOT_COMPLETED）
- Kiosk 鎖定，員工退不出去
- 前台服務 + WakeLock → 螢幕常亮不被系統殺
- **音效：`SoundPool` 走鬧鐘音量通道**（廚房吵，媒體音量不夠）
  - 新單：提示音
  - 未 bump 逾時：每 30 秒重響，逐級升級成急促警報
- WebSocket 斷線自動重連 + 離線快取最後狀態
- （選配）USB bump bar 鍵盤映射

> 選 Kotlin 殼的決定性理由：**Chrome autoplay policy 會讓純 PWA 在無人觸碰螢幕時完全不出聲**，
> 廚房盒子開機自啟後沒人點屏 → 新單不響 = 產品報廢。原生音效不受此限。

**WebView 內的 UI 負責**（網頁，改版刷新即可，不用重裝 APK）：
- 訂單卡片：桌號 / 單號 / 下單時間 / **計時器（綠→黃→紅）**
- **逐項 bump**（單道菜完成）+ **整單 bump**
- **Recall 召回**（誤按撈回，KDS 必備）
- **All Day view**（今日各菜品累計待做數）
- 備註 / 加料醒目顯示
- 站點切換（一台盒子可綁一或多個 PrinterSet）

### 4.3 顧客取餐叫號屏
- 同一 Bridge 的唯讀頁面，Android 盒子接電視，全螢幕瀏覽器即可（不需觸控）
- 顯示：製作中 / 可取餐 兩欄，新完成的號碼閃爍 + 提示音

### 4.4 Admin 設定台（網頁）
- 站點 ↔ PrinterSet 綁定
- 逾時門檻（黃/紅秒數）、音量、字體大小
- 帳套 / 連線設定（**全部 DB 驅動，不寫死**）
- License 啟用
- 出餐效率報表（平均出餐時長、逾時率、按時段/菜品）

---

## 5. 分期

| 期 | 內容 | 驗收 |
|---|---|---|
| **P1** | Bridge + 單一 FOOD 站 + bump/recall + 計時器 + Kotlin 殼（音效/Kiosk/自啟） | 一台盒子在真實門店跑一週不漏單 |
| **P2** | 多頻道多站點、All Day view、顧客叫號屏、退菜警示、Admin 設定台、安裝包 | 可交付客戶自行安裝 |
| **P3** | 回寫 `PosOrderDtl.Served`（POS 端看得到已上菜）、出餐效率報表、Supabase license 正式上線、老闆手機告警（WhatsApp/Telegram） | 商品化完成 |

**P1 堅持完全 read-only**：廚房是餐廳最不能出事的環節。
先讓它跑一個月，證明穩了再談回寫。出問題隨時拔掉退回打印機。

---

## 6. License（依公司強制規範）

- 沿用既有 Supabase 統一授權體系（見 memory `btx_license_system_master_guide`）
- 授權維度建議：**按門店 + 按 KDS 站點數**
- Trial 30 天，狀態存雲端（防重裝刷 trial），到期完全鎖死
- Bridge 啟動時 + 每日驗證；**斷網時容許寬限期（廚房不能因為網路掛掉停擺）**

---

## 7. 待現場驗證清單（真實門店庫）

1. **`FNBKitchenReceipt` 是持久表還是打完即清的瞬時隊列？** → 決定主觸發源
2. **`FNBKitchenReceipt.Value` 的實際格式**（純文字？XML？序列化物件？）
3. **改數量 / 退菜時 AutoCount 如何補打 KOT** → 決定改單邏輯
4. **門店是單機還是多 POS 前端**（`PosOrderDtl.ComputerName` 顯示支援多終端）→ Bridge 裝哪、前端庫同步延遲
5. `PosOrderDtl.Printed` 在「送單」瞬間是否確實由 0 → 1
6. 套餐 / 加料在 KOT 上的呈現方式（`SetMealDtlGuid`、`PosOrderDtlModifier`）
7. 真實庫的 `PosPrinterSet` 有幾個頻道、item 映射用哪一種粒度

---

## 8. 硬體建議

| 項目 | 建議 |
|---|---|
| 廚房屏 | Android 觸控一體機 15"（優於盒子+螢幕，少一組線） |
| 防護 | 廚房油煙 → 加保護膜或選 IP 防護機型 |
| 網路 | **有線優先**，WiFi 備援 |
| 叫號屏 | 一般電視 + Android 盒子即可（不需觸控） |
| Bridge 主機 | 現有 POS 主機即可，不需另購 |

---

## 9. Repo 結構（規劃）

```
btx-kds/
├── bridge/          # .NET 8 Windows Service
├── web/             # KDS UI + 叫號屏 + Admin（WebView 載入）
├── android/         # Kotlin 殼
├── docs/            # 現場驗證紀錄、DB 探查結果
└── setup/           # Inno Setup 安裝包
```
