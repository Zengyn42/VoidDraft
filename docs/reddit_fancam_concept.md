# Reddit K-pop Fancam Pipeline — Concept & Design

> 设计时间：2026-05-18  
> 状态：概念设计阶段，尚未实现

---

## 一、背景与目标

Reddit 上的 K-pop fancam 内容存在以下特征：

- **Post 结构**：一篇 Reddit post 通常包含两类链接：
  - **Source 链接**（完整视频）：YouTube / TikTok / Bilibili 等平台
  - **Pixeldrain 链接**（片段剪辑）：由原帖主从 source 中剪取的高光片段

- **终极目标**：按 `idol + song + date` 组合，编辑高光混剪视频。  
  例如：`Twice TT 子瑜 20260425` → 汇聚所有相关片段，输出一个混剪。

---

## 二、Pipeline 架构设计（7步）

```
Reddit Post
    │
    ▼
[Step 1] 下载源视频 & 片段
    │
    ▼
[Step 2] 片段与原片对齐（定位时间戳）
    │
    ▼
[Step 3] 画质比对 & 升级提取
    │
    ▼
[Step 4] 动作分析 & 统计（LLM）
    │
    ▼
[Step 5] 偶像 & 演出信息识别（LLM）
    │
    ▼
[Step 6] 结构化存储
    │
    ▼
[Step 7] 混剪编译（最终产出）
```

---

## 三、各步骤详细设计

### Step 1：下载源视频与片段

**输入**：Reddit post URL  
**工具**：JDownloader（通过 `myjdapi` 操作 My.JDownloader Cloud API）

**逻辑**：
- 解析 post，找出所有链接
- YouTube / TikTok → JDownloader 下载完整 source 视频
- Pixeldrain → 直接 HTTP 下载片段（单文件 or album）
- 特殊情况：若 source 也在 Pixeldrain，则通过 **文件大小 + 时长** 判断哪个是完整版，哪些是片段

**设计决策**：
- Q：Pixeldrain 上可能有完整视频和片段混放，如何区分？  
  A：按文件大小排序——最大的通常是完整源视频；片段时长明显更短（多为10-60秒高光）

---

### Step 2：片段与原片对齐（时间戳定位）

**目标**：找出每个片段在原始完整视频中的起止时间戳

**主要方法：音频指纹匹配**
- 工具：`dejavu`、`audfprint` 或 `chromaprint/fpcalc`
- 原理：对完整视频和片段分别提取音频指纹，滑动匹配，找到最佳对齐偏移量

**回退方法：视觉帧匹配**
- 使用场景：片段无音轨（哑音 fancam）或音频被替换
- 工具：OpenCV 模板匹配 / SSIM 逐帧对比
- 原理：提取片段首帧/末帧，在完整视频中逐帧搜索相似帧

**设计决策**：
- Q：如果片段没有音频怎么办？  
  A：视觉帧匹配作为 fallback；两种方法都失败则标记为 `unmatched`，保留片段但不建立时间戳关联
- Q：音频指纹的可靠性如何？  
  A：一般 fancam 不会加水印音，但部分平台（TikTok）可能有背景音处理；音频指纹优先，失败再用视觉

---

### Step 3：画质比对与升级提取

**目标**：保留最高画质版本

**逻辑**：
1. 比对片段与原片对应时间段的分辨率 + FPS
2. 若原片质量 > 片段（例如原片 4K@60fps，片段 1080p@30fps）→ 从原片重新提取该时间段
3. 若片段质量更好（例如来自专业设备拍摄的近景镜头）→ 保留片段

**工具**：`ffmpeg` 提取片段（基于对齐的时间戳）

---

### Step 4：动作分析与统计（LLM / CV）

**目标**：对每个片段的舞蹈动作进行分类与统计

**数据**：
- 姿态估计：YOLO-Pose / MediaPipe / ViTPose
- 动作分类：LLM 描述（基于关键帧截图）
- 统计维度：镜头类型（近景/全身）、动作密度、能量级别

**输出**：每个片段的 `action_tags: List[str]`，用于后续检索和混剪排序

---

### Step 5：偶像与演出信息识别（LLM）

**目标**：识别 `girl_group / idol / performance_date / song / timestamp`

**信息来源（优先级从高到低）**：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | Reddit 标题 | 最直接，用户通常会标注 `[TWICE] Tzuyu - TT 20260425` |
| 2 | 片段文件名 | 常包含 YouTube video ID，如 `【youtube@s6JQrtlSuC0】` |
| 3 | Source 视频标题 | YouTube 标题通常含演出信息 |
| 4 | 上传日期 | YouTube 上传日期 ≈ 演出日期（±1-3天） |
| 5 | Reddit post 日期 | 最模糊，仅作参考 |

**关于文件名中的 YouTube ID**：
- 格式：`【youtube@s6JQrtlSuC0】` 中 `s6JQrtlSuC0` 即 YouTube video ID
- 可通过 YouTube Data API 反查视频标题、频道、上传时间
- 这是最强的元数据来源之一

**多人镜头处理**：
- Q：如果画面中有多个偶像怎么办？  
  A：路径中不写 idol 名，直接归入 `girl_group/performance_date/` 层级，以 song+timestamp 命名文件

**设计决策**：
- LLM 输入：Reddit 标题 + 文件名 + source 视频标题 + 日期信息
- LLM 输出：结构化 JSON `{group, idol, date, song, confidence}`
- confidence < 0.7 → fallback 到 Step 6 的 unidentified 目录

---

### Step 6：结构化存储

**目录结构**：

```
storage/
├── identified/
│   └── {girl_group}/
│       └── {performance_date}/
│           └── {idol}/               # 单人镜头
│               └── {song}_{timestamp}_{quality}.mp4
│           └── group/                # 多人镜头
│               └── {song}_{timestamp}_{quality}.mp4
└── unidentified/
    └── {reddit_post_id}/
        └── raw_clips/
```

**文件命名示例**：
```
twice/20260425/tzuyu/TT_02m15s_4K60fps.mp4
twice/20260425/group/TT_01m30s_1080p30fps.mp4
```

---

### Step 7：混剪编译

**触发条件**：用户指定 `idol + song + date` 组合

**流程**：
1. 从存储中检索所有匹配片段
2. 按 action_tags / quality / 时间戳排序
3. FFmpeg 拼接 + 转场处理
4. 输出：`{idol}_{song}_{date}_highlight.mp4`

**示例**：
```
目标：Twice TT 子瑜 20260425
检索：twice/20260425/tzuyu/*TT*
排序：4K优先，按原曲时间戳顺序
输出：tzuyu_TT_20260425_highlight.mp4
```

---

## 四、技术选型汇总

| 功能 | 工具 / 库 |
|------|-----------|
| Reddit 数据抓取 | Reddit JSON API（无需认证）|
| 视频下载 | JDownloader + `myjdapi` |
| Pixeldrain 下载 | 直接 HTTP |
| 音频指纹匹配 | `dejavu` / `audfprint` / `chromaprint` |
| 视觉帧匹配 | OpenCV SSIM |
| 视频提取/剪辑 | FFmpeg |
| 姿态估计 | YOLO-Pose / MediaPipe / ViTPose |
| 信息识别 | LLM（Claude/Gemini）|
| YouTube 元数据 | YouTube Data API v3 |
| 存储 | 本地文件系统（结构化目录）|

---

## 五、设计问答记录

**Q：source 和 clip 都在 Pixeldrain 时如何区分？**  
A：按文件大小 + 时长区分。完整视频通常是最大的文件（几百MB），片段是小文件（几十MB）。

**Q：片段没有音频时如何对齐？**  
A：用视觉帧匹配（OpenCV SSIM）。取片段首帧和末帧，在完整视频中搜索最相似帧。

**Q：多个偶像同框时如何处理文件路径？**  
A：`idol` 层级留空或改为 `group`，只保留 `girl_group/performance_date/` + song 信息。

**Q：如何从文件名反查完整视频信息？**  
A：文件名中 `【youtube@VIDEO_ID】` 格式包含 YouTube video ID，可直接调用 YouTube Data API 反查。

**Q：演出日期准确性如何保证？**  
A：YouTube 上传日期通常是演出次日，为最可靠的日期来源。Reddit 标题次之。

**Q：最终混剪的排序逻辑？**  
A：优先画质（4K > 1080p），其次按原曲中的时间戳顺序排列，保持歌曲完整叙事感。

**Q：如果一个 clip 无法识别归属怎么办？**  
A：fallback 到 `unidentified/{reddit_post_id}/` 目录。不丢弃，保留原始文件。

**Q：pipeline 是实时处理还是批量处理？**  
A：批量处理。以 Reddit post 为单位，下载 → 分析 → 存储 → 定期重跑新 post。

---

## 六、未来扩展方向

- **人脸识别**：引入偶像人脸识别模型，提升 idol 识别准确率
- **自动高光评分**：基于动作密度、镜头稳定性、画质综合打分，筛选最优片段
- **Web UI**：Gradio 界面，支持手动纠错偶像识别结果
- **Rednote 集成**：小红书同款 fancam 内容也纳入同一 pipeline
- **分布式处理**：大量视频时用 HuggingFace Jobs 或本地 GPU 加速分析

---

*— Hani · 无垠智穹*
