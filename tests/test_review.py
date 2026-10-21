# -*- coding: utf-8 -*-
"""flclash_proxy 离线审查测试:配置解析 / prefs 读写回滚 / 判死口径 /
内嵌切换接线 / switch_profile 回滚。全部打桩,不启动 FlClash、不联网。"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _fixture  # noqa: E402

ROOT = _fixture.fresh_root("flclash-t-review-")
DATA = _fixture.make_data(ROOT)
_fixture.apply_env(_fixture.env_for(ROOT, DATA))

import flclash_proxy as fp  # noqa: E402

_ORIG = {n: getattr(fp, n) for n in (
    "config_health", "switch_profile", "start_flclash", "stop_flclash",
    "core_up", "wait_prefs_settled", "ensure_inbound", "select_fastest",
    "snapshot_pids", "get_proxies", "get_mode", "pick_target_group",
    "collect_candidates", "direct_ok", "check_port_conflict", "proxy_snapshot",
    "holders_acquire", "holders_release", "ensure_core", "run_cmd",
    "restore_system_proxy", "embedded_failover")}

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + str(detail)) if detail else ""))


def restore_all():
    for n, f in _ORIG.items():
        setattr(fp, n, f)


orig_prefs_bytes = Path(fp.prefs_path()).read_bytes()
orig_root = json.loads(orig_prefs_bytes.decode("utf-8"))
orig_cfg = json.loads(orig_root["flutter.config"])

print("=" * 70)
print("A. 配置解析与标签")
print("=" * 70)
profiles = fp.list_profiles()
check("list_profiles 找到 2 份配置", len(profiles) == 2, str([p["id"] for p in profiles]))
check("按更新时间倒序",
      [p["id"] for p in profiles] == [_fixture.KELE_ID, _fixture.KITTY_ID],
      str([(p["id"], p["label"]) for p in profiles]))
labels = {p["id"]: p["label"] for p in profiles}
check("label 从「官网：」提取", labels.get(_fixture.KITTY_ID) == "kitty.fo",
      labels.get(_fixture.KITTY_ID))
check("label 从「导航页：」提取", labels.get(_fixture.KELE_ID) == "www.可乐云.net",
      labels.get(_fixture.KELE_ID))
check("get_current_profile_id", fp.get_current_profile_id() == _fixture.KITTY_ID,
      str(fp.get_current_profile_id()))

plain_yaml = os.path.join(ROOT, "plain.yaml")
Path(plain_yaml).write_text("proxies:\n  - { name: 香港A }\n", encoding="utf-8")
check("无标记时用 fallback 名",
      fp.profile_label(plain_yaml, str(_fixture.KITTY_ID)) == str(_fixture.KITTY_ID))
check("文件不存在时回退", fp.profile_label(os.path.join(ROOT, "nope.yaml"), "fallback") == "fallback")

print()
print("=" * 70)
print("B. prefs 读写 / 备份 / 边界校验")
print("=" * 70)
bak = fp.set_current_profile_id(_fixture.KELE_ID)
check("返回备份路径", os.path.isfile(bak), bak)
check("备份内容 == 改写前原文", Path(bak).read_bytes() == orig_prefs_bytes)
new_root = json.loads(Path(fp.prefs_path()).read_text(encoding="utf-8"))
check("只改 currentProfileId",
      new_root["flutter.config"] and
      json.loads(new_root["flutter.config"])["currentProfileId"] == _fixture.KELE_ID)
new_cfg = json.loads(new_root["flutter.config"])
check("flutter.config 其余键原样保留",
      {k: v for k, v in new_cfg.items() if k != "currentProfileId"} ==
      {k: v for k, v in orig_cfg.items() if k != "currentProfileId"})
check("顶层其他键原样保留(flutter.version)",
      new_root["flutter.version"] == orig_root["flutter.version"])
check("原子写无临时文件残留",
      not os.path.exists(os.path.join(DATA, ".shared_preferences.tmp")))
check("_under_data_dir 允许数据目录内", fp._under_data_dir(os.path.join(DATA, "x.json")))
check("_under_data_dir 拒绝 ../ 越界",
      not fp._under_data_dir(os.path.join(DATA, "..", "escape.json")))
check("_under_data_dir 拒绝绝对越界", not fp._under_data_dir(r"C:\Windows\x.json"))

fp.restore_prefs(bak)
check("restore_prefs 逐字节还原原文(原子回滚)",
      Path(fp.prefs_path()).read_bytes() == orig_prefs_bytes)

print()
print("=" * 70)
print("C. config_health: API 不可用必须返回 None(不得误判为判死)")
print("=" * 70)
# 必须打桩:本机若恰好开着 FlClash,config_health 会真连 API 扫描真实订阅(测试必须在离线环境下确定)
fp.core_up = lambda timeout=2: None
h = fp.config_health()
check("core 不在时 ok is None", h["ok"] is None, str(h))
check("reason = core-api-down", h["reason"] == "core-api-down", str(h))

print()
print("=" * 70)
print("D. embedded_failover: 有其他 px 实例时必须放弃切换")
print("=" * 70)
calls = []
fp.config_health = lambda **kw: calls.append(kw) or {"ok": False, "reason": "all-dead(99 个节点)"}
r = fp.embedded_failover(holders={111, 222})
check("多实例时返回 None", r is None)
check("多实例时根本不调用 config_health", not calls, str(calls))


def fake_switch_profile(targets, timeout_ms=3000):
    switched_calls.append(list(targets))
    return {"id": targets[0], "label": "L", "node": "N", "pids": {999}}


switched_calls = []
fp.switch_profile = fake_switch_profile
fp.config_health = lambda **kw: {"ok": False, "reason": "all-dead(99 个节点)"}
r = fp.embedded_failover(holders={111})
check("单实例判死 → 触发切换", switched_calls == [[_fixture.KELE_ID]], str(switched_calls))
check("返回切换结果", r and r["node"] == "N", str(r))

switched_calls.clear()
fp.config_health = lambda **kw: {"ok": None, "reason": "api-no-response"}
fp.embedded_failover(holders={111})
check("无法判定(None)时绝不切换", not switched_calls, str(switched_calls))

print()
restore_all()
print("=" * 70)
print("E. switch_profile: 切换失败必须回滚,成功必须落 prefs")
print("=" * 70)
fp.start_flclash = lambda: False
fp.stop_flclash = lambda pids=None: True  # 打桩,避免真去 taskkill/改系统代理
fp.core_up = lambda timeout=2: None
fp.wait_prefs_settled = lambda timeout=5.0: None
fp.ensure_inbound = lambda: (True, False)
r = fp.switch_profile([_fixture.KELE_ID])
check("启动失败 → 返回 None", r is None)
check("启动失败 → prefs 已回滚为原文",
      Path(fp.prefs_path()).read_bytes() == orig_prefs_bytes)

fp.start_flclash = lambda: True
fp.select_fastest = lambda timeout_ms=3000, group_override=None: "香港A"
fp.snapshot_pids = lambda: {4242}
r = fp.switch_profile([_fixture.KELE_ID])
check("成功 → 返回 dict", bool(r) and r["id"] == _fixture.KELE_ID, str(r))
check("成功 → node 记录正确", r and r["node"] == "香港A")
check("成功 → pids 记录", r and r["pids"] == {4242})
cur = json.loads(Path(fp.prefs_path()).read_text(encoding="utf-8"))
check("成功 → prefs.currentProfileId 已改",
      json.loads(cur["flutter.config"])["currentProfileId"] == _fixture.KELE_ID)
fp.restore_prefs(bak)

print()
print("=" * 70)
print("F. do_run 的 failover 分支接线")
print("=" * 70)


def run_case(no_failover, started_by_us):
    calls = {"embedded": [], "run": [], "stop": [], "restore": []}
    fp.direct_ok = lambda url=fp.CANARY, timeout=4: False
    fp.check_port_conflict = lambda: True
    fp.proxy_snapshot = lambda: ("snap",)
    fp.holders_acquire = lambda: {1}
    fp.holders_release = lambda: set()
    fp.ensure_core = lambda: (True, started_by_us, {111} if started_by_us else None)
    fp.ensure_inbound = lambda: (True, False)
    fp.select_fastest = lambda timeout_ms=3000, group_override=None: None
    fp.embedded_failover = lambda **kw: calls["embedded"].append(kw) or {
        "id": 2, "label": "L", "node": "N", "pids": {222}}
    fp.run_cmd = lambda cmd, use_proxy: calls["run"].append(use_proxy) or 0
    fp.stop_flclash = lambda pids=None: calls["stop"].append(pids) or True
    fp.restore_system_proxy = lambda snap: calls["restore"].append(snap)
    rc = fp.do_run(["prog"], force=True, no_failover=no_failover)
    return rc, calls


rc, calls = run_case(no_failover=False, started_by_us=True)
check("默认: select_fastest 失败后走 embedded_failover",
      len(calls["embedded"]) == 1, str(calls["embedded"]))
check("默认: 切换后以代理方式执行命令", calls["run"] == [True], str(calls["run"]))
check("默认: 重启后的新 PID 集用于关闭 FlClash", calls["stop"] == [{222}], str(calls["stop"]))
check("默认: 退出码透传", rc == 0)

rc, calls = run_case(no_failover=True, started_by_us=True)
check("--no-failover: 不触发配置切换", not calls["embedded"], str(calls["embedded"]))
check("--no-failover: 仍以当前节点执行", calls["run"] == [True])

rc, calls = run_case(no_failover=False, started_by_us=False)
check("复用实例: 会触发配置切换", len(calls["embedded"]) == 1)
check("复用实例: 不关闭用户自己的 FlClash", not calls["stop"], str(calls["stop"]))

print()
restore_all()
print("=" * 70)
print("G. 判死口径:--group 写错/无候选不再被当成「配置已死」")
print("=" * 70)
fp.get_proxies = lambda: {"节点选择": {"type": "Selector", "now": "A", "all": ["A"]},
                          "A": {"type": "Shadowsocks"}}
fp.get_mode = lambda: "rule"
fp.core_up = lambda timeout=2: {"version": "x"}
h = fp.config_health(group="用户打错的组名")
check("不存在的组 → ok=None(无法判定,不误判为死)", h["ok"] is None, str(h))
fp.pick_target_group = lambda proxies, mode, override=None: "节点选择"
fp.collect_candidates = lambda proxies, root: {}
h2 = fp.config_health()
check("无候选节点 → ok=None", h2["ok"] is None, str(h2))
fp.switch_profile = fake_switch_profile
switched_calls.clear()
fp.config_health = lambda **kw: {"ok": False, "reason": "no-group(用户打错的组名)"}
fp.embedded_failover(holders={111})
check("非 all-dead 的 ok=False 也不触发切换(双保险)", switched_calls == [], str(switched_calls))

print()
print("=" * 70)
print("K. P2/P3 修复回归")
print("=" * 70)
restore_all()
import re as _re
check("备份文件名含毫秒段(同秒不互相覆盖)",
      _re.search(r"\.bak-\d{8}-\d{6}-\d{3}(-\d+)?\.json$", bak) is not None, bak)
fp.core_up = lambda timeout=2: {"version": "x"}
fp.get_proxies = lambda: {"节点选择": {"type": "Selector", "now": "A", "all": ["A"]}}
_scan_calls = []
fp.scan_nodes = lambda cands, t: _scan_calls.append(1) or {}
fp._last_scan = {"reason": "all-dead", "total": 28, "alive": 0}
h3 = fp.config_health(presumed_dead=True)
check("presumed_dead: 补探仍死 → 直接沿用全灭结论,不再全量测速",
      h3["ok"] is False and h3["reason"].startswith("all-dead") and not _scan_calls,
      "%s scan=%d" % (h3, len(_scan_calls)))
captured = {}


def _ch_capture(**kw):
    captured.update(kw)
    return {"ok": False, "reason": "all-dead(沿用上一轮全量测速,28 个节点全部失败)"}


fp.config_health = _ch_capture
fp._last_scan = {"reason": "all-dead", "total": 28, "alive": 0}
fp.embedded_failover(holders={111})
check("embedded_failover 传递 presumed_dead=True",
      captured.get("presumed_dead") is True, str(captured))

print()
print("=" * 70)
print("H. 汇总")
print("=" * 70)
failed = [r for r in RESULTS if not r[0]]
print("总计 %d 项,失败 %d 项" % (len(RESULTS), len(failed)))
for _, name, detail in failed:
    print("FAIL: %s | %s" % (name, detail))
_fixture.cleanup(ROOT)
sys.exit(1 if failed else 0)
