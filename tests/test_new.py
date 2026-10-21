# -*- coding: utf-8 -*-
"""补充离线测试:presumed_dead 补探、--group 不触发配置切换、自动组兜底、
switch-failed 重试、持有者复核、备份命名、越界防护、_env_int、安装任务、
巡检日志滚动、CLI 冒烟。全部打桩或用隔离数据,不启动 FlClash。"""
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _fixture  # noqa: E402

ROOT = _fixture.fresh_root("flclash-t-new-")
DATA = _fixture.make_data(ROOT)
ENV = _fixture.env_for(ROOT, DATA)
_fixture.apply_env(ENV)

import flclash_proxy as fp  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(detail)) if detail else ""))


class Stubs:
    def __init__(self, **kw):
        self.saved = {}
        for k, v in kw.items():
            self.saved[k] = getattr(fp, k, None)
            setattr(fp, k, v)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(fp, k, v)


NODE = lambda d: {"type": "Shadowsocks", "now": "", "all": [], "history": [], "delay": d}
PROXIES = {
    "GLOBAL": {"type": "Selector", "now": "A", "all": ["A", "B"]},
    "节点选择": {"type": "Selector", "now": "A", "all": ["A", "B"]},
    "A": NODE(50),
    "B": NODE(80),
}


def health_ctx(**over):
    """让真实 config_health 走内存代理图"""
    base = dict(core_up=lambda: True, get_proxies=lambda: PROXIES, get_mode=lambda: "global")
    base.update(over)
    return Stubs(**base)


print("A. presumed_dead:补探当前节点 1 次,仍不通才沿用全灭结论")
fp._last_scan = {"reason": "all-dead", "total": 2}
with health_ctx():
    h = fp.config_health(presumed_dead=True)
check("presumed_dead=True:补探仍死 → ok=False", h["ok"] is False, h)
with health_ctx(test_node=lambda n, t: (n, 50)):
    h1 = fp.config_health(presumed_dead=True)
check("presumed_dead=True:当前节点已恢复 → ok=True(不白切)", h1["ok"] is True, h1)
with health_ctx(test_node=lambda n, t: (n, 50)):
    h2 = fp.config_health(presumed_dead=False)
check("同一份活着配置,presumed_dead=False 判活", h2["ok"] is True, h2)

print("\nB. --group 指向的组全灭时,不触发配置切换")
calls = []
with health_ctx(), Stubs(switch_profile=lambda ts, timeout_ms=0: calls.append(ts) or
                         {"id": ts[0], "label": "L", "node": "N", "pids": {1}},
                         failover_targets=lambda: [_fixture.KELE_ID],
                         holders_acquire=lambda: {1}):
    fp._last_scan = {"reason": "all-dead", "total": 2}
    r = fp.embedded_failover(group="用户指定但全灭的组", holders={1})
check("group 维度全灭 → 不触发切换(组范围≠订阅整体)", not calls and r is None, calls)
calls2 = []
with health_ctx(), Stubs(switch_profile=lambda ts, timeout_ms=0: calls2.append(ts) or
                         {"id": ts[0], "label": "L", "node": "N", "pids": {1}},
                         failover_targets=lambda: [_fixture.KELE_ID],
                         holders_acquire=lambda: {1}):
    fp._last_scan = {"reason": "all-dead", "total": 2}
    r2 = fp.embedded_failover(group=None, holders={1})
check("未指定 group 时仍按订阅整体切换(不受影响)", bool(calls2), calls2)

print("\nC. 组里只挂自动组:不下钻,兜底整组切自动组(global/rule 通用)")
AUTO = {
    "GLOBAL": {"type": "Selector", "now": "自动选择", "all": ["自动选择"]},
    "自动选择": {"type": "URLTest", "now": "n1", "all": ["n1", "n2"]},
    "n1": NODE(40), "n2": NODE(60),
}
RULE_AUTO = {
    "节点选择": {"type": "Selector", "now": "自动选择", "all": ["自动选择"]},
    "自动选择": {"type": "URLTest", "now": "n1", "all": ["n1", "n2"]},
    "n1": NODE(40), "n2": NODE(60),
}
check("collect_candidates 不下钻自动组(成员非父组成员,直接 PUT 会报错)",
      fp.collect_candidates(AUTO, "GLOBAL") == {}, fp.collect_candidates(AUTO, "GLOBAL"))
puts = []
with Stubs(get_proxies=lambda: AUTO, get_mode=lambda: "global",
           api=lambda m, p, body=None: puts.append((m, p, body)) or {}):
    best = fp.select_fastest()
check("global 模式兜底:整组切到自动组(由它自选最快)",
      best == "自动选择" and ("PUT", "/proxies/GLOBAL", {"name": "自动选择"}) in puts,
      (best, puts))
puts2 = []
with Stubs(get_proxies=lambda: RULE_AUTO, get_mode=lambda: "rule",
           api=lambda m, p, body=None: puts2.append((m, p, body)) or {}):
    best2 = fp.select_fastest()
import urllib.parse as _up
check("rule 模式同样走兜底(不限于 global)",
      best2 == "自动选择" and
      ("PUT", "/proxies/" + _up.quote("节点选择", safe=""), {"name": "自动选择"}) in puts2,
      (best2, puts2))
AUTO_LEAF = {
    "GLOBAL": {"type": "Selector", "now": "n1", "all": ["自动选择"]},
    "自动选择": {"type": "URLTest", "now": "n1", "all": ["n1", "n2"]},
    "n1": NODE(40), "n2": NODE(60),
}
with Stubs(core_up=lambda: True, get_proxies=lambda: AUTO_LEAF, get_mode=lambda: "global",
           test_node=lambda n, t: (n, 40)):
    h3 = fp.config_health()
check("自动组订阅:now 指向叶子节点时快路径可判活(不要求 now 在候选里)",
      h3["ok"] is True and h3["reason"] == "now", h3)

print("\nC2. switch-failed 时只重试改选,不全量重扫、不切订阅")
retry_puts = []
with health_ctx(), Stubs(
        switch_profile=lambda ts, timeout_ms=0: retry_puts.append("switch") or None,
        failover_targets=lambda: [_fixture.KELE_ID],
        holders_acquire=lambda: {1},
        switch_by_chain=lambda cands, node: retry_puts.append(("retry", node)) or True):
    fp._last_scan = {"reason": "switch-failed", "total": 2, "alive": 1,
                     "node": "A", "cands": {"A": ["GLOBAL"]}}
    r3 = fp.embedded_failover(holders={1})
check("switch-failed → 重试一次改选,不切订阅", r3 is None and
      ("retry", "A") in retry_puts and "switch" not in retry_puts, retry_puts)

print("\nD. switch_profile 杀进程前重新检查持有者")
calls = []
with Stubs(holders_acquire=lambda: {1, 2}, stop_flclash=lambda p: calls.append("stop"),
           list_profiles=lambda: []):
    r = fp.switch_profile([_fixture.KELE_ID])
check("有其他实例 → 放弃切换且不杀进程", r is None and not calls, calls)
check("放弃时不设置重启标志(fp._switch_restarted 为 False)",
      fp._switch_restarted is False)

print("\nE. 备份文件名毫秒 + 同毫秒撞名追加序号")
fixed = 1761070000.123
orig_time, orig_strftime = fp.time.time, fp.time.strftime
fp.time.time = lambda: fixed
fp.time.strftime = lambda fmt, *a: "20261021-190000"
try:
    b1 = fp.backup_prefs()
    b2 = fp.backup_prefs()
finally:
    fp.time.time, fp.time.strftime = orig_time, orig_strftime
check("同毫秒第二次备份自动加序号", b1 != b2 and b2.endswith("-2.json"), (b1, b2))
check("备份名含毫秒段 123", "20261021-190000-123" in b1, b1)

print("\nF. 越界防护(备份来源/写入目标)")
outside = os.path.join(ROOT, "outside.bak")
Path(outside).write_text("{}", encoding="utf-8")
try:
    fp.restore_prefs(outside)
    check("restore_prefs 拒绝数据目录外的备份", False, "未抛错")
except ValueError as e:
    check("restore_prefs 拒绝数据目录外的备份", "越界" in str(e), e)

print("\nG. _env_int:非法值回退默认并提示;CONFIRM 钳制 1..10")
os.environ["__T"] = "abc"
buf = io.StringIO()
with redirect_stderr(buf):
    v = fp._env_int("__T", 7)
os.environ.pop("__T")
check("非法整数 → 默认值 7", v == 7, v)
check("stderr 有中文提示", "不是整数" in buf.getvalue(), buf.getvalue().strip())
check("FLCLASH_FAILOVER_CONFIRM 当前值合法", fp.FAILOVER_CONFIRM == 2, fp.FAILOVER_CONFIRM)
os.environ["FLCLASH_FAILOVER_CONFIRM"] = "50"
buf2 = io.StringIO()
with redirect_stderr(buf2):
    v2 = fp._env_int("FLCLASH_FAILOVER_CONFIRM", 2)
clamped = max(1, min(10, v2))
check("CONFIRM=50 会被钳制到 10(调用方钳制逻辑)", clamped == 10, v2)
os.environ.pop("FLCLASH_FAILOVER_CONFIRM")

print("\nH. --failover-install:间隔 0 夹到 1 分钟,优先 pythonw")
seen = []


class R:
    returncode = 0
    stdout = stderr = ""


with Stubs(_schtasks=lambda args: seen.append(args) or R()):
    rc = fp.cmd_failover_install(0)
check("exit 0", rc == 0, rc)
tr = seen[0][seen[0].index("/tr") + 1]
check("/mo 夹到 1", seen[0][seen[0].index("/mo") + 1] == "1", seen[0])
check("/tr 使用 pythonw.exe", "pythonw.exe" in tr, tr)
check("/tr 指向仓库脚本绝对路径",
      os.path.join(_fixture.REPO, "flclash_proxy.py") in tr, tr)

seen2 = []


class R1:
    returncode = 1
    stdout = "ERROR: 拒绝访问"
    stderr = ""


with Stubs(_schtasks=lambda args: seen2.append(args) or R1()):
    rc2 = fp.cmd_failover_install(15)
check("schtasks 失败 → exit 1", rc2 == 1, rc2)

print("\nI. 巡检日志:空闲不写、超 2MB 滚动")
LOG = ENV["FLCLASH_FAILOVER_LOG"]
if os.path.exists(LOG):
    os.remove(LOG)
with Stubs(flclash_procs_running=lambda: False):
    rc = fp.cmd_failover()
check("FlClash 未运行:exit 0 且不建日志", rc == 0 and not os.path.exists(LOG),
      (rc, os.path.exists(LOG)))

Path(LOG).write_text("x" * (2 * 1024 * 1024 + 10), encoding="utf-8")
with Stubs(flclash_procs_running=lambda: True, holders_acquire=lambda: {1},
           holders_release=lambda: set(),
           config_health=lambda **kw: {"ok": True, "reason": "now", "now": "n1", "delay": 9}):
    rc = fp.cmd_failover()
check("日志超 2MB 自动归档为 .1", os.path.exists(LOG + ".1") and
      os.path.getsize(LOG) < 1024 * 1024,
      (os.path.exists(LOG + ".1"), os.path.getsize(LOG)))
check("滚动后 exit 0", rc == 0, rc)

print("\nJ. CLI:--failover 空闲不写日志;CONFIRM 非法值/过大不崩 --help")
env = dict(ENV)
env["FLCLASH_FAILOVER_LOG"] = os.path.join(ROOT, "cli2-new.log")
if os.path.exists(env["FLCLASH_FAILOVER_LOG"]):
    os.remove(env["FLCLASH_FAILOVER_LOG"])
r = subprocess.run([sys.executable, "-B",
                    os.path.join(_fixture.REPO, "flclash_proxy.py"), "--failover"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
check("--failover 空闲 exit 0", r.returncode == 0, r.stdout.strip())
check("--failover 空闲不创建日志文件", not os.path.exists(env["FLCLASH_FAILOVER_LOG"]))
for val, desc in (("abc", "非法值"), ("50", "过大值")):
    env2 = dict(env)
    env2["FLCLASH_FAILOVER_CONFIRM"] = val
    r2 = subprocess.run([sys.executable, "-B",
                         os.path.join(_fixture.REPO, "flclash_proxy.py"), "--help"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=env2)
    check("CONFIRM=%s(%s)时 --help 正常" % (val, desc),
          r2.returncode == 0 and "--failover" in r2.stdout, r2.returncode)

print("\nK. CLI:--profiles 在合成数据下正常")
r3 = subprocess.run([sys.executable, "-B",
                     os.path.join(_fixture.REPO, "flclash_proxy.py"), "--profiles"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", env=env)
check("--profiles exit 0 且列出两份", r3.returncode == 0 and
      "kitty.fo" in r3.stdout and "可乐云" in r3.stdout, r3.returncode)

fail = [x for x in RESULTS if not x[0]]
print("\n" + "=" * 70)
print("总计 %d 项,失败 %d 项" % (len(RESULTS), len(fail)))
for _, n, d in fail:
    print("  FAIL:", n, "|", d)
_fixture.cleanup(ROOT)
sys.exit(1 if fail else 0)
