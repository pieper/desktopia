# Thin wrapper over vast.sh. See vast.sh for details.
.PHONY: search best up up-best up-ghcr ls ssh sync wl-build wl-fixwayland wl-setup stream port pull status logs stop start down debug \
        slicer-up tunnel tunnel-close mcp-ready push-module slicer-restart

# --- rent / connect ---
search:    ; ./vast.sh search
best:      ; ./vast.sh best
up:        ; ./vast.sh up $(OFFER)
up-best:   ; ./vast.sh up-best
up-ghcr:   ; ./vast.sh up $(OFFER) ghcr
ls:        ; ./vast.sh ls
ssh:       ; ./vast.sh ssh
sync:      ; ./vast.sh sync

# --- build the headless Wayland compositor on a bare instance ---
wl-build:      ; ./vast.sh wl-build       # gst-wayland-display (Smithay compositor)
wl-fixwayland: ; ./vast.sh wl-fixwayland  # libwayland >= 1.23 into /usr/local
wl-setup:      ; ./vast.sh wl-setup       # both of the above, one shot

# --- run + reach the desktop ---
stream:    ; ./vast.sh stream             # compositor + encoder + QUIC server + desktop
port:      ; ./vast.sh port               # public IP:PORT for client/index.html
pull:      ; ./vast.sh pull $(REMOTE) $(LOCAL)

# --- lifecycle ---
status:    ; ./vast.sh status
logs:      ; ./vast.sh logs
stop:      ; ./vast.sh stop
start:     ; ./vast.sh start
down:      ; ./vast.sh down

# --- diagnostics (debug_utils/<name>.sh) ---
debug:     ; ./vast.sh debug $(SCRIPT)    # e.g. make debug SCRIPT=nvenc-check

# --- Slicer dev box over MCP (.claude/skills/vast-slicer/SKILL.md) ---
slicer-up:      ; ./vast.sh slicer-up          # rent + stream + Slicer stable with MCP on :2026
tunnel:         ; ./vast.sh tunnel             # localhost:2026 -> the box
tunnel-close:   ; ./vast.sh tunnel-close
mcp-ready:      ; ./vast.sh mcp wait-ready
push-module:    ; ./vast.sh push-module $(DIR) $(NAME)   # e.g. make push-module DIR=~/src/MyExt/MyModule
slicer-restart: ; ./vast.sh slicer-restart
