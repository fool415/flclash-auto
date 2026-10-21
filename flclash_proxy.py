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
  python flclash_proxy.py --buy kitty                       # 指定购买入口(sakura / kitty)
  python flclash_proxy.py --keep git pull                   # 命令结束后保持 FlClash 运行
  python flclash_proxy.py --force pip install xxx           # 跳过直连检测,强制走代理
  python flclash_proxy.py --direct git pull                 # 强制直连
  python flclash_proxy.py --timeout-ms 8000 git pull        # 放宽节点测速超时
  python flclash_proxy.py --profiles                        # 列出全部订阅配置及健康状态
  python flclash_proxy.py --failover                        # 单次巡检:配置判死则自动切换
  python flclash_proxy.py --switch other                    # 手动切换订阅配置
  python flclash_proxy.py --failover-install                # 注册每 15 分钟巡检计划任务
  python flclash_proxy.py --failover-remove                 # 删除巡检计划任务

环境变量(可选):FLCLASH_EXE / FLCLASH_API / FLCLASH_SECRET / FLCLASH_PORT /
               FLCLASH_CANARY / FLCLASH_TESTURL / FLCLASH_START_TIMEOUT /
               FLCLASH_DELAY_MS / FLCLASH_DATA / FLCLASH_FAILOVER_LOG /
               FLCLASH_FAILOVER_CONFIRM / FLCLASH_DEBUG(设为 1 输出 API 调试日志) /
               FLCLASH_BUY_URL(默认入口 sakura 的购买地址,默认 https://114514-sakuracat.com) /
               FLCLASH_INVITE_CODE(默认入口 sakura 的邀请码,默认 10spAT6Z;设为空则不追加 ?code=)
               ——内置两个购买入口:sakura(?code=)与 kitty(?invite=),见 README
"""

import argparse
import csv
import ctypes
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

if os.name != "nt":
    # 依赖 msvcrt 文件锁 / winreg 注册表 / Windows 任务计划程序,仅支持 Windows;
    # 在 import msvcrt 崩出 traceback 之前先给人话提示
    print("!! 本工具仅支持 Windows(msvcrt/winreg/任务计划程序),当前平台: %s"
          % os.name, file=sys.stderr)
    sys.exit(1)

import msvcrt
import winreg
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------- 可配置项(环境变量可覆盖) ----------------
# 路径类配置统一经 abspath 规范化,消除 ../ 等相对成分
FLCLASH_EXE = os.path.abspath(os.environ.get("FLCLASH_EXE", r"C:\Program Files\FlClash\FlClash.exe"))
API_BASE = os.environ.get("FLCLASH_API", "http://127.0.0.1:9090").rstrip("/")
API_SECRET = os.environ.get("FLCLASH_SECRET", "")


def _env_int(name, default):
    """整数型环境变量:非法值回退默认并 stderr 提示,
    不让一个写错的变量把整个工具在 import 期就崩掉(计划任务会静默失效)"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print("!! 环境变量 %s=%r 不是整数,使用默认值 %s" % (name, raw, default),
              file=sys.stderr)
        return default


PROXY_PORT = _env_int("FLCLASH_PORT", 7890)
CANARY = os.environ.get("FLCLASH_CANARY", "https://www.google.com/generate_204")
TEST_URL = os.environ.get("FLCLASH_TESTURL", "https://www.gstatic.com/generate_204")
START_TIMEOUT = _env_int("FLCLASH_START_TIMEOUT", 45)
DELAY_TIMEOUT_MS = _env_int("FLCLASH_DELAY_MS", 3000)
DEBUG = bool(os.environ.get("FLCLASH_DEBUG"))
# 订阅购买入口:本工具不提供节点,节点/订阅由用户自备
BUY_URL = os.environ.get("FLCLASH_BUY_URL", "https://114514-sakuracat.com").rstrip("/")
# 默认邀请码:--buy 打开注册页时自动带上;设 FLCLASH_INVITE_CODE= 空值可关闭
INVITE_CODE = os.environ.get("FLCLASH_INVITE_CODE", "10spAT6Z").strip()

# FlClash 数据目录(shared_preferences.json / profiles/),便携版或自定义安装可能不同。
# 路径一律在源头做规范化(abspath 折叠 ../ 等相对成分)后再使用。
DATA_DIR = os.path.abspath(
    os.environ.get("FLCLASH_DATA")
    or os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                    "com.follow", "clash"))
# 故障切换巡检日志(每次 --failover 追加一段)
FAILOVER_LOG = os.path.abspath(
    os.environ.get("FLCLASH_FAILOVER_LOG")
    or os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                    "FlClash-Failover", "failover.log"))
# 巡检判死所需的连续确认次数(防机场瞬时抖动导致反复重启 FlClash);
# 钳制在 1..10:设得过大等于拿着持有者锁睡几分钟,会卡住其他 px 的收尾
_fc_raw = _env_int("FLCLASH_FAILOVER_CONFIRM", 2)
FAILOVER_CONFIRM = max(1, min(10, _fc_raw))
if FAILOVER_CONFIRM != _fc_raw:
    print("!! FLCLASH_FAILOVER_CONFIRM=%s 超出 1..10 范围,已按 %d 处理"
          % (_fc_raw, FAILOVER_CONFIRM), file=sys.stderr)
# 本工具注册的巡检计划任务名;LEGACY 是被本工具取代的旧 PowerShell 版任务
FAILOVER_TASK_NAME = "FlClash-Failover"
LEGACY_TASK_NAMES = ("FlClash-ConfigFailover",)

GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance"}
SELECTABLE_TYPES = {"Selector"}  # 只有 Selector 支持通过 API 手动切换;
# URLTest/Fallback/LoadBalance 是自动组:不能把它们的成员钉到父组,但整组可以切
AUTO_GROUP_TYPES = GROUP_TYPES - SELECTABLE_TYPES
BUILTIN_TYPES = GROUP_TYPES | {"Direct", "Reject", "RejectDrop", "PassRule", "Compatible", "Pass"}
FLCLASH_PROC_NAMES = ("FlClash.exe", "FlClashCore.exe")
# 订阅面板常见的"信息节点"(剩余流量/到期时间等),不参与测速。
# 只保留强特征词:宽松词(如"官网/群组")会误伤 HK-官网专线、群组A-01 这类真实节点
INFO_NODE_PAT = re.compile(r"剩余流量|套餐到期|到期时间|流量重置|下次重置|剩余天数|距离下次")

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
    """7890 被非 FlClash 程序占用时(端口在监听但控制 API 无响应)默认中止:
    继续执行的话命令流量会全部导向那个未知程序。确知占用无害时设
    FLCLASH_ALLOW_PORT_CONFLICT=1 放行(旧变量 FLCLASH_ABORT_ON_PORT_CONFLICT
    已废弃——默认行为已改为中止)。"""
    if port_open(PROXY_PORT) and not core_up():
        print("!! 端口 %d 已被非 FlClash 进程监听,继续执行会让命令流量进入该未知程序"
              % PROXY_PORT)
        if os.environ.get("FLCLASH_ALLOW_PORT_CONFLICT"):
            print("→ 已按 FLCLASH_ALLOW_PORT_CONFLICT=1 继续(自担风险)")
            return True
        print("!! 已中止;确知该端口占用无害时,可设 FLCLASH_ALLOW_PORT_CONFLICT=1 放行")
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
    if our_pids:
        # 只结束本工具启动的实例:成功判据是"这些 PID 都没了"。
        # 端口可能被别的程序(甚至另一个 FlClash)占着,拿端口当判据会白等
        # 10 秒,再误报"仍有进程存活"。
        while time.time() - t0 < 10 and (snapshot_pids() & set(our_pids)):
            time.sleep(0.5)
        leftover = bool(snapshot_pids() & set(our_pids))
        fix_system_proxy_after_stop()
        # 进程退出后内核端口释放有一小段延迟,给下一个 px 让路(最多让 2 秒,
        # 端口被别的程序占着也不会卡住)
        t1 = time.time()
        while time.time() - t1 < 2 and port_open(PROXY_PORT):
            time.sleep(0.25)
        return not leftover
    while time.time() - t0 < 10 and snapshot_pids():
        time.sleep(0.5)
    ports_closed = not port_open(PROXY_PORT) and not port_open(api_port())
    fix_system_proxy_after_stop()
    return ports_closed and not snapshot_pids()


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
    if not our_pids:
        # tasklist 慢一步或与另一个 px 同时启动时差集可能为空;绝不能把空集合
        # 交给 stop_flclash(那会退化成按镜像名全杀,可能误杀别人的实例)
        our_pids = snapshot_pids()
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
    自动组(URLTest/Fallback/LoadBalance)不下钻:其成员不是父组成员,
    直接 PUT 会报错;组里只挂自动组的订阅由 select_fastest 的兜底整组切过去。
    覆盖 global 模式下 GLOBAL 直接挂子分组、节点藏在多层 Selector 里的情况。"""
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
    """root 下第一个自动组(URLTest/Fallback/LoadBalance),用于"组里只挂自动组"
    时整组切过去。三种都算:LoadBalance 同属自动组,漏掉会让这类订阅永远
    停在 no-candidate(既不修复也不切换)"""
    for m in proxies.get(root, {}).get("all", []):
        if proxies.get(m, {}).get("type") in AUTO_GROUP_TYPES:
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


# select_fastest 最近一次结果的摘要,供 embedded_failover 判断是否复用
# (刚全量测速全灭时,不必再扫一遍同样的节点)
_last_scan = {"reason": "none"}
# switch_profile 本次进程内是否重启过 FlClash(含全部失败后的回滚重启);
# do_run 收尾据此刷新 PID 集并复位端口补丁,避免留下没人管的实例
_switch_restarted = False


def _note_scan(reason, **extra):
    global _last_scan
    _last_scan = {"reason": reason}
    _last_scan.update(extra)


def scan_nodes(cands, timeout_ms):
    """并发测速,返回 {节点名: 延迟 ms 或 None}。
    64 并发:节点多的机场按 16 并发会线性吃掉巡检窗口(500 节点 ≈ 94 秒)"""
    results = {}
    with ThreadPoolExecutor(max_workers=min(64, max(1, len(cands)))) as ex:
        futs = [ex.submit(test_node, n, timeout_ms) for n in cands]
        for f in as_completed(futs):
            n, d = f.result()
            results[n] = d
    return results


def switch_by_chain(cands, node):
    """沿选择链(cands[node] + [node],root → ... → 直属组 → 节点)逐级 PUT 切换"""
    seq = cands[node] + [node]
    for i in range(len(seq) - 1):
        if api("PUT", "/proxies/%s" % urllib.parse.quote(seq[i], safe=""),
               {"name": seq[i + 1]}) is None:
            return False
    return True


def select_fastest(timeout_ms=DELAY_TIMEOUT_MS, group_override=None):
    """并发测速目标组可达的全部节点,沿选择链自动切换到延迟最低的节点"""
    proxies = get_proxies()
    if not proxies:
        print("!! 无法获取代理信息(外部控制 API 无响应)")
        _note_scan("api-no-proxies")
        return None
    mode = get_mode()
    if mode is None:
        print("!! 无法确认运行模式(获取 /configs 失败),按 rule 模式处理")
        mode = "rule"
    root = pick_target_group(proxies, mode, group_override)
    if root not in proxies:
        print("!! 分组 [%s] 不存在(用 --group 指定其他分组)" % root)
        _note_scan("no-group")
        return None

    cands = collect_candidates(proxies, root)
    if not cands:
        if real_node_count(proxies) == 0:
            print_buy_hint("当前配置里没有任何节点(可能还没购买/导入订阅)")
        # 兜底:无可手动切换的叶子节点时(组里只挂 URLTest/Fallback/LoadBalance 自动组),
        # 整组切到自动组让它自选最快——global/rule 模式通用
        auto = auto_group_in(proxies, root)
        if auto:
            r = api("PUT", "/proxies/%s" % urllib.parse.quote(root, safe=""),
                    {"name": auto})
            if r is not None:
                print("✓ [%s] 无可手动切换的节点,已切换到自动测速组 [%s]" % (root, auto))
                _note_scan("switched-to-auto-group", node=auto)
                return auto
        print("!! 分组 [%s] 下没有可测速的节点" % root)
        _note_scan("no-candidates")
        return None

    print("→ 测速 %d 个节点(分组: %s,模式: %s)..." % (len(cands), root, mode))
    results = scan_nodes(cands, timeout_ms)

    ok = sorted((d, n) for n, d in results.items() if d)
    for d, n in ok:
        via = "" if len(cands[n]) <= 1 else "(经 %s)" % " → ".join(cands[n])
        print("   %5d ms  %s %s" % (d, n, via))
    for n in sorted(results):
        if not results[n]:
            print("   超时/失败  %s" % n)

    if not ok:
        print("!! 所有节点测速失败")
        _note_scan("all-dead", total=len(results), alive=0)
        return None
    best_delay, best = ok[0]
    if not switch_by_chain(cands, best):
        print("!! 切换节点失败(可设置 FLCLASH_DEBUG=1 查看原因)")
        _note_scan("switch-failed", total=len(results), alive=len(ok),
                   node=best, cands=cands)
        return None
    _note_scan("scanned-ok", total=len(results), alive=len(ok), node=best)
    print("✓ 最快节点: %s(%d ms)→ 切换路径: %s" % (
        best, best_delay, " → ".join(cands[best] + [best])))
    return best


# ---------------- 订阅购买指引(本工具不提供节点) ----------------
# 购买入口:第三方订阅服务商,本项目只负责给链接,不运营节点、不碰账号与支付。
# 每条:(命令行名字, 显示名, 站点, 邀请码, 注册页读取的参数名)
# 注意参数名各家不同:sakura 读 ?code=,kitty 读 ?invite=(也兼容 ?code=),写错则邀请码不生效。
BUY_PROVIDERS = [
    ("sakura", "SakuraCat", BUY_URL, INVITE_CODE, "code"),
    ("kitty", "Kitty Network", "https://kitty.fo", "ZVTCCIUS", "invite"),
]


def find_provider(key=None):
    """按命令行名字/显示名找入口;不传则用第一个(默认入口),找不到返回 None"""
    if not key:
        return BUY_PROVIDERS[0]
    k = str(key).strip().lower()
    for prov in BUY_PROVIDERS:
        if k in (prov[0], prov[1].lower()):
            return prov
    return None


def provider_url(prov):
    """入口的注册地址;有邀请码就拼上该服务商认的参数名"""
    _, _, base, invite, param = prov
    base = base.rstrip("/")
    if invite:
        return "%s/register?%s=%s" % (base, param, urllib.parse.quote(invite))
    return base


def buy_url(key=None):
    """购买/注册地址;默认入口受 FLCLASH_BUY_URL / FLCLASH_INVITE_CODE 控制"""
    prov = find_provider(key) or BUY_PROVIDERS[0]
    return provider_url(prov)


def real_node_count(proxies):
    """真实节点数(排除分组本身与"剩余流量/到期时间"这类信息节点)"""
    return sum(1 for name, info in (proxies or {}).items()
               if info.get("type") not in BUILTIN_TYPES and not INFO_NODE_PAT.search(name))


def print_buy_hint(reason=None):
    """没有可用节点时给出购买/导入指引;只在确实缺节点时输出,不打扰正常使用"""
    print("-- 没有可用的代理节点?本工具只负责调度 FlClash,节点/订阅需要自备")
    if reason:
        print("   原因: %s" % reason)
    print("   订阅服务(第三方,非本项目运营,购买前请自行评估):")
    for prov in BUY_PROVIDERS:
        print("     · %-14s %s" % (prov[1], provider_url(prov)))
    print("   购买后: 复制订阅链接 → FlClash「配置」→ 添加订阅 → 更新 → 再执行本命令")


def cmd_buy(key=None):
    import webbrowser  # 只有 --buy 需要,延迟导入
    prov = find_provider(key)
    if prov is None:
        print("!! 未知的购买入口: %s" % key)
        print("   可选: %s" % ", ".join("%s(%s)" % (x[0], x[1]) for x in BUY_PROVIDERS))
        return 2
    _, name, _, invite, _ = prov
    url = provider_url(prov)
    print("%s 订阅购买/注册地址: %s" % (name, url))
    print("步骤: 1) 注册并购买套餐  2) 复制订阅链接  3) FlClash「配置」→ 添加订阅 → 更新")
    print("提示: 账号/套餐/支付/退款均由该订阅服务提供,本工具只给出入口,不保存任何账号信息")
    if invite:
        print("邀请码: %s(已包含在上面的链接里)" % invite)
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        print("!! 未能自动打开浏览器,请手动访问上面的地址")
    return 0


# ---------------- 订阅配置(profile)管理 ----------------
# FlClash 的配置列表存在数据目录:profiles\<id>.yaml,当前启用的配置 id 记录在
# shared_preferences.json 的 flutter.config(一层嵌套 JSON 字符串)的 currentProfileId。
# 切换配置必须先完全退出 FlClash 再改文件:它退出时会用内存里的设置覆写该文件。
PROFILE_LABEL_PAT = re.compile(
    r"官网[:：]\s*([^\s,'\"\]\}]+)|导航页[:：]\s*([^\s,'\"\]\}]+)")


def prefs_path():
    return os.path.join(DATA_DIR, "shared_preferences.json")


def read_prefs():
    with open(prefs_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def read_flutter_config(root=None):
    """prefs 根对象里的 flutter.config 是一层嵌套的 JSON 字符串"""
    root = read_prefs() if root is None else root
    raw = root.get("flutter.config")
    if raw is None:
        raise KeyError("prefs 里没有 flutter.config")
    return json.loads(raw) if isinstance(raw, str) else raw


def get_current_profile_id():
    return read_flutter_config().get("currentProfileId")


def _under_data_dir(path):
    """路径规范化后必须仍位于 DATA_DIR 内,拒绝任何越界读写"""
    base = os.path.abspath(DATA_DIR)
    target = os.path.abspath(path)
    return os.path.commonpath([base, target]) == base


def backup_prefs():
    """把当前 prefs 备份到数据目录内(时间戳精确到毫秒,同秒多次切换不互相覆盖)"""
    stamp = "%s-%03d" % (time.strftime("%Y%m%d-%H%M%S"),
                         int(time.time() * 1000) % 1000)
    dst = os.path.join(DATA_DIR, "shared_preferences.bak-%s.json" % stamp)
    n = 1
    while os.path.exists(dst):  # 极端并发下同毫秒撞名时追加序号
        n += 1
        dst = os.path.join(DATA_DIR, "shared_preferences.bak-%s-%d.json" % (stamp, n))
    if not _under_data_dir(dst):
        raise ValueError("备份路径越界: %s" % dst)
    shutil.copyfile(prefs_path(), dst)
    return dst


def write_prefs(root):
    """原子写回 prefs:先写临时文件再 os.replace,避免 FlClash 读到半截 JSON。
    写入目标经校验必须位于 DATA_DIR 内。"""
    target = prefs_path()
    if not _under_data_dir(target):
        raise ValueError("写入目标越界: %s" % target)
    tmp = os.path.join(DATA_DIR, ".shared_preferences.tmp")
    text = json.dumps(root, ensure_ascii=False, separators=(",", ":"))
    Path(tmp).write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def restore_prefs(backup):
    """把 prefs 还原到指定备份文件(切换全部失败时回滚用);来源与目标都必须在
    数据目录内。与 write_prefs 对称地走 tmp+os.replace 原子写——回滚发生在
    "新配置已切坏、正在抢救"的时刻,最怕半截文件;逐字节还原原文,不经 JSON 重序列化。"""
    if not _under_data_dir(prefs_path()):
        raise ValueError("还原目标越界: %s" % prefs_path())
    if not _under_data_dir(backup):
        raise ValueError("备份来源越界: %s" % backup)
    text = Path(backup).read_text(encoding="utf-8")
    tmp = os.path.join(DATA_DIR, ".shared_preferences.tmp")
    Path(tmp).write_text(text, encoding="utf-8")
    os.replace(tmp, prefs_path())


def set_current_profile_id(profile_id):
    """只改 flutter.config 里的 currentProfileId,其余键原样保留。
    写入前先备份,返回备份路径(供切换失败时还原)。"""
    root = read_prefs()
    cfg = read_flutter_config(root)
    cfg["currentProfileId"] = profile_id
    root["flutter.config"] = json.dumps(cfg, ensure_ascii=False, separators=(",", ":"))
    backup = backup_prefs()
    write_prefs(root)
    return backup


def wait_prefs_settled(timeout=5.0):
    """等 FlClash 退出时对 prefs 的残余覆写落完(以 mtime 稳定为准)"""
    try:
        last = os.path.getmtime(prefs_path())
    except OSError:
        return
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.25)
        try:
            now = os.path.getmtime(prefs_path())
        except OSError:
            return
        if now == last:
            return
        last = now


def profile_label(yaml_path, fallback):
    """机场配置通常在节点名里自带 官网/导航页 字样,拿来当人话名字"""
    try:
        with open(yaml_path, "r", encoding="utf-8", errors="replace") as f:
            m = PROFILE_LABEL_PAT.search(f.read())
        if m and (m.group(1) or m.group(2)):
            return m.group(1) or m.group(2)
    except OSError:
        pass
    return fallback


def list_profiles():
    """profiles 目录下的全部订阅配置:[{id, path, updated, label}],按更新时间倒序"""
    pdir = os.path.join(DATA_DIR, "profiles")
    out = []
    for name in os.listdir(pdir):
        if not name.endswith(".yaml"):
            continue
        path = os.path.join(pdir, name)
        if not os.path.isfile(path):
            continue
        try:
            pid = int(name[:-5])
        except ValueError:
            continue
        out.append({"id": pid, "path": path, "updated": os.path.getmtime(path),
                    "label": profile_label(path, name[:-5])})
    out.sort(key=lambda p: p["updated"], reverse=True)
    return out


def _profiles_or_die():
    """读取配置列表与当前 id;失败时打印提示并返回 None"""
    try:
        return list_profiles(), get_current_profile_id()
    except (OSError, ValueError, KeyError) as e:
        print("!! 读取 FlClash 配置信息失败:%s(数据目录: %s,可用 FLCLASH_DATA 指定)"
              % (e, DATA_DIR))
        return None


def failover_targets():
    """可切换的备用配置 id,按更新时间从新到旧(优先切最近的订阅)"""
    info = _profiles_or_die()
    if not info:
        return []
    profiles, cur = info
    return [p["id"] for p in profiles if p["id"] != cur]


# ---------------- 配置级健康检查与故障切换 ----------------
def config_health(timeout_ms=DELAY_TIMEOUT_MS, group=None, presumed_dead=False):
    """当前配置整体健康检查(只读,不改任何状态)。
    返回 dict:ok=True 配置可用 / ok=False 配置判死(仅 all-dead 一种) /
    ok=None 无法判定(API 无响应、组不存在、组下无可测速节点等)。
    后三类都不是"订阅死亡",绝不能触发切换——否则 --group 写错组名
    或配置结构特殊就会白白杀进程重启代理。
    presumed_dead=True:调用方(select_fastest 刚做完全量测速且全灭)已扫过一遍,
    这里只补探 1 次当前选中节点(两轮之间网络恢复时不至于白杀进程重启),
    仍不通才沿用全灭结论,不再重复十几秒的全量测速。"""
    if not core_up():
        return {"ok": None, "reason": "core-api-down"}
    proxies = get_proxies()
    if not proxies:
        return {"ok": None, "reason": "api-no-response"}
    if presumed_dead:
        root = pick_target_group(proxies, get_mode() or "rule", group)
        now = (proxies.get(root) or {}).get("now")
        if now:
            _, d = test_node(now, min(timeout_ms, 3000))
            if d:
                return {"ok": True, "reason": "now", "now": now, "delay": d}
        return {"ok": False,
                "reason": "all-dead(沿用上一轮全量测速,%d 个节点全部失败)"
                          % _last_scan.get("total", 0)}
    mode = get_mode()
    if mode is None:
        mode = "rule"
    root = pick_target_group(proxies, mode, group)
    if root not in proxies:
        return {"ok": None, "reason": "no-group(%s 不存在,可用 --group 指定)" % root}
    cands = collect_candidates(proxies, root)
    # 快路径:当前选中节点是真实节点就先探它,1 个请求即可判定,不必全量测速;
    # 不要求 now 在 cands 里——组里只挂自动组的订阅,now 指向自动组选中的
    # 叶子节点,同样能走这条快路径
    now = (proxies.get(root) or {}).get("now")
    now_type = (proxies.get(now) or {}).get("type")
    if now and now_type not in BUILTIN_TYPES and not INFO_NODE_PAT.search(now):
        _, d = test_node(now, min(timeout_ms, 3000))
        if d:
            return {"ok": True, "reason": "now", "now": now, "delay": d}
    if not cands:
        return {"ok": None, "reason": "no-candidate(%s 下无可测速节点,配置结构特殊)" % root}
    print("→ 当前节点不通,全量测速 %d 个节点 ..." % len(cands))
    results = scan_nodes(cands, timeout_ms)
    alive = sorted((d, n) for n, d in results.items() if d)
    if alive:
        return {"ok": True, "reason": "scan", "best": alive[0][1],
                "best_delay": alive[0][0], "alive": len(alive),
                "total": len(results), "cands": cands}
    return {"ok": False, "reason": "all-dead(%d 个节点)" % len(results)}


def repair_selection(health, say=print):
    """配置可用但当前选中节点不通:把主组改选到实测最快的节点"""
    if switch_by_chain(health["cands"], health["best"]):
        say("✓ 原选中节点不通,已改选 %s(%d ms)" % (health["best"], health["best_delay"]))
    else:
        say("→ 改选节点失败,保持原选择")


def switch_profile(targets, timeout_ms=DELAY_TIMEOUT_MS):
    """依次尝试切换到 targets 里的配置,直到某个配置测速有可用节点。
    流程:全停 FlClash → 等 prefs 落盘稳定 → 备份并改 currentProfileId →
    重启 → 测速验证。全部失败时还原最初的 prefs 备份并重启 FlClash(保证不断网)。
    返回 {id,label,node,pids} 或 None。"""
    # 切换要杀 FlClash:调用方判断之后可能隔了一段防抖/测速时间,
    # 杀前重新确认没有其他 px 实例进来(否则会打断它们正在执行的命令)
    if len(holders_acquire()) > 1:
        print("!! 切换前发现其他 px 实例正在使用 FlClash,放弃切换(避免打断它们)")
        return None
    global _switch_restarted
    _switch_restarted = True  # 无论成功还是失败回滚,FlClash 都会被重启
    try:
        labels = {p["id"]: p["label"] for p in list_profiles()}
    except OSError as e:
        # 配置目录读不动(被占用/半路删掉)不该让整个切换崩掉,标签退回 id
        print("!! 读取配置列表失败(%s),标签将显示为 id" % e)
        labels = {}
    first_backup = None
    for tid in targets:
        label = labels.get(tid, str(tid))
        try:
            print("→ 切换配置 → %s(%s)" % (label, tid))
            print("   停止 FlClash(必须完全退出,否则退出时会覆写刚改的设置)...")
            stop_flclash(None)
            wait_prefs_settled()
            backup = set_current_profile_id(tid)
            if first_backup is None:
                first_backup = backup
            # 杀进程后可能有残余覆写,回读确认,被覆写就再写一次
            if get_current_profile_id() != tid:
                print("   设置被残余进程覆写,重写一次")
                set_current_profile_id(tid)
            print("   启动 FlClash ...")
            if not start_flclash():
                raise RuntimeError("FlClash 启动失败")
            inbound_ok, _ = ensure_inbound()
            if not inbound_ok:
                raise RuntimeError("本地代理端口 %d 未就绪" % PROXY_PORT)
            node = select_fastest(timeout_ms=timeout_ms)
            if node is None:
                raise RuntimeError("该配置全部节点测速失败")
            print("✓ 已切换到配置 %s,当前节点: %s" % (label, node))
            return {"id": tid, "label": label, "node": node, "pids": snapshot_pids()}
        except Exception as e:
            print("!! 切换到 %s 失败:%s" % (label, e))
            _dbg("!! [debug] switch_profile: %s" % e)
    print("→ 所有备用配置均不可用,还原切换前的配置并重启 FlClash ...")
    try:
        stop_flclash(None)
        wait_prefs_settled()
        if first_backup:
            restore_prefs(first_backup)
        if not core_up():
            start_flclash()
            ensure_inbound()
    except Exception as e:
        print("!! 还原失败(%s),如代理异常请手动启动 FlClash" % e)
    return None


# ---------------- 命令执行 ----------------
def embedded_failover(timeout_ms=DELAY_TIMEOUT_MS, group=None, holders=None):
    """px 命令模式的内嵌配置切换:当前配置判死(全部节点测速失败)时切到其他可用配置。
    其他 px 实例共用 FlClash 时放弃切换(杀进程会打断它们正在执行的命令)。
    返回 switch_profile 的结果 dict 或 None(未切换)。"""
    if group:
        # 指定了 --group 时,"该组全灭"≠"订阅整体死亡":可能是用户钉住的组临时
        # 不可用而默认组仍有活节点,且新订阅里也未必有同名组,不做配置级切换
        print("→ 指定了 --group,组级不可用不触发配置切换(订阅整体以默认组衡量)")
        return None
    if len(holders or []) > 1:
        print("→ 有其他 px 实例共用 FlClash,跳过自动配置切换")
        return None
    reason = _last_scan.get("reason")
    if reason == "switch-failed":
        # 上一轮是"有活节点但沿链 PUT 失败":配置本身是活的,只重试一次改选,
        # 不做十几秒的全量重扫,更不切订阅
        node, cands = _last_scan.get("node"), _last_scan.get("cands")
        if node and cands and switch_by_chain(cands, node):
            print("✓ 重试改选成功: %s" % node)
        else:
            print("→ 节点改选未成功(配置可用),不切换配置")
        return None
    # select_fastest 刚全量测速且全灭时,只补探 1 次当前节点即下结论,不再全量重扫
    presumed = reason == "all-dead"
    h = config_health(timeout_ms=timeout_ms, group=group, presumed_dead=presumed)
    if h["ok"] is None:
        print("→ 无法确认当前配置状态(%s),不自动切换" % h["reason"])
        return None
    if h["ok"] is True:
        return None  # 配置其实可用(刚才测速/切换瞬时失败),不折腾
    if not str(h["reason"]).startswith("all-dead"):
        # 保险:只有"全部节点测速失败"才算订阅判死,其余口径一律不切
        print("→ 判定口径为「%s」,不属于节点全灭,不自动切换" % h["reason"])
        return None
    print("!! 当前配置不可用(%s)" % h["reason"])
    targets = failover_targets()
    if not targets:
        print("!! 没有可切换的备用配置(需要补订阅)")
        return None
    return switch_profile(targets, timeout_ms=timeout_ms)


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
           strict=False, no_failover=False):
    global _switch_restarted
    _switch_restarted = False  # 标志只在本命令的生命周期内有效
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
        best = None
        if not no_speedtest:
            best = select_fastest(timeout_ms=timeout_ms, group_override=group)
            if best is None and not no_failover:
                switched = embedded_failover(timeout_ms=timeout_ms, group=group,
                                             holders=holders)
                if switched:
                    best = switched["node"]
                    if started_by_us:
                        our_pids = switched["pids"]
                if _switch_restarted:
                    # 切换流程重启过 FlClash(成功切换,或全部失败后回滚拉起):
                    # 旧 PID 集已失效,按当前进程快照刷新;新实例的端口由其
                    # 自身配置决定,之前对旧实例临时打开的 mixed-port 不再还原,
                    # 否则收尾会把新实例的 7890 关掉、用户代理瞬间断流
                    _switch_restarted = False
                    inbound_opened = False
                    if started_by_us:
                        our_pids = snapshot_pids()
        if best is None:
            if strict:
                print("!! --strict:测速/切换失败,中止命令")
                return 1
            if not no_speedtest:
                print("→ 测速/切换未成功,将以当前节点继续执行")
        return run_cmd(cmd, use_proxy=True)
    finally:
        remaining = None
        if started_by_us and not keep:
            # 先摘除自己拿剩余持有者;确认无人共用后再快速复核一次才停,
            # 把"已释放但仍存活"的窗口压到最短(期间新实例若恰好登记,会
            # 复用现有 FlClash 或在 ensure_core 里自行拉起,不会被误杀)
            if registered:
                remaining = holders_release()
                registered = False
            if remaining:
                print("→ 其他 px 实例仍在使用,FlClash 保持运行(剩余 %d 个)" % len(remaining))
            elif len(holders_acquire()) > 1:
                holders_release()
                print("→ 收尾时有新 px 实例加入,FlClash 保持运行")
            else:
                print("→ 命令结束,关闭本工具启动的 FlClash (pid: %s)..." % fmt_pids(our_pids))
                closed = stop_flclash(our_pids)
                print("✓ FlClash 已关闭" if closed else
                      "!! 部分 FlClash 进程仍存活(可手动执行 --stop 结束)")
                restore_system_proxy(snap)
        else:
            if registered:
                remaining = holders_release()
                registered = False
            if inbound_opened and not keep and not remaining:
                # 复用实例时临时打开的 7890,结束后还原为关闭状态(不影响仍在使用的其他实例)
                api("PATCH", "/configs", {"mixed-port": 0})
                print("→ 已还原复用实例的本地代理端口状态(mixed-port=0)")


# ---------------- 子命令 ----------------
def cmd_status(group=None):
    pids = snapshot_pids()
    print("FlClash.exe : %s (pid: %s)" % ("运行中" if pids else "未运行", fmt_pids(pids)))
    v = core_up()
    if v:
        print("核心 API    : %s (clash.meta %s)" % (API_BASE, v.get("version")))
        print("代理端口    : 127.0.0.1:%d %s" % (
            PROXY_PORT, "监听中" if port_open(PROXY_PORT) else "未监听"))
        mode = get_mode() or "?"
        proxies = get_proxies()
        g = pick_target_group(proxies, mode, group)
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
    return 0


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
    inbound_ok, _ = ensure_inbound()
    if not inbound_ok:
        print("!! 本地代理端口 %d 未就绪(mixed-port 可能被设为 0,在 FlClash 界面开启)" % PROXY_PORT)
        return 1
    if not no_speedtest:
        select_fastest(timeout_ms=timeout_ms, group_override=group)
    # FlClash 自身会按其设置接管系统代理;若未接管则补开,保持可用。
    # 保守起见:已有指向其他服务器的系统代理(公司代理/PAC)时不覆盖,只提示
    time.sleep(1.5)
    if port_open(PROXY_PORT) and reg_get("ProxyEnable") != 1:
        server = reg_get("ProxyServer") or ""
        if server and ("127.0.0.1:%d" % PROXY_PORT) not in server:
            print("!! 检测到已有系统代理(%s),未覆盖;如需本工具接管请手动调整" % server)
        else:
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


# ---------------- 配置切换子命令 ----------------
def cmd_profiles():
    """列出全部订阅配置及当前健康状态"""
    info = _profiles_or_die()
    if not info:
        return 1
    profiles, cur = info
    print("FlClash 数据目录: %s" % DATA_DIR)
    print("--- 订阅配置 ---")
    for p in profiles:
        mark = "  <== 当前" if p["id"] == cur else ""
        print("  %-18d %s  (更新于 %s)%s" % (
            p["id"], p["label"],
            time.strftime("%m-%d %H:%M", time.localtime(p["updated"])), mark))
    if not core_up():
        print("(FlClash 未运行,未做健康检查)")
        return 0
    h = config_health()
    if h["ok"] is True:
        if h["reason"] == "now":
            print("健康检查    : 当前配置可用(当前节点 %s %dms)" % (h["now"], h["delay"]))
        else:
            print("健康检查    : 当前配置可用(%d/%d 个节点在线,最快 %s %dms)"
                  % (h["alive"], h["total"], h["best"], h["best_delay"]))
    elif h["ok"] is False:
        print("健康检查    : !! 当前配置不可用(%s)" % h["reason"])
    else:
        print("健康检查    : ?? 无法判定(%s)" % h["reason"])
    return 0


def cmd_switch(target, timeout_ms=DELAY_TIMEOUT_MS):
    """手动切换订阅配置。target 为配置 id 或 other(切到最近更新的备用配置)"""
    info = _profiles_or_die()
    if not info:
        return 1
    profiles, cur = info
    if target.lower() == "other":
        others = [p for p in profiles if p["id"] != cur]
        if not others:
            print("!! 只有一份配置,没有可切换的目标")
            return 1
        tid = others[0]["id"]  # 列表已按更新时间倒序,优先切最近的订阅
    else:
        try:
            tid = int(target)
        except ValueError:
            print("!! 无效的配置标识:%s(应为配置 id 数字或 other,用 --profiles 查看)"
                  % target)
            return 1
        if tid == cur:
            print("→ 目标配置就是当前配置,无需切换")
            return 0
        if tid not in {p["id"] for p in profiles}:
            print("!! 配置 %s 不存在(用 --profiles 查看全部配置)" % tid)
            return 1
    holders = holders_acquire()
    try:
        if len(holders) > 1:
            print("!! 有其他 px 实例共用 FlClash,为避免打断它们,拒绝切换")
            return 1
        result = switch_profile([tid], timeout_ms=timeout_ms)
        if not result:
            return 1
        print("✓ 手动切换完成,FlClash 保持运行(要停止可执行 --stop)")
        return 0
    finally:
        holders_release()


def cmd_failover(timeout_ms=DELAY_TIMEOUT_MS, confirm_checks=None):
    """单次故障切换巡检:FlClash 没跑跳过;当前配置可用(必要时修复节点选择);
    连续多次判死才切换到备用配置。结果同时追加到巡检日志。"""
    confirm = confirm_checks or FAILOVER_CONFIRM
    lines = []

    def say(msg):
        print(msg)
        lines.append(msg)

    def finish(code, write=True):
        if write:
            try:
                os.makedirs(os.path.dirname(FAILOVER_LOG), exist_ok=True)
                # 超过 2MB 归档为 .1(覆盖旧归档),长期巡检不无限膨胀
                if os.path.exists(FAILOVER_LOG) and \
                        os.path.getsize(FAILOVER_LOG) > 2 * 1024 * 1024:
                    os.replace(FAILOVER_LOG, FAILOVER_LOG + ".1")
                with open(FAILOVER_LOG, "a", encoding="utf-8") as f:
                    f.write("=== %s 巡检 ===\n%s\n" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"), "\n".join(lines)))
            except OSError as e:
                _dbg("!! [debug] 巡检日志写入失败: %s" % e)
        return code

    if not flclash_procs_running():
        # 空闲巡检(FlClash 没在跑)只打屏幕不写日志,避免长期空转把日志灌满;
        # 计划任务是否在跑可看任务自身的"上次运行时间"
        print("→ FlClash 未在运行,跳过巡检(不顺手拉起)")
        return finish(0, write=False)
    holders = holders_acquire()
    try:
        h = config_health(timeout_ms=timeout_ms)
        if h["ok"] is None:
            say("!! 无法判定当前配置状态(%s),本次不切换" % h["reason"])
            return finish(1)
        if h["ok"]:
            if h["reason"] == "now":
                say("✓ 当前配置可用(当前节点 %s %dms)" % (h["now"], h["delay"]))
            else:
                say("✓ 当前配置可用(%d/%d 个节点在线)" % (h["alive"], h["total"]))
                repair_selection(h, say)
            return finish(0)
        # 判死 → 复核防抖(机场瞬时抖动很常见,别一次不通就重启用户代理)
        say("!! 当前配置不可用(%s)" % h["reason"])
        for i in range(confirm - 1):
            say("   5 秒后复核(%d/%d)..." % (i + 2, confirm))
            time.sleep(5)
            h = config_health(timeout_ms=timeout_ms)
            if h["ok"] is True:
                say("✓ 复核已恢复,不切换")
                if h["reason"] != "now":
                    repair_selection(h, say)
                return finish(0)
            if h["ok"] is None:
                say("!! 复核无法判定(%s),本次不切换" % h["reason"])
                return finish(1)
        if len(holders) > 1:
            say("!! 其他 px 实例正在共用 FlClash,本次不切换")
            return finish(1)
        targets = failover_targets()
        if not targets:
            say("!! 没有可切换的备用配置(需要补订阅)")
            return finish(1)
        say("→ 连续 %d 次判死,自动切换配置" % confirm)
        result = switch_profile(targets, timeout_ms=timeout_ms)
        if result:
            say("✓ 已切换到配置 %s,当前节点 %s" % (result["label"], result["node"]))
            return finish(0)
        say("!! 全部备用配置不可用,已还原原配置(见上方输出)")
        return finish(1)
    finally:
        holders_release()


# ---------------- 巡检计划任务(schtasks) ----------------
def _schtasks(args):
    try:
        return subprocess.run(["schtasks"] + args, capture_output=True,
                              text=True, errors="replace", timeout=20)
    except subprocess.TimeoutExpired:
        # 任务计划服务卡死时别把整个工具吊住(计划任务里跑时还会持锁)

        class _Timeout:
            returncode = 1
            stdout = ""
            stderr = "schtasks 20 秒未响应(任务计划服务可能异常),已放弃"

        return _Timeout()


def cmd_failover_install(interval_min=15):
    """注册定时巡检计划任务,并卸载被取代的旧版(PowerShell)巡检任务"""
    if interval_min < 1:
        print("→ 间隔 %s 分钟无效,已按最小值 1 分钟处理" % interval_min)
        interval_min = 1
    if interval_min > 1439:
        # schtasks /sc minute 的 /MO 上限是 1439(24 小时 - 1 分钟),超了真机会注册失败
        print("→ 间隔 %s 分钟超出上限,已按 1439 分钟处理" % interval_min)
        interval_min = 1439
    script = os.path.abspath(__file__)
    py = sys.executable or "python"
    # 后台定时巡检优先用 pythonw,避免每次运行闪一个控制台窗口
    if os.path.basename(py).lower() == "python.exe":
        pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
        if os.path.isfile(pyw):
            py = pyw
    tr = '"%s" -u "%s" --failover' % (py, script)
    r = _schtasks(["/create", "/tn", FAILOVER_TASK_NAME, "/tr", tr,
                   "/sc", "minute", "/mo", str(interval_min), "/f"])
    if r.returncode != 0:
        print("!! 注册计划任务失败:%s" % (r.stdout + r.stderr).strip())
        return 1
    print("✓ 已注册计划任务「%s」(每 %d 分钟巡检一次)" % (FAILOVER_TASK_NAME, interval_min))
    print("  命令:%s" % tr)
    print("  日志:%s" % FAILOVER_LOG)
    for name in LEGACY_TASK_NAMES:
        if _schtasks(["/query", "/tn", name]).returncode == 0:
            d = _schtasks(["/delete", "/tn", name, "/f"])
            if d.returncode == 0:
                print("✓ 已卸载旧版巡检任务「%s」(PowerShell 版,被本工具取代)" % name)
            else:
                print("!! 卸载旧任务「%s」失败:%s(可手动删除)"
                      % (name, (d.stdout + d.stderr).strip()))
    print("  取消:python flclash_proxy.py --failover-remove")
    return 0


def cmd_failover_remove():
    r = _schtasks(["/delete", "/tn", FAILOVER_TASK_NAME, "/f"])
    if r.returncode == 0:
        print("✓ 已删除计划任务「%s」" % FAILOVER_TASK_NAME)
        return 0
    if _schtasks(["/query", "/tn", FAILOVER_TASK_NAME]).returncode != 0:
        print("→ 没有计划任务「%s」,无需删除" % FAILOVER_TASK_NAME)
        return 0
    print("!! 删除失败:%s" % (r.stdout + r.stderr).strip())
    return 1


def main():
    _u8()
    p = argparse.ArgumentParser(
        prog="flclash_proxy",
        description="FlClash 按需代理:检测直连 → 需要时自动启动 FlClash 并测速选最快节点 → "
                    "配置整体不可用时自动切换备用订阅配置 → 命令结束自动关闭",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:  python flclash_proxy.py curl -sI https://www.google.com")
    p.add_argument("--test", action="store_true", help="测速全部节点并显示(不执行命令)")
    p.add_argument("--status", action="store_true", help="查看 FlClash/代理当前状态")
    p.add_argument("--start", action="store_true", help="启动 FlClash 并选最快节点(保持运行)")
    p.add_argument("--stop", action="store_true", help="强制停止 FlClash(全部实例)")
    p.add_argument("--buy", nargs="?", const="", metavar="PROVIDER",
                   help="打开订阅购买/注册页面(本工具不提供节点,节点需自备);"
                        "可指定入口: %s,默认 sakura" % " / ".join(x[0] for x in BUY_PROVIDERS))
    p.add_argument("--keep", action="store_true", help="命令结束后保持 FlClash 运行(不自动关闭)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--force", action="store_true", help="跳过直连检测,强制走代理")
    mode.add_argument("--direct", action="store_true", help="强制直连,不走代理")
    p.add_argument("--no-speedtest", action="store_true", help="不测速,沿用当前节点")
    p.add_argument("--group", metavar="NAME", help="指定要切换的目标代理组(默认自动判断)")
    p.add_argument("--canary", metavar="URL", help="直连检测用的 URL")
    p.add_argument("--strict", action="store_true",
                   help="测速/切换失败时中止命令(默认降级为以当前节点执行)")
    p.add_argument("--no-failover", action="store_true",
                   help="当前配置节点全灭时不自动切换其他订阅配置")
    p.add_argument("--timeout-ms", type=int, default=DELAY_TIMEOUT_MS,
                   help="单节点测速超时(毫秒,默认 %d)" % DELAY_TIMEOUT_MS)
    p.add_argument("--profiles", action="store_true",
                   help="列出 FlClash 全部订阅配置及健康状态")
    p.add_argument("--failover", action="store_true",
                   help="单次故障切换巡检(当前配置判死则自动切换备用配置)")
    p.add_argument("--switch", metavar="ID|other",
                   help="手动切换订阅配置(other=最近更新的备用配置)")
    p.add_argument("--failover-install", nargs="?", const=15, type=int, metavar="MIN",
                   help="注册每 MIN 分钟巡检的计划任务(默认 15;并卸载旧版 PS1 任务)")
    p.add_argument("--failover-remove", action="store_true",
                   help="删除巡检计划任务")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="要执行的命令及其参数")
    args = p.parse_args()

    # ---- 参数体检:挡住会让整轮测速"全灭"进而误切配置的手滑输入 ----
    cmd = [a for a in (args.cmd or []) if a]
    subcmds = bool(args.status or args.stop or args.start or args.test or args.profiles
                   or args.buy is not None or args.failover or args.switch
                   or args.failover_install is not None or args.failover_remove)
    timeout_used = (not subcmds or args.start or args.test
                    or args.failover or args.switch)
    if timeout_used and not 500 <= args.timeout_ms <= 30000:
        clamped = max(500, min(30000, args.timeout_ms))
        print("!! --timeout-ms %d 超出 500..30000,已按 %d 处理"
              % (args.timeout_ms, clamped), file=sys.stderr)
        args.timeout_ms = clamped
    if subcmds:
        # 子命令模式不执行命令,也不吃大部分命令选项:静默吞掉容易让人以为生效了
        ignored = [name for name, val in (("--keep", args.keep), ("--canary", args.canary),
                                          ("--strict", args.strict),
                                          ("--no-failover", args.no_failover),
                                          ("--force", args.force), ("--direct", args.direct))
                   if val]
        if args.group and not (args.status or args.start or args.test):
            ignored.append("--group")
        if args.no_speedtest and not args.start:
            ignored.append("--no-speedtest")
        if args.timeout_ms != DELAY_TIMEOUT_MS and not (
                args.start or args.test or args.failover or args.switch):
            ignored.append("--timeout-ms")
        if ignored:
            print("→ 这些选项对当前子命令无效,已忽略:%s" % ", ".join(ignored), file=sys.stderr)
        if cmd:
            print("→ 子命令模式下不执行命令,已忽略:%s" % " ".join(cmd), file=sys.stderr)

    if args.status:
        sys.exit(cmd_status(args.group))
    if args.stop:
        rc = cmd_stop()
        if rc != 0:
            print("!! 未能完全停止(见上方提示)")
        sys.exit(rc)
    if args.start:
        sys.exit(cmd_start(args.group, args.no_speedtest, args.timeout_ms))
    if args.buy is not None:
        sys.exit(cmd_buy(args.buy or None))
    if args.test:
        sys.exit(cmd_test(args.group, args.timeout_ms))
    if args.profiles:
        sys.exit(cmd_profiles())
    if args.failover:
        sys.exit(cmd_failover(timeout_ms=args.timeout_ms))
    if args.switch:
        sys.exit(cmd_switch(args.switch, timeout_ms=args.timeout_ms))
    if args.failover_install is not None:
        sys.exit(cmd_failover_install(args.failover_install))
    if args.failover_remove:
        sys.exit(cmd_failover_remove())
    if cmd and cmd[0] == "--":
        # 显式分隔符:px -- <命令>,用于命令首参数本身以 - 开头的场景
        cmd = cmd[1:]
    if not cmd:
        p.print_help()
        return
    sys.exit(do_run(cmd, force=args.force, direct=args.direct,
                    no_speedtest=args.no_speedtest, keep=args.keep,
                    group=args.group, canary=args.canary,
                    timeout_ms=args.timeout_ms, strict=args.strict,
                    no_failover=args.no_failover))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n!! 已中断")
        sys.exit(130)
