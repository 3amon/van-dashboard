/* VanDash core — data polling, device helpers, chart primitives.
   Views are thin render layers: register with VanDash.onData(render). */
"use strict";
const VanDash = (() => {
  const POLL_MS = 2000;
  // Advert catch rate drops while the heater holds a GATT session — gaps of
  // ~2 min are normal, so the stale cutoff sits well above them.
  const STALE_S = 240;
  let last = null;
  const listeners = [];

  function statusChip(label, level) {
    const chip = document.createElement("span");
    chip.className = "connection " + level;
    const dot = document.createElement("i");
    chip.append(dot, document.createTextNode(label));
    return chip;
  }

  function renderConnections(statusEl, state) {
    statusEl.className = "connections";
    statusEl.replaceChildren();
    if (state.demo) statusEl.append(statusChip("demo", "yellow"));
    if (state.mirror) {
      // Served from the always-on mirror: say how fresh the van's last push is.
      document.documentElement.classList.add("mirror");
      const ago = state.mirror.last_push_ts == null ? null : state.now - state.mirror.last_push_ts;
      const label = ago == null ? "van: never seen" : ago < 90 ? "van live"
        : ago < 3600 ? `van seen ${Math.round(ago / 60)} min ago`
        : `van seen ${(ago / 3600).toFixed(1)} h ago`;
      statusEl.append(statusChip(label, ago != null && ago < 180 ? "green" : ago != null && ago < 3600 ? "yellow" : "red"));
    }
    const pi = state.pi;
    if (pi?.wifi_ssid) {
      const signal = pi.wifi_dbm;
      const level = signal == null || signal >= -67 ? "green" : signal >= -78 ? "yellow" : "red";
      statusEl.append(statusChip(pi.wifi_ssid, level));
      const internetLevel = pi.internet_ok === true ? "green"
        : pi.internet_ok === false ? "red" : "yellow";
      const internet = statusChip("Internet", internetLevel);
      if (pi.internet_latency_ms != null) internet.title = `${pi.internet_latency_ms} ms`;
      statusEl.append(internet);
    }
    for (const [name, device] of Object.entries(state.devices)) {
      const seconds = state.now - device.ts;
      // Epoch is intentionally GATT-polled about every five minutes and can
      // advertise less often near its current deadband. Judge it by its
      // established 45-minute stale limit, not Victron advert cadence.
      const greenAfter = device.kind === "battery" ? 10 * 60 : 60;
      const redAfter = device.kind === "battery" ? 45 * 60 : STALE_S;
      const level = seconds < greenAfter ? "green" : seconds < redAfter ? "yellow" : "red";
      statusEl.append(statusChip(name, level));
    }
    if (!statusEl.children.length) statusEl.append(statusChip("scanning", "yellow"));
  }

  // Public-tunnel visitors (van.example.net) are view-only: no sleep button,
  // no wake-tap overlay — those poke the panel in the actual van. The server
  // refuses /api/sleep for tunnel traffic too; this just hides the controls.
  const REMOTE = location.hostname.endsWith("example.net");

  // Kiosk flag: the Pi panel first loads with ?kiosk; remember it (localStorage)
  // so it survives view switches. A laptop never sets it, so it keeps its cursor.
  if (new URLSearchParams(location.search).has("kiosk"))
    localStorage.setItem("vandash-kiosk", "1");
  if (localStorage.getItem("vandash-kiosk") === "1")
    document.documentElement.classList.add("kiosk");

  /* ---------- data ---------- */
  async function poll() {
    const statusEl = document.getElementById("status");
    try {
      const res = await fetch("/api/state");
      last = await res.json();
      if (statusEl) renderConnections(statusEl, last);
      for (const fn of listeners) fn(last);
      syncSleep();
      syncDish();
    } catch {
      if (statusEl) {
        statusEl.className = "connections";
        statusEl.replaceChildren(statusChip("dashboard", "red"));
      }
    }
  }

  function onData(fn) {
    listeners.push(fn);
    if (last) fn(last);
  }

  async function fetchDay(daysAgo = 0, bucket = 300) {
    const res = await fetch(`/api/day?days_ago=${daysAgo}&bucket=${bucket}`);
    return res.json();
  }

  async function fetchSolarMax(days = 7) {
    const res = await fetch(`/api/solar-max?days=${days}`);
    return res.json();
  }

  // Replay mode: ?replay=<unix_ts> pins time-series charts to a historical
  // window ending at that timestamp (for demos/review). Views opt in.
  const replayEnd = (() => {
    const v = new URLSearchParams(location.search).get("replay");
    return v ? Number(v) : null;
  })();

  /* ---------- helpers ---------- */
  const fmt = (v, digits = 1) => (v == null ? "—" : Number(v).toFixed(digits));
  // Devices speak °C; cabin temps display in °F
  const degF = c => (c == null ? null : c * 9 / 5 + 32);
  const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function findDevice(kind) {
    if (!last) return null;
    for (const [name, d] of Object.entries(last.devices)) {
      if (d.kind === kind) return { name, ...d };
    }
    return null;
  }

  // Victron charge/device states are headline text in the tiles, and the long
  // ones ("Absorption", "Battery Safe") overflow into the neighboring tile at
  // headline font size — display the short form wherever a state is the value.
  const SHORT_STATE = {
    "Absorption": "Abs", "Repeated Absorption": "Abs", "Battery Safe": "Safe",
    "Storage": "Store", "Equalize Manual": "Eqlz", "Auto Equalize": "Eqlz",
    "External Control": "Ext", "Low Power": "Low", "Starting Up": "Start",
    "Power Supply": "PSU", "Not Charging": "Idle", "Recondition": "Recon",
    "Inverting": "Invert",
  };
  const shortState = s => (s == null ? s : SHORT_STATE[s] ?? s);

  const age = d => (d && last ? last.now - d.ts : Infinity);
  // The Epoch BMS is GATT-polled only when its sparse adverts are caught, so
  // readings an age that would mean "dead" for a Victron are normal for it.
  const STALE_BY_KIND = { battery: 2700 };
  const isStale = d => age(d) > (STALE_BY_KIND[d?.kind] ?? STALE_S);

  function battStatus(volts, amps) {
    // 24V LiFePO4 — voltage is a rough proxy only (no shunt installed)
    if (volts == null) return ["", "no data"];
    if (amps > 0.1 && volts > 26.5) return ["good", "charging · " + fmt(amps) + " A"];
    if (volts < 24.8) return ["critical", "very low"];
    if (volts < 25.6) return ["warning", "low"];
    return ["good", "ok" + (amps > 0.1 ? " · charging " + fmt(amps) + " A" : "")];
  }

  function setBadge(badgeEl, textEl, level, text) {
    badgeEl.className = "badge " + (level || "");
    textEl.textContent = text;
  }

  /* ---------- svg ---------- */
  const svgNS = "http://www.w3.org/2000/svg";
  function el(tag, attrs = {}) {
    const node = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  }

  /* ---------- line chart (time series with grid, hover, gaps) ----------
     makeChart({svg, wrap, tip, getData, getDomain, opts}) -> {redraw()}
     rows: [{t, <field>}]; opts: {field, color, yFloor, ySnap, xTickS, gapS,
     showGrid, endDot, areaOpacity, labelFmt(row)} */
  function makeChart(cfg) {
    const { svg, wrap, tip } = cfg;
    const o = Object.assign(
      { field: "w", color: "--series", yFloor: 50, ySnap: 50, xTickS: 1800,
        showGrid: true, endDot: true, areaOpacity: 0.1, pad: null,
        labelFmt: r => `<b>${Math.round(r[cfg.opts.field || "w"])}</b>` },
      cfg.opts || {}
    );
    let points = [];
    let area = null;
    let hoverLine = null;

    function redraw() {
      const { width: W, height: H } = svg.getBoundingClientRect();
      if (W < 50 || H < 50) return;
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
      svg.replaceChildren();
      hoverLine = null;

      const pad = o.pad || { left: 44, right: 16, top: 8, bottom: 22 };
      const x0 = pad.left, x1 = W - pad.right, y0 = H - pad.bottom, y1 = pad.top;
      area = { left: x0, right: x1, top: y1, bottom: y0 };

      const [tMin, tMax] = cfg.getDomain();
      const rows = cfg.getData().filter(r => r.t >= tMin && r.t <= tMax && r[o.field] != null);
      let vMax = Math.max(o.yFloor, ...rows.map(r => r[o.field]));
      if (o.scaleOn)  // include a secondary series (drawn via decorate) in the y-scale
        for (const r of cfg.getData()) {
          if (r.t < tMin || r.t > tMax) continue;
          const v = o.scaleOn(r);
          if (v != null && v > vMax) vMax = v;
        }
      const yTop = Math.ceil(vMax / o.ySnap) * o.ySnap;

      const X = t => x0 + ((t - tMin) / (tMax - tMin)) * (x1 - x0);
      const Y = v => y0 - (v / yTop) * (y0 - y1);

      if (o.showGrid) {
        // Adaptive tick spacing: appliance spikes can push yTop to a couple
        // of kW — keep the label count sane at any scale.
        let yStep = o.ySnap / 2;
        while (yTop / yStep > 7) yStep *= 2;
        for (let v = 0; v <= yTop; v += yStep) {
          svg.append(el("line", { x1: x0, x2: x1, y1: Y(v), y2: Y(v),
            stroke: v === 0 ? cssVar("--baseline") : cssVar("--grid"), "stroke-width": 1 }));
          if (v > 0) {
            const lab = el("text", { x: x0 - 8, y: Y(v) + 4, "text-anchor": "end",
              fill: cssVar("--muted"), "font-size": 11 });
            lab.textContent = v;
            svg.append(lab);
          }
        }
        const xTicks = o.xTicks
          ? o.xTicks(cfg.getData(), tMin, tMax)
          : (() => {
              const ticks = [];
              // Phone-width charts can't fit the panel's tick density.
              const step = o.xTickS * (W < 600 ? 2 : 1);
              for (let t = Math.ceil(tMin / step) * step; t <= tMax; t += step) ticks.push(t);
              return ticks;
            })();
        for (const t of xTicks) {
          const lab = el("text", { x: X(t), y: y0 + 16, "text-anchor": "middle",
            fill: cssVar("--muted"), "font-size": 11 });
          lab.textContent = o.xLabelFmt
            ? o.xLabelFmt(t)
            : new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
          svg.append(lab);
        }
      } else {
        svg.append(el("line", { x1: x0, x2: x1, y1: y0, y2: y0,
          stroke: cssVar("--baseline"), "stroke-width": 1 }));
      }

      if (!rows.length) {
        const note = el("text", { x: (x0 + x1) / 2, y: (y0 + y1) / 2, "text-anchor": "middle",
          fill: cssVar("--muted"), "font-size": 13 });
        note.textContent = "collecting data…";
        svg.append(note);
        points = [];
        return;
      }

      const gap = (o.gapS || ((last && last.bucket_seconds) || 30) * 3);
      const segments = [[]];
      for (const r of rows) {
        const seg = segments[segments.length - 1];
        if (seg.length && r.t - seg[seg.length - 1].t > gap) segments.push([r]);
        else seg.push(r);
      }

      points = rows.map(r => ({ x: X(r.t), y: Y(r[o.field]), row: r }));

      const color = cssVar(o.color);
      if (!o.hideSeries)
        for (const seg of segments) {
          if (seg.length < 2) continue; // a lone bucket is carried by the end dot
          const pts = seg.map(r => `${X(r.t)},${Y(r[o.field])}`).join(" ");
          if (o.areaOpacity > 0)
            svg.append(el("polygon", {
              points: `${X(seg[0].t)},${y0} ${pts} ${X(seg[seg.length - 1].t)},${y0}`,
              fill: color, opacity: o.areaOpacity }));
          svg.append(el("polyline", { points: pts, fill: "none", stroke: color,
            "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
        }
      if (o.endDot && points.length) {
        const p = points[points.length - 1];
        svg.append(el("circle", { cx: p.x, cy: p.y, r: 4, fill: color,
          stroke: cssVar("--surface"), "stroke-width": 2 }));
      }
      if (o.pointRadius)
        for (const p of points)
          svg.append(el("circle", { cx: p.x, cy: p.y, r: o.pointRadius,
            fill: color, stroke: cssVar("--surface"), "stroke-width": 2 }));
      if (o.decorate) o.decorate(svg, { X, Y, x0, x1, y0, y1, yTop, rows });
    }

    if (wrap && tip) {
      wrap.addEventListener("mousemove", ev => {
        if (!points.length || !area) return;
        const rect = wrap.getBoundingClientRect();
        const mx = ev.clientX - rect.left;
        if (mx < area.left || mx > area.right) return hideHover();
        let best = points[0];
        for (const p of points) if (Math.abs(p.x - mx) < Math.abs(best.x - mx)) best = p;
        if (hoverLine) hoverLine.remove();
        hoverLine = el("line", { x1: best.x, x2: best.x, y1: area.top, y2: area.bottom,
          stroke: cssVar("--muted"), "stroke-width": 1 });
        svg.append(hoverLine);
        tip.style.display = "block";
        const timeLabel = o.tipTimeFmt
          ? o.tipTimeFmt(best.row)
          : new Date(best.row.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        tip.innerHTML = o.labelFmt(best.row) + " · " + timeLabel;
        tip.style.left = Math.min(best.x + 12, rect.width - tip.offsetWidth - 4) + "px";
        tip.style.top = Math.max(best.y - 34, 0) + "px";
      });
      wrap.addEventListener("mouseleave", hideHover);
    }
    function hideHover() {
      if (hoverLine) { hoverLine.remove(); hoverLine = null; }
      if (tip) tip.style.display = "none";
    }

    window.addEventListener("resize", () => last && redraw());
    return { redraw };
  }

  /* ---------- sky: daylight-cycle background (hour -> [top, bottom]) ---------- */
  const SKY = [
    [0.0,  "#0a0e1c", "#0c0c0e"],
    [4.5,  "#0a0e1c", "#0c0c0e"],
    [6.0,  "#232048", "#131019"],
    [7.5,  "#54324a", "#1c1520"],   // dawn
    [9.0,  "#2c3d5c", "#171c26"],
    [13.0, "#2f4a70", "#1a2231"],   // midday
    [17.5, "#2f4a70", "#1a2231"],
    [19.5, "#5c4126", "#211a16"],   // golden hour
    [21.0, "#2b2140", "#141220"],   // dusk
    [22.5, "#0a0e1c", "#0c0c0e"],
    [24.0, "#0a0e1c", "#0c0c0e"],
  ];
  const hex2rgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
  const lerpHex = (a, b, f) => "#" + hex2rgb(a).map((v, i) =>
    Math.round(v + (hex2rgb(b)[i] - v) * f).toString(16).padStart(2, "0")).join("");
  function paintSky() {
    const d = new Date();
    const h = d.getHours() + d.getMinutes() / 60;
    let i = 0;
    while (i < SKY.length - 2 && SKY[i + 1][0] <= h) i++;
    const [h0, top0, bot0] = SKY[i], [h1, top1, bot1] = SKY[i + 1];
    const f = Math.min(1, Math.max(0, (h - h0) / (h1 - h0 || 1)));
    document.documentElement.style.setProperty("--sky-top", lerpHex(top0, top1, f));
    document.documentElement.style.setProperty("--sky-bottom", lerpHex(bot0, bot1, f));
  }
  function enableSky() {
    document.body.classList.add("sky");
    paintSky();
    setInterval(paintSky, 60000);
  }

  /* ---------- bedtime ----------
     🌙 pill → POST /api/sleep: the server kills the panel backlight and arms
     a wake at [server].wake_hour. The overlay swallows the wake-up tap (and
     stands in for darkness on machines without a backlight device). State is
     server-side, so a kiosk reload while asleep stays asleep. */
  let sleepOverlay = null;
  async function setSleep(sleep) {
    try {
      await fetch("/api/sleep", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sleep }),
      });
      await poll();
    } catch { /* next poll will resync */ }
  }
  function syncSleep() {
    if (sleepOverlay)
      sleepOverlay.classList.toggle("on", !!(last && last.sleep_until));
  }

  /* ---------- Starlink "stay on" pill ----------
     The dish is duty-cycled from state of charge (vandash/starlink.py). The
     pill shows what the relay is doing and, tapped, holds it on for 12 hours;
     tapped again it cancels the hold. Only the kiosk/LAN can use it. */
  const HOLD_HOURS = 12;
  let dishPill = null;
  function fmtClock(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  function syncDish() {
    if (!dishPill) return;
    const sl = last && last.starlink;
    dishPill.hidden = !sl;
    if (!sl) return;
    const on = sl.relay_on !== false;
    let text;
    if (sl.hold_until) text = `Dish on until ${fmtClock(sl.hold_until)} · tap to cancel`;
    else if (sl.phase === "saving" && !sl.want_on && sl.next_window_ts)
      text = `Dish off · next ${fmtClock(sl.next_window_ts)} · Stay on ${HOLD_HOURS} h`;
    else if (sl.mode !== "auto") text = `Dish ${sl.mode} (manual) · Stay on ${HOLD_HOURS} h`;
    else text = `Dish ${on ? "on" : "off"} · Stay on ${HOLD_HOURS} h`;
    dishPill.textContent = text;
    dishPill.classList.toggle("held", !!sl.hold_until);
    dishPill.classList.toggle("off", !on);
    dishPill.title = sl.reason || "";
  }
  async function toggleHold() {
    const sl = last && last.starlink;
    const body = sl && sl.hold_until ? { hold: false } : { hold_hours: HOLD_HOURS };
    try {
      await fetch("/api/starlink", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await poll();
    } catch { /* next poll will resync */ }
  }

  /* ---------- chrome ---------- */
  function startClock(id) {
    const node = document.getElementById(id);
    if (!node) return;
    const tick = () => {
      node.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    };
    setInterval(tick, 5000);
    tick();
  }

  const VIEWS = [["sky", "Sky"], ["flow", "Flow"], ["day", "Day"]];
  function injectSwitcher(current) {
    const nav = document.createElement("nav");
    nav.className = "switcher";
    for (const [slug, label] of VIEWS) {
      const a = document.createElement("a");
      a.href = "/v/" + slug;
      a.textContent = label;
      if (slug === current) a.className = "active";
      nav.append(a);
    }
    document.body.append(nav);
    if (REMOTE) return;

    // Inline SVG crescent — the DSI panel has no color-emoji font, so "🌙"
    // rendered as a tofu box.
    const MOON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true">'
      + '<path fill="currentColor" d="M21 12.79A9 9 0 1 1 11.21 3'
      + ' 7 7 0 0 0 21 12.79z"/></svg>';
    const moon = document.createElement("a");
    moon.href = "#";
    moon.className = "moon";
    moon.innerHTML = MOON_SVG;
    moon.title = "Sleep until morning (tap screen to wake)";
    moon.addEventListener("click", ev => { ev.preventDefault(); setSleep(true); });
    // Screen-off lives in the header by the clock (not in the view switcher,
    // where it sat one fat-finger from a view pill). Every view has a
    // <header> with #clock; fall back to the switcher if that ever changes.
    const clockEl = document.getElementById("clock");
    const header = clockEl ? clockEl.parentElement : document.querySelector("header");
    if (header) header.append(moon); else nav.append(moon);

    dishPill = document.createElement("a");
    dishPill.href = "#";
    dishPill.className = "dish";
    dishPill.hidden = true;  // shown once /api/state carries a starlink block
    dishPill.addEventListener("click", ev => { ev.preventDefault(); toggleHold(); });
    if (header) header.insertBefore(dishPill, moon); else nav.append(dishPill);
    syncDish();

    sleepOverlay = document.createElement("div");
    sleepOverlay.id = "sleep-overlay";
    sleepOverlay.innerHTML = '<div class="hint">' + MOON_SVG + '</div>';
    sleepOverlay.addEventListener("pointerdown", ev => {
      ev.preventDefault();
      setSleep(false);
    });
    document.body.append(sleepOverlay);
    syncSleep();
  }

  function renderRawTable(tableEl, devices) {
    if (!tableEl || !last) return;
    const rows = [["device", "field", "value"]];
    for (const d of devices) {
      if (!d) continue;
      for (const [k, v] of Object.entries(d)) {
        if (["kind", "name"].includes(k)) continue;
        const val = k === "ts" ? Math.round(last.now - d.ts) + "s ago"
          : Array.isArray(v) ? (v.join(", ") || "—") : String(v ?? "—");
        rows.push([d.name, k, val]);
      }
    }
    tableEl.innerHTML = rows.map((r, i) =>
      "<tr>" + r.map(c => `<t${i ? "d" : "h"}>${c}</t${i ? "d" : "h"}>`).join("") + "</tr>"
    ).join("");
  }

  setInterval(poll, POLL_MS);
  poll();

  return { onData, fetchDay, fetchSolarMax, fmt, degF, cssVar, findDevice, age, isStale, battStatus, shortState,
           setBadge, el, makeChart, startClock, injectSwitcher, renderRawTable,
           enableSky, replayEnd, get last() { return last; }, STALE_S };
})();
