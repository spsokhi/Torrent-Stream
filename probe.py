"""
probe.py - what is actually inside this file, without ffprobe.

"It plays but there is no sound" is almost always a codec the browser refuses,
and the only honest way to say so is to read the container. Matroska is EBML and
MP4 is a box tree; both are simple enough to walk in a few dozen lines, and doing
it here means the diagnosis works on a machine with no ffmpeg installed.

Only the header is read - the track list lives before the first cluster in
Matroska and inside moov in MP4, so a few megabytes is plenty.
"""

import os
import struct

# What a current Chrome/Firefox/Edge will decode without help. Deliberately
# conservative: anything not on these lists gets reported as "needs conversion"
# rather than silently producing a black frame or a silent track.
OK_VIDEO = {"h264", "vp8", "vp9", "av1", "theora"}
OK_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac", "pcm"}

MKV_CODECS = {
    "V_MPEG4/ISO/AVC": "h264", "V_MPEGH/ISO/HEVC": "hevc", "V_AV1": "av1",
    "V_VP8": "vp8", "V_VP9": "vp9", "V_THEORA": "theora", "V_MPEG2": "mpeg2",
    "V_MS/VFW/FOURCC": "vfw", "V_MPEG4/ISO/ASP": "mpeg4",
    "A_AAC": "aac", "A_MPEG/L3": "mp3", "A_MPEG/L2": "mp2", "A_OPUS": "opus",
    "A_VORBIS": "vorbis", "A_FLAC": "flac", "A_AC3": "ac3", "A_EAC3": "eac3",
    "A_DTS": "dts", "A_TRUEHD": "truehd", "A_PCM/INT/LIT": "pcm",
    "S_TEXT/UTF8": "srt", "S_TEXT/ASS": "ass", "S_TEXT/SSA": "ssa",
    "S_TEXT/WEBVTT": "webvtt", "S_HDMV/PGS": "pgs", "S_VOBSUB": "vobsub",
}

MP4_CODECS = {
    "avc1": "h264", "avc3": "h264", "hev1": "hevc", "hvc1": "hevc",
    "av01": "av1", "vp09": "vp9", "mp4v": "mpeg4",
    "mp4a": "aac", "ac-3": "ac3", "ec-3": "eac3", "dtsc": "dts",
    "alac": "alac", "Opus": "opus", "fLaC": "flac", ".mp3": "mp3",
    "tx3g": "tx3g", "wvtt": "webvtt", "c608": "cea608",
}

MKV_TRACK_TYPE = {1: "video", 2: "audio", 17: "subtitle"}


# ---------- Matroska / EBML ----------

def _vint(buf, pos, keep_marker):
    """EBML integers are self-describing: leading zeros in the first byte say how
    many bytes long the value is. IDs keep the marker bit, sizes strip it."""
    if pos >= len(buf):
        return None, pos
    first, length, mask = buf[pos], 1, 0x80
    if first == 0:
        return None, pos + 1
    while not (first & mask):
        mask >>= 1
        length += 1
        if length > 8:
            return None, pos + 1
    if pos + length > len(buf):
        return None, len(buf)
    val = first if keep_marker else (first & (mask - 1))
    for i in range(1, length):
        val = (val << 8) | buf[pos + i]
    return val, pos + length


def _uint(b):
    v = 0
    for x in b:
        v = (v << 8) | x
    return v


def _mkv_tracks(buf):
    SEGMENT, TRACKS, ENTRY, CLUSTER = 0x18538067, 0x1654AE6B, 0xAE, 0x1F43B675
    TYPE, CODEC, LANG, NAME, DEFAULT = 0x83, 0x86, 0x22B59C, 0x536E, 0x88
    out, stack = [], [(0, len(buf))]

    while stack:
        pos, stop = stack.pop()
        while pos < stop:
            eid, pos = _vint(buf, pos, True)
            if eid is None:
                break
            size, pos = _vint(buf, pos, False)
            if size is None:
                break
            # an unknown-size element (all value bits set) means "runs to the next
            # sibling", which for our purposes is just "keep reading children"
            end = stop if size > stop - pos else pos + size

            if eid in (SEGMENT, TRACKS):
                stack.append((pos, end))
                break
            if eid == CLUSTER:
                return out          # clusters are payload; Tracks is already behind us
            if eid == ENTRY:
                t = {"type": "?", "codec": "?", "lang": "", "name": "", "default": False}
                p = pos
                while p < end:
                    cid, p = _vint(buf, p, True)
                    if cid is None:
                        break
                    csz, p = _vint(buf, p, False)
                    if csz is None or p + csz > end:
                        break
                    data = buf[p:p + csz]
                    if cid == TYPE:
                        t["type"] = MKV_TRACK_TYPE.get(_uint(data), str(_uint(data)))
                    elif cid == CODEC:
                        raw = data.split(b"\x00")[0].decode("ascii", "replace")
                        t["raw"] = raw
                        t["codec"] = MKV_CODECS.get(raw, raw.lower())
                    elif cid == LANG:
                        t["lang"] = data.split(b"\x00")[0].decode("ascii", "replace")
                    elif cid == NAME:
                        t["name"] = data.split(b"\x00")[0].decode("utf-8", "replace")
                    elif cid == DEFAULT:
                        t["default"] = bool(_uint(data))
                    p += csz
                if t["codec"] != "?":
                    out.append(t)
            pos = end
    return out


# ---------- MP4 / ISO base media ----------

def _mp4_boxes(buf, pos, stop):
    while pos + 8 <= stop:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        kind = buf[pos + 4:pos + 8].decode("latin-1")
        body = pos + 8
        if size == 1:                                   # 64-bit extended size
            if pos + 16 > stop:
                return
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            body = pos + 16
        elif size == 0:
            size = stop - pos
        if size < 8 or pos + size > stop:
            return
        yield kind, body, pos + size
        pos += size


def _mp4_tracks(buf):
    out = []

    def walk(pos, stop, state):
        for kind, body, end in _mp4_boxes(buf, pos, stop):
            if kind in ("moov", "mdia", "minf", "stbl"):
                walk(body, end, state)
            elif kind == "trak":
                t = {"type": "?", "codec": "?", "lang": "", "name": "", "default": True}
                walk(body, end, t)
                if t["codec"] != "?":
                    out.append(t)
            elif kind == "hdlr" and body + 12 <= end:
                h = buf[body + 8:body + 12].decode("latin-1")
                state["type"] = {"vide": "video", "soun": "audio",
                                 "sbtl": "subtitle", "text": "subtitle",
                                 "subt": "subtitle"}.get(h, h)
            elif kind == "stsd" and body + 16 <= end:
                fmt = buf[body + 12:body + 16].decode("latin-1")
                state["raw"] = fmt
                state["codec"] = MP4_CODECS.get(fmt, fmt.strip().lower())
    walk(0, len(buf), {})
    return out


def _mp4_find_moov(path, cap=32 * 1024 * 1024):
    """Walk the top-level box list by seeking, so a 123 MB mdat costs nothing and
    moov is found wherever the muxer put it: at the front in files written for
    streaming, at the back in most others. A buffer walk never reaches the back."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            pos = 0
            while pos + 8 <= size:
                fh.seek(pos)
                hdr = fh.read(16)
                if len(hdr) < 8:
                    return []
                bsize = struct.unpack(">I", hdr[:4])[0]
                kind = hdr[4:8].decode("latin-1", "replace")
                if bsize == 1:                       # 64-bit extended size
                    if len(hdr) < 16:
                        return []
                    bsize = struct.unpack(">Q", hdr[8:16])[0]
                elif bsize == 0:
                    bsize = size - pos
                if bsize < 8:
                    return []
                if kind == "moov":
                    fh.seek(pos)
                    return _mp4_tracks(fh.read(min(bsize, cap)))
                pos += bsize
    except (OSError, struct.error):
        return []
    return []


# ---------- public ----------

def probe(path, read_bytes=6 * 1024 * 1024):
    """Best-effort look at a file that may still be downloading. Returns None when
    there is not enough of it on disk yet to say anything truthful."""
    try:
        with open(path, "rb") as fh:
            buf = fh.read(read_bytes)
    except OSError:
        return None
    if len(buf) < 1024:
        return None

    try:
        if buf[:4] == b"\x1a\x45\xdf\xa3":
            tracks, container = _mkv_tracks(buf), "matroska"
        elif buf[4:8] in (b"ftyp", b"moov", b"styp"):
            tracks, container = _mp4_find_moov(path), "mp4"
        else:
            return {"container": os.path.splitext(path)[1].lstrip(".").lower() or "?",
                    "tracks": [], "known": False}
    except Exception:
        return None

    # a file whose moov has not arrived yet parses to nothing; report "not yet"
    # rather than a file that genuinely contains no tracks
    if not tracks:
        return None

    for t in tracks:
        if t["type"] == "video":
            t["ok"] = t["codec"] in OK_VIDEO
        elif t["type"] == "audio":
            t["ok"] = t["codec"] in OK_AUDIO
        else:
            t["ok"] = False        # any subtitle track needs extracting either way

    return {"container": container, "tracks": tracks, "known": True}


def verdict(info, browser_container):
    """Turn a probe into the one sentence the viewer actually needs."""
    if not info or not info.get("known"):
        return {"playable": browser_container, "why": "", "fix": ""}

    vid = [t for t in info["tracks"] if t["type"] == "video"]
    aud = [t for t in info["tracks"] if t["type"] == "audio"]
    sub = [t for t in info["tracks"] if t["type"] == "subtitle"]
    bad_v = [t for t in vid if not t["ok"]]
    bad_a = [t for t in aud if not t["ok"]]

    problems = []
    if not browser_container:
        problems.append(f"the {info['container']} container is not one browsers open")
    if bad_v:
        problems.append(f"the video is {bad_v[0]['codec'].upper()}")
    if bad_a and len(bad_a) == len(aud):
        problems.append(f"the audio is {bad_a[0]['codec'].upper()}, "
                        "which no browser decodes")

    return {
        "playable": not problems,
        "why": "; ".join(problems),
        "sub_tracks": len(sub),
        # video that is already H.264 can be remuxed instead of re-encoded, which
        # is the difference between seconds and an hour
        "can_copy_video": bool(vid) and not bad_v,
    }
