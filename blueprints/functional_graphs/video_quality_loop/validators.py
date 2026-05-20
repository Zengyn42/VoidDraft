"""Deterministic + async nodes for video_quality_loop subgraph.

Nodes:
  batch_generate    — async: call ComfyUIClient N times to generate videos
  extract_frames    — sync:  ffmpeg extract keyframes from each video
  quality_evaluate  — sync:  call Ollama API (gemma4) with multi-image for quality scoring
  quality_gate      — sync:  check scores, route to retry or end
"""

import base64
import json
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

COMFYUI_HOST = "localhost"
COMFYUI_PORT = 8188
OLLAMA_URL = "http://localhost:11434/api/chat"
EVAL_MODEL = "gemma4:26b"

PASS_THRESHOLD = 7.0    # 总分 ≥ 7 合格
FAIL_DIMENSION = 4.0    # 任一维度 < 4 一票否决
EXTRACT_FPS = 2         # 每秒抽帧数
DEFAULT_BATCH_SIZE = 3
DEFAULT_MAX_ATTEMPTS = 3

EVAL_PROMPT_TEMPLATE = """你是视频质量评估专家。以下是一组来自AI生成视频的连续帧（按时间顺序排列）。

原始 Prompt: {prompt}

请按以下4个维度打分（每项0-10分）：
1. visual — 画面质量（清晰度、色彩、伪影、闪烁）
2. motion — 动作自然度（运动连贯性、有无突变、抖动）
3. anatomy — 人体/物体完整性（手指/面部/肢体是否正常、物体是否畸变）
4. consistency — Prompt一致性（生成内容是否符合描述）

严格按以下JSON格式输出（不要输出其他内容）：
{{"visual": 8, "motion": 7, "anatomy": 6, "consistency": 9, "total": 7.5, "issues": ["具体问题1", "具体问题2"]}}

注意：total 是四个维度的平均分。issues 列出具体发现的问题，没有问题则为空数组。"""


# ── Helper: import ComfyUI client ────────────────────────────────────

def _get_comfyui_client():
    """Lazy import ComfyUIClient to avoid import-time issues."""
    import importlib.util
    clients_dir = Path(__file__).resolve().parent.parent.parent.parent / "framework" / "clients" / "comfyui"
    spec = importlib.util.spec_from_file_location("comfyui_client", clients_dir / "comfyui_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ComfyUIClient(host=COMFYUI_HOST, port=COMFYUI_PORT)


def _get_workflow_manager():
    """Lazy import WorkflowManager."""
    import importlib.util
    clients_dir = Path(__file__).resolve().parent.parent.parent.parent / "framework" / "clients" / "comfyui"
    spec = importlib.util.spec_from_file_location("workflow_manager", clients_dir / "workflow_manager.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.WorkflowManager()


# ── Node 1: batch_generate (async) ──────────────────────────────────

async def batch_generate(state: dict) -> dict:
    """Call ComfyUI to generate batch_size videos.

    Reads from state:
      routing_context — JSON string with generation params (first call)
      vq_prompt, vq_image, etc. — from previous retry
      vq_feedback — evaluation feedback to refine prompt on retry

    Writes to state:
      vq_* fields populated, vq_videos with generation results
    """
    attempt = state.get("vq_attempt", 0)

    # First call: parse routing_context
    if attempt == 0:
        try:
            params = json.loads(state.get("routing_context", "{}"))
        except (json.JSONDecodeError, TypeError):
            params = {}

        prompt = params.get("prompt", state.get("vq_prompt", ""))
        image = params.get("image", state.get("vq_image", ""))
        workflow_type = params.get("workflow_type", state.get("vq_workflow_type", "img2vid"))
        batch_size = params.get("batch_size", state.get("vq_batch_size", DEFAULT_BATCH_SIZE))
        max_attempts = params.get("max_attempts", state.get("vq_max_attempts", DEFAULT_MAX_ATTEMPTS))
        width = params.get("width", state.get("vq_width", 1280))
        height = params.get("height", state.get("vq_height", 720))
        frame_rate = params.get("frame_rate", state.get("vq_frame_rate", 24))
        num_frames = params.get("num_frames", state.get("vq_num_frames", 241))
    else:
        prompt = state.get("vq_prompt", "")
        image = state.get("vq_image", "")
        workflow_type = state.get("vq_workflow_type", "img2vid")
        batch_size = state.get("vq_batch_size", DEFAULT_BATCH_SIZE)
        max_attempts = state.get("vq_max_attempts", DEFAULT_MAX_ATTEMPTS)
        width = state.get("vq_width", 1280)
        height = state.get("vq_height", 720)
        frame_rate = state.get("vq_frame_rate", 24)
        num_frames = state.get("vq_num_frames", 241)

    # Incorporate feedback into prompt on retry
    feedback = state.get("vq_feedback", "")
    effective_prompt = prompt
    if feedback and attempt > 0:
        effective_prompt = f"{prompt}\n\n[Quality requirements based on previous attempt: {feedback}]"

    attempt += 1
    logger.info(f"[batch_generate] attempt={attempt}/{max_attempts}, batch_size={batch_size}, workflow={workflow_type}")

    client = _get_comfyui_client()
    wf_manager = _get_workflow_manager()

    # Build file upload map based on workflow type
    files_map = {"image": image}
    if workflow_type == "keyframe_2":
        files_map["image_end"] = state.get("vq_image_end", "")
    elif workflow_type == "keyframe_3":
        files_map["image_end"] = state.get("vq_image_end", "")
        files_map["image_mid"] = state.get("vq_image_mid", "")
    elif workflow_type == "digital_human":
        files_map["audio"] = state.get("vq_audio", "")

    # Remove empty entries
    files_map = {k: v for k, v in files_map.items() if v}

    videos = []
    for i in range(batch_size):
        logger.info(f"[batch_generate] generating video {i+1}/{batch_size}")
        try:
            # Health check
            await client.health_check()

            # Upload files
            uploaded = {}
            for field, path in files_map.items():
                if path:
                    server_name = await client.upload_file(path)
                    uploaded[field] = server_name

            # Prepare and submit workflow
            workflow = wf_manager.prepare_workflow(
                workflow_type,
                prompt=effective_prompt,
                uploaded_files=uploaded,
                width=width,
                height=height,
                frame_rate=frame_rate,
                num_frames=num_frames,
            )
            prompt_id = await client.submit_workflow(workflow)
            logger.info(f"[batch_generate] submitted: prompt_id={prompt_id}")

            # Wait for completion
            await client.wait_for_completion(prompt_id)

            # Get outputs
            outputs = await client.get_outputs(prompt_id)
            video_outputs = [o for o in outputs if o["media_type"] == "video"]

            if video_outputs:
                # Download video
                output_dir = Path(tempfile.gettempdir()) / "vq_loop"
                output_dir.mkdir(parents=True, exist_ok=True)
                local_path = output_dir / f"attempt{attempt}_batch{i}_{prompt_id}.mp4"
                await client.download_file(video_outputs[0]["download_url"], local_path)
                videos.append({
                    "path": str(local_path),
                    "prompt_id": prompt_id,
                    "status": "completed",
                    "batch_index": i,
                })
                logger.info(f"[batch_generate] video {i+1} done: {local_path}")
            else:
                videos.append({"path": "", "prompt_id": prompt_id, "status": "no_output", "batch_index": i})

        except Exception as e:
            logger.error(f"[batch_generate] video {i+1} failed: {e}")
            videos.append({"path": "", "prompt_id": "", "status": f"error: {e}", "batch_index": i})

    return {
        "vq_prompt": prompt,
        "vq_image": image,
        "vq_workflow_type": workflow_type,
        "vq_batch_size": batch_size,
        "vq_max_attempts": max_attempts,
        "vq_width": width,
        "vq_height": height,
        "vq_frame_rate": frame_rate,
        "vq_num_frames": num_frames,
        "vq_attempt": attempt,
        "vq_videos": videos,
    }


# ── Node 2: extract_frames (sync) ───────────────────────────────────

def extract_frames(state: dict) -> dict:
    """Extract frames from each generated video using ffmpeg.

    Reads: vq_videos
    Writes: vq_frames
    """
    videos = state.get("vq_videos", [])
    all_frames = []

    for video in videos:
        video_path = video.get("path", "")
        if not video_path or not Path(video_path).exists():
            all_frames.append({"video_path": video_path, "frame_paths": [], "error": "file not found"})
            continue

        # Create output dir for frames
        frames_dir = Path(video_path).parent / f"{Path(video_path).stem}_frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        try:
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-vf", f"fps={EXTRACT_FPS}",
                "-q:v", "2",
                str(frames_dir / "frame_%04d.jpg"),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            if result.returncode != 0:
                logger.error(f"[extract_frames] ffmpeg error: {result.stderr[:200]}")
                all_frames.append({"video_path": video_path, "frame_paths": [], "error": result.stderr[:200]})
                continue

            # Collect frame paths (sorted)
            frame_paths = sorted(str(p) for p in frames_dir.glob("frame_*.jpg"))
            logger.info(f"[extract_frames] {video_path}: {len(frame_paths)} frames extracted")
            all_frames.append({"video_path": video_path, "frame_paths": frame_paths})

        except subprocess.TimeoutExpired:
            all_frames.append({"video_path": video_path, "frame_paths": [], "error": "ffmpeg timeout"})
        except Exception as e:
            all_frames.append({"video_path": video_path, "frame_paths": [], "error": str(e)})

    return {"vq_frames": all_frames}


# ── Node 3: quality_evaluate (sync) ──────────────────────────────────

def quality_evaluate(state: dict) -> dict:
    """Evaluate each video's quality by sending frames to Ollama (Gemma4).

    Reads: vq_frames, vq_prompt
    Writes: vq_evaluations
    """
    import requests

    frames_data = state.get("vq_frames", [])
    prompt = state.get("vq_prompt", "")
    evaluations = []

    for entry in frames_data:
        video_path = entry.get("video_path", "")
        frame_paths = entry.get("frame_paths", [])

        if not frame_paths:
            evaluations.append({
                "video_path": video_path,
                "visual": 0, "motion": 0, "anatomy": 0, "consistency": 0,
                "total": 0, "issues": [entry.get("error", "no frames")],
            })
            continue

        # Sample frames if too many (max ~20 frames to keep context manageable)
        if len(frame_paths) > 20:
            step = len(frame_paths) // 20
            sampled = frame_paths[::step][:20]
        else:
            sampled = frame_paths

        # Encode frames as base64
        images_b64 = []
        for fp in sampled:
            try:
                with open(fp, "rb") as f:
                    images_b64.append(base64.b64encode(f.read()).decode("utf-8"))
            except Exception as e:
                logger.warning(f"[quality_evaluate] failed to read frame {fp}: {e}")

        if not images_b64:
            evaluations.append({
                "video_path": video_path,
                "visual": 0, "motion": 0, "anatomy": 0, "consistency": 0,
                "total": 0, "issues": ["failed to encode frames"],
            })
            continue

        # Call Ollama
        eval_prompt = EVAL_PROMPT_TEMPLATE.format(prompt=prompt)
        try:
            resp = requests.post(
                OLLAMA_URL,
                json={
                    "model": EVAL_MODEL,
                    "messages": [{
                        "role": "user",
                        "content": eval_prompt,
                        "images": images_b64,
                    }],
                    "stream": False,
                    "format": "json",
                },
                timeout=120,
            )
            resp.raise_for_status()
            result = resp.json()
            content = result.get("message", {}).get("content", "{}")

            # Parse JSON response
            try:
                scores = json.loads(content)
                # Validate and default
                evaluation = {
                    "video_path": video_path,
                    "visual": float(scores.get("visual", 0)),
                    "motion": float(scores.get("motion", 0)),
                    "anatomy": float(scores.get("anatomy", 0)),
                    "consistency": float(scores.get("consistency", 0)),
                    "total": float(scores.get("total", 0)),
                    "issues": scores.get("issues", []),
                }
                # Recalculate total for safety
                dims = [evaluation["visual"], evaluation["motion"],
                        evaluation["anatomy"], evaluation["consistency"]]
                evaluation["total"] = round(sum(dims) / 4, 1)
                logger.info(f"[quality_evaluate] {video_path}: total={evaluation['total']}")
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"[quality_evaluate] failed to parse Ollama response: {content[:200]}")
                evaluation = {
                    "video_path": video_path,
                    "visual": 0, "motion": 0, "anatomy": 0, "consistency": 0,
                    "total": 0, "issues": [f"parse error: {e}"],
                }

        except Exception as e:
            logger.error(f"[quality_evaluate] Ollama call failed: {e}")
            evaluation = {
                "video_path": video_path,
                "visual": 0, "motion": 0, "anatomy": 0, "consistency": 0,
                "total": 0, "issues": [f"ollama error: {e}"],
            }

        evaluations.append(evaluation)

    return {"vq_evaluations": evaluations}


# ── Node 4: quality_gate (sync) ──────────────────────────────────────

def quality_gate(state: dict) -> dict:
    """Check evaluation scores, select best video or trigger retry.

    合格标准:
      - total >= PASS_THRESHOLD (7.0)
      - 所有维度 >= FAIL_DIMENSION (4.0)

    Reads: vq_evaluations, vq_attempt, vq_max_attempts
    Writes: vq_best_result, vq_feedback, routing_target
    """
    evaluations = state.get("vq_evaluations", [])
    attempt = state.get("vq_attempt", 1)
    max_attempts = state.get("vq_max_attempts", DEFAULT_MAX_ATTEMPTS)

    # Find qualified videos
    qualified = []
    all_issues = []
    for ev in evaluations:
        dims = [ev.get("visual", 0), ev.get("motion", 0),
                ev.get("anatomy", 0), ev.get("consistency", 0)]
        total = ev.get("total", 0)
        has_fatal = any(d < FAIL_DIMENSION for d in dims)

        if total >= PASS_THRESHOLD and not has_fatal:
            qualified.append(ev)
        else:
            all_issues.extend(ev.get("issues", []))

    # If any qualified, pick the best
    if qualified:
        best = max(qualified, key=lambda x: x["total"])
        logger.info(f"[quality_gate] PASS: best score={best['total']}, path={best['video_path']}")
        return {
            "vq_best_result": best,
            "vq_feedback": "",
            "routing_target": "__end__",
        }

    # If max attempts reached, pick the best available (even if below threshold)
    if attempt >= max_attempts:
        if evaluations:
            best = max(evaluations, key=lambda x: x.get("total", 0))
            logger.warning(f"[quality_gate] MAX ATTEMPTS: returning best={best['total']}")
            return {
                "vq_best_result": best,
                "vq_feedback": f"Max attempts ({max_attempts}) reached. Best score: {best['total']}",
                "routing_target": "__end__",
            }
        return {
            "vq_best_result": {},
            "vq_feedback": "All generation attempts failed",
            "routing_target": "__end__",
        }

    # Retry: collect feedback for prompt refinement
    unique_issues = list(set(all_issues))[:5]  # Top 5 unique issues
    feedback = "; ".join(unique_issues) if unique_issues else "quality below threshold"
    logger.info(f"[quality_gate] RETRY: attempt {attempt}/{max_attempts}, issues: {feedback}")

    return {
        "vq_feedback": feedback,
        "routing_target": "batch_generate",
    }
