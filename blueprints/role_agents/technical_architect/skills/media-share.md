# Skill: Media Share — Public Preview Pages for Large Files

Use when a result file (video, image, audio) is too large for Discord (~8 MB limit) and you need to show it to the user. Generates an HTML preview page with embedded player, copies files to a persistent share directory, and returns a public URL via Tailscale Funnel.

## When to Use

- Generated a video (e.g. CogVideoX output, ffmpeg render) and need to show it
- Produced a large image (screenshot, visualization, high-res chart)
- Multiple media results from a batch run that need comparison
- Any file the user should view in a browser rather than download

## Basic Usage

```bash
# Single file
python3 -m framework.media_share \
  --project <ProjectName> \
  /path/to/video.mp4

# Multiple files with title and background context
python3 -m framework.media_share \
  --project <ProjectName> \
  --title "Descriptive Title" \
  --context "Background notes about what this is, model params, etc." \
  /path/to/file1.mp4 /path/to/file2.png
```

**stdout** returns the public URL — capture it and send to the user in Discord.

## Parameters

| Flag | Required | Description |
|------|----------|-------------|
| `files` | Yes | One or more local file paths |
| `--project`, `-p` | **Yes** (always specify) | Project name for directory grouping |
| `--title` | No | Page title (default: "Shared Media") |
| `--context`, `-c` | Recommended | Background knowledge embedded in the HTML page |

## What `--context` Should Include

Always provide context so the share page is self-explanatory:
- What was generated and why
- Model name, key parameters (guidance scale, steps, resolution, etc.)
- Prompt or input description
- Comparison notes if multiple files (e.g. "left=v1, right=v2")
- Any relevant observations

## Directory Structure

```
EdenGateway/share/
  GenesisExp/
    a1b2c3d4/
      index.html        ← browser preview page
      context.md         ← background notes (plain text backup)
      output_001.mp4     ← copied media file
      output_002.mp4     ← copied media file
  NebulaAtlas/
    f5e6d7c8/
      index.html
      context.md
      screenshot.png
```

Files are **copied** (not symlinked) — they persist even if the original is moved or deleted.

## Managing Shares

```bash
# List all shares across all projects
python3 -m framework.media_share --cleanup

# List shares for a specific project
python3 -m framework.media_share --cleanup --project GenesisExp

# Remove a specific share
python3 -m framework.media_share --remove GenesisExp/a1b2c3d4

# Remove all shares
python3 -m framework.media_share --cleanup --all
```

## Typical Agent Workflow

1. Task produces large media output (video generation, visualization, etc.)
2. Agent runs `media_share` with `--project`, `--title`, `--context`
3. Captures the URL from stdout
4. Replies to user in Discord with the URL and a brief summary

Example:
```bash
URL=$(python3 -m framework.media_share \
  --project GenesisExp \
  --title "Dolly-In Camera Test" \
  --context "CogVideoX-5B, 49 frames @ 8fps, guidance_scale=6.0.
Prompt: 'Golden retriever running on beach, dolly in camera movement.'
Resolution: 720x480." \
  /tmp/cogvideo_output/sample_001.mp4 2>/dev/null)
echo "Preview: $URL"
```

## Supported Media Types

| Type | Extensions | HTML Element |
|------|-----------|-------------|
| Video | `.mp4` `.webm` `.mov` `.avi` `.mkv` `.m4v` | `<video>` player |
| Image | `.png` `.jpg` `.jpeg` `.gif` `.webp` `.bmp` `.svg` | `<img>` |
| Audio | `.mp3` `.wav` `.ogg` `.flac` `.m4a` | `<audio>` player |
| Other | any | Download link |

## Architecture & Design Decisions

### Why Copy Instead of Symlink

Files are **copied** into the share directory, not symlinked. This ensures:
- Share links remain valid even if the original file is moved, deleted, or overwritten
- Each share is a self-contained snapshot — no broken links weeks later
- Trade-off: uses more disk, but persistence is worth it for a knowledge archive

### Why Starlette Instead of http.server

The share server (`framework/share_server.py`) uses Starlette + uvicorn instead of Python's built-in `http.server` because:
- **HTTP Range requests**: `http.server` does NOT support `Range` headers. Without Range support, browsers cannot seek/scrub through video files — playback is start-to-finish only. Starlette's `StaticFiles` returns `206 Partial Content` with proper `Content-Range` headers, enabling free video seeking.
- **Proper MIME types**: Automatic content-type detection for all media formats.
- **Async I/O**: Non-blocking file serving, handles concurrent requests without threading issues.

### Network Topology (WSL2 + Tailscale)

```
Browser (anywhere) 
  → https://kingy.taile5f3af.ts.net/share/...  (Tailscale Funnel, HTTPS)
  → Windows host receives on port 443
  → Funnel proxies to localhost:8091
  → WSL2 share_server (Starlette) on 127.0.0.1:8091
  → Reads files from EdenGateway/share/
```

Key points:
- `tailscale` runs on **Windows**, not WSL. All `tailscale` commands must run in **Windows PowerShell (Admin)**.
- The share_server binds to `127.0.0.1` — accessible from Windows via localhost (WSL2 port forwarding), but not directly from LAN.
- Funnel provides HTTPS termination — no cert management needed.
- The root URL `/share/` returns 404 (no directory listing). Only specific share paths work. This is intentional — no browsing of all shares.

### Project-Based Organization

Shares are grouped by project name (`--project`) to keep the archive navigable:
- Each project gets its own subdirectory under `EdenGateway/share/`
- Within a project, each share gets a unique 8-char hex ID
- The `context.md` file alongside media files serves as a plain-text backup of the background knowledge, separate from the HTML

### Server Lifecycle

- The share_server is auto-started by `media_share.py` when not already running
- It runs as a detached background process (PID saved to `.server.pid`)
- If the server dies (reboot, crash), the next `media_share` call restarts it automatically
- The Tailscale Funnel path (`/share → localhost:8091`) is a one-time Windows-side configuration that persists across reboots

## Prerequisites

- Starlette + uvicorn installed (both in requirements.txt)
- Tailscale Funnel path configured once (PowerShell Admin):
  ```powershell
  tailscale funnel --bg --set-path /share localhost:8091
  ```
- Share directory: `/home/kingy/Foundation/EdenGateway/share/`
- Public base URL: `https://kingy.taile5f3af.ts.net/share/`
