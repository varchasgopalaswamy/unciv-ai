const rankings = replay.statistics ?? {};
const rankingNames = Object.keys(rankings);
const plotColors = ["#4dbaff", "#ff9c2a", "#33e6a0", "#ff6085", "#bc91ff", "#f9e85b"];
const plotDashes = [[], [8, 4], [3, 3], [9, 3, 2, 3], [14, 4], [2, 4]];
const chartNumber = new Intl.NumberFormat(undefined, {notation: "compact", maximumFractionDigits: 1});
const dollarOptions = {style: "currency", currency: "USD"};
const dollars = new Intl.NumberFormat(undefined, dollarOptions);
const chartDollars = new Intl.NumberFormat(undefined, {...dollarOptions, notation: "compact", maximumFractionDigits: 2});
const costMetric = "Estimated API cost (USD)";
const usageMetrics = new Set(["Cached input tokens", "Uncached input tokens", "Output tokens", costMetric]);
const firstStatTurn = replay.turns[0][1];
const lastStatTurn = replay.turns.at(-1)[1];
const civilizationStyles = Object.fromEntries(Object.keys(replay.civilizations).map((name, i) =>
  [name, {color: plotColors[i % plotColors.length], dash: plotDashes[i % plotDashes.length]}]));
let metric = rankingNames[0];
let statisticsPanel = null;
let expandedStatisticsPanel = null;
if (rankingNames.length) get("notes").parentElement.querySelector('[data-panel="statistics"]').hidden = false;

function chartEdges(width, font) {
  const left = Math.ceil(font * 3.5);
  return {left, right: Math.max(left + 1, width - Math.ceil(font * 1.85))};
}

function drawStatisticsPlot(chart, series, turn, {width, height, dpr, font}, cost) {
  const pixelsWide = Math.round(width * dpr);
  const pixelsHigh = Math.round(height * dpr);
  if (chart.width !== pixelsWide || chart.height !== pixelsHigh) {
    chart.width = pixelsWide;
    chart.height = pixelsHigh;
  }
  const context = chart.getContext("2d");
  context.setTransform(pixelsWide / width, 0, 0, pixelsHigh / height, 0, 0);
  const {left, right} = chartEdges(width, font);
  const top = font * 1.2, bottom = height - font * 3;
  const span = Math.max(1, lastStatTurn - firstStatTurn);
  const x = sample => left + (sample - firstStatTurn) / span * (right - left);
  let low = 0, high = 0;
  for (const samples of Object.values(series)) {
    for (const [, value] of samples) {
      low = Math.min(low, value);
      high = Math.max(high, value);
    }
  }
  const rawTick = Math.max(cost ? .01 : 1, (high - low) / 4);
  const unit = 10 ** Math.floor(Math.log10(rawTick));
  const tick = ([1, 2, 5, 10].find(multiplier => multiplier * unit >= rawTick) ?? 10) * unit;
  low = Math.floor(low / tick) * tick;
  high = Math.max(low + tick, Math.ceil(high / tick) * tick);
  const y = value => bottom - (value - low) / (high - low) * (bottom - top);

  context.clearRect(0, 0, width, height);
  context.font = `${font}px system-ui, sans-serif`;
  context.setLineDash([]);
  context.lineWidth = 1;
  context.textAlign = "right";
  context.textBaseline = "middle";
  for (let index = 0; index <= Math.round((high - low) / tick); index++) {
    const value = low + index * tick;
    const row = y(value);
    context.strokeStyle = "#344640";
    context.beginPath();
    context.moveTo(left, row);
    context.lineTo(right, row);
    context.stroke();
    context.fillStyle = "#e0ebe6";
    context.fillText((cost ? chartDollars : chartNumber).format(value), left - 6, row);
  }
  context.textAlign = "center";
  const divisions = Math.max(1, Math.min(4, Math.floor((right - left) / (font * 3.6))));
  for (const sample of new Set(Array.from({length: divisions + 1}, (_, i) => Math.round(firstStatTurn +
       i * (lastStatTurn - firstStatTurn) / divisions)))) {
    context.fillText(formatter.format(sample), x(sample), bottom + font);
  }
  context.fillText("Turn", (left + right) / 2, height - font * .75);

  context.strokeStyle = "#cfc9ac";
  context.setLineDash([3, 4]);
  context.beginPath();
  context.moveTo(x(turn), top);
  context.lineTo(x(turn), bottom);
  context.stroke();
  for (const [name, samples] of Object.entries(series)) {
    const {color, dash} = civilizationStyles[name];
    context.strokeStyle = context.fillStyle = color;
    context.lineWidth = 2.5;
    context.lineJoin = "round";
    context.setLineDash(dash);
    context.beginPath();
    let previous = -Infinity;
    for (const [sample, value] of samples) {
      if (sample === previous + 1) context.lineTo(x(sample), y(value));
      else context.moveTo(x(sample), y(value));
      previous = sample;
    }
    context.stroke();
    const selected = samples[upperBound(samples, turn) - 1];
    if (selected?.[0] === turn) {
      context.beginPath();
      context.arc(x(turn), y(selected[1]), 3.8, 0, 2 * Math.PI);
      context.fill();
    }
  }
}

function statisticsWidget(initialTurn, expanded) {
  const content = element("div", "");
  const controls = element("div", "", "stats-controls");
  const label = element("label", "Metric");
  const select = element("select", "");
  for (const name of rankingNames) select.add(new Option(name));
  label.append(select);
  controls.append(label);

  const chart = element("canvas", "", expanded ? "stats-chart stats-chart-expanded" : "stats-chart");
  chart.tabIndex = 0;
  chart.setAttribute("aria-label", "Statistics over time. Use arrow keys to inspect turns and Enter to jump.");
  const table = element("table", "", "stats-readout");
  const caption = element("caption", "");
  const rows = element("tbody", "");
  const footer = element("tfoot", "");
  const totalRow = element("tr", "");
  const totalLabel = element("th", "Total");
  totalLabel.scope = "row";
  const totalCell = element("td", "");
  totalRow.append(totalLabel, totalCell);
  footer.append(totalRow);
  const note = element("p", "", "stats-usage-note");
  table.append(caption, rows, footer);
  content.append(controls, chart, table, note);

  let replayTurn = initialTurn;
  let inspected = null;
  let rendered = null;
  function render() {
    const turn = inspected ?? replayTurn;
    const width = chart.clientWidth, height = chart.clientHeight;
    if (!chart.isConnected || !width || !height) return;
    const dpr = window.devicePixelRatio || 1;
    const font = Math.max(11, parseFloat(getComputedStyle(chart).fontSize) * .8);
    if (rendered?.metric === metric && rendered.turn === turn && rendered.width === width &&
        rendered.height === height && rendered.dpr === dpr && rendered.font === font) return;
    rendered = {metric, turn, width, height, dpr, font};
    select.value = metric;
    const series = rankings[metric];
    const cost = metric === costMetric;
    const usage = usageMetrics.has(metric);
    const number = cost ? dollars : formatter;
    drawStatisticsPlot(chart, series, turn, rendered, cost);
    caption.textContent = metric + " · Turn " + turn;
    const fragment = document.createDocumentFragment();
    let sum = 0, complete = true;
    for (const [name, samples] of Object.entries(series)) {
      const row = element("tr", "");
      const cell = element("th", "");
      cell.scope = "row";
      const swatch = element("span", "", "stats-swatch");
      swatch.style.backgroundColor = civilizationStyles[name].color;
      swatch.setAttribute("aria-hidden", "true");
      cell.append(swatch, document.createTextNode(name));
      const selected = samples[upperBound(samples, turn) - 1];
      const value = selected?.[0] === turn ? selected[1] : null;
      if (value === null) complete = false;
      else sum += value;
      row.append(cell, element("td", value === null ? "—" : number.format(value)));
      fragment.append(row);
    }
    rows.replaceChildren(fragment);
    footer.hidden = note.hidden = !usage;
    if (usage) {
      totalCell.textContent = complete ? number.format(sum) : "—";
      note.textContent = "Reported model usage through the selected turn.";
      if (cost) {
        const pricing = element("a", "OpenAI API pricing");
        pricing.href = "https://developers.openai.com/api/docs/pricing";
        pricing.target = "_blank";
        pricing.rel = "noreferrer";
        note.append(" The estimate uses Standard short-context text rates for the three recorded token types. ", pricing);
      }
    }
  }
  function inspectTurn(turn) {
    inspected = Math.max(firstStatTurn, Math.min(lastStatTurn, turn));
    render();
  }
  function plotTurn(event) {
    const box = chart.getBoundingClientRect();
    const {left, right} = chartEdges(rendered.width, rendered.font);
    const px = event.clientX - box.left - chart.clientLeft;
    return Math.round(firstStatTurn + (px - left) / (right - left) * (lastStatTurn - firstStatTurn));
  }
  function jumpToInspected() {
    const entry = replay.turns.find(([, turn]) => turn === inspected);
    if (!entry) return;
    if (expanded) treeDialog.close();
    inspected = null;
    jump(entry[0]);
  }

  select.addEventListener("change", () => {
    metric = select.value;
    render();
    if (expanded) requestDraw();
  });
  new ResizeObserver(render).observe(chart);
  window.addEventListener("resize", render);
  chart.addEventListener("pointermove", event => inspectTurn(plotTurn(event)));
  chart.addEventListener("pointerleave", () => { inspected = null; render(); });
  chart.addEventListener("click", event => { inspectTurn(plotTurn(event)); jumpToInspected(); });
  chart.addEventListener("focusout", () => { inspected = null; render(); });
  chart.addEventListener("keydown", event => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      inspectTurn((inspected ?? replayTurn) + (event.key === "ArrowLeft" ? -1 : 1));
    } else if (event.key === "Enter" && inspected != null) jumpToInspected();
    else return;
    event.preventDefault();
  });

  if (!expanded) {
    const expand = element("button", "Expand chart");
    controls.append(expand);
    expand.addEventListener("click", () => {
      pause();
      get("tree-title").textContent = "Statistics";
      expandedStatisticsPanel ??= statisticsWidget(replayTurn, true);
      const treeContent = get("tree-content");
      if (!expandedStatisticsPanel.content.isConnected) treeContent.replaceChildren(expandedStatisticsPanel.content);
      treeDialog.showModal();
      expandedStatisticsPanel.update(replayTurn);
    });
  }
  return {content, update(turn) {
    if (turn !== replayTurn) inspected = null;
    replayTurn = turn;
    render();
  }};
}

function showStatistics(turn) {
  lastNotes = null;
  lastProgress = null;
  statisticsPanel ??= statisticsWidget(turn, false);
  if (!statisticsPanel.content.isConnected) {
    get("notes").replaceChildren(statisticsPanel.content);
  }
  statisticsPanel.update(turn);
}
