#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netspeed — 跨平台 CLI 網路測速工具箱 (Windows / macOS / Linux)

功能:
  * 多來源測速: M-Lab NDT7 / fast.com(Netflix) / Cloudflare / Ookla(speedtest.net) / LibreSpeed
  * 對測 (兩端對開): 優先使用 iperf3 (可自訂執行檔路徑), 找不到時使用內建 TCP 對測
  * 對特定 IP 做 ping (可編輯清單, 自動偵測 default gateway, 可調次數/間隔/大小)
  * 查外網 IP 與電信商 (多來源自動備援)
  * 可編輯的端點設定 (設為預設值)
  * 歷史紀錄, 可從清單直接選取後重跑
  * 統整報告: 外網 IP/ISP + ping 統計 + 各來源測速

執行:
  netspeed                     倒數後跑預設測速 (Enter 可直接開始)
  netspeed all                 外網 IP + ping + 測速 統整報告
  netspeed speed all           只跑全部測速來源 (不含 ip/ping)
  netspeed speed mlab|fast|cloudflare|ookla|libre
  netspeed speed check         檢查測速工具並可協助下載缺少的執行檔
  netspeed ping [targets...]
  netspeed ip [-p provider]    外網 IP (預設 ipinfo.io, 可切換)
  netspeed pair serve|client <host>|history
  netspeed history             歷史紀錄 (每次操作一筆, 可進入查看)
  netspeed config|set|get|targets|endpoints|theme
  netspeed console             互動模式 (指令前綴自動補齊)

語系: 內建 English / 繁體中文, 預設英文 (config set lang zh-TW)。
設定檔與歷史紀錄位於使用者設定目錄下的 netspeed/ 資料夾。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

VERSION = "1.7.0"
APP = "netspeed"
PROTO_TAG = "NETSPEED1"

USE_COLOR = True

# --------------------------------------------------------------------------- #
# 語系 (i18n) — 程式內帶, 預設英文; 可用 config lang 切換
# --------------------------------------------------------------------------- #

LANG = "en"
LANG_NAMES = {"en": "English", "zh-TW": "繁體中文"}


def _norm_lang(value) -> str:
    s = str(value or "").strip().lower().replace("_", "-")
    if s in ("zh", "zh-tw", "zh-hant", "zh-hant-tw", "tw", "cht", "chinese", "traditional"):
        return "zh-TW"
    return "en"


def t(zh: str, en: str) -> str:
    """回傳目前語系的字串; 預設語系為英文。"""
    return en if LANG == "en" else zh


def apply_lang(cfg: dict) -> None:
    global LANG
    LANG = _norm_lang(cfg.get("lang", "en"))

# --------------------------------------------------------------------------- #
# 基本工具
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def config_dir() -> str:
    override = os.environ.get("NETSPEED_CONFIG_DIR")
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP)


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def history_path() -> str:
    return os.path.join(config_dir(), "history.jsonl")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# --------------------------------------------------------------------------- #
# 顏色 / 輸出
# --------------------------------------------------------------------------- #

_CODES = {
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "bold": "1", "dim": "2",
}


def col(text: str, *styles: str) -> str:
    if not USE_COLOR:
        return text
    codes = ";".join(_CODES.get(s, "0") for s in styles)
    return f"\033[{codes}m{text}\033[0m"


DEFAULT_THEME = {
    "title": "cyan,bold",      # 區塊標題
    "heading": "yellow,bold",  # help 段落標題
    "command": "green",        # 指令名稱
    "ok": "green",
    "warn": "yellow",
    "error": "red",
    "info": "default",         # default = 不著色
    "prompt": "green,bold",    # console 提示字元
    "dim": "dim",
}

THEME = dict(DEFAULT_THEME)

THEME_PRESETS = {
    "default": dict(DEFAULT_THEME),
    "mono": {k: "default" for k in DEFAULT_THEME},
    "ocean": {**DEFAULT_THEME, "title": "blue,bold", "heading": "cyan,bold",
              "command": "cyan", "prompt": "blue,bold"},
    "solarized": {**DEFAULT_THEME, "title": "yellow,bold", "heading": "cyan,bold",
                  "command": "green", "prompt": "yellow,bold"},
}


def tcol(text: str, key: str) -> str:
    styles = THEME.get(key, "")
    if not styles or styles == "default":
        return text
    return col(text, *styles.split(","))


def apply_theme(cfg: dict) -> None:
    global THEME
    THEME = deep_merge(DEFAULT_THEME, cfg.get("theme", {}) or {})
    apply_lang(cfg)


def section(title: str) -> None:
    line = "=" * 64
    print()
    print(tcol(line, "title"))
    print(tcol(f"  {title}", "title"))
    print(tcol(line, "title"))


def info(msg: str) -> None:
    print(tcol(msg, "info"))


def warn(msg: str) -> None:
    print(tcol(f"[!] {msg}", "warn"))


def error(msg: str) -> None:
    print(tcol(f"[x] {msg}", "error"))


def ok(msg: str) -> None:
    print(tcol(f"[v] {msg}", "ok"))


# --------------------------------------------------------------------------- #
# 進度顯示 — 執行中就地更新同一行 (例如 "(1/4) 正在 ping ..."), 讓使用者知道
# 沒卡住、目前跑到第幾項; 結果仍由最後的統整表呈現。僅在終端機 (tty) 顯示。
# --------------------------------------------------------------------------- #

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_PROGRESS_LEN = 0


def _visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def progress(text: str) -> None:
    global _PROGRESS_LEN
    if not sys.stdout.isatty():
        return
    pad = max(0, _PROGRESS_LEN - _visible_len(text))
    sys.stdout.write("\r" + text + " " * pad)
    sys.stdout.flush()
    _PROGRESS_LEN = _visible_len(text)


def progress_end() -> None:
    global _PROGRESS_LEN
    if sys.stdout.isatty() and _PROGRESS_LEN:
        sys.stdout.write("\r" + " " * _PROGRESS_LEN + "\r")
        sys.stdout.flush()
    _PROGRESS_LEN = 0


# --------------------------------------------------------------------------- #
# Debug 模式 — 開啟後把所有指令與外部程式的原始輸出 (未經處理) 印到 stderr,
# 方便使用者回報問題時貼上完整資訊。輸出到 stderr 才不會污染 --json / 管線。
# --------------------------------------------------------------------------- #

DEBUG = False


def dbg(category: str, message: str) -> None:
    """開啟 debug 時, 印出一行除錯訊息到 stderr。"""
    if not DEBUG:
        return
    try:
        sys.stderr.write(f"[debug] {category}: {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


def dbg_block(title: str, text: str) -> None:
    """開啟 debug 時, 印出一段原始 (未處理) 的文字區塊到 stderr。"""
    if not DEBUG or not text:
        return
    try:
        sys.stderr.write(f"[debug] --- {title} ---\n")
        sys.stderr.write(text if text.endswith("\n") else text + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def cmd_str(cmd) -> str:
    """把指令 list 還原成可讀、可複製的字串。"""
    try:
        return " ".join(shlex.quote(str(c)) for c in cmd)
    except Exception:
        return str(cmd)


def _debug_enabled(cfg: dict | None = None) -> bool:
    env = os.environ.get("NETSPEED_DEBUG", "").strip().lower()
    if env and env not in ("0", "false", "no", "off"):
        return True
    return bool(cfg.get("debug")) if cfg else False


# 本次執行/console session 內的快取 (給讀取較慢的指令用, 例如 netinfo)
_SESSION_CACHE: dict = {}


def cache_clear() -> None:
    _SESSION_CACHE.clear()


# --------------------------------------------------------------------------- #
# 設定檔
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "lang": "en",                  # en | zh-TW (預設英文)
    "debug": False,                # True = 執行時輸出 debug 訊息到 stderr
    "ping": {
        "method": "auto",          # auto | icmp | system
        "count": 5,
        "interval": 1.0,
        "timeout": 2.0,
        "size": 56,
        "auto_gateway": True,
        "targets": [
            "1.1.1.1",
            "8.8.8.8",
            "9.9.9.9",
        ],
    },
    "cloudflare": {
        "seconds": 10,
        "chunk_bytes": 25000000,
    },
    "iperf": {
        "path": "",                # 空 = 自動偵測 iperf3 / iperf
        "port": 5201,
        "duration": 8,
        "streams": 1,
    },
    "endpoints": {
        "mlab": "",                # 自訂 ndt7 伺服器 (空 = 自動)
        "fast": "",                # fast-cli 無自訂伺服器, 保留欄位
        "cloudflare": "https://speed.cloudflare.com",
        "ookla": "",               # Ookla server id (空 = 自動選)
        "libre": "",               # LibreSpeed 伺服器 URL (空 = 自動選)
    },
    "ip": {
        "provider": "ipinfo.io",   # ipinfo.io | ifconfig.co | ip-api.com
    },
    "sources": ["mlab", "fast", "cloudflare", "ookla", "libre"],
    "history": {
        "max": 300,
    },
    "update": {
        "repo": "mwp2100/netspeed",   # GitHub "owner/repo", 空 = 不檢查
        "check_on_start": True,
        "interval_hours": 24,
        "skip_version": "",
        "last_check": 0,
    },
    "theme": dict(DEFAULT_THEME),
}


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, val in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(path: str | None = None) -> dict:
    p = path or config_path()
    if not os.path.exists(p):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        save_config(cfg, p)
        return cfg
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        warn(t(f"設定檔讀取失敗 ({exc}), 使用預設值", f"Failed to read config ({exc}), using defaults"))
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return deep_merge(DEFAULT_CONFIG, data)


_DEFER_SAVE = False  # config 模式下為 True: 變更先套用到記憶體, 等 write 才寫檔


def save_config(cfg: dict, path: str | None = None, force: bool = False) -> None:
    if _DEFER_SAVE and not force:
        return
    p = path or config_path()
    ensure_dir(os.path.dirname(p))
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


def coerce(value: str):
    low = value.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if value.strip().startswith(("[", "{")):
        try:
            return json.loads(value)
        except Exception:
            pass
    return value


def set_dotted(cfg: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def get_dotted(cfg: dict, dotted: str):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# --------------------------------------------------------------------------- #
# 歷史紀錄
# --------------------------------------------------------------------------- #


def load_history() -> list:
    p = history_path()
    if not os.path.exists(p):
        return []
    records = []
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


def append_history(record: dict, cfg: dict) -> None:
    p = history_path()
    ensure_dir(os.path.dirname(p))
    record.setdefault("time", now_iso())
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    _trim_history(cfg)


def _trim_history(cfg: dict) -> None:
    limit = get_dotted(cfg, "history.max") or 300
    p = history_path()
    if not os.path.exists(p):
        return
    with open(p, "r", encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip()]
    if len(lines) <= limit:
        return
    with open(p, "w", encoding="utf-8") as fh:
        fh.writelines(lines[-limit:])


# --------------------------------------------------------------------------- #
# 外部程式偵測 / 執行
# --------------------------------------------------------------------------- #


_NPM_BIN_CACHE = None


def _npm_global_bin() -> str | None:
    """npm 全域執行檔目錄 (Windows 在 prefix 本身, Unix 在 prefix/bin)。"""
    global _NPM_BIN_CACHE
    if _NPM_BIN_CACHE is not None:
        return _NPM_BIN_CACHE or None
    _NPM_BIN_CACHE = ""
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        proc = run_cmd([npm, "prefix", "-g"], timeout=20)
    except Exception:
        return None
    prefix = (proc.stdout or "").strip()
    if proc.returncode != 0 or not prefix:
        return None
    path = prefix if os.name == "nt" else os.path.join(prefix, "bin")
    if os.path.isdir(path):
        _NPM_BIN_CACHE = path
        return path
    return None


def _local_tool_dirs() -> list:
    dirs = []
    for d in (os.path.dirname(os.path.abspath(__file__)), install_dir(), os.getcwd(),
              _npm_global_bin()):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def find_tool(names, custom: str | None = None) -> str | None:
    if custom:
        if os.path.isabs(custom) and os.path.exists(custom):
            return custom
        found = shutil.which(custom)
        if found:
            return found
        if os.path.exists(custom):
            return custom
    if isinstance(names, str):
        names = [names]
    for name in names:
        found = shutil.which(name)
        if found:
            dbg("find_tool", f"{names} -> {found}")
            return found
    exts = [""] if os.name != "nt" else [".exe", ".cmd", ".bat", ".ps1", ""]
    for d in _local_tool_dirs():
        for name in names:
            for ext in exts:
                cand = os.path.join(d, name + ext)
                if os.path.isfile(cand) and (os.name == "nt" or os.access(cand, os.X_OK)):
                    dbg("find_tool", f"{names} -> {cand}")
                    return cand
    dbg("find_tool", f"{names} -> not found")
    return None


def _fix_cmd(cmd: list) -> list:
    if os.name == "nt" and cmd and cmd[0].lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c"] + cmd
    return cmd


def run_cmd(cmd: list, timeout: float | None = None, input_data: bytes | None = None):
    fixed = _fix_cmd(list(cmd))
    if DEBUG:
        dbg("exec", cmd_str(fixed) + (f"   (timeout={timeout}s)" if timeout else ""))
    t0 = time.time()
    try:
        proc = subprocess.run(
            fixed,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            input=input_data,
        )
    except FileNotFoundError:
        proc = subprocess.CompletedProcess(fixed, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        proc = subprocess.CompletedProcess(fixed, 124, out or "", (err or "") + "\n[timeout]")
    if DEBUG:
        dbg("exec", f"rc={proc.returncode}  {time.time() - t0:.2f}s")
        dbg_block("stdout", proc.stdout or "")
        dbg_block("stderr", proc.stderr or "")
    return proc


# --------------------------------------------------------------------------- #
# 外網 IP / 電信商 (多來源自動備援)
# --------------------------------------------------------------------------- #


def _http_get(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"netspeed/{VERSION}"})
    dbg("http", f"GET {url}")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception as exc:
        dbg("http", f"GET {url} -> ERROR {exc}")
        raise
    dbg("http", f"GET {url} -> {len(data)} bytes  {time.time() - t0:.2f}s")
    return data.decode("utf-8", errors="replace")


IP_PROVIDERS = {
    "ipinfo.io": "https://ipinfo.io/json",
    "ifconfig.co": "https://ifconfig.co/json",
    "ip-api.com": "http://ip-api.com/json",
}
DEFAULT_IP_PROVIDER = "ipinfo.io"


def lookup_ip_isp(provider: str | None = None) -> dict:
    result = {"ip": None, "isp": None, "asn": None, "org": None,
              "city": None, "country": None, "source": None, "provider": None}

    chosen = (provider or "").strip().lower()
    if chosen not in IP_PROVIDERS:
        chosen = DEFAULT_IP_PROVIDER
    order = [chosen] + [k for k in IP_PROVIDERS if k != chosen]

    for name in order:
        url = IP_PROVIDERS[name]
        try:
            data = json.loads(_http_get(url))
        except Exception:
            continue
        if name == "ipinfo.io":
            result.update(
                ip=data.get("ip"),
                org=data.get("org"),
                city=data.get("city"),
                country=data.get("country"),
                source=name,
            )
            org = data.get("org") or ""
            m = re.match(r"(AS\d+)\s+(.*)", org)
            if m:
                result["asn"] = m.group(1)
                result["isp"] = m.group(2).strip()
        elif name == "ifconfig.co":
            result.update(
                ip=data.get("ip"),
                isp=data.get("asn_org"),
                asn=(f"AS{data['asn']}" if data.get("asn") else None),
                city=data.get("city"),
                country=data.get("country"),
                source=name,
            )
        elif name == "ip-api.com":
            result.update(
                ip=data.get("query"),
                isp=data.get("isp") or data.get("org"),
                asn=data.get("as"),
                city=data.get("city"),
                country=data.get("country"),
                source=name,
            )
        if result.get("ip"):
            result["provider"] = name
            return result

    # 最後備援: 只取 IP
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            result["ip"] = _http_get(url).strip()
            result["source"] = url.split("/")[2]
            result["provider"] = "fallback"
            break
        except Exception:
            continue
    return result


# --------------------------------------------------------------------------- #
# Ping (純 Python ICMP 優先, 失敗改用系統 ping)
# --------------------------------------------------------------------------- #


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def _stats(rtts: list) -> dict:
    if not rtts:
        return {"min": None, "avg": None, "max": None, "mdev": None, "jitter": None}
    n = len(rtts)
    avg = sum(rtts) / n
    var = sum((x - avg) ** 2 for x in rtts) / n
    jitter = None
    if n > 1:
        jitter = sum(abs(rtts[i] - rtts[i - 1]) for i in range(1, n)) / (n - 1)
    return {
        "min": min(rtts),
        "avg": avg,
        "max": max(rtts),
        "mdev": var ** 0.5,
        "jitter": jitter,
    }


def icmp_ping(host: str, count: int, interval: float, timeout: float, size: int) -> dict:
    """純 Python ICMP echo。需要 root/系統管理員權限, 否則丟出例外。"""
    try:
        addr = socket.gethostbyname(host)
    except socket.gaierror as exc:
        raise RuntimeError(f"無法解析主機 {host}: {exc}")

    payload_size = max(size - 8, 8)
    ident = os.getpid() & 0xFFFF
    rtts: list = []
    sent = 0

    sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    try:
        sock.settimeout(timeout)
        for seq in range(1, count + 1):
            sent += 1
            payload = struct.pack("!d", time.time()) + (b"\x00" * (payload_size - 8))
            header = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
            chk = _checksum(header + payload)
            header = struct.pack("!BBHHH", 8, 0, chk, ident, seq)
            packet = header + payload
            t0 = time.time()
            try:
                sock.sendto(packet, (addr, 0))
            except OSError:
                continue
            deadline = t0 + timeout
            while time.time() < deadline:
                try:
                    data, _ = sock.recvfrom(65535)
                except socket.timeout:
                    break
                if len(data) < 28:
                    continue
                ihl = (data[0] & 0x0F) * 4
                icmp = data[ihl:]
                if len(icmp) < 8:
                    continue
                typ, _code, _chk, _rid, rseq = struct.unpack("!BBHHH", icmp[:8])
                if typ == 0 and rseq == seq:
                    rtts.append((time.time() - t0) * 1000.0)
                    break
            if seq < count:
                time.sleep(interval)
    finally:
        sock.close()

    recv = len(rtts)
    st = _stats(rtts)
    st.update({"target": host, "ip": addr, "sent": sent, "recv": recv,
               "loss": round((sent - recv) / sent * 100.0, 1) if sent else 0.0,
               "method": "icmp"})
    return st


def parse_ping(text: str, expected: int) -> dict:
    result = {"sent": expected, "recv": None, "loss": None,
              "min": None, "avg": None, "max": None, "mdev": None, "jitter": None}

    m = re.search(r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets\s+)?received", text)
    if m:
        result["sent"], result["recv"] = int(m.group(1)), int(m.group(2))
    if result["recv"] is None:
        m = re.search(r"(?:Sent|已傳送)\s*=\s*(\d+).*?(?:Received|已接收)\s*=\s*(\d+)", text, re.S)
        if m:
            result["sent"], result["recv"] = int(m.group(1)), int(m.group(2))

    m = re.search(r"(\d+(?:\.\d+)?)%\s*(?:packet\s+)?loss", text, re.I)
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)%[^\n]*?(?:遺失|丢失)", text)
    if not m:
        m = re.search(r"(?:遺失|丢失)[^\n]*?(\d+(?:\.\d+)?)%", text)
    if m:
        result["loss"] = float(m.group(1))

    m = re.search(
        r"(?:rtt|round-trip)\s+min/avg/max/(?:mdev|stddev)\s*=\s*"
        r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)",
        text, re.I,
    )
    if m:
        result["min"], result["avg"], result["max"], result["mdev"] = (float(x) for x in m.groups())
    else:
        m = re.search(r"(?:Minimum|最小值)\s*=\s*(\d+)ms.*?(?:Maximum|最大值)\s*=\s*(\d+)ms.*?(?:Average|平均)\s*=\s*(\d+)ms", text, re.S)
        if m:
            result["min"], result["max"], result["avg"] = (float(x) for x in m.groups())

    rtts = [float(x) for x in re.findall(r"time[=<]\s*([\d.]+)\s*ms", text)]
    if not rtts:
        rtts = [float(x) for x in re.findall(r"(?:時間)[=<]\s*(\d+(?:\.\d+)?)\s*ms", text)]
    if rtts:
        st = _stats(rtts)
        for key in ("min", "avg", "max", "mdev", "jitter"):
            if result[key] is None:
                result[key] = st[key]
        if result["recv"] is None:
            result["recv"] = len(rtts)

    if result["loss"] is None and result["recv"] is not None and result["sent"]:
        result["loss"] = round((result["sent"] - result["recv"]) / result["sent"] * 100.0, 1)
    result["method"] = "system"
    return result


def system_ping(host: str, count: int, interval: float, timeout: float, size: int) -> dict:
    if os.name == "nt":
        cmd = ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), "-l", str(size), host]
    elif sys.platform == "darwin":
        cmd = ["ping", "-c", str(count), "-i", str(interval),
               "-W", str(int(timeout * 1000)), "-s", str(size), host]
    else:
        cmd = ["ping", "-c", str(count), "-i", str(interval),
               "-W", str(max(1, int(round(timeout)))), "-s", str(size), host]
    proc = run_cmd(cmd, timeout=count * (interval + timeout) + 10)
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    res = parse_ping(text, count)
    res["target"] = host
    res["ip"] = None
    try:
        res["ip"] = socket.gethostbyname(host)
    except Exception:
        pass
    res["_raw"] = text
    return res


def ping_target(host: str, cfg: dict, count=None, interval=None,
                timeout=None, size=None, method=None) -> dict:
    pc = cfg.get("ping", {})
    count = count if count is not None else pc.get("count", 5)
    interval = interval if interval is not None else pc.get("interval", 1.0)
    timeout = timeout if timeout is not None else pc.get("timeout", 2.0)
    size = size if size is not None else pc.get("size", 56)
    method = method or pc.get("method", "auto")

    if method in ("auto", "icmp"):
        try:
            return icmp_ping(host, count, interval, timeout, size)
        except (PermissionError, OSError, RuntimeError) as exc:
            if method == "icmp":
                warn(t(f"ICMP ping 失敗 ({exc}), 改用系統 ping",
                       f"ICMP ping failed ({exc}), falling back to system ping"))
            return system_ping(host, count, interval, timeout, size)
    return system_ping(host, count, interval, timeout, size)


def get_default_gateway() -> str | None:
    if os.name == "nt":
        proc = run_cmd(["powershell", "-NoProfile", "-Command",
                        "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
                        "Select-Object -First 1).NextHop"], timeout=15)
        gw = (proc.stdout or "").strip()
        if gw and gw.count(".") == 3:
            return gw
        proc = run_cmd(["route", "print", "-4"], timeout=15)
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[2]):
                    return parts[2]
        return None

    if sys.platform == "darwin":
        proc = run_cmd(["route", "-n", "get", "default"], timeout=10)
        m = re.search(r"gateway:\s*([\d.]+)", proc.stdout or "")
        return m.group(1) if m else None

    proc = run_cmd(["ip", "route", "show", "default"], timeout=10)
    m = re.search(r"default via ([\d.]+)", proc.stdout or "")
    if m:
        return m.group(1)
    proc = run_cmd(["route", "-n"], timeout=10)
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "0.0.0.0":
            return parts[1]
    return None


def resolve_ping_targets(cfg: dict, extra: list, use_gateway: bool = True,
                         include_config: bool = True) -> list:
    targets: list = []
    pc = cfg.get("ping", {})
    if use_gateway and pc.get("auto_gateway", True):
        gw = get_default_gateway()
        if gw:
            targets.append(gw)
        else:
            warn(t("偵測不到 default gateway", "Default gateway not detected"))
    if include_config:
        for t in pc.get("targets", []):
            if t not in targets:
                targets.append(t)
    for t in extra:
        if t not in targets:
            targets.append(t)
    return targets


# --------------------------------------------------------------------------- #
# Cloudflare (純 Python, 不需外部工具)
# --------------------------------------------------------------------------- #


def cloudflare_download(cfg: dict, seconds: float | None = None) -> float | None:
    base = (get_dotted(cfg, "endpoints.cloudflare") or "https://speed.cloudflare.com").rstrip("/")
    seconds = seconds if seconds is not None else cfg.get("cloudflare", {}).get("seconds", 10)
    chunk = cfg.get("cloudflare", {}).get("chunk_bytes", 25000000)
    url = f"{base}/__down?bytes={chunk}"
    total = 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"netspeed/{VERSION}"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                total += len(resp.read())
        except Exception:
            break
    dt = time.time() - t0
    if dt <= 0 or total == 0:
        return None
    return total * 8 / 1e6 / dt


def cloudflare_upload(cfg: dict, seconds: float | None = None) -> float | None:
    base = (get_dotted(cfg, "endpoints.cloudflare") or "https://speed.cloudflare.com").rstrip("/")
    seconds = seconds if seconds is not None else cfg.get("cloudflare", {}).get("seconds", 10)
    chunk = cfg.get("cloudflare", {}).get("chunk_bytes", 25000000)
    url = f"{base}/__up"
    data = b"\x00" * chunk
    total = 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"User-Agent": f"netspeed/{VERSION}",
                                                  "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
            total += len(data)
        except Exception:
            break
    dt = time.time() - t0
    if dt <= 0 or total == 0:
        return None
    return total * 8 / 1e6 / dt


# --------------------------------------------------------------------------- #
# 外部測速來源
# --------------------------------------------------------------------------- #


def _parse_speed_numbers(text: str) -> dict:
    """從任意文字抓 download/upload 數值 (Mbps)。"""
    out = {"download": None, "upload": None}
    for label, key in (("download", "download"), ("upload", "upload")):
        m = re.search(label + r"[^0-9]{0,40}?([\d.]+)\s*(Gbit|Mbit|Kbit|Gbps|Mbps|Kbps)",
                      text, re.I)
        if m:
            val = float(m.group(1))
            unit = m.group(2).lower()
            if unit.startswith("g"):
                val *= 1000
            elif unit.startswith("k"):
                val /= 1000
            out[key] = val
    return out


def run_mlab(cfg: dict, extra: list) -> dict:
    tool = find_tool("ndt7-client")
    if not tool:
        return {"source": "mlab", "error": t("找不到 ndt7-client", "ndt7-client not found")}
    cmd = [tool]
    server = get_dotted(cfg, "endpoints.mlab")
    if server:
        cmd += ["-server", server]
    cmd += extra
    proc = run_cmd(cmd, timeout=120)
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    nums = _parse_speed_numbers(text)
    return {"source": "mlab", "download": nums["download"], "upload": nums["upload"],
            "latency": None, "raw": text}


def run_fast(cfg: dict, extra: list, upload: bool = False) -> dict:
    tool = find_tool("fast")
    if not tool:
        return {"source": "fast", "error": t("找不到 fast-cli (npm install -g fast-cli)", "fast-cli not found (npm install -g fast-cli)")}
    cmd = [tool, "--json"]
    if upload:
        cmd.append("--upload")
    cmd += extra
    proc = run_cmd(cmd, timeout=120)
    text = proc.stdout or ""
    dl = ul = None
    try:
        data = json.loads(text)
        dl = data.get("downloadSpeed")
        ul = data.get("uploadSpeed")
    except Exception:
        nums = _parse_speed_numbers(text)
        dl, ul = nums["download"], nums["upload"]
    return {"source": "fast", "download": dl, "upload": ul, "latency": None, "raw": text}


def run_ookla(cfg: dict, extra: list) -> dict:
    tool = find_tool(["speedtest", "ookla"])
    if not tool:
        return {"source": "ookla", "error": t("找不到 Ookla speedtest CLI", "Ookla speedtest CLI not found")}
    cmd = [tool, "--accept-license", "--accept-gdpr", "--format=json"]
    sid = get_dotted(cfg, "endpoints.ookla")
    if sid:
        cmd.append(f"--server-id={sid}")
    cmd += extra
    proc = run_cmd(cmd, timeout=180)
    text = proc.stdout or ""
    dl = ul = lat = None
    server = None
    try:
        data = json.loads(text)
        dl = data.get("download", {}).get("bandwidth")
        ul = data.get("upload", {}).get("bandwidth")
        lat = data.get("ping", {}).get("latency")
        server = (data.get("server") or {}).get("name")
        if dl is not None:
            dl = dl * 8 / 1e6
        if ul is not None:
            ul = ul * 8 / 1e6
    except Exception:
        nums = _parse_speed_numbers(text)
        dl, ul = nums["download"], nums["upload"]
    return {"source": "ookla", "download": dl, "upload": ul, "latency": lat,
            "server": server, "raw": text}


def run_libre(cfg: dict, extra: list) -> dict:
    tool = find_tool("librespeed-cli")
    if not tool:
        return {"source": "libre", "error": t("找不到 librespeed-cli", "librespeed-cli not found")}
    cmd = [tool, "--json"]
    server = get_dotted(cfg, "endpoints.libre")
    if server:
        cmd += ["--server", server]
    cmd += extra
    proc = run_cmd(cmd, timeout=180)
    text = proc.stdout or ""
    dl = ul = lat = None
    srv = None
    try:
        data = json.loads(text)
        if isinstance(data, list) and data:
            data = data[0]
        dl = data.get("download")
        ul = data.get("upload")
        lat = data.get("ping")
        srv = data.get("server", {}).get("name") if isinstance(data.get("server"), dict) else data.get("server")
    except Exception:
        nums = _parse_speed_numbers(text)
        dl, ul = nums["download"], nums["upload"]
    return {"source": "libre", "download": dl, "upload": ul, "latency": lat,
            "server": srv, "raw": text}


SOURCE_LABELS = {
    "mlab": "M-Lab NDT7",
    "fast": "fast.com (Netflix)",
    "cloudflare": "Cloudflare",
    "ookla": "Ookla speedtest.net",
    "libre": "LibreSpeed",
}


def run_source(name: str, cfg: dict, args=None) -> dict:
    name = name.lower()
    if name in ("libre", "librespeed"):
        name = "libre"
    if name == "mlab":
        return run_mlab(cfg, [])
    if name == "fast":
        return run_fast(cfg, [], upload=getattr(args, "upload", False))
    if name in ("cloudflare", "cf"):
        seconds = getattr(args, "seconds", None)
        dl = cloudflare_download(cfg, seconds)
        ul = cloudflare_upload(cfg, seconds)
        return {"source": "cloudflare", "download": dl, "upload": ul, "latency": None}
    if name == "ookla":
        return run_ookla(cfg, [])
    if name == "libre":
        return run_libre(cfg, [])
    return {"source": name, "error": t(f"未知來源 {name}", f"unknown source {name}")}


def print_speed_result(res: dict) -> None:
    src = res.get("source", "?")
    label = SOURCE_LABELS.get(src, src)
    if res.get("error"):
        error(f"{label}: {res['error']}")
        return
    dl = res.get("download")
    ul = res.get("upload")
    lat = res.get("latency")
    parts = [f"{label}:"]
    parts.append(t(f"下載 {dl:8.2f} Mbps", f"down {dl:8.2f} Mbps") if dl is not None
                 else t("下載   n/a", "down   n/a"))
    parts.append(t(f"上傳 {ul:8.2f} Mbps", f"up   {ul:8.2f} Mbps") if ul is not None
                 else t("上傳   n/a", "up     n/a"))
    if lat is not None:
        parts.append(t(f"延遲 {lat:.1f} ms", f"lat {lat:.1f} ms"))
    print("  " + "  ".join(parts))


# --------------------------------------------------------------------------- #
# 對測 (pair)
# --------------------------------------------------------------------------- #


def find_iperf(cfg: dict, override: str | None = None) -> str | None:
    custom = override or get_dotted(cfg, "iperf.path")
    return find_tool(["iperf3", "iperf"], custom)


def iperf_client(host: str, port: int, duration: int, streams: int, mode: str,
                 iperf_path: str, udp: bool = False, bandwidth: str | None = None) -> dict:
    result = {"download": None, "upload": None, "latency": None, "jitter": None,
              "lost_percent": None, "lost_packets": None, "raw": ""}
    runs = []
    if mode in ("both", "up"):
        runs.append(("up", []))
    if mode in ("both", "down"):
        runs.append(("down", ["-R"]))
    for label, flags in runs:
        cmd = [iperf_path, "-c", host, "-p", str(port), "-t", str(duration),
               "-P", str(streams), "-J"] + flags
        if udp:
            cmd += ["-u", "-b", str(bandwidth or "0")]
        proc = run_cmd(cmd, timeout=duration + 30)
        result["raw"] += (proc.stdout or "") + "\n"
        try:
            data = json.loads(proc.stdout)
            end = data.get("end", {})
            if udp:
                summ = end.get("sum", {})
                mbps = summ.get("bits_per_second", 0) / 1e6
                if label == "up":
                    result["upload"] = mbps
                else:
                    result["download"] = mbps
                result["jitter"] = summ.get("jitter_ms")
                result["lost_percent"] = summ.get("lost_percent")
                result["lost_packets"] = summ.get("lost_packets")
            elif label == "up":
                result["upload"] = end.get("sum_sent", {}).get("bits_per_second", 0) / 1e6
                result["latency"] = end.get("sum_sent", {}).get("jitter_ms")
            else:
                result["download"] = end.get("sum_received", {}).get("bits_per_second", 0) / 1e6
        except Exception:
            pass
    return result


def _recv_line(sock: socket.socket, buf: bytearray) -> str | None:
    while b"\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    line, _, rest = buf.partition(b"\n")
    buf[:] = rest
    return line.decode("utf-8", "replace")


def builtin_serve(host: str, port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(5)
    srv.settimeout(0.5)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        udp.bind((host, port))
        threading.Thread(target=_builtin_udp_server, args=(udp,), daemon=True).start()
    except OSError:
        udp = None
    print(col(t(f"[內建對測伺服器] 監聽 {host}:{port} (TCP/UDP)  (Ctrl+C 結束)",
                f"[Builtin pair server] listening on {host}:{port} (TCP/UDP)  (Ctrl+C to stop)"), "green"))
    print(t("等待對測端連線...", "Waiting for peer connection..."))
    try:
        while True:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                break
            threading.Thread(target=_builtin_handle, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        if udp is not None:
            try:
                udp.close()
            except OSError:
                pass
    print(t("\n伺服器結束。", "\nServer stopped."))


def _builtin_udp_server(sock: socket.socket) -> None:
    """內建 UDP 對測伺服器: 單一 socket 迴圈處理控制訊息與資料。"""
    active: dict = {}
    sock.settimeout(0.5)
    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            data = None
        except OSError:
            return
        now = time.time()
        for a in list(active):
            st = active[a]
            if now > st["end"]:
                mbps = st["total"] * 8 / 1e6 / st["dur"] if st["dur"] > 0 else 0
                try:
                    sock.sendto(f"RESULT {mbps:.3f}\n".encode(), a)
                except OSError:
                    pass
                print(t(f"   上傳 {mbps:8.2f} Mbps  (來自 {a[0]}:{a[1]})",
                        f"   up   {mbps:8.2f} Mbps  (from {a[0]}:{a[1]})"))
                del active[a]
        if data is None:
            continue
        text = data.decode("utf-8", "replace") if len(data) < 128 else ""
        if text.startswith(PROTO_TAG):
            parts = text.split()
            if len(parts) >= 3:
                try:
                    seconds = float(parts[2])
                except ValueError:
                    continue
                if parts[1] == "udp-down":
                    print(col(t(f"\n-> UDP 連線來自 {addr[0]}:{addr[1]}",
                                f"\n-> UDP connection from {addr[0]}:{addr[1]}"), "cyan"))
                    threading.Thread(target=_builtin_udp_stream_down,
                                     args=(sock, addr, seconds), daemon=True).start()
                elif parts[1] == "udp-up":
                    active[addr] = {"end": now + seconds, "total": 0, "dur": seconds}
            continue
        if addr in active:
            active[addr]["total"] += len(data)


def _builtin_udp_stream_down(sock: socket.socket, addr, seconds: float) -> None:
    payload = b"\x00" * 1400
    t0 = time.time()
    total = 0
    while time.time() - t0 < seconds:
        try:
            sock.sendto(payload, addr)
        except OSError:
            break
        total += len(payload)
    dt = time.time() - t0
    mbps = total * 8 / 1e6 / dt if dt > 0 else 0
    print(t(f"   下載 {mbps:8.2f} Mbps  (傳給 {addr[0]}:{addr[1]})",
            f"   down {mbps:8.2f} Mbps  (to {addr[0]}:{addr[1]})"))


def _builtin_handle(conn: socket.socket, addr) -> None:
    peer = f"{addr[0]}:{addr[1]}"
    print(col(t(f"\n-> 連線來自 {peer}  ({now_iso()})", f"\n-> connection from {peer}  ({now_iso()})"), "cyan"))
    conn.settimeout(30)
    buf = bytearray()
    line = _recv_line(conn, buf)
    if not line:
        conn.close()
        return
    parts = line.split()
    if len(parts) < 3 or parts[0] != PROTO_TAG:
        conn.close()
        return
    mode, seconds = parts[1], float(parts[2])
    chunk = b"\x00" * (1 << 20)
    try:
        if mode == "down":
            t0 = time.time()
            total = 0
            while time.time() - t0 < seconds:
                conn.sendall(chunk)
                total += len(chunk)
            dt = time.time() - t0
            mbps = total * 8 / 1e6 / dt if dt > 0 else 0
            print(t(f"   下載 {mbps:8.2f} Mbps  (傳給 {peer})",
                    f"   down {mbps:8.2f} Mbps  (to {peer})"))
        elif mode == "up":
            total = 0
            t0 = time.time()
            while True:
                try:
                    data = conn.recv(1 << 20)
                except socket.timeout:
                    break
                if not data:
                    break
                total += len(data)
                if time.time() - t0 > seconds + 5:
                    break
            dt = time.time() - t0
            mbps = total * 8 / 1e6 / dt if dt > 0 else 0
            print(t(f"   上傳 {mbps:8.2f} Mbps  (來自 {peer})",
                    f"   up   {mbps:8.2f} Mbps  (from {peer})"))
            try:
                conn.sendall(f"RESULT {total}\n".encode())
            except OSError:
                pass
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _builtin_measure_down(host: str, port: int, seconds: float) -> float | None:
    try:
        s = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        warn(t(f"無法連線 {host}:{port} ({exc})", f"Cannot connect {host}:{port} ({exc})"))
        return None
    s.sendall(f"{PROTO_TAG} down {seconds}\n".encode())
    total = 0
    t0 = time.time()
    try:
        while time.time() - t0 < seconds + 5:
            data = s.recv(1 << 20)
            if not data:
                break
            total += len(data)
            if time.time() - t0 >= seconds:
                break
    except OSError:
        pass
    finally:
        s.close()
    dt = time.time() - t0
    return total * 8 / 1e6 / dt if dt > 0 and total else None


def _builtin_measure_up(host: str, port: int, seconds: float) -> float | None:
    try:
        s = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        warn(t(f"無法連線 {host}:{port} ({exc})", f"Cannot connect {host}:{port} ({exc})"))
        return None
    s.sendall(f"{PROTO_TAG} up {seconds}\n".encode())
    chunk = b"\x00" * (1 << 20)
    total = 0
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            s.sendall(chunk)
            total += len(chunk)
        s.shutdown(socket.SHUT_WR)
        s.settimeout(10)
        resp = s.recv(256).decode("utf-8", "replace")
        m = re.search(r"RESULT\s+(\d+)", resp)
        if m:
            total = int(m.group(1))
    except OSError:
        pass
    finally:
        s.close()
    dt = time.time() - t0
    return total * 8 / 1e6 / dt if dt > 0 and total else None


def _builtin_udp_down(host: str, port: int, seconds: float) -> float | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(f"{PROTO_TAG} udp-down {seconds}\n".encode(), (host, port))
    except OSError as exc:
        warn(t(f"無法連線 {host}:{port} ({exc})", f"Cannot connect {host}:{port} ({exc})"))
        s.close()
        return None
    s.settimeout(0.5)
    first = last = None
    total = 0
    deadline = time.time() + seconds + 3
    while time.time() < deadline:
        try:
            data, _ = s.recvfrom(65535)
        except socket.timeout:
            if first is not None and last is not None and time.time() - last > 1.0:
                break
            continue
        except OSError:
            break
        now = time.time()
        if first is None:
            first = now
        last = now
        total += len(data)
    s.close()
    dt = (last - first) if (first is not None and last is not None and last > first) else 0
    return total * 8 / 1e6 / dt if dt > 0 and total else None


def _builtin_udp_up(host: str, port: int, seconds: float) -> float | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(f"{PROTO_TAG} udp-up {seconds}\n".encode(), (host, port))
    except OSError as exc:
        warn(t(f"無法連線 {host}:{port} ({exc})", f"Cannot connect {host}:{port} ({exc})"))
        s.close()
        return None
    payload = b"\x00" * 1400
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            s.sendto(payload, (host, port))
        except OSError:
            break
    s.settimeout(5)
    mbps = None
    try:
        resp, _ = s.recvfrom(256)
        m = re.search(r"RESULT\s+([\d.]+)", resp.decode("utf-8", "replace"))
        if m:
            mbps = float(m.group(1))
    except (socket.timeout, OSError):
        pass
    s.close()
    return mbps


def builtin_client(host: str, port: int, duration: int, mode: str, udp: bool = False) -> dict:
    result = {"download": None, "upload": None, "latency": None}
    if udp:
        if mode in ("both", "down"):
            result["download"] = _builtin_udp_down(host, port, duration)
        if mode in ("both", "up"):
            result["upload"] = _builtin_udp_up(host, port, duration)
        return result

    results = {}
    threads = []

    def work(kind):
        if kind == "down":
            results["down"] = _builtin_measure_down(host, port, duration)
        else:
            results["up"] = _builtin_measure_up(host, port, duration)

    jobs = []
    if mode in ("both", "down"):
        jobs.append("down")
    if mode in ("both", "up"):
        jobs.append("up")
    for job in jobs:
        th = threading.Thread(target=work, args=(job,))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()

    result["download"] = results.get("down")
    result["upload"] = results.get("up")
    return result


def run_pair_client(host: str, cfg: dict, args) -> dict:
    port = args.port or get_dotted(cfg, "iperf.port") or 5201
    duration = args.duration or get_dotted(cfg, "iperf.duration") or 8
    streams = args.streams or get_dotted(cfg, "iperf.streams") or 1
    mode = getattr(args, "mode", "both")
    udp = getattr(args, "udp", False)
    bandwidth = getattr(args, "bandwidth", None)

    iperf_path = find_iperf(cfg, getattr(args, "iperf_path", None))
    method = args.method
    if method == "auto":
        method = "iperf3" if iperf_path else "builtin"

    if method == "iperf3" and not iperf_path:
        warn(t("找不到 iperf3, 改用內建對測", "iperf3 not found, using builtin pair"))
        method = "builtin"

    proto = "UDP" if udp else "TCP"
    print(col(t(f"[對測] 目標 {host}:{port}  方式={method}  協定={proto}  模式={mode}  秒數={duration}",
                f"[Pair] target {host}:{port}  method={method}  proto={proto}  mode={mode}  seconds={duration}"),
              "cyan"))
    t0 = time.time()
    if method == "iperf3":
        res = iperf_client(host, port, duration, streams, mode, iperf_path,
                           udp=udp, bandwidth=bandwidth)
    else:
        res = builtin_client(host, port, duration, mode, udp=udp)
    res["elapsed"] = round(time.time() - t0, 1)

    res.update({"kind": "pair", "time": now_iso(), "role": "client",
                "peer_ip": host, "peer_port": port, "method": method, "mode": mode,
                "proto": proto})
    return res


# --------------------------------------------------------------------------- #
# 報告
# --------------------------------------------------------------------------- #


def print_ip_section(ipinfo: dict) -> None:
    section(t("外網 IP / 線路", "Public IP / Line"))
    print(f"  {t('外網 IP', 'Public IP')} : {col(ipinfo.get('ip') or 'n/a', 'bold')}")
    isp = ipinfo.get("isp") or ipinfo.get("org") or "n/a"
    asn = ipinfo.get("asn") or ""
    print(f"  {t('電信商', 'ISP')}  : {isp} {('(' + asn + ')') if asn else ''}".rstrip())
    loc = ", ".join(x for x in (ipinfo.get("city"), ipinfo.get("country")) if x)
    if loc:
        print(f"  {t('地區', 'Location')}  : {loc}")
    print(f"  {t('來源', 'Source')}  : {ipinfo.get('source') or 'n/a'}"
          + (f"  ({t('提供者', 'provider')}: {ipinfo.get('provider')})" if ipinfo.get("provider") else ""))


def print_ping_table(results: list) -> None:
    header = (f"  {t('目標', 'Target'):<20} {t('傳送', 'Sent'):>4} {t('接收', 'Recv'):>4} "
              f"{t('遺失', 'Loss'):>6} {'min':>7} {'avg':>7} {'max':>7} {'mdev':>7} "
              f"{'jitter':>7}  {t('方式', 'Method')}")
    print(col(header, "bold"))
    print("  " + "-" * 96)

    def f(v):
        return f"{v:7.2f}" if isinstance(v, (int, float)) else f"{'n/a':>7}"

    for r in results:
        if r.get("error"):
            print(f"  {str(r.get('target')):<20} {col(t('失敗: ', 'failed: ') + str(r['error']), 'red')}")
            continue
        loss = r.get("loss")
        loss_s = f"{loss:5.1f}%" if isinstance(loss, (int, float)) else "  n/a"
        print(f"  {str(r.get('target')):<20} {str(r.get('sent') or 0):>4} "
              f"{str(r.get('recv') or 0):>4} {loss_s:>6} {f(r.get('min'))} {f(r.get('avg'))} "
              f"{f(r.get('max'))} {f(r.get('mdev'))} {f(r.get('jitter'))}  {r.get('method','')}")


def print_speed_table(results: list) -> None:
    section(t("測速結果 (Mbps)", "Speed results (Mbps)"))
    header = (f"  {t('來源', 'Source'):<22} {t('下載', 'Down'):>10} {t('上傳', 'Up'):>10} "
              f"{t('延遲(ms)', 'Latency(ms)'):>10}  {t('節點', 'Server')}")
    print(col(header, "bold"))
    print("  " + "-" * 80)
    dls = []
    uls = []
    for r in results:
        src = SOURCE_LABELS.get(r.get("source"), r.get("source"))
        if r.get("error"):
            print(f"  {str(src):<22} {col(str(r['error']), 'yellow')}")
            continue
        dl = r.get("download")
        ul = r.get("upload")
        lat = r.get("latency")
        if dl is not None:
            dls.append(dl)
        if ul is not None:
            uls.append(ul)
        dl_s = f"{dl:10.2f}" if dl is not None else f"{'n/a':>10}"
        ul_s = f"{ul:10.2f}" if ul is not None else f"{'n/a':>10}"
        lat_s = f"{lat:10.1f}" if lat is not None else f"{'n/a':>10}"
        print(f"  {str(src):<22} {dl_s} {ul_s} {lat_s}  {r.get('server') or ''}")
    print("  " + "-" * 80)
    avg_dl = f"{sum(dls)/len(dls):10.2f}" if dls else f"{'n/a':>10}"
    avg_ul = f"{sum(uls)/len(uls):10.2f}" if uls else f"{'n/a':>10}"
    print(col(f"  {t('平均', 'avg'):<22} {avg_dl} {avg_ul}", "bold"))


def print_pair_result(res: dict) -> None:
    section(t("對測結果", "Pair result"))
    print(t(f"  角色    : {res.get('role')}", f"  Role    : {res.get('role')}"))
    print(t(f"  對端    : {res.get('peer_ip')}:{res.get('peer_port')}",
            f"  Peer    : {res.get('peer_ip')}:{res.get('peer_port')}"))
    print(t(f"  方式    : {res.get('method')}", f"  Method  : {res.get('method')}"))
    if res.get("proto"):
        print(t(f"  協定    : {res.get('proto')}", f"  Proto   : {res.get('proto')}"))
    print(t(f"  時間    : {res.get('time')}", f"  Time    : {res.get('time')}"))
    if res.get("download") is not None:
        print(t("  下載    : ", "  Download: ") + col(f"{res['download']:.2f} Mbps", "green"))
    if res.get("upload") is not None:
        print(t("  上傳    : ", "  Upload  : ") + col(f"{res['upload']:.2f} Mbps", "green"))
    if res.get("jitter") is not None:
        print(t(f"  Jitter  : {res['jitter']:.3f} ms", f"  Jitter  : {res['jitter']:.3f} ms"))
    if res.get("lost_percent") is not None:
        print(t(f"  遺失    : {res['lost_percent']:.2f}%", f"  Loss    : {res['lost_percent']:.2f}%"))
    if res.get("elapsed") is not None:
        print(t(f"  耗時    : {res['elapsed']}s", f"  Elapsed : {res['elapsed']}s"))


# --------------------------------------------------------------------------- #
# 指令實作
# --------------------------------------------------------------------------- #


def cmd_ip(cfg: dict, args) -> dict:
    provider = getattr(args, "provider", None) or get_dotted(cfg, "ip.provider") or DEFAULT_IP_PROVIDER
    cache_key = f"ip:{provider}"
    if not getattr(args, "refresh", False) and cache_key in _SESSION_CACHE:
        ipinfo = _SESSION_CACHE[cache_key]
    else:
        ipinfo = lookup_ip_isp(provider)
        _SESSION_CACHE[cache_key] = ipinfo
    if getattr(args, "json", False):
        print(json.dumps(ipinfo, ensure_ascii=False, indent=2))
    else:
        print_ip_section(ipinfo)
    if not getattr(args, "no_save", False):
        rec = {"kind": "ip"}
        rec.update({k: ipinfo.get(k) for k in
                    ("ip", "isp", "asn", "org", "city", "country", "source", "provider")})
        append_history(rec, cfg)
    return ipinfo


def _ping_effective(cfg: dict, args) -> dict:
    pc = cfg.get("ping", {})
    return {
        "count": getattr(args, "count", None) or pc.get("count", 5),
        "interval": getattr(args, "interval", None) or pc.get("interval", 1.0),
        "timeout": getattr(args, "timeout", None) or pc.get("timeout", 2.0),
        "size": getattr(args, "size", None) or pc.get("size", 56),
        "method": getattr(args, "method", None) or pc.get("method", "auto"),
    }


def _ping_gateway_text(cfg: dict, args=None) -> str:
    if get_dotted(cfg, "ping.auto_gateway") is False or getattr(args, "no_gateway", False):
        return t("關閉", "off")
    gw = get_default_gateway()
    return gw or t("無", "none")


def _ping_header(cfg: dict, eff: dict, args=None) -> str:
    return (t("Ping 測試", "Ping test")
            + f" count:{eff['count']} interval:{eff['interval']} "
              f"timeout:{eff['timeout']} size:{eff['size']} "
              f"gateway:{_ping_gateway_text(cfg, args)}")


def _do_ping(cfg: dict, args, targets: list, save=None) -> list:
    use_json = getattr(args, "json", False)
    no_save = (not save) if save is not None else getattr(args, "no_save", False)
    eff = _ping_effective(cfg, args)
    if not targets:
        error(t("沒有可 ping 的目標", "No ping targets available"))
        return []
    if not use_json:
        section(_ping_header(cfg, eff, args))
    results = []
    total = len(targets)
    for i, tg in enumerate(targets, 1):
        if not use_json:
            progress("  " + t(f"({i}/{total}) 正在 ping {col(tg, 'bold')} ...",
                              f"({i}/{total}) pinging {col(tg, 'bold')} ..."))
        r = ping_target(tg, cfg, eff["count"], eff["interval"], eff["timeout"],
                        eff["size"], eff["method"])
        results.append(r)
    if use_json:
        progress_end()
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        progress_end()
        print_ping_table(results)
    if not no_save and results:
        rec = {"kind": "ping", "params": dict(eff),
               "targets": [{k: r.get(k) for k in
                            ("target", "ip", "sent", "recv", "loss", "min", "avg",
                             "max", "mdev", "jitter", "method", "error")} for r in results]}
        append_history(rec, cfg)
    return results


def _ping_pick_target(targets: list):
    if not targets:
        warn(t("沒有目標", "No targets"))
        return None
    print("  " + t("選擇目標:", "Select target:"))
    for i, tg in enumerate(targets, 1):
        print(f"    [{i}] {tg}")
    print(tcol("    " + t("[Enter/q] 返回", "[Enter/q] back"), "dim"))
    try:
        choice = input(col(t("  請輸入: ", "  Enter: "), "bold")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(targets):
        return targets[int(choice) - 1]
    return None


def _ping_params_menu(cfg: dict) -> None:
    params = [("count", t("次數", "count")), ("interval", t("間隔(秒)", "interval(s)")),
              ("timeout", t("逾時(秒)", "timeout(s)")), ("size", t("封包大小", "packet size")),
              ("method", "auto|icmp|system")]
    while True:
        pc = cfg.get("ping", {})
        print()
        print(tcol("  " + t("Ping 參數:", "Ping parameters:"), "heading"))
        for i, (k, desc) in enumerate(params, 1):
            print(f"    [{i}] {k} = {pc.get(k)}   ({desc})")
        print(tcol("    " + t("[Enter/q] 返回", "[Enter/q] back"), "dim"))
        try:
            choice = input(col(t("  請輸入: ", "  Enter: "), "bold")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in ("", "q", "quit", "exit"):
            return
        if choice.isdigit() and 1 <= int(choice) <= len(params):
            key = params[int(choice) - 1][0]
            try:
                val = input(t(f"  {key} 新值: ", f"  {key} new value: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if val:
                set_dotted(cfg, f"ping.{key}", coerce(val))
                save_config(cfg)
                ok(f"ping.{key} = {get_dotted(cfg, f'ping.{key}')}")
        else:
            warn(t("無效的選擇", "Invalid choice"))


def _ping_targets_menu(cfg: dict) -> None:
    while True:
        targets = cfg.setdefault("ping", {}).setdefault("targets", [])
        print()
        print(tcol("  " + t("Ping 目標清單:", "Ping target list:"), "heading"))
        for i, tg in enumerate(targets, 1):
            print(f"    [{i}] {tg}")
        print(tcol("    " + t("[a] 新增   [d] 刪除   [Enter/q] 返回",
                              "[a] add   [d] delete   [Enter/q] back"), "dim"))
        try:
            choice = input(col(t("  請輸入: ", "  Enter: "), "bold")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in ("", "q", "quit", "exit"):
            return
        if choice == "a":
            try:
                tg = input(t("  新增目標 (IP/主機): ", "  New target (IP/host): ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if tg:
                if tg not in targets:
                    targets.append(tg)
                save_config(cfg)
                ok(t("已新增 {t}", "Added {t}").format(t=tg))
        elif choice == "d":
            try:
                n = input(t("  刪除編號: ", "  Delete number: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if n.isdigit() and 1 <= int(n) <= len(targets):
                removed = targets.pop(int(n) - 1)
                save_config(cfg)
                ok(t("已刪除 {t}", "Removed {t}").format(t=removed))
        else:
            warn(t("無效的選擇", "Invalid choice"))


def _ping_settings_menu(cfg: dict) -> None:
    while True:
        pc = cfg.get("ping", {})
        print()
        print(tcol("  " + t("修改 Ping 設定:", "Edit Ping settings:"), "heading"))
        print("    [1] " + t("預設閘道自動加入: ", "Auto-add default gateway: ")
              + ("on" if pc.get('auto_gateway', True) else "off"))
        print("    [2] " + t(f"目標清單 ({len(pc.get('targets', []))} 個)",
                              f"Target list ({len(pc.get('targets', []))})"))
        print("    [3] " + t("參數 (count / interval / timeout / size / method)",
                             "Parameters (count / interval / timeout / size / method)"))
        print(tcol("    " + t("[Enter/q] 返回", "[Enter/q] back"), "dim"))
        try:
            choice = input(col(t("  請輸入: ", "  Enter: "), "bold")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in ("", "q", "quit", "exit"):
            return
        if choice == "1":
            set_dotted(cfg, "ping.auto_gateway", not pc.get("auto_gateway", True))
            save_config(cfg)
            ok(f"ping.auto_gateway = {get_dotted(cfg, 'ping.auto_gateway')}")
        elif choice == "2":
            _ping_targets_menu(cfg)
        elif choice == "3":
            _ping_params_menu(cfg)
        else:
            warn(t("無效的選擇", "Invalid choice"))


def _ping_menu(cfg: dict, args) -> list:
    while True:
        eff = _ping_effective(cfg, args)
        print()
        section(_ping_header(cfg, eff, args))
        print("    [1] " + t("全部目標", "All targets"))
        print("    [2] " + t("選擇單一目標", "Pick a single target"))
        print("    [3] " + t("修改設定", "Edit settings"))
        print(tcol("    " + t("[Enter/q] 取消", "[Enter/q] cancel"), "dim"))
        try:
            choice = input(col(t("  請輸入: ", "  Enter: "), "bold")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        if choice in ("", "q", "quit", "exit"):
            return []
        if choice == "1":
            targets = resolve_ping_targets(cfg, [], use_gateway=not getattr(args, "no_gateway", False))
            return _do_ping(cfg, args, targets)
        if choice == "2":
            targets = resolve_ping_targets(cfg, [], use_gateway=not getattr(args, "no_gateway", False))
            picked = _ping_pick_target(targets)
            if picked:
                return _do_ping(cfg, args, [picked])
        elif choice == "3":
            _ping_settings_menu(cfg)
        else:
            warn(t("無效的選擇", "Invalid choice"))


def cmd_ping(cfg: dict, args) -> list:
    extra = getattr(args, "targets", None) or []
    if extra or getattr(args, "only", False) or getattr(args, "json", False) \
            or not sys.stdin.isatty():
        targets = resolve_ping_targets(cfg, extra,
                                       use_gateway=not getattr(args, "no_gateway", False),
                                       include_config=not getattr(args, "only", False))
        return _do_ping(cfg, args, targets)
    return _ping_menu(cfg, args)


def _speed_result_record(res: dict) -> dict:
    return {k: res.get(k) for k in
            ("source", "download", "upload", "latency", "server", "error")}


def _speed_record(results: list) -> dict:
    return {"kind": "speed", "results": [_speed_result_record(r) for r in results]}


def _ip_record(ipinfo: dict) -> dict:
    rec = {"kind": "ip"}
    rec.update({k: ipinfo.get(k) for k in
                ("ip", "isp", "asn", "org", "city", "country", "source", "provider")})
    return rec


def _resolve_sources(cfg: dict, args) -> list:
    if getattr(args, "sources", None):
        return [s.strip() for s in args.sources.split(",") if s.strip()]
    return cfg.get("sources", DEFAULT_CONFIG["sources"])


def cmd_source(name: str, cfg: dict, args) -> dict:
    label = SOURCE_LABELS.get(name, name)
    if not getattr(args, "json", False):
        section(t("測速 - ", "Speed - ") + label)
        progress("  " + t(f"(1/1) 測速中 {label} ...", f"(1/1) testing {label} ..."))
    res = run_source(name, cfg, args)
    if getattr(args, "json", False):
        progress_end()
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        progress_end()
        print_speed_result(res)
    if not getattr(args, "no_save", False) and not res.get("error"):
        append_history(_speed_record([res]), cfg)
    return res


def cmd_speed_all(cfg: dict, args) -> list:
    """speed all — 只跑測速來源, 不含 ip / ping。"""
    sources = _resolve_sources(cfg, args)
    use_json = getattr(args, "json", False)
    results = []
    total = len(sources)
    if not use_json:
        section(t("測速中", "Speed test"))
    for i, name in enumerate(sources, 1):
        if not use_json:
            progress("  " + t(f"({i}/{total}) 測速中 {SOURCE_LABELS.get(name, name)} ...",
                              f"({i}/{total}) testing {SOURCE_LABELS.get(name, name)} ..."))
        try:
            res = run_source(name, cfg, args)
        except Exception as exc:  # noqa: BLE001
            res = {"source": name, "error": str(exc)}
        results.append(res)
    if use_json:
        progress_end()
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        progress_end()
        print_speed_table(results)
        section(t("測速完成", "Speed test complete"))
        print(f"  {t('時間', 'Time')}: {now_iso()}")
    if not getattr(args, "no_save", False) and any(not r.get("error") for r in results):
        append_history(_speed_record(results), cfg)
    return results


def cmd_all(cfg: dict, args) -> dict:
    sources = _resolve_sources(cfg, args)
    use_json = getattr(args, "json", False)
    collected = {"ip": None, "ping": [], "speed": []}

    if not getattr(args, "no_ip", False):
        ipinfo = lookup_ip_isp(get_dotted(cfg, "ip.provider"))
        collected["ip"] = ipinfo
        if not use_json:
            print_ip_section(ipinfo)
    if not getattr(args, "no_ping", False):
        collected["ping"] = _do_ping(cfg, args, resolve_ping_targets(cfg, []), save=False)

    total = len(sources)
    if not use_json:
        section(t("測速中", "Speed test"))
    for i, name in enumerate(sources, 1):
        if not use_json:
            progress("  " + t(f"({i}/{total}) 測速中 {SOURCE_LABELS.get(name, name)} ...",
                              f"({i}/{total}) testing {SOURCE_LABELS.get(name, name)} ..."))
        try:
            res = run_source(name, cfg, args)
        except Exception as exc:  # noqa: BLE001
            res = {"source": name, "error": str(exc)}
        collected["speed"].append(res)

    if use_json:
        progress_end()
        print(json.dumps(collected, ensure_ascii=False, indent=2))
    else:
        progress_end()
        print_speed_table(collected["speed"])
        section(t("統整完成", "Summary complete"))
        print(f"  {t('時間', 'Time')}: {now_iso()}")

    if not getattr(args, "no_save", False):
        rec = {"kind": "all"}
        if collected["ip"] is not None:
            rec["ip"] = {k: collected["ip"].get(k) for k in
                         ("ip", "isp", "asn", "org", "city", "country", "source", "provider")}
        if collected["ping"]:
            rec["ping"] = {
                "params": _ping_effective(cfg, args),
                "targets": [{k: r.get(k) for k in
                             ("target", "ip", "sent", "recv", "loss", "min", "avg",
                              "max", "mdev", "jitter", "method", "error")}
                            for r in collected["ping"]],
            }
        rec["speed"] = [_speed_result_record(r) for r in collected["speed"]]
        append_history(rec, cfg)
    return collected


SPEED_TOOLS = [
    {"label": "M-Lab", "names": ["ndt7-client"], "bin": "ndt7-client",
     "kind": "github", "repo": "m-lab/ndt7-client-go",
     "url": "https://github.com/m-lab/ndt7-client-go/releases"},
    {"label": "fast.com", "names": ["fast"], "bin": "fast",
     "kind": "npm", "pkg": "fast-cli", "url": "npm install -g fast-cli"},
    {"label": "Ookla", "names": ["speedtest", "ookla"], "bin": "speedtest",
     "kind": "manual", "url": "https://www.speedtest.net/apps/cli"},
    {"label": "LibreSpeed", "names": ["librespeed-cli"], "bin": "librespeed-cli",
     "kind": "github", "repo": "librespeed/speedtest-cli",
     "url": "https://github.com/librespeed/speedtest-cli/releases"},
]


def _machine_arch() -> str:
    try:
        import platform
        m = platform.machine().lower()
    except Exception:
        m = ""
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("i386", "i686", "x86"):
        return "386"
    return "amd64"


def _platform_keywords() -> tuple:
    if os.name == "nt":
        return ("windows", _machine_arch())
    if sys.platform == "darwin":
        return ("darwin", _machine_arch())
    return ("linux", _machine_arch())


def _download_file(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"netspeed/{VERSION}"})
    dbg("http", f"GET {url}")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    dbg("http", f"GET {url} -> {len(data)} bytes  {time.time() - t0:.2f}s")
    return data


def _github_asset(repo: str, keywords: tuple) -> dict | None:
    rel = _github_latest(repo)
    assets = rel.get("assets") or []
    for a in assets:
        n = (a.get("name") or "").lower()
        if all(k in n for k in keywords):
            return a
    for a in assets:
        n = (a.get("name") or "").lower()
        if keywords[0] in n:
            return a
    return None


def _extract_archive(data: bytes, dest_dir: str, bin_name: str) -> str | None:
    import io
    import tarfile
    import zipfile
    low = bin_name.lower()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        for m in zf.namelist():
            base = os.path.basename(m)
            if base.lower().startswith(low):
                target = os.path.join(dest_dir, base)
                with zf.open(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                if os.name != "nt":
                    os.chmod(target, 0o755)
                return target
    except Exception:
        pass
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data))
        for m in tf.getmembers():
            base = os.path.basename(m.name)
            if m.isfile() and base.lower().startswith(low):
                fh = tf.extractfile(m)
                if fh is None:
                    continue
                target = os.path.join(dest_dir, base)
                with open(target, "wb") as out:
                    shutil.copyfileobj(fh, out)
                if os.name != "nt":
                    os.chmod(target, 0o755)
                return target
    except Exception:
        pass
    target = os.path.join(dest_dir, bin_name + (".exe" if os.name == "nt" else ""))
    with open(target, "wb") as out:
        out.write(data)
    if os.name != "nt":
        os.chmod(target, 0o755)
    return target


def _try_download_tool(tool: dict) -> tuple:
    dest = install_dir()
    ensure_dir(dest)
    kind = tool.get("kind")
    if kind == "npm":
        npm = shutil.which("npm")
        if not npm:
            return False, t("找不到 npm (需先安裝 Node.js)", "npm not found (install Node.js first)")
        proc = run_cmd([npm, "install", "-g", tool["pkg"]], timeout=300)
        if proc.returncode != 0:
            return False, ((proc.stderr or proc.stdout or "").strip()[:200] or "npm failed")
        global _NPM_BIN_CACHE
        _NPM_BIN_CACHE = None  # 重新偵測 (npm 全域目錄可能剛被建立)
        found = find_tool(tool["names"])
        if found:
            return True, found
        npm_bin = _npm_global_bin()
        return False, t(
            f"npm 回報成功但找不到 {tool['bin']} 執行檔; npm 全域目錄: {npm_bin or '未知'}",
            f"npm reported success but {tool['bin']} not found; npm global dir: {npm_bin or 'unknown'}")
    if kind == "github":
        try:
            asset = _github_asset(tool["repo"], _platform_keywords())
        except Exception as exc:
            return False, str(exc)
        if not asset:
            return False, t("找不到對應本平台的檔案", "no matching asset for this platform")
        try:
            data = _download_file(asset.get("browser_download_url") or "")
            path = _extract_archive(data, dest, tool["bin"])
        except Exception as exc:
            return False, str(exc)
        return (bool(path), path or "")
    return False, tool.get("url", "")


def _print_tool_download_help(missing: list) -> None:
    section(t("如何下載測速工具", "How to download speed tools"))
    for tool in missing:
        print("  " + col(tool["label"], "bold"))
        kind = tool.get("kind")
        if kind == "npm":
            print("    " + t("安裝 Node.js 後執行: npm install -g fast-cli",
                             "Install Node.js, then run: npm install -g fast-cli"))
        elif kind == "github":
            print("    " + t("下載對應本平台的檔案, 放到 PATH 或 netspeed 同層目錄:",
                             "Download the file for your platform to PATH or next to netspeed:"))
            print("    " + tool["url"])
        else:
            print("    " + t("下載並放到 PATH 或 netspeed 同層目錄:",
                             "Download to PATH or next to netspeed:"))
            print("    " + tool["url"])
    print()
    print(tcol("  " + t("放好後可用 speed check --refresh 重新檢查。",
                        "After placing them, run speed check --refresh."), "dim"))


def cmd_speed_check(cfg: dict, args) -> None:
    use_json = getattr(args, "json", False)
    entries = [(tool, find_tool(tool["names"])) for tool in SPEED_TOOLS]
    missing = [tool for tool, found in entries if not found]
    if use_json:
        print(json.dumps([{"tool": tool["label"], "found": bool(f), "path": f, "url": tool["url"]}
                          for tool, f in entries], ensure_ascii=False, indent=2))
        return

    section(t("測速工具檢查", "Speed tool check"))
    for tool, found in entries:
        mark = tcol("[v]", "ok") if found else tcol("[x]", "error")
        print(f"  {mark} {tool['label']:<14} {found or t('找不到', 'not found')}")
    print()
    if not missing:
        ok(t("所有測速工具都已就緒。", "All speed tools are available."))
        return
    print(t(f"缺少 {len(missing)} 個測速工具。", f"{len(missing)} speed tool(s) missing."))

    assume = getattr(args, "yes", False)
    if not sys.stdin.isatty() and not assume:
        for tool in missing:
            print(f"    {tool['label']}: {tool['url']}")
        return
    if assume:
        ans = "y"
    else:
        while True:
            try:
                ans = input(col(t("是否協助自動下載缺少的工具? [y=下載 / h=說明 / 任一鍵=返回]: ",
                                  "Download missing tools now? [y=download / h=help / any key=back]: "),
                                "bold")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if ans == "h":
                _print_tool_download_help(missing)
                continue
            break
    if ans != "y":
        info(t("已返回。", "Returned."))
        return
    for tool in missing:
        print("  " + t(f"嘗試下載 {tool['label']} ...", f"Downloading {tool['label']} ..."))
        good, detail = _try_download_tool(tool)
        if good:
            ok(t(f"{tool['label']} 已下載: {detail}", f"{tool['label']} downloaded: {detail}"))
        else:
            warn(t(f"{tool['label']} 下載失敗 ({detail}); 請手動安裝: {tool['url']}",
                   f"{tool['label']} download failed ({detail}); install manually: {tool['url']}"))
    cache_clear()


def cmd_pair_serve(cfg: dict, args) -> None:
    port = args.port or get_dotted(cfg, "iperf.port") or 5201
    host = args.host
    iperf_path = find_iperf(cfg, getattr(args, "iperf_path", None))
    method = args.method
    if method == "auto":
        method = "iperf3" if iperf_path else "builtin"
    if method == "iperf3" and not iperf_path:
        warn(t("找不到 iperf3, 改用內建對測伺服器", "iperf3 not found, using builtin pair server"))
        method = "builtin"

    if method == "iperf3":
        print(col(t(f"[iperf3 對測伺服器] {host}:{port}  (Ctrl+C 結束)",
                    f"[iperf3 pair server] {host}:{port}  (Ctrl+C to stop)"), "green"))
        proc = subprocess.Popen([iperf_path, "-s", "-p", str(port)])
        try:
            proc.wait()
        except KeyboardInterrupt:
            print(t("\n正在停止 iperf3 ...", "\nStopping iperf3 ..."))
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            print(t("伺服器結束。", "Server stopped."))
    else:
        builtin_serve(host, port)


def cmd_pair_client(cfg: dict, args) -> dict:
    res = run_pair_client(args.host, cfg, args)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_pair_result(res)
    if not args.no_save:
        rec = {"kind": "pair"}
        rec.update({k: res.get(k) for k in
                    ("role", "peer_ip", "peer_port", "method", "mode", "proto",
                     "download", "upload", "latency", "jitter", "lost_percent", "elapsed")})
        append_history(rec, cfg)
    return res


def _rec_ping_targets(rec: dict) -> list:
    if rec.get("targets"):
        return rec["targets"]
    if rec.get("target") is not None:
        return [rec]
    return []


def _rec_speed_results(rec: dict) -> list:
    if rec.get("results"):
        return rec["results"]
    if rec.get("source") is not None:
        return [rec]
    return []


def _avg(vals: list):
    nums = [v for v in vals if isinstance(v, (int, float))]
    return (sum(nums) / len(nums)) if nums else None


def _record_desc(rec: dict) -> str:
    kind = rec.get("kind")
    if kind == "speed":
        results = _rec_speed_results(rec)
        dl = _avg([r.get("download") for r in results])
        ul = _avg([r.get("upload") for r in results])
        dl_s = f"{dl:.1f}" if dl is not None else "n/a"
        ul_s = f"{ul:.1f}" if ul is not None else "n/a"
        names = ", ".join(str(SOURCE_LABELS.get(r.get("source"), r.get("source")))
                          for r in results)
        return t(f"測速 {len(results)} 來源  平均 下載 {dl_s} / 上傳 {ul_s} Mbps  ({names})",
                 f"Speed {len(results)} sources  avg down {dl_s} / up {ul_s} Mbps  ({names})")
    if kind == "ping":
        targets = _rec_ping_targets(rec)
        avg = _avg([r.get("avg") for r in targets])
        avg_s = f"{avg:.2f}ms" if avg is not None else "n/a"
        return t(f"Ping {len(targets)} 目標  平均 {avg_s}",
                 f"Ping {len(targets)} targets  avg {avg_s}")
    if kind == "all":
        n_ping = len((rec.get("ping") or {}).get("targets") or [])
        n_speed = len(rec.get("speed") or [])
        has_ip = bool(rec.get("ip"))
        return t(f"統整  IP:{'有' if has_ip else '無'} + Ping:{n_ping} + 測速:{n_speed}",
                 f"Summary  IP:{'yes' if has_ip else 'no'} + Ping:{n_ping} + Speed:{n_speed}")
    if kind == "pair":
        dl = rec.get("download")
        ul = rec.get("upload")
        dl_s = f"{dl:.1f}" if isinstance(dl, (int, float)) else "n/a"
        ul_s = f"{ul:.1f}" if isinstance(ul, (int, float)) else "n/a"
        return t(f"對測 {rec.get('peer_ip')}:{rec.get('peer_port')} ({rec.get('method')}) "
                 f"下載 {dl_s} / 上傳 {ul_s} Mbps",
                 f"Pair {rec.get('peer_ip')}:{rec.get('peer_port')} ({rec.get('method')}) "
                 f"down {dl_s} / up {ul_s} Mbps")
    if kind == "ip":
        return t(f"外網 IP {rec.get('ip')} ({rec.get('isp') or rec.get('org') or '?'})",
                 f"Public IP {rec.get('ip')} ({rec.get('isp') or rec.get('org') or '?'})")
    return kind or "?"


def _short_time(ts) -> str:
    ts = str(ts or "?")
    if len(ts) >= 16 and ts[4] == "-" and ts[7] == "-":
        return ts[5:16]
    return ts


def _print_record_detail(rec: dict) -> None:
    kind = rec.get("kind")
    if kind == "speed":
        print_speed_table(_rec_speed_results(rec))
    elif kind == "ping":
        targets = _rec_ping_targets(rec)
        params = rec.get("params") or {}
        if params:
            print(tcol("  " + t("參數", "Params") + ": " + " ".join(f"{k}={v}" for k, v in params.items()),
                       "dim"))
        print()
        print_ping_table(targets)
    elif kind == "all":
        if rec.get("ip"):
            print_ip_section(rec["ip"])
        ping = rec.get("ping") or {}
        if ping.get("targets"):
            params = ping.get("params") or {}
            if params:
                print(tcol("  " + t("Ping 參數", "Ping params") + ": "
                           + " ".join(f"{k}={v}" for k, v in params.items()), "dim"))
            print()
            print_ping_table(ping["targets"])
        if rec.get("speed"):
            print_speed_table(rec["speed"])
    elif kind == "pair":
        print_pair_result(rec)
    elif kind == "ip":
        print_ip_section(rec)
    else:
        print(json.dumps(rec, ensure_ascii=False, indent=2))


def rerun_record(rec: dict, cfg: dict, args) -> None:
    kind = rec.get("kind")
    if kind == "speed":
        for r in _rec_speed_results(rec):
            cmd_source(r.get("source"), cfg, args)
    elif kind == "ping":
        targets = [r.get("target") for r in _rec_ping_targets(rec) if r.get("target")]
        if not targets:
            warn(t("此紀錄沒有可重跑的目標", "No target to rerun in this record"))
            return
        sub = argparse.Namespace(targets=targets, count=None, interval=None, timeout=None,
                                 size=None, method=None, no_gateway=True, only=True,
                                 json=getattr(args, "json", False),
                                 no_save=getattr(args, "no_save", False),
                                 no_color=getattr(args, "no_color", False))
        cmd_ping(cfg, sub)
    elif kind == "all":
        sources = [r.get("source") for r in (rec.get("speed") or []) if r.get("source")]
        sub = argparse.Namespace(sources=(",".join(sources) if sources else None),
                                 no_ping=not bool((rec.get("ping") or {}).get("targets")),
                                 no_ip=not bool(rec.get("ip")),
                                 seconds=None, json=getattr(args, "json", False),
                                 no_save=getattr(args, "no_save", False),
                                 no_color=getattr(args, "no_color", False),
                                 config=None)
        cmd_all(cfg, sub)
    elif kind == "pair":
        sub = argparse.Namespace(host=rec.get("peer_ip"), port=rec.get("peer_port"),
                                 method=rec.get("method") or "auto", mode=rec.get("mode") or "both",
                                 duration=None, streams=None, iperf_path=None,
                                 json=getattr(args, "json", False),
                                 no_save=getattr(args, "no_save", False),
                                 no_color=getattr(args, "no_color", False))
        cmd_pair_client(cfg, sub)
    elif kind == "ip":
        cmd_ip(cfg, args)
    else:
        warn(t(f"不支援重跑類型: {kind}", f"Unsupported record type: {kind}"))


def _record_detail_menu(rec: dict, cfg: dict, args) -> None:
    _print_record_detail(rec)
    if not sys.stdin.isatty():
        return
    try:
        ans = input(col(t("\n[r] 重跑此項 / [Enter] 返回: ", "\n[r] rerun / [Enter] back: "),
                        "bold")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if ans == "r":
        rerun_record(rec, cfg, args)


def cmd_history(cfg: dict, args, interactive: bool = True) -> None:
    kind = getattr(args, "kind", None)
    limit = getattr(args, "limit", 20)
    run = getattr(args, "run", None)
    records = load_history()
    if kind:
        records = [r for r in records if r.get("kind") == kind]
    if not records:
        info(t("目前沒有歷史紀錄。", "No history yet."))
        return
    shown = records[-limit:][::-1]
    section(t("歷史紀錄", "History"))
    print(tcol(t("  格式: [編號] 時間  類型摘要 — 輸入編號可查看內容並重跑",
                 "  Format: [no] time  type-summary — enter a number to view details and rerun"),
               "dim"))
    for i, rec in enumerate(shown, 1):
        print(f"  [{i:>3}] {_short_time(rec.get('time')):<12} {_record_desc(rec)}")

    if run:
        idx = run
        if 1 <= idx <= len(shown):
            _record_detail_menu(shown[idx - 1], cfg, args)
        else:
            error(t("編號超出範圍", "Index out of range"))
        return

    if interactive and sys.stdin.isatty():
        try:
            choice = input(col(t("\n輸入編號查看/重跑 (Enter 取消): ",
                                 "\nEnter number to view/rerun (Enter to cancel): "), "bold")).strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(shown):
                _record_detail_menu(shown[idx - 1], cfg, args)
            else:
                error(t("編號超出範圍", "Index out of range"))


def cmd_config(cfg: dict, args) -> None:
    action = args.action
    values = getattr(args, "values", None) or []
    if action == "path":
        print(config_path())
    elif action == "edit":
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
        if not editor:
            editor = "notepad" if os.name == "nt" else "nano"
        subprocess.call([editor, config_path()])
    elif action == "reset":
        reset = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg.clear()
        cfg.update(reset)
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(t("設定已重設為預設值", "Config reset to defaults"))
    elif action == "export":
        path = values[0] if values else os.path.join(os.getcwd(), "netspeed-config.json")
        try:
            save_config(cfg, path)
        except OSError as exc:
            error(t(f"匯出失敗: {exc}", f"Export failed: {exc}"))
            return
        ok(t(f"已匯出設定: {path}", f"Config exported: {path}"))
    elif action == "import":
        if not values:
            error(t("用法: config import <檔案>", "Usage: config import <file>"))
            return
        path = values[0]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            error(t(f"讀取失敗: {exc}", f"Read failed: {exc}"))
            return
        merged = deep_merge(DEFAULT_CONFIG, data)
        cfg.clear()
        cfg.update(merged)
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(t(f"已匯入設定: {path}", f"Config imported: {path}"))
    else:
        print(tcol(t(f"設定檔: {config_path()}", f"Config file: {config_path()}"), "dim"))
        print(json.dumps(cfg, ensure_ascii=False, indent=2))


def cmd_theme(cfg: dict, args) -> None:
    action = args.action
    values = getattr(args, "values", None) or []
    theme = cfg.setdefault("theme", dict(DEFAULT_THEME))
    if action == "set":
        if len(values) < 2:
            error(t("用法: theme set <項目> <顏色>   顏色例: red / green,bold / default",
                    "Usage: theme set <key> <color>   e.g. red / green,bold / default"))
            return
        key, val = values[0], values[1]
        if key not in DEFAULT_THEME:
            warn(t(f"未知項目 {key} (可用: {', '.join(DEFAULT_THEME)})",
                   f"Unknown key {key} (available: {', '.join(DEFAULT_THEME)})"))
        theme[key] = val
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(f"theme.{key} = {val}")
    elif action == "reset":
        cfg["theme"] = dict(DEFAULT_THEME)
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(t("主題已重設為預設", "Theme reset to default"))
    elif action == "preset":
        name = values[0] if values else ""
        if name not in THEME_PRESETS:
            error(t(f"未知 preset: {name} (可用: {', '.join(THEME_PRESETS)})",
                    f"Unknown preset: {name} (available: {', '.join(THEME_PRESETS)})"))
            return
        cfg["theme"] = dict(THEME_PRESETS[name])
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(t(f"已套用主題 preset: {name}", f"Theme preset applied: {name}"))
    elif action == "export":
        path = values[0] if values else os.path.join(os.getcwd(), "netspeed-theme.json")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg.get("theme", {}), fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            error(t(f"匯出失敗: {exc}", f"Export failed: {exc}"))
            return
        ok(t(f"已匯出主題: {path}", f"Theme exported: {path}"))
    elif action == "import":
        if not values:
            error(t("用法: theme import <檔案>", "Usage: theme import <file>"))
            return
        path = values[0]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            error(t(f"讀取失敗: {exc}", f"Read failed: {exc}"))
            return
        cfg["theme"] = deep_merge(DEFAULT_THEME, data)
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(t(f"已匯入主題: {path}", f"Theme imported: {path}"))
    else:
        section(t("主題 (theme)", "Theme"))
        for key in DEFAULT_THEME:
            val = theme.get(key, DEFAULT_THEME[key])
            sample = t("範例 Sample", "Sample") if not val or val == "default" \
                else col(t("範例 Sample", "Sample"), *str(val).split(","))
            print(f"  {key:<10} {val:<16} {sample}")
        print()
        print(tcol(t("可設定項目: ", "Keys: ") + ", ".join(DEFAULT_THEME), "dim"))
        print(tcol("presets: " + ", ".join(THEME_PRESETS), "dim"))
        print(tcol(t("用法: theme set <項目> <顏色> / theme preset <名稱> / theme export|import <檔案>",
                     "Usage: theme set <key> <color> / theme preset <name> / theme export|import <file>"),
                   "dim"))


def cmd_targets(cfg: dict, args) -> None:
    action = args.action
    targets = cfg.setdefault("ping", {}).setdefault("targets", [])
    if action == "add":
        for tg in args.values:
            if tg not in targets:
                targets.append(tg)
        save_config(cfg)
        ok(t(f"已新增: {', '.join(args.values)}", f"Added: {', '.join(args.values)}"))
    elif action == "del":
        for tg in args.values:
            if tg in targets:
                targets.remove(tg)
        save_config(cfg)
        ok(t(f"已刪除: {', '.join(args.values)}", f"Removed: {', '.join(args.values)}"))
    elif action == "set":
        cfg["ping"]["targets"] = list(args.values)
        save_config(cfg)
        ok(t("已設定 ping 目標清單", "Ping target list updated"))
    elif action == "gateway":
        gw = get_default_gateway()
        print(t("Default gateway: ", "Default gateway: ") + (gw or "n/a"))
        return
    else:
        print(col(t("Ping 目標清單:", "Ping target list:"), "bold"))
        for i, tg in enumerate(targets, 1):
            print(f"  {i:>2}. {tg}")
        print(col(t(f"  自動偵測 default gateway: {cfg['ping'].get('auto_gateway')}",
                    f"  Auto default gateway: {cfg['ping'].get('auto_gateway')}"), "dim"))


def cmd_endpoints(cfg: dict, args) -> None:
    if args.action == "set":
        if len(args.values) < 2:
            error(t("用法: endpoints set <來源> <值>", "Usage: endpoints set <source> <value>"))
            return
        source, value = args.values[0], " ".join(args.values[1:])
        set_dotted(cfg, f"endpoints.{source}", value)
        save_config(cfg)
        ok(t(f"已設定 endpoints.{source} = {value}", f"Set endpoints.{source} = {value}"))
        return
    print(col(t("測速端點設定 (endpoints):", "Speed endpoints:"), "bold"))
    for key, val in cfg.get("endpoints", {}).items():
        print(f"  {key:<12}: {val or t('(自動)', '(auto)')}")
    print()
    print(col(t("啟用來源 (sources):", "Sources:"), "bold"))
    print("  " + ", ".join(cfg.get("sources", [])))


# --------------------------------------------------------------------------- #
# 系統網路資訊 (DHCP/靜態, IP/遮罩, 閘道, 靜態路由, sub IP)
# --------------------------------------------------------------------------- #


def _prefix_to_netmask(prefix) -> str | None:
    try:
        p = int(prefix)
    except (TypeError, ValueError):
        return None
    if p < 0 or p > 32:
        return None
    mask = (0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF if p else 0
    return socket.inet_ntoa(struct.pack("!I", mask))


def _netmask_to_prefix(mask) -> int | None:
    if not mask:
        return None
    try:
        packed = socket.inet_aton(mask)
    except OSError:
        return None
    return bin(int.from_bytes(packed, "big")).count("1")


def _hexmask_to_dotted(mask: str) -> str | None:
    try:
        return socket.inet_ntoa(struct.pack("!I", int(mask, 16)))
    except (ValueError, OSError):
        return None


def _loads_loose(text: str):
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def _is_loopback(name: str, ipv4: list) -> bool:
    low = name.lower()
    if low.startswith("lo") or "loopback" in low:
        return True
    return bool(ipv4) and all(str(i.get("ip", "")).startswith("127.") for i in ipv4)


def _linux_dhcp_methods() -> dict:
    methods = {}
    if shutil.which("nmcli"):
        p = run_cmd(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"], timeout=10)
        for line in (p.stdout or "").splitlines():
            if ":" not in line:
                continue
            name, _, dev = line.rpartition(":")
            if not dev:
                continue
            q = run_cmd(["nmcli", "-t", "-f", "ipv4.method", "connection", "show", name], timeout=10)
            val = (q.stdout or "").strip()
            if val:
                methods[dev] = (val == "auto")
        if methods:
            return methods

    for path in ["/etc/network/interfaces", "/etc/network/interfaces.d"]:
        try:
            files = ([path] if os.path.isfile(path)
                     else [os.path.join(path, f) for f in os.listdir(path)])
        except OSError:
            continue
        for f in files:
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        m = re.match(r"\s*iface\s+(\S+)\s+inet\s+(dhcp|static)", line)
                        if m:
                            methods[m.group(1)] = (m.group(2) == "dhcp")
            except OSError:
                pass
        if methods:
            return methods

    lease_dir = "/var/lib/dhcp"
    if os.path.isdir(lease_dir):
        for f in os.listdir(lease_dir):
            if not f.endswith(".leases"):
                continue
            try:
                with open(os.path.join(lease_dir, f), "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        m = re.match(r'\s*interface\s+"([^"]+)"', line)
                        if m:
                            methods[m.group(1)] = True
            except OSError:
                pass
    return methods


def _fmt_link_speed(mbps) -> str | None:
    try:
        n = int(round(float(mbps)))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n >= 1000:
        return f"{n / 1000:g} Gbps"
    return f"{n} Mbps"


def _linux_link_speed(name: str) -> str | None:
    try:
        with open(f"/sys/class/net/{name}/speed", "r", encoding="utf-8") as fh:
            return _fmt_link_speed(fh.read().strip())
    except Exception:
        pass
    proc = run_cmd(["ethtool", name], timeout=5)
    m = re.search(r"Speed:\s*(\d+)\s*Mb/s", proc.stdout or "")
    return _fmt_link_speed(m.group(1)) if m else None


def _mac_media_speed(text: str) -> str | None:
    m = re.search(r"(\d+)(G?)base", text)
    if not m:
        return None
    n = int(m.group(1))
    if m.group(2):
        n *= 1000
    return _fmt_link_speed(n)


def _net_linux() -> dict:
    data = {"interfaces": [], "routes": []}
    dhcp = _linux_dhcp_methods()

    addrs = _loads_loose(run_cmd(["ip", "-j", "addr", "show"], timeout=10).stdout or "")
    if addrs is None:
        addrs = []
    if isinstance(addrs, dict):
        addrs = [addrs]

    for iface in addrs:
        name = iface.get("ifname")
        ipv4 = []
        for ai in iface.get("addr_info", []) or []:
            if ai.get("family") != "inet":
                continue
            ip = ai.get("local")
            prefix = ai.get("prefixlen")
            ipv4.append({"ip": ip, "prefix": prefix,
                         "netmask": _prefix_to_netmask(prefix),
                         "cidr": f"{ip}/{prefix}"})
        for i, item in enumerate(ipv4):
            item["secondary"] = i > 0
        data["interfaces"].append({
            "name": name,
            "ipv4": ipv4,
            "mac": iface.get("address"),
            "status": iface.get("operstate"),
            "dhcp": dhcp.get(name),
            "gateway": None,
            "speed": _linux_link_speed(name),
        })

    routes = _loads_loose(run_cmd(["ip", "-j", "route", "show"], timeout=10).stdout or "")
    if routes is None:
        routes = []
    if isinstance(routes, dict):
        routes = [routes]
    for r in routes:
        dest = r.get("dst")
        gw = r.get("gateway")
        dev = r.get("dev")
        if dest == "default":
            for iface in data["interfaces"]:
                if iface["name"] == dev and not iface["gateway"]:
                    iface["gateway"] = gw
            continue
        if not gw or gw == "0.0.0.0":
            continue
        data["routes"].append({"dest": dest, "gateway": gw, "iface": dev,
                               "metric": r.get("metric"), "proto": r.get("protocol") or r.get("proto")})
    return data


def _net_windows() -> dict:
    data = {"interfaces": [], "routes": []}
    ps = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$ErrorActionPreference='SilentlyContinue';"
        "$ips=Get-NetIPAddress -AddressFamily IPv4|Select-Object InterfaceAlias,InterfaceIndex,IPAddress,PrefixLength,SkipAsSource;"
        "$ifs=Get-NetIPInterface -AddressFamily IPv4|Select-Object InterfaceAlias,InterfaceIndex,Dhcp;"
        "$routes=Get-NetRoute -AddressFamily IPv4|Select-Object InterfaceAlias,InterfaceIndex,DestinationPrefix,NextHop,RouteMetric,Protocol;"
        "$adapters=Get-NetAdapter|Select-Object Name,InterfaceIndex,MacAddress,Status,LinkSpeed;"
        "[pscustomobject]@{ips=@($ips);ifs=@($ifs);routes=@($routes);adapters=@($adapters)}|ConvertTo-Json -Depth 4 -Compress"
    )
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                          capture_output=True, timeout=40)
    root = _loads_loose(proc.stdout.decode("utf-8", "replace"))
    if not root:
        return data

    adapters = {a.get("InterfaceIndex"): a for a in (root.get("adapters") or [])}
    iface_map = {}

    def get_iface(idx, alias):
        if idx not in iface_map:
            ad = adapters.get(idx, {})
            iface_map[idx] = {
                "name": alias or ad.get("Name"),
                "ipv4": [],
                "mac": ad.get("MacAddress"),
                "status": ad.get("Status"),
                "dhcp": None,
                "gateway": None,
                "speed": ad.get("LinkSpeed"),
            }
        return iface_map[idx]

    for ip in root.get("ips") or []:
        iface = get_iface(ip.get("InterfaceIndex"), ip.get("InterfaceAlias"))
        iface["ipv4"].append({
            "ip": ip.get("IPAddress"),
            "prefix": ip.get("PrefixLength"),
            "netmask": _prefix_to_netmask(ip.get("PrefixLength")),
            "cidr": f"{ip.get('IPAddress')}/{ip.get('PrefixLength')}",
            "secondary": bool(ip.get("SkipAsSource")),
        })
    for it in root.get("ifs") or []:
        iface = get_iface(it.get("InterfaceIndex"), it.get("InterfaceAlias"))
        iface["dhcp"] = (str(it.get("Dhcp")).lower() == "enabled")
    for r in root.get("routes") or []:
        dest = r.get("DestinationPrefix")
        nexthop = r.get("NextHop")
        iface = get_iface(r.get("InterfaceIndex"), r.get("InterfaceAlias"))
        if dest == "0.0.0.0/0":
            if not iface["gateway"]:
                iface["gateway"] = nexthop
            continue
        if not nexthop or nexthop == "0.0.0.0":
            continue
        if str(r.get("Protocol")).lower() == "local":
            continue
        proto = r.get("Protocol")
        proto_name = {1: "Other", 2: "Local", 3: "Static", 4: "ICMP",
                      8: "RIP", 13: "OSPF", 14: "BGP", 16: "EIGRP"}.get(proto, proto)
        data["routes"].append({"dest": dest, "gateway": nexthop, "iface": iface["name"],
                               "metric": r.get("RouteMetric"), "proto": proto_name})
    for iface in iface_map.values():
        for i, item in enumerate(iface["ipv4"]):
            item["secondary"] = i > 0 or item.get("secondary")
    data["interfaces"] = list(iface_map.values())
    return data


def _net_macos() -> dict:
    data = {"interfaces": [], "routes": []}
    out = run_cmd(["ifconfig", "-a"], timeout=10).stdout or ""
    ifaces = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"^(\w+):\s+flags=", line)
        if m:
            cur = m.group(1)
            ifaces[cur] = {"name": cur, "ipv4": [], "mac": None, "status": None,
                           "dhcp": None, "gateway": None, "speed": None}
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("inet "):
            parts = s.split()
            ip = parts[1]
            mask = None
            if "netmask" in parts:
                mask = parts[parts.index("netmask") + 1]
                if mask.startswith("0x"):
                    mask = _hexmask_to_dotted(mask)
            prefix = _netmask_to_prefix(mask)
            ifaces[cur]["ipv4"].append({"ip": ip, "prefix": prefix, "netmask": mask,
                                        "cidr": f"{ip}/{prefix}" if prefix is not None else ip})
        elif s.startswith("ether "):
            ifaces[cur]["mac"] = s.split()[1]
        elif s.startswith("media:"):
            spd = _mac_media_speed(s)
            if spd:
                ifaces[cur]["speed"] = spd
        elif s.startswith("status:"):
            ifaces[cur]["status"] = s.split(":", 1)[1].strip()

    for name, iface in ifaces.items():
        for i, item in enumerate(iface["ipv4"]):
            item["secondary"] = i > 0
        if iface["ipv4"] and not name.startswith("lo"):
            p = run_cmd(["ipconfig", "getpacket", name], timeout=8)
            o = (p.stdout or "").lower()
            if p.returncode == 0 and ("yiaddr" in o or "server_identifier" in o):
                iface["dhcp"] = True
            elif p.returncode != 0 or "no dhcp" in o:
                iface["dhcp"] = False
    data["interfaces"] = list(ifaces.values())

    p = run_cmd(["route", "-n", "get", "default"], timeout=8)
    m = re.search(r"gateway:\s*([\d.]+)", p.stdout or "")
    dev = re.search(r"interface:\s*(\S+)", p.stdout or "")
    if m:
        for iface in data["interfaces"]:
            if dev and iface["name"] == dev.group(1):
                iface["gateway"] = m.group(1)

    out = run_cmd(["netstat", "-rn", "-f", "inet"], timeout=10).stdout or ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or not re.match(r"^[\d.]+", parts[0]):
            continue
        dest, gw, flags = parts[0], parts[1], parts[2]
        if dest == "default" or "S" not in flags:
            continue
        if dest.startswith(("127.", "169.254")):
            continue
        data["routes"].append({"dest": dest, "gateway": gw, "iface": parts[-1],
                               "metric": parts[4] if len(parts) > 4 else None, "proto": "static"})
    return data


def collect_netinfo(refresh: bool = False) -> dict:
    if not refresh and "netinfo" in _SESSION_CACHE:
        return _SESSION_CACHE["netinfo"]
    if os.name == "nt":
        data = _net_windows()
    elif sys.platform == "darwin":
        data = _net_macos()
    else:
        data = _net_linux()
    _SESSION_CACHE["netinfo"] = data
    return data


def _real_ifaces(data: dict) -> list:
    return [i for i in data.get("interfaces", []) if i.get("ipv4") and not _is_loopback(i["name"], i["ipv4"])]


def _dhcp_text(val) -> str:
    if val is True:
        return t("DHCP (自動取得)", "DHCP (automatic)")
    if val is False:
        return t("靜態 (手動設定)", "Static (manual)")
    return t("未知", "unknown")


def _print_iface(iface: dict) -> None:
    print(col(t(f"介面: {iface['name']}", f"Interface: {iface['name']}"), "bold")
          + (t(f"   狀態: {iface.get('status')}", f"   status: {iface.get('status')}")
             if iface.get("status") else "")
          + (f"   MAC: {iface['mac']}" if iface.get("mac") else ""))
    print(t(f"  設定方式 : {_dhcp_text(iface.get('dhcp'))}",
            f"  Config   : {_dhcp_text(iface.get('dhcp'))}"))
    if iface.get("speed"):
        print(t(f"  Link Speed : {iface.get('speed')}", f"  Link Speed : {iface.get('speed')}"))
    for item in iface.get("ipv4", []):
        tag = t("次要 Sub IP", "secondary") if item.get("secondary") else t("主要", "primary")
        mask = item.get("netmask") or "?"
        print(t(f"  IP       : {item.get('cidr'):<20} 遮罩 {mask:<16} [{tag}]",
                f"  IP       : {item.get('cidr'):<20} mask {mask:<16} [{tag}]"))
    print(t(f"  Default Gateway : {iface.get('gateway') or 'n/a'}",
            f"  Default Gateway : {iface.get('gateway') or 'n/a'}"))


def _print_routes(routes: list) -> None:
    if not routes:
        return
    print()
    print(col(t("Static Route (靜態路由):", "Static routes:"), "bold"))
    print(f"  {t('目標', 'Dest'):<22} {t('閘道', 'Gateway'):<18} {t('介面', 'Iface'):<12} "
          f"{t('計量', 'Metric'):>6}  {t('協定', 'Proto')}")
    for r in routes:
        print(f"  {str(r.get('dest')):<22} {str(r.get('gateway')):<18} "
              f"{str(r.get('iface') or ''):<12} {str(r.get('metric') or ''):>6}  {r.get('proto') or ''}")


def _default_iface(data: dict):
    real = _real_ifaces(data)
    for ifc in real:
        if ifc.get("gateway"):
            return ifc
    return real[0] if real else None


def _netinfo_menu(real: list, all_ifaces: list):
    if not real:
        return all_ifaces
    print("  " + t("選擇要顯示的介面:", "Select interface to show:"))
    for i, ifc in enumerate(real, 1):
        ips = ", ".join(x.get("cidr", "?") for x in ifc.get("ipv4", []))
        gw = ifc.get("gateway")
        tag = tcol("  (default route)", "dim") if gw else ""
        print(f"    [{i}] {ifc['name']:<24} {ips}{tag}")
    print(tcol("    " + t("[a] 全部    [Enter/q] 離開", "[a] all    [Enter/q] leave"), "dim"))
    try:
        choice = input(col(t("  請輸入: ", "  Enter: "), "bold")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if choice in ("", "q", "quit", "exit"):
        info(t("已取消。", "Cancelled."))
        return None
    if choice == "a":
        return real
    if choice.isdigit() and 1 <= int(choice) <= len(real):
        return [real[int(choice) - 1]]
    warn(t("無效的選擇", "Invalid choice"))
    return None


def cmd_netinfo(cfg: dict, args) -> None:
    data = collect_netinfo(refresh=getattr(args, "refresh", False))
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    all_ifaces = data.get("interfaces", [])
    real = _real_ifaces(data)

    if getattr(args, "interface", None):
        selected = [i for i in all_ifaces if i["name"] == args.interface]
        if not selected:
            error(t(f"找不到介面: {args.interface}", f"Interface not found: {args.interface}"))
            return
    elif getattr(args, "all", False):
        selected = real or all_ifaces
    elif getattr(args, "select", False):
        selected = _netinfo_menu(real, all_ifaces)
        if selected is None:
            return
    else:
        default = _default_iface(data)
        selected = [default] if default else (real or all_ifaces)

    if not selected:
        error(t("找不到作用中的網路介面", "No active network interface found"))
        return

    section(t("系統網路資訊", "Network information"))
    for ifc in selected:
        _print_iface(ifc)
        print()

    if (not getattr(args, "all", False) and not getattr(args, "interface", None)
            and not getattr(args, "select", False) and len(real) > 1):
        print(tcol(t(f"  (偵測到 {len(real)} 個介面; netinfo -i 可選擇, netinfo --all 顯示全部)",
                     f"  ({len(real)} interfaces found; netinfo -i to select, netinfo --all to show all)"),
                   "dim"))

    routes = data.get("routes", [])
    if getattr(args, "interface", None):
        routes = [r for r in routes if r.get("iface") == args.interface]
    _print_routes(routes)


# --------------------------------------------------------------------------- #
# 工具狀態檢查
# --------------------------------------------------------------------------- #


def _check_internet(timeout: float = 3.0) -> bool:
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _check_ping() -> tuple:
    try:
        r = icmp_ping("127.0.0.1", 1, 0.1, 1.0, 56)
        if r.get("recv"):
            return True, "ICMP (純 Python, 已可用)"
    except Exception:
        pass
    if shutil.which("ping"):
        return True, "系統 ping (需權限時自動使用)"
    return False, "無法 ping"


def _collect_tools(cfg: dict) -> list:
    checks = []
    okping, ping_detail = _check_ping()
    checks.append({"ok": okping, "label": "Ping", "detail": ping_detail,
                   "category": "ping", "in_all": True})
    for tool in SPEED_TOOLS:
        found = find_tool(tool["names"])
        checks.append({
            "ok": bool(found), "label": tool["label"],
            "detail": found or (t("找不到 — 下載: ", "not found — download: ") + tool["url"]),
            "category": "speed", "in_all": True,
        })
    iperf = find_iperf(cfg)
    checks.append({"ok": bool(iperf), "label": "iperf3",
                   "detail": iperf or t("找不到 — 將使用內建對測", "not found — will use builtin"),
                   "category": "pair", "in_all": False})
    return checks


def _collect_env(cfg: dict) -> list:
    env = []
    env.append((True, "Python", sys.version.split()[0]))
    try:
        ensure_dir(config_dir())
        with open(os.path.join(config_dir(), ".write_test"), "w") as fh:
            fh.write("x")
        os.remove(os.path.join(config_dir(), ".write_test"))
        env.append((True, t("設定檔目錄", "Config dir"), config_dir()))
    except Exception as exc:
        env.append((False, t("設定檔目錄", "Config dir"), f"{config_dir()} ({exc})"))
    env.append((True, t("Cloudflare 測速", "Cloudflare speed"),
                t("內建 (純 Python, 不需外部工具)", "builtin (pure Python)")))
    env.append((True, t("外網 IP / 電信商", "Public IP / ISP"),
                t("內建 (ipinfo / ifconfig / ip-api 備援)",
                  "builtin (ipinfo / ifconfig / ip-api)")))
    online = _check_internet()
    env.append((online, t("網際網路連線", "Internet"),
                t("可達", "reachable") if online
                else t("無法連線 (TCP 1.1.1.1:443)", "unreachable (TCP 1.1.1.1:443)")))
    return env


def cmd_show_tools(cfg: dict, args) -> None:
    if not getattr(args, "refresh", False) and "check" in _SESSION_CACHE:
        checks = _SESSION_CACHE["check"]
    else:
        checks = _collect_tools(cfg)
        _SESSION_CACHE["check"] = checks
    if getattr(args, "json", False):
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return
    section(t("工具狀態檢查", "Tool status"))
    print(tcol(t("  (* = 執行 all 時會一併執行)", "  (* = also runs under all)"), "dim"))
    cats = [("ping", t("Ping", "Ping")),
            ("speed", t("測速", "Speed")),
            ("pair", t("對測 (pair)", "Pair"))]
    for key, title in cats:
        items = [c for c in checks if c.get("category") == key]
        if not items:
            continue
        print()
        print(tcol(f"  [{title}]", "heading"))
        for c in items:
            mark = tcol("[v]", "ok") if c["ok"] else tcol("[x]", "error")
            star = "*" if c.get("in_all") else " "
            print(f"   {star}{mark} {c['label']:<16} {c['detail']}")
    usable = sum(1 for c in checks if c["ok"])
    print()
    print(f"  {t('可用項目', 'Available')}: {usable}/{len(checks)}")
    print(tcol("  " + t("提示: 外部工具可放 PATH, 或直接放在 netspeed 同層目錄: ",
                        "Tip: put tools on PATH, or next to netspeed: ")
               + os.path.dirname(os.path.abspath(__file__)), "dim"))


def cmd_show_env(cfg: dict, args) -> None:
    if not getattr(args, "refresh", False) and "env" in _SESSION_CACHE:
        env = _SESSION_CACHE["env"]
    else:
        env = _collect_env(cfg)
        _SESSION_CACHE["env"] = env
    if getattr(args, "json", False):
        print(json.dumps([{"ok": e[0], "item": e[1], "detail": e[2]} for e in env],
                         ensure_ascii=False, indent=2))
        return
    section(t("環境狀態檢查", "Environment check"))
    for good, item, detail in env:
        mark = tcol("[v]", "ok") if good else tcol("[x]", "error")
        print(f"  {mark} {item:<16} {detail}")
    usable = sum(1 for e in env if e[0])
    print()
    print(f"  {t('正常項目', 'OK items')}: {usable}/{len(env)}")


def cmd_show_debug(cfg: dict, args) -> None:
    global DEBUG
    action = str(getattr(args, "action", None) or "status").lower()
    if action in ("enable", "on", "true", "yes"):
        cfg["debug"] = True
        save_config(cfg, getattr(args, "config", None))
        DEBUG = True
        ok(t("已開啟 debug 模式 (已寫入設定; 之後執行都會輸出除錯訊息到 stderr)",
             "Debug mode enabled (saved; future runs print debug info to stderr)"))
    elif action in ("disable", "off", "false", "no"):
        cfg["debug"] = False
        save_config(cfg, getattr(args, "config", None))
        DEBUG = False
        ok(t("已關閉 debug 模式 (已寫入設定)",
             "Debug mode disabled (saved)"))
    elif action not in ("status", "show", ""):
        error(t(f"未知選項: {action} (可用: enable / disable / status)",
                f"Unknown option: {action} (use: enable / disable / status)"))
        return
    else:
        state = t("開啟", "on") if cfg.get("debug") else t("關閉", "off")
        print(f"  debug: {state}")
        print(tcol("  " + t("用法: show debug enable|disable   (輸出到 stderr, 可用 2> 存檔)",
                            "Usage: show debug enable|disable   (output to stderr, use 2> to save)"), "dim"))
        return
    print(tcol("  " + t("除錯訊息輸出到 stderr; 例如: netspeed all 2> debug.log",
                        "Debug info goes to stderr; e.g. netspeed all 2> debug.log"), "dim"))
    dbg("debug", t("debug 模式已開啟", "debug mode is now on"))


def cmd_show_ping(cfg: dict, args) -> None:
    pc = cfg.get("ping", {})
    section(t("Ping 設定", "Ping settings"))
    print(f"  {t('參數', 'Params')}: count:{pc.get('count')} interval:{pc.get('interval')} "
          f"timeout:{pc.get('timeout')} size:{pc.get('size')} method:{pc.get('method')}")
    auto = t("on", "on") if pc.get("auto_gateway", True) else "off"
    print(f"  {t('預設閘道自動加入', 'Auto default gateway')}: {auto}   "
          f"({t('目前閘道', 'current gateway')}: {_ping_gateway_text(cfg)})")
    targets = pc.get("targets", [])
    print(f"  {t('目標清單', 'Targets')} ({len(targets)}):")
    for i, tg in enumerate(targets, 1):
        print(f"    {i}. {tg}")
    print(tcol("  " + t("修改: config targets ... / ping 互動選單 / configure 模式",
                        "Edit: config targets ... / ping menu / configure mode"), "dim"))


def cmd_check(cfg: dict, args) -> None:
    cmd_show_tools(cfg, args)


# --------------------------------------------------------------------------- #
# 版本 / 更新 (GitHub Releases)
# --------------------------------------------------------------------------- #


def _parse_version(v) -> tuple:
    nums = re.findall(r"\d+", str(v))
    return tuple(int(n) for n in nums[:4]) if nums else (0,)


def _github_latest(repo: str) -> dict:
    return json.loads(_http_get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=8))


def _do_update(repo: str, tag: str, release: dict) -> bool:
    url = None
    for asset in release.get("assets", []) or []:
        if asset.get("name") == "netspeed.py":
            url = asset.get("browser_download_url")
            break
    if not url:
        url = f"https://raw.githubusercontent.com/{repo}/{tag}/netspeed.py"
    try:
        content = _http_get(url, timeout=20)
    except Exception as exc:
        error(t(f"下載更新失敗: {exc}", f"Download failed: {exc}"))
        return False
    if "def main(" not in content or "netspeed" not in content:
        error(t("下載內容不像是 netspeed.py, 已中止", "Downloaded content is not netspeed.py; aborted"))
        return False
    path = os.path.abspath(__file__)
    try:
        shutil.copy2(path, path + ".bak")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
    except OSError as exc:
        error(t(f"寫入失敗: {exc}", f"Write failed: {exc}"))
        return False
    inst = os.path.join(install_dir(), "netspeed.py")
    if os.path.exists(inst) and os.path.abspath(inst) != path:
        try:
            shutil.copy2(path, inst)
        except OSError:
            pass
    return True


def check_update(cfg: dict, interactive: bool = True, force: bool = False) -> None:
    repo = get_dotted(cfg, "update.repo")
    if not repo:
        if force:
            warn(t("尚未設定更新來源。請先設定: netspeed set update.repo <你的帳號>/<repo>",
                   "Update repo not set. Use: netspeed set update.repo <owner>/<repo>"))
        return
    if not force and not get_dotted(cfg, "update.check_on_start"):
        return
    now = time.time()
    last = get_dotted(cfg, "update.last_check") or 0
    interval = (get_dotted(cfg, "update.interval_hours") or 24) * 3600
    if not force and now - last < interval:
        return
    try:
        release = _github_latest(repo)
    except Exception:
        return
    set_dotted(cfg, "update.last_check", now)
    save_config(cfg)

    tag = release.get("tag_name") or ""
    latest = tag.lstrip("vV")
    if not latest or _parse_version(latest) <= _parse_version(VERSION):
        if force:
            ok(t(f"已是最新版本 ({VERSION})", f"Already latest ({VERSION})"))
        return
    if latest == get_dotted(cfg, "update.skip_version"):
        if force:
            info(t(f"版本 {latest} 已被標記為不更新。", f"Version {latest} marked as skipped."))
        return

    section(t("發現新版本", "New version available"))
    print(t(f"  目前版本 : {VERSION}", f"  Current : {VERSION}"))
    print(t(f"  最新版本 : {col(latest, 'green', 'bold')}", f"  Latest  : {col(latest, 'green', 'bold')}"))
    body = (release.get("body") or "").strip()
    if body:
        print(t("  更新內容 :", "  Changelog:"))
        for line in body.splitlines()[:20]:
            print("    " + line)
    print()
    if not interactive:
        return
    try:
        ans = input(col(t("是否更新? [y=更新 / n=稍後 / s=此版本不再通知]: ",
                          "Update now? [y=yes / n=later / s=skip this version]: "), "bold")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if ans == "s":
        set_dotted(cfg, "update.skip_version", latest)
        save_config(cfg)
        ok(t(f"已設定不再通知版本 {latest}", f"Will not notify for version {latest}"))
    elif ans in ("y", "yes"):
        if _do_update(repo, tag, release):
            ok(t("更新完成, 請重新啟動 netspeed。", "Update complete; please restart netspeed."))
        else:
            error(t("更新失敗。", "Update failed."))


def cmd_update(cfg: dict, args) -> None:
    if getattr(args, "reset_skip", False):
        set_dotted(cfg, "update.skip_version", "")
        save_config(cfg)
        ok(t("已清除『不再通知』的版本設定", "Cleared 'skip version' setting"))
    check_update(cfg, interactive=sys.stdin.isatty(), force=True)


# --------------------------------------------------------------------------- #
# 安裝 / 移除 (放到 PATH, 建立 netspeed 啟動器)
# --------------------------------------------------------------------------- #


def install_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Programs", APP)
    return os.path.join(os.path.expanduser("~"), ".local", "share", APP)


def bin_dir() -> str:
    if os.name == "nt":
        return install_dir()
    return os.path.join(os.path.expanduser("~"), ".local", "bin")


def _launcher_path() -> str:
    return os.path.join(bin_dir(), "netspeed.cmd" if os.name == "nt" else "netspeed")


def _python_exe() -> str:
    return sys.executable or ("python" if os.name == "nt" else "python3")


def _path_has(dirpath: str) -> bool:
    target = os.path.normcase(os.path.normpath(dirpath))
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p and os.path.normcase(os.path.normpath(p)) == target:
            return True
    return False


def _win_env_path():
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0,
                         winreg.KEY_READ | winreg.KEY_WRITE)
    try:
        value, typ = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        value, typ = "", winreg.REG_EXPAND_SZ
    return key, value, typ


def _win_broadcast():
    try:
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x1A, 0, "Environment", 0x0002, 1000, None)
    except Exception:
        pass


def _win_add_path(dirpath: str) -> bool:
    import winreg
    key, value, _typ = _win_env_path()
    entries = [e for e in value.split(os.pathsep) if e]
    if any(os.path.normcase(os.path.normpath(e)) == os.path.normcase(os.path.normpath(dirpath))
           for e in entries):
        winreg.CloseKey(key)
        return False
    entries.append(dirpath)
    winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, os.pathsep.join(entries))
    winreg.CloseKey(key)
    _win_broadcast()
    return True


def _win_remove_path(dirpath: str) -> bool:
    import winreg
    key, value, _typ = _win_env_path()
    entries = [e for e in value.split(os.pathsep) if e]
    kept = [e for e in entries
            if os.path.normcase(os.path.normpath(e)) != os.path.normcase(os.path.normpath(dirpath))]
    changed = len(kept) != len(entries)
    if changed:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, os.pathsep.join(kept))
        _win_broadcast()
    winreg.CloseKey(key)
    return changed


def _unix_rc_files() -> list:
    home = os.path.expanduser("~")
    shell = os.environ.get("SHELL", "")
    files = []
    if "zsh" in shell:
        files.append(os.path.join(home, ".zshrc"))
    if "bash" in shell:
        files.append(os.path.join(home, ".bashrc"))
    files.append(os.path.join(home, ".profile"))
    seen = []
    for f in files:
        if f not in seen:
            seen.append(f)
    return seen


def _unix_add_path(dirpath: str) -> list:
    line = f'export PATH="{dirpath}:$PATH"'
    touched = []
    for f in _unix_rc_files():
        content = ""
        if os.path.exists(f):
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except Exception:
                content = ""
        if dirpath in content:
            continue
        try:
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(f"\n# added by {APP}\n{line}\n")
            touched.append(f)
        except Exception:
            pass
    return touched


def _unix_remove_path(dirpath: str) -> list:
    touched = []
    for f in _unix_rc_files():
        if not os.path.exists(f):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except Exception:
            continue
        kept = [ln for ln in lines
                if f"# added by {APP}" not in ln and dirpath not in ln]
        if len(kept) != len(lines):
            try:
                with open(f, "w", encoding="utf-8") as fh:
                    fh.writelines(kept)
                touched.append(f)
            except Exception:
                pass
    return touched


def _confirm(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return True
    try:
        ans = input(col(prompt, "bold"))
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans.strip().lower() in ("y", "yes")


def _is_installed() -> bool:
    return (os.path.exists(os.path.join(install_dir(), "netspeed.py"))
            or os.path.exists(_launcher_path()))


def cmd_install(cfg: dict, args) -> None:
    if _is_installed():
        print(t(f"偵測到已安裝於: {install_dir()}", f"Already installed at: {install_dir()}"))
        if not _confirm(t("是否重新安裝 (覆蓋)? [y/N]: ", "Reinstall (overwrite)? [y/N]: "),
                        getattr(args, "yes", False)):
            info(t("已取消安裝。", "Install cancelled."))
            return
    src = os.path.abspath(__file__)
    idir = install_dir()
    ensure_dir(idir)
    dst = os.path.join(idir, "netspeed.py")
    if os.path.abspath(dst) != src:
        shutil.copy2(src, dst)

    ldir = bin_dir()
    ensure_dir(ldir)
    launcher = _launcher_path()
    python = _python_exe()
    if os.name == "nt":
        content = f'@echo off\r\n"{python}" "{dst}" %*\r\n'
    else:
        content = f'#!/usr/bin/env bash\nexec "{python}" "{dst}" "$@"\n'
    with open(launcher, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    if os.name != "nt":
        os.chmod(launcher, 0o755)

    if getattr(args, "no_path", False):
        path_note = t("已略過 (--no-path)", "skipped (--no-path)")
    elif os.name == "nt":
        added = _win_add_path(ldir)
        path_note = t("已加入使用者 PATH", "added to user PATH") if added \
            else t("PATH 已包含此目錄", "PATH already contains it")
    else:
        touched = _unix_add_path(ldir)
        path_note = (t("已更新 ", "updated ") + ", ".join(touched)) if touched \
            else t("PATH 設定已存在", "PATH already set")

    section(t("安裝完成", "Install complete"))
    print(t(f"  程式      : {dst}", f"  Program   : {dst}"))
    print(t(f"  啟動器    : {launcher}", f"  Launcher  : {launcher}"))
    print(t(f"  PATH      : {path_note}", f"  PATH      : {path_note}"))
    print(t(f"  設定/歷史 : {config_dir()}", f"  Config    : {config_dir()}"))
    print()
    if os.name == "nt":
        print(t("請重開終端機後即可使用: ", "Reopen terminal to use: ") + col("netspeed all", "bold"))
    else:
        print(t(f"重開終端機或執行 source 後即可使用: {col('netspeed all', 'bold')}",
                f"Reopen terminal or source to use: {col('netspeed all', 'bold')}"))
        print(t(f"（也可直接: {col(f'{python} {dst} all', 'bold')}）",
                f"(or directly: {col(f'{python} {dst} all', 'bold')})"))


def cmd_uninstall(cfg: dict, args) -> None:
    if _is_installed():
        if not _confirm(t("確定要移除已安裝的 netspeed? [y/N]: ", "Uninstall netspeed? [y/N]: "),
                        getattr(args, "yes", False)):
            info(t("已取消移除。", "Uninstall cancelled."))
            return
    elif not args.purge:
        info(t("找不到已安裝的檔案 (可能尚未安裝)。", "No installed files found (maybe not installed)."))
        return

    removed = []
    for p in (os.path.join(install_dir(), "netspeed.py"), _launcher_path()):
        if os.path.exists(p):
            try:
                os.remove(p)
                removed.append(p)
            except OSError:
                pass
    for d in {install_dir(), bin_dir()}:
        try:
            os.rmdir(d)
        except OSError:
            pass

    if not getattr(args, "no_path", False):
        if os.name == "nt":
            _win_remove_path(bin_dir())
        else:
            _unix_remove_path(bin_dir())

    section(t("移除完成", "Uninstall complete"))
    if removed:
        for p in removed:
            print(t(f"  已刪除: {p}", f"  removed: {p}"))
    else:
        print(t("  找不到已安裝的檔案 (可能尚未安裝)", "  no installed files found (maybe not installed)"))
    if args.purge:
        shutil.rmtree(config_dir(), ignore_errors=True)
        print(t(f"  已清除設定與歷史: {config_dir()}", f"  config & history removed: {config_dir()}"))
    else:
        print(t(f"  設定與歷史保留於: {config_dir()}  (加 --purge 可一併刪除)",
                f"  config & history kept at: {config_dir()}  (use --purge to remove)"))


# --------------------------------------------------------------------------- #
# 說明 / 互動
# --------------------------------------------------------------------------- #

def help_sections() -> list:
    return [
    (t("全部 (all)", "All"), [
        ("all", t("外網 IP + ping + 測速 統整報告", "public IP + ping + speed summary report")),
    ]),
    (t("測速 (speed)", "Speed"), [
        ("speed all", t("全部測速來源 (不含 ip / ping)", "all speed sources (no ip / ping)")),
        ("speed mlab|fast|cloudflare|ookla|libre", t("單一來源測速", "single source")),
        ("speed check", t("檢查測速工具, 可協助下載缺少的執行檔", "check speed tools, offer to download missing ones")),
        ("", t("例: speed cloudflare --seconds 15    speed fast --upload",
               "e.g. speed cloudflare --seconds 15    speed fast --upload")),
    ]),
    ("Ping", [
        ("ping [targets...]", t("對目標 ping (預設含 default gateway + 清單)",
                                "ping targets (includes default gateway + list)")),
        ("", t("選項: -c/--count  -i/--interval  -W/--timeout  -s/--size  --method auto|icmp|system  --no-gateway",
               "opts: -c/--count  -i/--interval  -W/--timeout  -s/--size  --method auto|icmp|system  --no-gateway")),
    ]),
    (t("外網 IP", "Public IP"), [
        ("ip [-p provider]", t("查外網 IP 與電信商 (預設 ipinfo.io)",
                               "public IP & ISP (default ipinfo.io)")),
    ]),
    (t("顯示資訊 (show)", "Show"), [
        ("show ping", t("Ping 設定 (含目前閘道值)", "ping settings (with current gateway)")),
        ("show tools", t("工具狀態 (* = all 會執行; 分 ping / 測速)",
                         "tool status (* = runs under all; ping / speed)")),
        ("show speed", t("測速工具檢查 (等同 speed check)", "speed tool check (same as speed check)")),
        ("show env", t("環境狀態檢查 (Python / 設定目錄 / 網路)",
                       "environment check (Python / config dir / internet)")),
        ("show debug [enable|disable]", t("除錯模式: 輸出指令與原始結果到 stderr",
                                          "debug mode: print commands & raw output to stderr")),
        ("show interfaces", t("本機網路設定: DHCP/IP/閘道/Link Speed/路由",
                              "local network: DHCP/IP/gateway/link speed/routes")),
        ("show config|theme|history|version", t("各項目前狀態", "current state")),
        ("netinfo [iface] [--all] [-i] [--refresh]", t("等同 show interfaces", "same as show interfaces")),
    ]),
    (t("對測 (pair)", "Pair"), [
        ("pair serve", t("開對測伺服器 (優先 iperf3, 否則內建; 支援 TCP/UDP)",
                         "run pair server (iperf3 or builtin; TCP/UDP)")),
        ("pair client <host>", t("連到對測端 (--mode both|down|up --duration --streams --udp -b 100M)",
                                 "connect to peer (--mode both|down|up --duration --streams --udp -b 100M)")),
        ("pair history", t("看對測歷史", "pair history")),
    ]),
    (t("設定 (config)", "Config"), [
        ("config show|path|edit|reset|export|import", t("設定檔 (可匯出/匯入分享)",
                                                        "config file (export/import)")),
        ("config set <key> <value>", t("例: config set iperf.path /usr/local/bin/iperf3",
                                       "e.g. config set iperf.path /usr/local/bin/iperf3")),
        ("config get <key>", t("讀取設定值", "read a config value")),
        ("config lang en|zh-TW", t("切換語系 (預設 en)", "switch language (default en)")),
        ("config targets [list|add|del|set|gateway]", t("ping 目標清單", "ping target list")),
        ("config endpoints [list|set <來源> <值>]", t("測速端點設定", "speed endpoint settings")),
        ("config theme [list|set|reset|preset|export|import]", t("顏色主題", "color theme")),
        ("history", t("歷史紀錄, 可輸入編號查看內容並重跑",
                      "history; enter a number to view details and rerun")),
    ]),
    (t("互動模式 (Cisco 風格)", "Interactive (Cisco style)"), [
        ("console", t("進入互動模式 (shell 亦可; 不帶參數也會倒數後跑預設)",
                      "interactive mode (alias shell; no-arg runs default after countdown)")),
        ("show / configure / write / end", t("console 內: show <x>, configure 進設定模式, write 存檔",
                                             "in console: show <x>, configure, write to save")),
        ("refresh", t("console 內: 清除本次快取 (netinfo / check / env / ip)",
                      "in console: clear cache (netinfo / check / env / ip)")),
    ]),
    (t("版本 / 安裝", "Version / Install"), [
        ("update [--reset-skip]", t("檢查/更新到最新版本 (來源: update.repo)",
                                    "check/update to latest (update.repo)")),
        ("version", t("顯示版本", "show version")),
        ("install [-y] | uninstall [--purge] [-y]", t("安裝 / 移除", "install / uninstall")),
    ]),
]

COMMAND_ORDER = [
    "speed", "all", "ping", "show", "pair", "config", "netinfo", "check", "ip",
    "update", "console", "install", "uninstall", "help", "refresh", "exit",
    "configure", "write", "no", "end",
]

def command_help() -> dict:
    return {
    "speed": t("測速 (all/mlab/fast/cloudflare/ookla/libre)", "speed (all/mlab/fast/cloudflare/ookla/libre)"),
    "all": t("外網 IP + ping + 測速 統整報告", "public IP + ping + speed summary"),
    "mlab": t("M-Lab NDT7 測速", "M-Lab NDT7"),
    "fast": t("fast.com (Netflix) 測速", "fast.com (Netflix)"),
    "cloudflare": t("Cloudflare 測速 (內建)", "Cloudflare (builtin)"),
    "ookla": t("Ookla speedtest.net 測速", "Ookla speedtest.net"),
    "libre": t("LibreSpeed 測速", "LibreSpeed"),
    "ping": t("對目標 ping", "ping targets"),
    "ip": t("查外網 IP 與電信商", "public IP & ISP"),
    "check": t("檢查各來源工具是否可用", "check tool availability"),
    "netinfo": t("顯示本機網路設定", "local network settings"),
    "pair": t("對測 (serve / client / history)", "pair (serve / client / history)"),
    "history": t("歷史紀錄, 可選取查看/重跑", "history; view/rerun"),
    "config": t("設定檔 (show/path/edit/reset)", "config (show/path/edit/reset)"),
    "set": t("設定值 set <key> <value>", "set <key> <value>"),
    "get": t("讀取設定值 get <key>", "get <key>"),
    "targets": t("ping 目標清單管理", "ping target list"),
    "endpoints": t("測速端點設定", "speed endpoints"),
    "theme": t("顏色主題 (可自訂/匯出/匯入)", "color theme"),
    "update": t("檢查 / 更新版本", "check / update version"),
    "version": t("顯示版本", "show version"),
    "console": t("進入互動模式 (shell 亦可)", "interactive mode (alias shell)"),
    "install": t("安裝並加入 PATH", "install & add to PATH"),
    "uninstall": t("移除程式", "uninstall"),
    "help": t("顯示完整說明", "show full help"),
    "refresh": t("清除本次快取 (netinfo / check / env / ip)", "clear cache (netinfo / check / env / ip)"),
    "exit": t("離開 console", "quit console"),
    "show": t("Cisco 風格: show <項目> (ping/tools/env/...)",
              "Cisco style: show <topic> (ping/tools/env/...)"),
    "debug": t("除錯模式: show debug enable|disable", "debug mode: show debug enable|disable"),
    "configure": t("進入設定模式 (configure / conf)", "enter config mode (configure / conf)"),
    "conf": t("configure 的縮寫", "alias of configure"),
    "write": t("設定模式: 儲存設定到檔案 (write / save)", "config mode: save (write / save)"),
    "save": t("write 的別名", "alias of write"),
    "no": t("設定模式: 還原某項為預設 (no <key>)", "config mode: reset a key (no <key>)"),
    "end": t("設定模式: 離開 (end / exit)", "config mode: leave (end / exit)"),
}


def print_help() -> None:
    print(tcol(t("netspeed — 跨平台 CLI 網路測速工具箱", "netspeed — cross-platform CLI network speed toolkit"),
               "title") + "  " + tcol(f"v{VERSION}", "dim"))
    print()
    print(t("用法: ", "Usage: ") + col("netspeed <command> [args...]", "bold"))
    if _IN_CONSOLE:
        print("      " + t("(互動模式中; 輸入 exit 離開, help 看指令)",
                           "(interactive mode; type exit to quit, help for commands)"))
    else:
        print("      " + col("netspeed", "bold")
              + t("  (不帶參數 → 倒數後跑預設; console 進互動模式; exit 離開)",
                  "  (no args → run default after countdown; console for interactive; exit to quit)"))
    for title, items in help_sections():
        if _IN_CONSOLE:
            items = [(n, d) for (n, d) in items if not n.lower().startswith(("console", "shell"))]
            if not items:
                continue
        print()
        print(tcol(f"【{title}】" if LANG != "en" else f"[{title}]", "heading"))
        for name, desc in items:
            if name:
                print("  " + tcol(f"{name:<34}", "command") + desc)
            else:
                print("  " + " " * 34 + tcol(desc, "dim"))
    print()
    print(tcol("【通用選項】" if LANG != "en" else "[Common options]", "heading"))
    for name, desc in [("--json", t("以 JSON 輸出", "JSON output")),
                       ("--no-color", t("關閉顏色", "disable color")),
                       ("--debug", t("輸出除錯訊息到 stderr", "print debug info to stderr")),
                       ("--config <path>", t("指定設定檔", "custom config path")),
                       ("--no-save", t("不寫入歷史紀錄", "do not save history")),
                       ("--no-update-check", t("本次不檢查更新", "skip update check"))]:
        print("  " + tcol(f"{name:<34}", "command") + desc)
    print()
    print(tcol(t("語系: config set lang en|zh-TW  (預設 en)",
                 "Language: config set lang en|zh-TW  (default en)"), "dim"))


# --------------------------------------------------------------------------- #
# 參數解析
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="以 JSON 輸出")
    common.add_argument("--no-color", action="store_true", help="關閉顏色")
    common.add_argument("--config", metavar="PATH", help="指定設定檔路徑")
    common.add_argument("--no-save", action="store_true", help="不寫入歷史紀錄")
    common.add_argument("--no-update-check", action="store_true", help="本次不檢查更新")
    common.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                        help="輸出除錯訊息到 stderr")

    p = argparse.ArgumentParser(prog="netspeed", description="跨平台 CLI 網路測速工具箱")
    p.add_argument("--no-color", action="store_true", help="關閉顏色")
    p.add_argument("--config", metavar="PATH", help="指定設定檔路徑")
    p.add_argument("--debug", action="store_true", help="輸出除錯訊息到 stderr")
    p.add_argument("--version", action="version", version=f"netspeed {VERSION}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    # 測速分層: speed <all|mlab|fast|cloudflare|ookla|libre>
    speed = sub.add_parser("speed", help="測速 (all / mlab / fast / cloudflare / ookla / libre)")
    speed_sub = speed.add_subparsers(dest="speed_cmd")
    s_all = speed_sub.add_parser("all", parents=[common], help="全部測速來源 (不含 ip / ping)")
    s_all.add_argument("--sources", help="要跑的來源, 逗號分隔")
    s_all.add_argument("--no-ping", action="store_true", help=argparse.SUPPRESS)
    s_all.add_argument("--no-ip", action="store_true", help=argparse.SUPPRESS)
    s_all.add_argument("--seconds", type=float, default=None)
    for name, hlp in [
        ("mlab", "M-Lab NDT7"), ("fast", "fast.com (Netflix)"),
        ("cloudflare", "Cloudflare"), ("ookla", "Ookla speedtest.net"),
        ("libre", "LibreSpeed"),
    ]:
        s = speed_sub.add_parser(name, parents=[common], help=hlp)
        s.add_argument("args", nargs="*", help="透傳參數")
        if name == "fast":
            s.add_argument("--upload", action="store_true")
        if name == "cloudflare":
            s.add_argument("--seconds", type=float, default=None)
    s_ck = speed_sub.add_parser("check", parents=[common],
                                help="檢查測速工具, 並可協助下載缺少的執行檔")
    s_ck.add_argument("--refresh", action="store_true", help="重新檢查 (不讀快取)")
    s_ck.add_argument("-y", "--yes", action="store_true", help="不詢問, 直接嘗試下載")

    al = sub.add_parser("all", parents=[common], help="全部: 外網 IP + ping + 測速 (統整報告)")
    al.add_argument("--sources", help="要跑的來源, 逗號分隔")
    al.add_argument("--no-ping", action="store_true", help="略過 ping")
    al.add_argument("--no-ip", action="store_true", help="略過外網 IP")
    al.add_argument("--seconds", type=float, default=None)

    pp = sub.add_parser("ping", parents=[common], help="對目標 ping")
    pp.add_argument("targets", nargs="*")
    pp.add_argument("-c", "--count", type=int)
    pp.add_argument("-i", "--interval", type=float)
    pp.add_argument("-W", "--timeout", type=float)
    pp.add_argument("-s", "--size", type=int)
    pp.add_argument("--method", choices=["auto", "icmp", "system"])
    pp.add_argument("--no-gateway", action="store_true")

    ip = sub.add_parser("ip", parents=[common], help="查外網 IP 與電信商")
    ip.add_argument("--refresh", action="store_true", help="重新查詢 (不讀快取)")
    ip.add_argument("-p", "--provider", choices=list(IP_PROVIDERS),
                    help="IP 查詢來源 (預設 ipinfo.io)")

    ck = sub.add_parser("check", parents=[common], help="檢查各來源工具是否可用")
    ck.add_argument("--refresh", action="store_true", help="重新檢查 (不讀快取)")

    ni = sub.add_parser("netinfo", parents=[common], help="顯示本機網路設定")
    ni.add_argument("interface", nargs="?", help="指定要顯示的介面名稱")
    ni.add_argument("--all", action="store_true", help="顯示所有介面")
    ni.add_argument("-i", "--select", action="store_true", help="互動選單選擇介面")
    ni.add_argument("--refresh", action="store_true", help="重新讀取 (不讀快取)")

    pair = sub.add_parser("pair", help="對測 (iperf3 或內建)")
    pair_sub = pair.add_subparsers(dest="pair_cmd")
    ps = pair_sub.add_parser("serve", parents=[common], help="開對測伺服器")
    ps.add_argument("--host", default="0.0.0.0")
    ps.add_argument("--port", type=int)
    ps.add_argument("--method", choices=["auto", "iperf3", "builtin"], default="auto")
    ps.add_argument("--iperf-path")
    ps.add_argument("--duration", type=int)
    ps.add_argument("--udp", action="store_true", help="UDP 對測 (iperf3 自動偵測; 內建支援 UDP)")
    pc = pair_sub.add_parser("client", parents=[common], help="連到對測端")
    pc.add_argument("host")
    pc.add_argument("--port", type=int)
    pc.add_argument("--method", choices=["auto", "iperf3", "builtin"], default="auto")
    pc.add_argument("--mode", choices=["both", "down", "up"], default="both")
    pc.add_argument("--duration", type=int)
    pc.add_argument("--streams", type=int)
    pc.add_argument("--iperf-path")
    pc.add_argument("--udp", action="store_true", help="使用 UDP 測試 (需 iperf3 或內建)")
    pc.add_argument("-b", "--bandwidth", help="UDP 目標頻寬 (iperf3 -b, 例 100M / 0=不限)")
    pair_sub.add_parser("history", parents=[common], help="對測歷史")

    ph = sub.add_parser("history", parents=[common], help="歷史紀錄")
    ph.add_argument("--kind", choices=["speed", "ping", "pair", "ip"])
    ph.add_argument("--limit", type=int, default=20)
    ph.add_argument("--run", type=int)

    # 設定分層: config <show|set|get|targets|endpoints|theme|path|edit|reset|export|import>
    cf = sub.add_parser("config", parents=[common], help="設定 (show/set/get/targets/endpoints/theme/...)")
    cf_sub = cf.add_subparsers(dest="cfg_cmd")
    for nm in ("show", "path", "edit", "reset"):
        cf_sub.add_parser(nm, parents=[common])
    _cfe = cf_sub.add_parser("export", parents=[common]); _cfe.add_argument("path", nargs="?")
    _cfi = cf_sub.add_parser("import", parents=[common]); _cfi.add_argument("path")
    _cfs = cf_sub.add_parser("set", parents=[common]); _cfs.add_argument("key"); _cfs.add_argument("value")
    _cfg = cf_sub.add_parser("get", parents=[common]); _cfg.add_argument("key")
    _cft = cf_sub.add_parser("targets", parents=[common])
    _cft.add_argument("action", nargs="?", default="list", choices=["list", "add", "del", "set", "gateway"])
    _cft.add_argument("values", nargs="*")
    _cfn = cf_sub.add_parser("endpoints", parents=[common])
    _cfn.add_argument("action", nargs="?", default="list", choices=["list", "set"])
    _cfn.add_argument("values", nargs="*")
    _cfh = cf_sub.add_parser("theme", parents=[common])
    _cfh.add_argument("action", nargs="?", default="list",
                      choices=["list", "set", "reset", "preset", "export", "import"])
    _cfh.add_argument("values", nargs="*")
    _cfl = cf_sub.add_parser("lang", parents=[common], help="語系 (en / zh-TW)")
    _cfl.add_argument("value", nargs="?")

    # 平鋪設定指令 (隱藏, 仍可用)
    th = sub.add_parser("theme", parents=[common], help=argparse.SUPPRESS)
    th.add_argument("action", nargs="?", default="list",
                    choices=["list", "set", "reset", "preset", "export", "import"])
    th.add_argument("values", nargs="*")

    st = sub.add_parser("set", parents=[common], help=argparse.SUPPRESS)
    st.add_argument("key")
    st.add_argument("value")

    gt = sub.add_parser("get", parents=[common], help=argparse.SUPPRESS)
    gt.add_argument("key")

    pt = sub.add_parser("targets", parents=[common], help=argparse.SUPPRESS)
    pt.add_argument("action", nargs="?", default="list",
                    choices=["list", "add", "del", "set", "gateway"])
    pt.add_argument("values", nargs="*")

    pe = sub.add_parser("endpoints", parents=[common], help=argparse.SUPPRESS)
    pe.add_argument("action", nargs="?", default="list", choices=["list", "set"])
    pe.add_argument("values", nargs="*")

    ins = sub.add_parser("install", parents=[common], help="安裝並加入 PATH")
    ins.add_argument("--no-path", action="store_true", help="不安裝 PATH (自行管理)")
    ins.add_argument("-y", "--yes", action="store_true", help="不詢問, 直接重新安裝")
    un = sub.add_parser("uninstall", parents=[common], help="移除程式")
    un.add_argument("--purge", action="store_true", help="同時刪除設定與歷史紀錄")
    un.add_argument("--no-path", action="store_true", help="不動 PATH 設定")
    un.add_argument("-y", "--yes", action="store_true", help="不詢問, 直接移除")

    up = sub.add_parser("update", parents=[common], help="檢查/更新到最新版本")
    up.add_argument("--reset-skip", action="store_true", help="清除『不再通知』的版本設定")

    sub.add_parser("version", parents=[common], help="顯示版本")

    for _nm in ("show-ping", "show-tools", "show-env"):
        _sp = sub.add_parser(_nm, parents=[common], help=argparse.SUPPRESS)
        if _nm in ("show-tools", "show-env"):
            _sp.add_argument("--refresh", action="store_true")

    _sd = sub.add_parser("show-debug", parents=[common], help=argparse.SUPPRESS)
    _sd.add_argument("action", nargs="?", default="status")

    sub.add_parser("console", parents=[common], aliases=["shell"], help="進入互動模式")

    sub.add_parser("help", help="顯示完整說明")

    # Python 3.13 的 argparse 對 subparser 的 help=SUPPRESS 無效, 手動移除隱藏項
    for action in list(getattr(sub, "_choices_actions", [])):
        if action.help == argparse.SUPPRESS:
            sub._choices_actions.remove(action)

    return p


def _dispatch_config(cfg: dict, args) -> None:
    sub = getattr(args, "cfg_cmd", None) or "show"
    ns = dict(json=getattr(args, "json", False), no_save=getattr(args, "no_save", False),
              config=getattr(args, "config", None), no_color=getattr(args, "no_color", False))
    if sub in ("show", "path", "edit", "reset"):
        cmd_config(cfg, argparse.Namespace(action=sub, values=[], **ns))
    elif sub == "export":
        path = getattr(args, "path", None)
        cmd_config(cfg, argparse.Namespace(action="export", values=([path] if path else []), **ns))
    elif sub == "import":
        cmd_config(cfg, argparse.Namespace(action="import", values=[args.path], **ns))
    elif sub == "set":
        set_dotted(cfg, args.key, coerce(args.value))
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(t(f"已設定 {args.key} = {get_dotted(cfg, args.key)}",
             f"Set {args.key} = {get_dotted(cfg, args.key)}"))
    elif sub == "get":
        print(json.dumps(get_dotted(cfg, args.key), ensure_ascii=False))
    elif sub == "targets":
        cmd_targets(cfg, argparse.Namespace(action=args.action, values=args.values))
    elif sub == "endpoints":
        cmd_endpoints(cfg, argparse.Namespace(action=args.action, values=args.values))
    elif sub == "theme":
        cmd_theme(cfg, argparse.Namespace(action=args.action, values=args.values, **ns))
    elif sub == "lang":
        if getattr(args, "value", None):
            set_dotted(cfg, "lang", args.value)
            save_config(cfg, getattr(args, "config", None))
            apply_theme(cfg)
            ok(t(f"已切換語系: {LANG} ({LANG_NAMES.get(LANG, LANG)})",
                 f"Language set: {LANG} ({LANG_NAMES.get(LANG, LANG)})"))
        else:
            print(f"{LANG} ({LANG_NAMES.get(LANG, LANG)})")
    else:
        warn(t(f"未知 config 子指令: {sub}", f"Unknown config subcommand: {sub}"))


def _dispatch(args, cfg: dict, parser) -> None:
    cmd = args.cmd
    if cmd == "all":
        cmd_all(cfg, args)
    elif cmd in ("mlab", "fast", "cloudflare", "ookla", "libre"):
        cmd_source(cmd, cfg, args)
    elif cmd == "speed":
        sc = getattr(args, "speed_cmd", None)
        if sc == "all":
            cmd_speed_all(cfg, args)
        elif sc in ("mlab", "fast", "cloudflare", "ookla", "libre"):
            cmd_source(sc, cfg, args)
        elif sc in ("check", "tools"):
            cmd_speed_check(cfg, args)
        else:
            parser.parse_args(["speed", "--help"])
    elif cmd == "ping":
        cmd_ping(cfg, args)
    elif cmd == "ip":
        cmd_ip(cfg, args)
    elif cmd == "check":
        cmd_check(cfg, args)
    elif cmd == "show-ping":
        cmd_show_ping(cfg, args)
    elif cmd == "show-tools":
        cmd_show_tools(cfg, args)
    elif cmd == "show-env":
        cmd_show_env(cfg, args)
    elif cmd == "show-debug":
        cmd_show_debug(cfg, args)
    elif cmd == "netinfo":
        cmd_netinfo(cfg, args)
    elif cmd == "pair":
        if args.pair_cmd == "serve":
            cmd_pair_serve(cfg, args)
        elif args.pair_cmd == "client":
            cmd_pair_client(cfg, args)
        elif args.pair_cmd == "history":
            args.kind = "pair"
            cmd_history(cfg, args)
        else:
            parser.parse_args(["pair", "--help"])
    elif cmd == "history":
        cmd_history(cfg, args)
    elif cmd == "config":
        _dispatch_config(cfg, args)
    elif cmd == "set":
        set_dotted(cfg, args.key, coerce(args.value))
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(t(f"已設定 {args.key} = {get_dotted(cfg, args.key)}",
             f"Set {args.key} = {get_dotted(cfg, args.key)}"))
    elif cmd == "get":
        print(json.dumps(get_dotted(cfg, args.key), ensure_ascii=False))
    elif cmd == "targets":
        cmd_targets(cfg, args)
    elif cmd == "endpoints":
        cmd_endpoints(cfg, args)
    elif cmd == "theme":
        cmd_theme(cfg, args)
    elif cmd == "refresh":
        cache_clear()
        ok(t("已清除本次快取 (netinfo / check / env / ip 將重新讀取)",
             "Cache cleared (netinfo / check / env / ip will reload)"))
    elif cmd == "install":
        cmd_install(cfg, args)
    elif cmd == "uninstall":
        cmd_uninstall(cfg, args)
    elif cmd == "update":
        cmd_update(cfg, args)
    elif cmd == "version":
        print(f"netspeed {VERSION}")
    elif cmd in ("console", "shell"):
        console()
    elif cmd == "help":
        print_help()
    else:
        parser.print_help()


def run_command(argv, cfg: dict | None = None) -> int:
    global USE_COLOR, DEBUG
    argv = list(argv)
    if argv and argv[0].lower() in ALIAS_MAP:
        argv = ALIAS_MAP[argv[0].lower()] + argv[1:]
    if argv and argv[0].lower() == "show":
        rest = argv[1:]
        if rest and rest[0].lower() == "debug":
            argv = ["show-debug"] + rest[1:]
        else:
            idx = 0
            while idx < len(rest) and not rest[idx].startswith("-"):
                idx += 1
            topic_tokens, flags = rest[:idx], rest[idx:]
            mapped = _resolve_show(topic_tokens)
            if mapped is None:
                _print_show_topics()
                return 0
            argv = mapped + flags
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    if getattr(args, "no_color", False) or not sys.stdout.isatty():
        USE_COLOR = False

    if args.cmd is None:
        parser.print_help()
        return 0

    if cfg is None:
        cfg = load_config(getattr(args, "config", None))
    apply_theme(cfg)

    if getattr(args, "debug", False) or _debug_enabled(cfg):
        DEBUG = True
    dbg("cmd", "netspeed " + cmd_str(argv))
    dbg("cmd", f"config={config_path()}  lang={LANG}  color={USE_COLOR}")

    if not getattr(args, "json", False) and args.cmd not in ("help", "version") and not _IN_CONSOLE:
        print(tcol(f"netspeed v{VERSION}", "title"))

    if (not getattr(args, "no_update_check", False) and not getattr(args, "json", False)
            and args.cmd not in ("help", "version", "update")):
        try:
            check_update(cfg, interactive=sys.stdin.isatty())
        except Exception:
            pass

    try:
        _dispatch(args, cfg, parser)
    except KeyboardInterrupt:
        print("\n已中止。")
        return 130
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    return 0


COMMANDS = {
    "all", "mlab", "fast", "cloudflare", "ookla", "libre", "ping", "ip",
    "pair", "history", "config", "set", "get", "targets", "endpoints", "theme",
    "check", "netinfo", "update", "version", "install", "uninstall",
    "console", "shell", "help", "refresh", "speed",
    "show-ping", "show-tools", "show-env", "show-debug",
    # Cisco 風格 (由 console / run_command 前置處理)
    "show", "configure", "conf", "write", "save", "no", "end",
}

# Cisco 風格: show <x> 對應到既有指令
SHOW_MAP = {
    "config": ["config"],
    "ping": ["show-ping"],
    "interfaces": ["netinfo"],
    "tools": ["show-tools"],
    "env": ["show-env"],
    "debug": ["show-debug"],
    "speed": ["speed", "check"],
    "theme": ["theme"],
    "history": ["history"],
    "version": ["version"],
}

# 平鋪別名 -> 分層指令 (仍可用, 但不列在 help)
ALIAS_MAP = {
    "mlab": ["speed", "mlab"],
    "fast": ["speed", "fast"],
    "cloudflare": ["speed", "cloudflare"],
    "ookla": ["speed", "ookla"],
    "libre": ["speed", "libre"],
}


def _resolve_show(tokens: list):
    key = " ".join(tokens).lower().strip()
    if not key:
        return None
    matches = [k for k in SHOW_MAP if k.startswith(key)]
    if len(matches) == 1:
        return list(SHOW_MAP[matches[0]])
    return None


def _print_show_topics() -> None:
    print(tcol(t("可用的 show 項目:", "Available show topics:"), "heading"))
    for k in SHOW_MAP:
        print("  " + tcol(f"show {k:<18}", "command") + "→ " + " ".join(SHOW_MAP[k]))


# --------------------------------------------------------------------------- #
# console 輸入: ? 顯示候選, Tab 補齊, Enter 執行
# --------------------------------------------------------------------------- #


ARG_CANDIDATES = {
    "pair": ["serve", "client", "history"],
    "targets": ["list", "add", "del", "set", "gateway"],
    "endpoints": ["list", "set"],
    "config": ["show", "set", "get", "targets", "endpoints", "theme", "lang",
               "path", "edit", "reset", "export", "import"],
    "theme": ["list", "set", "reset", "preset", "export", "import"],
    "history": ["--kind", "--limit", "--run"],
    "update": ["--reset-skip"],
    "all": ["--sources", "--no-ping", "--no-ip", "--seconds"],
    "fast": ["--upload"],
    "cloudflare": ["--seconds"],
    "check": ["--refresh", "--json"],
    "ip": ["--refresh", "-p", "--provider"],
    "install": ["--no-path", "-y"],
    "uninstall": ["--purge", "--no-path", "-y"],
    "show": list(SHOW_MAP.keys()),
    "show-debug": ["enable", "disable", "status"],
    "speed": ["all", "mlab", "fast", "cloudflare", "ookla", "libre", "check"],
}

PAIR_OPTS = {
    "serve": ["--host", "--port", "--method", "--iperf-path", "--duration", "--udp"],
    "client": ["--port", "--method", "--mode", "--duration", "--streams", "--iperf-path", "--udp", "-b", "--bandwidth"],
    "history": [],
}

def arg_help() -> dict:
    return {
    "serve": "開對測伺服器", "client": "連到對測端", "history": "對測歷史",
    "list": "列出", "add": "新增", "del": "刪除", "set": "設定", "gateway": "顯示 default gateway",
    "show": "顯示設定", "path": "顯示設定檔路徑", "edit": "用編輯器開啟", "reset": "重設為預設",
    "export": "匯出成檔案", "import": "從檔案匯入", "preset": "套用主題預設組",
    "lang": "語系 (en / zh-TW)",
    "all": "全部測速來源", "check": "檢查測速工具並可協助下載", "tools": "檢查測速工具",
    "mlab": "M-Lab", "fast": "fast.com", "cloudflare": "Cloudflare", "ookla": "Ookla", "libre": "LibreSpeed",
    "ping": "Ping 設定", "env": "環境狀態檢查", "interfaces": "本機網路設定",
    "debug": "除錯模式", "enable": "開啟", "disable": "關閉", "status": "顯示狀態",
    "--all": "顯示所有介面", "--refresh": "重新讀取 (不讀快取)", "--json": "JSON 輸出",
    "--sources": "指定來源", "--no-ping": "略過 ping", "--no-ip": "略過外網 IP",
    "--seconds": "秒數", "--upload": "加測上傳", "--kind": "篩選類型", "--limit": "筆數",
    "--run": "直接重跑編號", "--no-path": "不動 PATH", "-y": "不詢問",
    "-p": "IP 查詢來源", "--provider": "IP 查詢來源 (ipinfo.io/ifconfig.co/ip-api.com)",
    "--udp": "UDP 測試", "-b": "UDP 頻寬", "--bandwidth": "UDP 目標頻寬 (例 100M, 0=不限)",
    "--purge": "同時刪除設定", "--reset-skip": "清除不再通知版本",
}

_CONSOLE_CFG: dict | None = None
_IN_CONSOLE = False


def _config_keys() -> list:
    keys: list = []

    def walk(prefix, node):
        for k, v in node.items():
            path = f"{prefix}{k}"
            if isinstance(v, dict):
                walk(path + ".", v)
            else:
                keys.append(path)

    walk("", DEFAULT_CONFIG)
    return sorted(keys)


def _netinfo_ifaces() -> list:
    try:
        data = collect_netinfo()
        return [i["name"] for i in _real_ifaces(data)]
    except Exception:
        return []


def _ping_candidates() -> list:
    if not _CONSOLE_CFG:
        return []
    try:
        return resolve_ping_targets(_CONSOLE_CFG, [], use_gateway=True)
    except Exception:
        return []


def _arg_candidates(cmd: str, completed: list) -> list:
    cmd = cmd.lower()
    if cmd == "netinfo":
        opts = ["--all", "--select", "--refresh", "--json"]
        if not completed or completed[0].startswith("-"):
            return _netinfo_ifaces() + opts
        return opts
    if cmd == "pair":
        if not completed:
            return ARG_CANDIDATES["pair"]
        return PAIR_OPTS.get(completed[0], [])
    if cmd in ("set", "get", "no"):
        return _config_keys()
    if cmd == "show":
        if completed and completed[0].lower() == "debug":
            return ["enable", "disable", "status"]
        return list(SHOW_MAP.keys())
    if cmd == "ping":
        return _ping_candidates()
    return ARG_CANDIDATES.get(cmd, [])


def _command_pool() -> list:
    """console 內可用指令; 已在 console 時排除 console/shell。"""
    if _IN_CONSOLE:
        return [c for c in COMMAND_ORDER if c not in ("console", "shell")]
    return list(COMMAND_ORDER)


def _candidates_for(buffer: str) -> list:
    if " " not in buffer:
        t = buffer.lower()
        return [c for c in _command_pool() if c.startswith(t)]
    parts = buffer.split(" ")
    cmd = parts[0].lower()
    if buffer.endswith(" "):
        partial = ""
        completed = parts[1:-1]
    else:
        partial = parts[-1]
        completed = parts[1:-1]
    return [c for c in _arg_candidates(cmd, completed) if c.startswith(partial)]


def _complete_candidates(text: str) -> list:
    return _candidates_for(text)


def _expand_prefix(token: str, in_config: bool = False) -> str:
    """指令前綴唯一符合時自動補齊 (in -> install, wr -> write)。"""
    low = token.lower()
    if not low:
        return token
    if in_config:
        pool = ["set", "no", "write", "save", "end", "exit", "show", "help",
                "config", "targets", "endpoints", "theme", "get", "check",
                "netinfo", "ip", "version"]
    else:
        pool = _command_pool()
    matches = [c for c in pool if c.startswith(low)]
    if len(matches) == 1 and matches[0] != low:
        return matches[0]
    return token


def _print_candidates(prompt: str, text: str) -> None:
    cands = _candidates_for(text)
    at_arg = " " in text
    sys.stdout.write("\r\n")
    if cands:
        for c in cands:
            desc = (arg_help() if at_arg else command_help()).get(c, "")
            sys.stdout.write("  " + tcol(f"{c:<16}", "command") + desc + "\r\n")
    else:
        sys.stdout.write("\r\n")
    sys.stdout.write(prompt + text)
    sys.stdout.flush()


def _tab_complete(prompt: str, text: str) -> str:
    if " " not in text:
        cands = [c for c in _command_pool() if c.startswith(text.lower())]
        if not cands:
            return text
        new = cands[0] + " " if len(cands) == 1 else os.path.commonprefix(cands)
        if len(new) > len(text):
            return new
        if len(cands) > 1:
            _print_candidates(prompt, text)
        return text
    head, _, partial = text.rpartition(" ")
    parts = text.split(" ")
    cands = [c for c in _arg_candidates(parts[0].lower(), parts[1:-1]) if c.startswith(partial)]
    if not cands:
        sys.stdout.write("\r\n")
        sys.stdout.flush()
        return text
    completion = cands[0] if len(cands) == 1 else os.path.commonprefix(cands)
    if len(completion) > len(partial):
        return head + " " + completion
    if len(cands) > 1:
        _print_candidates(prompt, text)
    return text


def _render_line(prompt: str, plain_len: int, text: str, pos: int, prev_len: int) -> int:
    cur = plain_len + len(text)
    out = "\r" + prompt + text
    if cur < prev_len:
        out += " " * (prev_len - cur)
    back = len(text) - pos
    if back > 0:
        out += "\b" * back
    sys.stdout.write(out)
    sys.stdout.flush()
    return cur


def _raw_input_windows(prompt: str, plain_len: int, history: list) -> str:
    import msvcrt
    buf: list = []
    pos = 0
    hidx = len(history)
    prev = plain_len
    sys.stdout.write(prompt)
    sys.stdout.flush()
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            return "".join(buf)
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x1a":
            raise EOFError
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()
            if code == "H":      # 上
                if history and hidx > 0:
                    hidx -= 1
                    buf = list(history[hidx])
                    pos = len(buf)
            elif code == "P":    # 下
                if hidx < len(history) - 1:
                    hidx += 1
                    buf = list(history[hidx])
                    pos = len(buf)
                elif hidx == len(history) - 1:
                    hidx = len(history)
                    buf = []
                    pos = 0
            elif code == "K":    # 左
                if pos > 0:
                    pos -= 1
            elif code == "M":    # 右
                if pos < len(buf):
                    pos += 1
            elif code == "G":    # Home
                pos = 0
            elif code == "O":    # End
                pos = len(buf)
            elif code == "S":    # Delete
                if pos < len(buf):
                    del buf[pos]
            prev = _render_line(prompt, plain_len, "".join(buf), pos, prev)
            continue
        if ch in ("\b", "\x7f"):
            if pos > 0:
                del buf[pos - 1]
                pos -= 1
            prev = _render_line(prompt, plain_len, "".join(buf), pos, prev)
            continue
        if ch == "\t":
            new = _tab_complete(prompt, "".join(buf))
            buf[:] = list(new)
            pos = len(buf)
            prev = _render_line(prompt, plain_len, new, pos, prev)
            continue
        if ch == "?":
            _print_candidates(prompt, "".join(buf))
            prev = _render_line(prompt, plain_len, "".join(buf), pos, plain_len + len(buf))
            continue
        buf.insert(pos, ch)
        pos += 1
        prev = _render_line(prompt, plain_len, "".join(buf), pos, prev)


def _raw_input_unix(prompt: str, plain_len: int, history: list) -> str:
    import codecs
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buf: list = []
    pos = 0
    hidx = len(history)
    prev = plain_len
    dec = codecs.getincrementaldecoder("utf-8")("ignore")
    try:
        tty.setraw(fd)
        sys.stdout.write(prompt)
        sys.stdout.flush()
        while True:
            b = os.read(fd, 1)
            if not b:
                raise EOFError
            if b in (b"\r", b"\n"):
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return "".join(buf)
            if b == b"\x03":
                raise KeyboardInterrupt
            if b == b"\x04":
                raise EOFError
            if b == b"\x1b":
                seq = b""
                while select.select([fd], [], [], 0.01)[0]:
                    seq += os.read(fd, 1)
                if seq.startswith(b"[A") or seq == b"OA":
                    if history and hidx > 0:
                        hidx -= 1
                        buf = list(history[hidx])
                        pos = len(buf)
                elif seq.startswith(b"[B") or seq == b"OB":
                    if hidx < len(history) - 1:
                        hidx += 1
                        buf = list(history[hidx])
                        pos = len(buf)
                    elif hidx == len(history) - 1:
                        hidx = len(history)
                        buf = []
                        pos = 0
                elif seq.startswith(b"[C") or seq == b"OC":
                    if pos < len(buf):
                        pos += 1
                elif seq.startswith(b"[D") or seq == b"OD":
                    if pos > 0:
                        pos -= 1
                elif seq.startswith(b"[H") or seq == b"OH":
                    pos = 0
                elif seq.startswith(b"[F") or seq == b"OF":
                    pos = len(buf)
                elif seq.startswith(b"[3~"):
                    if pos < len(buf):
                        del buf[pos]
                prev = _render_line(prompt, plain_len, "".join(buf), pos, prev)
                continue
            if b in (b"\x7f", b"\x08"):
                if pos > 0:
                    del buf[pos - 1]
                    pos -= 1
                prev = _render_line(prompt, plain_len, "".join(buf), pos, prev)
                continue
            if b == b"\t":
                new = _tab_complete(prompt, "".join(buf))
                buf[:] = list(new)
                pos = len(buf)
                prev = _render_line(prompt, plain_len, new, pos, prev)
                continue
            if b == b"?":
                _print_candidates(prompt, "".join(buf))
                prev = _render_line(prompt, plain_len, "".join(buf), pos, plain_len + len(buf))
                continue
            s = dec.decode(b)
            if s:
                for c in s:
                    buf.insert(pos, c)
                    pos += 1
                prev = _render_line(prompt, plain_len, "".join(buf), pos, prev)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_command_line(prompt: str, plain_len: int, history: list) -> str:
    if not sys.stdin.isatty():
        return input(prompt)
    try:
        if os.name == "nt":
            return _raw_input_windows(prompt, plain_len, history)
        return _raw_input_unix(prompt, plain_len, history)
    except (ImportError, OSError, ValueError):
        return input(prompt)


def _config_mode_cmd(tokens: list, cfg: dict, state: dict):
    """設定模式指令。回傳 'exit' 表示離開設定模式, 否則 None。"""
    cmd = tokens[0].lower()
    if cmd in ("end", "exit"):
        return "exit"
    if cmd in ("write", "save"):
        save_config(cfg, force=True)
        state["dirty"] = False
        ok(t("已儲存設定 (write)", "Config saved (write)"))
        return None
    if cmd == "set":
        if len(tokens) < 3:
            error(t("用法: set <key> <value>", "Usage: set <key> <value>"))
            return None
        set_dotted(cfg, tokens[1], coerce(" ".join(tokens[2:])))
        apply_theme(cfg)
        state["dirty"] = True
        ok(f"set {tokens[1]} = {get_dotted(cfg, tokens[1])}   ({t('尚未 write', 'not written yet')})")
        return None
    if cmd == "no":
        if len(tokens) < 2:
            error(t("用法: no <key>", "Usage: no <key>"))
            return None
        if tokens[1] not in _config_keys():
            warn(t(f"未知 key: {tokens[1]}", f"Unknown key: {tokens[1]}"))
            return None
        set_dotted(cfg, tokens[1], get_dotted(DEFAULT_CONFIG, tokens[1]))
        apply_theme(cfg)
        state["dirty"] = True
        ok(f"no {tokens[1]}   ({t('尚未 write', 'not written yet')})")
        return None
    if cmd in ("endpoints", "targets", "theme", "config"):
        sub_parser = build_parser()
        try:
            sub = sub_parser.parse_args(tokens)
        except SystemExit:
            return None
        _dispatch(sub, cfg, sub_parser)
        state["dirty"] = True
        return None
    if cmd in ("show", "get", "check", "netinfo", "ip", "version"):
        run_command(tokens, cfg)
        return None
    if cmd == "help":
        print_help()
        return None
    error(t("設定模式不支援此指令 (可用: set / no / endpoints / targets / theme / show / write / end)",
            "Unsupported in config mode (use: set / no / endpoints / targets / theme / show / write / end)"))
    return None


def console(start_in_config: bool = False) -> int:
    global USE_COLOR, _CONSOLE_CFG, _DEFER_SAVE, _IN_CONSOLE, DEBUG
    _IN_CONSOLE = True
    if not sys.stdout.isatty():
        USE_COLOR = False
    cfg = load_config()
    _CONSOLE_CFG = cfg
    apply_theme(cfg)
    DEBUG = _debug_enabled(cfg)
    history: list = []
    in_config = start_in_config
    _DEFER_SAVE = start_in_config
    state = {"dirty": False}
    print(tcol("netspeed " + t("互動模式", "interactive mode"), "title") + "  " + tcol(f"v{VERSION}", "dim"))
    print(tcol(t("輸入 help 看指令; ? 候選, Tab 補齊, ↑↓ 歷史, ←→ 移動游標; exit 離開。",
                 "Type help for commands; ? candidates, Tab complete, ↑↓ history, ←→ move cursor; exit to quit."),
               "dim"))
    print(tcol(t(f"設定檔: {config_path()}", f"Config: {config_path()}"), "dim"))
    print(tcol(t("指令前綴會自動補齊 (例如 in → install, wr → write)。",
                 "Command prefixes auto-complete (e.g. in → install, wr → write)."), "dim"))
    print()
    try:
        check_update(cfg, interactive=sys.stdin.isatty())
    except Exception:
        pass
    while True:
        plain = "netspeed(config)# " if in_config else "netspeed> "
        prompt = tcol(plain, "prompt")
        try:
            line = read_command_line(prompt, len(plain), history)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        line = line.strip()
        if not line:
            continue
        history.append(line)
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            error(t(f"無法解析: {exc}", f"Cannot parse: {exc}"))
            continue
        if not tokens:
            continue
        tokens[0] = _expand_prefix(tokens[0], in_config)
        first = tokens[0].lower()

        if in_config:
            result = _config_mode_cmd(tokens, cfg, state)
            if result == "exit":
                _DEFER_SAVE = False
                in_config = False
                if state["dirty"]:
                    warn(t("已離開設定模式; 有變更尚未 write, 下次執行仍為舊設定",
                           "Left config mode; changes not written, next run uses old settings"))
                    state["dirty"] = False
            continue

        if first in ("exit", "quit", "q"):
            break
        if first == "refresh":
            cache_clear()
            ok(t("已清除本次快取 (netinfo / check / env / ip 將重新讀取)",
                 "Cache cleared (netinfo / check / env / ip will reload)"))
            continue
        if first in ("configure", "conf"):
            _DEFER_SAVE = True
            in_config = True
            state["dirty"] = False
            ok(t("進入設定模式。變更即時套用; 需 write 才會寫入檔案。end 離開。",
                 "Entered config mode. Changes apply live; write to persist. end to leave."))
            continue
        if first in ("write", "save"):
            save_config(cfg, force=True)
            ok(t("已儲存設定 (write)", "Config saved (write)"))
            continue
        if first == "no":
            error(t("no 僅能在設定模式使用 (先輸入 configure)",
                    "no is only available in config mode (enter configure first)"))
            continue
        if first == "end":
            warn(t("目前不在設定模式", "Not in config mode"))
            continue
        if first in ("console", "shell"):
            warn(t("已在互動模式中", "Already in interactive mode"))
            continue
        if first not in COMMANDS:
            cands = _candidates_for(first)
            if cands and len(tokens) == 1:
                print()
                for c in cands:
                    print("  " + tcol(f"{c:<16}", "command") + command_help().get(c, ""))
                continue
            error(t(f"未知指令: {tokens[0]}  (輸入 help 查看可用指令)",
                    f"Unknown command: {tokens[0]}  (type help)"))
            continue
        run_command(tokens, cfg)
    if in_config:
        _DEFER_SAVE = False
    print(t("bye", "bye"))
    return 0


def _enable_windows_ansi() -> None:
    if os.name != "nt" or not sys.stdout.isatty():
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # VT processing
    except Exception:
        pass


def _countdown_wait(seconds: int) -> str:
    """倒數等待; 回傳 'run' | 'console' | 'cancel'。"""
    def _decide(ch: str) -> str:
        if ch in ("\x03",):
            return "console"
        if ch.lower() == "c":
            return "console"
        if ch in ("\r", "\n"):
            return "run"
        return "cancel"

    def _msg(i: int) -> str:
        return ("\r  " + t(f"倒數 {i} 秒...  (按 Enter 直接執行, c 進互動模式)",
                            f"Countdown {i}s...  (Enter to run now, c for console)") + "   ")

    if os.name == "nt":
        import msvcrt
        for i in range(seconds, 0, -1):
            m = _msg(i)
            sys.stdout.write(m)
            sys.stdout.flush()
            end = time.time() + 1
            while time.time() < end:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        msvcrt.getwch()
                        return "cancel"
                    return _decide(ch)
                time.sleep(0.05)
        sys.stdout.write("\r" + " " * len(_msg(seconds)) + "\r")
        sys.stdout.flush()
        return "run"

    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        for i in range(seconds, 0, -1):
            m = _msg(i)
            sys.stdout.write(m)
            sys.stdout.flush()
            end = time.time() + 1
            while time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.05)
                if r:
                    ch = os.read(fd, 1).decode("utf-8", "replace")
                    if ch == "\x1b":
                        while select.select([fd], [], [], 0.01)[0]:
                            os.read(fd, 1)
                        return "cancel"
                    return _decide(ch)
        sys.stdout.write("\r" + " " * len(_msg(seconds)) + "\r")
        sys.stdout.flush()
        return "run"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def run_default_with_countdown(seconds: int = 5) -> int:
    global DEBUG
    cfg = load_config()
    apply_theme(cfg)
    DEBUG = _debug_enabled(cfg)
    print(tcol(f"netspeed v{VERSION}", "title"))
    try:
        check_update(cfg, interactive=True)
    except Exception:
        pass
    sources = cfg.get("sources", DEFAULT_CONFIG["sources"])
    section(t("預設測速", "Default speed test"))
    print("  " + t("將以預設設定執行:", "Will run with default settings:"))
    print("    " + t("外網 IP / 電信商 + Ping + 測速來源: ",
                     "Public IP / ISP + Ping + sources: ")
          + ", ".join(SOURCE_LABELS.get(s, s) for s in sources))
    print(tcol(t("  倒數中可按鍵: [c] 進入互動模式 / [其他鍵] 取消 / 不按鍵則倒數後執行",
                 "  During countdown: [c] console / [any other key] cancel / no key = run"), "dim"))
    print(tcol(t("  直接按 Enter 可立即開始測試。", "  Press Enter to start immediately."), "dim"))
    print()
    action = _countdown_wait(seconds)
    if action == "console":
        print(t("進入互動模式。", "Entering interactive mode."))
        return console()
    if action == "cancel":
        print(t("已取消。", "Cancelled."))
        return 0
    args = argparse.Namespace(sources=None, no_ping=False, no_ip=False, seconds=None,
                              json=False, no_color=False, config=None, no_save=False,
                              no_update_check=True)
    cmd_all(cfg, args)
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        if not sys.stdin.isatty():
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _enable_windows_ansi()

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].lower() in ("configure", "conf") and len(argv) == 1:
        if not sys.stdin.isatty():
            print_help()
            return 0
        return console(start_in_config=True)
    if not argv:
        if not sys.stdin.isatty():
            print_help()
            return 0
        return run_default_with_countdown()
    return run_command(argv)


if __name__ == "__main__":
    sys.exit(main())
