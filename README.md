# netspeed — 跨平台 CLI 網路測速工具箱

單一 Python 檔案，支援 **Windows / macOS / Linux**。可對多個來源測速、兩端對測、ping、
查外網 IP 與電信商，並把結果整理成統整報告，歷史紀錄可挑選重跑。

---

## 1. 系統需求

- **Python 3.8+**（已內建於多數系統；Windows 可從 <https://www.python.org/downloads/> 安裝，
  安裝時勾選「Add Python to PATH」）。
- 免安裝任何套件即可使用 **Cloudflare 測速、外網 IP/電信商、Ping**（皆為內建實作）。
- 其餘測速來源為**選用**，用到時才需要對應的外部工具（見第 7 節）。

---

## 2. 快速執行（不需安裝）

在程式所在資料夾直接執行：

```bash
# Windows
netspeed.cmd all
# 或
python netspeed.py all

# macOS / Linux
./netspeed all
# 或
python3 netspeed.py all
```

**不帶參數**會進入互動 console 模式（類似 `diskpart`），輸入 `help` 看指令、`exit` 離開：

```bash
python netspeed.py
# netspeed> help
# netspeed> check
# netspeed> netinfo
# netspeed> all
# netspeed> exit
```

輸入時的輔助（像 diskpart 一樣）：

- 打 `i` 再按 `?` → 列出 `ip`、`install` 及說明。
- 接著打 `n`（變成 `in`）按 `Tab` → 自動補成 `install`。
- 若輸入已是完整指令（例如 `ip`），直接 `Enter` 就會執行；`Tab` 補齊後 `Enter` 也直接執行。
- **參數也能補齊**：例如 `netinfo ` 再按 `Tab` 會列出可選介面與選項；`pair ` 會列出 `serve/client/history`；
  `set theme.` 會列出所有 `theme.*` 設定鍵。輸入部分字元再按 `?` 會依前綴篩選。
- 較慢的讀取（`netinfo`、`check`、`ip`）第一次執行後會**快取**，之後在同一 console 內立即回應；
  要強制重讀用 `--refresh` 或輸入 `refresh` 清快取。
- **歷史與游標**：按 `↑`/`↓` 叫回上/下一筆指令，`←`/`→` 移動游標，`Home`/`End` 到行首/行尾，`Delete` 刪除。
- 按 `?` 若沒有任何符合項目，只會跳一行，不會多印訊息。

---

## 3. 安裝（建議，會產生 `netspeed` 指令）

一行指令即可安裝到使用者目錄、建立啟動器並加入 PATH：

```bash
# Windows
python netspeed.py install

# macOS / Linux
python3 netspeed.py install
```

安裝後**重開終端機**，即可在任何位置使用：

```bash
netspeed all
netspeed ping 8.8.8.8
netspeed pair client 192.168.1.20
```

安裝位置：

| 平台 | 程式 | 啟動器 | PATH |
|------|------|--------|------|
| Windows | `%LOCALAPPDATA%\Programs\netspeed\netspeed.py` | 同目錄 `netspeed.cmd` | 加入「使用者」PATH（不影響系統 PATH） |
| macOS / Linux | `~/.local/share/netspeed/netspeed.py` | `~/.local/bin/netspeed` | 於 shell 設定檔加入 `~/.local/bin` |

> 設定與歷史紀錄存放於使用者設定目錄（見第 6 節）。

---

## 4. 移除

```bash
# Windows
python netspeed.py uninstall

# macOS / Linux
python3 netspeed.py uninstall
```

移除項目：程式檔、啟動器、PATH 設定。**設定與歷史紀錄預設會保留**；若要一併清除：

```bash
python netspeed.py uninstall --purge     # Windows
python3 netspeed.py uninstall --purge    # macOS / Linux
```

手動移除（若不使用 `uninstall`）：

- Windows：刪除 `%LOCALAPPDATA%\Programs\netspeed` 整個資料夾，並到
  「設定 → 系統 → 進階系統設定 → 環境變數」把該目錄從使用者 PATH 移除。
- macOS / Linux：`rm -rf ~/.local/share/netspeed ~/.local/bin/netspeed`，
  並移除 shell 設定檔中 `# added by netspeed` 那兩行。
- 設定/歷史：刪除第 6 節所列的資料夾。

---

## 5. 指令總覽

```
netspeed                        進入互動 console 模式（輸入 help / exit）
netspeed check                  檢查各來源工具是否可用（執行前先確認）
netspeed netinfo [介面] [--all] 本機網路設定：DHCP/靜態、IP/遮罩、閘道、靜態路由、Sub IP
netspeed all                    外網IP → Ping → 全部來源 → 統整報告
netspeed mlab|fast|cloudflare|ookla|libre
netspeed ping [目標...]         預設含 default gateway 與清單
netspeed ip                     外網 IP 與電信商
netspeed pair serve             開對測伺服器（優先 iperf3，否則內建）
netspeed pair client <host>     連到對測端（下載/上傳/雙向）
netspeed pair history           對測歷史
netspeed history                歷史紀錄，可輸入編號直接重跑
netspeed targets [list|add|del|set|gateway]
netspeed endpoints [list|set <來源> <值>]
netspeed config [show|path|edit|reset]
netspeed set <key> <value>      例: set iperf.path /usr/local/bin/iperf3
netspeed get <key>
netspeed theme [list|set|reset|preset|export|import]
netspeed update [--reset-skip]  檢查/更新到最新版本（來源: update.repo）
netspeed version                顯示版本
netspeed install [-y] | uninstall [--purge] [-y]

# Cisco 風格（console 內）
netspeed show config            等同 netspeed config（也支援 show ip / show tools / show conf 縮寫）
netspeed configure              進入設定模式（提示字元變 netspeed(config)#）
```

常用選項：`--json`、`--no-color`、`--no-save`、`--config <path>`、`--no-update-check`。

### 範例

```bash
netspeed check
netspeed netinfo                 # 多介面時會列出並詢問要顯示哪個
netspeed netinfo --all --json
netspeed all --sources cloudflare,ookla --seconds 15
netspeed ping 168.95.1.1 -c 10 -i 0.5 -W 2 -s 56
netspeed pair serve --port 5201
netspeed pair client 192.168.1.20 --mode both --duration 10 --streams 4
netspeed set iperf.path "C:\tools\iperf3.exe"
netspeed targets add 168.95.1.1
netspeed history --kind pair
```

---

## 6. 設定與歷史紀錄位置

可用環境變數 `NETSPEED_CONFIG_DIR` 覆寫。

| 平台 | 路徑 |
|------|------|
| Windows | `%APPDATA%\netspeed\` |
| macOS | `~/Library/Application Support/netspeed/` |
| Linux | `~/.config/netspeed/`（或 `$XDG_CONFIG_HOME/netspeed`） |

- `config.json`：所有設定（ping 清單/參數、測速端點、iperf 路徑、啟用來源）。
- `history.jsonl`：歷史紀錄（每行一筆，可用 `netspeed history` 挑選重跑）。

以 `netspeed config edit` 可直接用編輯器修改全部設定。

---

## 7. 選用外部工具（用到該來源才需要）

| 來源 | 需要 | 安裝方式 |
|------|------|----------|
| Cloudflare | 無 | 內建 |
| 外網 IP / 電信商 | 無 | 內建 |
| Ping | 無 | 內建純 Python ICMP；無權限時自動改用系統 `ping` |
| 對測（內建模式） | 無 | 內建 TCP 對測 |
| M-Lab | `ndt7-client` | 從 <https://github.com/m-lab/ndt7-client-go/releases> 下載 |
| fast.com | `fast` | 先裝 Node.js，再 `npm install -g fast-cli` |
| Ookla | `speedtest` | <https://www.speedtest.net/apps/cli> 下載官方 CLI |
| LibreSpeed | `librespeed-cli` | <https://github.com/librespeed/speedtest-cli/releases> 下載 |
| 對測（iperf3 模式） | `iperf3` | Linux `apt install iperf3` / macOS `brew install iperf3` / Windows 下載 iperf3.exe |

**工具要放哪裡？** 兩種方式都行：

1. 放進 PATH（例如 Windows 的 `C:\Windows\System32` 或任一 PATH 目錄）。
2. **直接放在 `netspeed` 同層目錄**（也就是 `netspeed.py` 所在的資料夾）——netspeed 會自動找到。

找不到工具時，該來源會顯示提示與下載網址，其餘功能不受影響。iperf3 也可用
`netspeed set iperf.path <路徑>` 指定自訂執行檔位置。用 `netspeed check` 可隨時查看狀態與下載連結。

---

## 8. 版本更新（GitHub）

程式執行時（含進入 console 時）會依設定檢查 GitHub Releases 是否有新版本；有新版會顯示更新內容，並詢問：

- `y` = 更新（下載最新 `netspeed.py` 覆蓋，並保留 `.bak` 備份）
- `n` = 稍後
- `s` = 此版本不再通知（之後若有更新版本才會再提示）

先在設定裡指定你的 repo（需先把 `netspeed.py` 發佈到 GitHub，release 的 tag 用 `v1.0.0` 這種格式）：

```bash
netspeed set update.repo <你的帳號>/<repo>
netspeed update            # 立即檢查（強制）
netspeed update --reset-skip   # 清除「不再通知」的版本
netspeed set update.check_on_start false   # 關閉啟動時自動檢查
netspeed set update.interval_hours 24      # 檢查頻率（小時）
```

未設定 `update.repo` 時不會做任何檢查。也可用 `--no-update-check` 只略過單次。

---

## 9. Cisco 風格設定模式（console）

在 console 內可用 Cisco 交換器習慣的指令：

```
netspeed> show config          # = 原本的 config
netspeed> show ip              # = netinfo
netspeed> show conf            # 支援縮寫（唯一前綴）
netspeed> show                  # 列出所有可用 show 項目
netspeed> configure            # 進入設定模式（或打 conf）
netspeed(config)# set ping.count 10
netspeed(config)# endpoints set ookla 12345
netspeed(config)# theme set ok magenta
netspeed(config)# no ping.count        # 還原該項為預設
netspeed(config)# show config          # 讀取用指令仍可用
netspeed(config)# write                # 寫入檔案（或 save）
netspeed(config)# end                  # 離開（或 exit）
```

重點：

- 設定模式內變更**即時套用**（當下就生效），但**要 `write` 才會寫入檔案**；
  若沒 `write` 就 `end`，會提醒你「下次執行仍為舊設定」。
- `show` 對應表：`config`/`running-config` → `config`，`ip`/`interfaces` → `netinfo`，
  `tools` → `check`，另有 `targets`、`endpoints`、`theme`、`history`、`version`、`speed`。
- 直接下 `netspeed show config` 也可以（不必進 console）。

---

## 10. 顏色主題（theme）

可自訂各元素的顏色，存檔後下次執行即生效，也能匯出分享：

```bash
netspeed theme                          # 顯示目前主題
netspeed theme preset ocean             # 套用預設組 (default / mono / ocean / solarized)
netspeed theme set error magenta        # 自訂某項顏色
netspeed theme set title "blue,bold"    # 顏色可逗號串接多個樣式
netspeed theme reset                    # 還原預設
netspeed theme export my-theme.json     # 匯出主題分享
netspeed theme import my-theme.json     # 匯入他人主題
```

可設定的項目：`title`、`heading`、`command`、`ok`、`warn`、`error`、`info`、`prompt`、`dim`；
顏色值用 ANSI 名稱（`red`/`green`/`yellow`/`blue`/`magenta`/`cyan`/`bold`/`dim`，`default` 表示不著色），
多個用逗號串接，例如 `cyan,bold`。整份設定也可用 `config export|import <檔案>` 分享。

---

## 11. 疑難排解

- **`netspeed` 找不到**：安裝後請重開終端機（PATH 變更需重新載入）。
- **Ping 顯示「無回應」**：ICMP 需要權限；無權限時會自動退回系統 `ping`。可用
  `netspeed ping <目標> --method system` 強制使用系統 ping。
- **Cloudflare 上下載出現 `n/a`**：多為當下網路或連線被中斷，重跑即可。

---

## 授權

本專案採用 [MIT License](LICENSE)。
