# Thin wrapper over vast.sh for the dev/test loop. See vast.sh for details.
.PHONY: search best up up-best up-ghcr ls ssh sync provision run gltest egltest wl-build wl-check wl-fixwayland wltest inspect status logs port stop start down

search:    ; ./vast.sh search
best:      ; ./vast.sh best
up:        ; ./vast.sh up $(OFFER)
up-best:   ; ./vast.sh up-best
up-ghcr:   ; ./vast.sh up $(OFFER) ghcr
ls:        ; ./vast.sh ls
ssh:       ; ./vast.sh ssh
sync:      ; ./vast.sh sync
provision: ; ./vast.sh provision
run:       ; ./vast.sh run
gltest:    ; ./vast.sh gltest
egltest:   ; ./vast.sh egltest
wl-build:  ; ./vast.sh wl-build
wl-check:  ; ./vast.sh wl-check
wl-fixwayland: ; ./vast.sh wl-fixwayland
wltest:    ; ./vast.sh wltest
inspect:   ; ./vast.sh inspect
status:    ; ./vast.sh status
logs:      ; ./vast.sh logs
port:      ; ./vast.sh port
stop:      ; ./vast.sh stop
start:     ; ./vast.sh start
down:      ; ./vast.sh down
