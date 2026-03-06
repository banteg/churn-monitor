import { createQuery, useQueryClient } from "@tanstack/solid-query";
import {
  For,
  Show,
  createEffect,
  createMemo,
  createResource,
  createSignal,
  onCleanup,
  onMount,
} from "solid-js";

const STORAGE_KEY_TREEMAP_METRIC = "churn-monitor.metric";
const STORAGE_KEY_COMMIT_ORDER = "churn-monitor.commit-order";
const EMPTY_SUMMARY = {
  added_lines: 0,
  changed_files: 0,
  commit_count: 0,
  deleted_lines: 0,
  net_lines: 0,
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

function loadStoredValue(key, fallback, isValid) {
  try {
    const value = window.localStorage.getItem(key);
    return isValid(value) ? value : fallback;
  } catch {
    return fallback;
  }
}

function saveStoredValue(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Ignore localStorage failures in restricted contexts.
  }
}

function loadTreemapMetric() {
  return loadStoredValue(STORAGE_KEY_TREEMAP_METRIC, "churn", (value) => value === "net" || value === "churn");
}

function loadCommitOrder() {
  return loadStoredValue(
    STORAGE_KEY_COMMIT_ORDER,
    "reverse",
    (value) => value === "chrono" || value === "reverse",
  );
}

function number(value) {
  return new Intl.NumberFormat().format(value);
}

function formatSigned(value) {
  return value > 0 ? `+${value}` : `${value}`;
}

function formatRepoPath(path, homeDir) {
  if (!path) {
    return "-";
  }
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

function formatRelativeTime(timestampMs, nowMs) {
  if (timestampMs === null || Number.isNaN(timestampMs)) {
    return "-";
  }

  const elapsedSeconds = Math.max(0, Math.round((nowMs - timestampMs) / 1000));
  if (elapsedSeconds < 5) {
    return "just now";
  }
  if (elapsedSeconds < 60) {
    return `${elapsedSeconds}s ago`;
  }

  const elapsedMinutes = Math.round(elapsedSeconds / 60);
  if (elapsedMinutes < 60) {
    return `${elapsedMinutes}m ago`;
  }

  const elapsedHours = Math.round(elapsedMinutes / 60);
  if (elapsedHours < 24) {
    return `${elapsedHours}h ago`;
  }

  const elapsedDays = Math.round(elapsedHours / 24);
  return `${elapsedDays}d ago`;
}

function parseTimestamp(value) {
  if (!value) {
    return null;
  }
  const timestampMs = Date.parse(value);
  return Number.isFinite(timestampMs) ? timestampMs : null;
}

function targetActivityMs(target) {
  return parseTimestamp(target.last_activity_at) ?? -1;
}

function sortTargets(targets) {
  return [...targets].sort((left, right) => {
    const rightTime = targetActivityMs(right);
    const leftTime = targetActivityMs(left);
    if (rightTime !== leftTime) {
      return rightTime - leftTime;
    }
    return left.head_ref.localeCompare(right.head_ref);
  });
}

function mergeTarget(previous, incoming) {
  return {
    id: incoming.id ?? previous?.id ?? "",
    head_ref: incoming.head_ref ?? previous?.head_ref ?? "",
    worktree_path: incoming.worktree_path ?? previous?.worktree_path ?? null,
    last_activity_at: incoming.last_activity_at ?? previous?.last_activity_at ?? null,
    summary: incoming.summary ?? previous?.summary ?? null,
    is_current: incoming.is_current ?? previous?.is_current ?? false,
  };
}

function mergeTargetsPayload(previous, incoming) {
  const existingTargets = new Map((previous?.targets ?? []).map((target) => [target.id, target]));
  const nextTargets = incoming.reset ? new Map() : new Map(existingTargets);

  for (const target of incoming.targets ?? []) {
    nextTargets.set(target.id, mergeTarget(existingTargets.get(target.id), target));
  }

  return {
    selected_target_id: incoming.selected_target_id ?? previous?.selected_target_id ?? "",
    targets: [...nextTargets.values()],
  };
}

function branchPillStats(target) {
  const summary = target.summary;
  if (!summary) {
    return "loading...";
  }
  if (summary.changed_files === 0) {
    return "aligned with base";
  }
  return `${number(summary.changed_files)} files · +${number(summary.added_lines)} -${number(
    summary.deleted_lines,
  )}`;
}

function buildEventsUrl(baseRef) {
  const params = new URLSearchParams();
  if (baseRef) {
    params.set("base", baseRef);
  }
  return params.size ? `/api/events?${params.toString()}` : "/api/events";
}

async function fetchJson(path, params = {}) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value) {
      url.searchParams.set(key, value);
    }
  }

  const response = await window.fetch(url);
  if (!response.ok) {
    let detail = response.statusText || "Request failed.";
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      // Keep the fallback message.
    }
    throw new Error(detail);
  }
  return response.json();
}

function leafMetricValue(node, metric) {
  if (metric === "net") {
    return Math.abs(node.net_lines);
  }
  return node.value;
}

function treemapValues(snapshot, metric) {
  if (metric === "churn") {
    return snapshot.nodes.map((node) => node.value);
  }

  const totals = new Map();
  const childrenByParent = new Map();

  for (const node of snapshot.nodes) {
    const parentId = node.parent ?? "";
    if (!childrenByParent.has(parentId)) {
      childrenByParent.set(parentId, []);
    }
    childrenByParent.get(parentId).push(node);
  }

  function computeTotal(node) {
    if (totals.has(node.id)) {
      return totals.get(node.id);
    }

    const children = childrenByParent.get(node.id) ?? [];
    const total =
      children.length === 0
        ? leafMetricValue(node, metric)
        : children.reduce((sum, child) => sum + computeTotal(child), 0);

    totals.set(node.id, total);
    return total;
  }

  return snapshot.nodes.map((node) => computeTotal(node));
}

function nodeChurnWeight(nodeValue, maxValue) {
  if (nodeValue <= 0 || maxValue <= 0) {
    return 0;
  }
  return Math.log1p(nodeValue) / Math.log1p(maxValue);
}

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

function metricLabel(metric) {
  return metric === "net" ? "abs net" : "churn";
}

function hasTreemapArea(snapshot, metric) {
  const values = treemapValues(snapshot, metric);
  return snapshot.nodes.some((node, index) => node.kind === "file" && values[index] > 0);
}

function topChanges(nodes, direction) {
  const leaves = nodes.filter((node) => node.kind === "file");
  const filtered = leaves.filter((node) =>
    direction === "positive" ? node.net_lines > 0 : node.net_lines < 0,
  );

  return filtered
    .sort((left, right) => {
      if (direction === "positive") {
        return right.net_lines - left.net_lines || right.value - left.value;
      }
      return left.net_lines - right.net_lines || right.value - left.value;
    })
    .slice(0, 6);
}

function orderedCommits(commits, order) {
  return [...commits].sort((left, right) => {
    const leftValue = parseTimestamp(left.committed_at) ?? 0;
    const rightValue = parseTimestamp(right.committed_at) ?? 0;
    return order === "chrono" ? leftValue - rightValue : rightValue - leftValue;
  });
}

function statusView(problem, connectionState, isFetchingSnapshot, hasSnapshot, isSwitching) {
  if (problem) {
    return { label: "Needs attention", tone: "negative" };
  }
  if (isSwitching) {
    return { label: "Switching", tone: "neutral" };
  }
  if (!hasSnapshot) {
    if (connectionState === "error") {
      return { label: "Reconnecting", tone: "neutral" };
    }
    if (connectionState === "open" && isFetchingSnapshot) {
      return { label: "Syncing", tone: "neutral" };
    }
    return { label: "Connecting", tone: "neutral" };
  }
  if (isFetchingSnapshot) {
    return { label: "Syncing", tone: "neutral" };
  }
  if (connectionState === "error") {
    return { label: "Reconnecting", tone: "neutral" };
  }
  return { label: "Live", tone: "positive" };
}

function TreemapCanvas(props) {
  const [plotly] = createResource(async () => {
    const module = await import("plotly.js-dist-min");
    return module.default ?? module;
  });
  let element;

  createEffect(() => {
    const Plotly = plotly();
    const snapshot = props.snapshot;
    const metric = props.metric;
    if (!Plotly || !snapshot || !element) {
      return;
    }

    const values = treemapValues(snapshot, metric);
    const ids = snapshot.nodes.map((node) => node.id);
    const labels = snapshot.nodes.map((node) => node.label);
    const parents = snapshot.nodes.map((node) => node.parent ?? "");
    const maxChurn = snapshot.nodes.reduce((maximum, node) => Math.max(maximum, node.value), 0);
    const colors = snapshot.nodes.map((node) => nodeColorBucket(node, maxChurn));
    const sizeLabel = metricLabel(metric);
    const hoverText = snapshot.nodes.map((node, index) => {
      const previous = node.previous_path ? `<br>rename from: ${node.previous_path}` : "";
      const binary = node.is_binary ? "<br>binary diff" : "";
      return [
        `<b>${node.path}</b>`,
        `kind: ${node.kind}`,
        `size (${sizeLabel}): ${number(values[index])}`,
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

    Plotly.react(element, [trace], layout, { responsive: true, displaylogo: false });
  });

  onCleanup(() => {
    const Plotly = plotly();
    if (Plotly && element) {
      Plotly.purge(element);
    }
  });

  return (
    <Show
      when={plotly()}
      fallback={
        <div class="treemap">
          <div class="empty-state">Loading treemap…</div>
        </div>
      }
    >
      <div ref={element} class="treemap" />
    </Show>
  );
}

function ChangeList(props) {
  return (
    <ol class="change-list">
      <Show
        when={!props.message}
        fallback={<li>{props.message}</li>}
      >
        <Show when={props.entries.length} fallback={<li>No changes in this direction yet.</li>}>
          <For each={props.entries}>
            {(entry, index) => (
              <li>
                <div class="change-row">
                  <span class="entry-index neutral">{index() + 1}.</span>
                  <div class="entry-body">
                    <span class="entry-title">{entry.path}</span>
                    <div class="change-meta">
                      <span class={props.direction === "positive" ? "positive" : "negative"}>
                        net {formatSigned(entry.net_lines)}
                      </span>
                      <span class="delta-pair">
                        <span class="positive">+{number(entry.added_lines)}</span>
                        <span class="negative">-{number(entry.deleted_lines)}</span>
                      </span>
                    </div>
                  </div>
                </div>
              </li>
            )}
          </For>
        </Show>
      </Show>
    </ol>
  );
}

function CommitList(props) {
  return (
    <ol class="commit-list">
      <Show when={!props.message} fallback={<li>{props.message}</li>}>
        <Show when={props.commits.length} fallback={<li>No branch commits yet.</li>}>
          <For each={props.commits}>
            {(commit, index) => {
              const timestampMs = parseTimestamp(commit.committed_at);
              const label =
                props.order === "chrono" ? index() + 1 : props.commits.length - index();

              return (
                <li>
                  <div class="commit-row">
                    <span class="entry-index neutral">{label}.</span>
                    <span class="entry-title">{commit.subject}</span>
                    <span class="entry-time neutral">
                      {timestampMs === null ? "-" : formatRelativeTime(timestampMs, props.nowMs)}
                    </span>
                  </div>
                </li>
              );
            }}
          </For>
        </Show>
      </Show>
    </ol>
  );
}

export default function App() {
  const queryClient = useQueryClient();
  const [treemapMetric, setTreemapMetric] = createSignal(loadTreemapMetric());
  const [commitOrder, setCommitOrder] = createSignal(loadCommitOrder());
  const [selectedTargetId, setSelectedTargetId] = createSignal("");
  const [connectionState, setConnectionState] = createSignal("connecting");
  const [problemMessage, setProblemMessage] = createSignal("");
  const [nowMs, setNowMs] = createSignal(Date.now());
  const [hasSeenSnapshot, setHasSeenSnapshot] = createSignal(false);

  const configQuery = createQuery(() => ({
    queryKey: ["config"],
    queryFn: () => fetchJson("/api/config"),
  }));

  const targetsQuery = createQuery(() => ({
    queryKey: ["targets"],
    enabled: Boolean(configQuery.data),
    queryFn: () => fetchJson("/api/targets"),
  }));

  const snapshotQuery = createQuery(() => {
    const config = configQuery.data;
    const targetId = selectedTargetId();
    return {
      queryKey: ["snapshot", config?.defaultBase ?? "", targetId],
      enabled: Boolean(config && targetId),
      queryFn: () => fetchJson("/api/snapshot", { base: config.defaultBase, target: targetId }),
    };
  });

  onMount(() => {
    const timerId = window.setInterval(() => {
      setNowMs(Date.now());
    }, 1000);
    onCleanup(() => {
      window.clearInterval(timerId);
    });
  });

  createEffect(() => {
    const payload = targetsQuery.data;
    if (!payload) {
      return;
    }

    const availableTargetIds = new Set(payload.targets.map((target) => target.id));
    const currentTargetId = selectedTargetId();
    if (!currentTargetId || !availableTargetIds.has(currentTargetId)) {
      setSelectedTargetId(payload.selected_target_id);
    }
  });

  createEffect(() => {
    const config = configQuery.data;
    if (!config) {
      return;
    }

    setConnectionState("connecting");
    const stream = new EventSource(buildEventsUrl(config.defaultBase));

    stream.onopen = () => {
      setConnectionState("open");
    };

    stream.onerror = () => {
      setConnectionState("error");
    };

    stream.addEventListener("targets", (event) => {
      const payload = JSON.parse(event.data);
      queryClient.setQueryData(["targets"], (previous) => mergeTargetsPayload(previous, payload));
    });

    stream.addEventListener("invalidate", () => {
      const targetId = selectedTargetId();
      if (!targetId) {
        return;
      }
      queryClient.invalidateQueries({
        queryKey: ["snapshot", config.defaultBase ?? "", targetId],
      });
    });

    stream.addEventListener("problem", (event) => {
      const payload = JSON.parse(event.data);
      setProblemMessage(payload.detail || "Unable to load monitor data.");
      setConnectionState("error");
    });

    onCleanup(() => {
      stream.close();
    });
  });

  createEffect(() => {
    const snapshot = snapshotQuery.data;
    if (snapshot && snapshot.target_id === selectedTargetId()) {
      setHasSeenSnapshot(true);
      setProblemMessage("");
    }
  });

  const sortedTargets = createMemo(() => sortTargets(targetsQuery.data?.targets ?? []));

  const selectedTarget = createMemo(() => {
    const targetId = selectedTargetId();
    return sortedTargets().find((target) => target.id === targetId) ?? null;
  });

  const currentSnapshot = createMemo(() => {
    const snapshot = snapshotQuery.data;
    return snapshot && snapshot.target_id === selectedTargetId() ? snapshot : null;
  });

  const currentSummary = createMemo(() => currentSnapshot()?.summary ?? EMPTY_SUMMARY);
  const topAdditions = createMemo(() => topChanges(currentSnapshot()?.nodes ?? [], "positive"));
  const topCuts = createMemo(() => topChanges(currentSnapshot()?.nodes ?? [], "negative"));
  const commits = createMemo(() => orderedCommits(currentSnapshot()?.commits ?? [], commitOrder()));

  const snapshotError = createMemo(() => snapshotQuery.error?.message || "");
  const targetsError = createMemo(() => targetsQuery.error?.message || "");
  const configError = createMemo(() => configQuery.error?.message || "");
  const activeProblem = createMemo(
    () => problemMessage() || snapshotError() || targetsError() || configError(),
  );

  const isSwitching = createMemo(
    () => Boolean(selectedTargetId()) && !currentSnapshot() && snapshotQuery.isFetching && hasSeenSnapshot(),
  );

  const status = createMemo(() =>
    statusView(
      activeProblem(),
      connectionState(),
      snapshotQuery.isFetching,
      Boolean(currentSnapshot()),
      isSwitching(),
    ),
  );

  const repoRootLabel = createMemo(() => {
    const homeDir = configQuery.data?.homeDir ?? "";
    return formatRepoPath(
      currentSnapshot()?.repo_root ?? selectedTarget()?.worktree_path ?? configQuery.data?.repoRoot ?? "",
      homeDir,
    );
  });

  const baseRefLabel = createMemo(() => currentSnapshot()?.base_ref ?? configQuery.data?.defaultBase ?? "-");
  const headRefLabel = createMemo(() => currentSnapshot()?.head_ref ?? selectedTarget()?.head_ref ?? "-");
  const lastEditLabel = createMemo(() => {
    const timestampMs = parseTimestamp(currentSnapshot()?.last_edit_at);
    return timestampMs === null ? "-" : `last edit ${formatRelativeTime(timestampMs, nowMs())}`;
  });

  const treemapMessage = createMemo(() => {
    const snapshot = currentSnapshot();
    if (activeProblem()) {
      return activeProblem();
    }
    if (!snapshot) {
      return selectedTargetId() ? "Loading branch snapshot…" : "Loading monitor…";
    }
    if (!hasTreemapArea(snapshot, treemapMetric())) {
      return treemapMetric() === "net"
        ? "No net line changes relative to the selected base."
        : "No line changes relative to the selected base.";
    }
    return "";
  });

  const panelMessage = createMemo(() => {
    if (activeProblem()) {
      return activeProblem();
    }
    if (!currentSnapshot()) {
      return selectedTargetId() ? "Loading branch snapshot…" : "Loading monitor…";
    }
    return "";
  });

  function updateTreemapMetric(metric) {
    if (metric !== "churn" && metric !== "net") {
      return;
    }
    setTreemapMetric(metric);
    saveStoredValue(STORAGE_KEY_TREEMAP_METRIC, metric);
  }

  function updateCommitOrder(order) {
    if (order !== "chrono" && order !== "reverse") {
      return;
    }
    setCommitOrder(order);
    saveStoredValue(STORAGE_KEY_COMMIT_ORDER, order);
  }

  function selectTarget(targetId) {
    if (!targetId || targetId === selectedTargetId()) {
      return;
    }
    setProblemMessage("");
    setSelectedTargetId(targetId);
  }

  return (
    <div class="shell">
      <section class="branch-bar" aria-label="Recently active branches">
        <div class="target-pills">
          <Show
            when={sortedTargets().length}
            fallback={<div class="empty-state">Loading branches…</div>}
          >
            <For each={sortedTargets()}>
              {(target) => {
                const activityMs = parseTimestamp(target.last_activity_at);
                const homeDir = configQuery.data?.homeDir ?? "";

                return (
                  <button
                    type="button"
                    class="branch-pill"
                    classList={{ active: target.id === selectedTargetId() }}
                    aria-pressed={target.id === selectedTargetId() ? "true" : "false"}
                    title={
                      target.worktree_path ? formatRepoPath(target.worktree_path, homeDir) : undefined
                    }
                    onClick={() => selectTarget(target.id)}
                  >
                    <span class="branch-pill-name">{target.head_ref}</span>
                    <div class="branch-pill-meta">
                      <span class="branch-pill-time">
                        {activityMs === null
                          ? "no recent activity"
                          : `updated ${formatRelativeTime(activityMs, nowMs())}`}
                      </span>
                      <span class="branch-pill-stats">{branchPillStats(target)}</span>
                    </div>
                  </button>
                );
              }}
            </For>
          </Show>
        </div>
      </section>

      <section class="topbar repo-strip">
        <div class="info-item repo-item">
          <span class="label">Repo</span>
          <strong>{repoRootLabel()}</strong>
        </div>
        <div class="info-item">
          <span class="label">Branch</span>
          <strong>{headRefLabel()}</strong>
        </div>
        <div class="info-item">
          <span class="label">Base</span>
          <strong>{baseRefLabel()}</strong>
        </div>
        <div class="status-group">
          <span class="last-edit">{lastEditLabel()}</span>
          <div class={`status-pill ${status().tone}`}>{status().label}</div>
        </div>
      </section>

      <header class="topbar summary-bar">
        <section class="summary-strip" aria-label="Diff summary">
          <div class="summary-item neutral">
            <span>Changed files</span>
            <strong>{number(currentSummary().changed_files)}</strong>
          </div>
          <div class="summary-item neutral">
            <span>Commits</span>
            <strong>{number(currentSummary().commit_count)}</strong>
          </div>
          <div class="summary-item positive">
            <span>Added</span>
            <strong>{number(currentSummary().added_lines)}</strong>
          </div>
          <div class="summary-item negative">
            <span>Deleted</span>
            <strong>{number(currentSummary().deleted_lines)}</strong>
          </div>
          <div
            class="summary-item"
            classList={{
              positive: currentSummary().net_lines > 0,
              negative: currentSummary().net_lines < 0,
              neutral: currentSummary().net_lines === 0,
            }}
          >
            <span>Net</span>
            <strong>{formatSigned(currentSummary().net_lines)}</strong>
          </div>
          <div class="summary-item neutral">
            <span>Churn</span>
            <strong>{number(currentSummary().added_lines + currentSummary().deleted_lines)}</strong>
          </div>
        </section>
      </header>

      <section class="content-grid">
        <section class="main-stack">
          <article class="panel treemap-panel">
            <div class="panel-header">
              <div class="metric-toggle" role="group" aria-label="Size metric">
                <button
                  type="button"
                  class="metric-button"
                  classList={{ active: treemapMetric() === "churn" }}
                  aria-pressed={treemapMetric() === "churn" ? "true" : "false"}
                  onClick={() => updateTreemapMetric("churn")}
                >
                  Churn
                </button>
                <button
                  type="button"
                  class="metric-button"
                  classList={{ active: treemapMetric() === "net" }}
                  aria-pressed={treemapMetric() === "net" ? "true" : "false"}
                  onClick={() => updateTreemapMetric("net")}
                >
                  Net
                </button>
              </div>
            </div>
            <Show
              when={currentSnapshot() && !treemapMessage()}
              fallback={
                <div class="treemap">
                  <div class="empty-state">{treemapMessage()}</div>
                </div>
              }
            >
              <TreemapCanvas snapshot={currentSnapshot()} metric={treemapMetric()} />
            </Show>
          </article>

          <section class="change-panels">
            <article class="panel">
              <div class="panel-header">
                <p class="panel-title">Top Additions</p>
              </div>
              <ChangeList entries={topAdditions()} direction="positive" message={panelMessage()} />
            </article>

            <article class="panel">
              <div class="panel-header">
                <p class="panel-title">Top Cuts</p>
              </div>
              <ChangeList entries={topCuts()} direction="negative" message={panelMessage()} />
            </article>
          </section>
        </section>

        <aside class="side-stack">
          <article class="panel">
            <div class="panel-header">
              <p class="panel-title">Commits</p>
              <div class="metric-toggle" role="group" aria-label="Commit order">
                <button
                  type="button"
                  class="metric-button"
                  classList={{ active: commitOrder() === "reverse" }}
                  aria-pressed={commitOrder() === "reverse" ? "true" : "false"}
                  onClick={() => updateCommitOrder("reverse")}
                >
                  Reverse
                </button>
                <button
                  type="button"
                  class="metric-button"
                  classList={{ active: commitOrder() === "chrono" }}
                  aria-pressed={commitOrder() === "chrono" ? "true" : "false"}
                  onClick={() => updateCommitOrder("chrono")}
                >
                  Chrono
                </button>
              </div>
            </div>
            <CommitList
              commits={commits()}
              order={commitOrder()}
              nowMs={nowMs()}
              message={panelMessage()}
            />
          </article>
        </aside>
      </section>
    </div>
  );
}
