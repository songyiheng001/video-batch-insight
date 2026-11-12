---
name: video-batch-insight
description: 把本地视频（或 B站链接 / UP 主主页）批量解析成可用知识素材：音频通道本地 ASR 出逐字稿，画面通道抽关键帧出总览图，再由多模态模型融合成结构化笔记。当用户说「解析这批视频」「把视频扒下来整理」「批量转文字」「视频做笔记」「拆解某个 UP 主的内容」「把这堆课程视频整理成文档」时使用。
agent_created: true
---

# 视频批量解析（转写 + 画面 + 结构化笔记）

用户手上有一堆视频要**系统性地整理成内容**时，别一条条上传云端、也别只看文字。
一条视频里的信息有**两个来源**，缺一个就丢一半：

| 通道 | 拿到什么 | 怎么拿 |
|---|---|---|
| 音频 | 他**说了**什么 | 本地 faster-whisper 转写 |
| 画面 | 屏幕上**写了**什么（PPT / 图表 / 板书 / 界面文案） | 抽关键帧拼总览图，让多模态模型读 |

讲解类视频里，PPT 上的信息密度常常**比口播还高**。只听音频等于扔掉一半内容。

---

## 0. 环境（已就绪，不要重复装）

- Python：`C:\Users\songyiheng\.workbuddy\binaries\python\envs\asr\Scripts\python.exe`
  已装 `faster-whisper 1.2.1` + `ctranslate2 4.8.2` + `av` + `numpy` + `Pillow`
  + `nvidia-cublas-cu12` + `nvidia-cudnn-cu12`（**GPU 必需，别卸**）。
- ffmpeg 9.0.2 已在 PATH（脚本内置多处兜底路径）。
- 模型缓存 `C:\Users\songyiheng\.cache\fw`，已下 `small`；`large-v3` 首次会下约 1.5G。
- 国内下模型**必须** `HF_ENDPOINT=https://hf-mirror.com`（脚本已自动设默认值）。
- GPU：RTX 4070 12GB。
- **CUDA 运行库的坑**：`ctranslate2` 在 Windows 上用 GPU 需要 `cublas64_12.dll` /
  `cudnn64_9.dll`，它们装在 `site-packages/nvidia/*/bin`，**不在默认 DLL 搜索路径**。
  脚本里 `common.setup_cuda_dlls()` 用 `os.add_dll_directory()` 注册，跑之前必须调用。
  报错 `Library cublas64_12.dll is not found` = 这一步没做。

---

## 1. 三段链路

```
素材 → ① 转写(音频) ─┐
     → ② 抽帧(画面) ─┴→ ③ 多模态模型融合 → 结构化笔记
```

### ① 转写（本地，GPU）

```bash
"<asr venv>/python.exe" "<skill>/scripts/batch.py" "<输入>" --out "<输出根目录>"
```

产出：`transcript.md`（段落化带 `[mm:ss]`）、`transcript.srt`、`segments.json`、`meta.json`

### ② 抽帧（本地，纯图像算法，不调模型）

产出：`overview_01.jpg …`（**先读这些**）、`frames/kf_*.jpg`（字看不清时补读）、`keyframes.json`

### ③ 融合（这一步由你——多模态模型——来做，不写代码）

**读数顺序（不能跳）**：

1. 先读 `transcript.md` —— 拿到完整口播脉络与时间戳；
2. 再**逐张读完** `keyframes.json` 里 `overviews` 列出的**每一张**总览图。
   只读第一张 = 只看了视频开头。关键帧多时会分多张，顺序 = 时间顺序；
3. 总览图里小字读不清时，按 `keyframes.json` 的帧号去 `Read` 对应的
   `frames/kf_*.jpg` 单帧原图补细节 —— **不要重跑脚本、不要把总览图放大**；
4. 最后写 `notes.md`。

**`notes.md` 的固定结构**：

```
# 《标题》内容解析

> 素材来源 / UP 主 / 时长 / 解析时间；声明逐字稿为 ASR 结果

## 一句话主旨
## 内容脉络（按时间轴）
| 时间 | 板块 | 讲了什么 | 画面依据 |
（每段标时间戳，并写清是"听来的"还是"从画面上读到的"）
## 核心观点与论证
## 金句原文
## 可复用的模型 / 框架 / 清单
## 来源与真伪提醒（搬运号？原出处？）
```

**注意**：`notes.md` 必须写进 `<输出>/<视频名>/notes.md`，和逐字稿、总览图放一起。
对话窗口里**只做简短确认**，不要长篇输出（用户偏好）。

---

## 2. 命令速查

输出根目录默认 `C:\Users\songyiheng\Downloads\视频解析`。

```bash
ASR="C:\Users\songyiheng\.workbuddy\binaries\python\envs\asr\Scripts\python.exe"
SC="C:\Users\songyiheng\.workbuddy\skills\video-batch-insight\scripts"

# 本地文件夹（递归，视频+音频都收）
"$ASR" "$SC/batch.py" "D:/我的视频" --out "C:/Users/songyiheng/Downloads/视频解析"

# 本地文件 + B站链接混着来
"$ASR" "$SC/batch.py" "D:/a.mp4" "https://www.bilibili.com/video/BV1xx411c7mD" --out ...

# 某个 UP 主最近 10 条
"$ASR" "$SC/batch.py" --bili-up 123456 --up-limit 10 --out ...

# 只要逐字稿，不要画面
"$ASR" "$SC/batch.py" "D:/videos" --out ... --skip-frames

# 只要画面通道
"$ASR" "$SC/batch.py" "D:/videos" --out ... --skip-asr

# 先看看会处理哪些文件（不实际跑）
"$ASR" "$SC/batch.py" "D:/videos" --list
```

单独跑某一路：

```bash
"$ASR" "$SC/asr.py"    "D:/videos" --out ... [--model large-v3] [--device auto]
"$ASR" "$SC/frames.py" "D:/videos" --out ... [--cols 3 --sheet-max-cells 12 --sheet-width 1600]
"$ASR" "$SC/bili.py"   resolve "https://b23.tv/xxxx"
"$ASR" "$SC/bili.py"   list <mid> 30
"$ASR" "$SC/bili.py"   dl BVxxxx --out "D:/素材"
"$ASR" "$SC/bili.py"   up <mid> --out "D:/素材" --limit 20 --max-duration 3600
```

---

## 3. 参数档位（给模型读的总览图，别乱调）

**这是全项目最容易搞错的地方。** 多模态模型的视觉编码器会把整张图**压到长边约
1080~1568px** 再送进来。所以：

> 每格能分到多少像素，只取决于**这张图里有多少格**，跟图做多大基本无关。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--cols` | 3 | 列数，**固定 3~4**，要更多帧就加行数 |
| `--sheet-max-cells` | 12 | 每张最多几格，**最多 16** |
| `--sheet-width` | 1600 | 图宽，**1600~2400**，别超过 2400 |
| `--sheet-max-sheets` | 4 | 最多几张 |

三条硬规则：**每张 ≤12 格、列数 ≤4、图宽 ≤2400**。
违反的后果：每格只剩一两百像素 → 字幕和注释全糊；图宽超 ~8000px → 读图工具直接拒收。

### 遇到抽帧抽太多 / 太少

| 现象 | 怎么办 |
|---|---|
| 选出帧数触顶（跑到 `--max-frames`） | 内容属于"连续微动"（真人出镜、口播）。**调高 `--noise-k`（4→6~8）或手动 `--threshold 0.02~0.04`** |
| 抽出来的帧是空白画面 | 别手动调高 threshold！改回自动（`--threshold 0`），要减量用 `--max-frames` 抽稀 |
| 静止 PPT 视频抽太密 | `--noise-k 3`；或 `--min-time 1.0` |

**原理**：阈值 = 画面差异中位数（≈底噪）× `--noise-k`，夹在 `[0.008, 0.08]`。
调高阈值有坑 —— 元素淡入淡出不再被当成"变化"，算法会在动画中途误判成"变化已结束"，
抽到元素还没出现的空白中间态。**要减少数量优先用抽稀，不要调高阈值。**

---

## 4. B 站相关的坑（都踩过了，别重踩）

- **投稿列表**只能走 **App 端 appkey 签名接口**（`app.bilibili.com/x/v2/space/archive`），
  无需 cookie。Web 端 `/x/space/*` 有强风控（-412 / -799），wbi 签名合成指纹过不了（-352）。
- **视频流下载必须带 `Referer` + 浏览器 UA**，否则 403。
- **未登录清晰度受限**（通常 480P/720P）。PPT 上的小字在 480P 下会糊 ——
  要 1080P 让用户提供 `SESSDATA`，传 `--sessdata`，或设环境变量 `BILI_SESSDATA`。
- **优先用现成 CC 字幕**：如果视频本来就有字幕（UP 主上传或 B 站 AI 字幕），
  `download_bvid()` 会顺手存成 `<同名>.srt`。**这种比跑 ASR 准得多，直接用，别转写。**
- 批量请求 `web-interface/view` 约 10 条后会限流，要 `sleep 1.5` 以上（脚本里已有）。

---

## 5. 已知边界（说清楚，别假装能做到）

- **不做说话人分离**。多人访谈要区分「谁说的」，得另配 `WhisperX` 或
  `MOSS-Transcribe-Diarize`（复旦 OpenMOSS，0.9B，一趟出转写+说话人+时间戳）。
- **不做内嵌硬字幕 OCR**。如果视频画面上烧录了字幕，当前只靠 ASR；要更准可以加 OCR 通道交叉验证。
- **不改原素材**：不转码、不剪裁、不覆盖用户文件。抽帧只是读。
- **断点续跑**：已完成的文件默认跳过，`--force` 才重跑。
- 逐字稿是 **ASR 结果不是官方字幕**，人名/机构/术语一定有同音错。
  用 `--fix 错词=对词`（可多次）做定向修正，并在 `notes.md` 里诚实声明改过哪些。

---

## 6. 常用同音词修正（真实案例，按需扩充）

`--fix 六阳桌=六郎庄 --fix 车祸咖啡=车库咖啡 --fix 临河博弈=零和博弈 --fix 软式子=软柿子`

中文人名/专有名词 whisper 几乎必错。校正前**先联网核对**：搜标题/UP 主/关键人名，
往往能找到原出处与二手解读，可交叉验证并判断这条视频是不是搬运。

真实可复用样本：协同→「工具写作」、memory.md→「Emery的MD」、从 0 到 1→「从临到一」、
风控→「封供」、认知边界→「人子的边界」。

---

## 7. 故障排除（真实踩过的坑）

### 7.1 `File model.bin is incomplete` / `json.exception.parse_error.101`
**别急着重下 3GB。** 这是**两个进程同时下载同一个模型**导致的：
blobs 里的文件是完整的，但 `snapshots/<commit>/` 里除 model.bin 外**全是 0 字节**
（Windows 上 HF 的符号链接创建失败）。

修法 —— 从 `blobs/` 按**文件大小**反查文件名，用硬链接补回去：

```python
import os
root = os.path.expanduser("~/.cache/fw/models--Systran--faster-whisper-large-v3")
size2name = {3087284237:"model.bin", 2480617:"tokenizer.json", 1068114:"vocabulary.json",
             2394:"config.json", 340:"preprocessor_config.json",
             1519:".gitattributes", 2052:"README.md"}
blobs, snapdir = os.path.join(root,"blobs"), os.path.join(root,"snapshots")
for d in os.listdir(snapdir):
    for b in os.listdir(blobs):
        if b.endswith(".incomplete"): continue
        sz = os.path.getsize(os.path.join(blobs,b))
        if sz not in size2name: continue
        p = os.path.join(snapdir,d,size2name[sz])
        if os.path.exists(p): os.remove(p)
        os.link(os.path.join(blobs,b), p)
```
**铁律：同一个模型绝不允许两个进程并发下载。** 预下模型的任务没结束前，不要起解析任务。

### 7.2 转写慢得不正常（GPU 100% 但只有 ~1x 实时）
先看 `nvidia-smi`：如果**显存已接近满载**（如 11.7G/12G），说明是**显存竞争**，
不是代码问题。动态壁纸（Wallpaper Engine）、游戏、浏览器硬件加速都会抢。
让用户关掉这些程序，吞吐通常能翻好几倍。追求速度也可换 `--model large-v3-turbo`。

### 7.3 想给用户一个"双击就能跑"的入口（Windows）
`.bat` 中文乱码的三种结局（**必须用第 3 种**）：
1. UTF-8 **无 BOM** → cmd 按 ANSI(936) 解析 → 中文全乱，`-^>` 这类转义还会被破坏导致命令截断。
2. UTF-8 **带 BOM** → 中文正常，但**第一行 `@echo off` 失效**（BOM 被算进命令名，报"不是内部或外部命令"）。
3. ✅ **文件存 GBK(936) + 保留 `chcp 65001`**：cmd 用 936 解析文件、用 65001 输出到控制台，两边都对。

另外在 bat 里必须设 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`，否则**文件名含 emoji 时
Python 的 print 会抛 UnicodeEncodeError 直接中断整批**。

转换：`[System.IO.File]::WriteAllText($p,$c,[System.Text.Encoding]::GetEncoding(936))`

⚠️ 在 Git Bash 里用 `cmd //c xx.bat` 测试会看到乱码 —— 那是**管道抓取**的显示问题
（重定向时 cmd 用 OEM 码页而非 chcp 码页），**不代表真实双击窗口异常**，别被误导去反复改编码。

### 7.4 「窗口一关，任务就死」——GUI 起的任务必须脱离父进程

**症状**：图形界面里点开始 → 解析跑起来 → 用户关掉窗口（或界面崩了）→
解析进程**跟着一起消失**，跑了几小时全白费。日志文件 0 字节。

**根因**：在 Windows 上，`subprocess.Popen(..., creationflags=DETACHED_PROCESS)`
**不足以**让子进程独立。子进程仍会被并入父进程的 **Job Object**，
父进程一退，Job 关闭，子进程被连带回收。实测：日志 0 字节、PID 消失。

**修法**（已验证）：加一层中转启动器 `scripts/run_detached.py`

```
界面  →  run_detached.py  →  batch.py（真正的解析）
        （只负责转身，立即退出）
```

启动器里 flags 要加 `CREATE_BREAKAWAY_FROM_JOB`：

```python
DETACHED_PROCESS         = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB= 0x01000000

subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                 stdin=subprocess.DEVNULL,
                 creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                               | CREATE_BREAKAWAY_FROM_JOB)
```

若某些环境不允许 BREAKAWAY（抛 OSError），退回 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`。

**界面侧**改为「tail 日志文件」而不是持有子进程管道：

- 子进程 stdout/stderr 直接写 `_运行日志.txt`
- 界面循环读该文件的新增内容显示出来
- **不能靠 `proc.poll()` 判断结束**（子进程已经跟界面脱钩，界面拿不到真实退出码）
  → 让看守进程在日志末尾写 `#EXIT#<code>` 和 `#END#`，界面见到 `#END#` 即判完成
- 无输出超过 ~30 秒判为异常中止

**验证方法**（务必实测，别想当然）：父进程启动解析后立刻 `os._exit(0)`，
然后看日志是否继续增长、产出是否照常落地。**光看日志首次出现是不够的**——
要确认父进程死后它仍在写。

### 7.5 GUI 进度反馈：用带内协议，别去解析人话日志

界面要显示「第 3/10 个 · 预计还需 12 分」，不要在 `print` 的人话里做正则。
在 `common.py` 里加一个结构化进度通道：

```python
PROGRESS_PREFIX = "@@P|"
def progress(phase, i=0, n=0, name="", extra=""):
    # phase: begin / item / stage / end
    print("%s%s|%d|%d|%s|%s" % (PROGRESS_PREFIX, phase, i, n, name, extra), flush=True)
```

界面用 `re.compile(r"@@P\|[^\n\r]*")` 把这些行**摘出来**（显示前必须剥掉，
否则日志区会混进机器码），其余内容照旧进日志区。

**耗时估算**：在 `item` 事件里用 `已用 / (i-1) * 剩余条数` 推 ETA，
比"按文件个数乘常数"准得多（视频长短差异太大）。

### 7.6 目录扫描：`.ts` 既是视频也是 TypeScript

**症状**：用户选了个文件夹，工具报 200+ 个视频，实际一个视频没有 ——
全是 `node_modules` 里的 `.ts` 源码，白跑几小时。

**修法**（`common.py`）：
1. `.ts` **默认不收**，只有显式 `--allow-ts` 才当视频
2. 加 `SKIP_DIRS` 黑名单，枚举时跳过：
   `node_modules`、`.git`、`__pycache__`、`venv`、`site-packages`、
   `dist`、`build`、`.next`、`$RECYCLE.BIN` 等
3. 跳过以 `.` 开头的隐藏目录
4. 枚举改用 `os.scandir` 手写栈（别用 `rglob("*")`，它没法剪枝，
   在 node_modules 上会慢到离谱）

⚠️ `SKIP_DIRS` 里**不要**放 `skills` 这种通用词 —— 用户自己的视频目录可能就叫这名。
只放几乎不可能装视频的目录名。

**给用户加一层保护**：图形界面在「开始解析」前先跑一次 `--list-only` 清点，
弹窗告知「共 N 个文件，估计要跑 X 分钟」，确认后才真正开跑。
这是拦下"选错文件夹"的唯一有效时机。

### 7.7 「音频转文字」是视频解析里的一个勾选项（2026-11-12）

**别做成第二个功能。** 用户要的是：解析视频时，产出的汇总分析里附带文字稿。
曾经做错过一版 —— 把界面拆成「视频解析 / 音频转文字」双页签、另开一套音频模式，
用户直接否掉："你做成两个功能干嘛"。正确形态是**单一流程 + 一个勾选**。

GUI 的 ① 区有一行「解析内容」：

- ☑ 音频转文字（逐字稿 / 字幕）
- ☑ 关键帧总览图（画面）

`_run_args()` 按勾选拼参数：勾音频 → `--attach-transcript`；不勾 → `--skip-asr`；
不勾画面 → `--skip-frames`；两项都不勾直接弹窗拦下。`_start()` 里记下
`_ran_asr` / `_ran_frames`，`_finish()` 只按本次勾选的那几路判定有没有产出，
否则「只跑画面」时会被「逐字稿 0 份」误报成失败。

`batch.py` 新增 `--attach-transcript`：`write_index()` 末尾追加「## 文字稿全文」章节，
`plain_body()` 优先从 `segments.json` 并段，回退读 `transcript.md` 去掉 `**[mm:ss]**` 前缀。
所以即便 `--skip-asr` 沿用旧结果，汇总里也能带上文字稿。

命令行等价：

```bash
"$ASR" "$SC/batch.py" "D:/视频.mp4" --out "D:/解析结果" --attach-transcript
```

另外两条健壮性改动：`has_video=False` 时自动跳过抽帧（文件夹混进 mp3 不会刷报错）；
`main()` 补默认 `HF_ENDPOINT=https://hf-mirror.com`（命令行直跑时 faster-whisper
检查模型版本撞 huggingface.co 会 502，GUI 走 run_detached 本来就会设）。

界面主区域放进可滚动画布：窗口偏矮时 pack 会把最后一块「运行记录」压成 1px。
改布局后务必截图实测。

### 7.8 抽帧是串行的话，改成并发

原实现逐帧起一个 ffmpeg 子进程、顺序等待，200 帧就是 200 次串行往返。
改成 `ThreadPoolExecutor`（I/O 型任务，线程足够）：

```python
workers = max(4, min(8, (os.cpu_count() or 4) // 2))
```

并发度别超过 8~16（磁盘寻道 + 解码器都会打架），且要 `<= len(times)`。
按时间排序结果再返回，保证帧序稳定。
