#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B 站素材获取：解析链接 / 列 UP 主投稿 / 下载视频与字幕。

用法:
    python bili.py resolve <链接或BV号>
    python bili.py list <mid> [N]                     # 列 UP 主最新 N 条
    python bili.py dl <BV号|链接> --out <目录>          # 下载单个视频
    python bili.py up <mid|空间链接> --out <目录> [--limit N]   # 批量下载 UP 主投稿

关键说明:
    - 投稿列表走 **App 端 appkey 签名接口**，无需 cookie。Web 端 /x/space/* 有强风控
      （-412 / -799 / -352），合成指纹过不了，别走那条路。
    - 视频流下载 **必须带 Referer + 浏览器 UA**，否则 403。
    - 未登录状态下清晰度受限（通常最高 480P/720P）。要 1080P 需提供 SESSDATA：
      python bili.py dl BVxxx --out DIR --sessdata "<你的SESSDATA>"
    - 若视频本身有 CC 字幕（UP 主上传或 B 站 AI 字幕），会自动一并下载成 srt，
      这种情况比跑 ASR 准得多，优先用。
"""
import os
import re
import sys
import json
import time
import hashlib
import argparse
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common as C  # noqa: E402

WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
APP_UA = ("Mozilla/5.0 BiliDroid/7.70.0 (bbcallen@gmail.com) os/android "
          "mobi_app/android build/7700300")
APPKEY = "1d8b6e7d45233436"
SECRET = "560c52ccd288fed045859ed18bffd973"


# ---------------------------------------------------------------- HTTP

def http_json(url, headers=None, data=None, timeout=25):
    h = {"User-Agent": WEB_UA}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_bytes(url, headers=None, timeout=60):
    h = {"User-Agent": WEB_UA}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download_stream(url, dest, referer, sessdata=None, chunk=1 << 20):
    h = {"User-Agent": WEB_UA, "Referer": referer, "Origin": "https://www.bilibili.com"}
    if sessdata:
        h["Cookie"] = "SESSDATA=" + sessdata
    req = urllib.request.Request(url, headers=h)
    total = 0
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        while True:
            b = r.read(chunk)
            if not b:
                break
            f.write(b)
            total += len(b)
    return total


# ---------------------------------------------------------------- 签名（App 接口）

def app_sign(params):
    q = urllib.parse.urlencode(sorted(params.items()))
    p = dict(params)
    p["sign"] = hashlib.md5((q + SECRET).encode()).hexdigest()
    return urllib.parse.urlencode(p)


# ---------------------------------------------------------------- 链接解析

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"^av(\d+)$", re.I)
_SPACE_RE = re.compile(r"space\.bilibili\.com/(\d+)")


def resolve(target):
    """把一个链接/ID 解析成 {kind, id}。kind ∈ bv|av|mid。"""
    t = target.strip()
    if t.startswith(("http://", "https://", "b23.tv", "www.bilibili.com")):
        url = t if t.startswith("http") else "https://" + t
        if "b23.tv" in url:
            req = urllib.request.Request(url, headers={"User-Agent": WEB_UA})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    url = r.geturl()
            except Exception as e:
                # 部分环境 HEAD 会 SSL 失败，退回解析 Location
                raise RuntimeError("短链解析失败：%s（%s）" % (url, e))
        m = _SPACE_RE.search(url)
        if m:
            return {"kind": "mid", "id": m.group(1), "url": url}
        m = _BV_RE.search(url)
        if m:
            return {"kind": "bv", "id": m.group(1), "url": url}
        m = re.search(r"/(?:video/)?av(\d+)", url, re.I)
        if m:
            return {"kind": "av", "id": m.group(1), "url": url}
        if re.fullmatch(r"\d{5,}", url.rsplit("/", 1)[-1]):
            return {"kind": "mid", "id": url.rsplit("/", 1)[-1], "url": url}
        raise RuntimeError("认不出这个链接：%s" % url)

    m = _BV_RE.search(t)
    if m:
        return {"kind": "bv", "id": m.group(1)}
    if _AV_RE.match(t):
        return {"kind": "av", "id": _AV_RE.match(t).group(1)}
    if t.isdigit():
        return {"kind": "mid", "id": t}
    raise RuntimeError("认不出这个输入：%s" % t)


# ---------------------------------------------------------------- 投稿列表

def list_up(mid, limit=30, page_size=20, pause=3.0):
    """列 UP 主投稿，按发布时间倒序。"""
    out, seen = [], set()
    pages = max(1, (limit + page_size - 1) // page_size)
    for pn in range(1, pages + 1):
        params = {"vmid": str(mid), "ps": page_size, "pn": pn,
                  "mobi_app": "android", "platform": "android",
                  "appkey": APPKEY, "ts": int(time.time()),
                  "order": "pubdate", "build": 7700300}
        r = http_json("https://app.bilibili.com/x/v2/space/archive?" + app_sign(params))
        if r.get("code") != 0:
            C.log("  pn=%d 失败：%s %s" % (pn, r.get("code"), r.get("message")))
            break
        data = r.get("data") or {}
        items = data.get("item") or []
        for it in items:
            if it["bvid"] in seen:
                continue
            seen.add(it["bvid"])
            st = it.get("stat") or {}
            out.append({
                "bvid": it["bvid"],
                "title": it["title"],
                "date": time.strftime("%Y-%m-%d",
                                      time.localtime(it.get("ctime") or it.get("pubdate") or 0)),
                "dur_sec": it.get("duration"),
                "play": it.get("play") or st.get("view"),
            })
        total = data.get("count")
        if not items or len(out) >= limit or (total and len(out) >= total):
            break
        time.sleep(pause)
    return out[:limit]


# ---------------------------------------------------------------- 视频信息

def get_view(bvid, sessdata=None, aid=None):
    q = "bvid=" + bvid if bvid else "aid=" + str(aid)
    h = {"Referer": "https://www.bilibili.com/"}
    if sessdata:
        h["Cookie"] = "SESSDATA=" + sessdata
    r = http_json("https://api.bilibili.com/x/web-interface/view?" + q, headers=h)
    if r.get("code") != 0:
        raise RuntimeError("取视频信息失败：%s %s" % (r.get("code"), r.get("message")))
    return r["data"]


def get_playurl(bvid, cid, sessdata=None, qn=120):
    h = {"Referer": "https://www.bilibili.com/video/" + bvid}
    if sessdata:
        h["Cookie"] = "SESSDATA=" + sessdata
    url = ("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s"
           "&fnval=16&fnver=0&fourk=1&qn=%d&platform=pc" % (bvid, cid, qn))
    r = http_json(url, headers=h)
    if r.get("code") != 0:
        raise RuntimeError("取播放地址失败：%s %s" % (r.get("code"), r.get("message")))
    return r["data"]


def get_subtitles(bvid, cid, sessdata=None):
    """取 CC 字幕列表（可能有），返回 [{lan, url}]。"""
    h = {"Referer": "https://www.bilibili.com/video/" + bvid}
    if sessdata:
        h["Cookie"] = "SESSDATA=" + sessdata
    try:
        r = http_json("https://api.bilibili.com/x/player/v2?bvid=%s&cid=%s" % (bvid, cid),
                      headers=h)
        subs = ((r.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        return [{"lan": s.get("lan"), "lan_doc": s.get("lan_doc"),
                 "url": "https:" + s["subtitle_url"] if s.get("subtitle_url", "").startswith("//")
                 else s.get("subtitle_url")}
                for s in subs if s.get("subtitle_url")]
    except Exception:
        return []


def save_subtitle_as_srt(sub_url, dest, sessdata=None):
    data = json.loads(http_bytes(sub_url, headers={
        "Referer": "https://www.bilibili.com/"}).decode("utf-8"))
    body = data.get("body") or []
    lines = []
    for i, seg in enumerate(body, 1):
        lines.append(str(i))
        lines.append("%s --> %s" % (C.srt_ts(seg.get("from", 0)), C.srt_ts(seg.get("to", 0))))
        lines.append(str(seg.get("content", "")).strip())
        lines.append("")
    Path(dest).write_text("\n".join(lines), encoding="utf-8")
    return len(body)


# ---------------------------------------------------------------- 下载

def pick_streams(dash, prefer_height=None):
    """从 dash 里挑画质最高的视频流 + 码率最高的音频流。"""
    vids = dash.get("video") or []
    auds = dash.get("audio") or []
    if not vids:
        raise RuntimeError("该视频没有 dash 视频流（可能需登录或为付费内容）")

    def vkey(v):
        return ((v.get("height") or 0), (v.get("bandwidth") or 0))

    vids_sorted = sorted(vids, key=vkey, reverse=True)
    if prefer_height:
        cand = [v for v in vids_sorted if (v.get("height") or 0) <= prefer_height]
        if cand:
            vids_sorted = cand
    v = vids_sorted[0]
    a = sorted(auds, key=lambda x: x.get("bandwidth") or 0, reverse=True)[0] if auds else None
    return v, a


def download_bvid(bvid, out_root, sessdata=None, all_parts=False, quality=None,
                  keep_streams=False, quiet=False):
    view = get_view(bvid, sessdata)
    title = C.safe_name(view.get("title") or bvid)
    up = (view.get("owner") or {}).get("name") or ""
    pages = view.get("pages") or []
    out_dir = C.ensure_dir(Path(out_root) / title)

    targets = pages if (all_parts and pages) else pages[:1]
    results = []
    for idx, pg in enumerate(targets, 1):
        cid = pg.get("cid")
        part = C.safe_name(pg.get("part") or ("P%d" % idx))
        stem = title if len(targets) == 1 else "%s_%s" % (title, part)
        final = out_dir / (stem + ".mp4")
        if final.exists() and not keep_streams:
            if not quiet:
                C.log("    已存在，跳过：%s" % final.name)
            results.append({"bvid": bvid, "cid": cid, "file": str(final), "status": "exists"})
            continue

        if not quiet:
            C.log("    P%d/%d %s" % (idx, len(targets), part))
        d = get_playurl(bvid, cid, sessdata)
        dash = d.get("dash") or {}
        try:
            v, a = pick_streams(dash, prefer_height=quality)
        except Exception as e:
            # 退回 durl（老接口，单文件 flv/mp4）
            durl = d.get("durl") or []
            if not durl:
                raise
            tmp = out_dir / (stem + ".dl.mp4")
            download_stream(durl[0]["url"], str(tmp),
                            referer="https://www.bilibili.com/video/" + bvid,
                            sessdata=sessdata)
            tmp.replace(final)
            results.append({"bvid": bvid, "cid": cid, "file": str(final), "status": "ok"})
            continue

        vtmp = out_dir / (stem + ".v.m4s")
        atmp = out_dir / (stem + ".a.m4s")
        try:
            download_stream(v["baseUrl"], str(vtmp),
                            referer="https://www.bilibili.com/video/" + bvid,
                            sessdata=sessdata)
            if a:
                download_stream(a["baseUrl"], str(atmp),
                                referer="https://www.bilibili.com/video/" + bvid,
                                sessdata=sessdata)
                C.run([C.ffmpeg(), "-y", "-v", "error", "-i", str(vtmp), "-i", str(atmp),
                       "-c", "copy", "-movflags", "+faststart", str(final)])
            else:
                C.run([C.ffmpeg(), "-y", "-v", "error", "-i", str(vtmp),
                       "-c", "copy", str(final)])
        finally:
            for t in (vtmp, atmp):
                try:
                    t.unlink(missing_ok=True)
                except Exception:
                    pass

        # 顺手把 CC 字幕拉下来（有的话比跑 ASR 准）
        subs = get_subtitles(bvid, cid, sessdata)
        if subs:
            try:
                n = save_subtitle_as_srt(subs[0]["url"], out_dir / (stem + ".srt"), sessdata)
                if not quiet:
                    C.log("      附字幕：%s（%d 条）" % (subs[0].get("lan_doc"), n))
            except Exception:
                pass

        h = (v.get("height") or 0)
        results.append({"bvid": bvid, "cid": cid, "file": str(final),
                        "status": "ok", "height": h, "up": up})
    return {"title": title, "up": up, "dir": str(out_dir), "items": results}


# ---------------------------------------------------------------- CLI

def cmd_resolve(target):
    r = resolve(target)
    C.log(json.dumps(r, ensure_ascii=False))
    if r["kind"] == "bv":
        v = get_view(r["id"])
        C.log("标题：%s\nUP：%s\n时长：%s 秒\n分P：%d"
              % (v.get("title"), (v.get("owner") or {}).get("name"),
                 v.get("duration"), len(v.get("pages") or [])))
    return 0


def cmd_list(mid, n):
    vids = list_up(mid, n)
    C.log("%-11s %7s %10s  %s" % ("date", "dur(s)", "play", "title"))
    for v in vids:
        C.log("%-11s %7s %10s  %s" % (v["date"], v["dur_sec"], v["play"], v["title"]))
    C.log("\n共 %d 条" % len(vids))
    return 0


def cmd_dl(target, args):
    r = resolve(target)
    if r["kind"] == "mid":
        return cmd_up(r["id"], args)
    bvid = r["id"]
    if r["kind"] == "av":
        v = get_view(None, args.sessdata, aid=r["id"])
        bvid = v["bvid"]
    out = download_bvid(bvid, args.out, sessdata=args.sessdata,
                        all_parts=args.all_parts, quality=args.quality)
    C.log(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_up(target, args):
    r = resolve(target)
    mid = r["id"]
    vids = list_up(mid, args.limit)
    C.log("UP 主 %s 最近 %d 条：" % (mid, len(vids)))
    for v in vids:
        C.log("  %s  %6ss  %s" % (v["date"], v["dur_sec"], v["title"]))
    out_root = C.ensure_dir(Path(args.out) / ("up_" + mid))
    ok = 0
    for v in vids:
        if args.max_duration and (v["dur_sec"] or 0) > args.max_duration:
            C.log("  跳过（超过 %ds）：%s" % (args.max_duration, v["title"]))
            continue
        try:
            download_bvid(v["bvid"], out_root, sessdata=args.sessdata,
                          quality=args.quality)
            ok += 1
        except Exception as e:
            C.log("  !! %s 失败：%s" % (v["bvid"], e))
        time.sleep(1.5)
    C.log("\n成功下载 %d / %d，输出在 %s" % (ok, len(vids), out_root))
    return 0


def main():
    ap = argparse.ArgumentParser(description="B 站素材获取")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("resolve"); p.add_argument("target")

    p = sub.add_parser("list")
    p.add_argument("mid"); p.add_argument("n", nargs="?", type=int, default=30)

    def add_dl_args(p):
        p.add_argument("target")
        p.add_argument("--out", required=True)
        p.add_argument("--sessdata", default=os.environ.get("BILI_SESSDATA"))
        p.add_argument("--quality", type=int, default=None,
                       help="限制最高清晰度高度，如 720")
        p.add_argument("--all-parts", action="store_true", help="多分P全下")
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--max-duration", type=int, default=0,
                       help="只下不超过这个秒数的视频，0=不限")

    p = sub.add_parser("dl"); add_dl_args(p)
    p = sub.add_parser("up"); add_dl_args(p)

    args = ap.parse_args()
    if args.cmd == "resolve":
        return cmd_resolve(args.target)
    if args.cmd == "list":
        return cmd_list(args.mid, args.n)
    if args.cmd == "dl":
        return cmd_dl(args.target, args)
    if args.cmd == "up":
        return cmd_up(args.target, args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
