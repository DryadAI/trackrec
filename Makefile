PREFIX  ?= /usr/local
DESTDIR ?=
APP_ID  := net.dryadai.trackrec

BINDIR  := $(DESTDIR)$(PREFIX)/bin
DATADIR := $(DESTDIR)$(PREFIX)/share

.PHONY: install uninstall check

install:
	install -Dm755 trackrec.py     $(BINDIR)/trackrec
	install -Dm755 trackrec_gui.py $(BINDIR)/trackrec-gui
	install -Dm644 data/$(APP_ID).desktop $(DATADIR)/applications/$(APP_ID).desktop
	install -Dm644 data/$(APP_ID).svg     $(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg
	install -Dm644 README.md $(DATADIR)/doc/trackrec/README.md

uninstall:
	rm -f $(BINDIR)/trackrec $(BINDIR)/trackrec-gui
	rm -f $(DATADIR)/applications/$(APP_ID).desktop
	rm -f $(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg
	rm -rf $(DATADIR)/doc/trackrec

check:
	python3 -m py_compile trackrec.py trackrec_gui.py
	desktop-file-validate data/$(APP_ID).desktop
