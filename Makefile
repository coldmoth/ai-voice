.PHONY: test app install rebuild watch dmg
test:    ; .venv/bin/python -m pytest -q
app:     ; scripts/build-app.sh
install: ; scripts/install-app.sh
rebuild: ; scripts/rebuild.sh --restart
watch:   ; scripts/watch-app.sh
dmg:     ; scripts/build-app.sh --dmg
