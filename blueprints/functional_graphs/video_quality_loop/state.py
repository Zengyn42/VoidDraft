"""
video_quality_schema — 视频质量循环子图 state schema

用于 batch generate → extract frames → evaluate → retry 循环。
entity.json 中通过 "state_schema": "video_quality_schema" 引用。
"""

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from framework.schema.reducers import _merge_dict
from framework.registry import register_schema


class VideoQualityState(TypedDict):
    # ── 标准框架字段 ─────────────────────────────────────────
    messages: Annotated[list[BaseMessage], add_messages]
    routing_target: str
    routing_context: str
    workspace: str
    project_root: str
    project_meta: dict
    last_stable_commit: str
    retry_count: int
    rollback_reason: str
    node_sessions: Annotated[dict, _merge_dict]
    knowledge_vault: str
    project_docs: str
    connector: str
    resilience_log: Annotated[list[dict], operator.add]

    # ── 视频质量循环专用字段 ──────────────────────────────────
    vq_prompt: str              # 扩写后的 prompt
    vq_image: str               # 输入图片路径
    vq_image_end: str           # 结束帧路径（keyframe_2/3）
    vq_image_mid: str           # 中间帧路径（keyframe_3）
    vq_audio: str               # 音频路径（digital_human）
    vq_workflow_type: str       # img2vid / keyframe_2 / keyframe_3 / digital_human
    vq_batch_size: int          # 每轮生成数量，默认 3
    vq_width: int               # 视频宽度
    vq_height: int              # 视频高度
    vq_frame_rate: int          # 帧率
    vq_num_frames: int          # 总帧数

    vq_videos: list[dict]       # [{path, prompt_id, status}, ...] 生成结果
    vq_frames: list[dict]       # [{video_path, frame_paths: [...]}, ...] 抽帧结果
    vq_evaluations: list[dict]  # [{video_path, visual, motion, anatomy, consistency, total, issues}, ...]
    vq_attempt: int             # 当前第几次尝试（从 1 开始）
    vq_max_attempts: int        # 最大重试次数，默认 3
    vq_feedback: str            # 上一轮评估反馈（用于优化 prompt）
    vq_best_result: dict        # 最终选出的最佳视频 {path, score, ...}


register_schema("video_quality_schema", VideoQualityState)
