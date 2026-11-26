let lastProgress = null;
const treeDialog = get("tree-dialog");

function setProgress(scene, progress) {
  scene.progress = progress ?? null;
  scene.cities = new Map((progress?.cities ?? []).map(city => [sightKey(city.position), city]));
}

function progressBar(value, cost, label) {
  const meter = document.createElement("progress");
  meter.max = cost;
  meter.value = Math.min(value, cost);
  meter.setAttribute("aria-label", label);
  return meter;
}

function researchSummary(research) {
  const summary = document.createDocumentFragment();
  const current = research.queue[0];
  summary.append(element("h2", current || "No research selected"));
  if (research.rate != null) summary.append(element("p", research.rate + " science per turn", "progress-meta"));
  const [stored, cost, turns] = research.progress[current] ?? [];
  if (stored != null) {
    summary.append(element("p", stored + (cost > 0 ? " / " + cost : "") + " science invested" +
      (turns ? " · " + turns + " turns at recorded rate" : "")));
    if (cost > 0) summary.append(progressBar(stored, cost, current + " research progress"));
  }
  if (research.queue.length > 1) summary.append(element("p", "Up next · " + research.queue.slice(1).join(" → ")));
  if (research.available?.length) summary.append(element("p", "Available · " + research.available.join(", "), "progress-meta"));
  summary.append(element("p", research.known.length + " technologies researched", "progress-meta"));
  return summary;
}

function cultureSummary(culture) {
  const summary = document.createDocumentFragment();
  const stored = culture.stored;
  summary.append(element("h2", stored + (culture.cost > 0 ? " / " + culture.cost : "") + " culture"));
  if (culture.cost > 0) summary.append(progressBar(stored, culture.cost, "Progress toward the next policy"));
  const rates = [culture.rate != null ? culture.rate + " culture per turn" : "",
    culture.turns != null ? culture.turns + " turns to next policy" : ""];
  if (rates.some(Boolean)) summary.append(element("p", rates.filter(Boolean).join(" · "), "progress-meta"));
  if (culture.free) summary.append(element("p", culture.free + " free " + (culture.free === 1 ? "policy" : "policies")));
  if (culture.available?.length) summary.append(element("p", "Available · " + culture.available.join(", "), "progress-meta"));
  const opened = (art.policyTree ?? []).filter(branch => culture.adopted.includes(branch.name));
  if (opened.length) summary.append(element("p", "Opened · " + opened.map(branch => branch.name).join(", ")));
  else if (culture.adopted.length) summary.append(element("p", culture.adopted.join(", ")));
  return summary;
}

function treeNode(name, status, requires, progress) {
  const node = element("div", "", "tree-node " + status[0]);
  const parts = [status[1], progress, requires?.length ? "Requires " + requires.join(", ") : ""].filter(Boolean);
  node.append(element("strong", name), element("span", parts.slice(0, progress ? 2 : 1).join(" · ")));
  node.title = parts.join(" · ");
  node.setAttribute("aria-label", name + " · " + parts.join(" · "));
  return node;
}

function researchTree(research) {
  const tree = art.researchTree ?? [];
  const scroll = element("div", "", "tree-scroll");
  const grid = element("div", "", "tech-grid");
  const width = 138, height = 56;
  const wires = document.createElement("canvas");
  wires.className = "tree-wires";
  wires.width = width * (1 + Math.max(...tree.map(tech => tech.column)));
  wires.height = height * (1 + Math.max(...tree.map(tech => tech.row)));
  wires.setAttribute("aria-hidden", "true");
  const context = wires.getContext("2d");
  context.strokeStyle = "#4c685e";
  context.lineWidth = 1.5;
  const byName = new Map(tree.map(tech => [tech.name, tech]));
  const known = new Set(research.known);
  const columns = new Map();
  const current = research.queue[0];
  for (const tech of tree) {
    const x = width * tech.column + 9, y = height * (tech.row + .5);
    for (const requirement of tech.requires) {
      const prior = byName.get(requirement);
      if (!prior) continue;
      const fromX = width * (prior.column + 1) - 9, fromY = height * (prior.row + .5);
      context.beginPath();
      context.moveTo(fromX, fromY);
      context.lineTo((fromX + x) / 2, fromY);
      context.lineTo((fromX + x) / 2, y);
      context.lineTo(x, y);
      context.stroke();
    }
    columns.set(tech.column, tech.era);
  }
  grid.append(wires);
  let previousEra;
  for (const [column, era] of [...columns].sort(([a], [b]) => a - b)) {
    if (era === previousEra) continue;
    const title = element("strong", era, "tree-era");
    title.style.gridColumn = column + 1;
    title.style.gridRow = 1;
    grid.append(title);
    previousEra = era;
  }
  for (const tech of tree) {
    const {name, requires, column, row} = tech;
    const available = research.available?.includes(name) ?? requires.every(required => known.has(required));
    const status = name === current ? ["current", "Researching"] : known.has(name) ? ["known", "Researched"] :
      research.queue.includes(name) ? ["queued", "Queued"] : available ? ["available", research.available ? "Available" : "Requirements met"] :
        ["", "Locked"];
    const [stored, cost] = research.progress[name] ?? [];
    const progress = stored != null && name === current ? stored + (cost > 0 ? "/" + cost : "") : "";
    const node = treeNode(name, status, requires, progress);
    node.style.gridColumn = column + 1;
    node.style.gridRow = row + 1;
    grid.append(node);
  }
  scroll.append(grid);
  return scroll;
}

function policyTree(culture) {
  const adopted = new Set(culture.adopted);
  const branches = element("div", "", "policy-branches");
  for (const branch of art.policyTree) {
    const section = element("section", "", "policy-branch");
    const tree = element("div", "", "policy-tree");
    const opened = adopted.has(branch.name);
    const complete = adopted.has(branch.name + " Complete");
    const branchStatus = complete ? "Complete" : opened ? "Opened" :
      culture.available?.includes(branch.name) ? "Available" : "Unopened";
    section.append(element("h3", branch.name + " · " + branchStatus), element("p", branch.era, "progress-meta"));
    for (const policy of branch.policies) {
      const completeNode = policy.name === branch.name + " Complete";
      const status = adopted.has(policy.name) ? ["known", completeNode ? "Complete" : "Adopted"] :
        culture.available?.includes(policy.name) ? ["available", "Available"] : ["", completeNode ? "Finisher" : "Unadopted"];
      const node = treeNode(policy.name, status, policy.requires);
      node.style.gridColumn = policy.column ? policy.column + " / span 2" : "1 / -1";
      if (policy.row) node.style.gridRow = policy.row;
      tree.append(node);
    }
    section.append(tree);
    branches.append(section);
  }
  return branches;
}

function showProgress(scene, kind) {
  if (lastProgress?.kind === kind && lastProgress.data === scene.progress) return;
  lastProgress = {kind, data: scene.progress};
  lastNotes = null;
  const display = get("notes");
  const state = scene.progress?.[kind];
  if (!state) {
    display.replaceChildren(element("p", kind === "research" ? "No research was recorded for this view." :
      "No social policies were recorded for this view.", "empty"));
    return;
  }
  const research = kind === "research";
  const summary = research ? researchSummary(state) : cultureSummary(state);
  const layout = research ? art.researchTree : art.policyTree;
  const block = element("article", "");
  block.append(summary);
  if (layout?.length) {
    const title = research ? "Research tree" : "Social policy tree";
    const open = element("button", "Open " + title.toLowerCase(), "tree-button");
    open.addEventListener("click", () => {
      pause();
      get("tree-title").textContent = title + " · " + civilization;
      get("tree-content").replaceChildren(research ? researchTree(state) : policyTree(state));
      treeDialog.showModal();
      if (research && state.queue.length) {
        const column = layout.find(tech => tech.name === state.queue[0])?.column;
        if (column != null) treeDialog.querySelector(".tree-scroll").scrollLeft = Math.max(0, (column - 2) * 138);
      }
    });
    block.append(open);
  }
  display.replaceChildren(block);
}

get("close-tree").addEventListener("click", () => treeDialog.close());
