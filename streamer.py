"""
streamer.py - the torrent engine.

One libtorrent session, one torrent, one selected file, exposed as a seekable
byte stream. Everything that talks to libtorrent lives here; routes.py only
reads what this module publishes.

The piece-strategy switch is the point of the project: "streaming" biases the
swarm towards the playhead, "rarest-first" is what a normal torrent client does.
Both are instrumented, so the cost of each is a number rather than a feeling.
"""

import collections
import hashlib
import os
import re
import subprocess
import shutil
import threading
import time

import probe

try:
    import libtorrent as lt
except ImportError:
    raise SystemExit("libtorrent missing.  Run:  pip install libtorrent")


PORT = 8080
SAVE_PATH = os.path.abspath("./downloads")
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".mpg", ".mpeg"}
SUB_EXT = {".srt", ".vtt", ".ass", ".ssa", ".sub"}
BROWSER_SAFE = {".mp4", ".m4v", ".webm"}

HEAD_PIECES = 8
TAIL_PIECES = 5
WINDOW_PIECES = 32
DEADLINE_STEP_MS = 600

# libtorrent piece priorities: 0 skips, 1 is "eventually", 4 is the default a
# normal client uses everywhere, 7 jumps the queue. The streaming strategy is
# essentially "spend 7s sparingly and in the right place".
PRIO_SKIP, PRIO_LOW, PRIO_NORMAL, PRIO_TOP = 0, 1, 4, 7

MODES = ("streaming", "rarest")
STALL_SECONDS = 0.5        # a read that blocks longer than this counts as a stall
PIECE_TIMEOUT = 180        # give up on a piece after this long and end the response

HAS_FFMPEG = shutil.which("ffmpeg") is not None
HAS_FFPROBE = shutil.which("ffprobe") is not None


def _delete_files_flag():
    """The remove_torrent flag moved between libtorrent releases."""
    for holder in (getattr(lt, "options_t", None), getattr(lt, "session", None)):
        flag = getattr(holder, "delete_files", None)
        if flag is not None:
            return flag
    return None


DELETE_FILES = _delete_files_flag()


# peer_info.flags bits we surface, and what each one means for data flow.
# libtorrent's naming is from our side of the wire: `choked` is us choking them,
# `remote_choked` is them choking us. Getting this backwards inverts the whole
# tit-for-tat picture, which is the one thing the peer table exists to show.
PEER_FLAGS = {
    "interesting": lt.peer_info.interesting,              # we want pieces they have
    "choked": lt.peer_info.choked,                        # we are refusing to upload
    "remote_interested": lt.peer_info.remote_interested,  # they want pieces we have
    "remote_choked": lt.peer_info.remote_choked,          # they are refusing to upload
    "seed": lt.peer_info.seed,
    "optimistic_unchoke": lt.peer_info.optimistic_unchoke,
    "snubbed": lt.peer_info.snubbed,
    "upload_only": lt.peer_info.upload_only,
    "endgame": lt.peer_info.endgame_mode,
    "on_parole": lt.peer_info.on_parole,
    "handshake": lt.peer_info.handshake,
    "connecting": lt.peer_info.connecting,
    "encrypted": lt.peer_info.rc4_encrypted | lt.peer_info.plaintext_encrypted,
    "utp": getattr(lt.peer_info, "utp_socket", 0),
    "holepunched": lt.peer_info.holepunched,
}

ALERT_BUFFER = 800         # ring buffer depth; piece_finished alone can be chatty

# libtorrent stamps every torrent alert with the first six hex digits of the
# infohash. Useful in a log file, pure noise in a UI about one torrent.
IH_PREFIX = re.compile(r'^[0-9a-f]{6}\s+')

# Without an explicit mask libtorrent keeps most of this quiet. Peer and piece
# notifications are the noisy ones and also the interesting ones - they are what
# makes the protocol visible rather than inferred.
ALERT_MASK = (
    lt.alert.category_t.error_notification
    | lt.alert.category_t.peer_notification
    | lt.alert.category_t.connect_notification
    | lt.alert.category_t.tracker_notification
    | lt.alert.category_t.dht_notification
    | lt.alert.category_t.status_notification
    | lt.alert.category_t.storage_notification
    | lt.alert.category_t.piece_progress_notification
    | lt.alert.category_t.performance_warning
)


def classify(what):
    """Sort an alert into one of six bands the UI paints differently. Kept to the
    existing palette: teal for data that landed, amber for swarm discovery in
    flight, grey for high-volume low-signal churn, red for trouble."""
    if what == "hash_failed":
        return "corrupt"
    if "error" in what or "failed" in what or "rejected" in what:
        return "error"
    if what.startswith(("tracker_", "scrape_")):
        return "tracker"
    if what.startswith("dht_"):
        return "dht"
    if what.startswith(("peer_", "incoming_")):
        return "peer"
    if what.startswith(("piece_", "block_", "hash_")):
        return "piece"
    return "torrent"


PEER_SOURCES = [
    ("tracker", lt.peer_info.tracker),
    ("DHT", lt.peer_info.dht),
    ("PEX", lt.peer_info.pex),
    ("LSD", lt.peer_info.lsd),
    ("resume", lt.peer_info.resume_data),
    ("incoming", getattr(lt.peer_info, "incoming", 0)),
]


class Streamer:
    """One torrent, one selected file, exposed as a seekable byte stream."""

    def __init__(self):
        self.ses = lt.session({
            "listen_interfaces": "0.0.0.0:6881",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "connections_limit": 400,
            "alert_mask": ALERT_MASK,
        })
        self.handle = None
        self.ready = False
        self.error = None
        self._lock = threading.Lock()
        self._anchor = -1
        self._playhead = 0

        self.mode = "streaming"
        self.source = ""
        self.infohash = ""
        self.videos = []
        self.subs = []
        self.sub_pieces = set()
        self._probe = None
        self._probe_for = None
        self.file_index = None
        self.file_name = ""
        self.run = None        # the measurement in progress
        self.trials = {}       # mode -> run, kept by reference so counters keep rising

        self.alerts = collections.deque(maxlen=ALERT_BUFFER)
        self.alert_counts = collections.Counter()
        self.hash_fails = 0
        self._seq = 0
        self._alock = threading.Lock()
        threading.Thread(target=self._pump_alerts, daemon=True).start()

    # ---------- measurement ----------

    def _new_run(self, mode, cold):
        """A run is one attempt at "add a torrent and start playing it"."""
        run = {
            "mode": mode,
            "cold": cold,          # cold = started from an empty download directory
            "t_add": time.time(),
            "meta_s": None,        # seconds spent resolving metadata (same for both modes)
            "ttfb": None,          # seconds from add to the first byte handed to the player
            "bytes_before": None,  # payload pulled off the swarm before that first byte
            "peers_at_first_byte": None,
            "preexisting": 0.0,    # % of the torrent already on disk when the run began
            "stalls": 0,
            "stall_s": 0.0,
            "longest_stall": 0.0,
            "startup_wait": 0.0,   # blocking before the first byte; latency, not a stall
            "timeouts": 0,
            "name": self.file_name,
            "infohash": self.infohash,
        }
        self.run = run
        self.trials[mode] = run
        return run

    def _mark_metadata(self):
        r = self.run
        if r and r["meta_s"] is None:
            r["meta_s"] = round(time.time() - r["t_add"], 2)
            r["name"] = self.file_name
            r["infohash"] = self.infohash

    def _mark_block(self, secs, timed_out):
        """Blocking before the first byte is startup latency. After it, it's a stall:
        the player had frames and then ran out, which is what a viewer actually feels."""
        r = self.run
        if not r:
            return
        if timed_out:
            r["timeouts"] += 1
        if r["ttfb"] is None:
            r["startup_wait"] = round(r["startup_wait"] + secs, 2)
        elif secs > STALL_SECONDS:
            r["stalls"] += 1
            r["stall_s"] = round(r["stall_s"] + secs, 2)
            r["longest_stall"] = round(max(r["longest_stall"], secs), 2)

    def _mark_first_byte(self):
        r = self.run
        if not r or r["ttfb"] is not None or self.handle is None:
            return
        s = self.handle.status()
        r["ttfb"] = round(time.time() - r["t_add"], 2)
        r["bytes_before"] = int(getattr(s, "total_payload_download", 0)
                                or getattr(s, "total_download", 0))
        r["peers_at_first_byte"] = s.num_peers

    def trials_view(self):
        return [{k: v for k, v in self.trials[m].items() if k != "t_add"}
                for m in MODES if m in self.trials]

    # ---------- lifecycle ----------

    def start(self, source, mode=None):
        self.stop()
        self.ready, self.error = False, None
        if mode in MODES:
            self.mode = mode
        self.source = (source or "").strip()
        self.infohash = ""
        try:
            params = self._params(self.source)
        except Exception as exc:
            self.error = f"Couldn't read that link: {exc}"
            return False
        params.save_path = SAVE_PATH
        if self.mode == "streaming":
            params.flags |= lt.torrent_flags.sequential_download
        self.clear_events()
        self.handle = self.ses.add_torrent(params)
        self._new_run(self.mode, cold=True)
        threading.Thread(target=self._await_metadata, daemon=True).start()
        return True

    @staticmethod
    def _params(source):
        source = source.strip()
        if source.startswith("magnet:"):
            return lt.parse_magnet_uri(source)
        if not os.path.exists(source):
            raise ValueError("not a magnet link and no such file on disk")
        p = lt.add_torrent_params()
        p.ti = lt.torrent_info(source)
        return p

    def stop(self, delete_data=False):
        if self.handle:
            try:
                if delete_data and DELETE_FILES is not None:
                    self.ses.remove_torrent(self.handle, DELETE_FILES)
                else:
                    self.ses.remove_torrent(self.handle)
            except Exception:
                pass
        self.handle, self.ready, self._anchor = None, False, -1
        self.run, self._playhead = None, 0

    def _read_infohash(self):
        try:
            return str(self.handle.info_hashes().get_best())
        except Exception:
            try:
                return str(self.handle.status().info_hash)
            except Exception:
                return ""

    def _await_metadata(self, timeout=180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.handle is None:
                return
            if self.handle.status().has_metadata:
                break
            time.sleep(0.25)
        else:
            self.error = "No peers responded. The torrent may be dead, or UDP is blocked."
            return

        self.ti = self.handle.torrent_file()
        self.fs = self.ti.files()
        self.infohash = self._read_infohash()
        self.videos = [
            {"index": i,
             "name": os.path.basename(self.fs.file_path(i)),
             "size": self.fs.file_size(i)}
            for i in range(self.fs.num_files())
            if os.path.splitext(self.fs.file_path(i))[1].lower() in VIDEO_EXT
        ]
        if not self.videos:
            self.videos = [{"index": i,
                            "name": os.path.basename(self.fs.file_path(i)),
                            "size": self.fs.file_size(i)}
                           for i in range(self.fs.num_files())]
        self.subs = [
            {"index": i,
             "name": os.path.basename(self.fs.file_path(i)),
             "size": self.fs.file_size(i)}
            for i in range(self.fs.num_files())
            if os.path.splitext(self.fs.file_path(i))[1].lower() in SUB_EXT
        ]
        self.videos.sort(key=lambda v: -v["size"])
        self.select(self.videos[0]["index"])

        # Data left over from a previous run makes time-to-first-byte meaningless,
        # so the run stops claiming to be a cold start.
        pre = round(self.handle.status().progress * 100, 1)
        if self.run:
            self.run["preexisting"] = pre
            if pre > 1.0:
                self.run["cold"] = False
        self._mark_metadata()
        self.ready = True

    # ---------- file selection ----------

    def select(self, index):
        self.file_index = index
        self.file_size = self.fs.file_size(index)
        self.rel_path = self.fs.file_path(index)
        self.file_name = os.path.basename(self.rel_path)
        self.disk_path = os.path.join(SAVE_PATH, self.rel_path)
        self.browser_playable = (
            os.path.splitext(self.file_name)[1].lower() in BROWSER_SAFE
        )
        self._probe, self._probe_for = None, None

        sub_idx = {s["index"] for s in self.subs}
        self.handle.prioritize_files(
            [PRIO_TOP if i in sub_idx else PRIO_LOW if i == index else PRIO_SKIP
             for i in range(self.fs.num_files())]
        )
        self.sub_pieces = set()
        for si in sub_idx:
            sz = self.fs.file_size(si)
            if sz:
                a = self.ti.map_file(si, 0, 1).piece
                b = self.ti.map_file(si, sz - 1, 1).piece
                self.sub_pieces.update(range(a, b + 1))

        self.first_piece = self.piece_for(0)
        self.last_piece = self.piece_for(self.file_size - 1)
        self._anchor, self._playhead = -1, 0
        self.apply_strategy()

    # ---------- offset <-> piece ----------

    def piece_for(self, offset):
        return self.ti.map_file(self.file_index, offset, 1).piece

    def bytes_left_in_piece(self, offset):
        req = self.ti.map_file(self.file_index, offset, 1)
        return self.ti.piece_size(req.piece) - req.start

    # ---------- prioritisation ----------

    def set_mode(self, mode):
        if mode not in MODES or mode == self.mode:
            return False
        self.mode = mode
        if self.handle is None or not self.ready:
            return True
        self.apply_strategy()
        # Switching mid-torrent means the next run starts on partly-downloaded
        # data, so its time-to-first-byte cannot be compared with a cold start.
        pre = round(self.handle.status().progress * 100, 1)
        r = self._new_run(mode, cold=False)
        r["preexisting"] = pre
        r["meta_s"] = 0.0
        return True

    def apply_strategy(self):
        if self.mode == "rarest":
            self._apply_rarest()
        else:
            self._set_sequential(True)
            self._anchor = -1          # force set_window to rebuild the whole map
            self.set_window(self._playhead)

    def _set_sequential(self, on):
        try:
            if on:
                self.handle.set_flags(lt.torrent_flags.sequential_download)
            else:
                self.handle.unset_flags(lt.torrent_flags.sequential_download)
        except AttributeError:
            self.handle.set_sequential_download(on)

    def _apply_rarest(self):
        """Classic torrenting. Every piece of the file is equally wanted, nothing
        is deadlined, and the sequential hint is off, so libtorrent falls back to
        picking whichever piece is scarcest in the swarm. Good for the swarm,
        useless for playback, which is the thing this switch exists to show."""
        with self._lock:
            self._set_sequential(False)
            prios = [PRIO_SKIP] * self.ti.num_pieces()
            for p in range(self.first_piece, self.last_piece + 1):
                prios[p] = PRIO_NORMAL
            for p in self.sub_pieces:
                prios[p] = PRIO_TOP
            self.handle.prioritize_pieces(prios)
            try:
                self.handle.clear_piece_deadlines()
            except AttributeError:
                for p in range(self.first_piece, self.last_piece + 1):
                    self.handle.reset_piece_deadline(p)
            self._anchor = -1

    def set_window(self, offset):
        if self.mode != "streaming":
            return
        with self._lock:
            cur = self.piece_for(min(offset, self.file_size - 1))
            if cur == self._anchor:
                return
            self._anchor = cur

            prios = [PRIO_SKIP] * self.ti.num_pieces()
            for p in range(self.first_piece, self.last_piece + 1):
                prios[p] = PRIO_LOW
            for p in range(self.first_piece,
                           min(self.first_piece + HEAD_PIECES, self.last_piece + 1)):
                prios[p] = PRIO_TOP
            for p in range(max(self.last_piece - TAIL_PIECES + 1, self.first_piece),
                           self.last_piece + 1):
                prios[p] = PRIO_TOP

            end = min(cur + WINDOW_PIECES, self.last_piece + 1)
            for p in range(cur, end):
                prios[p] = PRIO_TOP
            for p in self.sub_pieces:
                prios[p] = PRIO_TOP
            self.handle.prioritize_pieces(prios)
            # Deadlines are relative to now and rise across the window, so the
            # piece the player needs next outranks the one after it.
            for n, p in enumerate(range(cur, end)):
                self.handle.set_piece_deadline(p, DEADLINE_STEP_MS * n)

    def wait_for_piece(self, piece, timeout=PIECE_TIMEOUT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.handle is None:
                return False
            if self.handle.have_piece(piece):
                return True
            time.sleep(0.08)
        return False

    # ---------- reading ----------

    def stream(self, start, end):
        self._playhead = start
        self.set_window(start)
        waited = 0.0
        while not os.path.exists(self.disk_path) and waited < 60:
            time.sleep(0.2)
            waited += 0.2
        if not os.path.exists(self.disk_path):
            return

        pos = start
        with open(self.disk_path, "rb") as f:
            while pos <= end:
                if self.handle is None:
                    return
                piece = self.piece_for(pos)
                if not self.handle.have_piece(piece):
                    self._playhead = pos
                    self.set_window(pos)
                    t0 = time.time()
                    ok = self.wait_for_piece(piece)
                    self._mark_block(time.time() - t0, timed_out=not ok)
                    if not ok:
                        return
                elif self.mode == "streaming" and piece - self._anchor > WINDOW_PIECES // 2:
                    self._playhead = pos
                    self.set_window(pos)

                size = min(self.bytes_left_in_piece(pos), end - pos + 1, 256 * 1024)
                f.seek(pos)
                data = f.read(size)
                if not data:
                    time.sleep(0.05)
                    continue
                yield data
                self._mark_first_byte()
                pos += len(data)

    # ---------- telemetry ----------

    def piece_map(self, buckets=200):
        """Downsample the bitfield so the UI can draw it at any torrent size."""
        total = self.last_piece - self.first_piece + 1
        buckets = min(buckets, total)
        cells = []
        for b in range(buckets):
            idx = self.first_piece + (b * total) // buckets
            cells.append(1 if self.handle.have_piece(idx) else 0)

        span = max(total - 1, 1)
        out = {
            "cells": cells,
            "head": HEAD_PIECES / span,
            "tail": TAIL_PIECES / span,
            "pins": self.mode == "streaming",
            "window": None,
        }
        if self.mode == "streaming" and self._anchor >= 0:
            out["window"] = {
                "start": max(self._anchor - self.first_piece, 0) / span,
                "end": min(self._anchor - self.first_piece + WINDOW_PIECES, total) / span,
            }
        return out

    def snapshot(self):
        if self.error:
            return {"state": "error", "message": self.error, "mode": self.mode,
                    "trials": self.trials_view()}
        if self.handle is None:
            return {"state": "idle", "mode": self.mode, "trials": self.trials_view()}
        s = self.handle.status()
        if not self.ready:
            return {"state": "resolving", "peers": s.num_peers, "mode": self.mode,
                    "trials": self.trials_view()}
        return {
            "state": "ready",
            "name": self.file_name,
            "size": self.file_size,
            "progress": round(s.progress * 100, 1),
            "down": round(s.download_rate / 1000, 1),
            "up": round(s.upload_rate / 1000, 1),
            "peers": s.num_peers,
            "seeds": s.num_seeds,
            "playable": self.browser_playable,
            "files": self.videos,
            "selected": self.file_index,
            "infohash": self.infohash,
            "mode": self.mode,
            "trials": self.trials_view(),
            "map": self.piece_map(),
        }


    # ---------- metadata inspector ----------

    def _bnode(self, key, val, depth=0):
        """Render one bdecoded value as a JSON-safe tree node. Binary values are
        described rather than shipped - `pieces` alone is 20 bytes per piece, which
        for a large torrent is megabytes of SHA-1 nobody wants to scroll."""
        k = key.decode("utf-8", "replace") if isinstance(key, bytes) else key
        node = {"k": k, "open": depth < 1}

        if isinstance(val, dict):
            node.update(t="dict", n=len(val),
                        c=[self._bnode(kk, vv, depth + 1) for kk, vv in val.items()])
        elif isinstance(val, list):
            node.update(t="list", n=len(val),
                        c=[self._bnode(i, vv, depth + 1) for i, vv in enumerate(val)])
        elif isinstance(val, int):
            node.update(t="int", v=val)
        elif isinstance(val, (bytes, bytearray)):
            b = bytes(val)
            if k == "pieces" and len(b) % 20 == 0 and len(b) >= 20:
                node.update(t="pieces", n=len(b) // 20, bytes=len(b),
                            first=b[:20].hex(), last=b[-20:].hex())
            else:
                try:
                    text = b.decode("utf-8")
                    printable = text.isprintable() or "\n" in text
                except UnicodeDecodeError:
                    text, printable = "", False
                if printable and len(b) <= 300:
                    node.update(t="str", v=text, bytes=len(b))
                else:
                    node.update(t="blob", bytes=len(b), hex=b[:24].hex())
        else:
            node.update(t="str", v=str(val))
        return node

    def metadata(self):
        if not (self.ready and self.handle is not None and self.ti):
            return {"ok": False}
        ti = self.ti

        # The exact bytes of the info dictionary, as they arrived off the wire.
        # This is the thing the infohash is computed from - not the file, not the
        # magnet link, just these bytes - which is why changing one byte of the
        # info dict makes it a different torrent.
        raw_info = bytes(ti.info_section())
        derived = hashlib.sha1(raw_info).hexdigest()

        # Decode only bytes that actually travelled. Regenerating the torrent with
        # create_torrent() would be easier but it invents an outer dictionary and
        # stamps *today* as the creation date, which is worse than showing nothing.
        origin, tree_src = "magnet", None
        if self.source and not self.source.startswith("magnet:"):
            try:
                with open(self.source, "rb") as fh:
                    tree_src = lt.bdecode(fh.read())
                origin = "file"
            except Exception:
                tree_src = None
        if tree_src is None:
            # A magnet link carries the info dictionary and nothing else: announce
            # URLs, creation date, comment and created-by live in the outer
            # dictionary of a .torrent file and were never transferred.
            tree_src = lt.bdecode(raw_info)
        tree = self._bnode("info" if origin == "magnet" else "torrent", tree_src, 0)

        tiers = {}
        for t in ti.trackers():
            tiers.setdefault(t.tier, []).append(t.url)

        created = ti.creation_date()
        return {
            "ok": True,
            "infohash": self.infohash,
            "derived": derived,
            "matches": derived == self.infohash,
            "info_bytes": len(raw_info),
            "name": ti.name(),
            "piece_length": ti.piece_length(),
            "num_pieces": ti.num_pieces(),
            "total_size": ti.total_size(),
            "num_files": ti.num_files(),
            "private": bool(ti.priv()),
            "creator": ti.creator() or "",
            "comment": ti.comment() or "",
            "created": created if isinstance(created, int) else 0,
            "tiers": [{"tier": t, "urls": u} for t, u in sorted(tiers.items())],
            "origin": origin,
            "tree": tree,
        }

    # ---------- what is actually in the file ----------

    def media_info(self):
        """Read the container to find out why playback is silent, black or fine.
        Cached once it succeeds: codecs do not change mid-file, and the answer
        needs the header on disk, which for moov-at-the-end MP4 means waiting for
        the tail pieces."""
        if not self.ready:
            return {"ready": False}
        if self._probe is None or self._probe_for != self.file_index:
            info = probe.probe(self.disk_path)
            if info is None:
                return {"ready": False, "probing": True, "ffmpeg": HAS_FFMPEG}
            self._probe, self._probe_for = info, self.file_index
        out = {"ready": True, "container": self._probe["container"],
               "tracks": self._probe["tracks"], "ffmpeg": HAS_FFMPEG,
               "complete": self.file_complete(self.file_index)}
        out.update(probe.verdict(self._probe, self.browser_playable))
        return out

    def embedded_subs(self):
        """Subtitle tracks living inside the video file rather than beside it.
        Matroska interleaves their cues through the whole file, so unlike a
        sidecar .srt these cannot be read until the download finishes."""
        if not (self.ready and self._probe):
            return []
        out, n = [], 0
        for t in self._probe["tracks"]:
            if t["type"] != "subtitle":
                continue
            out.append({"n": n, "codec": t["codec"], "lang": t.get("lang", ""),
                        "name": t.get("name", ""),
                        "text": t["codec"] in ("srt", "ass", "ssa", "webvtt")})
            n += 1
        return out

    def embedded_vtt(self, n):
        if not HAS_FFMPEG:
            return None, "embedded subtitles need ffmpeg on PATH"
        if not self.file_complete(self.file_index):
            return None, "embedded subtitle cues are spread through the whole file, "                         "so the download has to finish first"
        try:
            r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", self.disk_path,
                                "-map", f"0:s:{int(n)}", "-f", "webvtt", "pipe:1"],
                               capture_output=True, timeout=120)
            if not r.stdout:
                return None, (r.stderr.decode("utf-8", "replace")[:200] or "no output")
            return r.stdout.decode("utf-8", "replace"), None
        except Exception as exc:
            return None, f"ffmpeg failed: {exc}"

    # ---------- subtitles ----------

    def _sub_lang(self, name):
        """Guess a language tag from the filename. Release groups put it in the
        stem (Movie.en.srt, Movie.English.srt) often enough to be worth trying."""
        stem = os.path.splitext(name)[0].lower()
        for tag, keys in (("en", ("en", "eng", "english")),
                          ("es", ("es", "spa", "spanish", "castellano")),
                          ("fr", ("fr", "fre", "fra", "french")),
                          ("de", ("de", "ger", "deu", "german")),
                          ("it", ("it", "ita", "italian")),
                          ("nl", ("nl", "dut", "nld", "dutch")),
                          ("pl", ("pl", "pol", "polish")),
                          ("pt", ("pt", "por", "portuguese", "brazilian")),
                          ("ru", ("ru", "rus", "russian")),
                          ("hi", ("hi", "hin", "hindi")),
                          ("ar", ("ar", "ara", "arabic")),
                          ("zh", ("zh", "chi", "zho", "chinese")),
                          ("ja", ("ja", "jpn", "japanese")),
                          ("ko", ("ko", "kor", "korean")),
                          ("tr", ("tr", "tur", "turkish")),
                          ("sv", ("sv", "swe", "swedish")),
                          ("da", ("da", "dan", "danish")),
                          ("fi", ("fi", "fin", "finnish")),
                          ("no", ("no", "nor", "norwegian"))):
            for part in re.split(r"[.\-_\s\[\]()]+", stem):
                if part in keys:
                    return tag
        return ""

    def file_complete(self, index):
        size = self.fs.file_size(index)
        if not size:
            return True
        a = self.ti.map_file(index, 0, 1).piece
        b = self.ti.map_file(index, size - 1, 1).piece
        return all(self.handle.have_piece(p) for p in range(a, b + 1))

    def sub_list(self):
        if not self.ready:
            return {"subs": [], "ffmpeg": HAS_FFMPEG}
        out = []
        for s in self.subs:
            ext = os.path.splitext(s["name"])[1].lower()
            out.append({
                "index": s["index"],
                "name": s["name"],
                "size": s["size"],
                "lang": self._sub_lang(s["name"]),
                "ready": self.file_complete(s["index"]),
                # .ass/.ssa carry styling WebVTT has no equivalent for, so those
                # only work when ffmpeg is around to do the conversion
                "supported": ext in (".srt", ".vtt") or HAS_FFMPEG,
            })
        return {"subs": out, "ffmpeg": HAS_FFMPEG}

    def subtitle_vtt(self, index):
        """Return WebVTT text, or None with a reason. Browsers accept WebVTT in a
        <track> and nothing else, so everything is converted on the way out."""
        match = next((s for s in self.subs if s["index"] == index), None)
        if match is None:
            return None, "not a subtitle file in this torrent"
        if not self.file_complete(index):
            return None, "still downloading"

        path = os.path.join(SAVE_PATH, self.fs.file_path(index))
        if not os.path.exists(path):
            return None, "not on disk yet"
        ext = os.path.splitext(match["name"])[1].lower()

        if ext in (".ass", ".ssa", ".sub"):
            if not HAS_FFMPEG:
                return None, f"{ext} needs ffmpeg on PATH"
            try:
                out = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path,
                                      "-f", "webvtt", "pipe:1"],
                                     capture_output=True, timeout=30)
                return out.stdout.decode("utf-8", "replace"), None
            except Exception as exc:
                return None, f"ffmpeg failed: {exc}"

        # subtitle files are frequently cp1252 rather than utf-8, and a single bad
        # byte otherwise throws the whole track away
        raw = open(path, "rb").read()
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            return None, "could not decode text"

        text = text.replace("\r\n", "\n")
        if ext == ".vtt":
            return text, None
        # SRT to WebVTT is a header plus decimal commas becoming points; SRT cue
        # numbers are legal WebVTT cue identifiers, so they can stay
        text = re.sub(r"(\d\d:\d\d:\d\d),(\d\d\d)", r"\1.\2", text)
        return "WEBVTT\n\n" + text.lstrip("\ufeff"), None

    # ---------- protocol events ----------

    def _pump_alerts(self):
        """pop_alerts() must be drained by exactly one thread, and anything left
        undrained piles up inside libtorrent - so this loop owns it and nothing
        else in the app is allowed to call it."""
        while True:
            try:
                self.ses.wait_for_alert(500)
                for a in self.ses.pop_alerts():
                    self._record_alert(a)
            except Exception:
                time.sleep(0.5)

    def _record_alert(self, a):
        what = a.what()
        # strip the per-line torrent identifier so the log reads as protocol
        # rather than as the same six characters repeated 800 times
        msg = IH_PREFIX.sub("", a.message())
        try:
            name = a.torrent_name
            if name and msg.startswith(name):
                msg = msg[len(name):].lstrip(" :-")
        except Exception:
            pass

        with self._alock:
            self._seq += 1
            self.alert_counts[what] += 1
            if what == "hash_failed":
                self.hash_fails += 1
            self.alerts.append({
                "seq": self._seq,
                "t": time.time(),
                "what": what,
                "cat": classify(what),
                "msg": msg[:220],
            })

    def events(self, since=0, limit=400):
        with self._alock:
            buf = list(self.alerts)
            counts = dict(self.alert_counts)
            hf, seq = self.hash_fails, self._seq
        fresh = [e for e in buf if e["seq"] > since]
        # two different kinds of "you didn't see everything", worth separating:
        # dropped = rolled out of the ring buffer for good; trimmed = still in the
        # buffer, just more than one response can carry
        dropped = 0
        if since and buf and buf[0]["seq"] > since + 1:
            dropped = buf[0]["seq"] - since - 1
        bands = collections.Counter()
        for what, n in counts.items():
            bands[classify(what)] += n
        return {
            "events": fresh[-limit:],
            "seq": seq,
            "dropped": dropped,
            "trimmed": max(0, len(fresh) - limit),
            "counts": counts,
            "bands": dict(bands),
            "hash_fails": hf,
            "buffer": len(buf),
        }

    def clear_events(self):
        with self._alock:
            self.alerts.clear()
            self.alert_counts.clear()
            self.hash_fails = 0

    # ---------- peers ----------

    def peer_table(self):
        """One row per connected peer. Kept off /api/status because get_peer_info()
        walks every connection and this panel polls on its own, slower interval."""
        if self.handle is None or not self.ready:
            return {"peers": [], "totals": {}}

        rows = []
        for p in self.handle.get_peer_info():
            f = p.flags
            try:
                ip = "%s:%d" % (p.ip[0], p.ip[1])
            except Exception:
                ip = str(p.ip)
            client = p.client
            if isinstance(client, bytes):
                client = client.decode("utf-8", "replace")
            rows.append({
                "ip": ip,
                "client": (client or "").strip() or "unknown",
                "source": [n for n, bit in PEER_SOURCES if bit and p.source & bit],
                "down": p.payload_down_speed,
                "up": p.payload_up_speed,
                "total_down": p.total_download,
                "total_up": p.total_upload,
                "progress": round(p.progress * 100, 1),
                "rtt": p.rtt,
                "hashfails": p.num_hashfails,
                "failcount": p.failcount,
                # libtorrent's own guess at the upload rate that would buy an
                # unchoke from this peer - tit-for-tat with a number on it
                "recip": getattr(p, "estimated_reciprocation_rate", 0),
                "flags": {k: bool(bit and f & bit) for k, bit in PEER_FLAGS.items()},
            })

        rows.sort(key=lambda r: (-r["down"], -r["up"], -r["progress"]))
        totals = {
            "connected": len(rows),
            "seeds": sum(1 for r in rows if r["flags"]["seed"]),
            # they let us download                      we let them download
            "feeding_us": sum(1 for r in rows if not r["flags"]["remote_choked"]
                              and r["flags"]["interesting"]),
            "fed_by_us": sum(1 for r in rows if not r["flags"]["choked"]
                             and r["flags"]["remote_interested"]),
            "snubbed": sum(1 for r in rows if r["flags"]["snubbed"]),
        }
        return {"peers": rows, "totals": totals}

    # ---------- bandwidth ----------

    def limits(self):
        s = self.ses.get_settings()
        return {"down": s.get("download_rate_limit", 0),
                "up": s.get("upload_rate_limit", 0)}

    def set_limits(self, down=None, up=None):
        """Session-wide caps in bytes/sec. libtorrent reads 0 as *unlimited*, so
        there is no way to say "stop uploading" - the UI sends 1 B/s for that,
        which is the closest thing to off. Starving your upload and watching the
        swarm choke you back is the whole point of exposing this."""
        patch = {}
        if down is not None:
            patch["download_rate_limit"] = max(0, int(down))
        if up is not None:
            patch["upload_rate_limit"] = max(0, int(up))
        if patch:
            self.ses.apply_settings(patch)
        return self.limits()


streamer = Streamer()
