# VoidDraft × Pulsify — 设计规则

> 记录时间：2026-05-18 / 最后更新：2026-05-19  
> 来源：fancam_harvester pipeline 开发过程中确立的架构原则

---

## 一、仓库职责划分

| 仓库 | 定位 | 放什么 |
|------|------|--------|
| **ZenithLoom** | 纯引擎 | LangGraph 框架、DeterministicNode、LlmNode、SubgraphRefNode、graph_builder |
| **VoidDraft** | 业务蓝图 | entity.json、state.py、validators.py、pipeline 特有逻辑 |
| **Pulsify** | 通用视频处理库 | 下载、剪辑、分析、合成——与平台/业务无关的通用功能 |
| **EdenGateway** | 运行时实例 | entity.json 实例配置、数据库、session |

**判断标准：** 如果这个功能换一个业务场景还能复用 → 放 Pulsify。  
如果它绑定了 Reddit/Pixeldrain/K-pop 等具体业务 → 放 VoidDraft。

---

## 二、validators.py 职责

- validators.py 是**薄层**：只做状态读写、流程编排、错误收集
- 实际逻辑委托给 Pulsify（`from pulsify.xxx import yyy`）
- 不在 validators.py 里写通用工具函数（video_info、下载等）

---

## 三、Reddit 数据抓取

- 使用 Reddit 公开 JSON API，**无需浏览器、无需 Scrapling**
  ```
  GET https://www.reddit.com/r/{sub}/new.json?limit=25&after={token}
  GET https://www.reddit.com/r/{sub}/comments/{post_id}.json
  ```
- User-Agent 必须用 Reddit 格式：  
  `python:fancam_harvester:v1.0 (by /u/fancam_bot)`  
  浏览器 UA 会被 403
- NSFW subreddit（如 r/kpopfap）需要 cookie：`over18=1`
- Comments 递归抓取：Pixeldrain 链接通常在评论里，不在 post body

---

## 四、Source 视频下载规则

1. **多 source 全下载**：一个帖子有多个 source URL，全部尝试
2. **下载前预检时长**：via `yt-dlp --dump-json`，超过 5 分钟（300s）→ skip，不下载
3. **最高画质**：format = `bestvideo[vcodec^=avc1]+bestaudio/bestvideo+bestaudio/best`，不限制分辨率
4. **H.264 优先**：OpenCV/ffmpeg 兼容性更好，避免后续重编码
5. 实现：`pulsify.fetcher.url_downloader.download_urls()`

---

## 五、Pixeldrain 下载规则

- 支持 album（`/l/{id}`）和单文件（`/u/{id}`）
- 用 Pixeldrain API 查 album 文件列表，逐个下载
- 实现：`content_retriever/downloaders/pixeldrain.py`（VoidDraft，因为 Pixeldrain 是业务特定平台）

**Hotlink 保护 / Captcha 限制**：

| 错误值 | 含义 | 解决方案 |
|--------|------|----------|
| `file_rate_limited_captcha_required` | 文件被访问次数超阈值，或非浏览器下载 | 需要 premium API key，或手动浏览器下载 |
| 403 无 body | IP 临时封禁 | 等待或换 IP |

- **Free API key（免费注册账号）**：可以获得，但**不能**绕过 hotlink captcha。captcha 绕过只有 uploader 或 downloader 持有 premium 账号时才生效。
- **推荐方案**：配置 `PIXELDRAIN_API_KEY` 环境变量（premium 账号），下载时用 Basic Auth（username="", password=api_key）
- **Scrapling / Playwright 无法绕过**：hCaptcha 需要用户交互，headless 浏览器无法自动解决
- pipeline 遇到此错误时：记录 `PixeldrainCaptchaError`，标记该文件为 `download_failed`，继续处理其他文件

---

## 六、视频元数据提取

- 优先 `ffprobe`（JSON 输出，精确）
- fallback `ffmpeg -hide_banner -i`（解析 stderr）
- 注意：`-v quiet` 会把 stream info 也屏蔽，必须用 `-hide_banner`
- 实现：`pulsify.utils.video_info.get_video_info()`

---

## 七、Pulsify 作为 Library

- 已通过 `pip install -e . --break-system-packages` 安装
- 包结构：`src/pulsify/`（标准 src layout）
- 所有内部 import 使用 `pulsify.*` 命名空间，避免冲突
- **不要**在 VoidDraft 里维护 sys.path hack，直接 `from pulsify.xxx import yyy`

---

## 八、文件名中的元数据

Pixeldrain 上的片段文件名常含 YouTube video ID：
```
asa@Hfv9X8yZkoY]-1.mp4   →  YouTube ID: Hfv9X8yZkoY
[asa@t60fZbeGSgA]-2.mp4  →  YouTube ID: t60fZbeGSgA
```
格式：`【youtube@VIDEO_ID】` 或 `[name@VIDEO_ID]`  
可通过 YouTube Data API 或 yt-dlp `--dump-json` 反查标题、上传日期。

---

## 九、片段与源视频对齐（Align 层）

**关键发现**：Pixeldrain 上的 clip 通常是**手机拍摄的竖屏 fancam**（portrait），
而 source 是 YouTube 横屏视频（landscape）。两者是同一场演出的不同机位，
**clip 并非从 source 中截取**，因此视觉/音频对齐本质上不适用于这种场景。

**实际对齐场景**（何时有意义）：
- clip 是有音频的横屏视频，且和 source 同机位/同源
- 文件名含 YouTube ID 仅说明这场演出的 source 视频，不代表 clip 从 source 截取

**对齐策略（auto 模式，依次尝试）**：
1. 音频指纹（librosa chroma features，滑动窗口互相关）— 有音轨时优先
2. Motion 对齐（Pulsify YOLOAnalyzer，COCO 17 关键点 → torso-normalized 姿态向量，余弦相似度滑窗）— 仅限舞蹈类 clip
3. 视觉帧匹配（OpenCV SSIM）— 最后兜底
- 置信度低于阈值 → 标记 `unmatched`，clip 作为独立素材保存（不丢弃）
- **大多数手机 fancam clip 会以 `unmatched` 状态进入存储层**，这是正常的

**Dance 分类（`is_dance_clip(title, filename)`）**：
- 含 직캠/fancam/stage/무대/showcase/音乐节目名 等关键词 → 舞蹈类，尝试 motion 对齐
- 含 vlog/interview/behind/mukbang 等关键词 → 非舞蹈，跳过 motion 对齐
- 无明确关键词 → 默认视为舞蹈（r/kpopfap 场景下 90%+ 是舞台）
- 实现：`fancam_harvester/align/__init__.py: is_dance_clip()`

**Motion + Crop 两阶段对齐（`pulsify.align.motion_align.find_offset`）**：

*Stage 1 — 动作粗筛*
- 2fps 采样，YOLO 提取 COCO 17 关键点
- 姿态归一化：mid-hip 为原点，肩髋距为尺度，仅 body 关键点（5–16），置信度加权
- 滑动窗口余弦相似度 → 筛出 top-K 候选偏移（默认 K=5）

*Stage 2 — Person Crop 外观验证*
- 对 top-K 候选，在对应时间戳提取帧（默认每候选 5 帧）
- 用 YOLO bounding box 裁剪出 person crop（portrait clip 无 bbox 时取全帧）
- 统一 resize 到 128×256，计算 HSV 颜色直方图交集（对同一场演出同一套装有效）
- combined = motion_sim × crop_sim

- 置信度 combined < 0.40 → AlignmentError → 标记 `unmatched`
- YOLO 默认 `yolo11n-pose.pt`（6MB nano，速度优先）
- 结构：`MotionAlignResult(offset_sec, confidence, motion_score, crop_score)`

**对齐加速 — 480p 下采样**：
- `find_offset(resize_height=480)`：所有 YOLO 推理和 crop 比较都在 480p 上进行
- 找到最佳 offset 后，如需精确验证可在原分辨率抽帧（当前未实现，留作 TODO）
- 实现：`_resize_frame(frame, height)`，保持宽高比，downscale 用 INTER_AREA

**Clip > Source 处理**：
- audio_fingerprint.py 和 motion_align.py 均加了 swap 逻辑
- 当 clip 比 source 长时，内部交换搜索方向，返回时对 offset 取反

---

## 十、Pixeldrain Album 中 Source 文件识别

当 Reddit 帖子的 source 来自 **RedNote / TikTok**（无法可靠爬取），poster 通常会把原始视频也上传到同一个 Pixeldrain album。

**识别规则（5 条，全部满足即为 source）**：

| # | 规则 | 说明 |
|---|------|------|
| 1 | 文件大小最大 | 原视频码率高，即使时长短也比 merged 文件大（如 4K source < 1080p merged） |
| 2 | 文件名无 clip 标记 | 不含 `Clip N`、`pt1`、`_1`、`-2` 等后缀 |
| 3 | 时长 > 20s | 区分短 highlight clip（probed 后检查，未 probed 时跳过此规则） |
| 4 | 有音轨 | merged/highlight 有时会剥离音频，source 保留原音（probed 后检查） |
| 5 | post source 平台是 RedNote/TikTok | 辅助判断（不满足时仍可继续，只降低置信度） |

**质量比较回退逻辑**：

```
1. 尝试从 source URL（YouTube/RedNote/TikTok）下载原视频
2. 同时识别 Pixeldrain album 中的 source 候选
3. 若原视频下载失败 → 使用 Pixeldrain source 候选
4. 若原视频下载成功但质量 < Pixeldrain 候选 → 使用 Pixeldrain 候选
5. 质量比较 = width × height × fps，5% 以内视为相同
```

**实现**：
- `fancam_harvester/clip_analyzer.py`
  - `PixeldrainFile(file_id, name, size_bytes, duration_sec, has_audio, width, height, fps)`
  - `identify_source_in_album(files, source_platform, min_duration_sec=20.0) → SourceCandidateResult`
  - `fill_probe_info(pf, path)` — 下载后 probe 并回写
  - `better_quality(path_a, path_b) → "a" | "b" | "equal"`
  - `probe_video(path)` — 三级 fallback：ffprobe → ffmpeg stderr 解析 → cv2

**真实案例（Tzuyu 帖子）**：
- Pixeldrain album 8 个文件：Merged(107MB)、Clip 1-6(9-23MB)、RedNote(150MB)
- `identify_source_in_album` 正确识别 `260517 Tzuyu [RedNote-Laetitia].mp4`（150MB, 4K@60fps, 23.4s）
- 即使时长仅 23.4s（< 30s threshold），凭大小和无 clip 标记仍成功识别

---

## 十一、Merged Clip 检测与分段

**Merged clip 文件名解析（`fancam_harvester/clip_analyzer.py`）**：

| 模式 | 含义 | 示例 |
|------|------|------|
| `clip(N,M,...)` | 明确 merged，含分量索引 | `clip(3,1,2).mp4` |
| `_full` / `_complete` / `Merged` / `Combined` | 明确 merged，无索引 | `fancam_full.mp4` |
| `Clip N` / `pt1` / `part2` / `_1` / `-2` | 独立片段 | `Clip 3.mp4` |
| 其他 | 独立片段（默认） | |

- `parse_clip_filename(fn) → MergedClipInfo`
- `find_merge_groups(filenames)` → 将 individual + merged 按 index 或 base stem 匹配分组
- `find_merge_by_duration(duration_map, tolerance=2.0)` → 通过时长之和匹配（适用于时间戳命名的文件）

**场景切点检测（`pulsify.align.scene_cut`）**：

首选 **PySceneDetect `ContentDetector`**（via `pulsify.tools.ShotDetector`）：
- 明显优于自研 optical flow 方案（Tzuyu 测试：7/7 vs 5/7 切点）
- 默认 threshold=25.0，`min_gap_sec=1.0`
- 接口：`detect_cuts(video, threshold=25.0, min_gap_sec=1.0) → list[float]`

Fallback 自研 optical flow（当 ShotDetector 不可用时）：
- 核心分数：`hist_chi_squared(A,B) × (1 + mean_flow_mag)`
- 自适应阈值：`mean + 5.0 × std`

**切点验证（`verify_cuts` + `flag_duplicate_matches`）**：
- `verify_cuts(merged, clip_paths, cut_timestamps)` — 对每个分段起始帧与各 clip 起始帧做 HSV 直方图匹配
- `flag_duplicate_matches` — 同一 clip 匹配多个分段时，保留最高分，其余标为假切
- 需要有 individual clip 文件才能验证

**已知局限 — 软切（dissolve）**：
- ContentDetector 基于帧间色彩差值，对硬切有效
- 若两相邻 clip 场景相似（相同布景/服装），切点分数可能低于阈值（Tzuyu Clip5/6 案例：分数 ~15，低于 threshold=25）
- 应对策略：使用 `AdaptiveDetector`（无效）或降低 threshold（增加误报风险），最终建议：有 individual clips 时用 `verify_cuts` 二次确认

**慢动作检测（`pulsify.align.slowmo_detect`）**：
- `detect_slowmo(video_path) → SlowmoInfo(is_slowmo, speed_factor, detected_by)`
- 三种检测方法（优先级顺序）：
  1. FPS metadata：r_fps / avg_fps 比值（如 60fps 视频 avg_fps=30 → 2x 慢动作）
  2. 音频 BPM（librosa）：BPM < 75 → 2x；BPM < 50 → 4x
  3. Optical flow 幅度：低于正常舞蹈场景阈值
- `try_alignment_with_speed_factors(align_fn, src, clip, speed_factors=[1.0, 2.0, 4.0])` → align 失败时依次提速重试
- 实现：`pulsify/align/slowmo_detect.py`

---

---

*— Hani · 无垠智穹*
