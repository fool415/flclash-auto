# -*- coding: utf-8 -*-
"""第四轮复审锁定的行为:LoadBalance 自动组兜底、--timeout-ms 钳制、
--failover-install 上限、switch_profile 配置列表异常兜底、stop_flclash
以 PID 为准、cmd_start 端口未开、子命令忽略选项时出提示。
全部打桩/隔离数据,不启动 FlClash、不联网、不改注册表。"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _fixture  # noqa: E402

ROOT = _fixture.fresh_root("flclash-t-hard-")
DATA = _fixture.make_data(ROOT)
ENV = _fixture.env_for(ROOT, DATA)
_fixture.apply_env(ENV)

import flclash_proxy as fp  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(detail)) if detail else ""))


class Stubs:
    """临时替换模块级函数,退出时恢复"""

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


PY = sys.executable or "python"
SCRIPT = os.path.join(_fixture.REPO, "flclash_proxy.py")


def run_cli(*argv):
    return subprocess.run([PY, "-B", SCRIPT] + list(argv), env=ENV,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


# ---- 1. LoadBalance 也算自动组,可整组切换 ----
LB = "♻️自动选择"
proxies_lb = {
    "GLOBAL": {"type": "Selector", "now": LB, "all": ["♻️节点选择"]},
    "♻️节点选择": {"type": "Selector", "now": LB, "all": [LB]},
    LB: {"type": "LoadBalance", "now": "香港A", "all": ["香港A", "日本B"]},
    "香港A": {"type": "Shadowsocks"},
    "日本B": {"type": "Shadowsocks"},
}
check("auto_group_in 把 LoadBalance 组当自动组", fp.auto_group_in(proxies_lb, "♻️节点选择") == LB)

calls = []
with Stubs(get_proxies=lambda: proxies_lb, get_mode=lambda: "rule",
           api=lambda *a, **k: calls.append(a) or {"ok": True}):
    got = fp.select_fastest()
check("组里只挂 LoadBalance 时整组切到自动组", got == LB, got)
check("切换后 _last_scan 记录 switched-to-auto-group",
      fp._last_scan.get("reason") == "switched-to-auto-group", fp._last_scan.get("reason"))

# ---- 2. --timeout-ms 钳制,不让 1ms 把全部节点判成超时 ----
# --profiles 不吃 --timeout-ms(只提示忽略),用 --test:隔离环境里找不到 FlClash,
# 会在测速前快速失败,但又确实走到了参数体检这一步
r = run_cli("--test", "--timeout-ms", "1")
check("--timeout-ms 1 被钳到 500 并提示", "500..30000" in r.stderr, r.stderr.strip()[:80])
r = run_cli("--test", "--timeout-ms", "999999")
check("--timeout-ms 999999 被钳到 30000 并提示", "已按 30000 处理" in r.stderr,
      r.stderr.strip()[:80])

# ---- 3. --failover-install 间隔夹到 schtasks 上限 1439 ----
mo = []
with Stubs(_schtasks=lambda argv: (mo.append(argv) or
                                   __import__("types").SimpleNamespace(returncode=1, stdout="", stderr="x"))):
    rc = fp.cmd_failover_install(5000)
check("--failover-install 5000 按 1439 提交", rc == 1 and "1439" in mo[0], mo[0] if mo else None)

# ---- 4. 配置列表读取异常时 switch_profile 不崩 ----
def boom():
    raise OSError("profiles 目录不见了")


with Stubs(list_profiles=boom, holders_acquire=lambda: {os.getpid()},
           stop_flclash=lambda pids=None: True, wait_prefs_settled=lambda: None,
           set_current_profile_id=lambda tid: None, get_current_profile_id=lambda: None,
           start_flclash=lambda: False, ensure_inbound=lambda: (False, False),
           core_up=lambda: False):
    res = fp.switch_profile([1])
check("list_profiles 抛 OSError 时切换不崩、返回 None", res is None, res)

# ---- 5. stop_flclash 以"自己的 PID 是否消失"为准,不因别人的端口白等 ----
import time as _time
t0 = _time.time()
with Stubs(snapshot_pids=lambda: set(), port_open=lambda p: True,
           fix_system_proxy_after_stop=lambda: None):
    ok = fp.stop_flclash({424242})
check("our_pids 已不在时判定停止成功(端口被他人占用也不误报)", ok is True, ok)
check("且不再白等端口释放(<3s)", _time.time() - t0 < 3, "%.1fs" % (_time.time() - t0))

# ---- 6. cmd_start 端口未就绪时返回失败 ----
with Stubs(ensure_core=lambda: (True, False, set()), ensure_inbound=lambda: (False, False)):
    rc = fp.cmd_start()
check("cmd_start 端口未就绪返回 1", rc == 1, rc)

# ---- 7. 子命令模式下被忽略的选项/命令要出提示 ----
r = run_cli("--profiles", "--keep", "--group", "X", "echo", "hi")
check("忽略 --keep/--group 有提示", "--keep" in r.stderr and "--group" in r.stderr,
      r.stderr.strip()[:100])
check("子命令模式下不执行命令有提示", "不执行命令" in r.stderr, r.stderr.strip()[:100])

# ---- 汇总 ----
fail = [n for ok, n, _ in RESULTS if not ok]
print("\n" + "=" * 70)
print("总计 %d 项,失败 %d 项%s" % (len(RESULTS), len(fail), (": " + ", ".join(fail)) if fail else ""))
_fixture.cleanup(ROOT)
sys.exit(1 if fail else 0)
