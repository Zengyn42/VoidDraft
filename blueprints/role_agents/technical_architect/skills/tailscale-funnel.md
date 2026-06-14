# Skill: Tailscale Funnel — Expose Local Services Publicly

Tailscale Funnel publishes a local port as a permanent public HTTPS endpoint:
`https://<machine-name>.<tailnet-name>.ts.net`

**This machine's base URL**: `https://kingy.taile5f3af.ts.net`

> **WSL2 note**: `tailscale` runs on the **Windows host**, not inside WSL2.
> All `tailscale` commands must run in **Windows PowerShell (as Administrator)**.
> Python servers and file ops run in WSL as usual.

---

## Core Commands (PowerShell, Admin)

```powershell
# View current funnel config
tailscale funnel status

# Expose a local port publicly (foreground)
tailscale funnel <port>

# Expose a port in the background (survives reconnects)
tailscale funnel --bg <port>

# Add a path-based route (e.g. /viz → port 8090)
tailscale funnel --bg --set-path /viz localhost:8090

# Remove all funnel routes
tailscale funnel --bg reset

# Expose within tailnet only (not public)
tailscale serve <port>
```

---

## Expose a Static HTML File

**Step 1 — WSL**: Start a local HTTP server in the directory with your HTML:
```bash
mkdir -p /tmp/html_export
cp /path/to/file.html /tmp/html_export/index.html
python3 -m http.server 8090 --directory /tmp/html_export
```

**Step 2 — PowerShell (Admin)**: Funnel the port:
```powershell
tailscale funnel --bg 8090
```

Public URL: `https://kingy.taile5f3af.ts.net` → immediately accessible worldwide.

---

## Multi-Path Routing

```powershell
# Example config:
# |-- /        proxy http://127.0.0.1:8089   (Label Studio)
# |-- /agent   proxy http://localhost:8765   (ZenithLoom)
# |-- /viz     proxy http://localhost:8090   (custom HTML)

# Add /viz without disturbing existing routes:
tailscale funnel --bg --set-path /viz localhost:8090

# Remove /viz only:
tailscale funnel --bg --set-path /viz off
```

---

## Expose the Observability Dashboard

```powershell
# Observability server runs on 8765 (single-port with WS proxy)
tailscale funnel --bg 8765
```

Frontend WS auto-connects via `wss://kingy.taile5f3af.ts.net/ws` — no config needed.

---

## Quick Checklist

- [ ] Python HTTP server running in WSL on target port
- [ ] `tailscale funnel status` — verify no conflicting routes
- [ ] PowerShell running as Administrator
- [ ] Verify externally: `curl -I https://kingy.taile5f3af.ts.net[/path]`
- [ ] Cleanup: `tailscale funnel --bg reset`
