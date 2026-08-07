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

---

## 角色與身分

一個角色 = 一組權限。定義在 [docker/roles.toml](docker/roles.toml)，預設兩個：

| 角色 | 給誰 | 拿得到 |
|---|---|---|
| `pm` | 跟客戶對資料的人 | 讀盤點結果、看 schema、取遮罩過的樣本、匯出資料字典。**不能**啟動掃描、不能改盤點內容 |
| `de` | 做盤點交付的工程師 | `pm` 的全部，再加跑掃描、即時統計、補描述、匯出 `dbt schema.yml` |

```bash
docker compose run --rm provision role list       # 有哪些角色
docker compose run --rm provision role show de    # 展開繼承後的實際權限
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

編輯 `docker/roles.toml`，然後重簽：

```bash
PROVISION_FORCE=1 docker compose up provision
```

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

**直接問來源**（永遠都在）：

| 工具 | 回傳 |
|---|---|
| `list_databases` | 這條連線碰得到的資料庫 |
| `list_containers` | 一頁的表 / view / collection |
| `get_schema` | 某個 container 的欄位，含主鍵與可否為 null |
| `get_sample` | 幾筆資料，**預設遮罩個資** |
| `profile_column` | 單一欄位的單一統計值，當場算 |

**讀盤點結果**（`MCP_STAGING_DB` 設了才有，compose 預設有）：

| 工具 | 做什麼 |
|---|---|
| `inventory_start` / `inventory_status` / `inventory_cancel` | 背景掃描 |
| `inventory_summary` | 盤點結果的統計數字 —— **先問這個** |
| `inventory_containers` / `inventory_columns` | 一頁已盤點的表 / 欄位 |
| `inventory_search` | 用關鍵字找表和欄位 |
| `inventory_relationships` | 所有外鍵，可以直接畫 ER 圖 |
| `inventory_changes` | 兩次掃描之間上游 schema 變了什麼 |
| `inventory_annotate` | 寫下這張表或欄位到底裝什麼 |
| `inventory_export` | 整份寫成檔案，**只回傳路徑**（`markdown` / `csv` / `dbt_yaml`） |

典型的交付流程：`inventory_start` 跑一次掃描 → `inventory_annotate` 把看懂的東西寫
回去 → `inventory_export(format="dbt_yaml")` 產出 `schema.yml`。之後 PM 讀的就是這
份盤點結果，不會再去打資料庫。

`get_sample` 預設把個資遮罩成 `a***@***.com`、`***`——形狀留著，值不留，因為取樣出
來的資料會進 agent 的 context 並留在每一份對話記錄裡。要看真值需要
`--allow-raw-sample` 那項授權，預設兩個角色都沒有。

postgres 和 mssql 的 container 名字帶 schema（`dbo.orders`）；只有一個 schema 有那
張表的話，寫 `orders` 也可以。

---

## 遇到問題

**`token 格式不對` / server 回 401**
把 token 手動貼進 `.mcp.json` 如果就通了，代表是 `${...}` 沒被展開。預設已經是直接
寫入 token，所以會遇到這個通常是設了 `PROVISION_INLINE_TOKEN=0`——那個寫法要求
client 會展開變數，而且啟動它的 shell 就是你 `export` 的那個 shell；從桌面應用程式
啟動的 client 兩個都不成立。

**`this image was not built for <engine>`**
`.env` 的 `MCP_ENGINE` 和 image 對不上。`docker compose up -d --build`。

**`Login timeout expired (HYT00)`**
連不到。容器裡的 `localhost` 是容器自己——資料庫跑在 docker host 上的話要用
`SHOP_HOST=host.docker.internal`。確認路由：

```bash
docker compose exec server python -c "import socket;socket.create_connection(('192.168.0.142',1433),5);print('ok')"
```

**`certificate verify failed`**
自簽憑證，見 [mssql 的自簽憑證](#mssql-的自簽憑證)。

**`InvalidSignatureError`**
`docker compose down -v` 會帶走金鑰的 volume，但留下 bind mount 的 `./out`——手上那
張 token 是用已經不存在的金鑰簽的。重跑 `docker compose up provision` 會偵測到並重
簽。

**agent 說某個工具被拒絕**
那是設定，不是故障。`docker compose run --rm provision role show <角色>` 看它實際拿
到什麼。

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
