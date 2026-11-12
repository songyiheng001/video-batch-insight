#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脱离父进程启动器。

为什么需要它：
    在 Windows 上直接用 subprocess.Popen(creationflags=DETACHED_PROCESS) 起一个
    长时间任务，子进程仍会被并入父进程的 Job Object。父进程（图形界面）一退出，
    子进程就被连带回收 —— 实测表现为解析跑到一半突然没了、日志文件 0 字节。

    本脚本由界面进程启动，它自己再以「脱离作业对象」的方式拉起真正的解析进程。
    Python 的 Popen 在 creationflags 含 CREATE_NEW_PROCESS_GROUP / DETACHED_PROCESS
    时会走 CreateProcess，配合 CREATE_BREAKAWAY_FROM_JOB 才能彻底脱钩。

用法（由界面自动调用，不需要手敲）:
    python run_detached.py <日志文件> <真正的命令...>

行为:
    - 真正的解析进程 stdout/stderr 全部追加写入 <日志文件>
    - 解析结束后在日志尾部写 #EXIT#<code> 和 #END#，界面靠它判断"跑完了"
    - 本脚本立即退出，不等待解析完成
"""
import os
import sys
import subprocess

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("用法: run_detached.py <日志文件> <命令...>\n")
        return 2
    log_path = sys.argv[1]
    cmd = sys.argv[2:]

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    lf = open(log_path, "a", encoding="utf-8", buffering=1)
    try:
        proc = subprocess.Popen(
            cmd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=env, creationflags=flags)
    except OSError:
        # 某些环境不允许 BREAKAWAY，退回普通脱钩方式
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            proc = subprocess.Popen(
                cmd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, creationflags=flags)
        except Exception as e:
            lf.write("\n无法启动解析进程：%s\n#EXIT#-1\n#END#\n" % e)
            lf.close()
            return 1

    # 另起一个看守，等解析结束就把退出码写进日志末尾
    guard = (
        "import sys,time,subprocess\r\n"
        "pid=int(sys.argv[1]); log=sys.argv[2]\r\n"
        "while True:\r\n"
        "    try:\r\n"
        "        import ctypes\r\n"
        "        k=ctypes.windll.kernel32\r\n"
        "        h=k.OpenProcess(0x1000,False,pid)\r\n"
        "        if not h: break\r\n"
        "        code=ctypes.c_ulong()\r\n"
        "        ok=k.GetExitCodeProcess(h,ctypes.byref(code))\r\n"
        "        k.CloseHandle(h)\r\n"
        "        if ok and code.value!=259: break\r\n"
        "    except Exception:\r\n"
        "        break\r\n"
        "    time.sleep(1)\r\n"
        "code=0\r\n"
        "try: code=code.value\r\n"
        "except Exception: pass\r\n"
        "with open(log,'a',encoding='utf-8') as f:\r\n"
        "    f.write('\\n#EXIT#%d\\n#END#\\n' % code)\r\n"
    )
    try:
        subprocess.Popen([sys.executable, "-c", guard, str(proc.pid), log_path],
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                         | CREATE_BREAKAWAY_FROM_JOB)
    except Exception:
        pass
    lf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
