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
  netspeed                     顯示說明並詢問是否以預設方式測速
  netspeed all                 全部來源 + ping + 外網 IP
  netspeed mlab|fast|cloudflare|ookla|libre
  netspeed ping [targets...]
  netspeed ip
  netspeed pair serve|client <host>|history
  netspeed history
  netspeed config|set|get|targets|endpoints

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

VERSION = "1.3.0"
APP = "netspeed"
PROTO_TAG = "NETSPEED1"

USE_COLOR = True

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


# 本次執行/console session 內的快取 (給讀取較慢的指令用, 例如 netinfo)
_SESSION_CACHE: dict = {}


def cache_clear() -> None:
    _SESSION_CACHE.clear()


# --------------------------------------------------------------------------- #
# 設定檔
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
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
        warn(f"設定檔讀取失敗 ({exc}), 使用預設值")
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


def _local_tool_dirs() -> list:
    dirs = []
    for d in (os.path.dirname(os.path.abspath(__file__)), install_dir(), os.getcwd()):
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
            return found
    exts = [""] if os.name != "nt" else ["", ".exe", ".cmd", ".bat", ".ps1"]
    for d in _local_tool_dirs():
        for name in names:
            for ext in exts:
                cand = os.path.join(d, name + ext)
                if os.path.isfile(cand) and (os.name == "nt" or os.access(cand, os.X_OK)):
                    return cand
    return None


def _fix_cmd(cmd: list) -> list:
    if os.name == "nt" and cmd and cmd[0].lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c"] + cmd
    return cmd


def run_cmd(cmd: list, timeout: float | None = None, input_data: bytes | None = None):
    fixed = _fix_cmd(list(cmd))
    try:
        return subprocess.run(
            fixed,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            input=input_data,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(fixed, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return subprocess.CompletedProcess(fixed, 124, out or "", (err or "") + "\n[timeout]")


# --------------------------------------------------------------------------- #
# 外網 IP / 電信商 (多來源自動備援)
# --------------------------------------------------------------------------- #


def _http_get(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"netspeed/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def lookup_ip_isp() -> dict:
    result = {"ip": None, "isp": None, "asn": None, "org": None,
              "city": None, "country": None, "source": None}

    providers = [
        ("ipinfo.io", "https://ipinfo.io/json"),
        ("ifconfig.co", "https://ifconfig.co/json"),
        ("ip-api.com", "http://ip-api.com/json"),
    ]
    for name, url in providers:
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
            return result

    # 最後備援: 只取 IP
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            result["ip"] = _http_get(url).strip()
            result["source"] = url.split("/")[2]
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
                warn(f"ICMP ping 失敗 ({exc}), 改用系統 ping")
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
            warn("偵測不到 default gateway")
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
        return {"source": "mlab", "error": "找不到 ndt7-client"}
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
        return {"source": "fast", "error": "找不到 fast-cli (npm install -g fast-cli)"}
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
        return {"source": "ookla", "error": "找不到 Ookla speedtest CLI"}
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
        return {"source": "libre", "error": "找不到 librespeed-cli"}
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
    return {"source": name, "error": f"未知來源 {name}"}


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
    parts.append(f"下載 {dl:8.2f} Mbps" if dl is not None else "下載   n/a")
    parts.append(f"上傳 {ul:8.2f} Mbps" if ul is not None else "上傳   n/a")
    if lat is not None:
        parts.append(f"延遲 {lat:.1f} ms")
    print("  " + "  ".join(parts))


# --------------------------------------------------------------------------- #
# 對測 (pair)
# --------------------------------------------------------------------------- #


def find_iperf(cfg: dict, override: str | None = None) -> str | None:
    custom = override or get_dotted(cfg, "iperf.path")
    return find_tool(["iperf3", "iperf"], custom)


def iperf_client(host: str, port: int, duration: int, streams: int, mode: str,
                 iperf_path: str) -> dict:
    result = {"download": None, "upload": None, "latency": None, "raw": ""}
    runs = []
    if mode in ("both", "up"):
        runs.append(("up", []))
    if mode in ("both", "down"):
        runs.append(("down", ["-R"]))
    for label, flags in runs:
        cmd = [iperf_path, "-c", host, "-p", str(port), "-t", str(duration),
               "-P", str(streams), "-J"] + flags
        proc = run_cmd(cmd, timeout=duration + 30)
        result["raw"] += (proc.stdout or "") + "\n"
        try:
            data = json.loads(proc.stdout)
            end = data.get("end", {})
            if label == "up":
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
    print(col(f"[內建對測伺服器] 監聽 {host}:{port}  (Ctrl+C 結束)", "green"))
    print("等待對測端連線...")
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
    print("\n伺服器結束。")


def _builtin_handle(conn: socket.socket, addr) -> None:
    peer = f"{addr[0]}:{addr[1]}"
    print(col(f"\n-> 連線來自 {peer}  ({now_iso()})", "cyan"))
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
            print(f"   下載 {mbps:8.2f} Mbps  (傳給 {peer})")
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
            print(f"   上傳 {mbps:8.2f} Mbps  (來自 {peer})")
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
        warn(f"無法連線 {host}:{port} ({exc})")
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
        warn(f"無法連線 {host}:{port} ({exc})")
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


def builtin_client(host: str, port: int, duration: int, mode: str) -> dict:
    result = {"download": None, "upload": None, "latency": None}
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

    iperf_path = find_iperf(cfg, getattr(args, "iperf_path", None))
    method = args.method
    if method == "auto":
        method = "iperf3" if iperf_path else "builtin"

    if method == "iperf3" and not iperf_path:
        warn("找不到 iperf3, 改用內建對測")
        method = "builtin"

    print(col(f"[對測] 目標 {host}:{port}  方式={method}  模式={mode}  秒數={duration}", "cyan"))
    t0 = time.time()
    if method == "iperf3":
        res = iperf_client(host, port, duration, streams, mode, iperf_path)
    else:
        res = builtin_client(host, port, duration, mode)
    res["elapsed"] = round(time.time() - t0, 1)

    res.update({"kind": "pair", "time": now_iso(), "role": "client",
                "peer_ip": host, "peer_port": port, "method": method, "mode": mode})
    return res


# --------------------------------------------------------------------------- #
# 報告
# --------------------------------------------------------------------------- #


def print_ip_section(ipinfo: dict) -> None:
    section("外網 IP / 線路")
    print(f"  外網 IP : {col(ipinfo.get('ip') or 'n/a', 'bold')}")
    isp = ipinfo.get("isp") or ipinfo.get("org") or "n/a"
    asn = ipinfo.get("asn") or ""
    print(f"  電信商  : {isp} {('(' + asn + ')') if asn else ''}".rstrip())
    loc = ", ".join(x for x in (ipinfo.get("city"), ipinfo.get("country")) if x)
    if loc:
        print(f"  地區    : {loc}")
    print(f"  來源    : {ipinfo.get('source') or 'n/a'}")


def print_ping_table(results: list) -> None:
    header = f"  {'目標':<20} {'傳送':>4} {'接收':>4} {'遺失':>6} {'min':>7} {'avg':>7} {'max':>7} {'mdev':>7} {'jitter':>7}  方式"
    print(col(header, "bold"))
    print("  " + "-" * 96)

    def f(v):
        return f"{v:7.2f}" if isinstance(v, (int, float)) else f"{'n/a':>7}"

    for r in results:
        if r.get("error"):
            print(f"  {r['target']:<20} {col('失敗: ' + r['error'], 'red')}")
            continue
        loss = r.get("loss")
        loss_s = f"{loss:5.1f}%" if isinstance(loss, (int, float)) else "  n/a"
        print(f"  {str(r.get('target')):<20} {str(r.get('sent') or 0):>4} "
              f"{str(r.get('recv') or 0):>4} {loss_s:>6} {f(r.get('min'))} {f(r.get('avg'))} "
              f"{f(r.get('max'))} {f(r.get('mdev'))} {f(r.get('jitter'))}  {r.get('method','')}")


def print_speed_table(results: list) -> None:
    section("測速結果 (Mbps)")
    header = f"  {'來源':<22} {'下載':>10} {'上傳':>10} {'延遲(ms)':>10}  節點"
    print(col(header, "bold"))
    print("  " + "-" * 80)
    dls = []
    uls = []
    for r in results:
        src = SOURCE_LABELS.get(r.get("source"), r.get("source"))
        if r.get("error"):
            print(f"  {src:<22} {col(r['error'], 'yellow')}")
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
        print(f"  {src:<22} {dl_s} {ul_s} {lat_s}  {r.get('server') or ''}")
    print("  " + "-" * 80)
    avg_dl = f"{sum(dls)/len(dls):10.2f}" if dls else f"{'n/a':>10}"
    avg_ul = f"{sum(uls)/len(uls):10.2f}" if uls else f"{'n/a':>10}"
    print(col(f"  {'平均':<22} {avg_dl} {avg_ul}", "bold"))


def print_pair_result(res: dict) -> None:
    section("對測結果")
    print(f"  角色    : {res.get('role')}")
    print(f"  對端    : {res.get('peer_ip')}:{res.get('peer_port')}")
    print(f"  方式    : {res.get('method')}")
    print(f"  時間    : {res.get('time')}")
    if res.get("download") is not None:
        print("  下載    : " + col(f"{res['download']:.2f} Mbps", "green"))
    if res.get("upload") is not None:
        print("  上傳    : " + col(f"{res['upload']:.2f} Mbps", "green"))
    if res.get("elapsed") is not None:
        print(f"  耗時    : {res['elapsed']}s")


# --------------------------------------------------------------------------- #
# 指令實作
# --------------------------------------------------------------------------- #


def cmd_ip(cfg: dict, args) -> dict:
    if not getattr(args, "refresh", False) and "ip" in _SESSION_CACHE:
        ipinfo = _SESSION_CACHE["ip"]
    else:
        ipinfo = lookup_ip_isp()
        _SESSION_CACHE["ip"] = ipinfo
    if args.json:
        print(json.dumps(ipinfo, ensure_ascii=False, indent=2))
    else:
        print_ip_section(ipinfo)
    if not args.no_save:
        rec = {"kind": "ip"}
        rec.update(ipinfo)
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


def _do_ping(cfg: dict, args, targets: list) -> list:
    use_json = getattr(args, "json", False)
    no_save = getattr(args, "no_save", False)
    eff = _ping_effective(cfg, args)
    if not targets:
        error("沒有可 ping 的目標")
        return []
    if not use_json:
        section(f"Ping 測試 count:{eff['count']} interval:{eff['interval']} "
                f"timeout:{eff['timeout']} size:{eff['size']}")
    results = []
    for t in targets:
        if not use_json:
            print(f"  正在 ping {col(t, 'bold')} ...")
        r = ping_target(t, cfg, eff["count"], eff["interval"], eff["timeout"],
                        eff["size"], eff["method"])
        results.append(r)
        if not use_json:
            loss = r.get("loss")
            avg = r.get("avg")
            print(f"    -> {r.get('ip') or t}  平均 {avg:.2f} ms  遺失 {loss:.1f}%"
                  if isinstance(avg, (int, float)) and isinstance(loss, (int, float))
                  else f"    -> {r.get('ip') or t}  無回應")
        if not no_save:
            rec = {"kind": "ping", "target": t}
            for k in ("sent", "recv", "loss", "min", "avg", "max", "mdev", "jitter"):
                rec[k] = r.get(k)
            append_history(rec, cfg)
    if use_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print()
        print_ping_table(results)
    return results


def _ping_pick_target(targets: list):
    if not targets:
        warn("沒有目標")
        return None
    print("  選擇目標:")
    for i, t in enumerate(targets, 1):
        print(f"    [{i}] {t}")
    print(tcol("    [Enter/q] 返回", "dim"))
    try:
        choice = input(col("  請輸入: ", "bold")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(targets):
        return targets[int(choice) - 1]
    return None


def _ping_params_menu(cfg: dict) -> None:
    params = [("count", "次數"), ("interval", "間隔(秒)"), ("timeout", "逾時(秒)"),
              ("size", "封包大小"), ("method", "auto|icmp|system")]
    while True:
        pc = cfg.get("ping", {})
        print()
        print(tcol("  Ping 參數:", "heading"))
        for i, (k, desc) in enumerate(params, 1):
            print(f"    [{i}] {k} = {pc.get(k)}   ({desc})")
        print(tcol("    [Enter/q] 返回", "dim"))
        try:
            choice = input(col("  請輸入: ", "bold")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in ("", "q", "quit", "exit"):
            return
        if choice.isdigit() and 1 <= int(choice) <= len(params):
            key = params[int(choice) - 1][0]
            try:
                val = input(f"  {key} 新值: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if val:
                set_dotted(cfg, f"ping.{key}", coerce(val))
                save_config(cfg)
                ok(f"ping.{key} = {get_dotted(cfg, f'ping.{key}')}")
        else:
            warn("無效的選擇")


def _ping_targets_menu(cfg: dict) -> None:
    while True:
        targets = cfg.setdefault("ping", {}).setdefault("targets", [])
        print()
        print(tcol("  Ping 目標清單:", "heading"))
        for i, t in enumerate(targets, 1):
            print(f"    [{i}] {t}")
        print(tcol("    [a] 新增   [d] 刪除   [Enter/q] 返回", "dim"))
        try:
            choice = input(col("  請輸入: ", "bold")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in ("", "q", "quit", "exit"):
            return
        if choice == "a":
            try:
                t = input("  新增目標 (IP/主機): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if t:
                if t not in targets:
                    targets.append(t)
                save_config(cfg)
                ok(f"已新增 {t}")
        elif choice == "d":
            try:
                n = input("  刪除編號: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                continue
            if n.isdigit() and 1 <= int(n) <= len(targets):
                removed = targets.pop(int(n) - 1)
                save_config(cfg)
                ok(f"已刪除 {removed}")
        else:
            warn("無效的選擇")


def _ping_settings_menu(cfg: dict) -> None:
    while True:
        pc = cfg.get("ping", {})
        print()
        print(tcol("  修改 Ping 設定:", "heading"))
        print(f"    [1] 預設閘道自動加入: {'on' if pc.get('auto_gateway', True) else 'off'}")
        print(f"    [2] 目標清單 ({len(pc.get('targets', []))} 個)")
        print("    [3] 參數 (count / interval / timeout / size / method)")
        print(tcol("    [Enter/q] 返回", "dim"))
        try:
            choice = input(col("  請輸入: ", "bold")).strip().lower()
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
            warn("無效的選擇")


def _ping_menu(cfg: dict, args) -> list:
    while True:
        eff = _ping_effective(cfg, args)
        print()
        section(f"Ping 測試 count:{eff['count']} interval:{eff['interval']} "
                f"timeout:{eff['timeout']} size:{eff['size']}")
        print("    [1] 全部目標")
        print("    [2] 選擇單一目標")
        print("    [3] 修改設定")
        print(tcol("    [Enter/q] 取消", "dim"))
        try:
            choice = input(col("  請輸入: ", "bold")).strip().lower()
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
            warn("無效的選擇")


def cmd_ping(cfg: dict, args) -> list:
    extra = getattr(args, "targets", None) or []
    if extra or getattr(args, "only", False) or getattr(args, "json", False) \
            or not sys.stdin.isatty():
        targets = resolve_ping_targets(cfg, extra,
                                       use_gateway=not getattr(args, "no_gateway", False),
                                       include_config=not getattr(args, "only", False))
        return _do_ping(cfg, args, targets)
    return _ping_menu(cfg, args)


def cmd_source(name: str, cfg: dict, args) -> dict:
    if not args.json:
        section(f"測速 - {SOURCE_LABELS.get(name, name)}")
    res = run_source(name, cfg, args)
    if not args.json:
        print_speed_result(res)
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    if not args.no_save and not res.get("error"):
        rec = {"kind": "speed"}
        rec.update({k: res.get(k) for k in ("source", "download", "upload", "latency", "server")})
        append_history(rec, cfg)
    return res


def cmd_all(cfg: dict, args) -> dict:
    if args.sources:
        sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    else:
        sources = cfg.get("sources", DEFAULT_CONFIG["sources"])

    collected = {"ip": None, "ping": [], "speed": []}

    if not args.no_ip:
        ipinfo = lookup_ip_isp()
        collected["ip"] = ipinfo
        if not args.no_save:
            rec = {"kind": "ip"}
            rec.update(ipinfo)
            append_history(rec, cfg)
        if not args.json:
            print_ip_section(ipinfo)
    if not args.no_ping:
        collected["ping"] = _do_ping(cfg, args, resolve_ping_targets(cfg, []))

    for name in sources:
        try:
            res = run_source(name, cfg, args)
        except Exception as exc:  # noqa: BLE001
            res = {"source": name, "error": str(exc)}
        collected["speed"].append(res)
        if not args.no_save and not res.get("error"):
            rec = {"kind": "speed"}
            rec.update({k: res.get(k) for k in ("source", "download", "upload", "latency", "server")})
            append_history(rec, cfg)

    if args.json:
        print(json.dumps(collected, ensure_ascii=False, indent=2))
    else:
        print_speed_table(collected["speed"])
        section("統整完成")
        print(f"  時間: {now_iso()}")
    return collected


def cmd_pair_serve(cfg: dict, args) -> None:
    port = args.port or get_dotted(cfg, "iperf.port") or 5201
    host = args.host
    iperf_path = find_iperf(cfg, getattr(args, "iperf_path", None))
    method = args.method
    if method == "auto":
        method = "iperf3" if iperf_path else "builtin"
    if method == "iperf3" and not iperf_path:
        warn("找不到 iperf3, 改用內建對測伺服器")
        method = "builtin"

    if method == "iperf3":
        print(col(f"[iperf3 對測伺服器] {host}:{port}  (Ctrl+C 結束)", "green"))
        proc = subprocess.Popen([iperf_path, "-s", "-p", str(port)])
        try:
            proc.wait()
        except KeyboardInterrupt:
            print("\n正在停止 iperf3 ...")
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("伺服器結束。")
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
                    ("role", "peer_ip", "peer_port", "method", "mode",
                     "download", "upload", "latency", "elapsed")})
        append_history(rec, cfg)
    return res


def _record_desc(rec: dict) -> str:
    kind = rec.get("kind")
    if kind == "speed":
        dl = rec.get("download")
        ul = rec.get("upload")
        dl_s = f"{dl:.1f}" if isinstance(dl, (int, float)) else "n/a"
        ul_s = f"{ul:.1f}" if isinstance(ul, (int, float)) else "n/a"
        return f"測速 {SOURCE_LABELS.get(rec.get('source'), rec.get('source'))}  下載 {dl_s} / 上傳 {ul_s} Mbps"
    if kind == "ping":
        avg = rec.get("avg")
        avg_s = f"{avg:.2f}ms" if isinstance(avg, (int, float)) else "n/a"
        loss = rec.get("loss")
        loss_s = f"{loss:.0f}%" if isinstance(loss, (int, float)) else "n/a"
        return f"Ping {rec.get('target')}  平均 {avg_s}  遺失 {loss_s}"
    if kind == "pair":
        dl = rec.get("download")
        ul = rec.get("upload")
        dl_s = f"{dl:.1f}" if isinstance(dl, (int, float)) else "n/a"
        ul_s = f"{ul:.1f}" if isinstance(ul, (int, float)) else "n/a"
        return (f"對測 {rec.get('peer_ip')}:{rec.get('peer_port')} ({rec.get('method')}) "
                f"下載 {dl_s} / 上傳 {ul_s} Mbps")
    if kind == "ip":
        return f"外網 IP {rec.get('ip')} ({rec.get('isp') or rec.get('org') or '?'})"
    return kind or "?"


def rerun_record(rec: dict, cfg: dict, args) -> None:
    kind = rec.get("kind")
    if kind == "speed":
        cmd_source(rec.get("source"), cfg, args)
    elif kind == "ping":
        targets = [rec.get("target")]
        sub = argparse.Namespace(targets=targets, count=None, interval=None, timeout=None,
                                 size=None, method=None, no_gateway=True, only=True,
                                 json=args.json, no_save=args.no_save, no_color=args.no_color)
        cmd_ping(cfg, sub)
    elif kind == "pair":
        sub = argparse.Namespace(host=rec.get("peer_ip"), port=rec.get("peer_port"),
                                 method=rec.get("method") or "auto", mode=rec.get("mode") or "both",
                                 duration=None, streams=None, iperf_path=None,
                                 json=args.json, no_save=args.no_save, no_color=args.no_color)
        cmd_pair_client(cfg, sub)
    elif kind == "ip":
        cmd_ip(cfg, args)
    else:
        warn(f"不支援重跑類型: {kind}")


def cmd_history(cfg: dict, args, interactive: bool = True) -> None:
    kind = getattr(args, "kind", None)
    limit = getattr(args, "limit", 20)
    run = getattr(args, "run", None)
    records = load_history()
    if kind:
        records = [r for r in records if r.get("kind") == kind]
    if not records:
        info("目前沒有歷史紀錄。")
        return
    shown = records[-limit:][::-1]
    section("歷史紀錄")
    for i, rec in enumerate(shown, 1):
        print(f"  [{i:>3}] {rec.get('time','?'):<20} {_record_desc(rec)}")

    if run:
        idx = run
        if 1 <= idx <= len(shown):
            rerun_record(shown[idx - 1], cfg, args)
        else:
            error("編號超出範圍")
        return

    if interactive and sys.stdin.isatty():
        try:
            choice = input(col("\n輸入編號以重跑該項 (Enter 取消): ", "bold")).strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(shown):
                rerun_record(shown[idx - 1], cfg, args)
            else:
                error("編號超出範圍")


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
        ok("設定已重設為預設值")
    elif action == "export":
        path = values[0] if values else os.path.join(os.getcwd(), "netspeed-config.json")
        try:
            save_config(cfg, path)
        except OSError as exc:
            error(f"匯出失敗: {exc}")
            return
        ok(f"已匯出設定: {path}")
    elif action == "import":
        if not values:
            error("用法: config import <檔案>")
            return
        path = values[0]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            error(f"讀取失敗: {exc}")
            return
        merged = deep_merge(DEFAULT_CONFIG, data)
        cfg.clear()
        cfg.update(merged)
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(f"已匯入設定: {path}")
    else:
        print(tcol(f"設定檔: {config_path()}", "dim"))
        print(json.dumps(cfg, ensure_ascii=False, indent=2))


def cmd_theme(cfg: dict, args) -> None:
    action = args.action
    values = getattr(args, "values", None) or []
    theme = cfg.setdefault("theme", dict(DEFAULT_THEME))
    if action == "set":
        if len(values) < 2:
            error("用法: theme set <項目> <顏色>   顏色例: red / green,bold / default")
            return
        key, val = values[0], values[1]
        if key not in DEFAULT_THEME:
            warn(f"未知項目 {key} (可用: {', '.join(DEFAULT_THEME)})")
        theme[key] = val
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(f"theme.{key} = {val}")
    elif action == "reset":
        cfg["theme"] = dict(DEFAULT_THEME)
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok("主題已重設為預設")
    elif action == "preset":
        name = values[0] if values else ""
        if name not in THEME_PRESETS:
            error(f"未知 preset: {name} (可用: {', '.join(THEME_PRESETS)})")
            return
        cfg["theme"] = dict(THEME_PRESETS[name])
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(f"已套用主題 preset: {name}")
    elif action == "export":
        path = values[0] if values else os.path.join(os.getcwd(), "netspeed-theme.json")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg.get("theme", {}), fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            error(f"匯出失敗: {exc}")
            return
        ok(f"已匯出主題: {path}")
    elif action == "import":
        if not values:
            error("用法: theme import <檔案>")
            return
        path = values[0]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            error(f"讀取失敗: {exc}")
            return
        cfg["theme"] = deep_merge(DEFAULT_THEME, data)
        save_config(cfg, getattr(args, "config", None))
        apply_theme(cfg)
        ok(f"已匯入主題: {path}")
    else:
        section("主題 (theme)")
        for key in DEFAULT_THEME:
            val = theme.get(key, DEFAULT_THEME[key])
            sample = "範例 Sample" if not val or val == "default" else col("範例 Sample", *str(val).split(","))
            print(f"  {key:<10} {val:<16} {sample}")
        print()
        print(tcol("可設定項目: " + ", ".join(DEFAULT_THEME), "dim"))
        print(tcol("presets: " + ", ".join(THEME_PRESETS), "dim"))
        print(tcol("用法: theme set <項目> <顏色> / theme preset <名稱> / theme export|import <檔案>", "dim"))


def cmd_targets(cfg: dict, args) -> None:
    action = args.action
    targets = cfg.setdefault("ping", {}).setdefault("targets", [])
    if action == "add":
        for t in args.values:
            if t not in targets:
                targets.append(t)
        save_config(cfg)
        ok(f"已新增: {', '.join(args.values)}")
    elif action == "del":
        for t in args.values:
            if t in targets:
                targets.remove(t)
        save_config(cfg)
        ok(f"已刪除: {', '.join(args.values)}")
    elif action == "set":
        cfg["ping"]["targets"] = list(args.values)
        save_config(cfg)
        ok("已設定 ping 目標清單")
    elif action == "gateway":
        gw = get_default_gateway()
        print(f"Default gateway: {gw or 'n/a'}")
        return
    else:
        print(col("Ping 目標清單:", "bold"))
        for i, t in enumerate(targets, 1):
            print(f"  {i:>2}. {t}")
        print(col(f"  自動偵測 default gateway: {cfg['ping'].get('auto_gateway')}", "dim"))


def cmd_endpoints(cfg: dict, args) -> None:
    if args.action == "set":
        if len(args.values) < 2:
            error("用法: endpoints set <來源> <值>")
            return
        source, value = args.values[0], " ".join(args.values[1:])
        set_dotted(cfg, f"endpoints.{source}", value)
        save_config(cfg)
        ok(f"已設定 endpoints.{source} = {value}")
        return
    print(col("測速端點設定 (endpoints):", "bold"))
    for key, val in cfg.get("endpoints", {}).items():
        print(f"  {key:<12}: {val or '(自動)'}")
    print()
    print(col("啟用來源 (sources):", "bold"))
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
                           "dhcp": None, "gateway": None}
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
        return "DHCP (自動取得)"
    if val is False:
        return "靜態 (手動設定)"
    return "未知"


def _print_iface(iface: dict) -> None:
    print(col(f"介面: {iface['name']}", "bold")
          + (f"   狀態: {iface.get('status')}" if iface.get("status") else "")
          + (f"   MAC: {iface['mac']}" if iface.get("mac") else ""))
    print(f"  設定方式 : {_dhcp_text(iface.get('dhcp'))}")
    for item in iface.get("ipv4", []):
        tag = "次要 Sub IP" if item.get("secondary") else "主要"
        mask = item.get("netmask") or "?"
        print(f"  IP       : {item.get('cidr'):<20} 遮罩 {mask:<16} [{tag}]")
    print(f"  Default Gateway : {iface.get('gateway') or 'n/a'}")


def _print_routes(routes: list) -> None:
    if not routes:
        return
    print()
    print(col("Static Route (靜態路由):", "bold"))
    print(f"  {'目標':<22} {'閘道':<18} {'介面':<12} {'計量':>6}  協定")
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
    print("  選擇要顯示的介面:")
    for i, ifc in enumerate(real, 1):
        ips = ", ".join(x.get("cidr", "?") for x in ifc.get("ipv4", []))
        gw = ifc.get("gateway")
        tag = tcol("  (default route)", "dim") if gw else ""
        print(f"    [{i}] {ifc['name']:<24} {ips}{tag}")
    print(tcol("    [a] 全部    [Enter/q] 離開", "dim"))
    try:
        choice = input(col("  請輸入: ", "bold")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if choice in ("", "q", "quit", "exit"):
        info("已取消。")
        return None
    if choice == "a":
        return real
    if choice.isdigit() and 1 <= int(choice) <= len(real):
        return [real[int(choice) - 1]]
    warn("無效的選擇")
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
            error(f"找不到介面: {args.interface}")
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
        error("找不到作用中的網路介面")
        return

    section("系統網路資訊")
    for ifc in selected:
        _print_iface(ifc)
        print()

    if (not getattr(args, "all", False) and not getattr(args, "interface", None)
            and not getattr(args, "select", False) and len(real) > 1):
        print(tcol(f"  (偵測到 {len(real)} 個介面; netinfo -i 可選擇, netinfo --all 顯示全部)", "dim"))

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


def cmd_check(cfg: dict, args) -> None:
    if not getattr(args, "refresh", False) and "check" in _SESSION_CACHE:
        checks = _SESSION_CACHE["check"]
        _print_check(cfg, args, checks)
        return
    checks = _collect_checks(cfg)
    _SESSION_CACHE["check"] = checks
    _print_check(cfg, args, checks)


def _collect_checks(cfg: dict) -> list:
    checks = []

    checks.append((True, "Python", sys.version.split()[0]))

    try:
        ensure_dir(config_dir())
        with open(os.path.join(config_dir(), ".write_test"), "w") as fh:
            fh.write("x")
        os.remove(os.path.join(config_dir(), ".write_test"))
        checks.append((True, "設定檔目錄", config_dir()))
    except Exception as exc:
        checks.append((False, "設定檔目錄", f"{config_dir()} ({exc})"))

    checks.append((True, "Cloudflare 測速", "內建 (純 Python, 不需外部工具)"))
    checks.append((True, "外網 IP / 電信商", "內建 (ipinfo / ifconfig / ip-api 備援)"))

    okping, ping_detail = _check_ping()
    checks.append((okping, "Ping", ping_detail))

    tool_map = [
        ("M-Lab", ["ndt7-client"], "ndt7-client.exe",
         "https://github.com/m-lab/ndt7-client-go/releases"),
        ("fast.com", ["fast"], "fast.cmd (Node.js)",
         "npm install -g fast-cli  /  https://github.com/sindresorhus/fast-cli"),
        ("Ookla", ["speedtest", "ookla"], "speedtest.exe",
         "https://www.speedtest.net/apps/cli"),
        ("LibreSpeed", ["librespeed-cli"], "librespeed-cli.exe",
         "https://github.com/librespeed/speedtest-cli/releases"),
    ]
    for label, names, fname, url in tool_map:
        found = find_tool(names)
        checks.append((bool(found), label, found or f"找不到 — 下載: {url}"))

    iperf = find_iperf(cfg)
    checks.append((bool(iperf), "對測 iperf3",
                   iperf or "找不到 — 將使用內建對測  (下載: https://iperf.fr/iperf-download.php)"))

    online = _check_internet()
    checks.append((online, "網際網路連線", "可達" if online else "無法連線 (TCP 1.1.1.1:443)"))
    return checks


def _print_check(cfg: dict, args, checks: list) -> None:
    if getattr(args, "json", False):
        print(json.dumps([{"ok": c[0], "item": c[1], "detail": c[2]} for c in checks],
                         ensure_ascii=False, indent=2))
        return

    section("工具狀態檢查")
    for good, item, detail in checks:
        mark = tcol("[v]", "ok") if good else tcol("[x]", "error")
        print(f"  {mark} {item:<16} {detail}")
    usable = sum(1 for c in checks if c[0])
    print()
    print(f"  可用項目: {usable}/{len(checks)}")
    print(tcol(f"  提示: 外部工具可放在 PATH, 或直接放在 netspeed 同層目錄: {os.path.dirname(os.path.abspath(__file__))}",
               "dim"))


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
        error(f"下載更新失敗: {exc}")
        return False
    if "def main(" not in content or "netspeed" not in content:
        error("下載內容不像是 netspeed.py, 已中止")
        return False
    path = os.path.abspath(__file__)
    try:
        shutil.copy2(path, path + ".bak")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
    except OSError as exc:
        error(f"寫入失敗: {exc}")
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
            warn("尚未設定更新來源。請先設定: netspeed set update.repo <你的帳號>/<repo>")
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
            ok(f"已是最新版本 ({VERSION})")
        return
    if latest == get_dotted(cfg, "update.skip_version"):
        if force:
            info(f"版本 {latest} 已被標記為不更新。")
        return

    section("發現新版本")
    print(f"  目前版本 : {VERSION}")
    print(f"  最新版本 : {col(latest, 'green', 'bold')}")
    body = (release.get("body") or "").strip()
    if body:
        print("  更新內容 :")
        for line in body.splitlines()[:20]:
            print("    " + line)
    print()
    if not interactive:
        return
    try:
        ans = input(col("是否更新? [y=更新 / n=稍後 / s=此版本不再通知]: ", "bold")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if ans == "s":
        set_dotted(cfg, "update.skip_version", latest)
        save_config(cfg)
        ok(f"已設定不再通知版本 {latest}")
    elif ans in ("y", "yes"):
        if _do_update(repo, tag, release):
            ok("更新完成, 請重新啟動 netspeed。")
        else:
            error("更新失敗。")


def cmd_update(cfg: dict, args) -> None:
    if getattr(args, "reset_skip", False):
        set_dotted(cfg, "update.skip_version", "")
        save_config(cfg)
        ok("已清除『不再通知』的版本設定")
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
        print(f"偵測到已安裝於: {install_dir()}")
        if not _confirm("是否重新安裝 (覆蓋)? [y/N]: ", getattr(args, "yes", False)):
            info("已取消安裝。")
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
        path_note = "已略過 (--no-path)"
    elif os.name == "nt":
        added = _win_add_path(ldir)
        path_note = "已加入使用者 PATH" if added else "PATH 已包含此目錄"
    else:
        touched = _unix_add_path(ldir)
        path_note = ("已更新 " + ", ".join(touched)) if touched else "PATH 設定已存在"

    section("安裝完成")
    print(f"  程式      : {dst}")
    print(f"  啟動器    : {launcher}")
    print(f"  PATH      : {path_note}")
    print(f"  設定/歷史 : {config_dir()}")
    print()
    if os.name == "nt":
        print("請重開終端機後即可使用: " + col("netspeed all", "bold"))
    else:
        print(f"重開終端機或執行 source 後即可使用: {col('netspeed all', 'bold')}")
        print(f"（也可直接: {col(f'{python} {dst} all', 'bold')}）")


def cmd_uninstall(cfg: dict, args) -> None:
    if _is_installed():
        if not _confirm("確定要移除已安裝的 netspeed? [y/N]: ", getattr(args, "yes", False)):
            info("已取消移除。")
            return
    elif not args.purge:
        info("找不到已安裝的檔案 (可能尚未安裝)。")
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

    section("移除完成")
    if removed:
        for p in removed:
            print(f"  已刪除: {p}")
    else:
        print("  找不到已安裝的檔案 (可能尚未安裝)")
    if args.purge:
        shutil.rmtree(config_dir(), ignore_errors=True)
        print(f"  已清除設定與歷史: {config_dir()}")
    else:
        print(f"  設定與歷史保留於: {config_dir()}  (加 --purge 可一併刪除)")


# --------------------------------------------------------------------------- #
# 說明 / 互動
# --------------------------------------------------------------------------- #

HELP_SECTIONS = [
    ("測速", [
        ("speed all", "全部來源 + ping + 外網 IP, 最後給統整報告"),
        ("speed mlab|fast|cloudflare|ookla|libre", "單一來源測速"),
        ("", "例: speed cloudflare --seconds 15    speed fast --upload"),
    ]),
    ("Ping", [
        ("ping [targets...]", "對目標 ping (預設含 default gateway + 清單)"),
        ("", "選項: -c/--count  -i/--interval  -W/--timeout  -s/--size  --method auto|icmp|system  --no-gateway"),
    ]),
    ("顯示資訊 (show)", [
        ("show ip", "外網 IP 與電信商"),
        ("show net", "本機網路設定: DHCP/靜態, IP/遮罩, 閘道, 靜態路由, Sub IP"),
        ("show tools", "檢查各來源工具是否可用 (含下載網址)"),
        ("show config|targets|endpoints|theme|history|version", "各項目前狀態"),
        ("netinfo [iface] [--all] [-i] [--refresh]", "等同 show net (netinfo 直接可用)"),
    ]),
    ("對測 (pair)", [
        ("pair serve", "開對測伺服器 (優先 iperf3, 否則內建)"),
        ("pair client <host>", "連到對測端 (--mode both|down|up --duration --streams)"),
        ("pair history", "看對測歷史"),
    ]),
    ("設定 (config)", [
        ("config show|path|edit|reset|export|import", "設定檔 (可匯出/匯入分享)"),
        ("config set <key> <value>", "例: config set iperf.path /usr/local/bin/iperf3"),
        ("config get <key>", "讀取設定值"),
        ("config targets [list|add|del|set|gateway]", "ping 目標清單"),
        ("config endpoints [list|set <來源> <值>]", "測速端點設定"),
        ("config theme [list|set|reset|preset|export|import]", "顏色主題 (可自訂/匯出/匯入分享)"),
        ("history", "歷史紀錄, 可輸入編號直接重跑"),
    ]),
    ("互動模式 (Cisco 風格)", [
        ("console", "進入互動模式 (shell 亦可; 不帶參數也會倒數後跑預設)"),
        ("show / configure / write / end", "console 內: show <x>, configure 進設定模式, write 存檔"),
        ("refresh", "console 內: 清除本次快取 (netinfo / check / ip)"),
    ]),
    ("版本 / 安裝", [
        ("update [--reset-skip]", "檢查/更新到最新版本 (來源: update.repo)"),
        ("version", "顯示版本"),
        ("install [-y] | uninstall [--purge] [-y]", "安裝 / 移除"),
    ]),
]

COMMAND_ORDER = [
    "speed", "ping", "show", "pair", "config", "netinfo",
    "update", "console", "install", "uninstall", "help", "refresh", "exit",
    "configure", "write", "no", "end",
]

COMMAND_HELP = {
    "speed": "測速 (all/mlab/fast/cloudflare/ookla/libre)",
    "all": "全部來源 + ping + 外網 IP 統整報告",
    "mlab": "M-Lab NDT7 測速",
    "fast": "fast.com (Netflix) 測速",
    "cloudflare": "Cloudflare 測速 (內建)",
    "ookla": "Ookla speedtest.net 測速",
    "libre": "LibreSpeed 測速",
    "ping": "對目標 ping",
    "ip": "查外網 IP 與電信商",
    "check": "檢查各來源工具是否可用",
    "netinfo": "顯示本機網路設定",
    "pair": "對測 (serve / client / history)",
    "history": "歷史紀錄, 可選取重跑",
    "config": "設定檔 (show/path/edit/reset)",
    "set": "設定值 set <key> <value>",
    "get": "讀取設定值 get <key>",
    "targets": "ping 目標清單管理",
    "endpoints": "測速端點設定",
    "theme": "顏色主題 (可自訂/匯出/匯入)",
    "update": "檢查 / 更新版本",
    "version": "顯示版本",
    "console": "進入互動模式 (shell 亦可)",
    "install": "安裝並加入 PATH",
    "uninstall": "移除程式",
    "help": "顯示完整說明",
    "refresh": "清除本次快取 (netinfo / check / ip)",
    "exit": "離開 console",
    "show": "Cisco 風格: show <項目> (config/ip/tools/...)",
    "configure": "進入設定模式 (configure / conf)",
    "conf": "configure 的縮寫",
    "write": "設定模式: 儲存設定到檔案 (write / save)",
    "save": "write 的別名",
    "no": "設定模式: 還原某項為預設 (no <key>)",
    "end": "設定模式: 離開 (end / exit)",
}


def print_help() -> None:
    print(tcol("netspeed — 跨平台 CLI 網路測速工具箱", "title"))
    print()
    print("用法: " + col("netspeed <command> [args...]", "bold"))
    print("      " + col("netspeed", "bold") + "  (不帶參數 → 互動 console 模式; help 看指令, exit 離開)")
    for title, items in HELP_SECTIONS:
        print()
        print(tcol(f"【{title}】", "heading"))
        for name, desc in items:
            if name:
                print("  " + tcol(f"{name:<34}", "command") + desc)
            else:
                print("  " + " " * 34 + tcol(desc, "dim"))
    print()
    print(tcol("【通用選項】", "heading"))
    for name, desc in [("--json", "以 JSON 輸出"), ("--no-color", "關閉顏色"),
                       ("--config <path>", "指定設定檔"), ("--no-save", "不寫入歷史紀錄"),
                       ("--no-update-check", "本次不檢查更新")]:
        print("  " + tcol(f"{name:<34}", "command") + desc)


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

    p = argparse.ArgumentParser(prog="netspeed", description="跨平台 CLI 網路測速工具箱")
    p.add_argument("--no-color", action="store_true", help="關閉顏色")
    p.add_argument("--config", metavar="PATH", help="指定設定檔路徑")
    p.add_argument("--version", action="version", version=f"netspeed {VERSION}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    # 測速分層: speed <all|mlab|fast|cloudflare|ookla|libre>
    speed = sub.add_parser("speed", help="測速 (all / mlab / fast / cloudflare / ookla / libre)")
    speed_sub = speed.add_subparsers(dest="speed_cmd")
    s_all = speed_sub.add_parser("all", parents=[common], help="全部來源 + ping + 外網 IP")
    s_all.add_argument("--sources", help="要跑的來源, 逗號分隔")
    s_all.add_argument("--no-ping", action="store_true")
    s_all.add_argument("--no-ip", action="store_true")
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
    pc = pair_sub.add_parser("client", parents=[common], help="連到對測端")
    pc.add_argument("host")
    pc.add_argument("--port", type=int)
    pc.add_argument("--method", choices=["auto", "iperf3", "builtin"], default="auto")
    pc.add_argument("--mode", choices=["both", "down", "up"], default="both")
    pc.add_argument("--duration", type=int)
    pc.add_argument("--streams", type=int)
    pc.add_argument("--iperf-path")
    pair_sub.add_parser("history", parents=[common], help="對測歷史")

    ph = sub.add_parser("history", parents=[common], help="歷史紀錄")
    ph.add_argument("--kind", choices=["speed", "ping", "pair", "ip"])
    ph.add_argument("--limit", type=int, default=20)
    ph.add_argument("--run", type=int)

    # 設定分層: config <show|set|get|targets|endpoints|theme|path|edit|reset|export|import>
    cf = sub.add_parser("config", help="設定 (show/set/get/targets/endpoints/theme/...)")
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
        ok(f"已設定 {args.key} = {get_dotted(cfg, args.key)}")
    elif sub == "get":
        print(json.dumps(get_dotted(cfg, args.key), ensure_ascii=False))
    elif sub == "targets":
        cmd_targets(cfg, argparse.Namespace(action=args.action, values=args.values))
    elif sub == "endpoints":
        cmd_endpoints(cfg, argparse.Namespace(action=args.action, values=args.values))
    elif sub == "theme":
        cmd_theme(cfg, argparse.Namespace(action=args.action, values=args.values, **ns))
    else:
        warn(f"未知 config 子指令: {sub}")


def _dispatch(args, cfg: dict, parser) -> None:
    cmd = args.cmd
    if cmd == "all":
        cmd_all(cfg, args)
    elif cmd in ("mlab", "fast", "cloudflare", "ookla", "libre"):
        cmd_source(cmd, cfg, args)
    elif cmd == "speed":
        sc = getattr(args, "speed_cmd", None)
        if sc == "all":
            cmd_all(cfg, args)
        elif sc in ("mlab", "fast", "cloudflare", "ookla", "libre"):
            cmd_source(sc, cfg, args)
        else:
            parser.parse_args(["speed", "--help"])
    elif cmd == "ping":
        cmd_ping(cfg, args)
    elif cmd == "ip":
        cmd_ip(cfg, args)
    elif cmd == "check":
        cmd_check(cfg, args)
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
        ok(f"已設定 {args.key} = {get_dotted(cfg, args.key)}")
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
        ok("已清除本次快取 (netinfo / check / ip 將重新讀取)")
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
    global USE_COLOR
    argv = list(argv)
    if argv and argv[0].lower() in ALIAS_MAP:
        argv = ALIAS_MAP[argv[0].lower()] + argv[1:]
    if argv and argv[0].lower() == "show":
        mapped = _resolve_show(argv[1:])
        if mapped is None:
            _print_show_topics()
            return 0
        argv = mapped
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
    # Cisco 風格 (由 console / run_command 前置處理)
    "show", "configure", "conf", "write", "save", "no", "end",
}

# Cisco 風格: show <x> 對應到既有指令
SHOW_MAP = {
    "config": ["config"],
    "running-config": ["config"],
    "ip": ["ip"],
    "net": ["netinfo"],
    "interfaces": ["netinfo"],
    "tools": ["check"],
    "targets": ["targets"],
    "endpoints": ["endpoints"],
    "theme": ["theme"],
    "history": ["history"],
    "version": ["version"],
    "speed": ["speed", "all"],
}

# 平鋪別名 -> 分層指令 (仍可用, 但不列在 help)
ALIAS_MAP = {
    "all": ["speed", "all"],
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
    print(tcol("可用的 show 項目:", "heading"))
    for k in SHOW_MAP:
        print("  " + tcol(f"show {k:<18}", "command") + "→ " + " ".join(SHOW_MAP[k]))


# --------------------------------------------------------------------------- #
# console 輸入: ? 顯示候選, Tab 補齊, Enter 執行
# --------------------------------------------------------------------------- #


ARG_CANDIDATES = {
    "pair": ["serve", "client", "history"],
    "targets": ["list", "add", "del", "set", "gateway"],
    "endpoints": ["list", "set"],
    "config": ["show", "set", "get", "targets", "endpoints", "theme",
               "path", "edit", "reset", "export", "import"],
    "theme": ["list", "set", "reset", "preset", "export", "import"],
    "history": ["--kind", "--limit", "--run"],
    "update": ["--reset-skip"],
    "all": ["--sources", "--no-ping", "--no-ip", "--seconds"],
    "fast": ["--upload"],
    "cloudflare": ["--seconds"],
    "check": ["--refresh", "--json"],
    "install": ["--no-path", "-y"],
    "uninstall": ["--purge", "--no-path", "-y"],
    "show": list(SHOW_MAP.keys()),
    "speed": ["all", "mlab", "fast", "cloudflare", "ookla", "libre"],
}

PAIR_OPTS = {
    "serve": ["--host", "--port", "--method", "--iperf-path", "--duration"],
    "client": ["--port", "--method", "--mode", "--duration", "--streams", "--iperf-path"],
    "history": [],
}

ARG_HELP = {
    "serve": "開對測伺服器", "client": "連到對測端", "history": "對測歷史",
    "list": "列出", "add": "新增", "del": "刪除", "set": "設定", "gateway": "顯示 default gateway",
    "show": "顯示設定", "path": "顯示設定檔路徑", "edit": "用編輯器開啟", "reset": "重設為預設",
    "export": "匯出成檔案", "import": "從檔案匯入", "preset": "套用主題預設組",
    "--all": "顯示所有介面", "--refresh": "重新讀取 (不讀快取)", "--json": "JSON 輸出",
    "--sources": "指定來源", "--no-ping": "略過 ping", "--no-ip": "略過外網 IP",
    "--seconds": "秒數", "--upload": "加測上傳", "--kind": "篩選類型", "--limit": "筆數",
    "--run": "直接重跑編號", "--no-path": "不動 PATH", "-y": "不詢問",
    "--purge": "同時刪除設定", "--reset-skip": "清除不再通知版本",
}

_CONSOLE_CFG: dict | None = None


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
    if cmd == "ping":
        return _ping_candidates()
    return ARG_CANDIDATES.get(cmd, [])


def _candidates_for(buffer: str) -> list:
    if " " not in buffer:
        t = buffer.lower()
        return [c for c in COMMAND_ORDER if c.startswith(t)]
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


def _print_candidates(prompt: str, text: str) -> None:
    cands = _candidates_for(text)
    at_arg = " " in text
    sys.stdout.write("\r\n")
    if cands:
        for c in cands:
            desc = (ARG_HELP if at_arg else COMMAND_HELP).get(c, "")
            sys.stdout.write("  " + tcol(f"{c:<16}", "command") + desc + "\r\n")
    sys.stdout.write(prompt + text)
    sys.stdout.flush()


def _tab_complete(prompt: str, text: str) -> str:
    if " " not in text:
        cands = [c for c in COMMAND_ORDER if c.startswith(text.lower())]
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
        ok("已儲存設定 (write)")
        return None
    if cmd == "set":
        if len(tokens) < 3:
            error("用法: set <key> <value>")
            return None
        set_dotted(cfg, tokens[1], coerce(" ".join(tokens[2:])))
        apply_theme(cfg)
        state["dirty"] = True
        ok(f"set {tokens[1]} = {get_dotted(cfg, tokens[1])}   (尚未 write)")
        return None
    if cmd == "no":
        if len(tokens) < 2:
            error("用法: no <key>")
            return None
        if tokens[1] not in _config_keys():
            warn(f"未知 key: {tokens[1]}")
            return None
        set_dotted(cfg, tokens[1], get_dotted(DEFAULT_CONFIG, tokens[1]))
        apply_theme(cfg)
        state["dirty"] = True
        ok(f"no {tokens[1]}   (尚未 write)")
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
    error("設定模式不支援此指令 (可用: set / no / endpoints / targets / theme / show / write / end)")
    return None


def console(start_in_config: bool = False) -> int:
    global USE_COLOR, _CONSOLE_CFG, _DEFER_SAVE
    if not sys.stdout.isatty():
        USE_COLOR = False
    cfg = load_config()
    _CONSOLE_CFG = cfg
    apply_theme(cfg)
    history: list = []
    in_config = start_in_config
    _DEFER_SAVE = start_in_config
    state = {"dirty": False}
    print(tcol("netspeed 互動模式", "title"))
    print(tcol("輸入 help 看指令; ? 候選, Tab 補齊, ↑↓ 歷史, ←→ 移動游標; exit 離開。", "dim"))
    print(tcol(f"設定檔: {config_path()}", "dim"))
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
            error(f"無法解析: {exc}")
            continue
        if not tokens:
            continue
        first = tokens[0].lower()

        if in_config:
            result = _config_mode_cmd(tokens, cfg, state)
            if result == "exit":
                _DEFER_SAVE = False
                in_config = False
                if state["dirty"]:
                    warn("已離開設定模式; 有變更尚未 write, 下次執行仍為舊設定")
                    state["dirty"] = False
            continue

        if first in ("exit", "quit", "q"):
            break
        if first == "refresh":
            cache_clear()
            ok("已清除本次快取 (netinfo / check / ip 將重新讀取)")
            continue
        if first in ("configure", "conf"):
            _DEFER_SAVE = True
            in_config = True
            state["dirty"] = False
            ok("進入設定模式。變更即時套用; 需 write 才會寫入檔案。end 離開。")
            continue
        if first in ("write", "save"):
            save_config(cfg, force=True)
            ok("已儲存設定 (write)")
            continue
        if first == "no":
            error("no 僅能在設定模式使用 (先輸入 configure)")
            continue
        if first == "end":
            warn("目前不在設定模式")
            continue
        if first not in COMMANDS:
            cands = _candidates_for(first)
            if cands and len(tokens) == 1:
                print()
                for c in cands:
                    print("  " + tcol(f"{c:<16}", "command") + COMMAND_HELP.get(c, ""))
                continue
            error(f"未知指令: {tokens[0]}  (輸入 help 查看可用指令)")
            continue
        run_command(tokens, cfg)
    if in_config:
        _DEFER_SAVE = False
    print("bye")
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

    if os.name == "nt":
        import msvcrt
        for i in range(seconds, 0, -1):
            sys.stdout.write(f"\r  倒數 {i} 秒... ")
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
        sys.stdout.write("\r" + " " * 24 + "\r")
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
            sys.stdout.write(f"\r  倒數 {i} 秒... ")
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
        sys.stdout.write("\r" + " " * 24 + "\r")
        sys.stdout.flush()
        return "run"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def run_default_with_countdown(seconds: int = 5) -> int:
    cfg = load_config()
    apply_theme(cfg)
    try:
        check_update(cfg, interactive=True)
    except Exception:
        pass
    sources = cfg.get("sources", DEFAULT_CONFIG["sources"])
    section("預設測速")
    print("  將以預設設定執行:")
    print("    外網 IP / 電信商 + Ping + 測速來源: "
          + ", ".join(SOURCE_LABELS.get(s, s) for s in sources))
    print(tcol("  倒數中可按鍵: [c] 進入互動模式 / [其他鍵] 取消 / 不按鍵則倒數後執行", "dim"))
    print()
    action = _countdown_wait(seconds)
    if action == "console":
        print("進入互動模式。")
        return console()
    if action == "cancel":
        print("已取消。")
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
