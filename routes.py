"""
routes.py - the HTTP surface.

Three kinds of endpoint live here:

  * control      /api/add, /api/select, /api/mode, /api/limits, /api/rerun, /api/stop
  * telemetry    /api/status (1 Hz, deliberately cheap)
                 /api/peers and /api/events poll separately and only when open
  * bytes        /stream (Range-aware), /subs/<n> (WebVTT), /transcode (ffmpeg)

Anything that touches libtorrent belongs in streamer.py, not here.
"""

import mimetypes
import re
import subprocess
import time

from flask import Blueprint, Response, jsonify, request

from streamer import HAS_FFMPEG, HAS_FFPROBE, PORT, streamer

bp = Blueprint("api", __name__)


# ---------- control ----------

@bp.post("/api/add")
def api_add():
    body = request.json or {}
    ok = streamer.start(body.get("source", ""), body.get("mode"))
    return jsonify({"ok": ok, "error": streamer.error})


@bp.post("/api/select")
def api_select():
    if streamer.ready:
        streamer.select(int(request.json["index"]))
    return jsonify({"ok": True})


@bp.post("/api/mode")
def api_mode():
    mode = (request.json or {}).get("mode", "")
    changed = streamer.set_mode(mode)
    return jsonify({"ok": True, "mode": streamer.mode, "changed": changed})


@bp.post("/api/limits")
def api_limits():
    body = request.json or {}
    return jsonify({"ok": True,
                    "limits": streamer.set_limits(body.get("down"), body.get("up"))})


@bp.post("/api/rerun")
def api_rerun():
    """Wipe the downloaded data and add the same torrent again in the given mode.
    The A/B only means anything from a cold start, so this is the honest way to
    take the second measurement."""
    mode = (request.json or {}).get("mode", streamer.mode)
    source = streamer.source
    if not source:
        return jsonify({"ok": False, "error": "nothing to re-run"}), 409
    streamer.stop(delete_data=True)
    time.sleep(0.6)                       # let libtorrent finish unlinking files
    ok = streamer.start(source, mode)
    return jsonify({"ok": ok, "error": streamer.error})


@bp.post("/api/reset-trials")
def api_reset_trials():
    streamer.trials = {}
    if streamer.run:
        streamer.trials[streamer.run["mode"]] = streamer.run
    return jsonify({"ok": True})


@bp.post("/api/stop")
def api_stop():
    streamer.stop()
    return jsonify({"ok": True})


# ---------- telemetry ----------

@bp.get("/api/status")
def api_status():
    snap = streamer.snapshot()
    snap["ffmpeg"] = HAS_FFMPEG
    snap["ffprobe"] = HAS_FFPROBE
    snap["limits"] = streamer.limits()
    return jsonify(snap)


@bp.get("/api/peers")
def api_peers():
    return jsonify(streamer.peer_table())


@bp.get("/api/events")
def api_events():
    since = request.args.get("since", type=int, default=0)
    return jsonify(streamer.events(since))


@bp.post("/api/events/clear")
def api_events_clear():
    streamer.clear_events()
    return jsonify({"ok": True})


@bp.get("/api/media")
def api_media():
    info = streamer.media_info()
    info["embedded"] = streamer.embedded_subs()
    return jsonify(info)


@bp.get("/api/metadata")
def api_metadata():
    return jsonify(streamer.metadata())


@bp.get("/api/subs")
def api_subs():
    return jsonify(streamer.sub_list())


# ---------- bytes ----------


@bp.get("/subs/embedded/<int:n>")
def subs_embedded(n):
    if not streamer.ready:
        return "not ready", 409
    text, why = streamer.embedded_vtt(n)
    if text is None:
        return why, 409
    return Response(text, mimetype="text/vtt", headers={"Cache-Control": "no-store"})


@bp.get("/subs/<int:index>")
def subs(index):
    """Subtitles as WebVTT, because that is the only thing a <track> will take."""
    if not streamer.ready:
        return "not ready", 409
    text, why = streamer.subtitle_vtt(index)
    if text is None:
        return why, 409
    return Response(text, mimetype="text/vtt",
                    headers={"Cache-Control": "no-store"})

@bp.get("/stream")
def stream():
    if not streamer.ready:
        return "not ready", 409
    size = streamer.file_size
    start, end, partial = 0, size - 1, False

    # A Range header is a closed interval and both ends are inclusive, so the
    # length is end - start + 1. Getting that off by one makes players stall at
    # the very end of the file in ways that look like a torrent problem.
    header = request.headers.get("Range")
    if header:
        m = re.match(r"bytes=(\d+)-(\d*)", header.strip())
        if m:
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
            partial = True

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Cache-Control": "no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    return Response(
        streamer.stream(start, end),
        status=206 if partial else 200,
        headers=headers,
        mimetype=mimetypes.guess_type(streamer.file_name)[0] or "video/mp4",
    )


@bp.get("/transcode")
def transcode():
    """Pipe our own stream through ffmpeg into fragmented MP4 the browser eats."""
    if not (streamer.ready and HAS_FFMPEG):
        return "unavailable", 409

    # If the video is already something browsers decode, remux it instead of
    # re-encoding: copying a 720p H.264 stream is realtime, re-encoding is not.
    # Only the audio actually needs converting in the common EAC3-in-MKV case.
    video = (["-c:v", "copy"] if streamer.media_info().get("can_copy_video")
             else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "24"])
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error",
         "-i", f"http://127.0.0.1:{PORT}/stream", "-sn"]
        + video +
        ["-c:a", "aac", "-ac", "2", "-b:a", "192k",
         "-movflags", "frag_keyframe+empty_moov+default_base_moof",
         "-f", "mp4", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    def pump():
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()

    return Response(pump(), mimetype="video/mp4",
                    headers={"Cache-Control": "no-store"})
