# -*- coding: utf-8 -*-
"""顺序跑全部离线测试套件(纯 mock + 合成数据,不启动 FlClash、不联网)。
退出码:全部通过 0,任一失败非 0。CI 直接 `python -B tests/run_all.py`。"""
import os
import subprocess
import sys

# 以 tests/ 为工作目录,子进程直接用纯文件名,不在命令里拼接任何路径
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# 套件里有大量中文与 ✓ 之类的符号:Windows 控制台默认 cp936、CI 的 cp1252 都会
# UnicodeEncodeError。这里强制本进程与子进程都用 UTF-8,不依赖调用方先设环境变量。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

SUITES = ("test_fastpath.py", "test_review.py", "test_new.py", "test_hardening.py")


def _spawn(name):
    try:
        return subprocess.run(["python", "-B", name], env=CHILD_ENV).returncode
    except FileNotFoundError:
        # 找不到 python 时回退 Windows 启动器(与 px.cmd 的策略一致)
        return subprocess.run(["py", "-3", "-B", name], env=CHILD_ENV).returncode


def main():
    rc = 0
    for name in SUITES:
        print("\n" + "#" * 22 + " %s " % name + "#" * 22, flush=True)
        rc = rc or _spawn(name)
    print("\n========== %s ==========" % ("全部测试通过" if rc == 0 else "存在失败"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
