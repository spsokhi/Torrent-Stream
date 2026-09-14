# Torrent Stream

A BitTorrent client that streams video while it downloads — and shows you exactly
what the protocol is doing while it does it.

Paste a magnet link, press Play, and the video starts within seconds instead of
after the download finishes. That part is useful. The interesting part is the
instrumentation around it: a live piece map, a peer table where choking is drawn
rather than described, a protocol event log, a bencode inspector, and an A/B
switch that measures the cost of streaming versus classic torrenting on the same
torrent.

Flask and libtorrent. No frontend framework, no build step, no CDN.

<!-- Screenshot goes here. The piece strip mid-download with the amber fetch
     window sitting just ahead of the playhead is the shot worth taking. -->

---

## Run it

```bash
pip install -r requirements.txt
python app.py
```

A browser opens at `http://127.0.0.1:8080`. Paste this well-seeded, entirely legal
test torrent:

```
magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10
```

That's [Sintel](https://durian.blender.org/), a Blender Foundation open movie. It
ships nine `.srt` subtitle files, which makes it a good exercise for most of the
panels. More legal torrents at [webtorrent.io/free-torrents](https://webtorrent.io/free-torrents)
and [archive.org](https://archive.org).

**Optional:** put `ffmpeg` on PATH to enable in-browser conversion of formats
Chrome and Firefox refuse. On Windows: `winget install Gyan.FFmpeg`, then open a
**new terminal** — the app reads PATH once at startup.

Everything runs on your machine. Nothing is uploaded anywhere except to the swarm,
which is what a torrent client does.

---

## The experiment worth running first

The mode switch above the piece strip is the reason this project exists.

| | streaming | rarest-first |
|---|---|---|
| piece priority | head and tail pinned to 7, rolling 32-piece window at the playhead | uniform 4 across the whole file |
| deadlines | `set_piece_deadline()` rising across the window | none |
| `sequential_download` | on | off |
| piece choice | wherever the playhead is | whatever is scarcest in the swarm |

**rarest-first is not a strawman.** It is what every normal torrent client does,
and it is the right algorithm: grabbing the scarcest piece first is what keeps a
swarm alive, because rare pieces get replicated before the only seed holding them
disappears. It is simply catastrophic for playback, and the point is to make that
trade legible rather than to declare a winner.

To measure it honestly:

1. Add the torrent in **streaming** mode, let it play for a minute.
2. Press **Re-run cold in rarest-first**. This deletes the downloaded data and
   re-adds the same torrent, because time-to-first-byte measured on a
   half-downloaded torrent is meaningless.
3. Wait. It will take considerably longer to produce a first frame.

The comparison table fills in both columns:

```
                                  streaming      rarest-first
time to first playable byte          6.1 s          …
  metadata resolve                   4.1 s          …
  piece fetch                        2.0 s          …
  blocked waiting on pieces          0.0 s          …
bytes fetched before playback       1.5 MB          …
stalls over 500 ms                       0          …
longest single stall                 0.0 s          …
total time stalled                   0.0 s          …
peers at first byte                     15          …
reads that gave up                       0          …
```

Those streaming figures are a real run on a home connection — your numbers will
differ, and the rarest-first column is left blank deliberately. Run it yourself;
the gap is the whole lesson.

The row that matters most is **bytes fetched before playback**. Streaming needs
roughly a megabyte. Rarest-first has to fetch a large fraction of the entire file
before piece 0 happens to turn up, because nothing tells it that piece 0 is special.

Two definitions shape the numbers, and both are choices:

- Blocking **before** the first byte is startup latency. Blocking **after** it is a
  stall — the player had frames and then ran out, which is what a viewer feels.
- A run counts as **cold** only if the download directory was empty. Switching
  mode mid-torrent starts a new run marked *warm*, the winner highlighting turns
  off, and the table says why.

---

## What the panels show

### The piece strip

Every cell is a bucket of pieces, downsampled to 200 so it works at any torrent
size. Teal means downloaded. The amber outline is the active fetch window; the
amber bars at either end are the pinned head and tail.

The tail is pinned because MP4 files usually store their `moov` atom — the index
the player needs before it can decode anything — at the *end* of the file. Without
tail pinning, an MP4 will not start playing until the download is nearly complete.

Switch to rarest-first and the pins and window disappear, because in that mode
they do not exist.

### Swarm

`get_peer_info()` as a live table, on its own poll that stops entirely when the
panel is collapsed.

Choke state is **drawn, not spelled**. Two bars per peer, down and up:

- **teal filled** — bytes can move
- **amber outline** — one side wants, the other is refusing. That is a choke.
- **amber filled** on the up bar — an *optimistic unchoke*: a free slot BitTorrent
  hands out at random so a peer with nothing to trade can still get started
- **grey** — neither side is asking

The `needs 12 kB/s` tag is libtorrent's own `estimated_reciprocation_rate`: what it
thinks you would have to upload to buy an unchoke from that peer. Tit-for-tat with
a number on it.

**Tit-for-tat only shows up between leechers.** A seed has everything, wants
nothing back, and unchokes whoever it likes. On a well-seeded torrent you will sit
at `0 we unchoked` and still download fine. To see reciprocity actually bite, find
a torrent with few seeds and many leechers, cap your upload to **off**, and watch
the down bars fall back to outlines.

### Bandwidth caps

Session-wide limits next to the mode switch. The lowest setting is labelled **off**
and sends 1 byte/sec, because libtorrent reads a rate limit of `0` as *unlimited* —
there is no way to say "stop".

### Protocol events

`pop_alerts()` drained on a background thread into a ring buffer, cursor-polled so
each request ships only what is new.

Colour follows the same vocabulary as the piece strip: teal for data that landed,
amber for swarm discovery in flight, grey for peer churn, white for milestones, red
for trouble.

**`hash_failed` is loud** — a standing counter that does not scroll away. It means a
peer sent bytes that did not match the SHA-1 in the torrent, so libtorrent threw
the whole piece away and put that peer on parole. It is the integrity guarantee
doing its job, and you rarely get to watch it happen.

Filter to *pieces landing* while in **streaming** mode and you will see pieces
complete out of order — `30`, `14`, `53`, `54`, `103`. That is the 32-piece deadline
window having several requests in flight at once, and it is the clearest evidence
that "sequential" is a bias, not a guarantee.

### Torrent metadata

The bencoded dictionary as a collapsible tree, and above it the derivation that
makes BitTorrent work:

```
info dictionary        20,242 bytes, exactly as received
SHA-1 of those bytes   08ada5a7a6183aae1e09d831df6748d566095a10
infohash in use        08ada5a7a6183aae1e09d831df6748d566095a10
```

The infohash is not stored anywhere in the torrent. It is recomputed from the info
dictionary every time, which is why changing one byte in there produces a different
torrent that no peer in the swarm will discuss with you.

This decodes **only bytes that actually travelled**. Regenerating the torrent
through `create_torrent()` would be easier, but it invents an outer dictionary and
stamps *today* as the creation date. So if you arrived via a magnet link, you get
the info dictionary and nothing else — announce URLs, creation date, comment and
created-by live in the outer dictionary of a `.torrent` file and were never
transferred. That is a real property of magnet links, not a limitation of the
viewer.

### Player

`−10s` / `+10s`, speed, volume, PiP, fullscreen, buffering indicator, and keyboard:

| key | action | key | action |
|---|---|---|---|
| `space` `k` | play / pause | `m` | mute |
| `←` `→` | ∓5s | `f` | fullscreen |
| `j` `l` | ∓10s | `p` | picture-in-picture |
| `shift`+`←` `→` | ∓30s | `<` `>` | speed |
| `,` `.` | frame step | `0`–`9` | seek to percentage |

Volume, speed and subtitle choice persist. Playback position persists too, but
resuming is a **button**, not an automatic seek — auto-seeking would yank the piece
window deep into the file and wreck the measurement you just took.

Sidecar subtitle files (`.srt`, `.vtt`) are fetched at priority 7 and converted to
WebVTT, since that is the only thing a `<track>` accepts. On Sintel all nine
languages land about two seconds after playback starts.

---

## "It plays but there is no sound"

Almost always a codec the browser refuses, and the app now says so precisely
instead of shrugging:

```
matroska container
video       H264    browsers decode this
audio       EAC3    no browser decodes this
subtitle    SRT     embedded — needs extracting, not a <track> file
```

Common culprits: **E-AC-3 / AC-3 / DTS** audio (very common in `WEB-DL` and
`BluRay` releases — look for `DDP` or `DD5.1` in the filename) and **HEVC** video.
No browser decodes any of them.

`probe.py` reads this out of the container directly — Matroska is EBML, MP4 is a box
tree, both walkable in pure Python — so the diagnosis works with **no ffmpeg
installed**, which is exactly when you need it.

Your options:

- **Open the stream URL in VLC or mpv.** They play everything. For MKV releases
  this is genuinely the right answer, not a workaround — and embedded subtitles
  work there too.
- **Install ffmpeg and press Convert for browser.** When the video is already H.264
  it is copied through untouched and only the audio is re-encoded, so conversion
  keeps up with playback instead of taking hours.

Subtitles *embedded inside* an MKV are not sidecar files, so the subtitle picker
cannot see them. Extraction needs ffmpeg and needs the download to finish first,
because Matroska interleaves subtitle cues through the entire file.

---

## Architecture

```
app.py        Flask wiring and entry point
routes.py     the HTTP surface, and nothing else
streamer.py   the libtorrent engine: piece strategies, instrumentation, alerts
probe.py      dependency-free container reader (EBML and MP4 box tree)
static/       index.html, app.css, app.js — no build step, edit and reload
```

Anything that touches libtorrent lives in `streamer.py`. `routes.py` only
publishes what the engine exposes.

| endpoint | purpose |
|---|---|
| `GET /api/status` | 1 Hz telemetry. Deliberately cheap — new data gets new endpoints |
| `GET /api/peers` | peer table, own poll, stops when the panel is closed |
| `GET /api/events` | protocol alerts, cursor-based via `?since=` |
| `GET /api/media` | container and codec probe |
| `GET /api/metadata` | bencode tree and infohash derivation |
| `GET /api/subs` | sidecar subtitle files |
| `POST /api/mode` | `streaming` or `rarest` |
| `POST /api/limits` | session-wide rate caps, bytes/sec |
| `POST /api/rerun` | wipe data and re-add in the given mode |
| `GET /stream` | the video, Range-aware |
| `GET /subs/<n>` | a subtitle track as WebVTT |
| `GET /transcode` | ffmpeg remux, when available |

Three rules the code follows:

- `/api/status` stays cheap. `get_peer_info()` walks every open connection and
  `pop_alerts()` is chatty, so both get their own endpoint and their own interval.
- `pop_alerts()` is drained by exactly one thread. Anything left undrained
  accumulates inside libtorrent.
- Both piece strategies rewrite the **entire** priority map, so anything that needs
  pinning — head, tail, subtitle files — has to be re-pinned inside both, or
  `prioritize_files()` gets silently undone.

---

## Notes

Piece priorities in libtorrent: `0` skips, `1` is "eventually", `4` is the default
a normal client uses everywhere, `7` jumps the queue. The streaming strategy is
essentially *spend 7s sparingly and in the right place*.

Deadlines set by `set_piece_deadline()` are relative to now and rise across the
window, so the piece the player needs next outranks the one after it.

Tested against libtorrent 2.1.1 and Python 3.10.

## Legal

A torrent client is a tool. Use it on content you have the right to — the Blender
open movies, Creative Commons releases, Linux ISOs, and the Internet Archive are
all well seeded and entirely legal. What you do beyond that is on you.

No license file yet. Add one before sharing if you want people to reuse the code.
