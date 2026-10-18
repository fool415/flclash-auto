#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FlClash 按需代理启动器
======================
- 先检测直连是否可用:可用则直接执行命令(直连本身就是最快路径)
- 直连不通时自动启动 FlClash,通过 Clash API 并发测速全部节点,自动切换到最快节点
  (global 模式下若 GLOBAL 组直接挂的是子分组,会沿 Selector 链展开测速并逐级切换)
- 命令执行完毕后,若 FlClash 是本工具启动的,按 PID 精确关闭并恢复系统代理状态;
  若 FlClash 原本就在运行,则复用且不做关闭

用法示例:
  python flclash_proxy.py curl -sI https://www.google.com   # 自动模式
  python flclash_proxy.py --test                            # 测速全部节点
  python flclash_proxy.py --status                          # 查看当前状态
  python flclash_proxy.py --start                           # 启动并选最快节点(保持运行)
  python flclash_proxy.py --stop                            # 强制停止 FlClash
  python flclash_proxy.py --buy                             # 打开订阅购买/注册页面(节点需自备)
  python flclash_proxy.py --keep git pull                   # 命令结束后保持 FlClash 运行
  python flclash_proxy.py --force pip install xxx           # 跳过直连检测,强制走代理
  python flclash_proxy.py --direct git pull                 # 强制直连
  python flclash_proxy.py --timeout-ms 8000 git pull        # 放宽节点测速超时

环境变量(可选):FLCLASH_EXE / FLCLASH_API / FLCLASH_SECRET / FLCLASH_PORT /
               FLCLASH_CANARY / FLCLASH_TESTURL / FLCLASH_START_TIMEOUT /
               FLCLASH_DELAY_MS / FLCLASH_DEBUG(设为 1 输出 API 调试日志) /
               FLCLASH_BUY_URL(订阅购买地址,默认 https://114514-sakuracat.com) /
               FLCLASH_INVITE_CODE(邀请码,设置后 --buy 打开带 ?code= 的注册链接)
"""

import argparse
import csv
import ctypes
import io
import json
import msvcrt
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import winreg
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------- 可配置项(环境变量可覆盖) ----------------
FLCLASH_EXE = os.environ.get("FLCLASH_EXE", r"C:\Program Files\FlClash\FlClash.exe")
API_BASE = os.environ.get("FLCLASH_API", "http://127.0.0.1:9090").rstrip("/")
API_SECRET = os.environ.get("FLCLASH_SECRET", "")
PROXY_PORT = int(os.environ.get("FLCLASH_PORT", "7890"))
CANARY = os.environ.get("FLCLASH_CANARY", "https://www.google.com/generate_204")
TEST_URL = os.environ.get("FLCLASH_TESTURL", "https://www.gstatic.com/generate_204")
START_TIMEOUT = int(os.environ.get("FLCLASH_START_TIMEOUT", "45"))
DELAY_TIMEOUT_MS = int(os.environ.get("FLCLASH_DELAY_MS", "3000"))
DEBUG = bool(os.environ.get("FLCLASH_DEBUG"))
# 订阅购买入口:本工具不提供节点,节点/订阅由用户自备
BUY_URL = os.environ.get("FLCLASH_BUY_URL", "https://114514-sakuracat.com").rstrip("/")
INVITE_CODE = os.environ.get("FLCLASH_INVITE_CODE", "").strip()

GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance"}
SELECTABLE_TYPES = {"Selector"}  # 只有 Selector 支持通过 API 手动切换
BUILTIN_TYPES = GROUP_TYPES | {"Direct", "Reject", "RejectDrop", "PassRule", "Compatible", "Pass"}
FLCLASH_PROC_NAMES = ("FlClash.exe", "FlClashCore.exe")
# 订阅面板常见的"信息节点"(剩余流量/到期时间等),不参与测速
INFO_NODE_PAT = re.compile(r"剩余流量|套餐到期|到期时间|流量重置|官网|过期时间|群组|下次重置|剩余天数|距离下次")

REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_REFRESH = 37
# 跨实例引用计数锁文件:多个 px 并发时,只有最后一个退出的实例才允许关闭 FlClash
LOCK_FILE = os.path.join(tempfile.gettempdir(), "flclash_proxy.lock")


def _u8():
    """输出编码:真控制台按系统代码页(原生 WriteConsoleW 输出,不受影响),
    管道/重定向按 UTF-8(Git Bash/现代工具惯例);统一 replace 防 emoji 崩溃并逐行刷新"""
    for s in (sys.stdout, sys.stderr):
        try:
            if s.isatty():
                s.reconfigure(errors="replace", line_buffering=True)
            else:
                s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass


def _dbg(msg):
    if DEBUG:
        print(msg, file=sys.stderr)


# ---------------- Clash 外部控制 API ----------------
# 控制地址是本地回环,显式绕过任何代理(含系统代理),不受用户代理环境影响
_API_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


_SECRET_WARNED = False


def api(method, path, data=None, timeout=5):
    """调用 Clash API;失败返回 None(设 FLCLASH_DEBUG=1 可在 stderr 看到失败原因)"""
    global _SECRET_WARNED
    req = urllib.request.Request(API_BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if API_SECRET:
        req.add_header("Authorization", "Bearer " + API_SECRET)
    body = json.dumps(data).encode() if data is not None else None
    try:
        with _API_OPENER.open(req, body, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
            return json.loads(text) if text.strip() else {}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and not _SECRET_WARNED:
            _SECRET_WARNED = True
            print("!! 控制返回 HTTP %d:外部控制可能设置了访问密钥,"
                  "请通过环境变量 FLCLASH_SECRET 指定" % e.code, file=sys.stderr)
        _dbg("!! [debug] %s %s -> HTTP %s %s" % (method, path, e.code, e.reason))
        return None
    except Exception as e:
        _dbg("!! [debug] %s %s -> %s: %s" % (method, path, type(e).__name__, e))
        return None


def api_port():
    p = urllib.parse.urlparse(API_BASE)
    return p.port or (443 if p.scheme == "https" else 80)


def core_up(timeout=2):
    """核心是否在运行(以外部控制 API 是否应答为准)"""
    v = api("GET", "/version", timeout=timeout)
    return v if isinstance(v, dict) and "version" in v else None


def port_open(port, host="127.0.0.1", timeout=0.6):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _tasklist_rows():
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    return [row for row in csv.reader(io.StringIO(out)) if len(row) > 1]


def snapshot_pids():
    """当前 FlClash 相关进程的 PID 集合(用于精确启停,避免误杀其他同名实例)"""
    pids = set()
    for row in _tasklist_rows():
        if row[0] in FLCLASH_PROC_NAMES:
            try:
                pids.add(int(row[1]))
            except ValueError:
                pass
    return pids


def procs_by_name():
    """按进程名归类当前 FlClash 相关进程"""
    d = {}
    for row in _tasklist_rows():
        if row[0] in FLCLASH_PROC_NAMES:
            try:
                d.setdefault(row[0], []).append(int(row[1]))
            except ValueError:
                pass
    return d


def flclash_procs_running():
    """任何 FlClash 相关进程(GUI 或核心)是否存在"""
    return bool(snapshot_pids())


def fmt_pids(pids):
    return ",".join(str(p) for p in sorted(pids or [])) or "?"


# ---------------- 跨实例引用计数(防并发 px 互杀 FlClash) ----------------
def pid_alive(pid):
    """校验 PID 是否存活(用于清理崩溃实例在锁文件中的残留)。
    注:极端情况下 PID 被系统回收复用会误判存活,后果只是延后一次自动关闭,可接受。"""
    if not pid or pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def _lock_fail_hint():
    print("!! 锁文件(%s)访问异常,按保守策略处理:本次不会自动关闭 FlClash,"
          "需要时可手动执行 --stop" % LOCK_FILE, file=sys.stderr)


def _holders_update(add):
    """登记/注销本进程持有者身份,返回更新后的存活持有者 PID 集合。
    msvcrt 文件锁保证跨进程原子;读取时自动剔除已退出的残留 PID。"""
    holders = {os.getpid()}  # 锁异常时保守按"自己仍在"处理,避免误判可关闭
    try:
        with open(LOCK_FILE, "a+") as f:
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            except OSError:
                _lock_fail_hint()
                return holders
            try:
                f.seek(0)
                raw = f.read().strip()
                try:
                    holders = {p for p in json.loads(raw) if pid_alive(p)} if raw else set()
                except Exception:
                    holders = set()
                if add:
                    holders.add(os.getpid())
                else:
                    holders.discard(os.getpid())
                f.seek(0)
                f.truncate()
                f.write(json.dumps(sorted(holders)))
                f.flush()
                return set(holders)
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        _lock_fail_hint()
        return {os.getpid()}


def holders_acquire():
    return _holders_update(add=True)


def holders_release():
    return _holders_update(add=False)


def check_port_conflict():
    """7890 被非 FlClash 程序占用时提醒(端口在监听但控制 API 无响应)。
    返回 False 表示应中止;设 FLCLASH_ABORT_ON_PORT_CONFLICT=1 时占用即中止。"""
    if port_open(PROXY_PORT) and not core_up():
        print("!! 警告:端口 %d 已被非 FlClash 进程监听,命令的代理流量会导向该未知程序" % PROXY_PORT)
        if os.environ.get("FLCLASH_ABORT_ON_PORT_CONFLICT"):
            print("!! 已按 FLCLASH_ABORT_ON_PORT_CONFLICT=1 中止执行")
            return False
    return True


# ---------------- 直连检测 ----------------
# 显式绕过任何代理(含系统代理)的 opener,保证测的是真正的直连
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def direct_ok(url=CANARY, timeout=4):
    """直连是否可用。
    对 generate_204 类 canary 严格要求 204,其余 URL 要求 2xx,
    避免 GFW 劫持/DNS 污染返回的假 302/404/200 被误判为直连可用。"""
    strict_204 = urllib.parse.urlparse(url).path.endswith("generate_204")

    def acceptable(code):
        return code == 204 if strict_204 else 200 <= code < 300

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "flclash-proxy-check/1.0")
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
            return acceptable(r.status)
    except urllib.error.HTTPError as e:
        return acceptable(e.code)
    except Exception as e:
        _dbg("!! [debug] 直连检测 %s -> %s: %s" % (url, type(e).__name__, e))
        return False


# ---------------- 系统代理注册表(winreg 直读直写,无子进程开销/编码问题) ----------------
def reg_get(name):
    """读 HKCU 系统代理键值;不存在返回 None"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0,
                            winreg.KEY_READ) as k:
            value, _ = winreg.QueryValueEx(k, name)
            return value
    except OSError:
        return None


def reg_set(name, value):
    """写键值;int 映射 REG_DWORD,str 映射 REG_SZ"""
    vtype = winreg.REG_SZ if isinstance(value, str) else winreg.REG_DWORD
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, vtype, value)
    except OSError:
        pass


def reg_delete(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


def refresh_system_proxy():
    """通知 Windows 立即应用代理设置变更"""
    try:
        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(0, INTERNET_OPTION_SETTINGS_CHANGED, 0, 0)
        wininet.InternetSetOptionW(0, INTERNET_OPTION_REFRESH, 0, 0)
    except Exception:
        pass


def fix_system_proxy_after_stop():
    """FlClash 停止后,若系统代理仍指向本地代理端口则关闭,避免全系统断网"""
    try:
        if reg_get("ProxyEnable") == 1:
            server = reg_get("ProxyServer") or ""
            if isinstance(server, str) and ("127.0.0.1:%d" % PROXY_PORT) in server \
                    and not port_open(PROXY_PORT):
                reg_set("ProxyEnable", 0)
                refresh_system_proxy()
    except Exception:
        pass


def proxy_snapshot():
    """记录系统代理注册表快照(启动 FlClash 前调用,停止后还原,保护用户原有代理设置)"""
    try:
        return (reg_get("ProxyEnable"), reg_get("ProxyServer"), reg_get("ProxyOverride"))
    except Exception:
        return None


def restore_system_proxy(snap):
    """把系统代理注册表恢复到 FlClash 启动前的快照;快照里没有的值删除还原"""
    if not snap:
        return
    enable, server, override = snap
    try:
        if enable is not None:
            reg_set("ProxyEnable", enable)
        if server is not None:
            reg_set("ProxyServer", server)
        else:
            reg_delete("ProxyServer")
        if override is not None:
            reg_set("ProxyOverride", override)
        else:
            reg_delete("ProxyOverride")
        refresh_system_proxy()
    except Exception:
        pass


# ---------------- 启动 / 停止 FlClash ----------------
def _script_dir():
    """本脚本所在目录(便携版探测基准)"""
    return os.path.dirname(os.path.abspath(__file__))


def find_flclash_exe():
    """按优先级查找 FlClash 可执行文件:
    环境变量 FLCLASH_EXE → 脚本同目录便携版 → 常见安装位置"""
    here = _script_dir()
    candidates = [
        FLCLASH_EXE,
        os.path.join(here, "FlClash.exe"),                # 便携版与本脚本同目录
        os.path.join(here, "FlClash", "FlClash.exe"),     # 便携版解压到 FlClash/ 子目录
        os.path.expandvars(r"%ProgramFiles%\FlClash\FlClash.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\FlClash\FlClash.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\FlClash\FlClash.exe"),
    ]
    # 兼容「FlClash-<版本>-windows-<arch>」这类保留解压目录名的布局(仅探测一层子目录)
    try:
        for entry in os.listdir(here):
            if entry.lower().startswith("flclash-"):
                sub = os.path.join(here, entry)
                if os.path.isdir(sub):
                    candidates.append(os.path.join(sub, "FlClash.exe"))
    except OSError:
        pass
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def start_flclash():
    exe = find_flclash_exe()
    if not exe:
        print("!! 找不到 FlClash(可用环境变量 FLCLASH_EXE 指定路径)")
        return False
    env = os.environ.copy()
    # 该变量会让 GUI 子进程被当成纯 Node 运行而静默退出,必须剔除
    env.pop("ELECTRON_RUN_AS_NODE", None)
    proc = subprocess.Popen(
        [exe], cwd=os.path.dirname(exe), env=env, close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    t0 = time.time()
    while time.time() - t0 < START_TIMEOUT:
        if core_up():
            return True
        if proc.poll() is not None:
            print("!! FlClash 启动后立即退出(退出码 %s)" % proc.returncode)
            return False
        time.sleep(0.5)
    return False


def stop_flclash(our_pids=None):
    """停止 FlClash。
    给定 our_pids 时只结束这些 PID(本工具启动的实例,精确无误杀);
    否则按进程名结束全部实例(--stop 手动模式,用户明确要求全停)。"""
    if our_pids:
        for pid in sorted(our_pids):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True, errors="replace")
    else:
        for exe in FLCLASH_PROC_NAMES:
            subprocess.run(["taskkill", "/F", "/T", "/IM", exe],
                           capture_output=True, text=True, errors="replace")
    t0 = time.time()
    while time.time() - t0 < 10:
        if not port_open(PROXY_PORT) and not port_open(api_port()):
            break
        time.sleep(0.5)
    ports_closed = not port_open(PROXY_PORT) and not port_open(api_port())
    fix_system_proxy_after_stop()
    leftover = bool(our_pids and (snapshot_pids() & set(our_pids)))
    return ports_closed and not leftover


def ensure_inbound():
    """确保本地混合代理端口已开启(核心可能以 mixed-port=0 启动,如用户在界面关闭了系统代理)。
    先给 7890 一小段自然就绪时间(9090 可应答后 7890 可能略有延迟),仍未监听再 PATCH 打开。
    返回 (是否可用, 是否由本工具打开);复用实例时打开的端口,结束后应还原。"""
    t0 = time.time()
    while time.time() - t0 < 3:
        if port_open(PROXY_PORT):
            return True, False
        time.sleep(0.3)
    api("PATCH", "/configs", {"mixed-port": PROXY_PORT})
    time.sleep(0.8)
    if port_open(PROXY_PORT):
        print("✓ 已开启本地代理端口 %d(核心此前未监听)" % PROXY_PORT)
        return True, True
    print("!! 核心未监听本地代理端口 %d,且 PATCH /configs 未能打开" % PROXY_PORT)
    return False, False


def ensure_core():
    """确保核心在运行。返回 (是否就绪, 是否由本工具启动, 本工具启动的 PID 集合)"""
    if core_up():
        print("→ FlClash 已在运行,复用现有实例")
        return True, False, None
    procs = procs_by_name()
    if procs:
        if procs.get("FlClashCore.exe") and not procs.get("FlClash.exe"):
            # 只有核心进程、无 GUI 且 API 无响应:多半是残留/卡死的核心,快速失败
            time.sleep(2)
            if core_up():
                print("→ FlClash 已在运行,复用现有实例")
                return True, False, None
            print("!! 核心进程存在但控制 API 无响应(可能已卡死),建议执行 --stop 清理后重试")
            return False, False, None
        # GUI 已开但核心未就绪(启动竞态):等待而不是再拉起第二个实例
        print("→ 检测到 FlClash 界面已在运行,等待其核心就绪 ...")
        t0 = time.time()
        while time.time() - t0 < START_TIMEOUT:
            if core_up():
                print("✓ 核心已就绪,复用现有实例")
                return True, False, None
            time.sleep(0.5)
        print("!! FlClash 界面在运行但核心一直未就绪(请在 FlClash 界面确认内核已启动)")
        return False, False, None
    before = snapshot_pids()
    print("→ FlClash 未运行,正在启动 ...")
    if not start_flclash():
        return False, False, None
    our_pids = snapshot_pids() - before
    print("✓ FlClash 已启动 (pid: %s)" % fmt_pids(our_pids))
    return True, True, our_pids


# ---------------- 测速与节点选择 ----------------
def get_proxies():
    return (api("GET", "/proxies") or {}).get("proxies", {})


def get_mode():
    """运行模式;API 失败返回 None 由调用方决定降级策略"""
    return (api("GET", "/configs") or {}).get("mode")


def pick_target_group(proxies, mode, override=None):
    """选定目标出口组:--group 指定 > global 模式取 GLOBAL > rule 模式按组名评分取主选择组"""
    if override:
        return override
    if mode == "global":
        return "GLOBAL"
    selectors = [k for k, v in proxies.items()
                 if v.get("type") == "Selector" and k != "GLOBAL"]

    def score(k):
        kl = k.lower()
        if "自动" in k or "auto" in kl:
            return -1  # 自动组自行管理,不手动切
        s = 0
        if "节点选择" in k:
            s += 100
        if "手动" in k:
            s += 50
        if "proxy" in kl or "代理" in k:
            s += 30
        if "选择" in k or "select" in kl:
            s += 10
        return s

    scored = sorted(((score(k), k) for k in selectors), reverse=True)
    scored = [(s, k) for s, k in scored if s >= 0]
    return scored[0][1] if scored else "GLOBAL"


def collect_candidates(proxies, root):
    """自 root 组沿 Selector 链(可手动切换的组)向下展开,返回
    {节点名: 选择链},选择链 = [root, ..., 节点直属组],用于逐级 PUT 切换。
    覆盖 global 模式下 GLOBAL 直接挂子分组、节点藏在多层组里的情况。"""
    result = {}
    visited = {root}
    queue = deque([(root, [root])])
    while queue:
        g, chain = queue.popleft()
        for m in proxies.get(g, {}).get("all", []):
            info = proxies.get(m)
            if not info:
                continue
            t = info.get("type")
            if t in SELECTABLE_TYPES:
                if m not in visited:
                    visited.add(m)
                    queue.append((m, chain + [m]))
            elif t not in BUILTIN_TYPES and not INFO_NODE_PAT.search(m):
                old = result.get(m)
                if old is None or len(chain) < len(old):
                    result[m] = chain
    return result


def auto_group_in(proxies, root):
    for m in proxies.get(root, {}).get("all", []):
        if proxies.get(m, {}).get("type") in ("URLTest", "Fallback"):
            return m
    return None


def test_node(name, timeout_ms):
    q = urllib.parse.quote(name, safe="")
    path = "/proxies/%s/delay?timeout=%d&url=%s" % (
        q, timeout_ms, urllib.parse.quote(TEST_URL, safe=""))
    r = api("GET", path, timeout=timeout_ms / 1000 + 4)
    if isinstance(r, dict) and r.get("delay"):
        return name, r["delay"]
    return name, None


def select_fastest(timeout_ms=DELAY_TIMEOUT_MS, group_override=None):
    """并发测速目标组可达的全部节点,沿选择链自动切换到延迟最低的节点"""
    proxies = get_proxies()
    if not proxies:
        print("!! 无法获取代理信息(外部控制 API 无响应)")
        return None
    mode = get_mode()
    if mode is None:
        print("!! 无法确认运行模式(获取 /configs 失败),按 rule 模式处理")
        mode = "rule"
    root = pick_target_group(proxies, mode, group_override)
    if root not in proxies:
        print("!! 分组 [%s] 不存在(用 --group 指定其他分组)" % root)
        return None

    cands = collect_candidates(proxies, root)
    if not cands:
        if real_node_count(proxies) == 0:
            print_buy_hint("当前配置里没有任何节点(可能还没购买/导入订阅)")
        # 兜底:global 模式下无可手动切换的节点时,切到自动测速组(URLTest 自选最快)
        if mode == "global":
            auto = auto_group_in(proxies, root)
            if auto:
                r = api("PUT", "/proxies/%s" % urllib.parse.quote(root, safe=""),
                        {"name": auto})
                if r is not None:
                    print("✓ [%s] 无可手动切换的节点,已切换到自动测速组 [%s]" % (root, auto))
                    return auto
        print("!! 分组 [%s] 下没有可测速的节点" % root)
        return None

    print("→ 测速 %d 个节点(分组: %s,模式: %s)..." % (len(cands), root, mode))
    results = {}
    with ThreadPoolExecutor(max_workers=min(16, len(cands))) as ex:
        futs = [ex.submit(test_node, n, timeout_ms) for n in cands]
        for f in as_completed(futs):
            n, d = f.result()
            results[n] = d

    ok = sorted((d, n) for n, d in results.items() if d)
    for d, n in ok:
        via = "" if len(cands[n]) <= 1 else "(经 %s)" % " → ".join(cands[n])
        print("   %5d ms  %s %s" % (d, n, via))
    for n in sorted(results):
        if not results[n]:
            print("   超时/失败  %s" % n)

    if not ok:
        print("!! 所有节点测速失败")
        return None
    best_delay, best = ok[0]
    seq = cands[best] + [best]  # root → ... → 直属组 → 节点
    for i in range(len(seq) - 1):
        if api("PUT", "/proxies/%s" % urllib.parse.quote(seq[i], safe=""),
               {"name": seq[i + 1]}) is None:
            print("!! 切换节点失败(可设置 FLCLASH_DEBUG=1 查看原因)")
            return None
    print("✓ 最快节点: %s(%d ms)→ 切换路径: %s" % (best, best_delay, " → ".join(seq)))
    return best


# ---------------- 订阅购买指引(本工具不提供节点) ----------------
def buy_url():
    """购买/注册地址;设置 FLCLASH_INVITE_CODE 时带上邀请码(注册页从 ?code= 读取)"""
    if INVITE_CODE:
        return "%s/register?code=%s" % (BUY_URL, urllib.parse.quote(INVITE_CODE))
    return BUY_URL


def real_node_count(proxies):
    """真实节点数(排除分组本身与"剩余流量/到期时间"这类信息节点)"""
    return sum(1 for name, info in (proxies or {}).items()
               if info.get("type") not in BUILTIN_TYPES and not INFO_NODE_PAT.search(name))


def print_buy_hint(reason=None):
    """没有可用节点时给出购买/导入指引;只在确实缺节点时输出,不打扰正常使用"""
    print("-- 没有可用的代理节点?本工具只负责调度 FlClash,节点/订阅需要自备")
    if reason:
        print("   原因: %s" % reason)
    print("   订阅服务(第三方,非本项目运营,购买前请自行评估): %s" % buy_url())
    print("   购买后: 复制订阅链接 → FlClash「配置」→ 添加订阅 → 更新 → 再执行本命令")


def cmd_buy():
    import webbrowser  # 只有 --buy 需要,延迟导入
    url = buy_url()
    print("订阅购买/注册地址: %s" % url)
    print("步骤: 1) 注册并购买套餐  2) 复制订阅链接  3) FlClash「配置」→ 添加订阅 → 更新")
    print("提示: 账号/套餐/支付/退款均由该订阅服务提供,本工具只给出入口,不保存任何账号信息")
    if INVITE_CODE:
        print("邀请码: %s(已包含在上面的链接里)" % INVITE_CODE)
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        print("!! 未能自动打开浏览器,请手动访问上面的地址")
    return 0


# ---------------- 命令执行 ----------------
def run_cmd(cmd, use_proxy):
    env = os.environ.copy()
    if use_proxy:
        p = "http://127.0.0.1:%d" % PROXY_PORT
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            env[k] = p
        # 大小写两个变量统一为同一集合,避免不同工具读到不一致的绕过列表
        base = env.get("NO_PROXY") or env.get("no_proxy") or "localhost,127.0.0.1,::1"
        env["NO_PROXY"] = env["no_proxy"] = base
    if use_proxy:
        tag = "  [经 127.0.0.1:%d]" % PROXY_PORT
    else:
        tag = "  [直连]"
    print("→ $ %s%s" % (subprocess.list2cmdline(cmd), tag))
    try:
        proc = subprocess.Popen(cmd, env=env)
    except FileNotFoundError:
        print("!! 找不到命令: %s" % cmd[0])
        return 127
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # 中断时确保用户命令的子进程一并结束,再交由外层 finally 关闭 FlClash
        print("\n!! 已中断,结束子进程 ...")
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise


def do_run(cmd, force=False, direct=False, no_speedtest=False,
           keep=False, group=None, canary=None, timeout_ms=DELAY_TIMEOUT_MS,
           strict=False):
    our_pids = None
    started_by_us = False
    snap = None
    registered = False
    remaining = None
    inbound_opened = False
    try:
        if direct:
            print("→ 强制直连模式")
            return run_cmd(cmd, use_proxy=False)
        if not force:
            url = canary or CANARY
            if direct_ok(url):
                print("→ 直连可用(%s 可达),无需代理" % url)
                return run_cmd(cmd, use_proxy=False)
            print("→ 直连失败(%s 不可达),需要代理" % url)
        if not check_port_conflict():
            return 1
        snap = proxy_snapshot()  # 在可能启动 FlClash 之前记录系统代理原始状态
        registered = True
        holders = holders_acquire()
        if len(holders) > 1:
            print("→ 当前有 %d 个 px 实例共用 FlClash" % len(holders))
        ready, started_by_us, our_pids = ensure_core()
        if not ready:
            return 1
        inbound_ok, inbound_opened = ensure_inbound()
        if not inbound_ok:
            return 1
        if not no_speedtest:
            if select_fastest(timeout_ms=timeout_ms, group_override=group) is None:
                if strict:
                    print("!! --strict:测速/切换失败,中止命令")
                    return 1
                print("→ 测速/切换未成功,将以当前节点继续执行")
        return run_cmd(cmd, use_proxy=True)
    finally:
        if registered:
            remaining = holders_release()
        if started_by_us and not keep:
            if remaining:
                print("→ 其他 px 实例仍在使用,FlClash 保持运行(剩余 %d 个)" % len(remaining))
            else:
                print("→ 命令结束,关闭本工具启动的 FlClash (pid: %s)..." % fmt_pids(our_pids))
                closed = stop_flclash(our_pids)
                print("✓ FlClash 已关闭" if closed else
                      "!! 部分 FlClash 进程仍存活(可手动执行 --stop 结束)")
                restore_system_proxy(snap)
        elif inbound_opened and not keep and not remaining:
            # 复用实例时临时打开的 7890,结束后还原为关闭状态(不影响仍在使用的其他实例)
            api("PATCH", "/configs", {"mixed-port": 0})
            print("→ 已还原复用实例的本地代理端口状态(mixed-port=0)")


# ---------------- 子命令 ----------------
def cmd_status():
    pids = snapshot_pids()
    print("FlClash.exe : %s (pid: %s)" % ("运行中" if pids else "未运行", fmt_pids(pids)))
    v = core_up()
    if v:
        print("核心 API    : %s (clash.meta %s)" % (API_BASE, v.get("version")))
        print("代理端口    : 127.0.0.1:%d %s" % (
            PROXY_PORT, "监听中" if port_open(PROXY_PORT) else "未监听"))
        mode = get_mode() or "?"
        proxies = get_proxies()
        g = pick_target_group(proxies, mode)
        now = (proxies.get(g) or {}).get("now", "?")
        print("模式/当前   : %s / [%s] → %s" % (mode, g, now))
        nodes = real_node_count(proxies)
        print("节点数量    : %d" % nodes)
        print("直连检测    : %s" % ("可用(无需代理)" if direct_ok() else "不可用(需要代理)"))
        if nodes == 0:
            print_buy_hint("当前配置里没有任何节点(可能还没购买/导入订阅)")
    else:
        print("核心 API    : 无响应(%s)" % API_BASE)
    try:
        pe = reg_get("ProxyEnable")
        ps = reg_get("ProxyServer") or "(未设置)"
        print("系统代理    : %s%s" % ("开启" if pe == 1 else "关闭",
                                      " → " + ps if pe == 1 else ""))
    except Exception:
        pass


def cmd_test(group=None, timeout_ms=DELAY_TIMEOUT_MS):
    registered = False
    started_by_us = False
    our_pids = None
    snap = None
    try:
        snap = proxy_snapshot()  # 与 do_run 对齐:自启场景关闭后还原系统代理
        registered = True
        holders = holders_acquire()
        if len(holders) > 1:
            print("→ 当前有 %d 个 px 实例共用 FlClash" % len(holders))
        ready, started_by_us, our_pids = ensure_core()
        if not ready:
            return 1
        ok = bool(select_fastest(timeout_ms=timeout_ms, group_override=group))
        return 0 if ok else 1
    finally:
        remaining = holders_release() if registered else None
        if started_by_us:
            if remaining:
                print("→ 其他 px 实例仍在使用,FlClash 保持运行(剩余 %d 个)" % len(remaining))
            else:
                print("→ 测速完成,关闭本工具启动的 FlClash (pid: %s)..." % fmt_pids(our_pids))
                print("✓ FlClash 已关闭" if stop_flclash(our_pids) else
                      "!! 未能完全停止(见上方提示)")
                restore_system_proxy(snap)


def cmd_start(group=None, no_speedtest=False, timeout_ms=DELAY_TIMEOUT_MS):
    ready, started, _ = ensure_core()
    if not ready:
        return 1
    ensure_inbound()
    if not no_speedtest:
        select_fastest(timeout_ms=timeout_ms, group_override=group)
    # FlClash 自身会按其设置接管系统代理;若未接管则补开,保持可用
    time.sleep(1.5)
    if port_open(PROXY_PORT) and reg_get("ProxyEnable") != 1:
        reg_set("ProxyServer", "127.0.0.1:%d" % PROXY_PORT)
        reg_set("ProxyEnable", 1)
        refresh_system_proxy()
    print("✓ FlClash 运行中(代理 127.0.0.1:%d),手动模式不会自动关闭" % PROXY_PORT)
    return 0


def cmd_stop():
    if not snapshot_pids() and not core_up():
        print("→ FlClash 本来就未在运行")
        return 0
    print("→ 正在停止 FlClash(全部实例)...")
    return 0 if stop_flclash(None) else 1


def main():
    _u8()
    p = argparse.ArgumentParser(
        prog="flclash_proxy",
        description="FlClash 按需代理:检测直连 → 需要时自动启动 FlClash 并测速选最快节点 → 命令结束自动关闭",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:  python flclash_proxy.py curl -sI https://www.google.com")
    p.add_argument("--test", action="store_true", help="测速全部节点并显示(不执行命令)")
    p.add_argument("--status", action="store_true", help="查看 FlClash/代理当前状态")
    p.add_argument("--start", action="store_true", help="启动 FlClash 并选最快节点(保持运行)")
    p.add_argument("--stop", action="store_true", help="强制停止 FlClash(全部实例)")
    p.add_argument("--buy", action="store_true",
                   help="打开订阅购买/注册页面(本工具不提供节点,节点需自备)")
    p.add_argument("--keep", action="store_true", help="命令结束后保持 FlClash 运行(不自动关闭)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--force", action="store_true", help="跳过直连检测,强制走代理")
    mode.add_argument("--direct", action="store_true", help="强制直连,不走代理")
    p.add_argument("--no-speedtest", action="store_true", help="不测速,沿用当前节点")
    p.add_argument("--group", metavar="NAME", help="指定要切换的目标代理组(默认自动判断)")
    p.add_argument("--canary", metavar="URL", help="直连检测用的 URL")
    p.add_argument("--strict", action="store_true",
                   help="测速/切换失败时中止命令(默认降级为以当前节点执行)")
    p.add_argument("--timeout-ms", type=int, default=DELAY_TIMEOUT_MS,
                   help="单节点测速超时(毫秒,默认 %d)" % DELAY_TIMEOUT_MS)
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="要执行的命令及其参数")
    args = p.parse_args()

    if args.status:
        cmd_status()
        return
    if args.stop:
        print("✓ FlClash 已停止" if cmd_stop() == 0 else "!! 未能完全停止(见上方提示)")
        return
    if args.start:
        sys.exit(cmd_start(args.group, args.no_speedtest, args.timeout_ms))
    if args.buy:
        sys.exit(cmd_buy())
    if args.test:
        sys.exit(cmd_test(args.group, args.timeout_ms))
    cmd = [a for a in (args.cmd or []) if a]
    if cmd and cmd[0] == "--":
        # 显式分隔符:px -- <命令>,用于命令首参数本身以 - 开头的场景
        cmd = cmd[1:]
    if not cmd:
        p.print_help()
        return
    sys.exit(do_run(cmd, force=args.force, direct=args.direct,
                    no_speedtest=args.no_speedtest, keep=args.keep,
                    group=args.group, canary=args.canary,
                    timeout_ms=args.timeout_ms, strict=args.strict))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n!! 已中断")
        sys.exit(130)
