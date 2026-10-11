# netspeed — cross-platform CLI network speed toolkit

[English](#english) · [繁體中文](#繁體中文)

A single Python file for **Windows / macOS / Linux**. Measure speed from multiple sources,
run a two-end pair test, ping, look up your public IP & ISP, and compile everything into a
summary report; history entries can be re-run.

- **Bilingual**: built-in **English / 繁體中文**, default **English**, switchable anytime (§13).
- **Prints the version on startup**: each run shows the current version (e.g. `netspeed v1.6.0`).
- **Debug mode**: `show debug enable` prints every command and the raw output of external
  programs, so a user can paste a complete log when something misbehaves (§14).

---

<a id="english"></a>
## English

### 1. Requirements

- **Python 3.8+** (already present on most systems; on Windows install from
  <https://www.python.org/downloads/> and tick "Add Python to PATH").
- **Cloudflare speed test, public IP/ISP and ping need no extra packages** (all built in).
- The other speed sources are **optional** and only need their external tool when used (§8).
  Run `speed check` to check and optionally download the missing executables.

### 2. One-line download & run (fastest)

Copy-paste and run (needs Python 3):

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/mwp2100/netspeed/main/netspeed.py -o netspeed.py && python3 netspeed.py

# Windows PowerShell
irm https://raw.githubusercontent.com/mwp2100/netspeed/main/netspeed.py -OutFile netspeed.py; python netspeed.py
```

Want to keep it around? Install it (see §4): `python3 netspeed.py install`.

### 3. How to run

```bash
python netspeed.py            # no args -> countdown, then the default speed test
python netspeed.py console    # interactive console (alias: shell)
python netspeed.py all        # public IP + ping + all sources -> summary report
python netspeed.py speed all  # only all speed sources (no ip / ping)
```

With **no arguments** it shows "will run with default settings" and a **5-second countdown**
("Countdown Ns... (Enter to run now, c for console)"):

- **Enter** → start immediately
- `c` → enter the interactive console
- **any other key** → cancel
- no key → after the countdown, run the default test (public IP + ping + all sources)

With **arguments** the command runs directly (CLI mode), e.g. `netspeed speed cloudflare --seconds 15`.

In the interactive console, type `help` for commands and `exit` to quit:

```bash
# netspeed> help
# netspeed> show interfaces
# netspeed> speed cloudflare
# netspeed> exit
```

Input helpers:

- **Command prefix completion**: a unique prefix is completed on submit, e.g.
  `in` → `install`, `wr` → `write`, `sho` → `show`.
- Type `sh` then `?` → lists `show`-related commands; press `Tab` to complete.
- If the input is already a full command (e.g. `show`), `Enter` runs it.
- **Arguments complete too**: `show ` lists `ping/tools/env/interfaces/...`; `speed ` lists
  `all/mlab/fast/cloudflare/ookla/libre/check`; `ip ` lists `--provider` etc.
- If there is **nothing to complete** after a command, `Tab`/`?` just moves to a new line.
- Slower reads (`netinfo`, `show tools`, `show env`, `ip`) are **cached** after the first run;
  force a reload with `--refresh` or by typing `refresh`.
- **History & cursor**: `↑`/`↓` recall previous/next command, `←`/`→` move the cursor,
  `Home`/`End` jump to line start/end, `Delete` deletes.
- Pressing `?` with no matches just moves to a new line.

### 4. Install (recommended — creates the `netspeed` command)

One command installs to your user directory, creates a launcher and adds it to PATH:

```bash
# Windows
python netspeed.py install

# macOS / Linux
python3 netspeed.py install
```

After installing, **reopen your terminal**, then use it anywhere:

```bash
netspeed all
netspeed ping 8.8.8.8
netspeed pair client 192.168.1.20
```

Install locations:

| Platform | Program | Launcher | PATH |
|----------|---------|----------|------|
| Windows | `%LOCALAPPDATA%\Programs\netspeed\netspeed.py` | `netspeed.cmd` in the same folder | added to the **user** PATH (not the system one) |
| macOS / Linux | `~/.local/share/netspeed/netspeed.py` | `~/.local/bin/netspeed` | `~/.local/bin` added to your shell profile |

> Settings and history live in the user config directory (§7).

### 5. Uninstall

```bash
# Windows
python netspeed.py uninstall

# macOS / Linux
python3 netspeed.py uninstall
```

Removes the program file, launcher and PATH entry. **Settings and history are kept** by default;
to remove them too:

```bash
python netspeed.py uninstall --purge     # Windows
python3 netspeed.py uninstall --purge    # macOS / Linux
```

Manual removal: delete the install folder (§4) and the PATH entry; for settings/history delete the
folder listed in §7.

### 6. Command overview

Commands are layered (top-level stays small):

```
netspeed                        countdown, then the default test (public IP + ping + all sources)
netspeed console                interactive mode (alias: shell)

netspeed all                    public IP + ping + all sources -> summary report
netspeed speed all              only all speed sources (no ip / ping)
netspeed speed mlab|fast|cloudflare|ookla|libre
netspeed speed check            check speed tools, offer to download missing ones
netspeed ping [targets...]      ping directly (header shows params + gateway)
netspeed ping                   with no target -> interactive menu (below)

netspeed ip [-p provider]       public IP & ISP (default ipinfo.io, switchable)
netspeed show ping              ping settings (with current gateway)
netspeed show tools             tool status (* = runs under `all`; ping / speed / pair)
netspeed show speed             speed tool check (same as speed check)
netspeed show env               environment check (Python / config dir / internet)
netspeed show debug [enable|disable]   debug mode (see §14)
netspeed show interfaces        local network (DHCP/IP/gateway/link speed/routes/sub-IP)
netspeed show config|theme|history|version
netspeed netinfo [iface] [--all] [-i] [--refresh]   same as show interfaces

netspeed pair serve             run pair server (iperf3 preferred, else builtin; TCP/UDP)
netspeed pair client <host>     connect to peer (down/up/both; --udp for UDP)
netspeed pair history           pair history

netspeed config show|path|edit|reset|export|import   config file (export/import to share)
netspeed config set <key> <value>   e.g. config set iperf.path /usr/local/bin/iperf3
netspeed config set lang zh-TW      switch language (en / zh-TW)
netspeed config get <key>
netspeed config targets [list|add|del|set|gateway]
netspeed config endpoints [list|set <source> <value>]
netspeed config theme [list|set|reset|preset|export|import]
netspeed history                history; enter a number to view details and re-run

netspeed update [--reset-skip]  check/update to latest (source: update.repo)
netspeed version                show version
netspeed install [-y] | uninstall [--purge] [-y]

# Cisco style (inside console)
netspeed> show ping             ping settings (with gateway)
netspeed> show tools / show env / show interfaces / show debug
netspeed> configure             enter config mode (prompt becomes netspeed(config)#)
```

> To keep the top level small, the following flat commands **still work but are hidden from
> help** — prefer the layered form: `mlab`/`fast`/`cloudflare`/`ookla`/`libre` → `speed <x>`;
> `set`/`get`/`targets`/`endpoints`/`theme` → `config <x>`. `ip`, `check` and `netinfo` remain
> top-level and also have aliases like `show tools` / `show interfaces`.

Common options: `--json`, `--no-color`, `--debug`, `--no-save`, `--config <path>`, `--no-update-check`.

#### Ping header & interactive menu

A direct `ping` prints the current parameters **and the detected default gateway**:

```
================================================================
  Ping test count:5 interval:1.0 timeout:2.0 size:56 gateway:192.168.1.1
================================================================
```

With no target (and an interactive terminal) it opens a menu:

```
    [1] All targets
    [2] Pick one target      # one more level lists the targets
    [3] Change settings      # one more level
    [Enter/q] cancel
```

`[3] Change settings` lets you adjust (same settings as `config`; saved on change):

```
    [1] Auto default gateway: on/off
    [2] Target list (3)      # add / delete targets
    [3] Parameters           # count / interval / timeout / size / method
```

#### Speed tool check (speed check / show speed)

`speed check` (or `show speed`) lists each source's executable; if any are missing it asks:

- `y` = **auto-download** (GitHub Releases types download the matching platform file into the
  netspeed install dir; fast.com tries `npm install -g`; the rest show the official URL)
- `h` = show **how to download** (per-tool instructions and URLs)
- any other key = back

Add `-y` to skip the prompt and try downloading immediately.

#### Pair UDP (pair --udp)

Add `--udp` to `pair client` for a UDP test (iperf3 mode adds `-u`; use `-b` for a target
bandwidth such as `-b 100M`, `-b 0` = unlimited; builtin mode has its own UDP test). UDP results
also show jitter and loss. `pair serve` listens on both TCP and UDP, no extra config needed.

```bash
netspeed pair serve                       # listens on TCP/UDP
netspeed pair client 192.168.1.20 --udp --duration 10 -b 200M
```

#### Examples

```bash
netspeed speed check                                 # check/download speed tools
netspeed show tools                                  # tool status (* = runs under all)
netspeed show env                                    # environment check
netspeed ip -p ip-api.com                            # switch IP lookup source
netspeed netinfo                                     # show the interface with the default route
netspeed netinfo -i                                  # interactive interface picker (Enter/q to quit)
netspeed netinfo --all --json
netspeed speed all --sources cloudflare,ookla --seconds 15
netspeed ping 168.95.1.1 -c 10 -i 0.5 -W 2 -s 56
netspeed pair serve --port 5201
netspeed pair client 192.168.1.20 --mode both --duration 10 --streams 4
netspeed set iperf.path "C:\tools\iperf3.exe"
netspeed targets add 168.95.1.1
netspeed history --kind pair
```

### 7. Config & history location

Override with the `NETSPEED_CONFIG_DIR` environment variable.

| Platform | Path |
|----------|------|
| Windows | `%APPDATA%\netspeed\` |
| macOS | `~/Library/Application Support/netspeed/` |
| Linux | `~/.config/netspeed/` (or `$XDG_CONFIG_HOME/netspeed`) |

- `config.json`: all settings (language, ping list/params, speed endpoints, iperf path, enabled sources).
- `history.jsonl`: history. **Each operation is one entry**: a `ping` (even with several targets),
  a `speed` (several sources), an `all` each become one entry; `show history` prints one line per
  entry, and entering a number opens the full result and lets you re-run.

`netspeed config edit` opens the whole config in your editor.

### 8. Optional external tools (only needed for that source)

| Source | Needs | Install |
|--------|-------|---------|
| Cloudflare | none | builtin |
| Public IP / ISP | none | builtin |
| Ping | none | builtin pure-Python ICMP; falls back to system `ping` without privileges |
| Pair (builtin mode) | none | builtin TCP test |
| M-Lab | `ndt7-client` | download from <https://github.com/m-lab/ndt7-client-go/releases> |
| fast.com | `fast` | install Node.js, then `npm install -g fast-cli` |
| Ookla | `speedtest` | download the official CLI from <https://www.speedtest.net/apps/cli> |
| LibreSpeed | `librespeed-cli` | download from <https://github.com/librespeed/speedtest-cli/releases> |
| Pair (iperf3 mode) | `iperf3` | Linux `apt install iperf3` / macOS `brew install iperf3` / Windows iperf3.exe |

**Where to put a tool?** Either is fine:

1. Somewhere on PATH, or
2. **Next to `netspeed`** (the folder containing `netspeed.py`) — netspeed finds it automatically.
   On Windows it also looks in the npm global directory, so `npm install -g fast-cli` is picked up
   even if that folder is not on PATH.

If a tool is missing, that source shows a hint and a download URL; everything else still works.
iperf3 can also be pointed to a custom path with `netspeed set iperf.path <path>`. Use
`netspeed speed check` to see status and **auto-download** missing executables; `netspeed show tools`
marks with `*` which items run under `all`.

### 9. Version update (GitHub)

While running (including when entering the console) it checks GitHub Releases per config; if a newer
version exists it shows the notes and asks:

- `y` = update (downloads the latest `netspeed.py`, keeps a `.bak` backup)
- `n` = later
- `s` = don't notify for this version

The update source defaults to **`mwp2100/netspeed`** and works out of the box. To point at your fork:

```bash
netspeed set update.repo <your-account>/<repo>
netspeed update                  # check now (forced)
netspeed update --reset-skip     # clear the "don't notify" version
netspeed set update.check_on_start false   # disable the startup check
netspeed set update.interval_hours 24      # check interval (hours)
```

Set `update.repo` to an empty string to disable checking entirely; `--no-update-check` skips one run.

### 10. Cisco-style config mode (console)

Inside the console you can use switch-like commands:

```
netspeed> show config          # = config
netspeed> show ping            # ping settings (with gateway)
netspeed> show tools           # tool status (* = runs under all)
netspeed> show speed           # speed tool check (= speed check)
netspeed> show env             # environment check
netspeed> show debug           # debug mode state / enable / disable
netspeed> show interfaces      # = netinfo
netspeed> show conf            # unique-prefix abbreviations work
netspeed> show                 # list all show topics
netspeed> configure            # enter config mode (or conf)
netspeed(config)# set ping.count 10
netspeed(config)# set lang zh-TW
netspeed(config)# set debug true
netspeed(config)# endpoints set ookla 12345
netspeed(config)# theme set ok magenta
netspeed(config)# no ping.count        # reset a key to default
netspeed(config)# show config          # read commands still work
netspeed(config)# write                # save to file (or save)
netspeed(config)# end                  # leave (or exit)
```

Key points:

- Changes in config mode **apply live** but are only **written to file on `write`**; if you `end`
  without writing, it warns that the next run still uses the old settings.
- `show` map: `config` → config, `ping` → ping settings, `tools` → tool status, `speed` → speed tool
  check, `env` → environment check, `debug` → debug mode, `interfaces` → `netinfo`, plus `theme`,
  `history`, `version`.
- You can also run `netspeed show config` directly (no console needed).
- **When already in the console**: `help` hides the `console` command and typing `console` is blocked.
- Prefixes complete: `sho` → `show`, `conf` → `configure`, `wr` → `write`, `in` → `install`.

### 11. Color themes

Customize each element's color; takes effect next run after saving, and can be exported to share:

```bash
netspeed theme                          # show current theme
netspeed theme preset ocean             # apply a preset (default / mono / ocean / solarized)
netspeed theme set error magenta        # set one element's color
netspeed theme set title "blue,bold"    # comma-join multiple styles
netspeed theme reset                    # restore defaults
netspeed theme export my-theme.json     # export to share
netspeed theme import my-theme.json     # import someone's theme
```

Configurable keys: `title`, `heading`, `command`, `ok`, `warn`, `error`, `info`, `prompt`, `dim`;
values are ANSI names (`red`/`green`/`yellow`/`blue`/`magenta`/`cyan`/`bold`/`dim`, `default` = no
color), comma-joined, e.g. `cyan,bold`. The whole config can be shared with
`config export|import <file>`.

### 12. Troubleshooting

- **`netspeed` not found**: reopen the terminal after installing (PATH changes need a reload).
- **Ping shows "no reply"**: ICMP needs privileges; without them it falls back to the system `ping`.
  Force it with `netspeed ping <target> --method system`.
- **Cloudflare up/down shows `n/a`**: usually a transient network drop; just re-run.
- **A tool seems "downloaded" but is still reported missing**: run `show speed` again; if it persists,
  turn on debug mode (§14) and check the raw npm output and the npm global directory in the log.

### 13. Language

Built-in **English** and **繁體中文**; **default is English**. The strings are embedded in the
program, no language files needed.

```bash
netspeed config lang zh-TW         # switch to Traditional Chinese
netspeed config set lang zh-TW     # same
netspeed config lang en            # switch to English (default)
netspeed config lang               # show current language
```

Also adjustable in console config mode:

```
netspeed> configure
netspeed(config)# set lang zh-TW
netspeed(config)# write
```

Stored in the `lang` field of `config.json` (`en` or `zh-TW`). Commands, prompts, errors and report
titles follow the language, as do `help`, `show` topics and interactive menus.

### 14. Debug mode

When something goes wrong, turn on debug mode to print **every command it runs and the raw,
unprocessed output** of external programs — enough for a user to hand you a complete log.

```bash
netspeed show debug enable     # turn on (saved; applies to future runs)
netspeed show debug disable    # turn off
netspeed show debug            # show current state
```

- Inside the console: `show debug enable|disable`.
- One-off without changing config: add `--debug` to any command, or set `NETSPEED_DEBUG=1`.
- Debug output goes to **stderr**, so `--json` and pipes stay clean. Save it with a redirect:

```bash
netspeed all 2> debug.log
```

Everything is prefixed `[debug]` and includes: the command line, each external program invocation
(full command, exit code, elapsed time, raw stdout/stderr), HTTP GET results, and tool-search
results. This is usually enough to diagnose failures such as a tool that reports success but is not
actually installed.

---

<a id="繁體中文"></a>
## 繁體中文

單一 Python 檔案，支援 **Windows / macOS / Linux**。可對多個來源測速、兩端對測、ping、
查外網 IP 與電信商，並把結果整理成統整報告，歷史紀錄可挑選重跑。

- **中英文語系**：內建 **English / 繁體中文**，預設英文，可隨時切換（見 §13）。
- **執行時顯示版本**：每次執行都會在開頭顯示目前版號（例如 `netspeed v1.6.0`）。
- **Debug 模式**：`show debug enable` 會印出每個指令與外部程式的原始輸出，方便回報問題（見 §14）。

### 1. 系統需求

- **Python 3.8+**（已內建於多數系統；Windows 可從 <https://www.python.org/downloads/> 安裝，
  安裝時勾選「Add Python to PATH」）。
- 免安裝任何套件即可使用 **Cloudflare 測速、外網 IP/電信商、Ping**（皆為內建實作）。
- 其餘測速來源為**選用**，用到時才需要對應的外部工具（見 §8）。可用 `speed check`
  檢查並協助下載缺少的執行檔。

### 2. 一行下載並執行（最快）

複製貼上就能跑（需要 Python 3）：

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/mwp2100/netspeed/main/netspeed.py -o netspeed.py && python3 netspeed.py

# Windows PowerShell
irm https://raw.githubusercontent.com/mwp2100/netspeed/main/netspeed.py -OutFile netspeed.py; python netspeed.py
```

跑完想留下來長期用，就執行安裝（見 §4）：`python3 netspeed.py install`。

### 3. 執行方式

```bash
python netspeed.py            # 不帶參數 → 倒數後跑預設測速
python netspeed.py console    # 進入互動 console（shell 亦可）
python netspeed.py all        # 外網 IP + Ping + 全部來源 統整報告
python netspeed.py speed all  # 只跑全部測速來源（不含 ip / ping）
```

**不帶參數**會先顯示「將以預設設定執行」並**倒數 5 秒**；倒數畫面會顯示
「倒數 N 秒...（按 Enter 直接執行, c 進互動模式）」：

- 按 **Enter** → 立即開始測試
- 按 `c` → 進入互動 console
- 按**其他任一鍵** → 取消
- 不按鍵 → 倒數結束後跑預設測速（外網 IP + Ping + 全部來源）

**帶參數**則直接執行該指令（cli 模式），例如 `netspeed speed cloudflare --seconds 15`。

互動 console 輸入 `help` 看指令、`exit` 離開：

```bash
# netspeed> help
# netspeed> show interfaces
# netspeed> speed cloudflare
# netspeed> exit
```

輸入時的輔助：

- **指令前綴自動補齊**：送出時若只打了唯一前綴，會自動視為完整指令，例如
  `in` → `install`、`wr` → `write`、`sho` → `show`。
- 打 `sh` 再按 `?` → 列出 `show` 相關；接著按 `Tab` 會自動補齊。
- 若輸入已是完整指令（例如 `show`），直接 `Enter` 就會執行。
- **參數也能補齊**：`show ` 會列出 `ping/tools/env/interfaces/...`；`speed ` 會列出
  `all/mlab/fast/cloudflare/ookla/libre/check`；`ip ` 會列出 `--provider` 等選項。
- 若指令後面**沒有可補的參數**，按 `Tab` 或 `?` 只會跳一行空白，不會多印訊息。
- 較慢的讀取（`netinfo`、`show tools`、`show env`、`ip`）第一次執行後會**快取**；
  要強制重讀用 `--refresh` 或輸入 `refresh` 清快取。
- **歷史與游標**：按 `↑`/`↓` 叫回上/下一筆指令，`←`/`→` 移動游標，`Home`/`End` 到行首/行尾，`Delete` 刪除。
- 打 `?` 若沒有任何符合項目，只會跳一行，不會多印訊息。

### 4. 安裝（建議，會產生 `netspeed` 指令）

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

> 設定與歷史紀錄存放於使用者設定目錄（見 §7）。

### 5. 移除

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

手動移除（若不使用 `uninstall`）：刪除安裝資料夾（見 §4）與 PATH 設定；
設定/歷史則刪除 §7 所列的資料夾。

### 6. 指令總覽

指令採分層（top-level 精簡）：

```
netspeed                        倒數後跑預設測速（外網IP + Ping + 全部來源）
netspeed console                進入互動模式（shell 亦可）

netspeed all                    外網 IP + Ping + 全部來源 → 統整報告
netspeed speed all              只跑全部測速來源（不含 ip / ping）
netspeed speed mlab|fast|cloudflare|ookla|libre
netspeed speed check            檢查測速工具，可協助下載缺少的執行檔
netspeed ping [目標...]         直接 ping（標頭會顯示目前參數與 gateway）
netspeed ping                   不帶目標時進入互動選單（見下）

netspeed ip [-p provider]       外網 IP 與電信商（預設 ipinfo.io，可切換）
netspeed show ping              Ping 設定（含目前 gateway 值）
netspeed show tools             工具狀態（* = 執行 all 時會跑；分 ping / 測速 / 對測）
netspeed show speed             測速工具檢查（等同 speed check）
netspeed show env               環境狀態檢查（Python / 設定目錄 / 網際網路）
netspeed show debug [enable|disable]   debug 模式（見 §14）
netspeed show interfaces        本機網路設定（DHCP/IP/閘道/Link Speed/靜態路由/Sub IP）
netspeed show config|theme|history|version
netspeed netinfo [介面] [--all] [-i] [--refresh]   等同 show interfaces

netspeed pair serve             開對測伺服器（優先 iperf3，否則內建；支援 TCP/UDP）
netspeed pair client <host>     連到對測端（下載/上傳/雙向；--udp 可測 UDP）
netspeed pair history           對測歷史

netspeed config show|path|edit|reset|export|import   設定檔（可匯出/匯入分享）
netspeed config set <key> <value>   例: config set iperf.path /usr/local/bin/iperf3
netspeed config set lang zh-TW      切換語系（en / zh-TW）
netspeed config get <key>
netspeed config targets [list|add|del|set|gateway]
netspeed config endpoints [list|set <來源> <值>]
netspeed config theme [list|set|reset|preset|export|import]
netspeed history                歷史紀錄，輸入編號可查看內容並重跑

netspeed update [--reset-skip]  檢查/更新到最新版本（來源: update.repo）
netspeed version                顯示版本
netspeed install [-y] | uninstall [--purge] [-y]

# Cisco 風格（console 內）
netspeed> show ping             顯示 Ping 設定（含 gateway）
netspeed> show tools / show env / show interfaces / show debug
netspeed> configure             進入設定模式（提示字元變 netspeed(config)#）
```

> 為精簡 top-level 項目，以下平鋪指令**仍可用但不在 help 顯示**，建議改用分層寫法：
> `mlab`/`fast`/`cloudflare`/`ookla`/`libre` → `speed <x>`；
> `set`/`get`/`targets`/`endpoints`/`theme` → `config <x>`。
> `ip`、`check`、`netinfo` 仍為 top-level，也有 `show tools`/`show interfaces` 等別名。

常用選項：`--json`、`--no-color`、`--debug`、`--no-save`、`--config <path>`、`--no-update-check`。

#### Ping 標頭與互動選單

直接 `ping` 時，標頭會帶出目前使用的參數**與目前偵測到的 default gateway**：

```
================================================================
  Ping 測試 count:5 interval:1.0 timeout:2.0 size:56 gateway:192.168.1.1
================================================================
```

不帶目標（且為互動終端）時會進入選單：

```
    [1] 全部目標
    [2] 選擇單一目標        # 再進一層列出目標供選擇
    [3] 修改設定            # 再進一層
    [Enter/q] 取消
```

`[3] 修改設定` 內可調整（與 `config` 編輯同一套設定，改完即存檔）：

```
    [1] 預設閘道自動加入: on/off
    [2] 目標清單 (3 個)     # 可新增 / 刪除目標
    [3] 參數                # count / interval / timeout / size / method
```

#### 測速工具檢查（speed check / show speed）

`speed check`（或 `show speed`）會列出各測速來源的執行檔狀態，若有缺少，會詢問：

- `y` = 協助**自動下載**（GitHub Releases 類型會下載對應平台檔案到 netspeed 安裝目錄；
  fast.com 會嘗試 `npm install -g`；其餘顯示官方下載網址）
- `h` = 顯示**如何下載**（各工具的安裝說明與網址）
- 其他任一鍵 = 返回上層

加 `-y` 可略過詢問直接嘗試下載。

#### 對測 UDP（pair --udp）

`pair client` 加上 `--udp` 即可做 UDP 對測（iperf3 模式會帶 `-u`，可用 `-b` 指定目標頻寬，
例如 `-b 100M`，`-b 0` 為不限；內建模式亦有內建 UDP 對測）。UDP 結果會多顯示 Jitter 與遺失率。
伺服器端 `pair serve` 會同時監聽 TCP/UDP，不需額外設定。

```bash
netspeed pair serve                       # 同時監聽 TCP/UDP
netspeed pair client 192.168.1.20 --udp --duration 10 -b 200M
```

#### 範例

```bash
netspeed speed check                                 # 檢查/下載測速工具
netspeed show tools                                  # 檢查工具 (* = all 會執行)
netspeed show env                                    # 環境狀態檢查
netspeed ip -p ip-api.com                            # 換 IP 查詢來源
netspeed netinfo                                     # 自動顯示有 default route 的介面
netspeed netinfo -i                                  # 互動選單選介面 (Enter/q 離開)
netspeed netinfo --all --json
netspeed speed all --sources cloudflare,ookla --seconds 15
netspeed ping 168.95.1.1 -c 10 -i 0.5 -W 2 -s 56
netspeed pair serve --port 5201
netspeed pair client 192.168.1.20 --mode both --duration 10 --streams 4
netspeed set iperf.path "C:\tools\iperf3.exe"
netspeed targets add 168.95.1.1
netspeed history --kind pair
```

### 7. 設定與歷史紀錄位置

可用環境變數 `NETSPEED_CONFIG_DIR` 覆寫。

| 平台 | 路徑 |
|------|------|
| Windows | `%APPDATA%\netspeed\` |
| macOS | `~/Library/Application Support/netspeed/` |
| Linux | `~/.config/netspeed/`（或 `$XDG_CONFIG_HOME/netspeed`） |

- `config.json`：所有設定（語系、ping 清單/參數、測速端點、iperf 路徑、啟用來源）。
- `history.jsonl`：歷史紀錄。**每次操作視為一筆**：一次 `ping`（即使多個目標）、一次
  `speed`（多個來源）、一次 `all` 都各存成一筆；`show history` 每筆一行，輸入編號可進入
  查看完整結果並選擇重跑。

以 `netspeed config edit` 可直接用編輯器修改全部設定。

### 8. 選用外部工具（用到該來源才需要）

| 來源 | 需要 | 安裝方式 |
|------|------|----------|
| Cloudflare | 無 | 內建 |
| 外網 IP / 電信商 | 無 | 內建 |
| Ping | 無 | 內建純 Python ICMP；無權限時自動改用系統 `ping` |
| 對測（內建模式） | 無 | 內建 TCP 對測 |
| M-Lab | `ndt7-client` | 從 <https://github.com/m-lab/ndt7-client-go/releases> 下載 |
| fast.com | `fast` | 先裝 Node.js，再 `npm install -g fast-cli` |
| Ookla | `speedtest` | <https://www.speedtest.net/apps/cli> 下載官方 CLI |
| LibreSpeed | `librespeed-cli` | 從 <https://github.com/librespeed/speedtest-cli/releases> 下載 |
| 對測（iperf3 模式） | `iperf3` | Linux `apt install iperf3` / macOS `brew install iperf3` / Windows 下載 iperf3.exe |

**工具要放哪裡？** 兩種方式都行：

1. 放進 PATH（例如 Windows 的 `C:\Windows\System32` 或任一 PATH 目錄）。
2. **直接放在 `netspeed` 同層目錄**（也就是 `netspeed.py` 所在的資料夾）——netspeed 會自動找到。
   Windows 上也會搜尋 npm 全域目錄，因此 `npm install -g fast-cli` 即使該目錄不在 PATH 也能被找到。

找不到工具時，該來源會顯示提示與下載網址，其餘功能不受影響。iperf3 也可用
`netspeed set iperf.path <路徑>` 指定自訂執行檔位置。用 `netspeed speed check` 可隨時查看
狀態並**協助下載**缺少的執行檔；`netspeed show tools` 則以 `*` 標示哪些項目在執行 `all` 時會一併執行。

### 9. 版本更新（GitHub）

程式執行時（含進入 console 時）會依設定檢查 GitHub Releases 是否有新版本；有新版會顯示更新內容，並詢問：

- `y` = 更新（下載最新 `netspeed.py` 覆蓋，並保留 `.bak` 備份）
- `n` = 稍後
- `s` = 此版本不再通知（之後若有更新版本才會再提示）

更新來源預設為 **`mwp2100/netspeed`**，開箱即會檢查，不需設定。若要指向自己的 fork：

```bash
netspeed set update.repo <你的帳號>/<repo>   # 換成自己的 repo
netspeed update            # 立即檢查（強制）
netspeed update --reset-skip   # 清除「不再通知」的版本
netspeed set update.check_on_start false   # 關閉啟動時自動檢查
netspeed set update.interval_hours 24      # 檢查頻率（小時）
```

將 `update.repo` 設為空字串即可完全關閉檢查；也可用 `--no-update-check` 只略過單次。

### 10. Cisco 風格設定模式（console）

在 console 內可用 Cisco 交換器習慣的指令：

```
netspeed> show config          # = 原本的 config
netspeed> show ping            # Ping 設定（含目前 gateway）
netspeed> show tools           # 工具狀態（* = all 會執行）
netspeed> show speed           # 測速工具檢查（= speed check）
netspeed> show env             # 環境狀態檢查
netspeed> show debug           # debug 模式狀態 / 開啟 / 關閉
netspeed> show interfaces      # = netinfo
netspeed> show conf            # 支援縮寫（唯一前綴）
netspeed> show                  # 列出所有可用 show 項目
netspeed> configure            # 進入設定模式（或打 conf）
netspeed(config)# set ping.count 10
netspeed(config)# set lang zh-TW
netspeed(config)# set debug true
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
- `show` 對應表：`config` → `config`，`ping` → Ping 設定，`tools` → 工具狀態，
  `speed` → 測速工具檢查，`env` → 環境狀態，`debug` → debug 模式，`interfaces` → `netinfo`，
  另有 `theme`、`history`、`version`。
- 直接下 `netspeed show config` 也可以（不必進 console）。
- **已在 console 內時**：`help` 不會顯示 `console` 指令，輸入 `console` 也會被擋下（已在互動模式）。
- 指令前綴會自動補齊：`sho` → `show`、`conf` → `configure`、`wr` → `write`、`in` → `install`。

### 11. 顏色主題（theme）

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

### 12. 疑難排解

- **`netspeed` 找不到**：安裝後請重開終端機（PATH 變更需重新載入）。
- **Ping 顯示「無回應」**：ICMP 需要權限；無權限時會自動退回系統 `ping`。可用
  `netspeed ping <目標> --method system` 強制使用系統 ping。
- **Cloudflare 上下載出現 `n/a`**：多為當下網路或連線被中斷，重跑即可。
- **工具顯示「已下載」但仍找不到**：再跑一次 `show speed`；若持續如此，開啟 debug 模式（見 §14）
  並查看紀錄中的 npm 原始輸出與 npm 全域目錄。

### 13. 語系（Language）

內建 **English** 與 **繁體中文** 兩種語系，**預設為 English**。語系為程式內建，不需額外語系檔。

```bash
netspeed config lang zh-TW         # 切換繁體中文
netspeed config set lang zh-TW     # 同上（等價寫法）
netspeed config lang en            # 切換英文（預設）
netspeed config lang               # 查看目前語系
```

也可在 console 的設定模式內調整：

```
netspeed> configure
netspeed(config)# set lang zh-TW
netspeed(config)# write
```

設定值存於 `config.json` 的 `lang` 欄位（`en` 或 `zh-TW`）。指令、提示、錯誤訊息與報表
標題都會依語系顯示；`help`、`show` 各項目與互動選單亦同。

### 14. Debug 模式

遇到異常時，開啟 debug 模式會印出**每個執行的指令與外部程式未經處理的原始輸出**，
足以讓使用者提供完整資訊以便除錯。

```bash
netspeed show debug enable     # 開啟（寫入設定；之後執行皆生效）
netspeed show debug disable    # 關閉
netspeed show debug            # 顯示目前狀態
```

- 在 console 內：`show debug enable|disable`。
- 不想改設定的單次使用：任何指令加上 `--debug`，或設定環境變數 `NETSPEED_DEBUG=1`。
- 除錯訊息輸出到 **stderr**，所以 `--json` 與管線輸出不受影響。可用重導存檔：

```bash
netspeed all 2> debug.log
```

每行以 `[debug]` 開頭，內容包含：指令列、每個外部程式的完整指令與回傳碼/耗時/原始 stdout+stderr、
HTTP GET 結果、工具搜尋結果。這通常足以診斷「工具顯示成功但其實沒安裝」這類問題。

---

## 授權 / License

本專案採用 [MIT License](LICENSE)。
