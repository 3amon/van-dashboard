/* The tiles + 8-hour-chart layout behind /v/sky.
   initRack(viewSlug, {sky}) builds the DOM and wires rendering. */
"use strict";
function initRack(viewSlug, { sky = false } = {}) {
  const { fmt, degF, findDevice, isStale, battStatus, setBadge, shortState } = VanDash;
  const WINDOW_S = 8 * 3600;

  document.body.innerHTML = `
  <header>
    <h1>Van Power</h1>
    <span id="status">connecting…</span>
    <span id="clock"></span>
  </header>

  <div class="tiles rack-tiles">
    <div class="tile" id="tile-batt" role="button" tabindex="0" aria-label="Show battery details">
      <div class="label">Battery</div>
      <div class="value" id="batt-v">—</div>
      <div class="sub" id="batt-sub">&nbsp;</div>
      <div class="badge" id="batt-badge"><span class="dot"></span><span id="batt-badge-text">no data</span></div>
      <svg class="battery-sparkline" id="battery-sparkline" viewBox="0 0 240 20" aria-label="Recent battery percentage"></svg>
    </div>
    <div class="tile" id="tile-mppt">
      <div class="label">Solar charger</div>
      <div class="value" id="mppt-state">—</div>
      <div class="sub" id="mppt-sub">&nbsp;</div>
    </div>
    <div class="tile" id="tile-yield">
      <div class="label">Yield today</div>
      <div class="value" id="yield">—</div>
      <div class="sub" id="yield-sub">&nbsp;</div>
    </div>
    <div class="tile" id="tile-orion">
      <div class="label" id="orion-label">Alternator</div>
      <div class="value" id="orion-state">—</div>
      <div class="sub" id="orion-sub">&nbsp;</div>
    </div>
    <div class="tile" id="tile-heater">
      <div class="label">Heater · cabin</div>
      <div class="value" id="heater-temp">—</div>
      <div class="badge" id="heater-badge"><span class="dot"></span><span id="heater-badge-text">no data</span></div>
    </div>
    <div class="tile" id="tile-pi">
      <div class="label">Pi health</div>
      <div class="value" id="pi-v">—</div>
      <div class="sub" id="pi-sub">&nbsp;</div>
      <div class="badge" id="pi-badge"><span class="dot"></span><span id="pi-badge-text">no data</span></div>
    </div>
  </div>

  <div class="chart-card" id="chart-card">
    <div class="chart-head">
      <div>
        <div class="label" id="chart-title"></div>
        <div class="hero" id="hero">—</div>
      </div>
      <div class="label" style="margin-left:auto" id="chart-range">last 8 hours</div>
    </div>
    <div class="chart-wrap chart-slide" id="chart-wrap">
      <svg id="chart" role="img" aria-label="Solar power over the last 8 hours"></svg>
      <div class="tooltip" id="tooltip"></div>
    </div>
    <div class="chart-wrap chart-slide hidden" id="weekly-chart-wrap">
      <svg id="weekly-chart" role="img" aria-label="Daily maximum solar power over the last 7 days"></svg>
      <div class="tooltip" id="weekly-tooltip"></div>
    </div>
    <div class="battery-details chart-slide hidden" id="battery-details">
      <div class="battery-details-head">
        <div>
          <div class="label">Battery details</div>
          <div class="battery-details-soc" id="detail-soc">—</div>
        </div>
        <button class="battery-details-close" id="battery-details-close" type="button" aria-label="Back to charts">‹ <span>Back</span></button>
      </div>
      <div class="battery-soc-history">
        <span>SoC history · reserve at 20%</span>
        <svg id="battery-soc-chart" viewBox="0 0 800 100" aria-label="Battery state of charge history"></svg>
      </div>
      <div class="battery-detail-grid">
        <div class="battery-detail"><span>Bank voltage</span><strong id="detail-voltage">—</strong></div>
        <div class="battery-detail"><span>Net current</span><strong id="detail-current">—</strong></div>
        <div class="battery-detail"><span id="detail-forecast-label">Reserve forecast</span><strong id="detail-remaining">—</strong></div>
        <div class="battery-detail"><span>BMS temperature</span><strong id="detail-temp">—</strong></div>
        <div class="battery-detail"><span>Cells · range Δ</span><strong id="detail-cells">—</strong></div>
        <div class="battery-detail"><span>Capacity remaining</span><strong id="detail-capacity">—</strong></div>
        <div class="battery-detail"><span>SoC · BMS vs shunt</span><strong id="detail-socs">—</strong></div>
        <div class="battery-detail"><span>BMS status</span><strong id="detail-bms">—</strong></div>
      </div>
    </div>
    <button class="chart-nav chart-prev" id="chart-prev" type="button" aria-label="Previous chart">‹</button>
    <button class="chart-nav chart-next" id="chart-next" type="button" aria-label="Next chart">›</button>
    <details class="readings">
      <summary>All readings</summary>
      <table id="raw"></table>
    </details>
  </div>`;

  const $ = id => document.getElementById(id);

  // Live-chart legend: one glyph per line, tinted the line's color, no words
  // (and no emoji — the DSI panel has no color-emoji font). Sun = solar,
  // car = alternator, plug = shore, house = draw.
  const legendIcon = (colorVar, title, body) =>
    `<svg viewBox="0 0 24 24" style="color:var(${colorVar})" fill="none"` +
    ` stroke="currentColor" stroke-width="2" stroke-linecap="round"` +
    ` stroke-linejoin="round"><title>${title}</title>${body}</svg>`;
  const LIVE_LEGEND = '<span class="legend">'
    + legendIcon("--series", "Solar",
        '<circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3'
        + 'M4.9 4.9l2.2 2.2M16.9 16.9l2.2 2.2M19.1 4.9l-2.2 2.2M7.1 16.9l-2.2 2.2"/>')
    + legendIcon("--alt", "Alternator",
        '<path d="M3 16v-3l2.5-5h9l4.5 5h2v3"/><path d="M3 16h18"/>'
        + '<circle cx="7.5" cy="17.5" r="1.8"/><circle cx="16.5" cy="17.5" r="1.8"/>')
    + legendIcon("--shore", "Shore charger",
        '<path d="M9 2v5M15 2v5"/>'
        + '<path d="M7 7h10v4a5 5 0 0 1-5 5 5 5 0 0 1-5-5z"/><path d="M12 16v6"/>')
    + legendIcon("--flame", "House draw",
        '<path d="M3 11l9-8 9 8"/><path d="M5 9.5V21h14V9.5"/>')
    + "</span>";
  $("chart-title").innerHTML = LIVE_LEGEND;

  // Replay mode: chart pinned to a historical window; tiles stay live
  let replayRows = null;
  const replayEnd = VanDash.replayEnd;
  if (replayEnd) {
    const fmtT = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    $("chart-range").textContent = fmtT(replayEnd - WINDOW_S) + " – " + fmtT(replayEnd);
    const daysAgo = Math.floor((new Date().setHours(0, 0, 0, 0) / 1000 - replayEnd) / 86400) + 1;
    VanDash.fetchDay(Math.max(daysAgo, 0), 30).then(d => {
      replayRows = d.rows;
      chart.redraw();
    });
  }

  // House draw: what the van consumes regardless of who supplies it — charge
  // sources minus what reaches the bank (shunt net). With no source active
  // this is exactly the shunt's discharge watts.
  const drawW = r => {
    if (r.v == null || r.a == null) return null;
    const srcW = (r.w ?? 0) + (r.alt_w ?? 0) + (r.ac_w ?? 0);
    const load = srcW - r.v * r.a;
    if (load >= 0) return load;
    // Negative means a charging bucket is missing a source sample (advert
    // gap): the load can't be priced, so leave a hole rather than a dip.
    return r.w != null && r.alt_w != null && r.ac_w != null ? 0 : null;
  };

  // The other charge sources share the solar (left) axis so all sources read
  // at one scale. alt_w is an estimate (the Orion advert has no current);
  // ac_w is the IP22's measured output.
  const SOURCES = [["alt_w", "--alt"], ["ac_w", "--shore"]];

  const chart = VanDash.makeChart({
    svg: $("chart"), wrap: $("chart-wrap"), tip: $("tooltip"),
    getData: () => (replayEnd ? (replayRows || []) : VanDash.last.history),
    getDomain: () => (replayEnd
      ? [replayEnd - WINDOW_S, replayEnd]
      : [VanDash.last.now - WINDOW_S, VanDash.last.now]),
    opts: { field: "w", color: "--series", yFloor: 50, ySnap: 50, xTickS: 3600,
            gapS: 300,  // advert gaps up to ~4 min are normal; don't slice the area fill
            endDot: !replayEnd, pad: { left: 44, right: 48, top: 8, bottom: 22 },
            scaleOn: r => Math.max(r.alt_w ?? 0, r.ac_w ?? 0),
            labelFmt: r => `<b>${Math.round(r.w ?? 0)} W</b> solar` +
              (r.alt_w > 1 ? ` · <b style="color:var(--alt)">${Math.round(r.alt_w)} W</b> alt` : "") +
              (r.ac_w > 1 ? ` · <b style="color:var(--shore)">${Math.round(r.ac_w)} W</b> shore` : "") +
              (drawW(r) == null ? "" : ` · <b style="color:var(--flame)">${Math.round(drawW(r))} W</b> draw`),
            // House draw on its own right-hand axis: a microwave minute is
            // ~30× peak solar, and one shared scale flattens the solar line
            // to the baseline. The draw scale is an integer multiple of the
            // solar scale so both axes share gridlines at round numbers.
            decorate(svg, { X, Y, x1, y0, y1, yTop }) {
              const [tMin, tMax] = replayEnd
                ? [replayEnd - WINDOW_S, replayEnd]
                : [VanDash.last.now - WINDOW_S, VanDash.last.now];
              const all = (replayEnd ? (replayRows || []) : VanDash.last.history)
                .filter(r => r.t >= tMin && r.t <= tMax);

              // Alternator / shore lines, left axis. A source that never woke
              // up in the window is skipped entirely — a zero line pinned
              // under the solar area is just noise.
              for (const [field, colorVar] of SOURCES) {
                const srows = all.filter(r => r[field] != null);
                if (!srows.some(r => r[field] > 5)) continue;
                const color = VanDash.cssVar(colorVar);
                const segs = [[]];
                for (const r of srows) {
                  const seg = segs[segs.length - 1];
                  if (seg.length && r.t - seg[seg.length - 1].t > 300) segs.push([r]);
                  else seg.push(r);
                }
                for (const seg of segs) {
                  if (seg.length < 2) continue;
                  svg.append(VanDash.el("polyline", {
                    points: seg.map(r => `${X(r.t)},${Y(r[field])}`).join(" "),
                    fill: "none", stroke: color, "stroke-width": 2,
                    opacity: 0.9, "stroke-linejoin": "round",
                    "stroke-linecap": "round" }));
                }
              }

              const rows = all.filter(r => drawW(r) != null);
              if (!rows.length) return;
              const dMax = Math.max(1, ...rows.map(drawW));
              const factor = [1, 2, 4, 5, 8, 10, 16, 20, 40, 50, 80, 100]
                .find(f => yTop * f >= dMax) || Math.ceil(dMax / yTop);
              const Y2 = v => y0 - (v / (yTop * factor)) * (y0 - y1);
              const color = VanDash.cssVar("--flame");
              let yStep = 25;  // mirror makeChart's adaptive tick spacing
              while (yTop / yStep > 7) yStep *= 2;
              for (let v = yStep; v <= yTop; v += yStep) {
                const lab = VanDash.el("text", { x: x1 + 8, y: Y(v) + 4,
                  "text-anchor": "start", fill: color, opacity: 0.8, "font-size": 11 });
                lab.textContent = v * factor;
                svg.append(lab);
              }
              const segs = [[]];
              for (const r of rows) {
                const seg = segs[segs.length - 1];
                if (seg.length && r.t - seg[seg.length - 1].t > 270) segs.push([r]);
                else seg.push(r);
              }
              for (const seg of segs) {
                if (seg.length < 2) continue;
                svg.append(VanDash.el("polyline", {
                  points: seg.map(r => `${X(r.t)},${Y2(drawW(r))}`).join(" "),
                  fill: "none", stroke: color, "stroke-width": 1.5,
                  opacity: 0.85, "stroke-linejoin": "round" }));
              }
            } },
  });

  let weeklyData = { start: 0, end: 1, rows: [] };
  const dayLabel = ts => new Date(ts * 1000).toLocaleDateString([], { weekday: "short" });
  const weeklyChart = VanDash.makeChart({
    svg: $("weekly-chart"), wrap: $("weekly-chart-wrap"), tip: $("weekly-tooltip"),
    getData: () => weeklyData.rows,
    getDomain: () => [weeklyData.start, weeklyData.end],
    opts: { field: "w", color: "--series", yFloor: 100, ySnap: 100,
            xTickS: 86400, gapS: 172800, endDot: false, hideSeries: true,
            pad: { left: 44, right: 54, top: 8, bottom: 22 },
            xTicks: rows => rows.map(r => r.t),
            xLabelFmt: dayLabel,
            tipTimeFmt: r => new Date(r.t * 1000).toLocaleDateString([], {
              weekday: "short", month: "short", day: "numeric" }),
            labelFmt: r => `<b>${Math.round(r.w ?? 0)} W</b> max · ` +
              `<b>${Number(r.kwh ?? 0).toFixed(2)} kWh</b> total`,
            decorate(svg, { X, Y, x0, x1, y0, y1, rows }) {
              const energyMax = Math.max(0.25, ...rows.map(r => r.kwh ?? 0));
              let energyStep = 0.25;
              while (energyMax / energyStep > 5) energyStep *= 2;
              const energyTop = Math.ceil(energyMax / energyStep) * energyStep;
              const Y2 = value => y0 - (value / energyTop) * (y0 - y1);
              const slot = (x1 - x0) / Math.max(rows.length, 1);
              const width = Math.min(30, slot * 0.3);
              const peakColor = VanDash.cssVar("--series");
              const energyColor = VanDash.cssVar("--sun");
              for (const r of rows) {
                if (r.w != null)
                  svg.append(VanDash.el("rect", { x: X(r.t) - width - 2, y: Y(r.w),
                    width, height: y0 - Y(r.w), rx: 3, fill: peakColor, opacity: 0.9 }));
                if (r.kwh != null)
                  svg.append(VanDash.el("rect", { x: X(r.t) + 2, y: Y2(r.kwh),
                    width, height: y0 - Y2(r.kwh), rx: 3, fill: energyColor, opacity: 0.9 }));
              }
              for (let value = energyStep; value <= energyTop; value += energyStep) {
                const label = VanDash.el("text", { x: x1 + 8, y: Y2(value) + 4,
                  "text-anchor": "start", fill: energyColor, "font-size": 11 });
                label.textContent = Number(value.toFixed(2));
                svg.append(label);
              }
            } },
  });

  const charts = ["live", "weekly"];
  let chartIndex = 0;
  let showingBatteryDetails = false;
  let manualUntil = 0;
  function showChart(index, manual = false) {
    showingBatteryDetails = false;
    $("chart-card").classList.remove("battery-details-open");
    document.body.classList.remove("battery-details-open");
    chartIndex = (index + charts.length) % charts.length;
    if (manual) manualUntil = Date.now() + 10 * 60 * 1000;
    const screen = charts[chartIndex];
    const weekly = screen === "weekly";
    $("chart-wrap").classList.toggle("hidden", screen !== "live");
    $("weekly-chart-wrap").classList.toggle("hidden", !weekly);
    $("battery-details").classList.add("hidden");
    for (const button of navButtons) button.classList.remove("nav-hidden");
    $("chart-title").innerHTML = weekly
      ? '<span style="color:var(--series)">Daily peak W</span> · <span style="color:var(--sun)">solar kWh</span>'
      : LIVE_LEGEND;
    $("chart-range").textContent = weekly ? "last 7 days" : "last 8 hours";
    if (replayEnd && !weekly) {
      const fmtT = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      $("chart-range").textContent = fmtT(replayEnd - WINDOW_S) + " – " + fmtT(replayEnd);
    }
    if (weekly) {
      const peak = Math.max(0, ...weeklyData.rows.map(r => r.w ?? 0));
      $("hero").innerHTML = peak ? Math.round(peak) + '<span class="unit"> W peak</span>' : "—";
    } else {
      const solar = findDevice("solar");
      $("hero").innerHTML = solar?.solar_power == null ? "—"
        : Math.round(solar.solar_power) + '<span class="unit"> W</span>';
    }
    requestAnimationFrame(() => (weekly ? weeklyChart : chart).redraw());
  }
  const navButtons = [$("chart-prev"), $("chart-next")];
  let navFadeTimer = null;
  function revealChartNav() {
    for (const button of navButtons) button.classList.remove("faded");
    clearTimeout(navFadeTimer);
    navFadeTimer = setTimeout(() => {
      for (const button of navButtons) button.classList.add("faded");
    }, 10000);
  }
  document.body.addEventListener("pointerdown", revealChartNav, { passive: true });
  document.body.addEventListener("pointermove", revealChartNav, { passive: true });
  $("chart-prev").addEventListener("click", () => showChart(chartIndex - 1, true));
  $("chart-next").addEventListener("click", () => showChart(chartIndex + 1, true));
  function showBatteryDetails() {
    showingBatteryDetails = true;
    $("chart-card").classList.add("battery-details-open");
    document.body.classList.add("battery-details-open");
    manualUntil = Date.now() + 10 * 60 * 1000;
    $("chart-wrap").classList.add("hidden");
    $("weekly-chart-wrap").classList.add("hidden");
    $("battery-details").classList.remove("hidden");
    $("chart-title").innerHTML = '<span style="color:var(--good)">Battery details</span>';
    $("chart-range").textContent = "live";
    for (const button of navButtons) button.classList.add("nav-hidden");
  }
  $("battery-details-close").addEventListener("click", event => {
    event.stopPropagation();
    showChart(chartIndex, true);
  });
  $("tile-batt").addEventListener("click", showBatteryDetails);
  $("tile-batt").addEventListener("keydown", event => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      showBatteryDetails();
    }
  });
  revealChartNav();
  setInterval(() => {
    if (Date.now() >= manualUntil) {
      showChart(showingBatteryDetails ? 0 : chartIndex + 1);
    }
  }, 30000);

  async function refreshWeekly() {
    try {
      weeklyData = await VanDash.fetchSolarMax(7);
      if (!showingBatteryDetails && charts[chartIndex] === "weekly") showChart(chartIndex);
    } catch { /* retain the previous series until the next refresh */ }
  }
  refreshWeekly();
  setInterval(refreshWeekly, 5 * 60 * 1000);

  let lastChartSig = "";
  let lastBatteryHistorySig = "";
  VanDash.onData(() => {
    const solar = findDevice("solar");
    const dcdc = findDevice("dcdc");
    const heater = findDevice("heater");
    const shunt = findDevice("shunt");
    const useShunt = shunt && !isStale(shunt);
    const epoch = findDevice("battery");
    const useEpoch = epoch && !isStale(epoch);

    // A ≥15-point disagreement between the meters gets both named on the
    // badge, stamping the BMS number with its age once it's stale (sparse
    // JBD adverts make long gaps routine).
    let mismatch = null;
    if (useShunt && shunt.soc != null && epoch?.soc != null
        && Math.abs(shunt.soc - epoch.soc) >= 15) {
      const mins = Math.round((VanDash.last.now - epoch.ts) / 60);
      const age = useEpoch ? "" : mins < 100 ? ` (${mins}m)` : ` (${Math.round(mins / 60)}h)`;
      mismatch = `⚠ BMS ${Math.round(epoch.soc)}%${age} · shunt ${Math.round(shunt.soc)}%`;
    }
    // The BMS's own count wins even when its last report is old (owner
    // call, 2026-09-23): SoC drifts a few points an hour at most, while
    // the shunt's estimate has been tens of points optimistic between
    // full-charge syncs. The shunt only fills in before first BMS contact.
    const soc = epoch?.soc != null ? epoch.soc
      : useShunt && shunt.soc != null ? shunt.soc : null;
    $("tile-batt").classList.remove("battery-high", "battery-low", "battery-critical");
    const batteryLevel = soc == null ? null
      : soc < 20 ? "battery-critical" : soc < 35 ? "battery-low"
      : soc >= 50 ? "battery-high" : null;
    if (batteryLevel) $("tile-batt").classList.add(batteryLevel);
    const forecast = reserveForecast(
      soc, VanDash.last.history, shunt?.current, epoch?.capacity_ah, epoch?.remaining_ah,
    );
    const charge = chargeForecast(epoch, VanDash.last.history, shunt?.current ?? epoch?.current);

    $("tile-batt").classList.toggle("stale", useShunt ? false : isStale(solar));
    $("tile-mppt").classList.toggle("stale", isStale(solar));
    $("tile-yield").classList.toggle("stale", isStale(solar));
    $("tile-orion").classList.toggle("stale", isStale(dcdc));
    $("tile-heater").classList.toggle("stale", isStale(heater));

    if (useShunt) {
      // SmartShunt reporting: SoC is the headline, net bank current the detail.
      $("batt-v").innerHTML = soc == null ? "—"
        : Math.round(soc) + '<span class="unit">%</span>';
      // Compact: three readings have to share one line (wrapping shoves the
      // badge out of the tile).
      $("batt-sub").textContent =
        fmt(shunt.voltage, 1) + "V " + fmt(shunt.current, 1) + "A"
        + (useEpoch && epoch.temp != null
           ? " " + fmt(degF(epoch.temp), 0) + "°F" : "");
      // A BMS protection alarm outranks whatever the shunt has to say.
      if (useEpoch && epoch.protection.length)
        setBadge($("batt-badge"), $("batt-badge-text"),
                 "critical", "BMS: " + epoch.protection[0]);
      else
        setBadge($("batt-badge"), $("batt-badge-text"),
                 ...shuntStatus(shunt, forecast, charge, mismatch));
    } else if (solar) {
      $("batt-v").innerHTML = fmt(solar.battery_voltage, 1) + '<span class="unit">V</span>';
      $("batt-sub").innerHTML = "&nbsp;";
      const [level, text] = battStatus(solar.battery_voltage, solar.battery_charging_current);
      setBadge($("batt-badge"), $("batt-badge-text"), level, text);
    }

    renderBatteryHistory(VanDash.last.history);
    renderBatteryDetails(shunt, epoch, solar, forecast, charge, soc);

    if (solar) {
      $("mppt-state").textContent = shortState(solar.charge_state) ?? "—";
      $("mppt-sub").textContent =
        (solar.charger_error && solar.charger_error !== "No Error")
          ? "Error: " + solar.charger_error
          : solar.name;

      const wh = solar.yield_today;
      $("yield").innerHTML = wh == null ? "—"
        : wh >= 1000 ? fmt(wh / 1000, 2) + '<span class="unit">kWh</span>'
        : Math.round(wh) + '<span class="unit">Wh</span>';
      const storedPeak = weeklyData.rows.length ? weeklyData.rows[weeklyData.rows.length - 1].w : null;
      const todayPeak = Math.max(storedPeak ?? 0, solar.solar_power ?? 0);
      $("yield-sub").textContent = todayPeak > 0 ? "peak " + Math.round(todayPeak) + " W today" : "daily solar";

      if (!showingBatteryDetails && charts[chartIndex] === "live")
        $("hero").innerHTML = Math.round(solar.solar_power) + '<span class="unit"> W</span>';
    }

    // Shore charger takes the tile over while it's plugged in and reporting;
    // unplugged it stops advertising entirely, and the alternator returns.
    const ac = findDevice("ac");
    if (ac && !isStale(ac)) {
      $("orion-label").textContent = "Shore charger";
      $("tile-orion").classList.toggle("stale", false);
      if (ac.charger_error && ac.charger_error !== "No Error") {
        $("orion-state").textContent = "Error";
        $("orion-sub").textContent = ac.charger_error;
      } else {
        $("orion-state").textContent = shortState(ac.charge_state) ?? "—";
        $("orion-sub").textContent =
          fmt(ac.output_current1, 1) + " A · " + fmt(ac.output_voltage1, 2) + " V";
      }
    } else if (dcdc) {
      $("orion-label").textContent = "Alternator";
      const off = (dcdc.off_reason || []).filter(r => r !== "No input power");
      if (dcdc.device_state && dcdc.device_state !== "Off") {
        $("orion-state").textContent = shortState(dcdc.device_state);
        $("orion-sub").textContent = "in " + fmt(dcdc.input_voltage) + " V · out " + fmt(dcdc.output_voltage) + " V";
      } else {
        $("orion-state").textContent = "Off";
        $("orion-sub").textContent = off.includes("Engine shutdown")
          ? "engine off · starter " + fmt(dcdc.input_voltage) + " V"
          : (off[0] || "starter " + fmt(dcdc.input_voltage) + " V").toLowerCase();
      }
    }

    if (heater) {
      $("heater-temp").innerHTML = fmt(degF(heater.cabin_temp), 0) + '<span class="unit">°F</span>';
      let level = "", text;
      if (heater.error_state > 1) {
        level = "critical";
        text = "⚠ " + (heater.error_string || "error");
      } else if (heater.run_state > 0) {
        level = "good";
        text = (heater.run_string || "running").toLowerCase() + " · set " + fmt(degF(heater.target_temp), 0) + "°";
      } else {
        text = "off · " + Math.round(heater.humidity) + "% rh";
      }
      setBadge($("heater-badge"), $("heater-badge-text"), level, text);
    }

    renderPi(VanDash.last.pi);

    VanDash.renderRawTable($("raw"), [solar, dcdc, ac, heater]);
    // Rebuild the chart only when a NEW bucket lands, not every poll.
    // (Keying on the tail bucket's VALUE meant a rebuild every 2s whenever
    // power was fluctuating — the visible chart snap.) The trace lags the
    // live tiles by up to one 30s bucket, which nobody can see.
    const h = VanDash.last.history;
    const tail = h.length ? h[h.length - 1] : null;
    const sig = h.length + ":" + (tail ? tail.t : "");
    if (sig !== lastChartSig) {
      lastChartSig = sig;
      chart.redraw();
    }
  });

  function reserveForecast(soc, history, current, capacityAh, remainingAh) {
    if (soc == null || capacityAh == null || current == null || current >= -0.1) return null;
    if (soc <= 20) return { hours: 0 };
    const cutoff = VanDash.last.now - 15 * 60;
    const amps = history.filter(r => r.t >= cutoff && r.a != null && r.a < -0.1).map(r => -r.a);
    amps.push(-current);
    const draw = amps.reduce((sum, value) => sum + value, 0) / amps.length;
    const usableAh = remainingAh == null
      ? capacityAh * (soc - 20) / 100
      : Math.max(0, remainingAh - capacityAh * 0.2);
    const hours = usableAh / draw;
    if (!Number.isFinite(hours) || hours > 14 * 24) return null;
    return { hours };
  }

  function chargeForecast(epoch, history, current) {
    if (epoch?.remaining_ah == null || epoch?.capacity_ah == null || current == null || current <= 0.1)
      return null;
    const cutoff = VanDash.last.now - 15 * 60;
    const amps = history.filter(r => r.t >= cutoff && r.a != null).map(r => r.a);
    amps.push(current);
    const average = amps.reduce((sum, value) => sum + value, 0) / amps.length;
    if (average <= 0.1) return null;
    const hours = Math.max(0, epoch.capacity_ah - epoch.remaining_ah) / average;
    if (!Number.isFinite(hours) || hours > 14 * 24) return null;
    return { hours, average };
  }

  function chargeLabel(charge, compact = false) {
    if (!charge) return null;
    if (charge.hours <= 0.05) return "full";
    if (charge.hours < 2) {
      const mins = Math.max(5, Math.round(charge.hours * 60 / 5) * 5);
      return compact ? `~${mins}m full` : `full in ~${mins}m`;
    }
    if (charge.hours < 24) {
      const hours = Math.round(charge.hours);
      return compact ? `~${hours}h full` : `full in ~${hours}h`;
    }
    const days = Math.round(charge.hours / 24);
    return compact ? `~${days}d full` : `full in ~${days}d`;
  }

  function forecastLabel(forecast) {
    if (!forecast) return null;
    if (forecast.hours <= 0) return "at 20% reserve";
    if (forecast.hours < 2) {
      const mins = Math.max(1, Math.round(forecast.hours * 60 / 5) * 5);
      return "20% reserve in " + mins + "m";
    }
    if (forecast.hours < 24) return "20% reserve in " + Math.round(forecast.hours) + "h";
    return "20% reserve in " + Math.round(forecast.hours / 24) + "d";
  }

  function renderBatteryHistory(history) {
    const rows = history.filter(r => r.soc != null);
    const tail = rows.length ? rows[rows.length - 1] : null;
    const signature = rows.length + ":" + (tail ? tail.t : "");
    if (signature === lastBatteryHistorySig) return;
    lastBatteryHistorySig = signature;
    for (const [id, width, height] of [["battery-sparkline", 240, 20], ["battery-soc-chart", 800, 100]]) {
      const svg = $(id);
      svg.replaceChildren();
      if (rows.length < 2) continue;
      const tMin = Math.max(rows[0].t, VanDash.last.now - 8 * 3600);
      const visible = rows.filter(r => r.t >= tMin);
      if (visible.length < 2) continue;
      const X = t => ((t - tMin) / Math.max(1, VanDash.last.now - tMin)) * width;
      const Y = soc => height - Math.max(0, Math.min(100, soc)) / 100 * height;
      if (id === "battery-soc-chart") {
        const reserve = VanDash.el("line", { x1: 0, x2: width, y1: Y(20), y2: Y(20),
          stroke: VanDash.cssVar("--warning"), "stroke-width": 1, "stroke-dasharray": "7 6", opacity: 0.7 });
        svg.append(reserve);
      }
      svg.append(VanDash.el("polyline", { points: visible.map(r => `${X(r.t)},${Y(r.soc)}`).join(" "),
        fill: "none", stroke: VanDash.cssVar("--good"), "stroke-width": id === "battery-sparkline" ? 2 : 3,
        "stroke-linecap": "round", "stroke-linejoin": "round" }));
    }
  }

  function renderBatteryDetails(shunt, epoch, solar, forecast, charge, displaySoc) {
    const voltage = shunt?.voltage ?? epoch?.voltage ?? solar?.battery_voltage;
    const current = shunt?.current ?? epoch?.current;
    $("detail-voltage").textContent = voltage == null ? "—" : fmt(voltage, 2) + " V";
    $("detail-current").textContent = current == null ? "—" :
      (current > 0 ? "+" : "") + fmt(current, 2) + " A";
    $("detail-forecast-label").textContent = charge ? "Charge estimate" : "Reserve forecast";
    $("detail-remaining").textContent = chargeLabel(charge) || forecastLabel(forecast) || "collecting trend";
    $("detail-temp").textContent = epoch?.temp == null ? "—" : fmt(degF(epoch.temp), 1) + " °F";
    const cells = epoch?.cells_mv || [];
    $("detail-cells").textContent = cells.length
      ? Math.min(...cells) + "–" + Math.max(...cells)
        + (epoch?.cell_delta_mv == null ? "" : " · Δ" + epoch.cell_delta_mv) + " mV"
      : "—";
    $("detail-capacity").textContent = epoch?.remaining_ah == null ? "—" :
      fmt(epoch.remaining_ah, 1) + " / " + fmt(epoch.capacity_ah, 0) + " Ah";
    $("detail-bms").textContent = epoch?.protection?.length
      ? epoch.protection.join(", ") : epoch ? "normal" : "no BMS data";
    $("detail-socs").textContent =
      (epoch?.soc == null ? "—" : Math.round(epoch.soc) + "%") + " vs " +
      (shunt?.soc == null ? "—" : Math.round(shunt.soc) + "%");
    $("detail-soc").innerHTML = displaySoc == null ? "—"
      : Math.round(displaySoc) + '<span class="unit">%</span>';
  }

  // Shunt badge: SoC drives the level, current direction the story. The sub
  // line already shows amps, so the badge carries state / time-to-go.
  function shuntStatus(s, forecast, charge, mismatch) {
    let level = s.soc == null ? ""
      : s.soc < 15 ? "critical" : s.soc < 35 ? "warning" : "good";
    let text = "idle";
    if (mismatch) {
      level = "warning";
      text = mismatch;
    }
    else if (s.current > 0.1) text = chargeLabel(charge) || "charging";
    else if (s.current < -0.1) text = forecastLabel(forecast) || "discharging";
    return [level, text];
  }
  // Pi's own power health (input 5V, draw, temp, throttle/undervoltage flags).
  function renderPi(pi) {
    const fresh = pi && VanDash.last.now - pi.ts < 60 && pi.ext5v != null;
    $("tile-pi").classList.toggle("stale", !fresh);
    if (!fresh) {
      $("pi-v").textContent = "—";
      $("pi-sub").innerHTML = "&nbsp;";
      setBadge($("pi-badge"), $("pi-badge-text"), "", pi ? "no reading" : "no data");
      return;
    }
    $("pi-v").innerHTML = fmt(pi.ext5v, 2) + '<span class="unit">V</span>';
    const parts = [];
    if (pi.temp != null) parts.push(fmt(pi.temp, 0) + "°C");
    if (pi.wifi_ssid) parts.push(pi.wifi_ssid);
    $("pi-sub").textContent = parts.join(" · ") || " ";

    // Terse labels — the badge line is short. Full detail is in the
    // pihealth events.log / `pihealth status` on the Pi.
    let level = "good", text = pi.internet_ok === true ? "online" : "healthy";
    if (pi.uv_now) { level = "critical"; text = "⚠ undervolt"; }
    else if (pi.throttled_now) { level = "critical"; text = "⚠ throttled"; }
    else if (pi.uv_occurred || pi.throttle_occurred) { level = "warning"; text = "⚠ past event"; }
    else if (pi.ext5v < 4.85) { level = "warning"; text = "input low"; }
    else if (pi.internet_ok === false) { level = "warning"; text = "no internet"; }
    const signal = pi.wifi_dbm == null ? "" : " · " + Math.round(pi.wifi_dbm) + " dBm";
    text += signal;
    setBadge($("pi-badge"), $("pi-badge-text"), level, text);
  }

  if (sky) VanDash.enableSky();
  VanDash.startClock("clock");
  VanDash.injectSwitcher(viewSlug);
}
