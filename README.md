# database-mcp-connector

一個 MCP server，讓 AI agent 讀得到資料來源的 catalog —— **有哪些表、哪些欄位、什麼
型別、代表什麼意思** —— 而不需要把資料庫帳密交給任何人。帳密只存在 server 的環境變
數裡。對資料來源全部是唯讀的。

支援 sqlite、postgres、mysql/mariadb、mssql、mongodb、datalake（parquet/csv/json），
以及把另一個 MCP server 當成來源。

> 本文的 `container` 一律指**來源裡的表 / view / collection**，不是 Docker 容器。

- [部署](#部署)
- [設定](#設定)
- [角色與身分](#角色與身分)
- [交給 agent](#交給-agent)
- [有哪些工具](#有哪些工具)
- [跑一次完整盤點](#跑一次完整盤點)
- [遇到問題](#遇到問題)
- [不用 Docker](#不用-docker)

---

## 部署

```bash
cp .env.example .env    # 編輯：engine、連線、要發哪些身分
docker compose up -d --build
```

就這樣。`.env` 裡的 `MCP_ENGINE` 同時決定 image 裝哪個驅動、server 用哪個 adapter、
以及 image 的 tag，所以你只要在那裡寫一次。

`--build` 只有在第一次、或改了 `MCP_ENGINE` 之後才需要。

起來的是兩個服務：

| 服務 | 生命週期 | 做什麼 |
|---|---|---|
| `provision` | 跑一次就結束 | 產生金鑰、簽 token、生出要交給 agent 的檔案 |
| `server` | 長駐 | 驗證 token，提供服務 |

`server` 等 `provision` 成功才會起來。簽章金鑰寫在一個 `server` 沒有掛載的 volume
上，所以就算 server 被攻下，攻擊者也沒辦法自己簽一張 token。

**server 會先真的連一次資料庫才開始服務**——連得到，而且帳密被接受——連不上就不啟
動。成功的話 log 裡會有一行：

```
connected to mssql as readonly@192.168.0.142:1433/shop: the address answers and the login is accepted
```

這是為了讓打錯的 host 或密碼在這裡就被發現，而不是等 agent 做到一半才從一個 tool 錯
誤裡看到。來源開機比 server 慢的話，設 `MCP_CONNECTION_CHECK=warn`（記一筆 log 然後
照常啟動）。

`provision` 每次 `up` 都會再跑，但只在「server 現在會拒絕手上這張 token」時才重簽
（金鑰換了、過期了、audience 改了），並且會說明原因。要無條件重簽：

```bash
PROVISION_FORCE=1 docker compose up provision
```

### 換 engine

換 image，不是換設定：

```bash
# .env: MCP_ENGINE=mssql
docker compose up -d --build
```

一個 engine 一個 image，tag 帶著 engine（`database-mcp-connector:mssql`）。驅動沒辦
法在 runtime 決定——mssql 的 ODBC 驅動不是 python 套件，而 image 是唯讀的、process
也不是 root。所以依賴在 build 時就照 `MCP_ENGINE` 裝好。

engine 和 image 對不上的時候 server **不會啟動**，錯誤訊息會直接說要用哪個
`MCP_ENGINE` 重 build。

> 你也可以寫成 `MCP_ENGINE=mssql docker compose up -d --build`，shell 的值會蓋過
> `.env`。但那樣**每次都得記得打**——漏一次就會退回預設的 sqlite，build 出另一個
> image、用錯的 adapter 起來。放在 `.env` 就是為了不用記。兩者擇一，不需要都做。

---

## 設定

全部在 `.env`。完整範例見 [.env.example](.env.example)。

### 來源

```bash
MCP_ENGINE=mssql
MCP_CONNECTION_REF=shop     # 下面那組變數的前綴
```

連線資訊住在你自己選的前綴底下：

```bash
SHOP_HOST=192.168.0.142
SHOP_PORT=1433
SHOP_USER=readonly
SHOP_PASSWORD=…
SHOP_DB=shop
```

認得的字尾：`_HOST` `_PORT` `_USER` `_PASSWORD` `_DB`/`_DATABASE` `_URI` `_PATH`
`_TOKEN` `_TRUST_SERVER_CERTIFICATE`。

| engine | 需要 | 也可以改用 |
|---|---|---|
| `sqlite` | `_PATH`（`.db` 檔） | `_URI` |
| `datalake` | `_PATH` 或 `_URI`（`s3://…`、`gs://…`） | |
| `postgres` | `_HOST` `_USER` `_PASSWORD` `_DB` | `_URI` —— 完整 libpq conninfo，也是唯一能接 unix socket 的方式 |
| `mysql` / `mariadb` | `_HOST` `_USER` `_PASSWORD` `_DB` | `_URI` |
| `mssql` | `_HOST` `_USER` `_PASSWORD` `_DB` | `_URI` —— 完整 ODBC 連線字串 |
| `mongodb` | `_URI` 或 `_HOST`，加 `_DB` | |
| `mcp` | `_URI` 或 `_PATH`，加 `_TOKEN` | |

檔案型的 engine（sqlite、datalake）要把來源掛進容器，`docker-compose.yml` 裡有註解
掉的範例。

### mssql 的自簽憑證

ODBC Driver 18 預設一定驗證憑證，所以地端伺服器第一次連會撞到
`certificate verify failed`。

```bash
SHOP_TRUST_SERVER_CERTIFICATE=1
```

**加密仍然開著**，關掉的只是「對方是不是它宣稱的那個人」這項驗證——也就是說這條連線
可以被中間人攔截，只適合你控制的網段。打開時 server 會在 log 裡講一次。

DBeaver、SSMS 這類客戶端預設就勾著「信任伺服器憑證」，所以它們連得過而且不會提這件
事。**用 GUI 連得上不代表這裡連得上**，兩邊的預設剛好相反。

先確認再改設定：

```bash
docker compose run --rm server test-connection
```

### 日誌

一個等級一個檔，寫在容器裡的 `/data/log/<日期>/<LEVEL>.log`，跟 staging 和稽核記錄
一起在 `mcp-data` volume 上。console 那份走 stderr，所以用 stdio transport 時
protocol 不會被日誌混進去。

```bash
docker compose exec server sh -c 'tail -f /data/log/$(date +%F)/ERROR.log'
```

預設保留 30 天，超過的整個日期目錄會在第一次寫 log 和跨日換檔時被刪掉：

```bash
LOG_RETENTION_DAYS=0     # 不刪
LOG_LEVEL=INFO           # DEBUG / INFO / WARNING / ERROR / CRITICAL
```

刪的只有 `log/` 底下名字剛好是 `YYYY-MM-DD` 的目錄。**稽核記錄不在裡面**——那是
`/data/audit/audit.jsonl`，`LOG_RETENTION_DAYS` 碰不到它。

這兩個變數只有 server 讀得到（`env_file` 只掛在 server 上）。`LOG_DIR` 不要從 `.env`
改：Dockerfile 已經把它指到 volume 上的 `/data/log`，改掉會寫進容器的可寫層，重建就
沒了。不用 Docker 跑的時候沒有這個變數，log 會落在專案根目錄的 `log/<日期>/`。

---

## 角色與身分

一個角色 = 一組權限。定義在 [docker/roles.toml](docker/roles.toml)，預設兩個：

| 角色 | 給誰 | 拿得到 |
|---|---|---|
| `pm` | 跟客戶對資料的人 | 讀盤點結果、看 schema、取遮罩過的樣本、匯出資料字典。**不能**啟動掃描、不能改盤點內容 |
| `de` | 做盤點交付的工程師 | `pm` 的全部，再加跑掃描、即時統計、補描述、匯出 `dbt schema.yml` |

`de` 是 `pm` 的**超集**——`roles.toml` 裡是 `extends = "pm"` 加 `add_tools`，所以
`pm` 叫得動的 `de` 一定也叫得動。逐個工具攤開是這樣（完整回傳內容見
[有哪些工具](#有哪些工具)）：

| 工具 | `pm` | `de` | 為什麼 |
|---|:--:|:--:|---|
| `list_databases` | ✅ | ✅ | 唯讀，只回名字 |
| `list_containers` | ✅ | ✅ | 唯讀，一次一頁 |
| `get_schema` | ✅ | ✅ | 唯讀，一張表的欄位 |
| `get_sample` | ✅ | ✅ | 唯讀，且**兩個角色都只拿得到遮罩過的值** |
| `profile_column` | ❌ | ✅ | 會讓來源當場做工（`costly`），不給對客戶的人 |
| `inventory_start` | ❌ | ✅ | 會讓來源做很久的工，而且寫入盤點 |
| `inventory_status` | ❌ | ✅ | 沒有 `inventory_start` 就沒有 job id 可問 |
| `inventory_cancel` | ❌ | ✅ | 同上；而且它會中斷別人的掃描 |
| `inventory_summary` | ✅ | ✅ | 讀盤點，幾百個位元組 |
| `inventory_containers` | ✅ | ✅ | 讀盤點 |
| `inventory_columns` | ✅ | ✅ | 讀盤點 |
| `inventory_search` | ✅ | ✅ | 讀盤點 |
| `inventory_relationships` | ✅ | ✅ | 讀盤點，「這兩張表怎麼接」 |
| `inventory_changes` | ❌ | ✅ | 兩次掃描之間的 diff，是盤點者在看的東西 |
| `inventory_annotate` | ❌ | ✅ | **唯一會寫入的工具**（寫盤點，不寫來源） |
| `inventory_export` | ✅ | ✅ | 只回傳路徑；`pm` 用 `markdown`，`de` 另外用 `dbt_yaml` |

不是工具、但一樣寫在 token 裡的三項，預設兩個角色都**沒有**：

| 設定 | 預設 | 打開之後 |
|---|---|---|
| `allow_raw_sample` | 關 | `get_sample(mask=False)` 才拿得到未遮罩的真值 |
| `annotate_as_human` | 關 | `inventory_annotate` 寫進去的描述記成人寫的，而不是 agent 猜的 |
| `databases` / `containers` | 不限制 | 限定只讀某幾個 database、或用 glob 擋掉某些表 |

```bash
docker compose run --rm provision role list       # 有哪些角色
docker compose run --rm provision role show de    # 展開繼承後的實際權限
```

要看**手上這張 token 實際帶了什麼**（上面那張表是設定檔，這是既成事實）：

```bash
python3 -c 'import base64,json,sys;b=open(sys.argv[1]).read().strip().split(".")[1];print(json.dumps(json.loads(base64.urlsafe_b64decode(b+"="*(-len(b)%4))),indent=1))' out/de/de.jwt
```

### 發身分

`.env` 裡是 `<名字>=<角色>`：

```bash
PROVISION_IDENTITIES=de=de,pm=pm,alice=pm
```

每個身分各自一組金鑰、一張 token、一包 `out/<名字>/`。補發一個人不用動 `.env`：

```bash
docker compose run --rm provision --kid bob --role pm
```

**撤銷**就是刪掉那把公鑰，不用重啟，30 秒內生效：

```bash
docker compose run --rm provision rm /keys/bob.pub
```

### 改權限

編輯 `docker/roles.toml`：

```toml
[roles.analyst]
extends    = "pm"                       # 繼承，再增減
add_tools  = ["profile_column"]
databases  = ["analytics"]              # 只能讀這幾個 database
containers = { deny = ["*_pii"] }       # 永遠讀不到；deny 贏過 allow
lifetime   = "7d"
```

工具名在載入時會比對真實的工具清單，打錯字直接報錯。可用的設定寫在
`roles.toml` 的檔頭。

### 重簽

`roles.toml` 和 SKILL.md 的範本都**烤在 image 裡**，所以改完要先 build 再重簽：

```bash
PROVISION_FORCE=1 docker compose up provision --build
```

`--build` 只有在改了 `docker/` 底下的東西之後才需要；只是想換一張新的 token 的話
`PROVISION_FORCE=1 docker compose up provision` 就夠了。（不想每次 build，把
`docker-compose.yml` 裡 `./docker/roles.toml:/etc/mcp/roles.toml:ro` 那行的註解拿
掉，roles.toml 就變成掛進去的。）

`PROVISION_FORCE` 的意思是「就算手上這張還驗得過也重簽一張」。**沒有它的時候
provision 不會重簽**——它只在 server 現在會拒絕手上這張 token 時才動手（金鑰換了、
過期了、audience 改了）。分得出來的地方在 log：

```
signing a token: PROVISION_FORCE is set          ← 重簽了
token /out/de/de.jwt still verifies …, keeping it ← 沒重簽，舊的還在
```

跑完之後：

1. `out/<名字>/` 整包**重新產生**，包含新的 `.jwt`、把 token 寫進去的 `.mcp.json`、
   和照新權限重寫的 `SKILL.md`。舊 token 不會被撤銷，只是沒人用了——要真的讓它失
   效，刪掉那把公鑰（見上面的[撤銷](#發身分)）。
2. **重新交付一次**。agent 手上那包是舊的，`cp -r out/de ~/.claude/plugins/de` 要再
   跑一次，然後重開 agent 讓它重讀。權限改了但沒重新交付，agent 會照舊的 SKILL.md
   做事，並且拿舊 token 去撞新的權限。
3. server **不用重啟**。它每 30 秒重讀一次公鑰目錄，而權限是寫在 token 裡的。

改的只是 SKILL.md 的文字、權限沒動的話，token 其實不用換，但重簽一張最省事——反正
整包都要重新交付。

---

## 交給 agent

`out/<名字>/` 整包就是一個身分——token、連線設定、使用說明都在裡面。

```
out/de/
  de.jwt                            憑證
  .mcp.json                         server 連線設定，token 直接寫在裡面
  skills/etl-agent-mcp/SKILL.md     這個角色能用什麼、該怎麼用
  .claude-plugin/plugin.json
  codex.toml                        同一個 server 的 Codex 寫法
  INSTALL.md
```

**整包是憑證**，不要進版控，交給誰就整包給誰。

Claude Code：

```bash
cp -r out/de ~/.claude/plugins/de
```

Codex：把 `codex.toml` 附加到 `~/.codex/config.toml`，並把 `SKILL.md` 當 context 傳
進去。

`SKILL.md` 是照那張 token 實際拿到的權限生成的，所以不會描述到這個角色叫不動的工
具。語言由 `PROVISION_LANG` 決定（`zh-TW` 或 `en`，預設 `zh-TW`）；工具名和參數名不
會被翻譯。

---

## 有哪些工具

**直接問來源**（永遠都在）。每一次呼叫都真的送一句 SQL 出去：

| 工具 | 主要參數 | 回傳什麼 |
|---|---|---|
| `list_databases` | 無 | 一個字串陣列。被 `databases` 限制的名字直接不出現在裡面 |
| `list_containers` | `database` `schema` `limit` `cursor` | `{containers[], next_cursor}`。每筆有 `database` `schema_name` `container_name` `container_type` `estimated_count`（概估列數）`native_description`（來源自己的表註解）`last_modified_at`。`next_cursor` 是 `null` 就是最後一頁 |
| `get_schema` | `container` `database` | 一個 `ColumnInfo` 陣列：`name` `ordinal` `native_type` `nullable` `is_pk` `is_fk` `native_description` `references_container` / `references_column`（外鍵指到哪） |
| `get_sample` | `container` `limit=3` `mask=True` | 幾筆 row 的 dict 陣列。`limit` 會被 `MCP_MAX_SAMPLE_LIMIT`（預設 100）夾住。個資欄位是 `a***@***.com`、`***`；`mask=False` 沒有授權的話**直接報錯**，不是靜靜回遮罩值 |
| `profile_column` | `container` `column` `mode` | 一個 `ProfileResult`，只填 `mode` 要的那一項：`distinct_count` / `null_ratio` / `top_values[]` / `min_value` / `max_value`。`approximate=true` 代表來源沒有掃完整份資料 |

**讀盤點結果**（`MCP_STAGING_DB` 設了才有，compose 預設有）。除了 `inventory_start`
的背景工作以外，這些**完全不碰來源資料庫**，讀的是 staging 裡上次掃描記下來的東西：

| 工具 | 主要參數 | 回傳什麼 |
|---|---|---|
| `inventory_start` | `database` `profile_modes` `force=False` `resume=True` | **一個 job id 字串，就這樣**。掃描還在背景跑。同一個 database 已經有掃描在跑會報 `ScanAlreadyRunningError` |
| `inventory_status` | `job_id` | `{job_id, database, state, containers_done, containers_failed, containers_skipped, cursor, error, started_at, finished_at}`。`state` 是 `running` / `done` / `failed` / `cancelled` |
| `inventory_cancel` | `job_id` | `true` / `false`。手上這張表跑完才停，已經做的進度留著 |
| `inventory_summary` | `database` | `{database, containers, containers_failed, estimated_rows, columns, columns_profiled}`。**幾百個位元組，先問這個**。全 0 代表還沒盤點過，不是「你沒有權限」 |
| `inventory_containers` | `database` `limit=100` `cursor` | `{containers[], next_cursor}`，每筆帶著它最後一次的掃描狀態 |
| `inventory_columns` | `container` `database` `schema` `limit` `cursor` `include_profile=True` | `{columns[], next_cursor}`，依 `ordinal` 排。每筆除了型別與鍵，還有 `description` / `description_source`（`agent` 還是人寫的）/ `sensitivity` / `profile`。寬表用 `include_profile=False`，統計佔的量比欄位本身多 |
| `inventory_search` | `keyword` `database` `kind` `limit` | `SearchHit[]`：`container_name` `column_name`（表本身命中時是 `null`）`native_type` `description` `match_in`（命中在名稱還是描述）。**刻意不帶統計**，所以一百筆也很便宜 |
| `inventory_relationships` | `database` | `Relationship[]`：`from_container` `from_column` → `to_container` `to_column`。夠直接畫 ER 圖 |
| `inventory_changes` | `database` `since` `limit` | `SchemaChange[]`，新的在前：`change_type`、`detail`（新增／刪除／改型別的欄位名）、`detected_at` |
| `inventory_annotate` | `database` `container` `container_description` `columns[]` | `{containers_updated, columns_updated, unknown_columns[]}`。**唯一會寫的工具，而且只寫盤點**。沒填的欄位保持原狀，填空字串是清掉。盤點裡不存在的欄位名會回在 `unknown_columns`，不會被吞掉 |
| `inventory_export` | `format` `database` `path` | `{path, bytes_written, containers, columns}`——**只有路徑，永遠不是內容**。`markdown`（給人讀的資料字典）/ `csv`（一列一欄位）/ `dbt_yaml`（dbt 的 `schema.yml`）。`path` 相對於 export 目錄，出不去 |

典型的交付流程：`inventory_start` 跑一次掃描 → `inventory_annotate` 把看懂的東西寫
回去 → `inventory_export(format="dbt_yaml")` 產出 `schema.yml`。之後 PM 讀的就是這
份盤點結果，不會再去打資料庫。

`get_sample` 預設把個資遮罩成 `a***@***.com`、`***`——形狀留著，值不留，因為取樣出
來的資料會進 agent 的 context 並留在每一份對話記錄裡。要看真值需要
`--allow-raw-sample` 那項授權，預設兩個角色都沒有。

postgres 和 mssql 的 container 名字帶 schema（`dbo.orders`）；只有一個 schema 有那
張表的話，寫 `orders` 也可以。

---

## 跑一次完整盤點

`inventory_start` **一次掃一個 database**——`database` 是單數，沒有「全部」這個值。
所以「先盤出所有資料庫」是兩步，需要 `de`（`pm` 沒有 `inventory_start`）：

```
list_databases()                        → ["shop", "MSSQL2019_VMData_COLA", …]

# 每個 database 各叫一次，各自拿到一個 job id
inventory_start(database="MSSQL2019_VMData_COLA")   → "9f3c…"
inventory_start(database="shop")                    → "1ab7…"

inventory_status(job_id="9f3c…")        → state 從 running 到 done
```

不同 database 可以同時跑；**同一個** database 重開會被擋（`ScanAlreadyRunningError`），
因為沒跑完的掃描本來就會從自己的 cursor 接著跑，沒變動的表會跳過。掃描慢不是理由，
重開只會繞遠路到同一個地方。要重新掃已經掃過而且沒變的表才用 `force=True`。

掃完之後 `inventory_summary` 的數字才會動，`inventory_search` /
`inventory_relationships` / `inventory_export` 才有東西可讀——在那之前它們回空的是
**盤點是空的**，不是權限問題。

給 agent 下這件事的時候，把「先 `list_databases`，再對每個 database 各跑一次
`inventory_start`」講出來。它讀到的 `SKILL.md` 會提醒它掃描很貴，模型有時候會把
「應該謹慎」理解成「我不被允許」，然後**連試都不試就宣稱自己沒權限**。

### 產出落在哪，怎麼取出來

`inventory_export` 只回傳路徑，檔案本身寫在容器裡的 `MCP_EXPORT_DIR`
（compose 預設 `/data/export`）。整個 `/data` 在 `mcp-data` 這個 volume 上——包含
staging、稽核記錄和匯出——所以容器重建不會弄丟它，但檔案不會自己出現在專案目錄裡。

取一個檔案：

```bash
docker compose cp server:/data/export/inventory.md ./export/
```

`export/` 已經在 `.gitignore` 裡：那是交付物，而且內容是客戶資料庫的表名與欄位名，
要不要進版控是客戶的決定。

看稽核記錄不用取出來：

```bash
docker compose exec server tail -f /data/audit/audit.jsonl
```

**server 沒在跑也拿得到。** volume 是獨立的，用一個丟掉的容器掛上去就好——image 壞
掉、compose 整組停掉都不影響：

```bash
docker run --rm -u "$(id -u):$(id -g)" \
  -v database-mcp-connector_mcp-data:/data -v "$PWD:/out" \
  alpine cp /data/export/inventory.md /out/
```

整包備份：

```bash
docker run --rm -v database-mcp-connector_mcp-data:/data -v "$PWD:/out" \
  alpine tar czf /out/mcp-data.tgz -C /data .
```

volume 全名是 `<專案名>_mcp-data`，專案名來自 `docker-compose.yml` 的 `name:`；
`docker volume ls` 可以確認。

> 沒有把 `/data` 或它底下任何一層 bind mount 到 host，是刻意的。容器裡的 process 是
> `uid 10001`，在 macOS 上 Docker Desktop 會把 host 目錄的 ownership 蓋掉所以寫得進
> 去，同一份 compose 換到 Linux 就是 `EACCES`——那會變成這份設定裡唯一「本機過、上線
> 炸」的東西。另外 `staging.db` 是開著 WAL 的 SQLite，檔案鎖跨 bind mount 不可靠；
> `audit/` 是稽核記錄，host 改得動的記錄不能拿來當證據。
>
> 代價是 `docker compose down -v` 會把 volume 一起帶走，**匯出的檔案也在裡面**。交付
> 物取出來再跑那個指令。

---

## 遇到問題

**`token 格式不對` / server 回 401**
把 token 手動貼進 `.mcp.json` 如果就通了，代表是 `${...}` 沒被展開。預設已經是直接
寫入 token，所以會遇到這個通常是設了 `PROVISION_INLINE_TOKEN=0`——那個寫法要求
client 會展開變數，而且啟動它的 shell 就是你 `export` 的那個 shell；從桌面應用程式
啟動的 client 兩個都不成立。

**`this image was not built for <engine>`**
`.env` 的 `MCP_ENGINE` 和 image 對不上。`docker compose up -d --build`。

**`cannot reach …` 而且 server 起不來**
啟動時的連線檢查擋下來了，後面接著的就是真正的原因（見下面幾條）。

server 沒起來就 `exec` 不進去，所以排查用這個——它在一個用完就丟的容器裡跑，設定跟
server 完全一樣，但不服務任何東西：

```bash
docker compose run --rm server test-connection
```

它回報的是**登入**成功與否，不是只有 ping 到 IP。要先讓 server 起來再慢慢查，設
`MCP_CONNECTION_CHECK=warn`。

**`Login timeout expired (HYT00)`**
連不到。容器裡的 `localhost` 是容器自己——資料庫跑在 docker host 上的話要用
`SHOP_HOST=host.docker.internal`。

**`Login failed for user (28000)`**
連得到，但帳密或預設資料庫不對。網路沒問題。

**`certificate verify failed`**
自簽憑證。`SHOP_TRUST_SERVER_CERTIFICATE=1`，見
[mssql 的自簽憑證](#mssql-的自簽憑證)。**DBeaver 連得過不代表這裡連得過**——它預設
就勾著 "Trust server certificate"，而且不會告訴你。

**`InvalidSignatureError`**
`docker compose down -v` 會帶走金鑰的 volume，但留下 bind mount 的 `./out`——手上那
張 token 是用已經不存在的金鑰簽的。重跑 `docker compose up provision` 會偵測到並重
簽。

**agent 說某個工具被拒絕**
先確認**它真的被拒絕過**。權限是在 server 裡擋的，擋下來一定會留一筆稽核記錄：

```bash
docker compose exec server sh -c 'tail -40 /data/audit/audit.jsonl'
```

看那個 `key_id` 有沒有一筆 `tool` 是它說被擋的那個工具、`outcome` 不是 `ok`。

- **有**——那是設定，不是故障。`docker compose run --rm provision role show <角色>`
  看它實際拿到什麼。
- **沒有那筆**——它根本沒呼叫過，這句「我沒有權限」是它自己編的。模型讀了
  `SKILL.md` 裡「掃描很貴」「取樣會進 transcript」那些話，有時候會歸納成「我大概不
  該做」再說成「我不被允許」。直接叫它去呼叫那個工具；真的沒權限的話它會拿到一句
  明確的錯誤，那才是答案。

要看 token 本身帶了哪些 scope，見[角色與身分](#角色與身分)最後那段。

**同一台機器上裝了好幾包身分**
每一包的 skill 名字和 MCP server 名字都是 `MCP_SERVER_NAME`（預設 `etl-agent-mcp`），
所以 `out/de` 和 `out/pm` 裝在一起會互相蓋掉，舊的 `out/<名字>/` 留著也一樣。要並存
就用不同的 `MCP_SERVER_NAME` 各發一包，不然**同一時間只裝一包**，換身分之前先把上一
包刪掉。稽核記錄的 `key_id` 是判斷「現在用的到底是哪一張 token」最準的地方。

---

## 不用 Docker

```bash
uv sync --extra server --extra postgres
export SHOP_HOST=db.internal SHOP_USER=readonly SHOP_PASSWORD=… SHOP_DB=shop
uv run mcp-connector --engine postgres --connection-ref shop --staging-db ./var/staging.db
```

`--extra server` 是 MCP 那層，每個 engine 的驅動各自一個 extra（`postgres`、`mysql`
含 mariadb、`mssql`、`mongo`、`datalake`、`mcp`）。sqlite 不用裝。mssql 的 ODBC 驅動
本身不是 python 套件，要另外裝。對照表在
[src/core/engines.py](src/core/engines.py)。

每個設定都有一個 flag 和一個 `MCP_*` 環境變數，flag 優先：

| flag | 環境變數 | 預設 |
|---|---|---|
| `--engine` | `MCP_ENGINE` | `sqlite` |
| `--connection-ref` | `MCP_CONNECTION_REF` | *必填* |
| `--database` | `MCP_DATABASE` | engine 自己的預設 |
| `--transport` | `MCP_TRANSPORT` | `stdio` |
| `--host` / `--port` | `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` |
| `--max-sample-limit` | `MCP_MAX_SAMPLE_LIMIT` | `100` |
| `--connection-check` | `MCP_CONNECTION_CHECK` | `require` —— 也可以是 `warn` / `off` |
| `--staging-db` | `MCP_STAGING_DB` | 未設 —— **沒有 inventory 工具** |
| `--export-dir` | `MCP_EXPORT_DIR` | 未設 —— **沒有 export 工具** |
| `--audit-log` | `MCP_AUDIT_LOG` | 未設 —— 只寫 log |
| `--audit-max-mb` / `--audit-backups` | `MCP_AUDIT_MAX_MB` / `MCP_AUDIT_BACKUPS` | `10` / `5` |
| `--profile-mode` | `MCP_PROFILE_MODES` | 未設 —— 每個欄位各自決定 |
| `--no-profile` | `MCP_PROFILE=false` | 預設會收集統計 |
| `--server-name` | `MCP_SERVER_NAME` | `etl-agent-mcp` |
| `--require-auth` | `MCP_REQUIRE_AUTH` | `false` |
| `--authorized-keys-dir` | `MCP_AUTHORIZED_KEYS_DIR` | 未設 |
| `--audience` | `MCP_AUDIENCE` | `etl-agent-mcp` |
| `--allow-insecure-http` | `MCP_ALLOW_INSECURE_HTTP` | `false` |
| | `MCP_ROLES_FILE` | 容器裡是 `/etc/mcp/roles.toml` |

走 `stdio` 時沒有 token 也不需要——能啟動這個 process 的人已經握有資料庫帳密，所以
`--require-auth` 在 stdio 上會直接報錯。走網路 transport 時，除非綁在 loopback 或明
確加上 `--allow-insecure-http`，否則不會在沒有認證的情況下對外服務。

image 本身也可以當 CLI 用：

```bash
docker compose run --rm provision token issue --key … --role pm
```

每一次呼叫——誰、哪個工具、什麼參數、跑出來的 SQL、回傳幾筆——都會進稽核記錄
（`MCP_AUDIT_LOG`，compose 預設寫在 `mcp-data` volume 的 `audit/audit.jsonl`）。
