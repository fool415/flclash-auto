# -*- coding: utf-8 -*-
"""离线测试:config_health 快路径(当前节点探测)与 repair_selection。
不启动 FlClash、不联网,全部打桩。"""
import importlib.util
import os
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "flclash_proxy.py")
os.environ.setdefault("FLCLASH_DATA",
                      os.path.join(tempfile.gettempdir(), "flclash-t-fastpath-data"))
os.environ.setdefault("FLCLASH_EXE",
                      os.path.join(tempfile.gettempdir(), "no-such-flclash.exe"))

spec = importlib.util.spec_from_file_location("fp", SRC)
fp = importlib.util.module_from_spec(spec)
sys.modules["fp"] = fp
spec.loader.exec_module(fp)

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append(bool(cond))
    tail = (" | %s" % extra) if extra else ""
    print("%s  %s%s" % ("PASS" if cond else "FAIL", name, tail))


PROXIES = {
    "节点选择": {"type": "Selector", "now": "香港A", "all": ["香港A", "日本B"]},
    "香港A": {"type": "Shadowsocks"},
    "日本B": {"type": "Shadowsocks"},
    "DIRECT": {"type": "Direct"},
}


def patch(**kw):
    for k, v in kw.items():
        setattr(fp, k, v)


def base_patches(now_alive=True, mode="rule"):
    calls = {"scan": 0, "tests": [], "pick_mode": None}

    patch(core_up=lambda timeout=2: {"version": "1.0"})
    patch(get_proxies=lambda: PROXIES)
    patch(get_mode=lambda: mode)

    def _pick(proxies, m, override=None):
        calls["pick_mode"] = m
        return "节点选择"
    patch(pick_target_group=_pick)
    patch(collect_candidates=lambda p, root: {"香港A": ["节点选择"], "日本B": ["节点选择"]})

    def _test(name, timeout_ms):
        calls["tests"].append((name, timeout_ms))
        return (name, 33 if now_alive else None)
    patch(test_node=_test)

    def _scan(cands, timeout_ms):
        calls["scan"] += 1
        return {n: None for n in cands}
    patch(scan_nodes=_scan)
    return calls


print("=" * 70)
print("I. config_health 快路径")
print("=" * 70)

# I1: 当前节点通 → 1 次探测即判定,不做全量测速
c = base_patches(now_alive=True)
h = fp.config_health()
check("当前节点通 → ok=True", h["ok"] is True, h)
check("reason=now", h.get("reason") == "now")
check("快路径只探测 1 个节点", len(c["tests"]) == 1, c["tests"])
check("快路径探测超时被夹到 <=3000ms", c["tests"][0][1] <= 3000)
check("快路径不做全量测速", c["scan"] == 0)

# I2: 当前节点不通 → 降级全量测速
c = base_patches(now_alive=False)
h = fp.config_health()
check("当前节点不通 → 全量测速 1 次", c["scan"] == 1)
check("全灭 → ok=False / all-dead", h["ok"] is False and h["reason"].startswith("all-dead"), h)

# I3: 全量测速有活节点 → ok=True / scan
c = base_patches(now_alive=False)
patch(scan_nodes=lambda cands, t: {"香港A": 80, "日本B": 45})
h = fp.config_health()
check("有活节点 → ok=True", h["ok"] is True, h)
check("reason=scan 且 best=最快", h["reason"] == "scan" and h["best"] == "日本B" and h["best_delay"] == 45)
check("健康结果带 cands 供 repair_selection 用", set(h["cands"]) == {"香港A", "日本B"})

# I4: mode 取不到时降级 rule
c = base_patches(now_alive=True, mode=None)
fp.config_health()
check("get_mode()=None 时按 rule 选组", c["pick_mode"] == "rule", c["pick_mode"])

print()
print("=" * 70)
print("J. repair_selection")
print("=" * 70)

out = []
patch(switch_by_chain=lambda cands, node: True)
fp.repair_selection({"cands": {"A": []}, "best": "A", "best_delay": 12}, say=out.append)
check("改选成功 → 打印已改选", any("已改选" in s for s in out), out)

out2 = []
patch(switch_by_chain=lambda cands, node: False)
fp.repair_selection({"cands": {"A": []}, "best": "A", "best_delay": 12}, say=out2.append)
check("改选失败 → 提示保持原选择", any("保持原选择" in s for s in out2), out2)

print()
print("=" * 70)
print("总计 %d 项,失败 %d 项" % (len(RESULTS), RESULTS.count(False)))
print("=" * 70)
sys.exit(1 if RESULTS.count(False) else 0)
