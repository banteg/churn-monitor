const config = window.DIFF_TREEMAP_CONFIG ?? {};
const state = {
  lastEditMs: null,
  snapshotKey: null,
  relativeTimer: null,
  stream: null,
};

const els = {
  changedFiles: document.getElementById("changed-files"),
  commitCount: document.getElementById("commit-count"),
  addedLines: document.getElementById("added-lines"),
  deletedLines: document.getElementById("deleted-lines"),
  netLines: document.getElementById("net-lines"),
  repoRoot: document.getElementById("repo-root"),
  headRef: document.getElementById("head-ref"),
  baseRef: document.getElementById("base-ref"),
  lastEdit: document.getElementById("last-edit"),
  statusPill: document.getElementById("status-pill"),
  commits: document.getElementById("commit-list"),
  additions: document.getElementById("top-additions"),
  deletions: document.getElementById("top-deletions"),
  treemap: document.getElementById("treemap"),
};

const TREEMAP_COLORSCALE = [
  [0.0, "#b65438"],
  [1 / 6, "#b65438"],
  [1 / 6, "#d88970"],
  [2 / 6, "#d88970"],
  [2 / 6, "#eadfd2"],
  [0.5, "#eadfd2"],
  [0.5, "#dcebe6"],
  [4 / 6, "#dcebe6"],
  [4 / 6, "#5da997"],
  [5 / 6, "#5da997"],
  [5 / 6, "#1b8f82"],
  [1.0, "#1b8f82"],
];

function formatSigned(value) {
  return value > 0 ? `+${value}` : `${value}`;
}

function number(value) {
  return new Intl.NumberFormat().format(value);
}

function formatRepoPath(path) {
  const homeDir = config.homeDir || "";
  if (!homeDir) {
    return path;
  }
  if (path === homeDir) {
    return "~";
  }
  if (path.startsWith(`${homeDir}/`)) {
    return `~${path.slice(homeDir.length)}`;
  }
  return path;
}

function formatRelativeTime(timestampMs) {
  if (timestampMs === null) {
    return "-";
  }

  const elapsedSeconds = Math.max(0, Math.round((Date.now() - timestampMs) / 1000));
  if (elapsedSeconds < 5) {
    return "last edit just now";
  }
  if (elapsedSeconds < 60) {
    return `last edit ${elapsedSeconds}s ago`;
  }

  const elapsedMinutes = Math.round(elapsedSeconds / 60);
  if (elapsedMinutes < 60) {
    return `last edit ${elapsedMinutes}m ago`;
  }

  const elapsedHours = Math.round(elapsedMinutes / 60);
  if (elapsedHours < 24) {
    return `last edit ${elapsedHours}h ago`;
  }

  const elapsedDays = Math.round(elapsedHours / 24);
  return `last edit ${elapsedDays}d ago`;
}

function renderLastEdit() {
  els.lastEdit.textContent = formatRelativeTime(state.lastEditMs);
}

function ensureRelativeTimer() {
  if (state.relativeTimer !== null) {
    return;
  }

  state.relativeTimer = window.setInterval(renderLastEdit, 1000);
}

function applySummary(snapshot) {
  els.changedFiles.textContent = number(snapshot.summary.changed_files);
  els.commitCount.textContent = number(snapshot.summary.commit_count);
  els.addedLines.textContent = number(snapshot.summary.added_lines);
  els.deletedLines.textContent = number(snapshot.summary.deleted_lines);
  els.netLines.textContent = formatSigned(snapshot.summary.net_lines);
  els.netLines.className =
    snapshot.summary.net_lines > 0
      ? "positive"
      : snapshot.summary.net_lines < 0
        ? "negative"
        : "neutral";

  els.repoRoot.textContent = formatRepoPath(snapshot.repo_root);
  els.headRef.textContent = snapshot.head_ref;
  els.baseRef.textContent = snapshot.base_ref;
  state.lastEditMs = snapshot.last_edit_at ? Date.parse(snapshot.last_edit_at) : null;
  renderLastEdit();
  ensureRelativeTimer();
}

function nodeChurnWeight(nodeValue, maxValue) {
  if (nodeValue <= 0 || maxValue <= 0) {
    return 0;
  }
  return Math.log1p(nodeValue) / Math.log1p(maxValue);
}

// High-churn mixed files keep a directional tint instead of collapsing to neutral.
function nodeColorBucket(node, maxValue) {
  if (node.is_binary && node.net_lines === 0) {
    return 0;
  }
  if (node.value <= 0 || node.net_lines === 0) {
    return 0;
  }

  const directionalShare = Math.abs(node.net_lines) / Math.max(1, node.value);
  const churnFloor = nodeChurnWeight(node.value, maxValue) * 0.4;
  const strength = Math.max(directionalShare, churnFloor);
  const bucket = strength >= 0.7 ? 3 : strength >= 0.35 ? 2 : 1;
  return Math.sign(node.net_lines) * bucket;
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
  const maxValue = snapshot.nodes.reduce((maximum, node) => Math.max(maximum, node.value), 0);
  const colors = snapshot.nodes.map((node) => nodeColorBucket(node, maxValue));
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
      colorscale: TREEMAP_COLORSCALE,
      cmin: -3,
      cmax: 3,
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

    const delta = document.createElement("span");
    delta.className = "delta-pair";

    const added = document.createElement("span");
    added.className = "positive";
    added.textContent = `+${number(entry.added_lines)}`;

    const deleted = document.createElement("span");
    deleted.className = "negative";
    deleted.textContent = `-${number(entry.deleted_lines)}`;

    delta.append(added, deleted);
    meta.append(net, delta);
    item.append(title, meta);
    container.append(item);
  }
}

function renderCommits(commits) {
  els.commits.replaceChildren();
  if (!commits.length) {
    const item = document.createElement("li");
    item.textContent = "No branch commits yet.";
    els.commits.append(item);
    return;
  }

  for (const commit of commits) {
    const item = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = commit.subject;

    const meta = document.createElement("div");
    meta.className = "change-meta";

    const sha = document.createElement("span");
    sha.className = "neutral";
    sha.textContent = commit.short_sha;

    meta.append(sha);
    item.append(title, meta);
    els.commits.append(item);
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
  els.commits.replaceChildren();
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
  renderCommits(snapshot.commits);
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
