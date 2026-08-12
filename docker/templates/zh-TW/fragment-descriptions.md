## 寫描述

`inventory_annotate` 是你搞懂的東西唯一留得下來的地方。

要把一整個 database 補完的時候，**一頁一頁跑，不要一張表一張表跑**：

1. `inventory_columns(database=…, only_missing_description=True, include_profile=False, limit=200)`
   ——拿到一頁還沒有描述的欄位，這一頁會橫跨好幾張表
2. 依欄位名、型別、以及同一張表的其他欄位，推斷每一欄裝什麼
3. `inventory_annotate(database=…, containers=[…])` 一次把這一頁涉及的所有表寫回去
4. 把拿到的 `next_cursor` 傳回第 1 步，直到沒有 `next_cursor`

一張表叫一次 `inventory_columns`，在有幾百張表的來源上就是幾百次往返，而且每一次
都順便把你用不到的統計搬過來一趟。`include_profile=False` 就是為了這個。

兩個規則：

- 來源資料庫自己帶的描述不歸你覆蓋，server 把那些存在另一個欄位，掃描會更新它們。
  `only_missing_description=True` 也已經把它們排除掉了——那一頁裡的欄位是真的沒有
  人講過的。
- 你寫的描述會被記成 agent 的推測。把你是根據什麼推的講出來。不確定就在描述裡直接
  說不確定，不要留一句看起來很篤定的話讓之後的人拿去信。

每寫完一頁就回報一次進度，不要整批跑完才講話——中途被打斷的時候，已經寫進去的那
幾頁不會不見，而下一次從 `only_missing_description` 接手就是了。
