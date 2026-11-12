#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video-batch-insight 公共模块。

职责：ffmpeg/ffprobe 定位与调用、CUDA 运行库注册、时间格式化、媒体文件枚举。
被 asr.py / frames.py / bili.py / batch.py 共用。
"""
import os
import sys
import json
import shutil
import subprocess
from pathlib import Path

# ---------------------------------------------------------------- 媒体扩展名

VIDEO_EXT = {
    ".mp4", ".mkv", ".mov", ".flv", ".wmv", ".webm", ".avi", ".m4v",
    ".mpg", ".mpeg", ".rmvb", ".3gp", ".f4v",
}
# `.ts` 单独处理：它既是 MPEG-TS 视频，又是 TypeScript 源码。
# 目录里出现 .ts 时几乎都是代码（node_modules 能有几百个），
# 所以默认不收，只有显式 --allow-ts 才当视频。
TS_EXT = {".ts"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".wma", ".opus"}
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT

# 这些目录一律不进：全是代码/缓存/回收站，混进来只会白耗时间。
SKIP_DIRS = {
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv",
    "site-packages", "dist-packages", ".idea", ".vscode", ".next", ".nuxt",
    ".cache", ".tox", "dist", "build", ".gradle", ".m2", "target",
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "appdata", ".workbuddy", ".npm", ".pnpm-store",
}


# ---------------------------------------------------------------- ffmpeg 定位

def _candidate_bin_dirs():
    """ffmpeg 可能存在的目录，按优先级排列。"""
    dirs = []
    env = os.environ.get("FFMPEG_BIN")
    if env:
        dirs.append(Path(env))
    which = shutil.which("ffmpeg")
    if which:
        dirs.append(Path(which).parent)
    # Windows 上常见的 WinGet / scoop / choco 安装位
    home = Path.home()
    dirs += [
        home / "AppData/Local/Microsoft/WinGet/Packages",
        Path("C:/ProgramData/chocolatey/bin"),
        Path("C:/ffmpeg/bin"),
        Path("C:/Program Files/ffmpeg/bin"),
        home / "scoop/shims",
    ]
    return dirs


def find_tool(name):
    """找到 ffmpeg / ffprobe 的可执行文件路径。"""
    exe = name + (".exe" if os.name == "nt" else "")
    for d in _candidate_bin_dirs():
        if not d or not d.exists():
            continue
        direct = d / exe
        if direct.is_file():
            return str(direct)
        # WinGet 的包目录形如 ffmpeg-9.0.2-full_build/bin/ffmpeg.exe
        try:
            for hit in d.glob("ffmpeg*/**/bin/" + exe):
                if hit.is_file():
                    return str(hit)
        except Exception:
            pass
    return None


_FFMPEG = None
_FFPROBE = None


def ffmpeg():
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = find_tool("ffmpeg")
        if not _FFMPEG:
            raise RuntimeError("找不到 ffmpeg，请安装后加入 PATH，或设置环境变量 FFMPEG_BIN")
    return _FFMPEG


def ffprobe():
    global _FFPROBE
    if _FFPROBE is None:
        _FFPROBE = find_tool("ffprobe")
        if not _FFPROBE:
            raise RuntimeError("找不到 ffprobe，请安装后加入 PATH，或设置环境变量 FFMPEG_BIN")
    return _FFPROBE


# ---------------------------------------------------------------- CUDA 运行库

_cuda_ready = False


def setup_cuda_dlls(verbose=False):
    """
    faster-whisper(ctranslate2) 在 Windows 上用 GPU 需要 cublas64_12.dll +
    cudnn64_9.dll。这两个来自 pip 包 nvidia-cublas-cu12 / nvidia-cudnn-cu12，
    但它们的 bin 目录不在默认 DLL 搜索路径里，必须显式注册。

    返回已注册的目录列表。
    """
    global _cuda_ready
    if os.name != "nt":
        _cuda_ready = True
        return []
    if _cuda_ready:
        return []

    roots = []
    try:
        import nvidia  # noqa: F401
        import nvidia as _nv
        roots.append(Path(_nv.__file__).parent)
    except Exception:
        pass

    # venv / 全局 site-packages 下的 nvidia 目录
    for base in {Path(sys.prefix), Path(sys.base_prefix)}:
        for sp in ("Lib/site-packages", "lib/python3/dist-packages"):
            p = base / sp / "nvidia"
            if p.is_dir():
                roots.append(p)

    added = []
    for root in roots:
        if not root.is_dir():
            continue
        for sub in sorted(root.iterdir()):
            binp = sub / "bin"
            if binp.is_dir():
                try:
                    os.add_dll_directory(str(binp))
                    added.append(str(binp))
                except Exception:
                    pass
                # 有些库靠 PATH 兜底
                if str(binp) not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = str(binp) + os.pathsep + os.environ.get("PATH", "")

    # ctranslate2 包内自带 cudnn64_9.dll，把包目录也加进来
    try:
        import ctranslate2 as _ct
        pkg = Path(_ct.__file__).parent
        try:
            os.add_dll_directory(str(pkg))
        except Exception:
            pass
    except Exception:
        pass

    if verbose:
        print("[cuda] 已注册 DLL 目录：")
        for a in added:
            print("   ", a)
        if not added:
            print("    （未找到 nvidia 包，GPU 可能不可用）")
    _cuda_ready = True
    return added


# ---------------------------------------------------------------- 进程与探针

def run(cmd, check=True, capture=False, text=True, timeout=None):
    """统一入口跑外部命令。cmd 为 list。"""
    if capture:
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if text:
            kwargs.update(text=True, errors="replace")
        p = subprocess.run(cmd, **kwargs)
    else:
        p = subprocess.run(cmd, timeout=timeout)
    if check and p.returncode != 0:
        if not capture:
            err = ""
        elif isinstance(p.stderr, bytes):
            err = p.stderr.decode("utf-8", "replace")[-2000:]
        else:
            err = (p.stderr or "")[-2000:]
        raise RuntimeError("命令失败 (%d): %s\n%s" % (p.returncode, " ".join(map(str, cmd)), err))
    return p


def probe(path):
    """返回 {duration, width, height, fps, has_video, has_audio, vcodec, acodec}。"""
    p = run([ffprobe(), "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)], capture=True)
    data = json.loads(p.stdout)
    info = {
        "duration": 0.0, "width": 0, "height": 0, "fps": 0.0,
        "has_video": False, "has_audio": False,
        "vcodec": None, "acodec": None, "size": 0,
    }
    fmt = data.get("format") or {}
    try:
        info["duration"] = float(fmt.get("duration") or 0)
    except Exception:
        pass
    try:
        info["size"] = int(fmt.get("size") or 0)
    except Exception:
        pass

    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and not info["has_video"]:
            info["has_video"] = True
            info["width"] = int(s.get("width") or 0)
            info["height"] = int(s.get("height") or 0)
            info["vcodec"] = s.get("codec_name")
            fr = s.get("avg_frame_rate") or "0/0"
            try:
                num, den = fr.split("/")
                info["fps"] = round(float(num) / float(den), 3) if float(den) else 0.0
            except Exception:
                pass
            if not info["duration"]:
                try:
                    info["duration"] = float(s.get("duration") or 0)
                except Exception:
                    pass
        elif s.get("codec_type") == "audio" and not info["has_audio"]:
            info["has_audio"] = True
            info["acodec"] = s.get("codec_name")
    return info


# ---------------------------------------------------------------- 格式化工具

def hhmmss(sec):
    """123.4 -> '02:03'；超过一小时 -> '1:02:03'"""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def srt_ts(sec):
    """SRT 时间戳 00:02:03,400"""
    ms = int(round(max(0.0, sec) * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def safe_name(name, limit=80):
    """把标题变成安全的文件名。"""
    bad = '<>:"/\\|?*\n\r\t'
    out = "".join("_" if c in bad else c for c in str(name)).strip(" .")
    out = " ".join(out.split())
    if len(out) > limit:
        out = out[:limit].rstrip()
    return out or "untitled"


def ensure_dir(p):
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- 媒体枚举

def is_media(p):
    return Path(p).suffix.lower() in MEDIA_EXT


def _walk_media(root, allowed, recursive=True):
    """递归枚举 root 下的媒体文件，跳过 SKIP_DIRS 里的目录。"""
    if not recursive:
        for f in root.glob("*"):
            if f.is_file() and f.suffix.lower() in allowed:
                yield f
        return
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except (PermissionError, OSError):
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() in SKIP_DIRS or e.name.startswith("."):
                        continue
                    stack.append(Path(e.path))
                elif e.is_file():
                    if Path(e.name).suffix.lower() in allowed:
                        yield Path(e.path)
            except OSError:
                continue


def collect_media(paths, recursive=True, include_audio=True,
                  allow_ts=False, verify=False):
    """
    把「文件 / 文件夹 / 混合列表」统一展开成媒体文件列表（去重、按路径排序）。

    allow_ts=True 才把 .ts 当视频（默认排除，避免把 TypeScript 源码当视频）。
    verify=True 时用 ffprobe 复核确实含视频/音频流，剔除伪装扩展名的文件。
    """
    allowed = set(MEDIA_EXT if include_audio else VIDEO_EXT)
    if allow_ts:
        allowed |= TS_EXT
    out, seen = [], set()
    for p in paths:
        p = Path(p)
        if p.is_file():
            if p.suffix.lower() in allowed:
                r = p.resolve()
                if r not in seen:
                    seen.add(r)
                    out.append(p)
        elif p.is_dir():
            for f in _walk_media(p, allowed, recursive):
                r = f.resolve()
                if r not in seen:
                    seen.add(r)
                    out.append(f)
    out.sort(key=lambda x: str(x).lower())
    if verify:
        kept = []
        for f in out:
            try:
                info = probe(f)
            except Exception:
                continue
            if info.get("has_video") or info.get("has_audio"):
                kept.append(f)
        out = kept
    return out


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- 进度协议
# 给图形界面用的结构化进度行，格式：
#     @@P|<phase>|<i>|<n>|<name>|<extra>
# phase 取值：
#     begin  整批开始（n=总数）
#     item   开始处理第 i 个（name=文件名）
#     stage  第 i 个的某个阶段完成（extra=阶段名:耗时秒）
#     end    整批结束（extra=总耗时秒）
# 命令行下这些行看不出来（会被 GUI 吃掉当进度条用），所以同时打一条人话日志。
PROGRESS_PREFIX = "@@P|"


def progress(phase, i=0, n=0, name="", extra=""):
    """打一条结构化进度行。GUI 用它更新进度条与状态文字。"""
    clean = lambda s: str(s).replace("|", "/").replace("\n", " ")[:120]
    print("%s%s|%d|%d|%s|%s" % (PROGRESS_PREFIX, phase, i, n,
                                clean(name), clean(extra)), flush=True)
