# Thin wrapper over vast.sh for the dev/test loop. See vast.sh for details.
.PHONY: search best up up-best up-ghcr ls ssh sync provision run gltest status logs port down

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
status:    ; ./vast.sh status
logs:      ; ./vast.sh logs
port:      ; ./vast.sh port
down:      ; ./vast.sh down
