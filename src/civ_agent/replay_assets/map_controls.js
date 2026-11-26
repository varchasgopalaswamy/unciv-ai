const camera = {zoom: 1, center: null};
const minimap = get("minimap");
const miniContext = minimap.getContext("2d");
const inspector = get("tile-inspector");
let mapGeometry = null;
let minimapGeometry = null;
let activeScene = null;
let hoverPoint = null;
let selectedTile = null;
const mapPointers = new Map();
let gesture = null;

function mapPoint(event, element) {
  const box = element.getBoundingClientRect();
  return [event.clientX - box.left, event.clientY - box.top];
}

function mapWorldPoint([x, y], geometry) {
  const {cx, cy, width, height, r} = geometry;
  return [cx + (x - width / 2) / r, cy + (y - height / 2) / r];
}

function tileAt(x, y) {
  // The replay axes point northwest and northeast. Round in cube coordinates.
  const q = (-2 * y / Math.sqrt(3) - x / 1.5) / 2;
  const t = (2 * y / Math.sqrt(3) - x / 1.5) / 2;
  const s = -q - t;
  let col = Math.round(q), row = Math.round(t);
  const layer = Math.round(s);
  const dq = Math.abs(col - q), dt = Math.abs(row - t), ds = Math.abs(layer - s);
  if (dq > dt && dq > ds) col = -row - layer;
  else if (dt > ds) row = -col - layer;
  return col + "," + (-row);
}

function inspectTile() {
  const key = hoverPoint && mapGeometry ? tileAt(...mapWorldPoint(hoverPoint, mapGeometry)) : selectedTile;
  const tile = key && activeScene?.tiles.get(key);
  inspector.hidden = !tile?.terrain;
  if (inspector.hidden) return;
  inspector.classList.toggle("above-tile", key === selectedTile && mapGeometry &&
    hexXY(tile.position)[1] > mapGeometry.cy);
  const visible = activeScene.visible.has(key);
  const details = tile.details ?? activeScene.details.get(key) ?? {};
  const production = tile.owner === civilization ? activeScene.cities.get(key) : null;
  const title = tile.city || tile.terrain;
  const status = activeScene.territory ? "Recorded territory" : visible ? "Visible" : "Fogged";
  const location = "(" + tile.position.x + ", " + tile.position.y + ") · " + status;
  inspector.replaceChildren(element("h2", title), element("p", location, "meta"));
  if (tile.city || tile.features?.length || details.naturalWonder) {
    inspector.append(element("p", [tile.terrain, ...(tile.features ?? []), details.naturalWonder].filter(Boolean).join(" · ")));
  }
  function fact(text) {
    if (text) inspector.append(element("p", text));
  }
  fact(tile.owner && "Owner · " + tile.owner);
  const population = production?.population ?? details.cityPopulation;
  fact(population != null && "Population · " + population);
  fact(details.cityHealth != null && "City health · " + details.cityHealth + (details.cityMaxHealth ? " / " + details.cityMaxHealth : ""));
  fact(production && "Production · " + (production.queue.join(" → ") || "No queued construction"));
  if (production?.queue.length && production.stored != null &&
      (production.cost > 0 || production.stored > 0)) {
    fact(production.stored + (production.cost > 0 ? " / " + production.cost : "") + " production" +
      (production.turns != null ? " · " + production.turns + " turns at recorded rate" : ""));
  }
  fact(details.resource && "Resource · " + details.resource + (details.resourceAmount != null ? " × " + details.resourceAmount : ""));
  fact(tile.improvement && "Improvement · " + tile.improvement + (details.improvementPillaged ? " (pillaged)" : ""));
  fact(details.improvementInProgress && "Under construction · " + details.improvementInProgress +
    (details.improvementTurns != null ? " · " + details.improvementTurns + " turns" : ""));
  fact(details.road && "Route · " + details.road + (details.roadPillaged ? " (pillaged)" : ""));
  fact((details.riverBottom || details.riverBottomLeft || details.riverBottomRight) && "River edge");
  fact(details.yields?.length && details.yields.map(yieldValue => yieldValue.amount + " " + yieldValue.stat).join(" · "));
  for (const unit of activeScene.units.values()) {
    if (sightKey(unit.position) === key && (visible || unit.owner === civilization)) {
      fact(unit.name + " · " + unit.owner + (unit.health != null ? " · " + unit.health + " HP" : ""));
    }
  }
  if (!visible && details.observedTurn != null) fact("Observed on turn " + details.observedTurn);
}

function drawNavigation(scene) {
  const width = minimap.width, height = minimap.height;
  const context = miniContext;
  context.fillStyle = "#101917";
  context.fillRect(0, 0, width, height);
  get("zoom-in").disabled = !mapGeometry || camera.zoom >= 8;
  get("zoom-out").disabled = !mapGeometry || camera.zoom <= 1;
  get("zoom-fit").disabled = !mapGeometry || camera.zoom === 1 && !camera.center;
  minimapGeometry = null;
  if (mapGeometry) {
    const {left, right, top, bottom, cx, cy, r} = mapGeometry;
    const midX = (left + right) / 2, midY = (top + bottom) / 2;
    const radius = Math.min((width - 10) / (right - left + 2), (height - 10) / (bottom - top + 2));
    minimapGeometry = {cx: midX, cy: midY, r: radius, width, height};
    for (const [key, tile] of scene.tiles) {
      const [worldX, worldY] = hexXY(tile.position);
      const x = width / 2 + (worldX - midX) * radius, y = height / 2 + (worldY - midY) * radius;
      context.globalAlpha = scene.visible.has(key) ? 1 : .5;
      context.fillStyle = tile.owner ? nationColors(tile.owner)[0] : terrain[tile.terrain] || "#354741";
      context.beginPath();
      traceHex(context, x, y, radius);
      context.fill();
    }
    context.globalAlpha = 1;
    const w = Math.min(width - 2, mapGeometry.width * radius / r);
    const h = Math.min(height - 2, mapGeometry.height * radius / r);
    const x = width / 2 + (cx - midX) * radius, y = height / 2 + (cy - midY) * radius;
    const l = Math.max(1, x - w / 2), t = Math.max(1, y - h / 2);
    context.strokeStyle = "#fff1c5";
    context.strokeRect(l, t, Math.max(0, Math.min(width - 1, x + w / 2) - l), Math.max(0, Math.min(height - 1, y + h / 2) - t));
  }
  inspectTile();
}

function centerMap(x, y) {
  const {left, right, top, bottom} = mapGeometry;
  camera.center = [Math.min(right + 1, Math.max(left - 1, x)), Math.min(bottom + 1, Math.max(top - 1, y))];
  requestDraw();
}

function mapCameraGeometry() {
  const [cx, cy] = camera.center ?? [mapGeometry.cx, mapGeometry.cy];
  return {...mapGeometry, cx, cy, r: mapGeometry.fit * camera.zoom};
}

function moveMap(zoom, [x, y], [worldX, worldY]) {
  const {width, height, fit} = mapGeometry;
  camera.zoom = Math.min(8, Math.max(1, zoom));
  const radius = fit * camera.zoom;
  centerMap(worldX - (x - width / 2) / radius, worldY - (y - height / 2) / radius);
}

function zoomMap(factor, point) {
  if (!mapGeometry) return;
  const zoom = Math.min(8, Math.max(1, camera.zoom * factor));
  if (zoom === camera.zoom) return;
  const anchor = point ?? [mapGeometry.width / 2, mapGeometry.height / 2];
  moveMap(zoom, anchor, mapWorldPoint(anchor, mapCameraGeometry()));
}

function fitMap() {
  camera.zoom = 1;
  camera.center = null;
  hoverPoint = null;
  requestDraw();
}

get("zoom-in").addEventListener("click", () => zoomMap(1.5));
get("zoom-out").addEventListener("click", () => zoomMap(1 / 1.5));
get("zoom-fit").addEventListener("click", fitMap);
canvas.addEventListener("wheel", event => {
  if (!mapGeometry || event.deltaY === 0) return;
  event.preventDefault();
  zoomMap(event.deltaY < 0 ? 1.2 : 1 / 1.2, mapPoint(event, canvas));
}, {passive: false});

function pointerSpread() {
  const [[x, y], second] = mapPointers.values();
  if (!second) return {point: [x, y], distance: 1};
  const [otherX, otherY] = second;
  return {point: [(x + otherX) / 2, (y + otherY) / 2], distance: Math.max(1, Math.hypot(x - otherX, y - otherY))};
}

function startGesture(type, moved = false) {
  const {point, distance} = pointerSpread();
  gesture = {type, point, distance, world: mapWorldPoint(point, mapCameraGeometry()), zoom: camera.zoom, moved};
}

canvas.addEventListener("pointerdown", event => {
  if (!mapGeometry || event.button !== 0 || mapPointers.size >= 2) return;
  if (gesture && (gesture.type !== "touch" || event.pointerType !== "touch")) return;
  const point = mapPoint(event, canvas);
  mapPointers.set(event.pointerId, point);
  canvas.setPointerCapture(event.pointerId);
  startGesture(event.pointerType, mapPointers.size === 2);
  hoverPoint = event.pointerType === "touch" ? null : point;
  inspectTile();
});
canvas.addEventListener("pointermove", event => {
  const point = mapPoint(event, canvas);
  if (mapPointers.has(event.pointerId) && mapGeometry) {
    mapPointers.set(event.pointerId, point);
    const spread = pointerSpread();
    const [x, y] = spread.point;
    if (gesture.moved || Math.hypot(x - gesture.point[0], y - gesture.point[1]) > 4) {
      gesture.moved = true;
      hoverPoint = null;
      moveMap(gesture.zoom * spread.distance / gesture.distance, spread.point, gesture.world);
      return;
    }
  }
  if (event.pointerType === "touch" || gesture && gesture.type !== event.pointerType) return;
  if (selectedTile !== null) {
    selectedTile = null;
    requestDraw();
  }
  hoverPoint = point;
  inspectTile();
});

function endGesture(event) {
  if (!mapPointers.delete(event.pointerId)) return;
  if (mapPointers.size && mapGeometry) {
    startGesture(gesture.type, true);
    return;
  }
  if (event.type === "pointerup" && event.pointerType === "touch" && !gesture.moved && mapGeometry) {
    const key = tileAt(...mapWorldPoint(mapPoint(event, canvas), mapCameraGeometry()));
    selectedTile = activeScene?.tiles.get(key)?.terrain ? key : null;
    requestDraw();
  }
  mapPointers.clear();
  gesture = null;
}
for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) canvas.addEventListener(type, endGesture);
function leaveInspector(event, keep) {
  if (event.pointerType !== "touch" && !keep.contains(event.relatedTarget)) {
    hoverPoint = null;
    inspectTile();
  }
}
canvas.addEventListener("pointerleave", event => leaveInspector(event, inspector));
inspector.addEventListener("pointerleave", event => leaveInspector(event, canvas));
minimap.addEventListener("click", event => {
  if (!minimapGeometry || !mapGeometry) return;
  const box = minimap.getBoundingClientRect();
  const [x, y] = mapPoint(event, minimap);
  centerMap(...mapWorldPoint([x * minimap.width / box.width, y * minimap.height / box.height], minimapGeometry));
});
minimap.addEventListener("keydown", event => {
  if (!mapGeometry) return;
  const offset = {ArrowLeft: [-2, 0], ArrowRight: [2, 0], ArrowUp: [0, -2], ArrowDown: [0, 2]}[event.key];
  if (!offset) return;
  event.preventDefault();
  centerMap(mapGeometry.cx + offset[0], mapGeometry.cy + offset[1]);
});
