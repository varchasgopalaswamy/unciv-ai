"use strict";

const replay = JSON.parse(document.getElementById("replay-data").textContent);
const get = id => document.getElementById(id);
if (replay.title) document.title = get("replay-title").textContent = replay.title;
const art = JSON.parse(get("art-data").textContent);
const canvas = get("map");
const ctx = canvas.getContext("2d");
const seek = get("seek");
const picker = get("civilization");
const turnPicker = get("turn");
const emblem = get("civ-emblem");
const tabs = [...document.querySelectorAll("[data-panel]")];
const isReview = note => note.kind === "review" || note.title === "Strategic review";
tabs.find(tab => tab.dataset.panel === "reviews").hidden =
  !Object.values(replay.civilizations).some(view => view.decisions.some(([, note]) => isReview(note)));
const highlights = replay.highlights?.notes
  ? {[replay.highlights.civilization]: replay.highlights.notes}
  : replay.highlights ?? {};
tabs.find(tab => tab.dataset.panel === "highlights").hidden = !Object.keys(highlights).length;
const relationHistory = replay.relations ?? [];
tabs.find(tab => tab.dataset.panel === "relations").hidden = !relationHistory.length;
const terrain = {
  Grassland: "#7d9b72", Plains: "#a19a65", Desert: "#cdb180", Tundra: "#8c9a93",
  Snow: "#c8d6d7", Mountain: "#758486", Coast: "#3d7d90", Ocean: "#29596f", Lakes: "#3d7d90"
};
const colors = ["#edaa83", "#abcbc3", "#c0aaf0", "#e19bb0", "#9dd58a", "#a2c9ec"];
const resourceColors = {Luxury: "#ffd800", Strategic: "#c14d00", Bonus: "#a8c3c9"};
const sightKey = tile => tile.x + "," + tile.y;
const hexXY = tile => [1.5 * (tile.y - tile.x), -Math.sqrt(3) * (tile.x + tile.y) / 2];
const hexCorners = Array.from({length: 6}, (_, i) => [Math.cos(i * Math.PI / 3), Math.sin(i * Math.PI / 3)]);
// Clockwise from the south edge, matching the unrotated Fantasy Hex border.
const neighborOffsets = [[-1, -1], [0, -1], [1, 0], [1, 1], [0, 1], [-1, 0]];
const snapshots = new Map();
const sheets = new Map();
const tintedSprites = new Map();
const civilianUnits = new Set(art.civilianUnits);
const controls = Object.fromEntries(["first", "previous", "play", "next", "last"].map(id => [id, get(id)]));
const formatter = new Intl.NumberFormat();
let civilization = Object.keys(replay.civilizations)[0];
let position = 0;
let panel = "decisions";
let playing = null;
let scheduledPaint = 0;
let textScale = 1;

const textSize = get("text-size");
const textSizeKey = "unciv-replay-text-size";
function applyTextSize() {
  document.documentElement.dataset.textSize = textSize.value;
  textScale = Number(textSize.value) / 100;
}
try {
  const storedSize = localStorage.getItem(textSizeKey);
  if ([...textSize.options].some(option => option.value === storedSize)) textSize.value = storedSize;
} catch {
  // Storage can be unavailable when a replay is opened as a local file.
}
applyTextSize();
textSize.addEventListener("change", () => {
  applyTextSize();
  try {
    localStorage.setItem(textSizeKey, textSize.value);
  } catch {
    // The setting still applies to the current page.
  }
  requestDraw();
});

for (const name of Object.keys(replay.civilizations)) {
  const model = replay.playerLabels?.[name];
  picker.add(new Option(model ? name + " · " + model : name, name));
}
for (const [step, turn] of replay.turns) turnPicker.add(new Option("Turn " + turn, step));
seek.max = replay.lastStep;
get("art-credits").hidden = !art.sheets;
for (const [name, source] of Object.entries(art.sheets ?? {})) {
  const image = new Image();
  image.addEventListener("load", () => { sheets.set(name, image); requestDraw(); }, {once: true});
  image.src = source;
}

function tintedSprite(name, color, image, sx, sy, sw, sh) {
  const key = name + ":" + color;
  if (!tintedSprites.has(key)) {
    const mask = document.createElement("canvas");
    mask.width = sw;
    mask.height = sh;
    const pixels = mask.getContext("2d");
    pixels.drawImage(image, sx, sy, sw, sh, 0, 0, sw, sh);
    pixels.globalCompositeOperation = "multiply";
    pixels.fillStyle = color;
    pixels.fillRect(0, 0, sw, sh);
    pixels.globalCompositeOperation = "destination-in";
    pixels.drawImage(image, sx, sy, sw, sh, 0, 0, sw, sh);
    tintedSprites.set(key, mask);
  }
  return tintedSprites.get(key);
}

function sprite(context, name, x, y, width, {bottom = false, color} = {}) {
  const region = art.regions?.[name];
  const image = region && sheets.get(region[0]);
  if (!image) return false;
  const [, sx, sy, sw, sh] = region;
  const height = width * sh / sw;
  const left = x - width / 2, top = y - height / (bottom ? 1 : 2);
  if (color) context.drawImage(tintedSprite(name, color, image, sx, sy, sw, sh), left, top, width, height);
  else context.drawImage(image, sx, sy, sw, sh, left, top, width, height);
  return true;
}

function tileRegion(name) {
  const preferred = `TileSets/HexaRealm/Tiles/${name}`;
  return art.regions?.[preferred] ? preferred : `TileSets/FantasyHex/Tiles/${name}`;
}

function tileLayers(tile) {
  if (!tile.terrain || !art.regions) return [];
  const land = [tile.terrain, ...(tile.features ?? [])];
  const improvement = tile.city ? "City center" : tile.improvement;
  const combined = improvement ? [...land, improvement].join("+") : land.join("+");
  if (art.tileRules[combined]) return art.tileRules[combined];
  if (art.regions[tileRegion(combined)]) return [combined];
  const landKey = land.join("+");
  const layers = art.tileRules[landKey] ?? (art.regions[tileRegion(landKey)] ? [landKey] : land);
  return improvement ? [...layers, improvement] : layers;
}

function drawEmblem() {
  const context = emblem.getContext("2d");
  context.clearRect(0, 0, emblem.width, emblem.height);
  emblem.hidden = !sprite(context, `NationIcons/${civilization}`, 28, 28, 52);
}

function upperBound(rows, value) {
  let lo = 0, hi = rows.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (rows[mid][0] <= value) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function newScene() {
  return {tiles: new Map(), visible: new Set(), units: new Map(), path: [], caption: "Opening observations",
    observedTurn: null, territory: false, dashedPath: false, details: new Map(), cities: new Map(), progress: null};
}

function updateScene(scene, event) {
  const kind = event.kind;
  if (kind === "chapter") {
    Object.assign(scene, newScene());
  } else if (kind === "turnSnapshot" || kind === "territorySnapshot") {
    for (const tile of event.removed) scene.tiles.delete(sightKey(tile));
    for (const tile of event.tiles) scene.tiles.set(sightKey(tile.position), tile);
    scene.territory = kind === "territorySnapshot";
    scene.observedTurn = event.turn;
    scene.visible = new Set(scene.territory ? scene.tiles.keys() : event.visible.map(sightKey));
    scene.units = new Map((event.units ?? []).map(unit => [unit.unit, unit]));
    setProgress(scene, event.progress);
    scene.path = [];
    scene.caption = scene.territory ? civilization + " territory history" : civilization + " · map snapshot";
  } else if (kind === "checkpointDetails") {
    for (const tile of event.tiles) scene.details.set(sightKey(tile.position), tile.details);
    setProgress(scene, event.progress);
  } else if (kind === "snapshotAction") {
    scene.path = event.path;
    scene.dashedPath = true;
    scene.caption = event.text;
  } else if (kind === "visibilityReset") {
    scene.visible.clear();
  } else if (kind === "visibility") {
    for (const tile of event.fogged) scene.visible.delete(sightKey(tile));
    for (const tile of event.visible) scene.visible.add(sightKey(tile));
  } else if (kind === "tiles" || kind === "exploration") {
    for (const tile of event.tiles) {
      const key = sightKey(tile.position);
      const prior = scene.tiles.get(key);
      if (kind === "tiles") {
        for (const unit of prior?.units ?? []) {
          const current = scene.units.get(unit.unit);
          if (current && sightKey(current.position) === key) scene.units.delete(unit.unit);
        }
        for (const unit of tile.units) scene.units.set(unit.unit, {...unit, position: tile.position});
      }
      scene.tiles.set(key, {...prior, ...tile});
    }
  } else if (kind.startsWith("unit")) {
    if (event.to) scene.units.set(event.unit, {...scene.units.get(event.unit), ...event, position: event.to});
    else scene.units.delete(event.unit);
    scene.path = [event.from, ...(event.transit ?? []), event.to].filter(Boolean);
    const verbs = {unitMoved: "moves", unitPlaced: "appears", unitAppeared: "comes into sight",
      unitRemoved: "is removed", unitDisappeared: "leaves sight"};
    scene.caption = event.owner + " · " + event.name + " " + verbs[kind];
  } else if (kind === "turnStarted") {
    scene.path = [];
    scene.caption = civilization + " begins its turn";
  } else if (kind === "combat") {
    const {locations = [], text} = event.report;
    // Unciv's primary attack reports list the target first and include the source only when known.
    scene.path = event.report.kind === "ObservedAttackResult" && event.report.showZeroDamage && locations.length === 2
      ? [locations[1], locations[0]]
      : [];
    scene.caption = text || "Combat observed";
  }
}

function sceneAt(rows, index) {
  let entry = snapshots.get(civilization);
  if (!entry) {
    entry = {index: 0, scene: newScene(), anchors: new Map([[0, newScene()]])};
    snapshots.set(civilization, entry);
  }
  if (entry.index > index) {
    const floor = Math.max(...[...entry.anchors.keys()].filter(key => key <= index));
    entry.index = floor;
    entry.scene = structuredClone(entry.anchors.get(floor));
  }
  while (entry.index < index) {
    updateScene(entry.scene, rows[entry.index][1]);
    entry.index += 1;
    if (entry.index % 192 === 0) {
      entry.anchors.set(entry.index, structuredClone(entry.scene));
      if (entry.anchors.size > 10) entry.anchors.delete([...entry.anchors.keys()][1]);
    }
  }
  return entry.scene;
}

function nationColors(owner) {
  const palette = art.nationColors?.[owner];
  if (palette) return palette.map(rgb => "rgb(" + rgb.slice(0, 3).join(",") + ")");
  let hash = 0;
  for (const letter of owner ?? "") hash = (33 * hash + letter.codePointAt(0)) >>> 0;
  return [colors[hash % colors.length], "#172321"];
}

function drawBorders(scene, tile, x, y, r) {
  const neighbors = neighborOffsets.map(([dx, dy]) => scene.tiles.get(sightKey({
    x: tile.position.x + dx, y: tile.position.y + dy,
  })));
  const [outer, inner] = nationColors(tile.owner);
  function joinsBorder(neighbor) {
    return neighbor?.owner === tile.owner || (!neighbor?.terrain && !scene.territory);
  }
  for (const [index, neighbor] of neighbors.entries()) {
    if (joinsBorder(neighbor)) continue;
    // In the south-facing texture, the next clockwise neighbor is on the left.
    const left = joinsBorder(neighbors[(index + 1) % 6]);
    const right = joinsBorder(neighbors[(index + 5) % 6]);
    const shape = left === right ? (left ? "Concave" : "Convex")
      : (left ? "ConcaveConvex" : "ConvexConcave");
    const edge = r * Math.sqrt(3) / 2;
    const prefix = "TileSets/FantasyHex/Borders/" + shape;
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(index * Math.PI / 3);
    if (!sprite(ctx, prefix + "Inner", 0, edge, r * 2, {bottom: true, color: outer})) {
      ctx.fillStyle = outer;
      ctx.fillRect(-r / 2, edge - 2.5, r, 2);
    }
    if (!sprite(ctx, prefix + "Outer", 0, edge, r * 2, {bottom: true, color: inner})) {
      ctx.fillStyle = inner;
      ctx.fillRect(-r / 2, edge - .7, r, .7);
    }
    ctx.restore();
  }
}

function drawHealthBar(left, top, width, height, health) {
  const fraction = Math.max(0, Math.min(1, health));
  ctx.fillStyle = "#111111";
  ctx.fillRect(left, top, width, height);
  ctx.fillStyle = fraction > 2 / 3 ? "#64d347" : fraction > 1 / 3 ? "#f19c35" : "#e53732";
  ctx.fillRect(left, top, width * fraction, height);
}

function drawCityLabel(tile, x, y, r, visible, production, details) {
  const [outer, inner] = nationColors(tile.owner);
  const font = Math.min(16, Math.max(11, r * .61)) * textScale;
  const pad = font * .47;
  const height = font * 1.42;
  const top = y + r * .56;
  const icon = `NationIcons/${tile.owner}`;
  const iconSpace = art.regions?.[icon] ? font + pad * .6 : 0;
  ctx.save();
  ctx.globalAlpha = visible ? 1 : .62;
  ctx.font = `600 ${font}px system-ui, sans-serif`;
  const population = production?.population ?? details?.cityPopulation;
  const name = population != null ? tile.city + " · Pop " + population : tile.city;
  const nameWidth = ctx.measureText(name).width + pad * 2 + iconSpace;
  const building = production?.queue[0];
  const turns = production?.turns;
  const productionLabel = building && Number.isInteger(turns) && turns >= 0
    ? building + " · " + turns + (turns === 1 ? " turn" : " turns")
    : building;
  const productionFont = `500 ${Math.max(10 * textScale, font * .76)}px system-ui, sans-serif`;
  ctx.font = productionFont;
  const width = Math.max(nameWidth, productionLabel ? ctx.measureText(productionLabel).width + pad * 2 : 0);
  const {cityHealth, cityMaxHealth} = details ?? {};
  const hasHealth = Number.isFinite(cityHealth) && Number.isFinite(cityMaxHealth) && cityMaxHealth > 0;
  const textHeight = height + (productionLabel ? font : 0);
  const barHeight = Math.max(3, font * .24);
  ctx.font = `600 ${font}px system-ui, sans-serif`;
  ctx.beginPath();
  ctx.roundRect(x - width / 2, top, width, textHeight + (hasHealth ? barHeight + pad * .65 : 0), height * .27);
  ctx.fillStyle = outer;
  ctx.fill();
  ctx.lineWidth = tile.owner === civilization ? 1.4 : 1;
  ctx.strokeStyle = tile.owner === civilization ? "#e9e9ac" : "#111111";
  ctx.stroke();
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = inner;
  ctx.fillText(name, x - iconSpace / 2, top + height / 2);
  if (iconSpace) sprite(ctx, icon, x + nameWidth / 2 - pad - font / 2, top + height / 2, font, {color: inner});
  if (building) {
    ctx.font = productionFont;
    ctx.fillText(productionLabel, x, top + height + font * .41);
  }
  if (hasHealth) drawHealthBar(x - width / 2 + pad, top + textHeight - 1, width - pad * 2, barHeight, cityHealth / cityMaxHealth);
  ctx.restore();
}

function drawResource(resource, x, y, r) {
  const name = `ResourceIcons/${resource}`;
  if (!sheets.has(art.regions?.[name]?.[0])) return;
  const size = Math.min(24, r * .84);
  const centerX = x - r * .44, centerY = y - r * .30;
  ctx.beginPath();
  ctx.arc(centerX, centerY, size / 2, 0, 2 * Math.PI);
  ctx.fillStyle = resourceColors[art.resourceTypes?.[resource]] ?? "#a8c3c9";
  ctx.fill();
  ctx.strokeStyle = "#111111";
  ctx.lineWidth = Math.max(.7, size * .085);
  ctx.stroke();
  sprite(ctx, name, centerX, centerY, size * .77);
}

function drawUnitFlag(unit, x, y, width) {
  const [outer, inner] = nationColors(unit.owner);
  const civilian = civilianUnits.has(unit.name);
  const flag = "UnitFlagIcons/UnitFlag" + (civilian ? "Civilian" : "");
  if (sprite(ctx, flag, x, y, width, {color: inner})) {
    if (!sprite(ctx, flag + "Inner", x, y, width, {color: outer})) {
      sprite(ctx, flag, x, y, width * .88, {color: outer});
    }
  } else {
    ctx.beginPath();
    ctx.arc(x, y, width / 2, 0, 2 * Math.PI);
    ctx.fillStyle = outer;
    ctx.fill();
    ctx.strokeStyle = inner;
    ctx.lineWidth = 1;
    ctx.stroke();
  }
  if (!sprite(ctx, `UnitIcons/${unit.name}`, x, y, width * (civilian ? .50 : .65), {color: inner})) {
    sprite(ctx, `NationIcons/${unit.owner}`, x, y, width * .55, {color: inner});
  }
  if (unit.health >= 0 && unit.health < 100) {
    const bar = width * .78, height = Math.max(2, width / 10), health = unit.health / 100;
    const left = x - bar / 2, top = y + width / 2 - height / 2;
    drawHealthBar(left, top, bar, height, health);
  }
}

function traceHex(context, x, y, radius) {
  context.moveTo(x + radius, y);
  for (let index = 1; index < hexCorners.length; index++) {
    const [dx, dy] = hexCorners[index];
    context.lineTo(x + dx * radius, y + dy * radius);
  }
  context.closePath();
}

function drawMap(scene) {
  mapGeometry = null;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = "#101917";
  ctx.fillRect(0, 0, width, height);
  ctx.font = `${15 * textScale}px system-ui, sans-serif`;
  if (!scene.tiles.size) {
    ctx.fillStyle = "#a9bcb3";
    ctx.textAlign = "center";
    ctx.fillText("No map observations at this position", width / 2, height / 2);
    return;
  }
  ctx.textAlign = "left";
  const points = [...scene.tiles.values()].map(tile => hexXY(tile.position));
  let left = Infinity, right = -Infinity, top = Infinity, bottom = -Infinity;
  for (const [x, y] of points) {
    left = Math.min(left, x); right = Math.max(right, x);
    top = Math.min(top, y); bottom = Math.max(bottom, y);
  }
  const fit = Math.max(1, Math.min(32, (width - 38) / (right - left + 3), (height - 38) / (bottom - top + 3)));
  const r = fit * camera.zoom;
  const [cx, cy] = camera.center ?? [(left + right) / 2, (top + bottom) / 2];
  mapGeometry = {left, right, top, bottom, width, height, cx, cy, r, fit};
  function screen(tile) {
    const [x, y] = hexXY(tile);
    return [width / 2 + (x - cx) * r, height / 2 + (y - cy) * r];
  }
  const offscreen = (x, y) => x < -r - 100 || y < -r - 50 || x > width + r + 100 || y > height + r + 50;
  for (const [key, tile] of scene.tiles) {
    const [x, y] = screen(tile.position);
    if (offscreen(x, y)) continue;
    ctx.globalAlpha = scene.visible.has(key) ? 1 : .36;
    ctx.beginPath();
    traceHex(ctx, x, y, r);
    ctx.fillStyle = terrain[tile.terrain] || "#354741";
    ctx.fill();
  }
  const tileList = [...scene.tiles.values()].sort((a, b) => hexXY(a.position)[1] - hexXY(b.position)[1]);
  for (const tile of tileList) {
    const [x, y] = screen(tile.position);
    if (offscreen(x, y)) continue;
    ctx.globalAlpha = scene.visible.has(sightKey(tile.position)) ? 1 : .45;
    for (const layer of tileLayers(tile)) {
      sprite(ctx, tileRegion(layer), x, y + Math.sqrt(3) * r / 2, 2 * r, {bottom: true});
    }
  }
  ctx.globalAlpha = 1;
  ctx.beginPath();
  for (const tile of tileList) {
    if (!tile.terrain) continue;
    const [x, y] = screen(tile.position);
    if (!offscreen(x, y)) traceHex(ctx, x, y, r);
  }
  ctx.strokeStyle = "rgba(8, 16, 17, .34)";
  ctx.lineWidth = Math.min(1, Math.max(.6, r * .065));
  ctx.stroke();
  for (const [key, tile] of scene.tiles) {
    if (!tile.owner) continue;
    const [x, y] = screen(tile.position);
    if (offscreen(x, y)) continue;
    ctx.globalAlpha = scene.visible.has(key) ? 1 : .42;
    drawBorders(scene, tile, x, y, r);
  }
  const selected = scene.tiles.get(selectedTile);
  if (selected?.terrain) {
    const [x, y] = screen(selected.position);
    ctx.globalAlpha = 1;
    ctx.beginPath();
    traceHex(ctx, x, y, r * .84);
    ctx.fillStyle = "#ffeb992b";
    ctx.fill();
    ctx.strokeStyle = "#151b18";
    ctx.lineWidth = 4;
    ctx.stroke();
    ctx.strokeStyle = "#ffe28a";
    ctx.lineWidth = 2;
    ctx.stroke();
  }
  for (const [key, tile] of scene.tiles) {
    const resource = (tile.details ?? scene.details.get(key))?.resource;
    if (!resource) continue;
    const [x, y] = screen(tile.position);
    if (offscreen(x, y)) continue;
    ctx.globalAlpha = scene.visible.has(key) ? 1 : .5;
    drawResource(resource, x, y, r);
  }

  ctx.globalAlpha = 1;
  const occupied = new Map();
  for (const unit of scene.units.values()) {
    const key = sightKey(unit.position);
    if (!scene.visible.has(key) && unit.owner !== civilization) continue;
    if (!occupied.has(key)) occupied.set(key, []);
    occupied.get(key).push(unit);
  }
  const banners = [];
  for (const units of occupied.values()) {
    units.sort((a, b) => Number(civilianUnits.has(a.name)) - Number(civilianUnits.has(b.name)) || a.unit - b.unit);
    for (const [index, unit] of units.entries()) {
      const [x, y] = screen(unit.position);
      if (offscreen(x, y)) continue;
      const unitWidth = Math.max(14, r * Math.min(1.28, 1.9 / units.length));
      const flagWidth = Math.min(25, Math.max(13, r * .93));
      const spacing = Math.max(unitWidth * .77, flagWidth + 1);
      const unitX = x + (index - (units.length - 1) / 2) * spacing;
      const foot = y + r * .39;
      const base = `TileSets/AbsoluteUnits/Units/${unit.name}`;
      const options = {bottom: true};
      if (!sprite(ctx, `${base}-${unit.owner}`, unitX, foot, unitWidth, options)) {
        sprite(ctx, base, unitX, foot, unitWidth, options);
      }
      banners.push([unit, unitX, y - unitWidth * .44 - flagWidth * .5, flagWidth]);
    }
  }
  if (scene.path.length > 1) {
    const path = scene.path.map(screen);
    if (scene.dashedPath) ctx.setLineDash([5, 4]);
    ctx.beginPath();
    path.forEach(([x, y], index) => index ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
    ctx.strokeStyle = "#fff1c5";
    ctx.lineWidth = 2.5;
    ctx.stroke();
    ctx.setLineDash([]);
    const [x, y] = path.at(-1), [px, py] = path.at(-2);
    const angle = Math.atan2(y - py, x - px);
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x - 8 * Math.cos(angle - .5), y - 8 * Math.sin(angle - .5));
    ctx.moveTo(x, y);
    ctx.lineTo(x - 8 * Math.cos(angle + .5), y - 8 * Math.sin(angle + .5));
    ctx.stroke();
  }
  for (const banner of banners) drawUnitFlag(...banner);
  for (const [key, tile] of scene.tiles) {
    if (!tile.city) continue;
    const [x, y] = screen(tile.position);
    if (offscreen(x, y)) continue;
    const production = tile.owner === civilization ? scene.cities.get(key) : null;
    const details = tile.details ?? scene.details.get(key);
    drawCityLabel(tile, x, y, r, scene.visible.has(key), production, details);
  }
}

function element(tag, text, className) {
  const result = document.createElement(tag);
  result.textContent = text;
  if (className) result.className = className;
  return result;
}

let lastNotes;
let reviewsPanel;
let highlightsPanel;
let highlightsCivilization;
let renderedRelations;

function showRelations(turn) {
  lastNotes = null;
  lastProgress = null;
  const rows = relationHistory[upperBound(relationHistory, position) - 1]?.[1] ?? [];
  if (renderedRelations?.rows === rows && renderedRelations.turn === turn && renderedRelations.table.isConnected) return;
  const table = element("table", "", "relations-table");
  table.createCaption().textContent = "Diplomatic relations · Turn " + turn;
  const header = table.createTHead().insertRow();
  for (const label of ["Civilizations", "Relation"]) {
    const cell = element("th", label);
    cell.scope = "col";
    header.append(cell);
  }
  const body = table.createTBody();
  for (const [first, second, labels] of rows) {
    const row = body.insertRow();
    const pair = element("th", first + " · " + second);
    pair.scope = "row";
    const cell = element("td", "");
    for (const label of labels) {
      const badge = element("span", label, "relation-status");
      badge.dataset.relation = label;
      cell.append(badge);
    }
    row.append(pair, cell);
  }
  const notes = get("notes");
  notes.replaceChildren(table);
  if (replay.reconstruction) {
    notes.append(element("p", "Earlier turns use archived player reports and confirmed diplomatic events.", "meta"));
  }
  renderedRelations = {rows, turn, table};
}

function highlightStep(target, perspective) {
  if (Number.isInteger(target.step)) return target.step;
  const at = replay.turns.findIndex(([, turn]) => turn === target.turn);
  if (at < 0) return 0;
  const start = replay.turns[at][0];
  const end = replay.turns[at + 1]?.[0] ?? replay.lastStep + 1;
  if (!target.event) return start;
  const view = replay.civilizations[perspective];
  const picked = [...view.decisions, ...view.diplomacy, ...view.map].find(([step, event]) =>
    step >= start && step < end &&
    (event.title === target.event || event.report?.text === target.event) &&
    (!target.detail || event.text?.some(text => text.includes(target.detail))));
  return picked?.[0] ?? start;
}

function showHighlights() {
  lastNotes = null;
  lastProgress = null;
  if (highlightsCivilization !== civilization) {
    highlightsPanel = element("section", "");
    highlightsCivilization = civilization;
    const model = replay.playerLabels?.[civilization];
    highlightsPanel.append(element("p", [civilization, model].filter(Boolean).join(" · "), "meta"));
    const notes = highlights[civilization] ?? [];
    for (const note of notes) {
      const card = element("article", "");
      card.append(element("h2", note.title), element("p", note.text));
      const links = element("div", "", "highlight-jumps");
      for (const target of note.jumps) {
        const button = element("button", target.label);
        button.addEventListener("click", () => {
          const perspective = target.civilization ?? highlightsCivilization;
          if (perspective !== civilization) {
            picker.value = perspective;
            picker.dispatchEvent(new Event("change"));
          }
          jump(highlightStep(target, perspective));
          const bounds = canvas.getBoundingClientRect();
          if (bounds.bottom < 0 || bounds.top > window.innerHeight) canvas.scrollIntoView({block: "nearest"});
        });
        links.append(button);
      }
      card.append(links);
      highlightsPanel.append(card);
    }
    if (!notes.length) highlightsPanel.append(element("p", "No highlights for this civilization.", "empty"));
  }
  if (!highlightsPanel.isConnected) get("notes").replaceChildren(highlightsPanel);
}

function reviewList(section, heading, values) {
  if (!values?.length) return;
  const list = element("ul", "");
  for (const value of values) list.append(element("li", value));
  section.append(element("h3", heading), list);
}

function noteCard(note, fullReview = false) {
  const card = element("article", "");
  const recipients = note.parties?.filter(party => party !== note.author) ?? [];
  const participants = note.author ? [note.author, ...recipients].join(recipients.length ? " → " : "") : recipients.join(" · ");
  const source = note.sourceTurn != null && note.sourceTurn !== note.turn ? "Based on turn " + note.sourceTurn : "";
  const details = [note.timingUnknown ? "Delivery time unavailable" : "Turn " + note.turn,
    participants, source, fullReview && note.horizonTurns ? note.horizonTurns + "-turn outlook" : "",
    note.turnOnly && !fullReview ? "Turn-level timing" : ""];
  card.append(element("h2", note.title), element("p", details.filter(Boolean).join(" · "), "meta"));
  for (const text of note.text) card.append(element("p", text));
  if (note.plans?.length) {
    const plans = element("details", "");
    plans.open = fullReview;
    plans.append(element("summary", fullReview ? "Proposed plans" : "Compare proposed plans"));
    for (const plan of note.plans) {
      const section = element("section", "", "review-plan");
      section.append(element("h3", plan.title + (plan.preferred ? " · preferred" : "")));
      for (const text of [plan.objective, fullReview && plan.investment, plan.tradeoff, plan.risks].filter(Boolean)) {
        section.append(element("p", text));
      }
      if (fullReview) {
        reviewList(section, "First actions", plan.firstActions);
        reviewList(section, "Expected results", plan.expectedObservations);
      }
      plans.append(section);
    }
    card.append(plans);
  }
  if (fullReview) {
    if (note.assumptions?.length) {
      const assumptions = element("details", "");
      assumptions.append(element("summary", "Assumptions"));
      const list = element("ul", "");
      for (const item of note.assumptions) list.append(element("li", item));
      assumptions.append(list);
      card.append(assumptions);
    }
    if (note.diplomacy?.assessment || note.diplomacy?.initiatives?.length) {
      const diplomacy = element("details", "");
      diplomacy.append(element("summary", "Diplomatic advice"));
      if (note.diplomacy.assessment) diplomacy.append(element("p", note.diplomacy.assessment));
      for (const initiative of note.diplomacy.initiatives ?? []) {
        const name = note.plans?.[initiative.plan - 1]?.title;
        diplomacy.append(element("h3", [initiative.players.join(", "), name].filter(Boolean).join(" · ")));
        for (const text of [initiative.objective, initiative.incentives, initiative.approach,
          initiative.nextStep, initiative.reconsiderWhen].filter(Boolean)) diplomacy.append(element("p", text));
      }
      card.append(diplomacy);
    }
  }
  return card;
}

function showReviews(view) {
  lastNotes = null;
  lastProgress = null;
  view.reviews ??= view.decisions.filter(([, note]) => isReview(note));
  const rows = view.reviews;
  const index = Math.max(0, upperBound(rows, position) - 1);
  if (reviewsPanel?.rows !== rows) {
    const content = element("section", "", "review-report");
    const report = element("div", "");
    const select = element("select", "");
    if (rows.length) {
      const label = element("label", "Review turn", "review-picker");
      for (const [step, note] of [...rows].reverse()) {
        select.add(new Option(note.turn === 0 ? "Opening review" : "Turn " + note.turn, step));
      }
      label.append(select);
      content.append(label, report);
      select.addEventListener("change", () => jump(Number(select.value)));
    } else {
      content.append(element("p", "No reviews recorded for " + civilization + ".", "empty"));
    }
    reviewsPanel = {content, report, rows, select};
  }
  if (!reviewsPanel.content.isConnected) get("notes").replaceChildren(reviewsPanel.content);
  if (!rows.length || reviewsPanel.index === index) return;
  const [step, note] = rows[index];
  reviewsPanel.index = index;
  reviewsPanel.select.value = step;
  reviewsPanel.report.replaceChildren(noteCard(note, true));
}

function showNotes(rows, index, count = 30) {
  if (lastNotes?.rows === rows && lastNotes.index === index && lastNotes.count >= count) return;
  lastNotes = {rows, index, count};
  const fragment = document.createDocumentFragment();
  for (const [, note] of rows.slice(Math.max(0, index - count), index).reverse()) fragment.append(noteCard(note));
  if (index === 0) fragment.append(element("p", panel === "decisions"
    ? "No agent decisions at this position. Built-in AI has no agent notebook."
    : "No diplomacy observed at this position.", "empty"));
  if (index > count) {
    const earlier = element("button", "Show earlier entries", "earlier");
    earlier.addEventListener("click", () => showNotes(rows, index, count + 30));
    fragment.append(earlier);
  }
  get("notes").replaceChildren(fragment);
}

function stops(view) {
  if (!view.stops) view.stops = [...new Set([
    ...view.map.filter(([, item]) => !["chapter", "visibilityReset", "exploration", "visibility"].includes(item.kind)),
    ...view.decisions, ...view.diplomacy,
    ...relationHistory,
    ...(replay.reconstruction ? replay.turns : []),
  ].map(([step]) => step))].sort((a, b) => a - b).map(step => [step]);
  return view.stops;
}

function draw() {
  const view = replay.civilizations[civilization];
  const [turnStart, turn] = replay.turns[upperBound(replay.turns, position) - 1];
  const scene = sceneAt(view.map, upperBound(view.map, position));
  activeScene = scene;
  drawMap(scene);
  drawNavigation(scene);
  drawEmblem();
  get("map-caption").textContent = scene.caption;
  if (panel === "research" || panel === "culture") showProgress(scene, panel);
  else if (panel === "statistics") showStatistics(turn);
  else if (panel === "highlights") showHighlights();
  else if (panel === "relations") showRelations(turn);
  else if (panel === "reviews") showReviews(view);
  else {
    lastProgress = null;
    showNotes(view[panel], upperBound(view[panel], position));
  }
  turnPicker.value = turnStart;
  const label = "Turn " + turn + (replay.reconstruction ? " · Step " : " · Event ") +
    formatter.format(position + 1) + " / " + formatter.format(replay.lastStep + 1);
  get("position").textContent = label;
  if (replay.reconstruction) {
    const source = get("replay-source");
    source.hidden = false;
    source.textContent = view.territory ? "Territory history over saved terrain" :
      scene.observedTurn == null ? "Map snapshot unavailable" : "Map snapshot from turn " + scene.observedTurn;
  }
  showVictory();
  seek.value = position;
  seek.setAttribute("aria-valuetext", label);
  controls.first.disabled = controls.previous.disabled = position === 0;
  controls.last.disabled = controls.next.disabled = controls.play.disabled = position === replay.lastStep;
}

const victoryTypes = {Scientific: "Science victory", Cultural: "Cultural victory",
  Diplomatic: "Diplomatic victory", Domination: "Domination victory", Time: "Time victory"};

function showVictory() {
  const outcome = replay.outcome;
  const banner = get("victory");
  banner.hidden = !outcome || position !== replay.lastStep;
  if (banner.hidden) return;
  const {winningCiv, victoryType, victoryTurn} = outcome;
  const model = replay.playerLabels?.[winningCiv];
  const emblem = get("winner-emblem");
  const context = emblem.getContext("2d");
  context.clearRect(0, 0, emblem.width, emblem.height);
  emblem.hidden = !sprite(context, `NationIcons/${winningCiv}`, 32, 32, 57);
  get("winner-name").textContent = winningCiv + " wins";
  get("victory-detail").textContent = [victoryTypes[victoryType] ?? victoryType,
    "Turn " + victoryTurn, model].filter(Boolean).join(" · ");
}

function requestDraw() {
  if (!scheduledPaint) scheduledPaint = requestAnimationFrame(() => {
    scheduledPaint = 0;
    draw();
  });
}

function pause() {
  if (playing !== null) clearInterval(playing);
  playing = null;
  controls.play.textContent = "▶";
  controls.play.setAttribute("aria-label", "Play");
}

function jump(step, manual = true) {
  if (manual) pause();
  position = Math.max(0, Math.min(replay.lastStep, step));
  if (position === replay.lastStep) pause();
  requestDraw();
}

function advance(direction, manual = true) {
  const events = stops(replay.civilizations[civilization]);
  const index = upperBound(events, direction > 0 ? position : position - 1);
  jump(direction > 0 ? events[index]?.[0] ?? replay.lastStep : events[index - 1]?.[0] ?? 0, manual);
}

function togglePlay() {
  if (playing !== null) pause();
  else if (position < replay.lastStep) {
    playing = setInterval(() => advance(1, false), 525);
    controls.play.textContent = "❚❚";
    controls.play.setAttribute("aria-label", "Pause");
  }
}

picker.addEventListener("change", () => {
  pause();
  civilization = picker.value;
  selectedTile = null;
  fitMap();
});
turnPicker.addEventListener("change", () => jump(Number(turnPicker.value)));
seek.addEventListener("input", () => jump(Number(seek.value)));
controls.first.addEventListener("click", () => jump(0));
controls.previous.addEventListener("click", () => advance(-1));
controls.next.addEventListener("click", () => advance(1));
controls.last.addEventListener("click", () => jump(replay.lastStep));
controls.play.addEventListener("click", togglePlay);
for (const tab of tabs) tab.addEventListener("click", () => {
  panel = tab.dataset.panel;
  tabs.forEach(button => button.setAttribute("aria-pressed", String(button === tab)));
  requestDraw();
});
document.addEventListener("keydown", event => {
  if (event.target.closest("button, select, input, summary, #notes, #minimap, dialog")) return;
  if (event.key === "ArrowLeft") advance(-1);
  else if (event.key === "ArrowRight") advance(1);
  else if (event.key === " ") togglePlay();
  else return;
  event.preventDefault();
});
new ResizeObserver(requestDraw).observe(canvas);
