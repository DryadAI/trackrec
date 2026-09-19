#!/usr/bin/env python3
"""trackrec — record browser playback into one WAV per track, named from now-playing metadata.

Deps (Arch): sudo pacman -S --needed playerctl ffmpeg libpulse
Usage: python3 trackrec.py [--player chromium|firefox] [--out ~/Music/rec] [--no-trim]
Play your playlist in the browser; Ctrl-C when done.
"""
import argparse, hashlib, os, re, signal, subprocess, sys, time
from pathlib import Path

POLL = 0.5


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def monitor_source():
    sink = sh("pactl", "get-default-sink")
    if not sink:
        sys.exit("pactl: no default sink found")
    return f"{sink}.monitor"


def pick_player(preferred):
    players = [p for p in sh("playerctl", "-l").splitlines() if p]
    if not players:
        return None
    if preferred:
        for p in players:
            if p.startswith(preferred):
                return p
        return None
    return players[0]


def now_playing(player):
    out = sh("playerctl", "-p", player, "metadata", "--format",
             "{{status}}\t{{artist}}\t{{title}}\t{{mpris:artUrl}}\t{{xesam:url}}\t{{mpris:length}}")
    p = (out.split("\t") + [""] * 6)[:6]
    return p[0], p[1].strip(), p[2].strip(), p[3], p[4], p[5]


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def track_tag(art_url, url, length_us, artist):
    """Short unique suffix. Suno UUID if a URL ever carries one; otherwise
    duration to 0.1s plus a 4-hex hash of the style prompt (Suno's `artist`)."""
    for src in (art_url, url):
        m = UUID_RE.search(src or "")
        if m:
            return m.group(0)[:8]
    parts = []
    if length_us.isdigit() and int(length_us) > 0:
        parts.append(f"{int(length_us) / 1_000_000:.1f}s")
    if artist:
        parts.append(hashlib.sha1(artist.encode()).hexdigest()[:4])
    return "-".join(parts) or time.strftime("%H%M%S")


def dump_metadata(player, path):
    """Sidecar with every MPRIS field — useful for grouping versions later."""
    try:
        path.write_text(sh("playerctl", "-p", player, "metadata") + "\n")
    except Exception:
        pass


def safe_name(s, limit=120):
    s = re.sub(r'[\\/:*?"<>|]+', "_", s).strip(" ._")
    return (s[:limit].rstrip(" ._")) or "untitled"


def unique(path):
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
        n += 1


class Recorder:
    def __init__(self, source, outdir, trim):
        self.source, self.outdir, self.trim = source, outdir, trim
        self.proc = self.raw = self.final = None

    def start(self, title, tag):
        # title only: Suno puts the whole style prompt in `artist`
        base = safe_name(f"{title} [{tag}]")
        self.final = unique(self.outdir / f"{base}.wav")
        self.raw = self.outdir / f".{self.final.stem}.raw.wav"
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "pulse", "-i", self.source,
             "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(self.raw)],
            stdin=subprocess.PIPE)
        print(f"● REC  {self.final.name}")

    def stop(self):
        if not self.proc:
            return
        try:
            self.proc.stdin.write(b"q"); self.proc.stdin.flush()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        self.proc = None
        if not self.raw.exists():
            print(f"✗ FAILED {self.final.name} (ffmpeg wrote nothing)")
            return
        if self.trim:
            # cut leading/trailing silence so the file starts and ends on the track
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(self.raw),
                 "-af", "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.1,"
                        "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.1,areverse",
                 str(self.final)])
            if r.returncode == 0:
                self.raw.unlink(missing_ok=True)
            else:
                self.raw.rename(self.final)
        else:
            self.raw.rename(self.final)
        print(f"■ SAVED {self.final.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", help="MPRIS player name prefix, e.g. chromium, firefox")
    ap.add_argument("--out", default="~/Music/rec")
    ap.add_argument("--no-trim", action="store_true")
    a = ap.parse_args()

    outdir = Path(a.out).expanduser(); outdir.mkdir(parents=True, exist_ok=True)
    source = monitor_source()
    rec = Recorder(source, outdir, not a.no_trim)
    print(f"source: {source}\nout:    {outdir}\nwaiting for player...")

    player = None
    current = None  # (artist, title)
    signal.signal(signal.SIGINT, lambda *_: (rec.stop(), sys.exit(0)))

    while True:
        if not player:
            player = pick_player(a.player)
            if player:
                print(f"player: {player}")
            else:
                time.sleep(1); continue
        status, artist, title, art, url, length = now_playing(player)
        key = (title, artist, length)
        if status == "Playing" and title and key != current:
            rec.stop()
            rec.start(title, track_tag(art, url, length, artist))
            dump_metadata(player, rec.final.with_suffix(".txt"))
            current = key
        elif status == "Stopped" and current:
            rec.stop(); current = None
        time.sleep(POLL)


if __name__ == "__main__":
    main()
