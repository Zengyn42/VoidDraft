# Content Retriever Pipeline — 使用指南

---

## 快速开始

```bash
cd /home/kingy/Foundation/VoidDraft

python3 -m pipelines.content_retriever.run \
  --config pipelines/content_retriever/configs/rednote_example.yaml \
  --max-posts 10
```

---

## 前置依赖

### Python 包

```bash
pip install playwright whisperx faster-whisper pyannote.audio torchaudio pyyaml requests
playwright install chromium
```

### 系统依赖

```bash
# ffmpeg（音频提取）
sudo apt install ffmpeg

# XHS-Downloader（小红书下载器）
pip install xhs-downloader   # 或参考其文档安装
```

### HuggingFace 模型授权

说话人分离依赖两个 gated 模型，需要用 HuggingFace 账号各自 agree 条款：

1. https://huggingface.co/pyannote/speaker-diarization-3.1
2. https://huggingface.co/pyannote/speaker-diarization-community-1

登录 HuggingFace CLI：

```bash
huggingface-cli login
```

---

## 配置文件

配置文件路径：`configs/rednote_example.yaml`

```yaml
# ── 数据源 ──────────────────────────────────────────────────────────────────
source:
  type: rednote
  account_user_id: 60b6bf360000000001007c35   # 从 profile URL 里取
  mode: favorites       # favorites=收藏 | posted=发布
  max_scroll: 20        # 滚动次数（收藏多可调高）
  request_delay: 1.5
  video_only: true      # 只处理视频，跳过图文

# ── 下载 ─────────────────────────────────────────────────────────────────────
download_dir: /home/kingy/Foundation/EdenGateway/rednote_downloads
max_posts: 50

# ── 视觉分析（通常不需要，转录靠 transcribe 节点）──────────────────────────
analyzer: none

# ── LLM 摘要 ─────────────────────────────────────────────────────────────────
summarize: true
summarize_backend: none        # "ollama" | "claude" | "none"
summarize_model: ""            # "qwen2.5:7b" (ollama) | "claude-haiku-4-5" (claude)
ollama_url: http://localhost:11434
```

### 更新 Cookie（登录态）

rednote.py 里硬编码了 cookie，Cookie 过期后需要手动更新：

1. 浏览器打开 https://www.rednote.com 并登录
2. F12 → Network → 找任意请求 → 复制 Cookie 头
3. 编辑 `sources/rednote.py`，更新 `_COOKIES` 字典

---

## 运行方式

### 基础运行

```bash
python3 -m pipelines.content_retriever.run \
  --config /path/to/rednote_example.yaml
```

### 限制处理数量

```bash
# 只处理 5 个新视频
python3 -m pipelines.content_retriever.run \
  --config configs/rednote_example.yaml \
  --max-posts 5
```

### 开启 Ollama 摘要

```bash
# 需要本地 Ollama 服务在跑，且已拉取对应模型
ollama pull qwen2.5:7b

python3 -m pipelines.content_retriever.run \
  --config configs/rednote_example.yaml \
  --max-posts 10 \
  --summarize-backend ollama \
  --summarize-model qwen2.5:7b
```

### 开启 Claude 摘要

```bash
export ANTHROPIC_API_KEY=sk-ant-...

python3 -m pipelines.content_retriever.run \
  --config configs/rednote_example.yaml \
  --max-posts 10 \
  --summarize-backend claude \
  --summarize-model claude-haiku-4-5
```

### CLI 参数一览

| 参数 | 类型 | 说明 |
|------|------|------|
| `--config` | str（必填） | YAML 配置文件路径 |
| `--max-posts` | int | 覆盖配置里的 max_posts |
| `--summarize-backend` | str | `ollama` / `claude` / `none` |
| `--summarize-model` | str | 对应后端的模型名 |
| `--thread-id` | str | LangGraph thread ID（预留，checkpointer 用） |

---

## 输出文件

所有文件写入 `download_dir`（默认 `/home/kingy/Foundation/EdenGateway/rednote_downloads/`）：

```
rednote_downloads/
├── pipeline_state.json                    ← 已处理的 URL 记录（防重复下载）
├── report_20260519_212740.md              ← 每次运行生成一个报告
│
└── {post_id}_{title}/
    ├── text.txt                           ← 帖子正文
    └── Download/
        ├── {filename}.mp4                 ← 下载的视频
        ├── {filename}.wav                 ← 提取的音频（16kHz mono）
        ├── {filename}.txt                 ← Whisper 转录文本
        └── {filename}.summary.json        ← LLM 摘要（有 summarize 时）
```

### 转录文本格式（.txt）

```
[SPEAKER_00 0.1s] 哈喽大家好今天来给大家介绍一下...
[SPEAKER_00 12.4s] 首先我们需要准备这些材料...
[SPEAKER_01 25.8s] 对对对就是这个步骤很重要...
```

- 单人视频：所有行 `SPEAKER_00`
- 多人视频：出现 `SPEAKER_01`、`SPEAKER_02` 等

### 摘要 JSON 格式（.summary.json）

```json
{
  "post_id": "69fc956a00000000230146af",
  "post_title": "甲状腺调理分三步，坚持跟着这样做！",
  "is_multi_speaker": true,
  "summary": "视频介绍通过刮痧三步骤调理甲状腺功能...",
  "key_points": [
    "头顶梳刮激活→颈部刮痧→膀胱经络疏通",
    "刮痧原理：激活微循环和免疫代谢",
    "甲状腺问题与肝气郁结相关，需配合情绪调理"
  ],
  "topic": "健康养生",
  "target_audience": "有甲状腺问题、对中医调理感兴趣的人群",
  "actionable": true
}
```

---

## 断点续跑

`pipeline_state.json` 记录了所有已下载的帖子 URL。每次运行只处理**新增**的收藏视频，不会重复下载已有内容。

如需强制重新处理所有内容：

```bash
rm /home/kingy/Foundation/EdenGateway/rednote_downloads/pipeline_state.json
```

---

## 处理不同账号

在 YAML 里修改 `account_user_id`，或创建新的配置文件：

```yaml
source:
  type: rednote
  account_user_id: <目标账号ID>   # 从 profile URL 里取
  mode: favorites
```

账号 ID 在 URL 里：`https://www.rednote.com/user/profile/60b6bf360000000001007c35`

---

## 常见问题

**Q: "Collected 0 new post(s)" — 没有新帖子**

所有收藏都已处理过。删除 `pipeline_state.json` 强制重跑，或等收藏更新。

**Q: Whisper 返回 0 segments**

该视频没有人声（纯音乐 / 字幕视频）。pipeline 会自动跳过这个视频的摘要，不报错。

**Q: diarization failed: 403**

HuggingFace 账号未授权 pyannote 模型。访问以下两个链接 agree 条款：
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/speaker-diarization-community-1

**Q: Cookie 失效，fetch 返回空**

浏览器重新登录 rednote，更新 `sources/rednote.py` 里的 `_COOKIES`。

**Q: Ollama 摘要超时**

默认超时 180 秒。模型太大或 CPU 推理慢时可能触发。换更小的模型（如 `qwen2.5:3b`），或用 GPU 加速 Ollama。

---

## 作为 ZenithLoom SubgraphRefNode（未来）

当 Hani 需要调用此 pipeline，在父图 `agent.json` 里加节点：

```json
{
  "id": "content_retriever",
  "agent_dir": "/home/kingy/Foundation/VoidDraft/pipelines/content_retriever",
  "session_mode": "fresh_per_call",
  "routing_hint": "当需要抓取并转录小红书收藏视频时使用"
}
```

- 进入点：`fetch`（来自 `entity.json["graph"]["entry"]`）
- 离开点：`report`（来自 `entity.json["graph"]["exit"]`）
- 配置通过 `subgraph_topic` 字段传入（需要额外 state 映射，参见 DESIGN.md §5）
