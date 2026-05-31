# Thin wrapper over vast.sh for the dev/test loop. See vast.sh for details.
.PHONY: search up up-ghcr ls ssh sync run logs port down

search:   ; ./vast.sh search
up:       ; ./vast.sh up $(OFFER)
up-ghcr:  ; ./vast.sh up $(OFFER) ghcr
ls:       ; ./vast.sh ls
ssh:      ; ./vast.sh ssh
sync:     ; ./vast.sh sync
run:      ; ./vast.sh run
logs:     ; ./vast.sh logs
port:     ; ./vast.sh port
down:     ; ./vast.sh down
