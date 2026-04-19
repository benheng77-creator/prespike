OpenClaw Control Panel — Deploy Guide
=====================================

WHAT THIS IS
------------
A portable, Node-only control panel for the OpenClaw scaffold. It exposes a
web UI with dropdowns and buttons to run lint / build / test / typecheck and
monitor the host. It opens a Cloudflare Quick Tunnel so the panel is
reachable from anywhere by URL — no port forwarding, no DNS, no account.

The panel is locked to a capability URL: every request must come through
/p/<TOKEN>/. The token is generated on first launch and stored in
state/token.txt. Bookmark the printed URL once and you never have to type
again.

THIS BUNDLE IS MEANT FOR THE TARGET PC
--------------------------------------
The setup PC (where this bundle was authored) does not run anything. Copy
the entire `openclaw-panel/` folder to the target PC and follow the steps
below there.

PREREQUISITES ON THE TARGET PC
------------------------------
1. Node.js 22 LTS or newer  -> https://nodejs.org/
2. (Optional) The OpenClaw repo, if you want lint/build/test to do more than
   report "no source". Set its path in config.json -> projectRoot.

NETWORK: outbound HTTPS to GitHub (one-time cloudflared download) and to
*.trycloudflare.com (the tunnel itself).

QUICK START — WINDOWS
---------------------
  1. Copy the openclaw-panel folder anywhere on the target PC, e.g.
     C:\Tools\openclaw-panel\
  2. Double-click  start.cmd
  3. The window prints two URLs:
       Public:  https://xxxx-xxxx.trycloudflare.com/p/<TOKEN>/
       Local:   http://127.0.0.1:8787/p/<TOKEN>/
  4. Open the Public URL in any browser, on any device. Bookmark it.
  5. (Optional) Auto-start at every Windows logon:
       Double-click  install-autostart.cmd
     Remove with     uninstall-autostart.cmd
  6. To stop the panel: double-click  stop.cmd

QUICK START — LINUX or macOS
----------------------------
  1. Copy the openclaw-panel folder, e.g. ~/openclaw-panel/
  2. chmod +x start.sh stop.sh install-autostart.sh uninstall-autostart.sh
  3. ./start.sh
  4. The terminal prints the Public and Local URLs. Bookmark the Public one.
  5. (Optional) Auto-start on login:
       ./install-autostart.sh
     Remove with     ./uninstall-autostart.sh
  6. To stop: ./stop.sh

USING THE PANEL
---------------
The UI is a single page with five cards:

  Public access     -> live tunnel URL with one-click Copy button
  Run an action     -> dropdown of named actions + Run button + quick grid
                       of one-click buttons
  Latest run        -> live status, exit code, duration, streamed output
  System            -> hostname, platform, Node version, uptime, CPU, RAM
  Recent runs       -> last 10 runs with status badges

Action list (no typing needed for any of these):
  pnpm install
  pnpm lint
  oxfmt check / oxfmt write
  tsc (root) / tsc plugin-sdk dts
  pnpm build
  pnpm test
  git status / git log (10)
  node --version / pnpm --version
  Disk usage

The panel never accepts a raw shell command from the UI — only an action id
from the dropdown. New actions go in panel/server.mjs (ACTIONS map).

FILE LAYOUT
-----------
  start.cmd / start.sh                 launchers (one click on target PC)
  stop.cmd  / stop.sh                  stop the panel + tunnel
  install-autostart.cmd / .sh          register auto-start on target PC
  uninstall-autostart.cmd / .sh        remove auto-start
  config.json                          editable port/host/projectRoot
  panel/server.mjs                     the HTTP server (zero deps)
  panel/public/index.html              the UI shell
  panel/public/app.js                  UI logic (SSE + fetch)
  panel/public/style.css               styling
  bin/cloudflared(.exe)                downloaded on first launch
  state/token.txt                      auth token (chmod 600)
  state/tunnel.url                     captured *.trycloudflare.com URL
  state/panel.log                      panel server log
  state/tunnel.log                     cloudflared log
  state/panel.pid, tunnel.pid          PIDs (Linux/macOS)

SECURITY NOTES
--------------
- Cloudflare Quick Tunnels are unauthenticated *at the cloudflared layer*.
  The capability URL is the only auth. Treat the printed Public URL like a
  password. Do not paste it into chat, screenshots, or public docs.
- The panel binds to 127.0.0.1 by default. The tunnel is the only way in.
- Anyone who knows the URL can run any of the allowlisted actions on the
  target PC. Run uninstall-autostart and stop the panel before sharing the
  PC.
- To rotate the token: stop the panel, delete state/token.txt, restart.
  The next start prints a new URL.

TROUBLESHOOTING
---------------
- "Tunnel URL did not appear after 30 seconds": check state/tunnel.log.
  Most common cause is no outbound HTTPS to *.trycloudflare.com.
- "Cannot find Node": install Node 22 LTS from https://nodejs.org/ then
  re-run the launcher.
- "Action runs but pnpm not found": install pnpm globally with
  `npm install -g pnpm` on the target PC, or set ACTIONS[id].cmd in
  panel/server.mjs to a fully-qualified path.
- Panel binds but tunnel doesn't connect: corporate proxies often block
  Cloudflare. Set HTTPS_PROXY before launching.

UPDATING
--------
Stop the panel (stop.cmd / stop.sh), copy a fresh openclaw-panel folder
over the old one (preserving state/token.txt and state/tunnel.url if you
want the same URL), then re-launch.

That's it. The whole bundle is deliberately small enough to read end-to-end.
