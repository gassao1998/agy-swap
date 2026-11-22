# agyswap

[English](README.md)

agyswap 讓 Windows 上的 Antigravity CLI（`agy`）同時記住幾個 Google 帳號的登入，需要時換來換去。每個帳號的額度分開算。快用完時，你可以把全域登入換成別的帳號，或只讓某個終端機改用別的帳號；也可以交給 `agyswap auto`，讓它在使用中的帳號撞到上限前自動換掉。

指令、參數、`--json` 欄位與設定鍵都照 [claude-swap](https://github.com/realiti4/claude-swap)（`cswap`）1.0.0。cswap 是 Claude Code 的多帳號切換工具，用過的人不必重學。agyswap 跟 Google 沒有關係。

## 現況

Alpha 版（1.0.0a1），只支援 Windows。切換的程式有離線測試把關，測試碰不到真的認證管理員。全域切換也在真的認證管理員上跑過。

Antigravity App 會不會跟著切換換帳號，目前沒人測過。`add-token` 把 Gemini API key 當成 `GEMINI_API_KEY` 交給 `agy`，`agy` 認不認這個變數也還不知道；論壇有回覆說還要把 `modelProvider` 設成 `gemini`，這點也沒實測過。

## Google 的條款

用之前請先讀這節。Antigravity 的官方 FAQ 寫：「Antigravity CLI headless mode (agy -p) is intended for direct scripting and CI workflows. Wrapping agy inside third-party coding agents or applications, or automating account rotation, is not supported under consumer plans.」（[FAQ](https://antigravity.google/docs/faq)）意思是 `agy -p` 只給直接寫腳本與 CI 用；把 agy 包進第三方 coding agent 或應用程式，或自動輪替帳號，消費者方案都不支援。Antigravity 的附加條款另外寫明，用第三方軟體、工具或服務存取服務屬於違約，「may be grounds for suspension or termination of your Antigravity and/or Gemini CLI accounts」（[條款](https://antigravity.google/terms)），也就是可能停掉你的 Antigravity 或 Gemini CLI 帳號。

agyswap 碰到的地方：

- `auto`、`run --auto` 就是自動輪替帳號。
- `run` 照字面就是包住 agy 的第三方程式，被其他 coding agent 呼叫也算。
- agyswap 會讀 agy 快取的 OAuth 登入：核對 `id_token_email`、在帳號資料夾存一份，再交回官方的 agy，自己不連 Google 的伺服器。Google 人員 9 月在論壇回答，腳本呼叫未經修改的 agy 可以，但不能擷取、轉交或重複使用它快取的憑證（[論壇](https://discuss.ai.google.dev/t/185992)）；agyswap 確實會讀取、搬動這些憑證。11 月的一則回覆則說，別家的 coding agent 啟動 `agy -p`，個人方案不支援（[論壇](https://discuss.ai.google.dev/t/201688)）。
- 自己擁有、自己付費的帳號手動切換：7 月 Google 人員的回答說不算違反（[論壇](https://discuss.ai.google.dev/t/174378)），10 月另一則回覆也說可以（[論壇](https://discuss.ai.google.dev/t/190436)）。

真的有人被停過。2026 年 2 月那一波之後，Google 開了申訴表單，第一次可以解除，再犯就永久停用（[論壇](https://discuss.ai.google.dev/t/131424)）。Google 的服務條款也保留權利，嚴重或重複違反服務專屬條款時，可以停止你使用服務，或刪除整個 Google 帳號（[條款](https://policies.google.com/terms)）。

官方支援的腳本用法是改用 API key 登入（11 月那則回覆說要把 `modelProvider` 設成 `gemini`，再設 `GEMINI_API_KEY`），或改用 Google Cloud 的 Pay As You Go。agyswap 的 `add-token` 只設 `GEMINI_API_KEY`，這些都還沒在這裡實測過。

agyswap 改變不了 Google 允許什麼。要用的話，風險由你自己的帳號承擔。

## 需要

- Windows 10 或 11
- Python 3.11 以上，以及 [uv](https://docs.astral.sh/uv/)
- 已安裝、且登入過一次的 Antigravity CLI（`agy`）
- Git，從 GitHub 安裝和跑 `agyswap upgrade` 時要用

## 安裝

從 clone 下來的資料夾安裝，之後可以用 `agyswap upgrade` 更新：

```powershell
git clone https://github.com/gassao1998/agy-swap
cd agy-swap
uv tool install --editable . --with "textual>=8.2.8,<9"
```

也可以直接從 GitHub 安裝：

```powershell
uv tool install "agyswap[tui] @ git+https://github.com/gassao1998/agy-swap"
```

這種裝法要更新時，同一行指令加上 `--force --refresh` 再跑一次。

textual 只有儀表板用得到。不裝的話（拿掉 `--with ...` 或 `[tui]`），其他指令照常能用，`agyswap tui` 會印出安裝提示。

## 快速上手

先把 `agy` 目前登入的帳號登記進來：

```powershell
agyswap add
```

登入第二個帳號。這會在新的帳號資料夾裡跑 agy 的登入，全域登入不受影響：

```powershell
agyswap login
```

加帳號時，別在 `agy` 裡 `/logout` 再登入別的帳號。`/logout` 會不會連 Google 那邊的舊登入一起撤銷，目前還不清楚。

接著就能這樣用：

```powershell
agyswap list                       # 所有帳號和各自的額度
agyswap switch                     # 全域登入換到下一個帳號
agyswap switch 2                   # 或換到指定的編號、email、別名
agyswap run 2 -- -p "hello"        # 只在這個終端機用 2 號帳號跑 agy
agyswap run --auto -- -p "hello"   # 挑一個這次呼叫的模型還有額度的帳號
agyswap auto                       # 使用中的帳號快用完前自動換
```

Git Bash 會把開頭是 `/` 的參數（例如 `/usage`）改寫成 Windows 路徑，再交給 agy。這種呼叫要設 `MSYS_NO_PATHCONV=1`。

## 儀表板

`agyswap tui`，或在終端機裡直接打 `agyswap`，會開出 textual 做的儀表板。使用中帳號的各個額度畫成長條，標出切換線和重置倒數，其他帳號一個一行。`agyswap watch` 直接開在即時監看頁。

按鍵：`s` 切換（再按 `b` 挑最好的帳號）、`w` 即時監看、`g` 自動切換頁、`f` 立刻查全部帳號、`ctrl+t` 換配色、`q` 離開。切換、移除、新增都會先問，預設停在「取消」。自動切換頁一開始只看不切，按 `l` 並確認後才真的切換。加 `--no-mouse` 可以保留終端機本身的選取文字功能。

## 運作方式

### 全域切換

`agy` 把登入存在 Windows 認證管理員的一般認證 `gemini:antigravity`。`agyswap switch` 換掉這一筆，之後新開的 `agy` 都用新帳號。Antigravity App 應該也讀同一筆，不過還沒測過。

寫入前，切換會先備份目前那筆登入，並把備份讀回來核對。agy 可能已經替要換下來的登入換發過新權杖。這份登入的 `id_token_email` 跟帳號對得上，才會存回該帳號的資料夾。接著寫入新登入、讀回核對，有任何不符就還原備份。切換沒做完會留下 `pending_switch.json`，之後的指令會提醒。

換下來的登入存不回去時（不在管理範圍的登入、用了 `--force`、還原失敗），備份另存成 `backup\unsaved-<utc>.bin`，之後的切換不會覆蓋這個檔案。

### 單一終端機

設了 `SSH_CONNECTION` 時，agy 不讀認證管理員，改讀家目錄底下的 `.gemini\antigravity-cli\antigravity-oauth-token`，兩邊存的是同一種 JSON。`agyswap run` 給每個帳號各自的家目錄，設好 `USERPROFILE`、`HOME` 與 SSH 的環境變數，再在那裡啟動 agy。

之後就算做了全域切換，正在跑的 agy 仍用原本的登入，各帳號的對話紀錄也分開存。agyswap 自己的訊息寫到 stderr，agy 的結束代碼原樣傳回。

### 額度

額度來自 agy 自己的 `/usage`。每個帳號查一次要兩三秒，所以結果會快取。5 小時與每週的限制對到 cswap 的 `fiveHour`、`sevenDay` 欄位，個別模型的限制（例如 Gemini 4 Argon 的週限制）放在 `scoped`。查詢在啟動 agy 前，會先在共用快取裡認領那個帳號；`list`、`status`、`auto` 和儀表板同時跑時，就不會重複查同一個帳號。

透過 SSH 登入時，那個登入工作階段讀不到認證管理員。這時 `list`、`status` 改從帳號資料夾查使用中帳號的額度，並標明沒檢查到全域登入。

### 自動切換

`agyswap auto` 是把 cswap 1.0.0 的自動切換迴圈改給 agy 用。策略一樣（`best`、`weekly-first`、`consume-first`、`priority-consume-first`、`next-available`），也有個別帳號與全域的門檻、遲滯、兩次切換之間的冷卻，以及連續幾次查不到額度就換帳號的備援。狀態存在磁碟上，所以從排程器跑 `agyswap auto --once`，行為跟常駐迴圈一樣。`auto` 不會換到 `agyswap run` 正在用的帳號。

## 指令

簡寫：`ls` 是 `list`、`rm` 是 `remove`、`update` 是 `upgrade`。`<帳號>` 可以是編號、email 或別名。

帳號：

| 指令 | 做什麼 |
| --- | --- |
| `agyswap add` | 登記認證管理員裡目前的登入。可加 `--slot`、`--email`、`--alias`。 |
| `agyswap login` | 在新帳號自己的資料夾裡登入，全域登入不動。 |
| `agyswap remove <帳號>` | 移除帳號與帳號資料夾。 |
| `agyswap alias <帳號> <名稱>` | 設短名稱（`--unset` 移除；不給參數就列出全部）。 |
| `agyswap swap <a> <b>` | 對調兩個帳號的編號。 |
| `agyswap move <帳號> <編號>` | 把帳號改成某個編號，那個編號已有帳號就對調。 |
| `agyswap add-token [KEY\|-]` | 實驗性：存一把 Gemini API key，只給 `run` 用。 |

狀態與切換：

| 指令 | 做什麼 |
| --- | --- |
| `agyswap list` | 所有帳號和額度。`--json`、`--token-status`。 |
| `agyswap status` | 使用中的帳號和額度。`--json`。 |
| `agyswap switch [<帳號>]` | 全域登入換到下一個帳號，或換到指定的帳號。`--strategy best\|next-available`、`--model`、`--json`。 |
| `agyswap run [<帳號>] [-- 參數]` | 只在這個終端機用某個帳號跑 agy。沒指定帳號時，用目前資料夾對應的帳號，沒有對應就用使用中的帳號。 |
| `agyswap run --auto [-- 參數]` | 用這次呼叫的模型還有額度的帳號跑 agy。 |
| `agyswap map <帳號> [路徑]` | 讓資料夾對應到帳號，給 `run` 用（不給參數就列出全部）。 |
| `agyswap unmap [路徑]` | 移除對應。 |
| `agyswap tui`、`agyswap watch` | 儀表板。`--no-mouse`。 |

自動切換：

| 指令 | 做什麼 |
| --- | --- |
| `agyswap auto` | 使用中的帳號快用完前切換。`--once`、`--dry-run`、`--json`、`--interval`、`--threshold`、`--weekly-threshold`、`--cooldown`、`--model`、`--strategy`、`--include-api-key-accounts`。加 `--once` 時，切換了回 0，出錯回 1，不必切換回 2，沒有帳號能接手回 3。 |
| `agyswap disable <帳號>` | 讓帳號暫時不參加輪替（`enable` 放回來）。 |
| `agyswap threshold [<帳號> <百分比>]` | 設定某個帳號的切換線（`--unset` 清掉；不給參數就列出全部）。 |
| `agyswap priority [<帳號> <層級>]` | 設定帳號的優先順序，數字大的先用（不給參數就列出全部）。 |

設定、轉移與維護：

| 指令 | 做什麼 |
| --- | --- |
| `agyswap config` | 用 `list`、`get`、`set`、`unset`、`path` 看或改 `settings.json`。 |
| `agyswap export <路徑\|->` | 匯出帳號，含登入。`--account`、`--full`（連各帳號資料夾的 agy `settings.json`）、`--force`。 |
| `agyswap import <路徑\|->` | 匯入匯出檔。`--force`、`--include-settings`。 |
| `agyswap import-usage <路徑\|->` | 採用別台電腦查到的額度（`list --json` 的輸出）。`--hold 秒數`。 |
| `agyswap upgrade` | 在 agyswap 的 clone 裡跑 `git pull --ff-only`。 |
| `agyswap purge` | 問過之後，刪掉 agyswap 自己的所有資料。 |
| `agyswap selftest` | 跑內建測試。 |

`--debug` 把除錯紀錄寫到 stderr 和 `agyswap.log`。

## 設定

用 `agyswap config set <鍵> <值>` 改。鍵打錯時會提示正確的寫法。

| 鍵 | 預設 | 意思 |
| --- | --- | --- |
| `autoswitch.threshold` | 90 | 5 小時的限制用到這個百分比就切換；沒設每週切換線時，任何一個限制都算。 |
| `autoswitch.weeklyThreshold` | 未設 | 每週與個別模型限制的切換線，未設時沿用 `threshold`。 |
| `autoswitch.intervalSeconds` | 60 | `auto` 迴圈多久查一次（秒）。 |
| `autoswitch.cooldownSeconds` | 300 | 兩次提前切換之間至少隔多久（秒）。 |
| `autoswitch.hysteresisPct` | 10 | 目標帳號的剩餘額度要比使用中的多出這麼多個百分點。 |
| `autoswitch.strategy` | best | `auto` 怎麼挑下一個帳號。 |
| `autoswitch.includeApiKeyAccounts` | false | 讓 `auto` 也能換到 API key 帳號。 |
| `autoswitch.unhealthyTicks` | 3 | 連續查失敗幾次，帳號就算狀況不好。 |
| `autoswitch.model` | 未設 | 也看這些模型的每週限制來決定切換（`gemini`、`claude` 或 `all`）。 |
| `ui.theme` | auto | 儀表板配色：`dark`、`light`，或 `auto` 跟著終端機。 |
| `run.shareHistory` | false | `run` 讓各帳號共用同一份對話紀錄。確定安全之前預設關閉。 |
| `updates.check` | true | 每天用 `git ls-remote` 查一次 GitHub，有新版就提示。 |

## 資料存在哪

全部放在 `%LOCALAPPDATA%\agyswap`，設了 `AGYSWAP_HOME` 就放那裡，兩者都必須在你自己的 Windows 使用者資料夾裡。`profiles\` 是每個帳號的家目錄，放登入檔、agy 的設定與對話紀錄。

`sequence.json` 是帳號清單，`settings.json` 是設定，`cache\` 是額度快取，`backup\` 是切換前替認證管理員做的備份。`agyswap.log` 記下每次為了查額度啟動 agy 的時間，登入只記一小段雜湊，不記內容。

資料夾以外，agyswap 只碰認證管理員裡的兩種項目：agy 用的登入 `gemini:antigravity`，只有切換會改這一筆；以及 `add-token` 存的 `agyswap:apikey:<資料夾>`。

## 安全

匯出檔裡就是登入本身，請當成密碼保管，別分享，也別 commit 進 git。agyswap 建立這個檔案時，從檔案出現的那一刻起就只有你的 Windows 帳號打得開。`agyswap export -` 寫到 stdout，但 stdout 是終端機時（包括 Git Bash）會拒絕。

`export --full` 會帶上各帳號的 agy `settings.json`，`agyswap import` 預設略過，加 `--include-settings` 才寫入。agy 的設定可以啟動其他程式（MCP 伺服器、hooks），所以這個參數只用在你自己匯出的檔案上。

agyswap 的資料夾與每個帳號資料夾，在寫入登入前都會先鎖成只有你的 Windows 帳號能存取。遇到 junction、符號連結，或屬於別的 Windows 帳號的資料夾，agyswap 一律拒絕。

`agyswap purge` 會列出要刪的東西，問過才刪。purge 只刪 agyswap 自己建立的檔案，以及 agyswap 存的 `agyswap:apikey:*` 項目，`gemini:antigravity` 不動。切換還沒做完，或 `agyswap run`、`agyswap auto` 正在跑時，purge 會拒絕。

## 致謝

指令表、參數、`--json` 欄位名稱、設定鍵與好幾個演算法來自 [claude-swap](https://github.com/realiti4/claude-swap) 1.0.0，以 MIT 授權釋出。改寫自 claude-swap 的模組會在 docstring 註明，詳見 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 授權

MIT，見 [LICENSE](LICENSE)。
