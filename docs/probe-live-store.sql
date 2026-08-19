/* ============================================================
   BTX KDS — 真實門店庫探查腳本  (100% READ-ONLY，不改任何資料)
   ------------------------------------------------------------
   用途：回答 SPEC.md §7 待驗證清單，決定 Bridge 的主觸發源。
   跑法：SSMS 連上門店的 SQL Server，選 AED_<門店>_FNB_FE 這個庫，
         整段執行，把每一節的結果截圖或存 csv 回傳。
   注意：一定要選 **_FE 結尾** 的 frontend 庫，不是 backend 庫。
   ============================================================ */

PRINT '=== [0] 目前連的是哪個庫 ===';
SELECT DB_NAME() AS CurrentDB;

/* ---------- [1] 廚房頻道有哪些 ---------- */
PRINT '=== [1] PosPrinterSet 廚房頻道 ===';
SELECT PrinterSetKey, PrinterSetID, Description, IsActive,
       IsSeparateQty, IsEachRecordPerReceipt
FROM   PosPrinterSet
ORDER  BY PrinterSetKey;

/* ---------- [2] item→頻道 用的是哪一種粒度 ---------- */
PRINT '=== [2] 路由映射筆數（看客戶實際用哪一種）===';
SELECT 'PosPrinterSetItem'      AS MapTable, COUNT(*) AS Rows FROM PosPrinterSetItem
UNION ALL SELECT 'PosPrinterSetItemGroup', COUNT(*) FROM PosPrinterSetItemGroup
UNION ALL SELECT 'PosPrinterSetItemType',  COUNT(*) FROM PosPrinterSetItemType;

/* ---------- [3] ★最關鍵：FNBKitchenReceipt 是持久表還是瞬時隊列？ ---------- */
PRINT '=== [3a] FNBKitchenReceipt 總筆數 + 時間範圍 ===';
SELECT COUNT(*)        AS TotalRows,
       MIN(PrintDate)  AS EarliestPrint,
       MAX(PrintDate)  AS LatestPrint,
       SUM(CASE WHEN IsPrinted = 1 THEN 1 ELSE 0 END) AS PrintedRows,
       SUM(CASE WHEN IsPrinted = 0 THEN 1 ELSE 0 END) AS UnprintedRows
FROM   FNBKitchenReceipt;
/*  判讀：
    TotalRows = 0            → 打完即清的瞬時隊列 → Bridge 改用 [5] 的 PosOrderDtl 方案
    TotalRows 大 且時間跨多天 → 持久紀錄表 → 可直接當主觸發源（最理想）        */

PRINT '=== [3b] 最近 20 筆 KOT（看 Value 到底長什麼樣）===';
SELECT TOP 20 AutoKey, PrinterKey, DocNo, TableNo, PrintDate, IsPrinted,
       ComputerName, LEN(Value) AS ValueLength,
       LEFT(Value, 500) AS ValuePreview
FROM   FNBKitchenReceipt
ORDER  BY AutoKey DESC;

/* ---------- [4] 今天的實際單量（估算輪詢負載）---------- */
PRINT '=== [4] 今日訂單量 ===';
SELECT COUNT(*) AS TodayOrders, MIN(CreatedTime) AS FirstOrder, MAX(CreatedTime) AS LastOrder
FROM   PosOrder
WHERE  CreatedTime >= CAST(GETDATE() AS date);

PRINT '=== [4b] 有幾台 POS 前端在下單（決定 Bridge 裝哪）===';
SELECT ComputerName, COUNT(*) AS Rows
FROM   PosOrderDtl
WHERE  LastModified >= DATEADD(day, -7, GETDATE())
GROUP  BY ComputerName;
/*  註：若 PosOrderDtl 沒有 ComputerName 欄位就改查 PosOrderSession.ComputerName  */

/* ---------- [5] Printed / Served 這兩個旗標實際怎麼用 ---------- */
PRINT '=== [5] 近 7 天 Printed / Served 分佈 ===';
SELECT d.Printed, d.Served, COUNT(*) AS Rows
FROM   PosOrderDtl d
JOIN   PosOrder o ON o.DocKey = d.DocKey
WHERE  o.CreatedTime >= DATEADD(day, -7, GETDATE())
GROUP  BY d.Printed, d.Served
ORDER  BY d.Printed, d.Served;
/*  判讀：Served 全是 0 → 客戶沒在用「已上菜」，Phase 3 回寫空間大
          Served 有 1   → POS 端已有人在用，回寫要更小心            */

/* ---------- [6] 一張真實單的完整長相（KDS 卡片要顯示什麼）---------- */
PRINT '=== [6] 最近一張單的明細 ===';
SELECT TOP 1 DocKey, DocNo, OrderNo, TableNo, Guests, CreatedTime, LastModified,
       IsCompleted, IsPaid, Cancelled, Printed, Remarks
INTO   #lastdoc
FROM   PosOrder
ORDER  BY DocKey DESC;
SELECT * FROM #lastdoc;

SELECT d.Seq, d.ItemCode, d.Description, d.Qty, d.UOM,
       d.Remarks, d.Modifier, d.DtlType, d.IsFOC,
       d.Printed, d.Served, d.VoidQty, d.ReturnQty,
       d.SetMealDtlGuid, d.ParentDtlGuid, d.LastModified
FROM   PosOrderDtl d
JOIN   #lastdoc l ON l.DocKey = d.DocKey
ORDER  BY d.Seq;
DROP TABLE #lastdoc;

/* ---------- [7] 加料明細長相 ---------- */
PRINT '=== [7] 最近的加料紀錄 ===';
SELECT TOP 20 * FROM PosOrderDtlModifier ORDER BY 1 DESC;

/* ---------- [8] 退單 ---------- */
PRINT '=== [8] 近 7 天退單 ===';
SELECT TOP 20 * FROM PosVoidOrder ORDER BY 1 DESC;

PRINT '=== 探查結束（未修改任何資料）===';
