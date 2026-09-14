const $ = id => document.getElementById(id);
const V = $("vid");
let playing = false, lastFile = null, mode = "streaming", infohash = "";

const mb = b => b > 1073741824
  ? (b/1073741824).toFixed(2) + " GB" : (b/1048576).toFixed(0) + " MB";
const mbx = b => b >= 1073741824 ? (b/1073741824).toFixed(2) + " GB"
  : b >= 1048576 ? (b/1048576).toFixed(1) + " MB" : (b/1024).toFixed(0) + " kB";
const clamp = (v,a,b) => Math.max(a, Math.min(b, v));
const fmt = s => {
  if (!isFinite(s) || s < 0) return "--:--";
  s = Math.floor(s);
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  const mm = h ? String(m).padStart(2,"0") : String(m);
  return (h ? h + ":" : "") + mm + ":" + String(x).padStart(2,"0");
};

/* ---------- strategy switch ---------- */

const MODE_LABEL = {streaming:"streaming", rarest:"rarest-first"};
const MODE_WHY = {
  streaming: "Head and tail pinned to priority 7, a rolling 32-piece deadline window at the playhead, sequential_download on.",
  rarest: "Every piece of the file at priority 4, all deadlines cleared, sequential_download off. libtorrent then takes whatever is scarcest in the swarm."
};

function paintMode() {
  for (const b of document.querySelectorAll(".seg button"))
    b.classList.toggle("on", b.dataset.mode === mode);
  $("why").textContent = MODE_WHY[mode];
  $("rerun").textContent = "Re-run cold in " + MODE_LABEL[mode === "streaming" ? "rarest" : "streaming"];
}
for (const b of document.querySelectorAll(".seg button")) {
  b.onclick = async () => {
    mode = b.dataset.mode; paintMode();
    await post("/api/mode", {mode});
  };
}

async function post(url, body) {
  return (await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"},
                           body: JSON.stringify(body || {})})).json();
}

/* ---------- A/B table ---------- */

const AB_ROWS = [
  {k:"time to first playable byte", f:t=>t.ttfb,   fmt:v=>v.toFixed(1)+" s", lo:1},
  {k:"metadata resolve",            f:t=>t.meta_s, fmt:v=>v.toFixed(1)+" s", ind:1},
  {k:"piece fetch",  f:t=>(t.ttfb!=null&&t.meta_s!=null)?t.ttfb-t.meta_s:null,
                     fmt:v=>v.toFixed(1)+" s", lo:1, ind:1},
  {k:"blocked waiting on pieces",  f:t=>t.startup_wait, fmt:v=>v.toFixed(1)+" s", lo:1, ind:1},
  {k:"bytes fetched before playback", f:t=>t.bytes_before, fmt:mbx, lo:1},
  {k:"stalls over 500 ms",   f:t=>t.stalls,        fmt:v=>String(v),        lo:1, live:1},
  {k:"longest single stall", f:t=>t.longest_stall, fmt:v=>v.toFixed(1)+" s", lo:1, live:1},
  {k:"total time stalled",   f:t=>t.stall_s,       fmt:v=>v.toFixed(1)+" s", lo:1, live:1},
  {k:"peers at first byte",  f:t=>t.peers_at_first_byte, fmt:v=>String(v)},
  {k:"reads that gave up",   f:t=>t.timeouts,      fmt:v=>String(v),        lo:1, live:1},
];

function renderAB(trials) {
  if (!trials || !trials.length) { $("ab").style.display = "none"; return; }
  $("ab").style.display = "block";

  const by = {}; trials.forEach(t => by[t.mode] = t);
  const cols = ["streaming","rarest"].filter(m => by[m]);

  $("abhead").innerHTML = "<tr><th></th>" + cols.map(m => {
    const t = by[m];
    const sub = t.ttfb == null ? "measuring…"
      : t.cold ? "cold start"
      : "warm start · " + t.preexisting + "% on disk";
    return "<th>" + MODE_LABEL[m] + "<small>" + sub + "</small></th>";
  }).join("") + "</tr>";

  const bothCold = cols.length === 2 && cols.every(m => by[m].cold);
  $("abbody").innerHTML = AB_ROWS.map(row => {
    const vals = cols.map(m => row.f(by[m]));
    let best = null;
    if (row.lo && bothCold && vals.length === 2
        && vals[0] != null && vals[1] != null && vals[0] !== vals[1])
      best = vals[0] < vals[1] ? 0 : 1;
    const tds = vals.map((v, i) => {
      if (v == null) return '<td class="none">—</td>';
      const cls = (i === best ? "win" : "n")
        + (row.live && by[cols[i]].ttfb != null ? " live" : "");
      return '<td class="' + cls + '">' + row.fmt(v) + "</td>";
    }).join("");
    return '<tr class="' + (row.ind ? "ind" : "") + '"><td>' + row.k + "</td>" + tds + "</tr>";
  }).join("");

  const notes = [];
  if (cols.length < 2)
    notes.push("Only one strategy measured. <b>Re-run cold</b> wipes the downloaded data and adds the same torrent again in the other mode, so both numbers start from zero.");
  const warm = cols.filter(m => !by[m].cold).map(m => MODE_LABEL[m]);
  if (warm.length)
    notes.push("<b>" + warm.join(" and ") + "</b> began on a partly-downloaded torrent, so time-to-first-byte and bytes-before-playback are not comparable. Stall counts still are.");
  const hashes = [...new Set(cols.map(m => by[m].infohash).filter(Boolean))];
  if (hashes.length > 1)
    notes.push("<b>Different torrents.</b> These two runs are not measuring the same file.");
  notes.push("Rows marked … keep climbing while playback continues.");
  $("caveat").innerHTML = notes.join("<br>");
}

$("rerun").onclick = async () => {
  const other = mode === "streaming" ? "rarest" : "streaming";
  if (!confirm("This deletes the downloaded data and re-adds the torrent in "
      + MODE_LABEL[other] + " mode, so the comparison starts from zero.\n\nGo ahead?")) return;
  reset(); $("spin").style.display = "block";
  mode = other; paintMode();
  const r = await post("/api/rerun", {mode: other});
  if (!r.ok) { $("spin").style.display = "none"; show(r.error || "re-run failed", true); }
};
$("clearab").onclick = async () => { await post("/api/reset-trials"); };

/* ---------- player ---------- */

function nudge(d) {
  const t = V.currentTime + d;
  V.currentTime = isFinite(V.duration) ? clamp(t, 0, V.duration - 0.25) : Math.max(0, t);
}
function setVol(v) {
  V.volume = clamp(v, 0, 1);
  V.muted = false;
  $("vol").value = V.volume;
  localStorage.setItem("ts.vol", V.volume);
}
function bumpRate(dir) {
  const r = $("rate"), i = clamp(r.selectedIndex + dir, 0, r.options.length - 1);
  r.selectedIndex = i; r.onchange({target: r});
}

$("pp").onclick  = () => V.paused ? V.play() : V.pause();
$("b10").onclick = () => nudge(-10);
$("f10").onclick = () => nudge(10);
$("mute").onclick = () => { V.muted = !V.muted; paintVol(); };
$("vol").oninput = e => setVol(+e.target.value);
$("rate").onchange = e => {
  V.playbackRate = +e.target.value;
  localStorage.setItem("ts.rate", e.target.value);
};
$("pip").onclick = () => {
  if (document.pictureInPictureElement) document.exitPictureInPicture();
  else V.requestPictureInPicture().catch(() => {});
};
$("fs").onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else (V.requestFullscreen || V.webkitRequestFullscreen || (()=>{})).call(V);
};
$("copy").onclick = () => {
  navigator.clipboard.writeText(location.origin + "/stream")
    .then(() => { $("copy").textContent = "Copied";
                  setTimeout(() => $("copy").textContent = "Copy stream URL", 1400); });
};
$("conv").onclick = () => { V.src = "/transcode?t=" + Date.now(); V.play(); };

function paintVol() {
  $("mute").textContent = (V.muted || V.volume === 0) ? "muted" : "vol";
}
function posKey() { return "ts.pos." + (infohash || "x") + "." + lastFile; }

let lastSave = 0;
function tick() {
  $("clock").textContent = fmt(V.currentTime) + " / " + fmt(V.duration);
  const now = Date.now();
  if (playing && V.currentTime > 5 && now - lastSave > 5000) {
    lastSave = now;
    try { localStorage.setItem(posKey(), Math.floor(V.currentTime)); } catch {}
  }
}
V.addEventListener("timeupdate", tick);
V.addEventListener("durationchange", tick);
V.addEventListener("loadedmetadata", tick);
V.addEventListener("play",  () => $("pp").textContent = "Pause");
V.addEventListener("pause", () => $("pp").textContent = "Play");
V.addEventListener("volumechange", paintVol);
for (const e of ["waiting","seeking","stalled"])
  V.addEventListener(e, () => $("buffering").style.display = "inline");
for (const e of ["playing","canplay","seeked"])
  V.addEventListener(e, () => $("buffering").style.display = "none");

addEventListener("keydown", e => {
  const t = e.target;
  if (t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) return;
  if (!playing || e.ctrlKey || e.altKey || e.metaKey) return;
  const k = e.key;
  let hit = true;
  if (k === " " || k === "k") V.paused ? V.play() : V.pause();
  else if (k === "ArrowRight") nudge(e.shiftKey ? 30 : 5);
  else if (k === "ArrowLeft")  nudge(e.shiftKey ? -30 : -5);
  else if (k === "l") nudge(10);
  else if (k === "j") nudge(-10);
  else if (k === "ArrowUp")   setVol(V.volume + .05);
  else if (k === "ArrowDown") setVol(V.volume - .05);
  else if (k === "m") { V.muted = !V.muted; paintVol(); }
  else if (k === "f") $("fs").click();
  else if (k === "p") $("pip").click();
  else if (k === "<") bumpRate(-1);
  else if (k === ">") bumpRate(1);
  else if (k === ",") { V.pause(); nudge(-1/24); }
  else if (k === ".") { V.pause(); nudge(1/24); }
  else if (k >= "0" && k <= "9" && isFinite(V.duration)) V.currentTime = V.duration * (+k/10);
  else hit = false;
  if (hit) e.preventDefault();
});

/* restore per-viewer preferences; localStorage is absent in some contexts */
try {
  const v = localStorage.getItem("ts.vol");
  if (v !== null) { V.volume = +v; $("vol").value = v; }
  const r = localStorage.getItem("ts.rate");
  if (r !== null) { $("rate").value = r; V.playbackRate = +r; }
} catch {}
paintVol();

/* ---------- torrent controls ---------- */

$("go").onclick = async () => {
  const source = $("src").value.trim();
  if (!source) { show("Paste a magnet link or a .torrent file path first.", true); return; }
  reset();
  $("spin").style.display = "block";
  const r = await post("/api/add", {source, mode});
  if (!r.ok) { $("spin").style.display = "none"; show(r.error, true); }
  else $("halt").disabled = false;
};

$("halt").onclick = async () => {
  await post("/api/stop");
  reset(); $("halt").disabled = true;
};

$("files").onchange = async e => {
  await post("/api/select", {index: +e.target.value});
  playing = false; lastFile = null; clearSubs();
  for (const id of ["vid","pc","keys"]) $(id).style.display = "none";
};

function show(msg, bad) {
  const n = $("note");
  n.textContent = msg; n.className = bad ? "note bad" : "note"; n.style.display = "block";
}
function reset() {
  playing = false; lastFile = null;
  for (const id of ["map","legend","vid","files","after","note","spin","pc",
                    "keys","resume","peers","events","meta","tracks"])
    $(id).style.display = "none";
  clearInterval(peerTimer); peersOpen = false;
  $("pbox").style.display = "none"; $("ptoggle").textContent = "show";
  clearInterval(evTimer); evOpen = false; evBuf = []; evSeq = 0;
  $("ebox").style.display = "none"; $("etoggle").textContent = "show";
  metaOpen = false; metaLoaded = false;
  $("mbox").style.display = "none"; $("mtoggle").textContent = "show";
  clearSubs();
  $("stat").textContent = ""; $("title").textContent = "";
  V.removeAttribute("src"); V.load();
}

/* ---------- 1 Hz telemetry ---------- */

setInterval(async () => {
  let s;
  try { s = await (await fetch("/api/status")).json(); } catch { return; }

  if (s.mode && s.mode !== mode) { mode = s.mode; }
  paintMode();
  paintCaps(s.limits);
  renderAB(s.trials);

  if (s.state === "error") { $("spin").style.display = "none"; show(s.message, true); return; }
  if (s.state === "idle") return;
  if (s.state === "resolving") {
    $("spin").style.display = "block";
    $("spin").textContent = s.peers
      ? "Found " + s.peers + " peers, waiting for file list…" : "Looking for peers…";
    return;
  }

  $("spin").style.display = "none";
  $("map").style.display = "block";
  $("peers").style.display = "block";
  $("events").style.display = "block";
  $("meta").style.display = "block";
  $("legend").style.display = "flex";
  $("title").textContent = s.name;
  infohash = s.infohash || "";
  $("stat").innerHTML =
    "<span><b>" + s.progress + "%</b> of " + mb(s.size) + "</span>" +
    "<span>down <b>" + s.down + "</b> kB/s</span>" +
    "<span>up <b>" + s.up + "</b> kB/s</span>" +
    "<span><b>" + s.peers + "</b> peers, " + s.seeds + " seeding</span>";

  const cells = $("cells");
  if (cells.children.length !== s.map.cells.length)
    cells.innerHTML = "<i></i>".repeat(s.map.cells.length);
  s.map.cells.forEach((v, i) => cells.children[i].classList.toggle("on", !!v));

  // rarest-first has no head/tail pins and no fetch window to draw
  $("pinh").style.display = $("pint").style.display = s.map.pins ? "block" : "none";
  $("pinh").style.width = (s.map.head * 100) + "%";
  $("pint").style.width = (s.map.tail * 100) + "%";
  if (s.map.window) {
    $("zone").style.display = "block";
    $("zone").style.left  = (s.map.window.start * 100) + "%";
    $("zone").style.width = ((s.map.window.end - s.map.window.start) * 100) + "%";
  } else {
    $("zone").style.display = "none";
  }

  if (s.files.length > 1) {
    const sel = $("files");
    if (sel.options.length !== s.files.length)
      sel.innerHTML = s.files.map(f =>
        '<option value="' + f.index + '">' + f.name + " — " + mb(f.size) + "</option>").join("");
    sel.value = s.selected; sel.style.display = "block";
  }

  if (!playing || lastFile !== s.selected) {
    playing = true; lastFile = s.selected;
    V.style.display = "block";
    V.src = "/stream?t=" + Date.now();
    $("pc").style.display = "flex";
    $("keys").style.display = "block";
    $("after").style.display = "flex";
    $("url").textContent = location.origin + "/stream";
    $("conv").style.display = s.ffmpeg ? "inline-block" : "none";
    clearInterval(subTimer);
    pollSubs(); subTimer = setInterval(pollSubs, 3000);
    clearInterval(mediaTimer); mediaDone = false;
    pollMedia(); mediaTimer = setInterval(pollMedia, 3000);

    let saved = 0;
    try { saved = +(localStorage.getItem(posKey()) || 0); } catch {}
    const rb = $("resume");
    if (saved > 15) {
      rb.textContent = "Resume at " + fmt(saved);
      rb.style.display = "inline-block";
      // seeking here pulls the window deep into the file, so make it deliberate
      rb.onclick = () => { V.currentTime = saved; V.play(); rb.style.display = "none"; };
    } else rb.style.display = "none";

    if (!s.playable) {
      show(s.ffmpeg
        ? "Browsers can't decode this container. Press convert, or open the address above in VLC."
        : "Browsers can't decode this container. Open the address above in VLC or mpv.", false);
    }
  }
}, 1000);

paintMode();

/* ---------- bandwidth caps ---------- */

/* libtorrent reads a rate limit of 0 as "unlimited", so the only way to say
   "stop" is a rate so low nothing fits through it. 1 B/s is that. */
const CAPS = [[0,"unlimited"],[4000000,"4 MB/s"],[1000000,"1 MB/s"],
              [250000,"250 kB/s"],[60000,"60 kB/s"],[1,"off"]];

for (const id of ["ldown","lup"]) {
  $(id).innerHTML = CAPS.map(c => '<option value="' + c[0] + '">' + c[1] + "</option>").join("");
  $(id).onchange = async () => {
    const r = await post("/api/limits", {down: +$("ldown").value, up: +$("lup").value});
    paintCaps(r.limits);
  };
}
function paintCaps(l) {
  if (!l) return;
  for (const [id, v] of [["ldown", l.down], ["lup", l.up]]) {
    if (document.activeElement !== $(id)) $(id).value = String(v);
    $(id).classList.toggle("capped", v !== 0);
  }
}

/* ---------- swarm ---------- */

const rate = b => b >= 1000000 ? (b/1000000).toFixed(1) + " MB/s"
  : b >= 1000 ? (b/1000).toFixed(0) + " kB/s" : b ? b + " B/s" : "·";

let peersOpen = false, peerTimer = null;

$("ptoggle").onclick = () => {
  peersOpen = !peersOpen;
  $("pbox").style.display = peersOpen ? "block" : "none";
  $("ptoggle").textContent = peersOpen ? "hide" : "show";
  clearInterval(peerTimer);
  // its own poll, slower than /api/status, and stopped entirely when collapsed:
  // get_peer_info() walks every open connection
  if (peersOpen) { pollPeers(); peerTimer = setInterval(pollPeers, 2000); }
};

/* Which way bytes can actually move. libtorrent names flags from our side:
   `choked` is us choking them, `remote_choked` is them choking us. */
function flowCell(f) {
  let down = "", up = "";
  if (f.interesting) down = f.remote_choked ? "choked" : "open";
  if (f.remote_interested) up = f.choked ? "choked"
                              : (f.optimistic_unchoke ? "opt" : "open");
  return '<span class="flow" title="' + flowTitle(f) + '">'
       + '<u>&#8595;</u><i class="' + down + '"></i>'
       + '<u>&#8593;</u><i class="' + up + '"></i></span>';
}
function flowTitle(f) {
  const a = f.interesting
    ? (f.remote_choked ? "we want pieces, they are choking us"
                       : "they are sending to us")
    : "we want nothing from them";
  const b = f.remote_interested
    ? (f.choked ? "they want pieces, we are choking them"
                : (f.optimistic_unchoke ? "optimistic unchoke - a free trial slot"
                                        : "we are sending to them"))
    : "they want nothing from us";
  return "down: " + a + "\nup: " + b;
}

function tags(p) {
  const t = [], f = p.flags;
  if (f.seed) t.push(['good', 'seed']);
  if (f.optimistic_unchoke) t.push(['hot', 'optimistic']);
  if (f.snubbed) t.push(['hot', 'snubbed']);
  if (f.endgame) t.push(['hot', 'endgame']);
  if (f.on_parole) t.push(['bad', 'parole']);
  if (p.hashfails) t.push(['bad', p.hashfails + ' hashfail']);
  if (f.connecting || f.handshake) t.push(['', 'connecting']);
  if (f.encrypted) t.push(['', 'enc']);
  // what libtorrent thinks we would have to upload to get unchoked here
  if (f.interesting && f.remote_choked && p.recip) t.push(['', 'needs ' + rate(p.recip)]);
  return t.map(x => '<span class="tag ' + x[0] + '">' + x[1] + "</span>").join("");
}

async function pollPeers() {
  let d;
  try { d = await (await fetch("/api/peers")).json(); } catch { return; }
  const t = d.totals || {};
  $("ptot").innerHTML = !d.peers.length ? "" :
    "<span><b>" + t.connected + "</b> connected</span>" +
    "<span><b>" + t.seeds + "</b> seeds</span>" +
    "<span><b>" + t.feeding_us + "</b> unchoked us</span>" +
    "<span><b>" + t.fed_by_us + "</b> we unchoked</span>" +
    (t.snubbed ? "<span><b>" + t.snubbed + "</b> snubbed</span>" : "");

  $("pbody").innerHTML = !d.peers.length
    ? '<tr><td class="empty" colspan="8">No peers connected.</td></tr>'
    : d.peers.map(p =>
        "<tr>" +
        '<td class="name">' + p.ip + "</td>" +
        "<td>" + esc(p.client).slice(0, 26) + "</td>" +
        "<td>" + (p.source.join(" ") || "·") + "</td>" +
        '<td class="c">' + flowCell(p.flags) + "</td>" +
        '<td class="r">' + rate(p.down) + "</td>" +
        '<td class="r">' + rate(p.up) + "</td>" +
        '<td class="r">' + p.progress + "%</td>" +
        "<td>" + tags(p) + "</td>" +
        "</tr>").join("");

  $("pnote").innerHTML = !d.peers.length ? "" :
    "Teal means bytes can move. Amber outline means one side wants and the other is "
    + "refusing &mdash; that is a choke. Amber filled on the up bar is an "
    + "<b>optimistic unchoke</b>: a free slot BitTorrent hands out at random so a peer "
    + "with nothing to trade can still get started.<br>"
    + "Tit-for-tat only shows up between <b>leechers</b>. A seed has everything, wants "
    + "nothing back, and unchokes whoever it likes &mdash; so on a well-seeded torrent "
    + "you will sit at <b>0 we unchoked</b> and still download fine. To see reciprocity "
    + "bite, find a torrent with few seeds and many leechers, cap your upload to "
    + "<b>off</b>, and watch the down bars fall back to outlines.";
}
const esc = s => String(s).replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));

/* ---------- protocol event log ---------- */

const BANDS = [["all","everything"],["piece","pieces landing"],["peer","peer churn"],
               ["tracker","tracker"],["dht","DHT"],["torrent","milestones"],
               ["error","errors"],["corrupt","corrupt pieces"]];
const LOG_KEEP = 600;          // client-side cap; the server ring buffer is its own

let evOpen = false, evTimer = null, evSeq = 0, evBuf = [], evBand = "all";

$("efilter").innerHTML = BANDS.map(b =>
  '<button data-band="' + b[0] + '"' + (b[0] === "all" ? ' class="on"' : "") + ">"
  + b[1] + '<span class="k" id="k-' + b[0] + '"></span></button>').join("");

for (const b of $("efilter").querySelectorAll("button"))
  b.onclick = () => {
    evBand = b.dataset.band;
    for (const o of $("efilter").querySelectorAll("button"))
      o.classList.toggle("on", o === b);
    drawLog();
  };

$("etoggle").onclick = () => {
  evOpen = !evOpen;
  $("ebox").style.display = evOpen ? "block" : "none";
  $("etoggle").textContent = evOpen ? "hide" : "show";
  clearInterval(evTimer);
  if (evOpen) { pollEvents(); evTimer = setInterval(pollEvents, 1500); }
};

$("eclear").onclick = async () => {
  await post("/api/events/clear");
  evBuf = []; evSeq = 0; drawLog();
};

const hhmmss = t => new Date(t * 1000).toTimeString().slice(0, 8);

async function pollEvents() {
  let d;
  try { d = await (await fetch("/api/events?since=" + evSeq)).json(); } catch { return; }
  evSeq = d.seq;

  if (d.dropped) evBuf.push({gap: d.dropped + " events rolled out of the buffer"});
  evBuf = evBuf.concat(d.events).slice(-LOG_KEEP);

  // cumulative session totals, not just what is still in the buffer
  const bands = d.bands || {};
  const all = Object.values(bands).reduce((a, b) => a + b, 0);
  for (const [band] of BANDS) {
    const el = $("k-" + band);
    if (el) el.textContent = (band === "all" ? all : bands[band]) || "";
  }

  // a hash failure means a peer sent bytes that did not match the torrent's own
  // SHA-1 for that piece, so libtorrent threw the whole piece away and refetched
  $("loud").style.display = d.hash_fails ? "block" : "none";
  if (d.hash_fails)
    $("loud").innerHTML = "<b>" + d.hash_fails + " piece"
      + (d.hash_fails > 1 ? "s" : "") + " failed the hash check and were thrown away.</b>"
      + "<span>A peer sent bytes that did not match the SHA-1 in the torrent file. "
      + "libtorrent discards the whole piece and refetches it, and puts that peer "
      + "on parole. This is the integrity guarantee doing its job.</span>";

  $("estat").textContent = d.buffer + " in server buffer · " + evBuf.length
    + " shown" + (d.trimmed ? " · " + d.trimmed + " trimmed this poll" : "");
  drawLog();
}

function drawLog() {
  const box = $("log");
  // only pin to the bottom if the reader is already there, otherwise scrolling
  // back through the log fights the 1.5s refresh
  const pinned = $("efollow").checked &&
    (box.scrollTop + box.clientHeight >= box.scrollHeight - 40);

  box.innerHTML = evBuf
    .filter(e => e.gap || evBand === "all" || e.cat === evBand)
    .map(e => e.gap
      ? '<div class="gap">— ' + e.gap + " —</div>"
      : '<div class="' + e.cat + '"><t>' + hhmmss(e.t) + "</t>  <w>"
        + e.what + "</w>" + esc(e.msg) + "</div>")
    .join("");

  if (pinned) box.scrollTop = box.scrollHeight;
}

/* ---------- subtitles ---------- */

const subTracks = new Map();          // torrent file index -> TextTrack
let subTimer = null;

function clearSubs() {
  clearInterval(subTimer); subTimer = null;
  clearInterval(mediaTimer); mediaTimer = null; mediaDone = false;
  subTracks.clear();
  for (const t of [...V.querySelectorAll("track")]) t.remove();
  $("subsel").innerHTML = '<option value="">subs off</option>';
  $("subsel").classList.remove("on");
  $("subwrap").style.display = "none";
}

$("subsel").onchange = e => {
  const want = e.target.value;
  for (const t of subTracks.values()) t.mode = "disabled";
  const t = subTracks.get(+want);
  if (t) t.mode = "showing";
  e.target.classList.toggle("on", !!want);
  try { localStorage.setItem("ts.sub", want); } catch {}
};

async function pollSubs() {
  let d;
  try { d = await (await fetch("/api/subs")).json(); } catch { return; }
  if (!d.subs.length) { clearInterval(subTimer); subTimer = null; return; }

  let pending = 0;
  for (const s of d.subs) {
    if (!s.ready || !s.supported) { if (s.supported) pending++; continue; }
    if (subTracks.has(s.index)) continue;

    const t = document.createElement("track");
    t.kind = "subtitles";
    t.label = s.name.replace(/\.[^.]+$/, "");
    if (s.lang) t.srclang = s.lang;
    t.src = "/subs/" + s.index;
    V.appendChild(t);
    subTracks.set(s.index, t.track);

    const o = document.createElement("option");
    o.value = s.index;
    o.textContent = (s.lang ? s.lang + " · " : "") + t.label;
    $("subsel").appendChild(o);
    $("subwrap").style.display = "inline-block";

    let saved = null;
    try { saved = localStorage.getItem("ts.sub"); } catch {}
    if (saved === String(s.index)) { $("subsel").value = saved; $("subsel").onchange(
      {target: $("subsel")}); }
  }
  // subtitle files are prioritised to 7 and are only a few kB, so this settles
  // within seconds; stop polling once nothing is left to wait for
  if (!pending) { clearInterval(subTimer); subTimer = null; }
}

/* ---------- metadata inspector ---------- */

let metaOpen = false, metaLoaded = false;

$("mtoggle").onclick = () => {
  metaOpen = !metaOpen;
  $("mbox").style.display = metaOpen ? "block" : "none";
  $("mtoggle").textContent = metaOpen ? "hide" : "show";
  // metadata never changes once resolved, so fetch it once rather than polling
  if (metaOpen && !metaLoaded) loadMeta();
};

async function loadMeta() {
  let m;
  try { m = await (await fetch("/api/metadata")).json(); } catch { return; }
  if (!m.ok) return;
  metaLoaded = true;

  $("derive").className = "derive" + (m.matches ? "" : " bad");
  $("derive").innerHTML =
    '<div><span class="l">info dictionary</span><span class="op">'
      + m.info_bytes.toLocaleString() + " bytes, exactly as received</span></div>"
    + '<div><span class="l">SHA-1 of those bytes</span><span class="h">'
      + m.derived + "</span></div>"
    + '<div><span class="l">infohash in use</span><span class="h">'
      + m.infohash + "</span></div>"
    + "<p>" + (m.matches
      ? "These match because the infohash <b>is</b> that SHA-1 &mdash; it is not stored "
        + "anywhere in the torrent, it is derived from the info dictionary every time. "
        + "Change one byte in there and it becomes a different torrent that no peer in "
        + "this swarm will talk to you about."
      : "These do not match, which should be impossible. Treat this torrent as suspect.")
    + "</p>";

  const facts = [
    ["piece length", mbx(m.piece_length)],
    ["pieces", m.num_pieces.toLocaleString()
      + ' <span class="u">&times; 20-byte SHA-1 = ' + mbx(m.num_pieces * 20)
      + " of hashes</span>"],
    ["total size", mbx(m.total_size)],
    ["files", m.num_files],
    ["private", m.private ? "yes — DHT and PEX disabled" : "no"],
    ["created by", m.creator || "&mdash;"],
    ["created", m.created ? new Date(m.created * 1000).toISOString().slice(0, 16)
                             .replace("T", " ") : "&mdash;"],
    ["comment", esc(m.comment) || "&mdash;"],
  ];
  $("facts").innerHTML = facts
    .map(f => "<tr><td>" + f[0] + "</td><td>" + f[1] + "</td></tr>").join("");

  $("trackers").innerHTML = !m.tiers.length ? "" :
    m.tiers.map(t => '<div class="tier"><b>tier ' + t.tier + "</b> — tried in order, "
      + "first one that answers wins</div>"
      + t.urls.map(u => "<a>" + esc(u) + "</a>").join("")).join("");

  $("tree").innerHTML = bnode(m.tree);
  $("mnote").innerHTML = m.origin === "magnet"
    ? "This torrent arrived as a <b>magnet link</b>, so only the info dictionary "
      + "exists to decode. Announce URLs, creation date, comment and created-by live "
      + "in the <i>outer</i> dictionary of a .torrent file and never travelled — the "
      + "trackers above came from the magnet's own <code>&amp;tr=</code> parameters."
    : "Decoded from the .torrent file on disk, outer dictionary and all.";
}

function bnode(n) {
  const k = n.k === "" ? '""' : n.k;
  if (n.t === "dict" || n.t === "list") {
    return "<details" + (n.open ? " open" : "") + "><summary><k>" + esc(k)
      + "</k><em>" + n.t + ", " + n.n + (n.t === "dict" ? " keys" : " items")
      + "</em></summary><div class='kids'>"
      + (n.c || []).map(bnode).join("") + "</div></details>";
  }
  let v, cls = n.t;
  if (n.t === "int") v = "<n>" + n.v.toLocaleString() + "</n>";
  else if (n.t === "pieces")
    v = "<v>" + n.n.toLocaleString() + " SHA-1 hashes, " + mbx(n.bytes) + "</v>"
      + "<em>" + n.first.slice(0, 12) + "… … " + n.last.slice(0, 12) + "…</em>";
  else if (n.t === "blob")
    v = "<v>" + n.bytes.toLocaleString() + " bytes</v><em>" + n.hex + "…</em>";
  else
    v = "<v>" + esc(n.v) + "</v>"
      + (n.bytes > 40 ? "<em>" + n.bytes + " bytes</em>" : "");
  return "<div class='row " + cls + "'><k>" + esc(k) + "</k>" + v + "</div>";
}

/* ---------- what is inside the file ---------- */

let mediaTimer = null, mediaDone = false;

async function pollMedia() {
  let m;
  try { m = await (await fetch("/api/media")).json(); } catch { return; }
  if (!m.ready) return;                       // header not on disk yet, keep trying
  clearInterval(mediaTimer); mediaTimer = null; mediaDone = true;

  const rows = m.tracks.map(t => {
    const ok = t.ok ? "yes" : "no";
    const verdict = t.type === "subtitle"
      ? "embedded — needs extracting, not a &lt;track&gt; file"
      : (t.ok ? "browsers decode this" : "no browser decodes this");
    return '<div class="t ' + ok + '"><span class="k">' + t.type + '</span>'
      + '<span class="c">' + t.codec.toUpperCase() + "</span>"
      + '<span class="v">' + verdict
      + (t.lang && t.lang !== "und" ? " · " + t.lang : "")
      + (t.name ? " · " + esc(t.name) : "") + "</span></div>";
  }).join("");

  let fix = "";
  if (!m.playable) {
    fix = '<div class="fix"><b>Why you are getting no sound:</b> ' + m.why + ". ";
    fix += m.ffmpeg
      ? "Press <b>Convert for browser</b> — the video is copied through untouched "
        + "and only the audio is re-encoded, so it keeps up with playback."
      : "Install ffmpeg and restart this app to enable the convert button: "
        + "<code>winget install Gyan.FFmpeg</code> — or just open the stream URL "
        + "below in <b>VLC</b> or <b>mpv</b>, which play everything.";
    if (m.sub_tracks)
      fix += " The " + m.sub_tracks + " subtitle track"
        + (m.sub_tracks > 1 ? "s are" : " is") + " <b>inside</b> the video file, not "
        + "separate .srt files, so the subtitle picker cannot see "
        + (m.sub_tracks > 1 ? "them" : "it")
        + (m.ffmpeg ? " until the download finishes." : " without ffmpeg either.");
    fix += "</div>";
  }
  $("tracks").innerHTML = '<div class="k" style="color:var(--ink-faint);'
    + 'margin-bottom:4px">' + m.container + " container</div>" + rows + fix;
  $("tracks").style.display = "block";
}
