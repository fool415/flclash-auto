# -*- coding: utf-8 -*-
"""合成 FlClash 数据目录:两份订阅 + prefs 指针,布局与真实数据一致但完全离线。
测试不读取本机 %APPDATA% 里的真实配置,CI/任意机器都能跑。"""
import json
import os
import shutil
import tempfile
from pathlib import Path

KITTY_ID = 367162709361299456
KELE_ID = 370943043286601728
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fresh_root(prefix):
    return tempfile.mkdtemp(prefix=prefix)


def cleanup(root):
    shutil.rmtree(root, ignore_errors=True)


def make_data(root):
    """在 root/data 下生成隔离数据目录(两份带 官网/导航页 标签的 yaml + prefs),
    返回数据目录路径。mtime 显式设定:按更新时间排序的断言不依赖文件系统时间刻度。"""
    data = os.path.join(root, "data")
    os.makedirs(os.path.join(data, "profiles"))
    cfg = {"currentProfileId": KITTY_ID, "autoUpdate": True,
           "updateInterval": 86400, "concurrency": 5, "logLevel": "info"}
    root_prefs = {"flutter.config": json.dumps(cfg, ensure_ascii=False),
                  "flutter.version": "1.0.0"}
    Path(os.path.join(data, "shared_preferences.json")).write_text(
        json.dumps(root_prefs, ensure_ascii=False), encoding="utf-8")
    yamls = {
        "%d.yaml" % KITTY_ID:
            "proxies:\n  - {name: '官网：kitty.fo'}\n  - {name: 香港A}\n  - {name: 日本B}\n",
        "%d.yaml" % KELE_ID:
            "proxies:\n  - {name: '导航页：www.可乐云.net'}\n  - {name: 香港B}\n  - {name: 日本C}\n",
    }
    for i, (name, text) in enumerate(yamls.items()):
        path = os.path.join(data, "profiles", name)
        Path(path).write_text(text, encoding="utf-8")
        os.utime(path, (1758000000 + i * 60,) * 2)
    return data


def env_for(root, data):
    """CLI 子进程用的隔离环境:数据目录/日志/exe 全部指向测试区,绝不碰真实 FlClash。"""
    env = dict(os.environ)
    env["FLCLASH_DATA"] = data
    env["FLCLASH_FAILOVER_LOG"] = os.path.join(root, "failover.log")
    env["FLCLASH_FAILOVER_CONFIRM"] = "2"
    env["FLCLASH_EXE"] = os.path.join(root, "no-such-flclash.exe")
    return env


def apply_env(env):
    """把隔离环境写回当前进程(须在 import flclash_proxy 之前)。"""
    for k in ("FLCLASH_DATA", "FLCLASH_FAILOVER_LOG",
              "FLCLASH_FAILOVER_CONFIRM", "FLCLASH_EXE"):
        os.environ[k] = env[k]
