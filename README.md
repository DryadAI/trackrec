# trackrec

Record whatever your browser is playing into **one WAV file per track**,
named automatically from the player's now-playing metadata. It comes with a
command-line recorder and a GTK4 app that lets you play back, rename and
retag what you recorded.

It works with any player that publishes MPRIS metadata: Chrome/Chromium,
Firefox, Spotify, mpv and others. It was built for recording Suno playlists.

## What it does

- Watches the player via `playerctl`. When the track changes, it closes the
  current file and starts a new one.
- Records the default output device's monitor with `ffmpeg` as 16-bit /
  44.1 kHz stereo WAV.
- Trims silence from the start and end of each file (use `--no-trim` to keep it).
- Names files `Title [tag].wav`. The tag is the track length plus a short hash
  of the artist field, so different versions with the same title don't
  overwrite each other. A `.txt` file next to each WAV stores all the MPRIS
  metadata.
- The GUI shows everything in the output folder. You can click a track to play
  it, show it in your file manager, change its file name, tag and embedded
  tags (title, artist, album, genre, year, comment), or move it to the Trash.
  Tracks that recorded too quietly are shown in orange.

## Install

### Arch Linux (AUR)

```sh
yay -S trackrec        # or paru -S trackrec
```

### Any distro, from source

Install the dependencies first:

| Distro | Command |
|---|---|
| Arch | `sudo pacman -S --needed python python-gobject gtk4 playerctl ffmpeg libpulse gst-plugins-base gst-plugins-good` |
| Debian / Ubuntu | `sudo apt install python3 python3-gi gir1.2-gtk-4.0 playerctl ffmpeg pulseaudio-utils gstreamer1.0-plugins-base gstreamer1.0-plugins-good` |
| Fedora | `sudo dnf install python3 python3-gobject gtk4 playerctl ffmpeg pulseaudio-utils gstreamer1-plugins-base gstreamer1-plugins-good` |

On Fedora, `ffmpeg` comes from RPM Fusion. On all three distros, PipeWire
(with its PulseAudio compatibility layer) and PulseAudio both work. `pactl`
only needs to be able to talk to one of them.

Then install trackrec:

```sh
git clone https://github.com/DryadAI/trackrec.git
cd trackrec
make install PREFIX=~/.local      # no root needed; make sure ~/.local/bin is on PATH
# or: sudo make install           # installs to /usr/local
```

You can also skip installing and run it from the checkout with `./trackrec_gui.py`.

To remove it: `make uninstall PREFIX=~/.local` (use the same PREFIX you installed with).

## Use

**GUI:** open **trackrec** from your app launcher, or run `trackrec-gui`.

1. Pick the player (use refresh if your browser isn't listed yet) and the folder to save into.
2. Press **Start**, then play your playlist in the browser.
3. Press **Stop** when you're done. The track that's recording gets closed and saved properly.

**CLI:**

```sh
trackrec --player chromium --out ~/Music/trackrec   # Ctrl-C to stop
```

| Flag | Default | Effect |
|---|---|---|
| `--player` | first player in `playerctl -l` | Player name prefix, e.g. `chromium` or `firefox` |
| `--out` | `~/Music/rec` | Output folder (the GUI defaults to `~/Music/trackrec`) |
| `--no-trim` | off | Keep the silence at the start and end |

## Troubleshooting

| Symptom | Fix |
|---|---|
| Stuck on "waiting for player" | Run `playerctl -l` while music plays. If the browser isn't listed, turn on media keys / Media Session in the browser. |
| Recordings are very quiet (shown orange in the GUI) | Recording starts from the output device's monitor, so turn up the browser tab volume and the output volume, e.g. in `pavucontrol`. |
| Silent recordings | The browser is playing to a different device than the default. Check `pactl get-default-sink` and `pactl list short sources`. |
| Track split in the middle of a song | The site changed the title partway through playback. |
| Files ending in `(2)`, `(3)` | The same title and tag were recorded again. trackrec never overwrites a file. |

## Notes

- It records **everything** playing on the default output device, so notification sounds get recorded too.
- Only record content you have the rights to. When a site offers a download
  (Suno does), downloading is cleaner.

## License

MIT, see [LICENSE](LICENSE).
