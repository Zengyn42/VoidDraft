# Content Retriever Pipeline — 设计文档

> 最后更新：2026-05-19

---

## 1. 定位与目标

从小红书（rednote）收藏夹批量抓取视频，自动完成：

1. **抓取** — 登录态收藏列表 API 拦截，获取视频 xsec_token
2. **下载** — XHS-Downloader 下载 MP4
3. **转录** — whisperX（Whisper + 词级对齐）+ pyannote 说话人分离
4. **摘要** — 可配置 LLM 生成结构化 JSON 摘要
5. **报告** — Markdown 汇总报告

---

## 2. 文件结构

```
pipelines/content_retriever/
├── DESIGN.md          ← 本文件
├── entity.json        ← ZenithLoom entry/exit 声明
├── graph.py           ← LangGraph 图定义（EntityLoader 优先加载）
├── state.py           ← ContentRetrieverState TypedDict
├── config.py          ← PipelineConfig dataclass + YAML 解析
├── llm_backend.py     ← LLM 摘要后端（Ollama / Claude / None）
├── validators.py      ← 所有节点函数（fetch/download/transcribe/summarize/report）
├── run.py             ← 独立 CLI 入口（graph.compile().invoke()）
├── configs/
│   └── rednote_example.yaml
├── sources/
│   └── rednote.py     ← Playwright 收藏 API 拦截
├── downloaders/
│   └── ...
└── analyzers/
    └── ...
```

---

## 3. LangGraph 图拓扑

```
START
  │
  ▼
[fetch]  ──── 无新帖 ───────────────────────────────► [report] → END
  │
  ▼
[download]  ── 无下载文件 ──────────────────────────► [report] → END
  │
  ▼
[transcribe]  ── 无语音内容 ────────────────────────► [report] → END
  │
  ▼
[summarize]  （summarize_backend=none 时被跳过）
  │
  ▼
[report] → END
```

### 条件路由规则

| 路由点 | 条件 | 目标 |
|--------|------|------|
| `fetch` 后 | `posts` 列表非空 | `download` |
| `fetch` 后 | `posts` 为空 | `report`（空报告） |
| `download` 后 | `downloads` 非空 | `transcribe` |
| `download` 后 | `downloads` 为空 | `report` |
| `transcribe` 后 | 有非空 transcript **且** `summarize_backend != none` | `summarize` |
| `transcribe` 后 | 无语音 **或** summarize 禁用 | `report` |

---

## 4. State Schema

```python
class ContentRetrieverState(TypedDict, total=False):
    config:      str                                       # JSON-serialised PipelineConfig
    posts:       Annotated[list[dict], operator.add]       # 抓取的帖子
    downloads:   Annotated[list[dict], operator.add]       # 下载记录
    transcripts: Annotated[list[dict], operator.add]       # 转录结果
    summaries:   Annotated[list[dict], operator.add]       # LLM 摘要
    errors:      Annotated[list[str],  operator.add]       # 错误消息（跨节点积累）
    report:      str                                       # 最终 Markdown 报告
```

**设计原则：**
- 所有列表字段用 `Annotated[list, operator.add]` reducer — 每个节点只追加自己的结果
- 每个节点初始化空 `errors: list[str] = []`，reducer 负责跨节点合并
- `config` 字段作为 JSON 字符串传递（节点内部 deserialise），避免 checkpointer 序列化问题

---

## 5. ZenithLoom 集成

### entity.json

```json
{
  "name": "content_retriever",
  "graph": {
    "state_schema": "content_retriever",
    "entry": "fetch",
    "exit":  "report"
  }
}
```

### 加载逻辑

ZenithLoom `EntityLoader` 按以下优先级加载图：

1. **`graph.py` 存在** → 直接调用 `build_graph(config, checkpointer)` ← **当前方案**
2. `entity.json["graph"]["nodes"]` 存在 → 声明式构建
3. 默认单节点 LLM 图

### 作为 SubgraphRefNode 使用（未来）

当父图（如 Hani）需要调用此 pipeline：

```json
// 父图 agent.json 节点配置
{
  "id": "content_retriever",
  "agent_dir": "/home/kingy/Foundation/VoidDraft/pipelines/content_retriever",
  "session_mode": "fresh_per_call",
  "routing_hint": "当需要抓取并处理小红书视频时使用"
}
```

- 进入点：`entity.json["graph"]["entry"]` = `fetch`
- 离开点：`entity.json["graph"]["exit"]`  = `report`
- 状态映射：`subgraph_topic` → `config`（父图传入），`report` → 父图输出字段

**注意：** 完整的 ZenithLoom 集成需要 `ContentRetrieverState` 继承 `BaseAgentState`，以及在父图 `agent.json` 中配置 `state_in`/`state_out` 字段映射。这是 TODO 项。

---

## 6. 各节点说明

### fetch

- **输入：** `config`
- **输出：** `posts` (list[dict])
- **实现：** Playwright 非持久 context，手动注入 cookie，监听 `note/collect/page` API 响应（favorites 模式）
- **去重：** 读取 `pipeline_state.json`（下载目录），已处理的 URL 跳过
- **关键字段：** `note_id`, `xsec_token`，拼装为 `xiaohongshu.com/explore/{id}?xsec_token={token}`

### download

- **输入：** `config`, `posts`
- **输出：** `downloads` (list[dict])
- **实现：** XHS-Downloader，文件写入 `dest_dir/Download/` 子目录
- **持久化：** 写回 `pipeline_state.json` 防止重复下载

### transcribe

- **输入：** `config`, `downloads`
- **输出：** `transcripts` (list[dict])
- **流程：**
  1. `ffmpeg` 提取 16kHz mono WAV
  2. `whisperX.load_model` + `transcribe()` (batch_size=8, language=zh)
  3. 0 segments → faster-whisper fallback (beam_size=5)
  4. `whisperX.load_align_model` + `align()`（词级时间戳）
  5. `pyannote/speaker-diarization-3.1` 说话人分离（需 HF token）
     - 输出为新版 `DiarizeOutput`，取 `.speaker_diarization` 字段
     - 输入用 torchaudio 预载为 waveform dict（规避 torchcodec 依赖）
  6. `whisperX.assign_word_speakers()` 将说话人标签合并回 segments
- **输出格式：** `[SPEAKER_00 0.0s] 文字内容`
- **is_multi_speaker：** `len(unique_speakers) > 1`

### summarize

- **输入：** `config`, `transcripts`
- **输出：** `summaries` (list[dict])
- **可选：** `summarize_backend = none` 时路由跳过此节点
- **LLM 调用：** 顺序（非并发），通过 `SummarizeLlmBackend` 封装
- **输出格式（JSON）：**

```json
{
  "summary": "2-4句话概括",
  "key_points": ["要点1", "要点2"],
  "topic": "健康养生",
  "target_audience": "适合人群描述",
  "actionable": true
}
```

- **持久化：** 摘要写入 `<transcript_file>.summary.json`

### report

- **输入：** `config`, `downloads`, `summaries`（+ 可选 `analysis`）
- **输出：** `report` (str, Markdown)
- **格式：** 每帖一节，含摘要、要点、文件信息
- **持久化：** 写入 `download_dir/report_YYYYMMDD_HHMMSS.md`

---

## 7. LLM 摘要后端

### SummarizeLlmBackend（`llm_backend.py`）

| backend | 实现 | 需要 |
|---------|------|------|
| `ollama` | `POST /api/generate`（stream=false） | 本地 Ollama 服务 |
| `claude` | `anthropic.Anthropic().messages.create()` | `ANTHROPIC_API_KEY` 环境变量 |
| `none` | 路由跳过，不实例化 | — |

### 配置示例

```yaml
# YAML 配置
summarize: true
summarize_backend: ollama
summarize_model: qwen2.5:7b
ollama_url: http://localhost:11434
```

```bash
# CLI override
python3 -m pipelines.content_retriever.run \
  --config configs/rednote_example.yaml \
  --max-posts 10 \
  --summarize-backend ollama \
  --summarize-model qwen2.5:7b
```

---

## 8. 关键技术决策

### 为什么用 Playwright 而不是 Scrapling

Scrapling 0.4.8 的 `page.css_first()` 不存在，API 不兼容。Playwright 非持久 context + 手动注入 cookie 方案更稳定，且能通过 `page.on("response")` 拦截 API 响应直接获取 `xsec_token`。

### 为什么 transcribe 顺序不并发

GPU 内存限制。whisperX medium 模型约占 2-3 GB VRAM，多进程并发会 OOM。

### 为什么 summarize 顺序不并发

用户决策：不用 Claude CLI 并发。LLM 调用顺序执行，简单可靠。

### 为什么 state 用 native list 而不是 JSON 字符串

旧版用 JSON 字符串（`state["posts"]` 是 `str`），是为了兼容 LangGraph 早期版本的序列化。现在用 `Annotated[list, operator.add]` reducer，每个节点直接操作 Python 对象，更符合 LangGraph 设计意图，也让 checkpointer 能正确合并并发节点的结果。

### 为什么 entity.json 不声明 nodes/edges

graph.py 存在时 ZenithLoom EntityLoader 优先加载它，声明式 nodes/edges 被忽略。entity.json 只需保留 `entry`/`exit` 元数据供 SubgraphRefNode 使用。

### pyannote DiarizeOutput 兼容

pyannote ≥ 3.x 的 `speaker-diarization-3.1` 返回 `DiarizeOutput` dataclass（含 `.speaker_diarization: Annotation`），而非直接返回 `Annotation`。用 `hasattr` 检测新旧两种格式。

### torchaudio 替代 torchcodec

PyTorch 2.8 + CUDA 128 环境下 torchcodec 的 FFmpeg 共享库找不到。改用 `torchaudio.load()` 预载 waveform dict 传给 pyannote，绕过 torchcodec。

---

## 9. TODO

- [ ] `ContentRetrieverState` 继承 `BaseAgentState`（ZenithLoom 完整集成）
- [ ] 父图 state_in/state_out 映射配置
- [ ] SQLite checkpointer 支持（断点续跑）
- [ ] 摘要节点支持 Gemini backend
- [ ] 批量跑完整收藏夹（50+ 视频）压测
