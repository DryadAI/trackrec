#!/usr/bin/env python3
"""trackrec-gui — GTK4 front end for trackrec.py.

Runs trackrec.py unchanged as a subprocess and reads its stdout. Shows every
WAV in the output folder as a library: click to play in-app, show in folder,
rename / retag, move to trash. Stop sends SIGINT, same as Ctrl-C, so the last
track is closed cleanly. Settings persist in $XDG_CONFIG_HOME/trackrec/gui.json.
"""
import json, queue, re, shutil, signal, subprocess, sys, threading, time
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

APP_ID = "net.dryadai.trackrec"
HERE = Path(__file__).resolve().parent
CONFIG = Path(GLib.get_user_config_dir()) / "trackrec" / "gui.json"
MUSIC = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC) or str(Path.home() / "Music")
DEFAULTS = {"out": str(Path(MUSIC) / "trackrec"), "trim": True}
REQUIRED = ("playerctl", "ffmpeg", "ffprobe", "pactl")


def find_recorder():
    """The CLI: `trackrec` installed next to us, a source checkout's trackrec.py, or PATH."""
    for p in (HERE / "trackrec", HERE / "trackrec.py"):
        if p.is_file():
            return p
    found = shutil.which("trackrec")
    return Path(found) if found else None


SCRIPT = find_recorder()
QUIET_PEAK_DB = -12.0  # warn when a track peaks below this

# "Title [tag].wav" or "Title [tag] (2).wav"; the tag part is optional
NAME_RE = re.compile(r"^(?P<title>.*?)(?: \[(?P<tag>[^\]]*)\])?(?P<dup> \(\d+\))?$")
# embedded RIFF INFO fields ffmpeg can write to WAV, in dialog order
META_FIELDS = [("title", "Title"), ("artist", "Artist"), ("album", "Album"),
               ("genre", "Genre"), ("date", "Year"), ("comment", "Comment")]

CSS = """
.state   { font-size: 1.15em; font-weight: bold; }
.rec     { color: #e5484d; }
.warn    { color: #d9822b; }
.dim     { opacity: 0.65; }
.mono    { font-family: monospace; }
.title   { font-weight: bold; }
.playing { background-color: alpha(@accent_bg_color, 0.18); }
"""


# --- small utilities ---------------------------------------------------------

def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def list_players():
    return [p for p in sh("playerctl", "-l").splitlines() if p]


def load_config():
    try:
        return {**DEFAULTS, **json.loads(CONFIG.read_text())}
    except Exception:
        return dict(DEFAULTS)


def save_config(cfg):
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    except OSError:
        pass


def split_name(stem):
    m = NAME_RE.match(stem)
    return m.group("title"), m.group("tag") or "", m.group("dup") or ""


def clean(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip(" ._")


def probe(path):
    """(duration_s, peak_db) — either may be None."""
    dur = sh("ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path))
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                        "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
    try:
        dur = float(dur)
    except ValueError:
        dur = None
    return dur, float(m.group(1)) if m else None


def read_tags(path):
    out = sh("ffprobe", "-v", "error", "-show_entries", "format_tags",
             "-of", "json", str(path))
    try:
        tags = json.loads(out).get("format", {}).get("tags", {})
    except ValueError:
        tags = {}
    tags = {k.lower(): v for k, v in tags.items()}
    return {k: tags.get(k, "") for k, _ in META_FIELDS}


def write_tags(path, tags):
    """Rewrite embedded tags without re-encoding (stream copy)."""
    tmp = path.with_name(f".{path.stem}.retag.wav")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
           "-map", "0", "-map_metadata", "-1", "-c", "copy"]
    for k, v in tags.items():
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    r = subprocess.run(cmd + [str(tmp)], capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(r.stderr.strip() or "ffmpeg failed")
    tmp.replace(path)


def fmt_time(s):
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


# --- library row --------------------------------------------------------------

class TrackRow(Gtk.ListBoxRow):
    def __init__(self, win, path):
        super().__init__()
        self.path = path
        title, tag, dup = split_name(path.stem)

        box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=10, margin_end=6)
        self.set_child(box)

        self.icon = Gtk.Image(icon_name="media-playback-start-symbolic")
        self.icon.add_css_class("dim")
        box.append(self.icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        t = Gtk.Label(label=title + dup, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        t.add_css_class("title")
        text.append(t)
        sub = Gtk.Label(label=f"[{tag}]" if tag else path.name, xalign=0,
                        ellipsize=Pango.EllipsizeMode.END)
        sub.add_css_class("dim")
        text.append(sub)
        box.append(text)
        self.set_tooltip_text(path.name)

        self.info = Gtk.Label(label="…", xalign=1)
        self.info.add_css_class("dim")
        self.info.add_css_class("mono")
        box.append(self.info)

        # per-row actions
        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        pop = Gtk.Popover(child=pop_box)
        for label, fn in (("Play", win.play),
                          ("Show in folder", win.show_in_folder),
                          ("Rename / edit tags…", win.edit),
                          ("Move to Trash…", win.trash)):
            b = Gtk.Button(label=label, has_frame=False)
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, f=fn: (pop.popdown(), f(self.path)))
            pop_box.append(b)
        menu = Gtk.MenuButton(icon_name="view-more-symbolic", popover=pop,
                              valign=Gtk.Align.CENTER, has_frame=False)
        box.append(menu)

    def set_info(self, dur, peak):
        parts = [fmt_time(dur) if dur is not None else "?:??"]
        if peak is not None:
            parts.append(f"{peak:+5.1f} dB")
        self.info.set_label("  ".join(parts))
        self.info.remove_css_class("dim")
        if peak is not None and peak < QUIET_PEAK_DB:
            self.info.add_css_class("warn")
            self.info.set_tooltip_text(
                "Very quiet — check the browser tab volume and the output device volume (e.g. in pavucontrol).")

    def set_playing(self, on):
        self.icon.set_from_icon_name("audio-volume-high-symbolic" if on
                                     else "media-playback-start-symbolic")
        (self.add_css_class if on else self.remove_css_class)("playing")


# --- edit dialog --------------------------------------------------------------

class EditDialog(Gtk.Window):
    def __init__(self, parent, path, on_save):
        super().__init__(transient_for=parent, modal=True, title="Edit track",
                         default_width=480, resizable=False)
        self.path, self.on_save = path, on_save
        title, tag, self.dup = split_name(path.stem)
        self.orig_tags = read_tags(path)

        hb = Gtk.HeaderBar(show_title_buttons=False)
        self.set_titlebar(hb)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        hb.pack_start(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self.on_save_clicked)
        hb.pack_end(save)
        self.set_default_widget(save)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin_top=14,
                        margin_bottom=14, margin_start=14, margin_end=14)
        self.set_child(grid)
        row = 0

        def section(text):
            nonlocal row
            lbl = Gtk.Label(label=text, xalign=0, margin_top=4 if row else 0)
            lbl.add_css_class("dim")
            grid.attach(lbl, 0, row, 2, 1)
            row += 1

        def field(label, value):
            nonlocal row
            grid.attach(Gtk.Label(label=label, xalign=1), 0, row, 1, 1)
            e = Gtk.Entry(text=value, hexpand=True, activates_default=True)
            grid.attach(e, 1, row, 1, 1)
            row += 1
            return e

        section("File name")
        self.name_e = field("Name", title + self.dup)
        self.tag_e = field("Tag", tag)
        self.preview = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, selectable=True)
        self.preview.add_css_class("dim")
        self.preview.add_css_class("mono")
        grid.attach(self.preview, 1, row, 1, 1)
        row += 1
        self.name_e.connect("changed", self.update_preview)
        self.tag_e.connect("changed", self.update_preview)

        section("Embedded tags (shown by music players)")
        self.meta_e = {}
        for key, label in META_FIELDS:
            val = self.orig_tags[key] or (title if key == "title" else "")
            self.meta_e[key] = field(label, val)

        self.err = Gtk.Label(xalign=0, wrap=True)
        self.err.add_css_class("rec")
        grid.attach(self.err, 0, row, 2, 1)
        self.update_preview()

    def new_name(self):
        title = clean(self.name_e.get_text())
        tag = clean(self.tag_e.get_text()).replace("[", "").replace("]", "")
        if not title:
            return None
        return f"{title} [{tag}].wav" if tag else f"{title}.wav"

    def update_preview(self, *_):
        n = self.new_name()
        self.preview.set_label(f"→ {n}" if n else "→ (name required)")

    def on_save_clicked(self, *_):
        name = self.new_name()
        if not name:
            self.err.set_label("Name can't be empty.")
            return
        tags = {k: e.get_text().strip() for k, e in self.meta_e.items()}
        err = self.on_save(self.path, name, tags if tags != self.orig_tags else None)
        if err:
            self.err.set_label(err)
        else:
            self.close()


# --- main window --------------------------------------------------------------

class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="trackrec", default_width=720, default_height=680)
        self.cfg = load_config()
        self.proc = None
        self.rec_started = None
        self.rows = {}           # Path -> TrackRow
        self.probe_cache = {}    # Path -> (mtime, dur, peak)
        self.lib_sig = None
        self.monitor = None
        self.reload_src = 0
        self.playing = None      # Path
        self.probe_q = queue.Queue()
        threading.Thread(target=self.probe_worker, daemon=True).start()

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        self.start_btn = Gtk.Button(label="Start")
        self.start_btn.add_css_class("suggested-action")
        self.start_btn.connect("clicked", self.on_start_stop)
        header.pack_start(self.start_btn)
        open_btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Open output folder")
        open_btn.connect("clicked", lambda *_: self.open_dir(self.outdir()))
        header.pack_end(open_btn)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.set_child(root)

        # settings
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        root.append(grid)

        grid.attach(Gtk.Label(label="Player", xalign=0), 0, 0, 1, 1)
        self.player_model = Gtk.StringList()
        self.player_dd = Gtk.DropDown(model=self.player_model, hexpand=True)
        grid.attach(self.player_dd, 1, 0, 1, 1)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan MPRIS players")
        refresh.connect("clicked", lambda *_: self.refresh_players())
        grid.attach(refresh, 2, 0, 1, 1)

        grid.attach(Gtk.Label(label="Save to", xalign=0), 0, 1, 1, 1)
        self.out_entry = Gtk.Entry(text=self.cfg["out"], hexpand=True,
                                   tooltip_text="Default folder — remembered between runs. Press Enter to apply.")
        self.out_entry.connect("activate", lambda *_: self.set_outdir(self.out_entry.get_text()))
        grid.attach(self.out_entry, 1, 1, 1, 1)
        browse = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Choose folder")
        browse.connect("clicked", self.on_browse)
        grid.attach(browse, 2, 1, 1, 1)

        grid.attach(Gtk.Label(label="Trim silence", xalign=0), 0, 2, 1, 1)
        self.trim_sw = Gtk.Switch(active=self.cfg["trim"], halign=Gtk.Align.START)
        self.trim_sw.connect("notify::active", lambda *_: self.persist())
        grid.attach(self.trim_sw, 1, 2, 1, 1)
        self.settings = [self.player_dd, refresh, self.out_entry, browse, self.trim_sw]

        # recorder state
        state_box = Gtk.Box(spacing=10)
        self.state_lbl = Gtk.Label(label="Idle", xalign=0, hexpand=True,
                                   ellipsize=Pango.EllipsizeMode.END)
        self.state_lbl.add_css_class("state")
        self.timer_lbl = Gtk.Label(label="", xalign=1)
        self.timer_lbl.add_css_class("mono")
        state_box.append(self.state_lbl)
        state_box.append(self.timer_lbl)
        root.append(state_box)

        # library
        lib_head = Gtk.Box(spacing=8)
        self.lib_lbl = Gtk.Label(label="Recordings", xalign=0, hexpand=True)
        self.lib_lbl.add_css_class("dim")
        lib_head.append(self.lib_lbl)
        lib_head.append(Gtk.Label(label="click to play", css_classes=["dim"]))
        root.append(lib_head)

        self.tracks = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                  activate_on_single_click=True)
        self.tracks.add_css_class("boxed-list")
        self.tracks.set_placeholder(Gtk.Label(label="No recordings in this folder yet",
                                              css_classes=["dim"], margin_top=16, margin_bottom=16))
        self.tracks.connect("row-activated", lambda _lb, row: self.play(row.path))
        sc = Gtk.ScrolledWindow(vexpand=True, child=self.tracks)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(sc)

        # player bar
        pbar = Gtk.Box(spacing=8)
        self.now_lbl = Gtk.Label(label="Nothing playing", xalign=0, width_chars=22,
                                 max_width_chars=28, ellipsize=Pango.EllipsizeMode.END)
        self.now_lbl.add_css_class("dim")
        pbar.append(self.now_lbl)
        self.controls = Gtk.MediaControls(hexpand=True)
        pbar.append(self.controls)
        root.append(pbar)

        # log
        self.log_buf = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buf, editable=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR, cursor_visible=False)
        log_sc = Gtk.ScrolledWindow(child=self.log_view, min_content_height=110)
        root.append(Gtk.Expander(label="Recorder log", child=log_sc))

        self.refresh_players()
        self.set_outdir(self.cfg["out"])
        GLib.timeout_add(500, self.tick)
        self.connect("close-request", self.on_close)
        self.check_deps()

    def check_deps(self):
        missing = [c for c in REQUIRED if not shutil.which(c)]
        if SCRIPT is None:
            missing.append("trackrec (the recorder script)")
        if missing:
            self.start_btn.set_sensitive(False)
            self.set_state("Missing: " + ", ".join(missing) + " — see README", recording=True)
            self.log("cannot record, not found on PATH: " + ", ".join(missing))

    # --- settings ------------------------------------------------------------
    def outdir(self):
        return Path(self.cfg["out"]).expanduser()

    def persist(self):
        self.cfg["trim"] = self.trim_sw.get_active()
        save_config(self.cfg)

    def set_outdir(self, text):
        d = Path(text).expanduser()
        self.cfg["out"] = str(d)
        self.out_entry.set_text(str(d))
        self.persist()
        if self.monitor:
            self.monitor.cancel()
            self.monitor = None
        self.lib_sig = None
        if d.is_dir():
            self.monitor = Gio.File.new_for_path(str(d)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.monitor.connect("changed", self.on_dir_changed)
        self.reload_library()

    def on_browse(self, *_):
        dlg = Gtk.FileDialog(title="Default save folder")
        if self.outdir().is_dir():
            dlg.set_initial_folder(Gio.File.new_for_path(str(self.outdir())))
        dlg.select_folder(self, None, self.on_folder_chosen)

    def on_folder_chosen(self, dlg, res):
        try:
            f = dlg.select_folder_finish(res)
        except GLib.Error:
            return
        if f:
            self.set_outdir(f.get_path())

    def refresh_players(self):
        players = list_players()
        self.player_model.splice(0, self.player_model.get_n_items(), ["(any)"] + players)
        for i, p in enumerate(players, start=1):
            if p.startswith("chromium"):
                self.player_dd.set_selected(i)
                return
        self.player_dd.set_selected(1 if players else 0)

    def selected_prefix(self):
        item = self.player_dd.get_selected_item()
        name = item.get_string() if item else "(any)"
        # chromium.instance411768 -> chromium; instance ids change across browser restarts
        return None if name == "(any)" else name.split(".")[0]

    # --- library -------------------------------------------------------------
    def on_dir_changed(self, _mon, f, other, _event):
        names = [x.get_basename() for x in (f, other) if x]
        if names and all(n.startswith(".") for n in names):
            return  # in-progress .raw / .retag temp files
        if self.reload_src:
            GLib.source_remove(self.reload_src)
        self.reload_src = GLib.timeout_add(400, self.reload_library)

    def reload_library(self):
        self.reload_src = 0
        d = self.outdir()
        files = []
        if d.is_dir():
            for p in d.glob("*.wav"):
                try:
                    files.append((p.stat().st_mtime, p))
                except OSError:
                    pass
        files.sort(reverse=True)
        sig = [(p, m) for m, p in files]
        if sig == self.lib_sig:
            return False
        self.lib_sig = sig

        self.tracks.remove_all()
        self.rows = {}
        for mtime, p in files:
            row = TrackRow(self, p)
            self.rows[p] = row
            self.tracks.append(row)
            cached = self.probe_cache.get(p)
            if cached and cached[0] == mtime:
                row.set_info(cached[1], cached[2])
            else:
                self.probe_q.put((p, mtime))
            row.set_playing(p == self.playing)
        n = len(files)
        self.lib_lbl.set_label(f"Recordings — {n} file{'s' if n != 1 else ''} in {d.name}/")
        return False

    def probe_worker(self):
        while True:
            p, mtime = self.probe_q.get()
            if p.exists():
                dur, peak = probe(p)
                GLib.idle_add(self.on_probed, p, mtime, dur, peak)

    def on_probed(self, p, mtime, dur, peak):
        self.probe_cache[p] = (mtime, dur, peak)
        row = self.rows.get(p)
        if row:
            row.set_info(dur, peak)
        return False

    # --- row actions ---------------------------------------------------------
    def play(self, path):
        if self.playing == path and self.controls.get_media_stream():
            s = self.controls.get_media_stream()
            s.pause() if s.get_playing() else s.play()
            return
        self.stop_playback()
        media = Gtk.MediaFile.new_for_filename(str(path))
        self.controls.set_media_stream(media)
        media.play()
        self.playing = path
        self.now_lbl.set_label(split_name(path.stem)[0])
        self.now_lbl.set_tooltip_text(path.name)
        self.now_lbl.remove_css_class("dim")
        for p, row in self.rows.items():
            row.set_playing(p == path)

    def stop_playback(self):
        s = self.controls.get_media_stream()
        if s:
            s.pause()
            if isinstance(s, Gtk.MediaFile):
                s.clear()
        self.controls.set_media_stream(None)
        if self.playing in self.rows:
            self.rows[self.playing].set_playing(False)
        self.playing = None
        self.now_lbl.set_label("Nothing playing")
        self.now_lbl.add_css_class("dim")

    def show_in_folder(self, path):
        uri = Gio.File.new_for_path(str(path)).get_uri()
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        def done(conn, res):
            try:
                conn.call_finish(res)
            except GLib.Error:
                self.open_dir(path.parent)

        bus.call("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                 "org.freedesktop.FileManager1", "ShowItems",
                 GLib.Variant("(ass)", ([uri], "")), None, Gio.DBusCallFlags.NONE,
                 5000, None, done)

    def open_dir(self, d):
        if d.is_dir():
            subprocess.Popen(["xdg-open", str(d)])

    def edit(self, path):
        EditDialog(self, path, self.apply_edit).present()

    def apply_edit(self, path, new_name, tags):
        """Returns an error string, or None on success."""
        if not path.exists():
            return "The file no longer exists."
        new_path = path.with_name(new_name)
        if new_path != path and new_path.exists():
            return f"“{new_name}” already exists in this folder."
        was_playing = self.playing == path
        if was_playing:
            self.stop_playback()
        try:
            if tags is not None:
                write_tags(path, tags)
            if new_path != path:
                path.rename(new_path)
                side, new_side = path.with_suffix(".txt"), new_path.with_suffix(".txt")
                if side.exists() and not new_side.exists():
                    side.rename(new_side)
        except Exception as e:
            return f"Couldn't save: {e}"
        self.log(f"edited: {path.name} → {new_path.name}" + (" (tags updated)" if tags else ""))
        self.reload_library()
        return None

    def trash(self, path):
        dlg = Gtk.AlertDialog(message=f"Move “{path.name}” to Trash?",
                              detail="Its .txt sidecar goes too. You can restore both from the Trash.",
                              buttons=["Cancel", "Move to Trash"], cancel_button=0, default_button=0)

        def chosen(d, res):
            try:
                if d.choose_finish(res) != 1:
                    return
            except GLib.Error:
                return
            if self.playing == path:
                self.stop_playback()
            try:
                for p in (path, path.with_suffix(".txt")):
                    if p.exists():
                        Gio.File.new_for_path(str(p)).trash(None)
                self.log(f"trashed: {path.name}")
            except GLib.Error as e:
                Gtk.AlertDialog(message="Couldn't move to Trash", detail=e.message).show(self)
            self.reload_library()

        dlg.choose(self, None, chosen)

    # --- recorder ------------------------------------------------------------
    def log(self, line):
        self.log_buf.insert(self.log_buf.get_end_iter(), line + "\n")
        self.log_view.scroll_to_iter(self.log_buf.get_end_iter(), 0, False, 0, 0)

    def set_state(self, text, recording=False):
        self.state_lbl.set_label(text)
        (self.state_lbl.add_css_class if recording else self.state_lbl.remove_css_class)("rec")

    def tick(self):
        self.timer_lbl.set_label(fmt_time(time.monotonic() - self.rec_started)
                                 if self.rec_started else "")
        return True

    def on_start_stop(self, *_):
        self.stop() if self.proc else self.start()

    def start(self):
        self.set_outdir(self.out_entry.get_text())
        cmd = [sys.executable, "-u", str(SCRIPT), "--out", str(self.outdir())]
        prefix = self.selected_prefix()
        if prefix:
            cmd += ["--player", prefix]
        if not self.trim_sw.get_active():
            cmd.append("--no-trim")
        self.log("$ " + " ".join(cmd))
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1)
        except OSError as e:
            self.log(f"failed to start: {e}")
            return
        for w in self.settings:
            w.set_sensitive(False)
        self.start_btn.set_label("Stop")
        self.start_btn.remove_css_class("suggested-action")
        self.start_btn.add_css_class("destructive-action")
        self.set_state("Starting…")
        threading.Thread(target=self.reader, args=(self.proc,), daemon=True).start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.set_state("Stopping — closing last track…")
            self.start_btn.set_sensitive(False)
            self.proc.send_signal(signal.SIGINT)

    def reader(self, proc):
        for line in proc.stdout:
            GLib.idle_add(self.on_line, line.rstrip("\n"))
        GLib.idle_add(self.on_exit, proc.wait())

    def on_line(self, line):
        self.log(line)
        if line.startswith("waiting for player"):
            self.set_state("Waiting for player — press play in the browser")
        elif line.startswith("player: "):
            self.set_state(f"Connected: {line[8:]} — waiting for playback")
        elif line.startswith("● REC"):
            name = line.split("REC", 1)[1].strip()
            self.set_state(f"● {split_name(Path(name).stem)[0]}", recording=True)
            self.rec_started = time.monotonic()
        elif line.startswith("■ SAVED"):
            self.rec_started = None
            self.set_state("Waiting for next track")
            self.reload_library()
        elif line.startswith("✗ FAILED"):
            self.rec_started = None
            self.set_state("Last track failed — see log")
        return False

    def on_exit(self, rc):
        self.proc = None
        self.rec_started = None
        self.log(f"[trackrec exited, code {rc}]")
        self.set_state("Idle" if rc == 0 else f"Stopped with error (code {rc}) — see log")
        for w in self.settings:
            w.set_sensitive(True)
        self.start_btn.set_sensitive(True)
        self.start_btn.set_label("Start")
        self.start_btn.remove_css_class("destructive-action")
        self.start_btn.add_css_class("suggested-action")
        self.reload_library()
        return False

    def on_close(self, *_):
        self.stop_playback()
        if self.proc and self.proc.poll() is None:
            # close the in-progress track properly before quitting
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            css = Gtk.CssProvider()
            css.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            win = Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
