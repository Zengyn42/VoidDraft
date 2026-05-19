# VoidDraft × Pulsify — 设计规则

> 记录时间：2026-05-18  
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
- 403 通常是临时 rate limit，不是永久失败
- 实现：`content_retriever/downloaders/pixeldrain.py`（VoidDraft，因为 Pixeldrain 是业务特定平台）

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

**对齐策略**：
- 主策略：音频指纹（librosa chroma features，滑动窗口互相关）
- Fallback：视觉帧匹配（OpenCV SSIM）— 用于无音轨片段
- 无音轨 clip 直接跳到视觉对齐
- 置信度低于阈值 → 标记 `unmatched`，clip 作为独立素材保存（不丢弃）
- **大多数手机 fancam clip 会以 `unmatched` 状态进入存储层**，这是正常的

---

*— Hani · 无垠智穹*
