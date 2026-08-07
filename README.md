# database-mcp-connector

一個 MCP server，讓 AI agent 能讀到資料來源的 catalog —— **有哪些表、哪些欄位、
什麼型別、代表什麼意思** —— 而不需要把資料庫帳密交給任何人。帳密只存在這個
server 的環境變數裡，其他地方都沒有。

已支援的 engine：sqlite、postgres、mysql/mariadb、mssql、mongodb、datalake
（parquet/csv/json），以及把另一個 MCP server 當成資料來源。

> 本文出現的 `container` 一律指**資料來源裡的表 / view / collection**，不是 Docker
> 容器。Docker 的部分會直接寫「容器」。

- [這個 server 提供什麼](#這個-server-提供什麼)
- [部署](#部署) — [選擇 transport](#選擇-transport)、[Compose](#docker-compose)、[stdio](#docker-走-stdio)、[Image 與 engine](#image-與-engine)、[不用 Docker](#不用-docker)
- [設定](#設定)
- [接上 agent](#接上-agent)
- [認證](#認證) — [角色](#角色)、[一把金鑰看得到什麼](#一把金鑰看得到什麼)
- [怎麼用才對](#怎麼用才對)

## 這個 server 提供什麼

對資料來源全部是唯讀的。能讓 engine 自己保證的就不靠自律：sqlite 用 `mode=ro`
開檔，postgres 和 mysql 把 session 設成唯讀。mssql、mongodb 和 datalake 的驅動沒
有這種開關。

**五個工具直接問資料來源**：

| 工具 | 回傳什麼 |
|---|---|
| `list_databases` | 這條連線碰得到的資料庫 |
| `list_containers` | 一頁的表 / view / collection，keyset 分頁 |
| `get_schema` | 某個 container 的欄位，含主鍵與可否為 null |
| `get_sample` | 幾筆資料，預設遮罩，上限由 `--max-sample-limit` 控制 |
| `profile_column` | 單一欄位的單一統計值 |

**設了 `--staging-db` 之後多十個。** 它們讀的是掃描結果 —— 已經記在本地一個 sqlite
檔裡了，所以完全不碰資料來源：

| 工具 | 做什麼 |
|---|---|
| `inventory_start` | 開始背景掃描，回傳 job id |
| `inventory_status` | 掃到哪了、為什麼停 |
| `inventory_cancel` | 做完手上這個 container 就停，進度保留 |
| `inventory_summary` | 盤點結果的統計數字 —— **先問這個** |
| `inventory_containers` | 一頁已盤點的 container |
| `inventory_columns` | 一頁某個 container 的欄位 |
| `inventory_search` | 用關鍵字找 container 和欄位 |
| `inventory_relationships` | 所有外鍵，以邊的形式回傳，可以直接畫 ER 圖 |
| `inventory_changes` | 兩次掃描之間上游 schema 變了什麼 |
| `inventory_annotate` | 寫下這張表或欄位到底裝什麼 |

**設了 `--export-dir` 再多一個**：`inventory_export`，把整份 catalog 寫成檔案，只
回傳路徑。

掃描中斷後會從 cursor 接續，schema 指紋沒變的 container 會跳過。但從 cursor 接續
的那一輪永遠不會回報「有東西被刪掉」—— 它根本沒看過前面那段。

## 部署

### 選擇 transport

這個決定會牽動後面幾乎所有事，所以先選它。

| transport | 形狀 | 認證 | 部署成 |
|---|---|---|---|
| `stdio` | client 自己生出 process，走它的 stdin/stdout | 沒有，而且 `--require-auth` 會被**拒絕** | 一個 client 一個 process，由 client 啟動 |
| `http` / `streamable-http` | 單一端點 `/mcp` | 簽章過的 bearer token | 長駐服務 |
| `sse` | 舊式雙端點，在 `/sse` | 簽章過的 bearer token | 只給不支援 streamable-http 的 client |

`http` 和 `streamable-http` 是同一個 transport 的兩個名字，用哪個都可以。

走 **stdio** 時認證沒有意義：能生出這個 process 的人已經握有它的環境變數，也就等
於握有資料庫帳密。所以 `--require-auth` 在這裡是直接報錯，而不是默默忽略。

走**網路 transport** 時的威脅是「知道 URL 的人就能讀資料庫」。所以除非綁在
loopback，或你明確加上 `--allow-insecure-http` 表示你是故意的，否則 server 不會
在沒有認證的情況下對外服務。

stdio 有個很容易踩的雷：**stdout 是 JSON-RPC 的通道**。包裝腳本往那裡印任何東西，
handshake 都會在 client 看到 server 之前就壞掉。這個專案的 entrypoint 所有輸出都
走 stderr 就是為了這件事，你自己寫的話也要這樣。

### Docker Compose

預設的部署方式：帶認證的網路 transport。同一個 image 起兩個服務，切開之後**提供服
務的那個容器永遠不會持有簽章金鑰**。

```bash
cp .env.example .env    # 編輯它 —— engine、連線、要發哪些身分
docker compose up -d --build
```

`--build` 是因為 image 是照 `MCP_ENGINE` 建的：換 engine 就是換一個 image。詳見
[Image 與 engine](#image-與-engine)。

| 服務 | 生命週期 | 掛載 | 做什麼 |
|---|---|---|---|
| `provision` | 跑一次就結束 | `/keys` 讀寫、`/private` 讀寫、`./out` | 產生金鑰對、簽 token、生出 client 端要用的檔案 |
| `server` | 長駐 | `/keys` **唯讀**、`/data` | 驗證 token 並提供服務 |

`server` 等的是 `service_completed_successfully`，所以 provision 失敗時 server 根
本不會起來，而不是起來之後服務一份沒人在裡面的允許清單。

**切割點在 volume，不在 image。** 簽章金鑰寫進一個 `server` 沒有掛載的 volume，所
以就算 server 被攻下，攻擊者也沒有能力自己簽一張 token 給自己。維持這個狀態。

`provision` 是冪等的 —— 每次 `up` 都會再跑一次，但只有在「server 現在**會拒絕**手上
這張 token」時才重簽：金鑰被換掉、token 過期、或 `MCP_AUDIENCE` 改了。三種情況它都
會說明原因。想無條件重簽就 `PROVISION_FORCE=1`。

> 這裡有個一定要知道的組合：金鑰在 named volume 上，token 在 bind mount 的 `./out`
> 上。`docker compose down -v` 會帶走前者、留下後者。所以檢查的是「這張 token 還驗
> 得過嗎」，而不是「檔案還在嗎」—— 只看檔案在不在的話，你會拿著一張用已經不存在的
> 金鑰簽出來的 token，然後 server 回 `InvalidSignatureError`。

跑完之後你收到的東西 —— `PROVISION_IDENTITIES` 裡每一個身分各一包：

```
out/
  de/                                一個 Claude Code plugin
    de.jwt                           憑證，權限 0600，請當憑證看待
    .mcp.json                        server 連線設定，預設直接帶著 token
    .claude-plugin/plugin.json
    skills/<server-name>/SKILL.md    依這個角色實際拿到的權限生成
    codex.toml                       同一個 server 的 Codex 寫法
    INSTALL.md
  pm/                                同上，但只有 pm 角色的權限
    …
```

一包就是一個身分：token、設定、說明都在同一個目錄裡，交給誰就整包給誰。**整包是憑
證**，不要進版控。

`.mcp.json` 預設把 token 直接寫進去（0600），而不是寫成 `${ETL_AGENT_MCP_TOKEN}`。
引用寫法要求兩件事同時成立：client 會展開環境變數，而且啟動它的 shell 就是你
`export` 的那個 shell。從桌面應用程式啟動的 client 兩個都不成立——它送出去的會是
`${ETL_AGENT_MCP_TOKEN}` 這串字面文字，server 因為「不是一個 JWT」而拒絕，錯誤訊息
從頭到尾不會提到變數。要用引用寫法就設 `PROVISION_INLINE_TOKEN=0`。

狀態都在 `mcp-data` volume 上：staging 資料庫、稽核記錄、log。image 裡不烤任何狀
態 —— staging 的 schema 第一次開檔時會自己建好，不需要事先做任何 setup。

檔案型的 engine 要把來源掛進容器，`docker-compose.yml` 裡有註解掉的範例。走網路的
engine 不需要。

### Docker 走 stdio

不用 compose、不用 token、不用開 port。由 client 每次自己啟動容器：

```json
{
  "mcpServers": {
    "shop-catalog": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "MCP_ENGINE=postgres",
        "-e", "MCP_CONNECTION_REF=shop",
        "-e", "SHOP_HOST", "-e", "SHOP_USER", "-e", "SHOP_PASSWORD", "-e", "SHOP_DB",
        "database-mcp-connector:postgres", "serve"
      ]
    }
  }
}
```

`-e NAME` 不帶值是 docker 的傳遞寫法：值來自啟動 client 的那個環境，所以這個檔案
裡不會留下任何機密。`MCP_TRANSPORT=stdio` 時 `provision` 會幫你生出這個形狀。

### Image 與 engine

**一個 engine 一個 image。** `MCP_ENGINE` 同時是 build argument 和 runtime 設定，
compose 從 `.env` 把同一個值餵給兩邊，所以你只需要講一次：

```bash
docker build --build-arg MCP_ENGINES=mssql -t database-mcp-connector:mssql .
```

驅動沒有辦法是 runtime 的決定：pyodbc 綁的是一個不屬於 python 套件的 ODBC 驅動、
image 是唯讀的、跑的 process 也不是 root。所以「我在 `.env` 指定 engine 了，依賴就
該裝好」這件事只能發生在 build，而現在就是這樣運作的。

只有 mssql 需要 OS 層的東西（unixODBC 加上微軟自己的驅動，以及一個額外的 apt
repository）。其他 engine 的 image 完全不會走到那一步，也不會多出 curl 和 gnupg。

**image tag 帶著 engine**（`database-mcp-connector:mssql`）。這不是命名美觀問題：
`docker compose up` 不加 `--build` 時會直接沿用同名 tag 的既有 image，tag 如果不隨
engine 改變，你會拿到一個「設定寫著這個 engine、驅動卻是另一個 engine」的容器，而
那種錯要到第一次呼叫 tool 才會炸。

engine 和 image 對不上的時候，server **不會啟動**——不是起來之後每個請求都失敗。
錯誤訊息會直接告訴你要用哪個 `MCP_ENGINE` 重 build。

一個 image 裡要放多個驅動就用 `MCP_ENGINES=mssql,postgres`，tag 會變成
`mssql-postgres`。那是逃生口，不是預設路徑。

**連線資訊永遠不會是 build argument。** build argument 會留在 `docker history` 裡。
host、帳號、密碼一律只走 runtime 環境變數。engine 是例外，因為它是一個驅動名稱，
不是機密。

image 也可以直接當 CLI 用，兩個角色沒涵蓋到的事情都能做 ——
`docker run --rm <image> token issue …`，或其他任何指令。

### 不用 Docker

```bash
uv sync --extra server
export SHOP_PATH=./var/shop.db
uv run mcp-connector --engine sqlite --connection-ref shop --staging-db ./var/staging.db
```

`--extra server` 是讓 MCP 那層能 import 進來。每個 engine 的驅動各自是一個 extra：
`postgres`、`mysql`（mariadb 共用）、`mssql`、`mongo`、`datalake`、`mcp`。sqlite
不需要裝任何東西。mssql 的 ODBC 驅動本身不是 python 套件，要另外安裝。

engine 和 extra 的對照表在 [src/core/engines.py](src/core/engines.py)，只有那一份
——Dockerfile 是把那個檔案當成 script 跑來算出要裝什麼的，所以新增一個 engine 只要
改那個 dict。

## 設定

### 連線

連線資訊**不是 flag**。它們住在你自己選的前綴底下的環境變數裡，`--connection-ref`
負責指出那個前綴：

```bash
export SHOP_HOST=db.internal SHOP_USER=readonly SHOP_PASSWORD=… SHOP_DB=shop
uv run mcp-connector --engine postgres --connection-ref shop
```

認得的字尾有 `_HOST`、`_PORT`、`_USER`、`_PASSWORD`、`_DB` / `_DATABASE`、`_URI`、
`_PATH`、`_TOKEN`、`_TRUST_SERVER_CERTIFICATE`。專案目錄下的 `.env` 會自動載入；用
compose 時，`.env` 同時也是拿來填 `docker-compose.yml` 裡 `${...}` 的那份。範例見
[.env.example](.env.example)。

| engine | 需要 | 或者，也可以用 |
|---|---|---|
| `sqlite` | `_PATH`（一個 `.db` 檔） | `_URI` |
| `datalake` | `_URI`（`s3://…`、`gs://…`）或 `_PATH` | |
| `postgres` | `_HOST`、`_USER`、`_PASSWORD`、`_DB` | `_URI` —— 完整的 libpq conninfo，也是唯一能接 unix socket 的方式 |
| `mysql` / `mariadb` | `_HOST`、`_USER`、`_PASSWORD`、`_DB` | `_URI`（`mysql://user:pass@host:port/db`） |
| `mssql` | `_HOST`、`_USER`、`_PASSWORD`、`_DB` | `_URI` —— 完整的 ODBC 連線字串 |
| `mongodb` | `_URI`（`mongodb://…`）或 `_HOST`，加上 `_DB` | url 的 path 部分會被當成 `_DB` |
| `mcp` | `_URI`（`http://…`）或 `_PATH`，加上 `_TOKEN` | |

`_DB` 就是 `--database` 會覆蓋的東西。對 postgres、mysql、mssql、mongodb 來說，它
是在一台有多個資料庫的伺服器上選一個；要另一個就會開第二條連線，pool 會把它跟第一
條並存。

#### mssql 的 TLS

連線字串是用 `Encrypt=yes` 且會驗證憑證組出來的。ODBC Driver 18 從預設不加密改成
預設加密，所以地端的自簽憑證伺服器第一次連一定會撞到：

```
[08001] SSL Provider: … certificate verify failed: self-signed certificate
```

```bash
SHOP_TRUST_SERVER_CERTIFICATE=1
```

**加密仍然開著，關掉的是「對方是不是它宣稱的那個人」這項驗證** —— 也就是說這條連線
在網路上是可以被中間人攔截的。所以它是一個要明確打開的旗標，不是自動推斷的：同一個
錯誤既可能是「憑證是自簽的，本來就知道」，也可能是「憑證出事了」，只有人分得出來。
打開的時候 server 會在 log 裡把這件事講一次。

比較乾淨的做法是把那張憑證放進容器的信任庫，但 runtime 跑的是非 root 使用者，那得
在 build 階段或掛一份 CA bundle 進去做。

連線逾時預設是 30 秒（驅動自己的預設是 15 秒，跨站台的第一次連線常常不夠）。逾時回
的是 `HYT00`，而且訊息完全不會提到網路——這個 adapter 會把它翻成一句看得懂的話，並
且提醒你容器裡的 `localhost` 是容器自己，docker host 要用 `host.docker.internal`。

### 其他設定

其他每個設定都有一個 flag 和一個 `MCP_*` 環境變數，flag 優先。

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
| `--audit-max-mb` | `MCP_AUDIT_MAX_MB` | `10` —— `0` 表示不輪替 |
| `--audit-backups` | `MCP_AUDIT_BACKUPS` | `5` |
| `--profile-mode` | `MCP_PROFILE_MODES` | 未設 —— 每個欄位各自決定 |
| `--no-profile` | `MCP_PROFILE=false` | 關閉 —— 預設會收集統計 |
| `--server-name` | `MCP_SERVER_NAME` | `etl-agent-mcp` |
| `--require-auth` | `MCP_REQUIRE_AUTH` | `false` |
| `--authorized-keys-dir` | `MCP_AUTHORIZED_KEYS_DIR` | 未設 |
| `--audience` | `MCP_AUDIENCE` | `etl-agent-mcp` |
| `--allow-insecure-http` | `MCP_ALLOW_INSECURE_HTTP` | `false` |

`--staging-db` 預設關閉，因為沒有地方累積掃描結果的話，掃到的東西就無處可放 ——
所以 inventory 工具乾脆整組不提供，而不是提供了卻每次呼叫都失敗。它也不可以指向正
在被盤點的那個資料庫，store 會拒絕。

`--profile-mode` 會把你指定的模式套用到每一個欄位。不設的話，每個欄位會拿到符合它
型別的統計 —— 數字或日期給範圍，類別型給最常見的值，對我們沒有意義的型別就只給
null 比例。

## 接上 agent

`provision` 會幫你把 client 端生出來：一份帶 server 設定的 `.mcp.json`，以及一份
告訴 agent 該怎麼用的 `SKILL.md`。兩份都是**對著實際部署生成的** —— 工具清單來自
[src/core/tools.py](src/core/tools.py)（角色驗證用的也是同一份），「這個 agent 可以
叫哪些工具」來自剛剛簽出來的那張 token，角色的用途說明來自 `roles.toml`，所以三者
不可能對不上。把 `out/<kid>/` 複製到 `~/.claude/plugins/`，或用 marketplace 指向它。

文件的語言由 `PROVISION_LANG` 決定（`zh-TW` 或 `en`，預設 `zh-TW`）。**工具名、參
數名、claim 名一律不翻譯**——那些是要真的打出去的識別碼。frontmatter 的
`description` 是中英並列的，因為那一行是決定 skill 要不要被觸發的比對依據，使用者
可能用任何一種語言問。要改用字或加語言，看
[docker/templates/](docker/templates/)：一個語言一個目錄，版型、片段、字串都在裡面。

手動設定的話，網路 transport 長這樣：

```json
{
  "mcpServers": {
    "shop-catalog": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer ${SHOP_CATALOG_TOKEN}" }
    }
  }
}
```

token 預設**直接寫進這個檔案**，權限 `0600`。它從此是一份憑證 —— 不要進版控，也不
要複製到任何你不會複製密碼過去的地方。

之所以不用 `${ETL_AGENT_MCP_TOKEN}` 引用：那個寫法要求兩件事同時成立，**client 會
展開環境變數**，而且**啟動 client 的 shell 就是你 `export` 的那個 shell**。從桌面
應用程式（Dock、Finder、IDE）啟動的 client 兩個都不成立——它繼承的是 launchd 的環
境，不是你的 `.zshrc`。這時候送出去的會是 `${ETL_AGENT_MCP_TOKEN}` 這串字面文字，
server 因為它不是一個 JWT 而拒絕，而錯誤訊息講的是「token 的形狀」，從頭到尾不會提
到變數——所以很容易往錯的方向查。

要用引用寫法，設 `PROVISION_INLINE_TOKEN=0` 重跑 provision，然後在**啟動 client 的
那個 shell** 裡：

```bash
export ETL_AGENT_MCP_TOKEN=$(cat out/de/de.jwt)
```

判斷是哪一種問題的方法：把 token 手動貼進 `.mcp.json`，如果就通了，代表檔案本身有
被讀到，只是展開沒發生。

## 認證

只適用於網路 transport。

`mcp.json` 可以帶一個固定的 header，但它沒辦法「算出」一個簽章，所以簽章這件事發生
在服務程序之外。server 只持有**公鑰** —— 它能驗證 token 但無法鑄造 token，所以攻下
server 並不會讓人取得自己發憑證的能力。

**這是簽名，不是加密。** token 是一張 JWT，它的 payload 是明文、誰都看得懂；簽章買
到的是「無法被偽造」，不是「無法被看見」。

```bash
# 每個 agent 做一次 —— 公鑰會被放到 server 會去讀的目錄
uv run mcp-connector token keygen --kid pm-explorer --keys-dir ./keys --out ./pm-explorer.pem

# 每張 token 做一次
uv run mcp-connector token issue --key ./pm-explorer.pem --kid pm-explorer \
    --scope list_containers --scope get_schema --lifetime 30d

# 啟動服務
uv run mcp-connector --engine sqlite --connection-ref shop \
    --transport streamable-http --host 0.0.0.0 \
    --require-auth --authorized-keys-dir ./keys
```

agent 拿到的**只有那張 token**。私鑰不會進到 server，公鑰不會給到 agent。

| claim | 作用 |
|---|---|
| `kid`（在 header） | 指出 `--authorized-keys-dir` 裡哪把公鑰能驗它 |
| `sub` | 呼叫者是誰，也是稽核記錄裡記下的身分 |
| `aud` | 必須等於 `--audience`，所以簽給別處的 token 在這裡會被拒 |
| `exp` | 必填；過期是 token 失效的主要方式 |
| `scopes` | 可以呼叫哪些工具，逐一列名。沒有 scope 就一個工具都叫不動。 |

**撤銷**就是 `rm ./keys/<kid>.pub` —— 不用重啟，30 秒內生效，因為目錄就是每 30 秒
重讀一次。

`exp` 驗證時有 60 秒的寬容，用來吸收時鐘誤差。只有在你簽很短的效期時才需要在意：
`--lifetime 5m` 的 token 實際上可以用六分鐘。

簽章只接受非對稱演算法（EdDSA、ES256、RS256）；HMAC 類的一律不接受，因為這裡的金鑰
是公開的。

### 角色

一長串 `--scope` 沒有人會想維護，也沒有人會想 review。權限集中寫在
`docker/roles.toml`，一個角色一段：

```bash
docker compose run --rm provision role list       # 有哪些角色
docker compose run --rm provision role show de    # 展開繼承後的實際權限
```

預設兩個：

| 角色 | 給誰 | 拿得到什麼 |
|---|---|---|
| `pm` | 跟客戶對資料的人 | 讀盤點結果、看 schema、取遮罩過的樣本、匯出資料字典。**不能**啟動掃描、不能改盤點內容 |
| `de` | 做盤點交付的工程師 | `pm` 的全部，再加上跑掃描、即時統計、補描述、匯出 `dbt schema.yml` |

`de` 是用 `extends = "pm"` 寫的，因為它的權限確實是 `pm` 的超集；兩份各自維護的清
單遲早會有一份忘了更新。檔案裡看得到的就是「`de` 比 `pm` 多了什麼」。

```toml
[roles.analyst]
extends    = "pm"
add_tools  = ["profile_column"]
databases  = ["analytics"]
containers = { deny = ["*_pii"] }
lifetime   = "7d"
```

`tools` 取代繼承來的清單，`add_tools` / `drop_tools` 增減。**每個工具名在載入時都會
比對真實的工具清單**，打錯字直接報錯——一個對不到任何工具的 scope 什麼都不會授予，
但 token 照樣簽得出來，是最難發現的那種錯。

發身分：

```bash
# .env
PROVISION_IDENTITIES=de=de,pm=pm,alice=pm
```

每個身分各自一組金鑰、一張 token、一包 `out/<kid>/`，SKILL.md 只描述那個角色拿得到
的工具。`docker compose run --rm provision --kid bob --role pm` 可以只補發一個人。

改完 `roles.toml` 之後要重簽：`PROVISION_FORCE=1 docker compose up provision`。

### 一把金鑰看得到什麼

角色是這些 flag 的集合寫法；直接下 flag 也可以，而且會**疊加**在角色上（只會放寬，
不會收窄——收窄的話 token 就跟它自己的 SKILL.md 對不上了）：

```bash
uv run mcp-connector token issue --key ./pm.pem --kid pm-explorer \
    --role pm \
    --database analytics \
    --allow-container 'dim_*' --allow-container 'fct_*' \
    --deny-container '*_pii'
```

| flag | claim | 沒帶的話 |
|---|---|---|
| `--database` | `databases` | 所有資料庫 |
| `--allow-container` / `--deny-container` | `containers` | 除了被 deny 的以外都可以 |
| `--allow-raw-sample` | `allow_raw_sample` | **不行** —— 只給遮罩過的資料 |
| `--annotate-as-human` | `annotate_as_human` | 描述會被記成 agent 寫的 |

**deny 贏過 allow。** 一把金鑰不能讀的 container，被直接點名要求時會**明確以該名稱
拒絕** —— 因為被告知「這張表不存在」的 agent 會繼續去找，被告知「你不能看」的 agent
才會去要權限 —— 同時它也不會出現在列表、搜尋和匯出裡，這樣 catalog 的樣貌就不會洩漏
給讀不到它的人。

**認證不等於授權。** 一張有效的 token 去叫 scope 以外的工具，一樣會拿到錯誤，而且這
次拒絕跟其他呼叫一樣會被記進稽核記錄。

## 怎麼用才對

### 讀一份塞不進 context 的 catalog

再大的 catalog 都不該進到 agent 的 context，而解法不是換更大的視窗 —— 是根本別把它
放進去：

| 你想知道 | 該問 |
|---|---|
| 這東西有多大 | `inventory_summary` —— 只有數字，幾百 bytes |
| 我要的東西在哪 | `inventory_search` —— 精準命中，不含統計 |
| 這張表裡有什麼 | `inventory_columns` —— 一頁；很寬的表加 `include_profile=False` |
| 全部 | `inventory_export` —— **一個檔案**，只回傳路徑 |

`inventory_export` 可以寫成 `markdown`（給人讀的資料字典）、`csv`（一欄一列）或
`dbt_yaml`（dbt 專案的 `schema.yml`），回傳的是「寫到哪、寫了多少」，而不是內容。這
就是為什麼「幫我盤點整個倉庫」這種要求，這個 server 答得出來。

給它的路徑是相對於 `--export-dir` 的，而且出不去；`../` 和 symlink 都會先解析再檢
查，因為呼叫者是一個轉述別人給的路徑的 agent。

### 個資

`get_sample` 會把真實資料放進 agent 的 context，接著就會流進那個 context 碰到的每一
份 log、對話記錄和歷史。所以**資料預設是遮罩的**：

```
{"id": 1, "email": "a***@***.com", "note": "hello", "api_key": "***"}
```

有用的「形狀」會留下來；至於密鑰類的則什麼都不留，因為那沒有什麼形狀值得展示。

哪些欄位算個資是由掃描判定的：先看欄位名稱，名稱看不出來的再取少量值來看。存下來的
只有結論，不會存判定時看到的值。這是猜的，而 `inventory_annotate` 可以永久覆蓋它 ——
人寫下的結論，之後的掃描不會推翻：

```json
{"column": "internal_ref", "sensitivity": "pii"}
```

`mask=False` 會回傳未遮罩的原始資料。走 stdio 時這是允許的；走認證過的 transport
時，那把金鑰必須被授予 `--allow-raw-sample`，而且稽核記錄會記下這件事發生過。

### 描述

inventory 用兩個不同的欄位存兩種描述：

- **`native_description`** —— 資料來源自己的註解。掃描會覆蓋它，而且它算在指紋裡，
  所以上游改了註解會觸發重掃。sqlite 完全沒有註解這種東西。
- **`description`** —— 透過 `inventory_annotate` 寫進來的。**掃描永遠不碰它**，而且
  它不算在指紋裡，所以寫描述不會觸發它自己的重掃。

`inventory_annotate` 是唯一會寫入的工具，而且它只寫進 inventory —— 資料來源永遠不會
被碰到。一段描述是誰寫的由 server 依呼叫者的金鑰決定，不是由呼叫者自己宣稱的：要記
成 `human` 需要 token 帶著那個授權，其他一律是 `ai`。

staging 檔案裡記著它自己的結構版本，舊版本寫出來的檔案會被拒絕而不是自動遷移。
inventory 是重掃就能重現的衍生資料，所以真正會失去的只有人工標註過的內容。

### container 怎麼命名

一個 container 就是一個字串，而這個字串必須能唯一指出一個 container。所以在 postgres
和 mssql 上它會帶 schema —— `public.users`、`dbo.orders`。如果只有一個 schema 有這張
表，光寫 `users` 也可以；有好幾個的時候，這次呼叫會連同清單一起被拒絕，而不是從其中
隨便挑一個回答。

sqlite、mysql、mongodb 在資料庫底下沒有 schema 這層，所以它們的 container 就是單純的
名字。mysql 的 `SCHEMA` 是 `DATABASE` 的同義詞，傳入一個不是當前連線資料庫的值會被拒
絕，而不是被忽略。

### collection 沒有 schema

mongodb 沒有宣告好的 schema，所以 `get_schema` 是從一個 collection 的前一百筆文件
**推論**出來的。它描述的是那個樣本，不是整個 collection：

- 某個欄位被看到裝過不只一種型別，就全部列出來，像 `int|string`
- 某份文件裡沒有的欄位，在那份文件裡算 null
- 只回報頂層欄位；巢狀文件是 `object`，陣列是 `array`
- 對一個樣本裡從沒出現過的欄位做 profile 會被拒絕，而不是回一個 null 比例 1.0 ——
  那會被讀成關於整個 collection 的事實
