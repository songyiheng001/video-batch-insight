#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
关键帧抽取：把一段视频压成几张「大模型读得清」的总览图。

用法:
    python frames.py <视频...> --out <输出目录>
    python frames.py demo.mp4 --cols 3 --sheet-max-cells 12 --sheet-width 1600

产出（每个视频一个子目录）:
    <out>/<视频名>/overview_01.jpg ...   关键帧总览图（先读这些）
    <out>/<视频名>/frames/kf_001_0.0s.jpg  单帧原图（字看不清时补读）
    <out>/<视频名>/keyframes.json        尺寸/时长/帧率/每帧时间戳

设计要点（直接决定模型能不能读清）:
    - 多模态模型的视觉编码器会把整张图压到长边约 1080~1568px。
      所以每格能分到多少像素，只取决于「这张图里有多少格」，跟图做多大基本无关。
      => 每张 ≤12 格、列数 ≤4、图宽 ≤2400（1600 是安全落点）。
    - 逐帧差异同时看两个角度，取较大值：全局平均差异（抓转场）+ 8×8 分块
      最大差异（抓角落小动作）。只看全局会被大量没变的像素稀释掉。
    - 阈值自适应 = 画面差异中位数（≈底噪）× 灵敏度，夹在 [0.008, 0.08]，不用手调。
    - 画面变了不急着选，等动画结束、稳定下来的帧才入选，避免抽到空白中间态。
"""
import sys
import os
import json
import math
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import common as C  # noqa: E402


# ---------------------------------------------------------------- 差异扫描

def scan_diffs(path, sample_fps=4.0, scan_width=200):
    """
    低分辨率灰度扫描，返回 (scores, times)。
    scores[i] 表示第 i 帧相对前一帧的变化强度（0~1）；scores[0] 恒为 0。
    """
    info = C.probe(path)
    w0 = info["width"] or 0
    h0 = info["height"] or 0
    if not info["has_video"] or not w0 or not h0:
        raise RuntimeError("不是视频文件或无法读取尺寸：%s" % path)

    # 计算等比缩放后的高度，取偶数（rawvideo 要求）
    hh = int(round(h0 * scan_width / float(w0)))
    hh = max(2, hh - (hh % 2))
    vf = "fps=%g,scale=%d:%d,format=gray" % (sample_fps, scan_width, hh)

    p = C.run([C.ffmpeg(), "-v", "error", "-i", str(path),
               "-an", "-sn", "-vf", vf,
               "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
              capture=True, text=False)
    buf = p.stdout
    cell = scan_width * hh
    n = len(buf) // cell
    if n < 2:
        raise RuntimeError("视频太短或解码失败：%s" % path)
    arr = np.frombuffer(buf[:n * cell], dtype=np.uint8).reshape(n, hh, scan_width)

    a = arr.astype(np.int16)
    diff = np.abs(a[1:] - a[:-1])            # (n-1, h, w)

    glob = diff.mean(axis=(1, 2)) / 255.0    # 全局平均差异
    # 8×8 分块，取变化最剧烈那一块
    bh, bw = max(1, hh // 8), max(1, scan_width // 8)
    blocks = []
    for by in range(0, hh - bh + 1, bh):
        for bx in range(0, scan_width - bw + 1, bw):
            blocks.append(diff[:, by:by + bh, bx:bx + bw].mean(axis=(1, 2)) / 255.0)
    blk = np.max(np.stack(blocks, axis=0), axis=0)   # (n-1,)

    scores = np.concatenate([[0.0], np.maximum(glob, blk)])
    times = np.arange(n) / float(sample_fps)
    return scores, times, info


def auto_threshold(scores, noise_k=4.0, lo=0.008, hi=0.08):
    """阈值 = 差异中位数（≈底噪）× 灵敏度，夹在 [lo, hi]。"""
    s = scores[1:] if len(scores) > 1 else scores
    base = float(np.median(s)) if len(s) else 0.0
    return min(hi, max(lo, base * noise_k))


# ---------------------------------------------------------------- 选帧

def pick_keyframes(scores, times, threshold, min_time=0.30, motion_time=1.5,
                   max_frames=200, include_last=True):
    """按下标挑出关键帧。返回下标列表。"""
    n = len(scores)
    sel = [0]
    last_t = 0.0
    i = 1
    while i < n:
        if scores[i] > threshold:
            j = i
            while j + 1 < n and scores[j + 1] > threshold:
                j += 1
                # 画面持续在变（运镜/长动效）：每隔 motion_time 补一张
                if times[j] - last_t >= motion_time:
                    sel.append(j)
                    last_t = times[j]
            # 等它变完：取变化结束后稳定下来的那一帧
            k = min(j + 1, n - 1)
            if times[k] - last_t >= min_time and k != sel[-1]:
                sel.append(k)
                last_t = times[k]
            i = j + 1
        else:
            i += 1

    if include_last and n - 1 != sel[-1] and times[-1] - last_t >= min_time:
        sel.append(n - 1)

    sel = sorted(set(sel))
    if len(sel) > max_frames:
        idx = np.linspace(0, len(sel) - 1, max_frames).round().astype(int)
        sel = [sel[k] for k in sorted(set(idx))]
    return sel


# ---------------------------------------------------------------- 抽帧

def _grab_one(path, t, idx, out_dir, quality, max_width):
    """抽单帧（独立进程，可并行）。失败返回 None。"""
    p = out_dir / ("kf_%03d_%.1fs.jpg" % (idx, t))
    cmd = [C.ffmpeg(), "-y", "-v", "error", "-ss", "%.3f" % t, "-i", str(path),
           "-frames:v", "1"]
    if max_width:
        cmd += ["-vf", "scale='min(%d,iw)':-2" % max_width]
    cmd += ["-q:v", str(max(2, min(31, 31 - int(quality / 3.5)))), str(p)]
    try:
        C.run(cmd)
    except Exception:
        return None
    if p.exists() and p.stat().st_size > 0:
        return (t, p)
    p.unlink(missing_ok=True)
    return None


def default_workers():
    """并发度：跟着 CPU 核数走，但别把磁盘/解码器打爆。"""
    n = os.cpu_count() or 4
    return max(4, min(8, n // 2 if n >= 8 else 4))


def extract_frames(path, times_sec, out_dir, quality=92, max_width=0, workers=None):
    """
    按时间点抽原图。多个 ffmpeg 并发跑（原来是逐帧串行，200 帧要起 200 次进程）。
    返回 [(time, Path)]，按时间升序。
    """
    out_dir = C.ensure_dir(out_dir)
    if not times_sec:
        return []
    workers = max(1, min(int(workers or default_workers()), 16, len(times_sec)))
    if workers == 1:
        got = []
        for i, t in enumerate(times_sec, 1):
            r = _grab_one(path, t, i, out_dir, quality, max_width)
            if r:
                got.append(r)
        return got

    got = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_grab_one, path, t, i, out_dir, quality, max_width)
                for i, t in enumerate(times_sec, 1)]
        for fu in as_completed(futs):
            try:
                r = fu.result()
            except Exception:
                r = None
            if r:
                got.append(r)
    got.sort(key=lambda x: x[0])
    return got


# ---------------------------------------------------------------- 拼总览图

def _load_font(size):
    from PIL import ImageFont
    for cand in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyh.ttf",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            return ImageFont.truetype(cand, size)
        except Exception:
            continue
    return ImageFont.load_default()


def compose_sheets(items, out_dir, cols=3, max_cells=12, max_sheets=4,
                   sheet_width=1600, quality=92):
    """
    items: [(time, Path)]。返回总览图路径列表。
    先抽稀到 max_sheets*min(max_cells,16) 以内，再尽量平均地分张。
    """
    from PIL import Image

    if not items:
        return []
    cap = max_sheets * min(max_cells, 16)
    if len(items) > cap:
        idx = np.linspace(0, len(items) - 1, cap).round().astype(int)
        items = [items[k] for k in sorted(set(idx))]

    n_sheets = max(1, math.ceil(len(items) / float(max_cells)))
    base, extra = divmod(len(items), n_sheets)
    chunks, pos = [], 0
    for s in range(n_sheets):
        k = base + (1 if s < extra else 0)
        chunks.append(items[pos:pos + k])
        pos += k

    # 网格几何
    gap = 8
    cell_w = max(80, int((sheet_width - gap * (cols + 1)) / cols))
    sample = Image.open(items[0][1])
    ratio = sample.height / float(sample.width)
    cell_h = max(40, int(cell_w * ratio))
    font_size = max(11, int(cell_w / 16))
    label_h = font_size + 8
    font = _load_font(font_size)

    out_paths = []
    for si, chunk in enumerate(chunks, 1):
        rows = int(math.ceil(len(chunk) / float(cols)))
        W = cell_w * cols + gap * (cols + 1)
        H = (cell_h + label_h) * rows + gap * (rows + 1)
        sheet = Image.new("RGB", (W, H), (255, 255, 255))
        from PIL import ImageDraw
        draw = ImageDraw.Draw(sheet)
        for ci, (t, fp) in enumerate(chunk):
            r, c = divmod(ci, cols)
            x = gap + c * (cell_w + gap)
            y = gap + r * (cell_h + label_h + gap)
            try:
                im = Image.open(fp).convert("RGB")
                im = im.resize((cell_w, cell_h), Image.LANCZOS)
                sheet.paste(im, (x, y))
            except Exception:
                draw.rectangle([x, y, x + cell_w, y + cell_h], outline=(200, 200, 200))
            draw.rectangle([x, y, x + cell_w, y + cell_h], outline=(190, 190, 190), width=1)
            label = "#%02d  %s" % (ci + 1, C.hhmmss(t))
            draw.text((x + 2, y + cell_h + 2), label, fill=(0, 0, 0), font=font)
        p = Path(out_dir) / ("overview_%02d.jpg" % si)
        sheet.save(str(p), "JPEG", quality=quality, subsampling=0)
        out_paths.append(p)
    return out_paths


# ---------------------------------------------------------------- 单文件流程

def process_one(path, out_root, args):
    path = Path(path)
    out_dir = C.ensure_dir(Path(out_root) / C.safe_name(path.stem))
    json_path = out_dir / "keyframes.json"
    if json_path.exists() and not args.force:
        C.log("  跳过（已完成）：%s" % path.name)
        return {"file": str(path), "status": "skipped", "out": str(out_dir)}

    C.log("  → %s" % path.name)
    scores, times, info = scan_diffs(path, args.sample_fps, args.scan_width)
    thr = args.threshold if args.threshold and args.threshold > 0 else \
        auto_threshold(scores, args.noise_k)
    sel = pick_keyframes(scores, times, thr,
                         min_time=args.min_time, motion_time=args.motion_time,
                         max_frames=args.max_frames,
                         include_last=not args.no_last)
    chosen_t = [float(times[i]) for i in sel]
    C.log("    差异阈值 %.4f，选出 %d 帧 / 共 %d 帧采样"
          % (thr, len(chosen_t), len(times)))

    fdir = out_dir / "frames"
    got = extract_frames(path, chosen_t, fdir, quality=args.quality,
                         max_width=args.max_width)
    sheets = compose_sheets(got, out_dir, cols=args.cols,
                            max_cells=args.sheet_max_cells,
                            max_sheets=args.sheet_max_sheets,
                            sheet_width=args.sheet_width,
                            quality=args.sheet_quality)

    meta = {
        "source": str(path),
        "width": info["width"], "height": info["height"],
        "duration": round(info["duration"], 2),
        "duration_text": C.hhmmss(info["duration"]),
        "fps": info["fps"],
        "threshold": round(thr, 5),
        "keyframe_count": len(got),
        "overview_count": len(sheets),
        "overview": str(sheets[0]) if sheets else None,
        "overviews": [str(s) for s in sheets],
        "keyframes": [{"n": i + 1, "time": round(t, 2), "path": str(p)}
                      for i, (t, p) in enumerate(got)],
    }
    json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.sheet_only:
        for _, p in got:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            fdir.rmdir()
        except Exception:
            pass

    C.log("    完成：%d 帧 → %d 张总览图" % (len(got), len(sheets)))
    return {"file": str(path), "status": "ok", "out": str(out_dir), "meta": meta}


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="抽取关键帧并拼成总览图")
    ap.add_argument("inputs", nargs="+", help="视频文件或文件夹")
    ap.add_argument("--out", required=True, help="输出根目录")
    ap.add_argument("--cols", type=int, default=3, help="每行列数，给模型读固定 3~4")
    ap.add_argument("--sheet-max-cells", type=int, default=12, help="每张最多几格")
    ap.add_argument("--sheet-max-sheets", type=int, default=4, help="最多几张")
    ap.add_argument("--sheet-width", type=int, default=1600,
                    help="总览图宽度，给模型读 1600~2400")
    ap.add_argument("--sheet-quality", type=int, default=95)
    ap.add_argument("--quality", type=int, default=92, help="单帧 JPEG 质量")
    ap.add_argument("--max-width", type=int, default=0, help="单帧最大宽度，0=原尺寸")
    ap.add_argument("--sample-fps", type=float, default=4.0, help="差异检测采样帧率")
    ap.add_argument("--scan-width", type=int, default=200, help="差异检测宽度")
    ap.add_argument("--threshold", type=float, default=0.0, help="0=自动")
    ap.add_argument("--noise-k", type=float, default=4.0, help="自动阈值灵敏度")
    ap.add_argument("--min-time", type=float, default=0.30)
    ap.add_argument("--motion-time", type=float, default=1.5)
    ap.add_argument("--max-frames", type=int, default=200)
    ap.add_argument("--no-last", action="store_true", help="不强制保留末帧")
    ap.add_argument("--sheet-only", action="store_true", help="只留总览图，不留单帧")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    media = C.collect_media(args.inputs, include_audio=False)
    if not media:
        C.log("没找到视频文件。")
        return 1
    if args.list:
        for m in media:
            C.log(str(m))
        return 0

    C.log("共 %d 个视频，总览图档位：%d 列 / 每张 %d 格 / 宽 %dpx"
          % (len(media), args.cols, args.sheet_max_cells, args.sheet_width))
    results = []
    for m in media:
        try:
            results.append(process_one(m, args.out, args))
        except Exception as e:
            C.log("  !! 失败：%s -> %s: %s" % (m.name, type(e).__name__, e))
            results.append({"file": str(m), "status": "error", "error": str(e)})
    C.log(json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
