#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量音频转写：faster-whisper（GPU 优先，自动回退 CPU）-> 逐字稿 + SRT + JSON。

用法:
    python asr.py <文件或文件夹...> --out <输出目录>
    python asr.py D:/videos --out C:/Users/me/Downloads/视频解析 --model large-v3

产出（每个视频一个子目录）:
    <out>/<视频名>/transcript.md     段落化逐字稿，带 [时间戳]
    <out>/<视频名>/transcript.srt    标准字幕
    <out>/<视频名>/segments.json     原始分段（程序消费用）
    <out>/<视频名>/meta.json         模型/设备/耗时/字数

要点:
    - 音轨用 ffmpeg 解成 16k 单声道裸 PCM，再交给 faster-whisper。
      不要直接把文件名丢给 model.transcribe()——新版 PyAV 会抛
      TypeError: open() got an unexpected keyword argument 'metadata_errors'。
    - condition_on_previous_text=False，避免长视频陷入重复循环。
    - 已完成的文件默认跳过，加 --force 重跑。
"""
import os
import sys
import json
import time
import shutil
import argparse
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common as C  # noqa: E402

DEFAULT_MODEL = "large-v3"
DEFAULT_PROMPT = "以下是普通话视频的转写，内容涉及知识讲解与经验分享，请使用简体中文并加上标点符号。"
DEFAULT_MODEL_DIR = str(Path.home() / ".cache" / "fw")

_MODEL_CACHE = {}


# ---------------------------------------------------------------- 模型

def resolve_device(device="auto"):
    """auto -> 能用 CUDA 就 CUDA，否则 CPU。返回 (device, compute_type)。"""
    if device == "cpu":
        return "cpu", "int8"
    if device == "cuda":
        return "cuda", "float16"
    # auto
    C.setup_cuda_dlls()
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def load_model(name, device, compute_type, model_dir):
    key = (name, device, compute_type)
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    from faster_whisper import WhisperModel
    C.log("  载入模型 %s (%s/%s)..." % (name, device, compute_type))
    m = WhisperModel(name, device=device, compute_type=compute_type,
                     download_root=model_dir)
    _MODEL_CACHE[key] = m
    return m


# ---------------------------------------------------------------- 音轨解码

def extract_pcm(media, out_pcm, sr=16000):
    """用 ffmpeg 把任意媒体解成 16k 单声道裸 s16le PCM。"""
    C.run([C.ffmpeg(), "-y", "-v", "error", "-i", str(media),
           "-vn", "-ac", "1", "-ar", str(sr),
           "-f", "s16le", "-acodec", "pcm_s16le", str(out_pcm)])
    return out_pcm


def read_pcm(path, sr=16000):
    import numpy as np
    pcm = np.fromfile(str(path), dtype="<i2")
    return pcm, len(pcm) / float(sr)


# ---------------------------------------------------------------- 转写

def transcribe_pcm(model, pcm, total_sec, language="zh", prompt=DEFAULT_PROMPT,
                   beam_size=5, show_progress=True):
    """pcm 为 int16 numpy 数组。返回 segments 列表。"""
    import numpy as np
    audio = pcm.astype(np.float32) / 32768.0
    segs, _info = model.transcribe(
        audio,
        language=language,
        beam_size=beam_size,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400),
        initial_prompt=prompt or None,
        condition_on_previous_text=False,   # 长视频防重复循环
    )
    out = []
    last_tick = time.time()
    for s in segs:
        txt = (s.text or "").strip()
        if txt:
            out.append({"start": round(float(s.start), 2),
                        "end": round(float(s.end), 2),
                        "text": txt})
        if show_progress and time.time() - last_tick > 3:
            last_tick = time.time()
            pct = (s.end / total_sec * 100) if total_sec else 0
            sys.stdout.write("\r    转写中 %s / %s (%4.1f%%)   "
                             % (C.hhmmss(s.end), C.hhmmss(total_sec), pct))
            sys.stdout.flush()
    if show_progress:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()
    return out


# ---------------------------------------------------------------- 输出

def apply_fixes(text, fixes):
    for a, b in fixes:
        text = text.replace(a, b)
    return text


def merge_paragraphs(segments, target=120):
    """把 ASR 分段合并成适合阅读的段落。"""
    paras = []
    cur, cur_start, cur_len = [], None, 0
    enders = "。！？；!?;"
    for s in segments:
        if cur_start is None:
            cur_start = s["start"]
        cur.append(s["text"])
        cur_len += len(s["text"])
        t = s["text"].rstrip()
        if (cur_len >= target and t and t[-1] in enders) or cur_len >= target * 1.6:
            paras.append({"start": cur_start, "end": s["end"], "text": "".join(cur)})
            cur, cur_start, cur_len = [], None, 0
    if cur:
        paras.append({"start": cur_start, "end": segments[-1]["end"], "text": "".join(cur)})
    return paras


def write_srt(segments, path):
    lines = []
    for i, s in enumerate(segments, 1):
        lines.append(str(i))
        lines.append("%s --> %s" % (C.srt_ts(s["start"]), C.srt_ts(s["end"])))
        lines.append(s["text"])
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def write_transcript_md(media, info, segments, meta, path, fixes_note=""):
    paras = merge_paragraphs(segments)
    body = []
    for p in paras:
        body.append("**[%s]** %s" % (C.hhmmss(p["start"]), p["text"]))
    note = fixes_note or "（未做同音词修正，专有名词请自行核对）"
    md = [
        "# %s · 逐字稿" % media.stem,
        "",
        "> 本文件由本地 faster-whisper（%s，%s）自动转写，**不是官方字幕**，"
        "专有名词（人名/机构/术语）可能存在同音误写。%s" % (meta["device"], meta["model"], note),
        "",
        "| 项目 | 值 |",
        "|---|---|",
        "| 来源文件 | `%s` |" % media.name,
        "| 时长 | %s |" % C.hhmmss(info.get("duration") or 0),
        "| 分辨率 | %s |" % (("%d×%d" % (info["width"], info["height"]))
                              if info.get("has_video") else "—（纯音频）"),
        "| 模型 / 设备 | %s / %s |" % (meta["model"], meta["device"]),
        "| 转写耗时 | %.1f 秒（%.1fx 实时） |" % (meta["elapsed"], meta.get("speed", 0)),
        "| 分段数 | %d |" % len(segments),
        "| 字数 | %d |" % meta["chars"],
        "",
        "---",
        "",
        "## 逐字稿",
        "",
    ]
    md += body
    md.append("")
    Path(path).write_text("\n".join(md), encoding="utf-8")


# ---------------------------------------------------------------- 单文件流程

def process_one(media, out_root, model_name, device, compute_type, model_dir,
                language, prompt, beam_size, fixes, force=False, quiet=False):
    media = Path(media)
    out_dir = C.ensure_dir(Path(out_root) / C.safe_name(media.stem))
    md_path = out_dir / "transcript.md"

    if md_path.exists() and not force:
        if not quiet:
            C.log("  跳过（已完成）：%s" % media.name)
        return {"file": str(media), "status": "skipped", "out": str(out_dir)}

    if not quiet:
        C.log("  → %s" % media.name)
    t0 = time.time()

    info = C.probe(media)
    if not info.get("has_audio"):
        C.log("  !! 无音轨，跳过：%s" % media.name)
        return {"file": str(media), "status": "no_audio", "out": str(out_dir)}

    tmpdir = tempfile.mkdtemp(prefix="vbi_")
    pcm_path = Path(tmpdir) / "audio.pcm"
    try:
        extract_pcm(media, pcm_path)
        pcm, audio_sec = read_pcm(pcm_path)
        total = info.get("duration") or audio_sec
        model = load_model(model_name, device, compute_type, model_dir)
        segments = transcribe_pcm(model, pcm, total, language=language,
                                  prompt=prompt, beam_size=beam_size,
                                  show_progress=not quiet)
    finally:
        # 无条件的彻底清理：一个 3 小时的音频 PCM 就有几百 MB，
        # 进程被强杀/磁盘写满时若不清理，残留会越堆越多（实测堆过 855MB）。
        try:
            pcm_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    if fixes:
        for s in segments:
            s["text"] = apply_fixes(s["text"], fixes)

    elapsed = time.time() - t0
    chars = sum(len(s["text"]) for s in segments)
    meta = {
        "model": model_name, "device": "%s/%s" % (device, compute_type),
        "elapsed": round(elapsed, 1),
        "speed": round(audio_sec / elapsed, 1) if elapsed else 0,
        "chars": chars, "segments": len(segments),
        "audio_sec": round(audio_sec, 1),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    (out_dir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=1), encoding="utf-8")
    write_srt(segments, out_dir / "transcript.srt")
    fixes_note = ""
    if fixes:
        fixes_note = "已定向修正：%s。" % "、".join("%s→%s" % (a, b) for a, b in fixes)
    write_transcript_md(media, info, segments, meta, md_path, fixes_note)
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    if not quiet:
        C.log("    完成：%d 字 / %d 段 / %.1fs（%.1fx 实时）"
              % (chars, len(segments), elapsed, meta["speed"]))
    return {"file": str(media), "status": "ok", "out": str(out_dir), "meta": meta}


# ---------------------------------------------------------------- CLI

def parse_fixes(items):
    fixes = []
    for it in items or []:
        if "=" in it:
            a, b = it.split("=", 1)
            fixes.append((a, b))
    return fixes


def main():
    ap = argparse.ArgumentParser(description="批量转写视频/音频为逐字稿")
    ap.add_argument("inputs", nargs="+", help="文件或文件夹（递归）")
    ap.add_argument("--out", required=True, help="输出根目录")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="模型名或本地路径")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="initial prompt，传空字符串可关闭")
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--model-dir", default=DEFAULT_MODEL_DIR)
    ap.add_argument("--fix", action="append", default=[],
                    help="同音词修正，形如 错误词=正确词，可多次")
    ap.add_argument("--force", action="store_true", help="覆盖已完成的结果")
    ap.add_argument("--list", action="store_true", help="只列出将要处理的文件")
    args = ap.parse_args()

    if not os.environ.get("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

    C.setup_cuda_dlls()
    media = C.collect_media(args.inputs)
    if not media:
        C.log("没找到任何媒体文件。")
        return 1

    if args.list:
        for m in media:
            C.log(str(m))
        C.log("共 %d 个文件" % len(media))
        return 0

    device, compute_type = resolve_device(args.device)
    C.log("设备：%s（%s）  模型：%s  共 %d 个文件"
          % (device, compute_type, args.model, len(media)))

    fixes = parse_fixes(args.fix)
    results, t0 = [], time.time()
    for m in media:
        try:
            results.append(process_one(
                m, args.out, args.model, device, compute_type, args.model_dir,
                args.lang, args.prompt, args.beam, fixes, force=args.force))
        except Exception as e:
            C.log("  !! 失败：%s -> %s: %s" % (m.name, type(e).__name__, e))
            results.append({"file": str(m), "status": "error", "error": str(e)})

    ok = sum(1 for r in results if r["status"] == "ok")
    sk = sum(1 for r in results if r["status"] == "skipped")
    er = sum(1 for r in results if r["status"] == "error")
    C.log("\n完成 %d，跳过 %d，失败 %d，总耗时 %.1f 秒" % (ok, sk, er, time.time() - t0))
    C.log(json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
