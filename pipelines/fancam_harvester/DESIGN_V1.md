# Fancam Harvester — V1 Design

> 最后更新：2026-05-20
> 状态：设计完成，待实现

---

## 术语约定

| 术语 | 含义 |
|---|---|
| **source clip** | 原始高清视频（来自 TikTok / YouTube / Pixeldrain 原视频），未经创意加工 |
| **pixeldrain clip** | Pixeldrain 相册里的 clip，可能经过创意加工（慢放、zoom-in）或仅为时间截取 |
| **merged clip** | Pixeldrain 相册里包含多段的合集文件（如 `clip(1,2,3).mp4`、`full.mp4`） |
| **individual clip** | Pixeldrain 相册里的单独片段（如 `1.mp4`、`clip2.mp4`） |
| **creative edit** | 对 source clip 做了创意加工：慢放（slowmo）或空间缩放（zoom-in）|
| **final clip** | 经筛选后保留的高质量 clip，作为最终输出 |

---

## 一、总体目标

1. **自动采集**：每日从 r/kpopfap 抓取帖子，提取 source clip + pixeldrain clip，筛选出最高质量的 final clip。
2. **元数据识别**：识别每个 clip 所属女团、成员、舞曲、表演时间，并追踪 Reddit 点赞数。
3. **持久化存储**：所有 clip 元数据、点赞时序、处理状态写入 SQLite。

---

## 二、运行环境

| 项目 | 配置 |
|---|---|
| Workspace | `~/Foundation/EdenGateway/RedditPulsify/` |
| SQLite 路径 | `~/Foundation/EdenGateway/RedditPulsify/fancam.db` |
| Cron 频率 | 每天一次（建议 UTC 06:00）|
| 抓取范围 | r/kpopfap 前 10 页（约 250 帖/次）|

### Workspace 目录结构

```
~/Foundation/EdenGateway/RedditPulsify/
├── fancam.db                # SQLite 数据库
├── sources/                 # source clip 下载（TikTok/YouTube）
│   └── {post_id}/
├── clips/                   # Pixeldrain 下载（所有文件）
│   └── {post_id}/
├── hd_clips/                # 从 source clip 截取的高清版本
├── final/                   # 最终保留的 final clips
│   └── {post_id}/
│       ├── {clip_id}.mp4               # 无创意加工，较高清版本
│       ├── {clip_id}_slowmo.mp4        # 慢放版（creative edit）
│       ├── {clip_id}_original.mp4      # 对应慢放的原速版本
│       ├── {clip_id}_zoom.mp4          # zoom-in 版（creative edit）
│       └── {clip_id}_fullframe.mp4     # 对应 zoom 的全帧版本
└── logs/
    └── run_{YYYYMMDD}.log
```

---

## 三、Pipeline 完整流程

### 3.1 Cron 入口：帖子分类

```
每日 Cron
    ↓
抓取 r/kpopfap 前10页 → 得到帖子列表 {post_id, created_utc, score, urls}
    ↓
对每个 post 查 SQLite posts 表:
    ├── NEW  → 走完整 Pipeline（3.2 ~ 3.8）
    └── SEEN → 走 Update 流程（3.9）
```

### 3.2 Download

**Source clip 下载优先级**（从高到低）：
1. Pixeldrain 相册内识别出的原视频（`identify_source_in_album`：排除 merged/individual marker，取最高像素数）
2. Pixeldrain 文件名中嵌入的 `[youtube@ID]`，包含两类来源：
   - individual clip 文件名：`qwer hina [youtube@6cbP4AOf36c]-2.mp4`
   - merged clip 文件名：`[youtube@v9TEtu3CAI4]-(24,18,...).mp4`（括号索引说明是 merged，但 YouTube ID 仍有效）
   - 提取方式：`_YT_IN_FILENAME_RE` 扫描所有相册文件名，去重，限制 ≤ 300s 下载
3. 帖子正文/评论中的 YouTube / TikTok / Bilibili 链接（修复 Markdown `[label](URL)` 解析）

**优先级仲裁**：
- 已找到 Pixeldrain 原视频（优先级1）→ 跳过优先级2、3 的下载
- 优先级1 未找到，优先级2 有可下载的 YouTube ID → 下载，跳过优先级3
- 优先级1、2 均失败 → 使用优先级3

**分辨率仲裁**：
- Pixeldrain source clip 像素数 ≥ 外部下载 source clip → 降级外部为 `external_ref`，不参与对齐
- 否则外部 source 优先

**Pixeldrain 下载**：
- 下载相册所有文件，分类标注 `clip_type`：`source` / `merged` / `individual`
- 每个文件的 `pixeldrain_filename` 记录到 DB，用于后续 album diff

### 3.3 Clip 选取：merged vs individual

```
Pixeldrain 相册中:
    有 individual clips?
        YES → 忽略 merged clip，直接处理 individual clips
        NO  → 对 merged clip 做场景切割（detect_cuts），拆分为 segments
              若未检测到任何 cut → 整段作为一个 clip 处理
              拆出的 segments 视为 individual clips 处理
```

### 3.4 Slowmo 检测

对每个 pixeldrain clip 运行 `pulsify.align.slowmo_detect.detect_slowmo()`：

```python
SlowmoInfo.is_slowmo: bool
SlowmoInfo.speed_factor: float   # e.g. 0.5 = 半速慢放
```

- `is_slowmo=True` → 标记 `is_slowmo=True, speed_factor`，**不跳过对齐**，用 `try_alignment_with_speed_factors` 做速度补偿对齐

### 3.5 Align（三层策略）

对每个 pixeldrain clip，对齐到 source clip：

```
1. Audio cross-correlation  → offset_sec, conf
   conf ≥ 0.10              → method="audio"

2. DINOv2 diagonal match    → offset_sec, conf
   conf ≥ 0.50              → method="dinov2"

3. YOLO pose motion         → offset_sec, conf
   conf ≥ 0.55              → method="pose"

全部失败                    → method="unmatched"
```

慢放 clip：`try_alignment_with_speed_factors(speed_factor)` 插入第一层前。

### 3.6 Zoom 检测

对已对齐的 pixeldrain clip，在对齐帧处与 source clip 做人脸比例对比：

```python
zoom_detect(
    clip_path,
    source_path,
    align_offset_sec,
    zoom_threshold=1.5
) → ZoomDetectResult(is_zoom_in, zoom_factor, method)
```

- **method="face"**：`face_area/frame_area` 比值，clip/source > 1.5 → zoom-in
- **method="aspect_ratio"**（fallback，cv2 不可用 or 检测不到脸）：宽高比差 > 0.15 → zoom-in

### 3.7 Creative Edit 判断 + Final Clip 选择

```
is_slowmo OR is_zoom_in → 有创意加工
    ↓
    保留 pixeldrain clip（creative edit 版）
    + 从 source clip 截取对应片段（原速/全帧版）
    ↓ 两个文件都写入 final/

无创意加工（pixeldrain clip = 纯时间截取，同角度）
    ↓
    比较质量：像素数优先，像素数相同时 fps 作为 tiebreaker
        ┌─ pixeldrain clip 像素数 > source clip → final = pixeldrain clip
        ├─ source clip 像素数 > pixeldrain clip → final = source clip 截取的 HD 版本
        └─ 像素数相同，fps 不同 → 两个都保留
    ↓ 写入 final/
```

**最终文件命名**：

| 场景 | 文件名 |
|---|---|
| 无创意加工，保留较高清 | `{clip_id}.mp4` |
| 慢放版 | `{clip_id}_slowmo.mp4` |
| 慢放对应原速版 | `{clip_id}_original.mp4` |
| zoom-in 版 | `{clip_id}_zoom.mp4` |
| zoom 对应全帧版 | `{clip_id}_fullframe.mp4` |

### 3.8 元数据识别 + 初始点赞记录

**LLM 识别**（基于帖子标题 + 评论）：
```
group_name, performer, song, perf_date, confidence
```

**点赞记录**：
```
post_age = now() - created_utc
post_age < 48h → upvote_log(post_id, now(), post_age_hours, score), settled=False
post_age ≥ 48h → posts.final_score=score, posts.settled=True
```

**写入 DB**：posts 表 + clips 表 + upvote_log 表

### 3.9 SEEN Post Update 流程

```
① 更新点赞数：
   settled=False AND post_age < 48h → 追加 upvote_log 记录
   settled=False AND post_age ≥ 48h → 写入 final_score，settled=True
   settled=True → 跳过

② Album diff（Q8）：
   pd.list_album() → 得到当前文件列表
   对比 clips 表中该 post_id 已有的 pixeldrain_filename
   新增文件 → 下载 → 走 3.4~3.8 完整 clip pipeline
```

---

## 四、SQLite Schema

```sql
-- 帖子主表
CREATE TABLE IF NOT EXISTS posts (
    post_id         TEXT PRIMARY KEY,
    subreddit       TEXT NOT NULL,
    title           TEXT,
    created_utc     REAL NOT NULL,          -- Unix timestamp
    reddit_url      TEXT,
    group_name      TEXT,
    performer       TEXT,
    song            TEXT,
    perf_date       TEXT,
    llm_confidence  REAL,
    crawled_at      REAL NOT NULL,          -- 首次抓取时间
    settled         INTEGER DEFAULT 0,      -- 0=pending, 1=settled
    final_score     INTEGER                 -- 48h+ 时的点赞数
);

-- Clip 记录（每个 pixeldrain individual clip 或从 merged 拆出的 segment）
CREATE TABLE IF NOT EXISTS clips (
    clip_id             TEXT PRIMARY KEY,   -- {post_id}_{stem}
    post_id             TEXT NOT NULL REFERENCES posts(post_id),
    pixeldrain_filename TEXT,               -- 原始 Pixeldrain 文件名（用于 album diff）
    clip_type           TEXT,               -- "individual" | "merged_segment" | "source"
    local_path          TEXT,               -- 下载到的绝对路径
    width               INTEGER,
    height              INTEGER,
    fps                 REAL,
    duration_sec        REAL,

    -- 创意加工
    is_slowmo           INTEGER DEFAULT 0,
    speed_factor        REAL    DEFAULT 1.0,
    is_zoom_in          INTEGER DEFAULT 0,
    zoom_factor         REAL    DEFAULT 1.0,
    zoom_method         TEXT,               -- "face" | "aspect_ratio" | "no_source"

    -- 对齐结果
    align_method        TEXT,               -- "audio"|"dinov2"|"pose"|"unmatched"
    align_offset_sec    REAL,
    align_confidence    REAL,
    source_clip_id      TEXT,               -- 对应的 source clip 记录

    -- Final clip 输出
    final_path          TEXT,               -- final/ 目录下的主文件
    final_creative_path TEXT,               -- creative edit 版本路径（若有）
    final_kept          TEXT                -- "pixeldrain"|"source_hd"|"both"
);

-- 点赞时序
CREATE TABLE IF NOT EXISTS upvote_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         TEXT NOT NULL REFERENCES posts(post_id),
    recorded_at     REAL NOT NULL,          -- Unix timestamp
    post_age_hours  REAL NOT NULL,
    score           INTEGER NOT NULL
);

-- Cron 运行日志
CREATE TABLE IF NOT EXISTS crawl_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          REAL NOT NULL,
    posts_seen      INTEGER DEFAULT 0,
    posts_new       INTEGER DEFAULT 0,
    posts_updated   INTEGER DEFAULT 0,
    clips_new       INTEGER DEFAULT 0,
    errors          TEXT                    -- JSON array of error strings
);
```

---

## 五、已知问题 / V2 Backlog

| # | 描述 | 优先级 |
|---|---|---|
| V2-001 | LLM 识别结果与视觉内容交叉验证（YOLO 检测人物与识别结果对比） | High |
| V2-002 | 统计分析与可视化界面 | Medium |
| V2-003 | KPOP 结构化数据库对接（group/member/song 标准化） | Medium |
| V2-004 | 跨机位 spatial crop：3840×2160 横屏中追踪并 crop 出竖屏 fancam 区域（人体追踪） | Low |
| V2-005 | album diff 触发频率优化：帖子接近 48h 时加密扫描 | Low |

---

## 六、待确认事项（实现前需回答）

*（本节在实现开始前清空）*

- 无

---

## 七、实现任务列表

按依赖顺序：

1. `storage/database.py` — SQLite schema + CRUD
2. `validators.py: download()` — 加入 `pixeldrain_filename` 记录；Q8 album diff
3. `validators.py: split_merged()` — merged/individual 判断逻辑（已部分实现）
4. `validators.py: analyze_clips()` — slowmo_detect + zoom_detect 集成
5. `validators.py: align()` — slowmo 速度补偿对齐
6. `validators.py: select_best_clip()` — creative edit 判断 + final clip 选取
7. `validators.py: extract_hd()` — 慢放原速版 + zoom 全帧版截取
8. `validators.py: store_results()` — 写入 DB + 移动文件到 final/
9. `run.py` — cron 入口：new/seen 分类 + upvote update
10. Cron 注册（`CronCreate`）
