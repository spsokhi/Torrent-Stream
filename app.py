#!/usr/bin/env python3
"""
app.py - paste a torrent link in your browser and watch it.

A torrent client that also shows you what a torrent client is doing. Pieces are
fetched in playback order and served over HTTP with Range support, so a browser
<video> element plays the file while it is still arriving.

    streamer.py   the libtorrent engine, piece strategies, instrumentation
    routes.py     the HTTP surface
    static/       the UI - plain HTML, CSS and JS, no build step
    app.py        this file: wiring and the entry point

Install:
    pip install libtorrent flask

Run:
    python app.py

Optional: install ffmpeg and put it on PATH to enable the "convert for browser"
button, which remuxes formats Chrome/Firefox can't play natively (MKV, HEVC).

Legal test content, well seeded:
    Sintel  magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10
    More at https://webtorrent.io/free-torrents and archive.org
"""

import os
import threading
import webbrowser

try:
    from flask import Flask, send_from_directory
except ImportError:
    raise SystemExit("flask missing.  Run:  pip install flask")

from routes import bp
from streamer import HAS_FFMPEG, HAS_FFPROBE, PORT, SAVE_PATH

HERE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0     # this is a dev tool; never cache the UI
app.register_blueprint(bp)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def main():
    os.makedirs(SAVE_PATH, exist_ok=True)
    tick = lambda ok: "available" if ok else "not found on PATH"
    print(f"\n  Torrent stream running at  http://127.0.0.1:{PORT}")
    print(f"  Downloads land in          {SAVE_PATH}")
    print(f"  ffmpeg  (convert button)   {tick(HAS_FFMPEG)}")
    print(f"  ffprobe (media probe)      {tick(HAS_FFPROBE)}\n")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
