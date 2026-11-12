#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
总调度：把「转写」和「抽帧」两路串起来，批量跑完一批视频。

用法:
    # 本地文件夹
    python batch.py "D:/我的视频" --out "C:/Users/me/Downloads/视频解析"
    # 混合：本地文件 + B站链接
    python batch.py "D:/a.mp4" "https://www.bilibili.com/video/BVxxxx" --out ...
    # UP 主批量（先列后下）
    python batch.py --bili-up 123456 --up-limit 10 --out ...

产出:
    <out>/_index.md                     本批汇总表
    <out>/<视频名>/transcript.md       段落化逐字稿（带时间戳）
    <out>/<视频名>/transcript.srt      标准字幕
    <out>/<视频名>/segments.json       原始分段
    <out>/<视频名>/overview_01.jpg …   关键帧总览图
    <out>/<视频名>/frames/kf_*.jpg     单帧原图（补读细节用）
    <out>/<视频名>/keyframes.json      关键帧元信息
"""
import sys
import os
import re
import json
import time
import glob
import shutil
import argparse
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import common as C      # noqa: E402
import asr              # noqa: E402
import frames           # noqa: E402
import bili             # noqa: E402

DEFAULT_OUT = str(Path.home() / "Downloads" / "视频解析")


def build_frames_args(a):
    return SimpleNamespace(
        force=a.force,
        sample_fps=a.sample_fps, scan_width=a.scan_width,
        threshold=a.threshold, noise_k=a.noise_k,
        min_time=a.min_time, motion_time=a.motion_time,
        max_frames=a.max_frames, no_last=a.no_last,
        quality=a.quality, max_width=a.max_width,
        cols=a.cols, sheet_max_cells=a.sheet_max_cells,
        sheet_max_sheets=a.sheet_max_sheets,
        sheet_width=a.sheet_width, sheet_quality=a.sheet_quality,
        sheet_only=a.sheet_only,
        workers=getattr(a, "workers", 0),
    )


def plain_body(out_dir):
    """取一份不带时间戳的逐字稿正文，供汇总分析附带。

    优先用 segments.json 重新并段；没有就退回 transcript.md 去掉 **[mm:ss]** 前缀。
    这样即便本批跳过了转写（--skip-asr）、沿用旧结果，汇总里也能带上文字稿。
    """
    d = Path(out_dir)
    seg = d / "segments.json"
    if seg.exists():
        try:
            items = json.loads(seg.read_text(encoding="utf-8"))
            paras = asr.merge_paragraphs(items)
            body = "\n\n".join(p["text"].strip() for p in paras if p["text"].strip())
            if body:
                return body
        except Exception:
            pass
    md = d / "transcript.md"
    if md.exists():
        try:
            got = []
            for ln in md.read_text(encoding="utf-8", errors="replace").splitlines():
                if ln.startswith("**["):
                    got.append(re.sub(r"^\*\*\[[^\]]+\]\*\*\s*", "", ln))
            return "\n\n".join(x for x in got if x.strip())
        except Exception:
            pass
    return ""


def write_index(out_root, rows, attach_transcript=False):
    lines = [
        "# 视频解析汇总",
        "",
        "> 由 video-batch-insight 生成本地解析：每个视频两路产出——",
        "> **逐字稿**（音频通道，ASR）与**关键帧总览图**（画面通道，读 PPT/图表/屏幕文字）。",
        "",
        "| # | 视频 | 时长 | 字数 | 关键帧 | 总览图 | 逐字稿 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        name = r.get("name", "")
        d = r.get("dir", "")
        rel = Path(d).name if d else ""
        dur = C.hhmmss(r["dur"]) if r.get("dur") else "—"
        chars = r.get("chars", "—")
        kf = r.get("kf", "—")
        ov = r.get("overviews", 0)
        md = "[transcript.md](%s/transcript.md)" % rel if r.get("chars") else "—"
        lines.append("| %d | %s | %s | %s | %s | %s | %s |"
                     % (i, name, dur, chars, kf, ov, md))
    lines += ["", "生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"), ""]
    n = 0
    if attach_transcript:
        lines += ["---", "", "## 文字稿全文", "",
                  "> 以下为本地 ASR 转写正文（已去掉时间戳），**不是官方字幕**；"
                  "人名 / 机构 / 术语可能有同音误写。", ""]
        for r in rows:
            body = plain_body(r.get("dir", ""))
            if not body:
                continue
            n += 1
            lines += ["### %d. %s" % (n, r.get("name", "")), "", body, ""]
        if n == 0:
            lines += ["（本批没有可用文字稿：可能未勾选「音频转文字」，或素材没有音轨。）", ""]
        lines += ["> 以上共 %d 篇。" % n, ""]
    p = Path(out_root) / "_index.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p, n


def main():
    ap = argparse.ArgumentParser(description="批量视频解析（转写 + 关键帧）")
    ap.add_argument("inputs", nargs="*", help="本地文件/文件夹，或 B站链接")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出根目录")
    ap.add_argument("--download-dir", default=None,
                    help="B站视频下载到哪里，默认为 <out>/_素材")

    # B 站批量
    ap.add_argument("--bili-up", default=None, help="UP 主 mid，批量下载其投稿")
    ap.add_argument("--up-limit", type=int, default=10)
    ap.add_argument("--up-max-duration", type=int, default=0, help="只下不超过该秒数的")
    ap.add_argument("--sessdata", default=None, help="B站登录态，可解锁 1080P")

    # 阶段开关
    ap.add_argument("--skip-asr", action="store_true")
    ap.add_argument("--skip-frames", action="store_true")
    ap.add_argument("--attach-transcript", action="store_true",
                    help="把每个视频的文字稿全文附到 _index.md 汇总分析里")
    ap.add_argument("--force", action="store_true", help="覆盖已完成的结果")
    ap.add_argument("--video-only", action="store_true",
                    help="只收视频，忽略纯音频文件")
    ap.add_argument("--allow-ts", action="store_true",
                    help="把 .ts 也当视频（默认排除：目录里 .ts 多为 TypeScript 源码）")
    ap.add_argument("--list-only", action="store_true",
                    help="只列出将要处理的文件并统计，不实际解析")

    # 转写参数
    ap.add_argument("--model", default=asr.DEFAULT_MODEL)
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--prompt", default=asr.DEFAULT_PROMPT)
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--model-dir", default=asr.DEFAULT_MODEL_DIR)
    ap.add_argument("--fix", action="append", default=[])

    # 抽帧参数
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--sheet-max-cells", type=int, default=12)
    ap.add_argument("--sheet-max-sheets", type=int, default=4)
    ap.add_argument("--sheet-width", type=int, default=1600)
    ap.add_argument("--sheet-quality", type=int, default=95)
    ap.add_argument("--quality", type=int, default=92)
    ap.add_argument("--max-width", type=int, default=0)
    ap.add_argument("--sample-fps", type=float, default=4.0)
    ap.add_argument("--scan-width", type=int, default=200)
    ap.add_argument("--threshold", type=float, default=0.0)
    ap.add_argument("--noise-k", type=float, default=4.0)
    ap.add_argument("--min-time", type=float, default=0.30)
    ap.add_argument("--motion-time", type=float, default=1.5)
    ap.add_argument("--max-frames", type=int, default=200)
    ap.add_argument("--no-last", action="store_true")
    ap.add_argument("--sheet-only", action="store_true")
    ap.add_argument("--workers", type=int, default=0,
                    help="抽帧并发数，0=按 CPU 自动（默认 4~8）")
    ap.add_argument("--list", action="store_true", help="只列出将要处理的文件")
    args = ap.parse_args()

    # 命令行直接跑（不经 run_detached.py）时也要走国内镜像：
    # 否则 faster-whisper 检查模型版本会撞 huggingface.co，代理环境里报 502。
    if not os.environ.get("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

    out_root = C.ensure_dir(args.out)
    dl_dir = C.ensure_dir(args.download_dir or (Path(out_root) / "_素材"))

    # 开机顺手清一下上次异常中止残留的音频临时目录（是隐形的磁盘黑洞）
    try:
        tmp_root = tempfile.gettempdir()
        for d in glob.glob(os.path.join(tmp_root, "vbi_*")):
            try:
                # 只清 1 小时前的，避免误删别的进程正在用的
                if time.time() - os.path.getmtime(d) > 3600:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass

    local_inputs, urls = [], []
    for x in args.inputs:
        if x.startswith(("http://", "https://", "b23.tv")):
            urls.append(x)
        else:
            local_inputs.append(x)

    # ---- 1. B 站下载 ----
    if args.bili_up:
        C.log("== 拉取 UP 主 %s 的投稿 ==" % args.bili_up)
        vids = bili.list_up(args.bili_up, args.up_limit)
        up_root = C.ensure_dir(dl_dir / ("up_" + str(args.bili_up)))
        for v in vids:
            if args.up_max_duration and (v["dur_sec"] or 0) > args.up_max_duration:
                C.log("  跳过（太长）：%s" % v["title"])
                continue
            try:
                bili.download_bvid(v["bvid"], up_root, sessdata=args.sessdata)
            except Exception as e:
                C.log("  !! %s 失败：%s" % (v["bvid"], e))
            time.sleep(1.5)
        local_inputs.append(str(up_root))

    for u in urls:
        C.log("== 下载 %s ==" % u)
        try:
            r = bili.resolve(u)
            if r["kind"] == "mid":
                vids = bili.list_up(r["id"], args.up_limit)
                up_root = C.ensure_dir(dl_dir / ("up_" + r["id"]))
                for v in vids:
                    bili.download_bvid(v["bvid"], up_root, sessdata=args.sessdata)
                    time.sleep(1.5)
                local_inputs.append(str(up_root))
            else:
                bvid = r["id"]
                if r["kind"] == "av":
                    bvid = bili.get_view(None, args.sessdata, aid=r["id"])["bvid"]
                bili.download_bvid(bvid, dl_dir, sessdata=args.sessdata)
                local_inputs.append(str(dl_dir))
        except Exception as e:
            C.log("  !! 下载失败：%s（%s）" % (u, e))

    # ---- 2. 枚举媒体 ----
    media = C.collect_media(local_inputs,
                            include_audio=not args.video_only,
                            allow_ts=args.allow_ts)
    if not media:
        C.log("没有可处理的媒体文件。")
        C.log("（提示：已自动跳过 node_modules / .git 等代码目录，"
              "以及 .ts 这类同名代码文件。如果确定要看 .ts 视频，加 --allow-ts）")
        C.progress("end", 0, 0, "", "0")
        return 1
    if args.list or args.list_only:
        if args.list:
            for m in media:
                C.log(str(m))
        C.log("共 %d 个文件" % len(media))
        C.progress("end", 0, len(media), "", "0")
        return 0

    # ---- 3. 设备与模型（整批只载一次）----
    device, compute_type = asr.resolve_device(args.device)
    if not args.skip_asr:
        asr.load_model(args.model, device, compute_type, args.model_dir)

    C.log("")
    C.log("=" * 64)
    C.log("输出目录：%s" % out_root)
    C.log("共 %d 个文件   设备：%s/%s   模型：%s" % (len(media), device, compute_type, args.model))
    C.log("转写：%s   抽帧：%s"
          % ("关" if args.skip_asr else "开", "关" if args.skip_frames else "开"))
    C.log("=" * 64)

    fixes = asr.parse_fixes(args.fix)
    fargs = build_frames_args(args)
    rows, t0 = [], time.time()
    C.progress("begin", 0, len(media))

    for i, m in enumerate(media, 1):
        C.log("\n[%d/%d] %s" % (i, len(media), m.name))
        C.progress("item", i, len(media), m.name)
        row = {"name": m.name, "file": str(m)}
        t_file = time.time()
        has_video = True
        try:
            info = C.probe(m)
            row["dur"] = info.get("duration") or 0
            has_video = bool(info.get("has_video"))
        except Exception:
            row["dur"] = 0
        row["dir"] = str(Path(out_root) / C.safe_name(m.stem))

        if not args.skip_asr:
            t_stage = time.time()
            try:
                r = asr.process_one(m, out_root, args.model, device, compute_type,
                                    args.model_dir, args.lang, args.prompt,
                                    args.beam, fixes, force=args.force)
                if r.get("meta"):
                    row["chars"] = r["meta"]["chars"]
                    row["asr_speed"] = r["meta"]["speed"]
                    row["asr_sec"] = r["meta"]["elapsed"]
            except Exception as e:
                C.log("  !! 转写失败：%s: %s" % (type(e).__name__, e))
                row["chars"] = 0
            C.progress("stage", i, len(media), m.name,
                       "转写 %.0f 秒" % (time.time() - t_stage))

        # 文件夹里混进纯音频时别硬跑抽帧（没有画面流，只会刷一条报错）
        if not args.skip_frames and not has_video:
            C.log("    纯音频，跳过抽帧（无画面流）")
        elif not args.skip_frames:
            t_stage = time.time()
            try:
                r = frames.process_one(m, out_root, fargs)
                if r.get("meta"):
                    row["kf"] = r["meta"]["keyframe_count"]
                    row["overviews"] = r["meta"]["overview_count"]
            except Exception as e:
                C.log("  !! 抽帧失败：%s: %s" % (type(e).__name__, e))
            C.progress("stage", i, len(media), m.name,
                       "抽帧 %.0f 秒" % (time.time() - t_stage))

        row["sec"] = round(time.time() - t_file, 1)
        rows.append(row)
        C.log("  ← 第 %d/%d 条完成，本条用时 %.0f 秒" % (i, len(media), row["sec"]))

    idx, n_attached = write_index(out_root, rows,
                                  attach_transcript=args.attach_transcript)
    if args.attach_transcript:
        C.log("汇总分析已附带 %d 篇文字稿全文" % n_attached)
    el = time.time() - t0
    C.progress("end", len(media), len(media), "", "%.0f" % el)
    C.log("\n" + "=" * 64)
    C.log("全部完成：%d 个文件，总用时 %.1f 秒（%.1f 分钟）"
          % (len(rows), el, el / 60))
    if rows:
        C.log("平均每个 %.1f 秒" % (el / len(rows)))
    C.log("汇总表：%s" % idx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
