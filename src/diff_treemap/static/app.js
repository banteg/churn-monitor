const config = window.DIFF_TREEMAP_CONFIG ?? {};
const state = {
  snapshotKey: null,
  stream: null,
};

const els = {
  changedFiles: document.getElementById("changed-files"),
  addedLines: document.getElementById("added-lines"),
  deletedLines: document.getElementById("deleted-lines"),
  netLines: document.getElementById("net-lines"),
  repoRoot: document.getElementById("repo-root"),
  headRef: document.getElementById("head-ref"),
  baseRef: document.getElementById("base-ref"),
  mergeBase: document.getElementById("merge-base"),
  generatedAt: document.getElementById("generated-at"),
  statusPill: document.getElementById("status-pill"),
  additions: document.getElementById("top-additions"),
  deletions: document.getElementById("top-deletions"),
  treemap: document.getElementById("treemap"),
};

function formatSigned(value) {
  return value > 0 ? `+${value}` : `${value}`;
}

function number(value) {
  return new Intl.NumberFormat().format(value);
}

function applySummary(snapshot) {
  els.changedFiles.textContent = number(snapshot.summary.changed_files);
  els.addedLines.textContent = number(snapshot.summary.added_lines);
  els.deletedLines.textContent = number(snapshot.summary.deleted_lines);
  els.netLines.textContent = formatSigned(snapshot.summary.net_lines);
  els.netLines.className =
    snapshot.summary.net_lines > 0
      ? "positive"
      : snapshot.summary.net_lines < 0
        ? "negative"
        : "neutral";

  els.repoRoot.textContent = snapshot.repo_root;
  els.headRef.textContent = snapshot.head_ref;
  els.baseRef.textContent = snapshot.base_ref;
  els.mergeBase.textContent = snapshot.merge_base.slice(0, 12);
  els.generatedAt.textContent = new Date(snapshot.generated_at).toLocaleTimeString();
}

function nodeColor(node) {
  if (node.is_binary && node.net_lines === 0) {
    return 0;
  }
  return node.net_lines;
}

function renderTreemap(snapshot) {
  const hasVisibleArea = snapshot.nodes.some((node) => node.kind === "file" && node.value > 0);
  if (!hasVisibleArea) {
    els.treemap.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No line changes relative to the selected base.";
    els.treemap.append(empty);
    return;
  }

  const ids = snapshot.nodes.map((node) => node.id);
  const labels = snapshot.nodes.map((node) => node.label);
  const parents = snapshot.nodes.map((node) => node.parent ?? "");
  const values = snapshot.nodes.map((node) => node.value);
  const colors = snapshot.nodes.map(nodeColor);
  const hoverText = snapshot.nodes.map((node) => {
    const previous = node.previous_path ? `<br>rename from: ${node.previous_path}` : "";
    const binary = node.is_binary ? "<br>binary diff" : "";
    return [
      `<b>${node.path}</b>`,
      `kind: ${node.kind}`,
      `churn: ${number(node.value)}`,
      `added: ${number(node.added_lines)}`,
      `deleted: ${number(node.deleted_lines)}`,
      `net: ${formatSigned(node.net_lines)}`,
      previous,
      binary,
    ].join("<br>");
  });

  const trace = {
    type: "treemap",
    ids,
    labels,
    parents,
    values,
    branchvalues: "total",
    textinfo: "label+value",
    customdata: hoverText,
    marker: {
      colors,
      colorscale: [
        [0.0, "#d96b4e"],
        [0.5, "#d7d0c4"],
        [1.0, "#1b8f82"],
      ],
      cmid: 0,
      line: { color: "rgba(255,250,243,0.92)", width: 1.5 },
    },
    hovertemplate: "%{customdata}<extra></extra>",
    tiling: { packing: "squarify", pad: 2 },
    pathbar: { visible: true, side: "top" },
  };

  const layout = {
    margin: { t: 10, r: 0, b: 0, l: 0 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: '"IBM Plex Sans", "Avenir Next", "Segoe UI", sans-serif', color: "#2d2416" },
    coloraxis: { cmid: 0 },
  };

  Plotly.react(els.treemap, [trace], layout, { responsive: true, displaylogo: false });
}

function renderChangeList(container, entries, direction) {
  container.replaceChildren();
  if (!entries.length) {
    const item = document.createElement("li");
    item.textContent = "No changes in this direction yet.";
    container.append(item);
    return;
  }

  for (const entry of entries) {
    const item = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = entry.path;

    const meta = document.createElement("div");
    meta.className = "change-meta";

    const net = document.createElement("span");
    net.className = direction === "positive" ? "positive" : "negative";
    net.textContent = `net ${formatSigned(entry.net_lines)}`;

    const churn = document.createElement("span");
    churn.textContent = `churn ${number(entry.value)}`;

    const delta = document.createElement("span");
    delta.textContent = `+${number(entry.added_lines)} / -${number(entry.deleted_lines)}`;

    meta.append(net, churn, delta);
    item.append(title, meta);
    container.append(item);
  }
}

function renderPanels(snapshot) {
  const leaves = snapshot.nodes.filter((node) => node.kind === "file");
  const additions = [...leaves]
    .filter((node) => node.net_lines > 0)
    .sort((left, right) => right.net_lines - left.net_lines || right.value - left.value)
    .slice(0, 6);
  const deletions = [...leaves]
    .filter((node) => node.net_lines < 0)
    .sort((left, right) => left.net_lines - right.net_lines || right.value - left.value)
    .slice(0, 6);

  renderChangeList(els.additions, additions, "positive");
  renderChangeList(els.deletions, deletions, "negative");
}

function setStatus(label, tone = "neutral") {
  els.statusPill.textContent = label;
  els.statusPill.className = `status-pill ${tone}`;
}

function renderError(detail) {
  state.snapshotKey = null;
  setStatus("Needs attention", "negative");
  els.treemap.replaceChildren();
  els.additions.replaceChildren();
  els.deletions.replaceChildren();

  const message = document.createElement("div");
  message.className = "empty-state";
  message.textContent = detail;
  els.treemap.append(message);
}

function applySnapshot(snapshot) {
  if (snapshot.snapshot_key === state.snapshotKey) {
    setStatus("Live", "neutral");
    return;
  }

  state.snapshotKey = snapshot.snapshot_key;
  applySummary(snapshot);
  renderTreemap(snapshot);
  renderPanels(snapshot);
  setStatus("Live", "positive");
}

function eventsUrl() {
  const params = new URLSearchParams();
  if (config.defaultBase) {
    params.set("base", config.defaultBase);
  }
  return params.size ? `/api/events?${params.toString()}` : "/api/events";
}

function connectStream() {
  if (state.stream) {
    state.stream.close();
  }

  setStatus("Connecting", "neutral");
  const stream = new EventSource(eventsUrl());
  state.stream = stream;

  stream.onopen = () => {
    if (state.stream === stream && state.snapshotKey === null) {
      setStatus("Syncing", "neutral");
    }
  };

  stream.onerror = () => {
    if (state.stream === stream) {
      setStatus("Reconnecting", "neutral");
    }
  };

  stream.addEventListener("snapshot", (event) => {
    if (state.stream !== stream) {
      return;
    }
    applySnapshot(JSON.parse(event.data));
  });

  stream.addEventListener("problem", (event) => {
    if (state.stream !== stream) {
      return;
    }
    const payload = JSON.parse(event.data);
    renderError(payload.detail || "Unable to load snapshot.");
  });
}

connectStream();
