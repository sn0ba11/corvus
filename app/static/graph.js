"use strict";

(() => {
  const configElement = document.querySelector("#graph-config");
  const svg = document.querySelector("#investigation-graph");
  if (!configElement || !svg) return;

  const config = JSON.parse(configElement.textContent || "{}");
  const viewport = document.querySelector("#graph-viewport");
  const edgeLayer = document.querySelector("#graph-edges");
  const edgeLabelLayer = document.querySelector("#graph-edge-labels");
  const nodeLayer = document.querySelector("#graph-nodes");
  const inspector = document.querySelector("#graph-inspector");
  const loading = document.querySelector("#graph-loading");
  const visibleCount = document.querySelector("#graph-visible-count");
  const saveState = document.querySelector("#graph-save-state");
  const searchInput = document.querySelector("#graph-search");
  const layoutSelect = document.querySelector("#graph-layout");
  const applyLayoutButton = document.querySelector("#graph-apply-layout");
  const fitButton = document.querySelector("#graph-fit");
  const oneHopButton = document.querySelector("#graph-one-hop");
  const excludeSelectedButton = document.querySelector("#graph-exclude-selected");
  const showAllButton = document.querySelector("#graph-show-all");
  const restoreExcludedButton = document.querySelector("#graph-restore-excluded");
  const saveButton = document.querySelector("#graph-save");
  const resetButton = document.querySelector("#graph-reset");
  const exportPngButton = document.querySelector("#graph-export-png");
  const exportJsonButton = document.querySelector("#graph-export-json");
  const showAllFindings = document.querySelector("#graph-show-all-findings");
  const showCandidates = document.querySelector("#graph-show-candidates");
  const showContradicted = document.querySelector("#graph-show-contradicted");
  const showCollectors = document.querySelector("#graph-show-collectors");
  const showImages = document.querySelector("#graph-show-images");
  const showLocations = document.querySelector("#graph-show-locations");
  const showArchives = document.querySelector("#graph-show-archives");
  const showEvents = document.querySelector("#graph-show-events");

  const SVG_NS = "http://www.w3.org/2000/svg";
  const PLATFORM_ICONS = new Map([
    ["snapchat", "👻"], ["instagram", "◎"], ["facebook", "f"], ["x", "𝕏"],
    ["twitter", "𝕏"], ["linkedin", "in"], ["github", "⌘"], ["reddit", "◉"],
    ["tiktok", "♪"], ["youtube", "▶"], ["telegram", "✈"], ["discord", "◈"],
    ["pinterest", "P"], ["twitch", "▣"], ["duolingo", "🦉"], ["mastodon", "M"],
    ["strava", "▲"], ["spotify", "●"], ["steam", "⚙"], ["medium", "M"],
  ]);
  const platformIcon = (value) => {
    const normalised = String(value || "").trim().toLowerCase().replace(/^www\./, "");
    if (!normalised) return "";
    for (const [name, icon] of PLATFORM_ICONS) {
      if (normalised === name || normalised.includes(name)) return icon;
    }
    const letters = normalised.replace(/[^a-z0-9]/g, "").slice(0, 2).toUpperCase();
    return letters || "•";
  };
  const positions = new Map();
  const nodeById = new Map();
  const edgeById = new Map();
  const neighbours = new Map();
  const excludedNodeIds = new Set();
  let graph = null;
  let selectedId = null;
  let selectedKind = null;
  let focusNodeId = null;
  let dirty = false;
  let transform = { x: 0, y: 0, scale: 1 };
  let pointerState = null;

  const createSvg = (name, attributes = {}) => {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => {
      if (value !== null && value !== undefined) element.setAttribute(key, String(value));
    });
    return element;
  };

  const truncate = (value, limit) => {
    const text = String(value || "");
    return text.length > limit ? `${text.slice(0, Math.max(1, limit - 1))}…` : text;
  };

  const safeFilename = (suffix) => `${String(config.filename || "investigation-graph").replace(/[^a-zA-Z0-9_.-]+/g, "-")}.${suffix}`;

  const downloadBlob = (blob, filename) => {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const nodeDimensions = (node) => {
    const type = node.data.type;
    if (type === "subject") return { width: 92, height: 92 };
    if (type === "image") return { width: 76, height: 76 };
    if (type === "location") return { width: 112, height: 82 };
    if (type === "collector") return { width: 126, height: 48 };
    if (["username", "email", "domain", "identifier"].includes(type)) return { width: 154, height: 54 };
    if (type === "archive") return { width: 150, height: 54 };
    if (type === "event") return { width: 176, height: 62 };
    return { width: 174, height: 66 };
  };

  const setStatus = (message, isError = false) => {
    if (!saveState) return;
    saveState.textContent = message;
    saveState.classList.toggle("error-text", isError);
  };

  const markDirty = () => {
    dirty = true;
    setStatus("Layout changed · not saved");
  };

  const graphPoint = (event) => {
    const rect = svg.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left - transform.x) / transform.scale,
      y: (event.clientY - rect.top - transform.y) / transform.scale,
    };
  };

  const applyTransform = () => {
    viewport.setAttribute("transform", `translate(${transform.x} ${transform.y}) scale(${transform.scale})`);
  };

  const stableSort = (items) => [...items].sort((left, right) => {
    const labelResult = String(left.data.label || "").localeCompare(String(right.data.label || ""));
    return labelResult || String(left.data.id).localeCompare(String(right.data.id));
  });

  const placeRing = (items, radius, startAngle = -Math.PI / 2) => {
    const sorted = stableSort(items);
    const count = Math.max(sorted.length, 1);
    sorted.forEach((node, index) => {
      const angle = startAngle + (Math.PI * 2 * index) / count;
      positions.set(node.data.id, {
        x: Math.cos(angle) * radius,
        y: Math.sin(angle) * radius,
      });
    });
  };

  const radialLayout = () => {
    const categories = {
      centre: [],
      identifiers: [],
      findings: [],
      evidence: [],
      collectors: [],
    };
    graph.nodes.forEach((node) => {
      const type = node.data.type;
      if (type === "subject" && node.data.id === "subject") categories.centre.push(node);
      else if (["subject", "username", "email", "identifier"].includes(type)) categories.identifiers.push(node);
      else if (["profile", "finding", "document"].includes(type)) categories.findings.push(node);
      else if (type === "collector") categories.collectors.push(node);
      else categories.evidence.push(node);
    });
    categories.centre.forEach((node) => positions.set(node.data.id, { x: 0, y: 0 }));
    placeRing(categories.identifiers, 190);
    placeRing(categories.findings, Math.max(350, 55 * categories.findings.length / Math.PI), -Math.PI / 2.2);
    placeRing(categories.evidence, Math.max(560, 52 * categories.evidence.length / Math.PI), -Math.PI / 2.5);
    placeRing(categories.collectors, Math.max(760, 48 * categories.collectors.length / Math.PI));
  };

  const groupedLayout = () => {
    const groups = new Map();
    graph.nodes.forEach((node) => {
      const type = node.data.type;
      if (!groups.has(type)) groups.set(type, []);
      groups.get(type).push(node);
    });
    const order = ["subject", "username", "email", "identifier", "profile", "finding", "event", "image", "domain", "document", "location", "archive", "collector"];
    const types = [...groups.keys()].sort((left, right) => {
      const leftIndex = order.indexOf(left);
      const rightIndex = order.indexOf(right);
      return (leftIndex === -1 ? 99 : leftIndex) - (rightIndex === -1 ? 99 : rightIndex) || left.localeCompare(right);
    });
    types.forEach((type, columnIndex) => {
      stableSort(groups.get(type)).forEach((node, rowIndex) => {
        positions.set(node.data.id, {
          x: (columnIndex - (types.length - 1) / 2) * 230,
          y: (rowIndex - (groups.get(type).length - 1) / 2) * 112,
        });
      });
    });
  };

  const provenanceLayout = () => {
    const layerFor = (node) => {
      const type = node.data.type;
      if (type === "collector") return 0;
      if (["subject", "username", "email", "identifier"].includes(type)) return 1;
      if (["profile", "finding", "document"].includes(type)) return 2;
      return 3;
    };
    const layers = [[], [], [], []];
    graph.nodes.forEach((node) => layers[layerFor(node)].push(node));
    layers.forEach((items, layerIndex) => {
      stableSort(items).forEach((node, rowIndex) => {
        positions.set(node.data.id, {
          x: (layerIndex - 1.5) * 420,
          y: (rowIndex - (items.length - 1) / 2) * 106,
        });
      });
    });
  };

  const chronologicalLayout = () => {
    const events = graph.nodes
      .filter((node) => node.data.type === "event" && node.data.occurred_at)
      .sort((left, right) => Date.parse(left.data.occurred_at) - Date.parse(right.data.occurred_at));
    const eventX = new Map();
    events.forEach((node, index) => {
      const x = (index - (events.length - 1) / 2) * 205;
      eventX.set(node.data.id, x);
      positions.set(node.data.id, { x, y: 0 });
    });
    const linkedPositions = new Map();
    graph.edges.forEach((edge) => {
      if (edge.data.relation !== "account_event") return;
      const x = eventX.get(edge.data.target);
      if (x === undefined) return;
      if (!linkedPositions.has(edge.data.source)) linkedPositions.set(edge.data.source, []);
      linkedPositions.get(edge.data.source).push(x);
    });
    linkedPositions.forEach((values, nodeId) => {
      positions.set(nodeId, { x: values.reduce((sum, value) => sum + value, 0) / values.length, y: -145 });
    });
    const remaining = stableSort(graph.nodes.filter((node) => !positions.has(node.data.id)));
    remaining.forEach((node, index) => {
      positions.set(node.data.id, {
        x: (index % 10 - 4.5) * 210,
        y: 210 + Math.floor(index / 10) * 100,
      });
    });
  };

  const applyLayout = (name, preserveSaved = false) => {
    if (!preserveSaved) positions.clear();
    if (name === "grouped") groupedLayout();
    else if (name === "provenance") provenanceLayout();
    else if (name === "chronological") {
      showEvents.checked = true;
      chronologicalLayout();
    } else radialLayout();
    render();
    fitGraph();
    if (!preserveSaved) markDirty();
  };

  const nodePassesFilters = (node) => {
    const data = node.data;
    if (excludedNodeIds.has(data.id)) return false;
    if (data.type === "collector") return showCollectors.checked;
    if (data.hidden_by_default && !showAllFindings.checked) return false;
    if (data.type === "image" && !showImages.checked) return false;
    if (data.type === "location" && !showLocations.checked) return false;
    if (data.type === "archive" && !showArchives.checked) return false;
    if (data.type === "event" && !showEvents.checked) return false;
    if (
      data.state === "candidate"
      && !showCandidates.checked
      && !showAllFindings.checked
      && data.type !== "location"
      && !data.prominent
    ) return false;
    if (data.state === "contradicted" && !showContradicted.checked) return false;
    if (focusNodeId) {
      const allowed = neighbours.get(focusNodeId) || new Set();
      return data.id === focusNodeId || allowed.has(data.id);
    }
    return true;
  };

  const edgePassesFilters = (edge, visibleNodes) => {
    const data = edge.data;
    if (!visibleNodes.has(data.source) || !visibleNodes.has(data.target)) return false;
    const target = nodeById.get(data.target);
    if (
      data.state === "candidate"
      && !showCandidates.checked
      && !showAllFindings.checked
      && target?.data.type !== "location"
      && !target?.data.prominent
    ) return false;
    if ((data.state === "rejected" || data.state === "contradicted") && !showContradicted.checked) return false;
    return true;
  };

  const visibleElements = () => {
    let nodes = graph.nodes.filter(nodePassesFilters);
    let ids = new Set(nodes.map((node) => node.data.id));
    let edges = graph.edges.filter((edge) => edgePassesFilters(edge, ids));
    const connected = new Set(["subject"]);
    edges.forEach((edge) => {
      connected.add(edge.data.source);
      connected.add(edge.data.target);
    });
    nodes = nodes.filter((node) => connected.has(node.data.id));
    ids = new Set(nodes.map((node) => node.data.id));
    edges = edges.filter((edge) => ids.has(edge.data.source) && ids.has(edge.data.target));
    return { nodes, edges, ids };
  };

  const pathForLocation = (width, height) => `M 0 ${-height / 2} L ${width / 2} 0 L 0 ${height / 2} L ${-width / 2} 0 Z`;

  const appendNodeShape = (group, node) => {
    const dimensions = nodeDimensions(node);
    const type = node.data.type;
    let shape;
    if (type === "subject") {
      shape = createSvg("circle", { r: dimensions.width / 2 });
    } else if (type === "image") {
      shape = createSvg("circle", { r: dimensions.width / 2 });
    } else if (type === "location") {
      shape = createSvg("path", { d: pathForLocation(dimensions.width, dimensions.height) });
    } else {
      shape = createSvg("rect", {
        x: -dimensions.width / 2,
        y: -dimensions.height / 2,
        width: dimensions.width,
        height: dimensions.height,
        rx: type === "collector" ? 24 : 12,
      });
    }
    shape.classList.add("graph-node-shape");
    group.append(shape);

    if (node.data.avatar_url && ["profile", "image"].includes(type)) {
      const image = createSvg("image", {
        href: node.data.avatar_url,
        x: type === "profile" ? -78 : -30,
        y: type === "profile" ? -22 : -30,
        width: type === "profile" ? 44 : 60,
        height: type === "profile" ? 44 : 60,
        preserveAspectRatio: "xMidYMid slice",
      });
      image.classList.add("graph-node-avatar");
      if (type === "image") image.setAttribute("clip-path", "url(#graph-avatar-clip)");
      group.append(image);
    }

    const label = createSvg("text", {
      class: "graph-node-label",
      "text-anchor": "middle",
      x: type === "profile" && node.data.avatar_url ? 18 : 0,
      y: node.data.subtitle ? -3 : 4,
    });
    const first = createSvg("tspan", { x: label.getAttribute("x"), dy: 0 });
    first.textContent = truncate(node.data.label, type === "subject" ? 22 : 25);
    label.append(first);
    if (node.data.subtitle) {
      const second = createSvg("tspan", { x: label.getAttribute("x"), dy: 17, class: "graph-node-subtitle" });
      const icon = node.data.type === "profile" ? platformIcon(node.data.platform || node.data.subtitle) : "";
      second.textContent = truncate(`${icon ? `${icon} ` : ""}${node.data.subtitle}`, 28);
      label.append(second);
    }
    group.append(label);

    const stateText = {
      confirmed: "Confirmed",
      candidate: "Candidate",
      contradicted: "Contradicted",
      rejected: "Rejected",
      observed: "Observed",
    }[node.data.state] || node.data.state;
    const hoverHelp = createSvg("title");
    hoverHelp.textContent = `${node.data.label}${node.data.subtitle ? ` · ${node.data.subtitle}` : ""}\n${stateText}. Click for provenance; drag to reposition.`;
    group.prepend(hoverHelp);
    group.setAttribute("aria-label", `${node.data.type}: ${node.data.label}. ${stateText}.`);
  };

  const searchMatches = (node, query) => {
    if (!query) return true;
    const fields = [node.data.label, node.data.subtitle, node.data.type, node.data.state];
    (node.data.details || []).forEach((detail) => fields.push(detail.label, detail.value));
    return fields.join(" ").toLowerCase().includes(query);
  };

  const render = () => {
    if (!graph) return;
    const visible = visibleElements();
    edgeLayer.replaceChildren();
    edgeLabelLayer.replaceChildren();
    nodeLayer.replaceChildren();
    const query = String(searchInput.value || "").trim().toLowerCase();
    const matchingNodeIds = new Set(visible.nodes.filter((node) => searchMatches(node, query)).map((node) => node.data.id));

    visible.edges.forEach((edge) => {
      const source = positions.get(edge.data.source);
      const target = positions.get(edge.data.target);
      if (!source || !target) return;
      const line = createSvg("line", {
        x1: source.x,
        y1: source.y,
        x2: target.x,
        y2: target.y,
        class: `graph-edge state-${edge.data.state}`,
        "data-edge-id": edge.data.id,
        "marker-end": edge.data.state === "candidate" ? "url(#graph-arrow-candidate)" : "url(#graph-arrow)",
      });
      if (selectedKind === "edge" && selectedId === edge.data.id) line.classList.add("selected");
      if (query && !matchingNodeIds.has(edge.data.source) && !matchingNodeIds.has(edge.data.target)) line.classList.add("search-dimmed");
      const edgeHelp = createSvg("title");
      edgeHelp.textContent = `${edge.data.label} · ${edge.data.state}. Click to inspect relationship provenance.`;
      line.append(edgeHelp);
      edgeLayer.append(line);

      const text = createSvg("text", {
        x: (source.x + target.x) / 2,
        y: (source.y + target.y) / 2 - 6,
        class: "graph-edge-label",
        "text-anchor": "middle",
        "data-edge-id": edge.data.id,
      });
      text.textContent = truncate(edge.data.label, 32);
      const labelHelp = createSvg("title");
      labelHelp.textContent = `${edge.data.label} · click to inspect relationship provenance.`;
      text.append(labelHelp);
      if (visible.edges.length > 65 && selectedId !== edge.data.id) text.classList.add("dense-hidden");
      edgeLabelLayer.append(text);
    });

    visible.nodes.forEach((node) => {
      const position = positions.get(node.data.id);
      if (!position) return;
      const group = createSvg("g", {
        class: `graph-node type-${node.data.type} state-${node.data.state}`,
        transform: `translate(${position.x} ${position.y})`,
        "data-node-id": node.data.id,
        tabindex: 0,
        role: "button",
      });
      if (selectedKind === "node" && selectedId === node.data.id) group.classList.add("selected");
      if (query && !matchingNodeIds.has(node.data.id)) group.classList.add("search-dimmed");
      appendNodeShape(group, node);
      nodeLayer.append(group);
    });

    const hiddenCount = Math.max(0, graph.nodes.length - visible.nodes.length);
    const excludedCount = excludedNodeIds.size;
    visibleCount.textContent = `${visible.nodes.length} visible nodes · ${visible.edges.length} relationships${hiddenCount ? ` · ${hiddenCount} hidden` : ""}${excludedCount ? ` · ${excludedCount} excluded` : ""}`;
    oneHopButton.disabled = selectedKind !== "node";
    excludeSelectedButton.disabled = selectedKind !== "node" || selectedId === "subject";
    showAllButton.disabled = !focusNodeId;
    restoreExcludedButton.disabled = excludedNodeIds.size === 0;
    applyTransform();
  };

  const graphBounds = () => {
    const visible = visibleElements().nodes;
    if (!visible.length) return null;
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    visible.forEach((node) => {
      const position = positions.get(node.data.id);
      if (!position) return;
      const dimensions = nodeDimensions(node);
      minX = Math.min(minX, position.x - dimensions.width / 2);
      maxX = Math.max(maxX, position.x + dimensions.width / 2);
      minY = Math.min(minY, position.y - dimensions.height / 2);
      maxY = Math.max(maxY, position.y + dimensions.height / 2);
    });
    return { minX, maxX, minY, maxY };
  };

  const fitGraph = () => {
    const bounds = graphBounds();
    const rect = svg.getBoundingClientRect();
    if (!bounds || !rect.width || !rect.height) return;
    const padding = 70;
    const width = Math.max(1, bounds.maxX - bounds.minX);
    const height = Math.max(1, bounds.maxY - bounds.minY);
    const scale = Math.min(1.35, Math.max(0.12, Math.min((rect.width - padding * 2) / width, (rect.height - padding * 2) / height)));
    transform = {
      scale,
      x: rect.width / 2 - ((bounds.minX + bounds.maxX) / 2) * scale,
      y: rect.height / 2 - ((bounds.minY + bounds.maxY) / 2) * scale,
    };
    applyTransform();
  };

  const addInspectorDetail = (container, label, value) => {
    if (value === null || value === undefined || value === "") return;
    const row = document.createElement("div");
    row.className = "graph-inspector-row";
    const term = document.createElement("strong");
    term.textContent = label;
    const description = document.createElement("span");
    description.textContent = String(value);
    row.append(term, description);
    container.append(row);
  };

  const renderInspectorLinks = (links) => {
    if (!links || !links.length) return;
    const section = document.createElement("div");
    section.className = "graph-inspector-actions";
    links.forEach((item) => {
      const link = document.createElement("a");
      link.className = `${item.kind === "external" ? "button" : "button primary"} graph-help`;
      link.href = item.url;
      link.textContent = item.label;
      link.title = item.kind === "external"
        ? `Open the public source in a separate tab: ${item.label}`
        : `Open the linked local evidence view: ${item.label}`;
      link.dataset.help = link.title;
      if (item.kind === "external") {
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      }
      section.append(link);
    });
    inspector.append(section);
  };

  const inspectNode = (nodeId) => {
    const node = nodeById.get(nodeId);
    if (!node) return;
    selectedId = nodeId;
    selectedKind = "node";
    inspector.replaceChildren();
    const eyebrow = document.createElement("p");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = node.data.type.replaceAll("_", " ").toUpperCase();
    const heading = document.createElement("h2");
    const icon = node.data.type === "profile" ? platformIcon(node.data.platform || node.data.subtitle) : "";
    heading.textContent = `${icon ? `${icon} ` : ""}${node.data.label}`;
    const subtitle = document.createElement("p");
    subtitle.textContent = node.data.subtitle || "Evidence entity";
    const state = document.createElement("span");
    state.className = `badge graph-state-${node.data.state}`;
    state.textContent = node.data.state.replaceAll("_", " ");
    inspector.append(eyebrow, heading, subtitle, state);
    const details = document.createElement("div");
    details.className = "graph-inspector-details";
    addInspectorDetail(details, "Entity ID", node.data.id);
    if (node.data.confidence !== null && node.data.confidence !== undefined) addInspectorDetail(details, "Confidence", `${Math.round(node.data.confidence * 100)}%`);
    (node.data.details || []).forEach((item) => addInspectorDetail(details, item.label, item.value));
    inspector.append(details);
    renderInspectorLinks(node.data.links || []);
    const visibilityActions = document.createElement("div");
    visibilityActions.className = "graph-inspector-actions graph-visibility-actions";
    const includeButton = document.createElement("button");
    includeButton.type = "button";
    includeButton.className = "button primary graph-help";
    includeButton.textContent = "Include this entity only";
    includeButton.title = "Temporarily isolate this entity and its directly connected evidence.";
    includeButton.dataset.help = includeButton.title;
    includeButton.addEventListener("click", includeSelectedOnly);
    visibilityActions.append(includeButton);
    if (nodeId !== "subject") {
      const excludeButton = document.createElement("button");
      excludeButton.type = "button";
      excludeButton.className = "button graph-help";
      excludeButton.textContent = "Exclude from graph";
      excludeButton.title = "Hide this entity from the current graph session without deleting or rejecting its evidence.";
      excludeButton.dataset.help = excludeButton.title;
      excludeButton.addEventListener("click", excludeSelected);
      visibilityActions.append(excludeButton);
    }
    inspector.append(visibilityActions);
    const hint = document.createElement("small");
    hint.textContent = "Visibility changes affect this graph view only. Drag the node to refine the layout; save the layout to retain positions.";
    inspector.append(hint);
    render();
  };

  const inspectEdge = (edgeId) => {
    const edge = edgeById.get(edgeId);
    if (!edge) return;
    selectedId = edgeId;
    selectedKind = "edge";
    const source = nodeById.get(edge.data.source);
    const target = nodeById.get(edge.data.target);
    inspector.replaceChildren();
    const eyebrow = document.createElement("p");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = "EVIDENCE RELATIONSHIP";
    const heading = document.createElement("h2");
    heading.textContent = edge.data.label;
    const description = document.createElement("p");
    description.textContent = `${source?.data.label || edge.data.source} → ${target?.data.label || edge.data.target}`;
    const state = document.createElement("span");
    state.className = `badge graph-state-${edge.data.state}`;
    state.textContent = edge.data.state.replaceAll("_", " ");
    inspector.append(eyebrow, heading, description, state);
    const details = document.createElement("div");
    details.className = "graph-inspector-details";
    addInspectorDetail(details, "Relationship", edge.data.relation.replaceAll("_", " "));
    addInspectorDetail(details, "Supporting sources", edge.data.support_count);
    if (edge.data.confidence !== null && edge.data.confidence !== undefined) addInspectorDetail(details, "Confidence", `${Math.round(edge.data.confidence * 100)}%`);
    (edge.data.details || []).forEach((item) => addInspectorDetail(details, item.label, item.value));
    inspector.append(details);
    const warning = document.createElement("small");
    warning.textContent = edge.data.state === "candidate"
      ? "This is a candidate relationship and must not be treated as an identity assertion without supporting evidence."
      : "This relationship is derived from retained case evidence. Inspect the connected entities for source-level provenance.";
    inspector.append(warning);
    render();
  };

  const saveLayout = async () => {
    const payload = {};
    positions.forEach((position, nodeId) => {
      payload[nodeId] = { x: Math.round(position.x * 100) / 100, y: Math.round(position.y * 100) / 100 };
    });
    saveButton.disabled = true;
    setStatus("Saving layout…");
    try {
      const response = await fetch(config.layout_url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csrf: config.csrf, positions: payload }),
      });
      if (!response.ok) throw new Error(`save failed (${response.status})`);
      const result = await response.json();
      dirty = false;
      setStatus(`Saved ${result.saved_position_count} node positions`);
    } catch (error) {
      setStatus(error.message || "Layout could not be saved", true);
    } finally {
      saveButton.disabled = false;
    }
  };

  const resetLayout = async () => {
    resetButton.disabled = true;
    setStatus("Resetting layout…");
    try {
      const response = await fetch(config.layout_url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csrf: config.csrf, action: "reset" }),
      });
      if (!response.ok) throw new Error(`reset failed (${response.status})`);
      positions.clear();
      radialLayout();
      dirty = false;
      render();
      fitGraph();
      setStatus("Saved layout cleared; subject-centred layout restored");
    } catch (error) {
      setStatus(error.message || "Layout could not be reset", true);
    } finally {
      resetButton.disabled = false;
    }
  };

  const exportGraphJson = () => {
    const output = JSON.parse(JSON.stringify(graph));
    output.layout = { positions: Object.fromEntries(positions), exported_at: new Date().toISOString() };
    downloadBlob(new Blob([JSON.stringify(output, null, 2)], { type: "application/json" }), safeFilename("json"));
  };

  const exportGraphPng = () => {
    const rect = svg.getBoundingClientRect();
    const clone = svg.cloneNode(true);
    clone.setAttribute("xmlns", SVG_NS);
    clone.setAttribute("width", String(Math.max(1, Math.round(rect.width))));
    clone.setAttribute("height", String(Math.max(1, Math.round(rect.height))));
    clone.querySelectorAll("image").forEach((image) => image.remove());
    const computed = getComputedStyle(document.documentElement);
    const style = createSvg("style");
    const background = computed.getPropertyValue("--panel").trim() || "#ffffff";
    const text = computed.getPropertyValue("--text").trim() || "#172018";
    const muted = computed.getPropertyValue("--muted").trim() || "#5d665e";
    const accent = computed.getPropertyValue("--accent").trim() || "#5e7458";
    const border = computed.getPropertyValue("--line").trim() || "#cfd8cf";
    style.textContent = `
      .graph-node-shape{fill:${background};stroke:${border};stroke-width:2}
      .type-subject .graph-node-shape{fill:${accent};stroke:${accent}}
      .type-subject text{fill:#fff}.graph-node-label{fill:${text};font:600 13px sans-serif}
      .graph-node-subtitle{fill:${muted};font:400 10px sans-serif}.graph-edge{stroke:${muted};stroke-width:1.7;opacity:.72}
      .graph-edge.state-candidate{stroke-dasharray:7 5}.graph-edge.state-rejected,.graph-edge.state-contradicted{stroke-dasharray:2 6}
      .graph-edge-label{fill:${muted};font:10px sans-serif}.search-dimmed{opacity:.14}
    `;
    clone.insertBefore(style, clone.firstChild);
    const serialised = new XMLSerializer().serializeToString(clone);
    const image = new Image();
    const url = URL.createObjectURL(new Blob([serialised], { type: "image/svg+xml;charset=utf-8" }));
    image.onload = () => {
      const scale = Math.min(2, window.devicePixelRatio || 1);
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(rect.width * scale));
      canvas.height = Math.max(1, Math.round(rect.height * scale));
      const context = canvas.getContext("2d");
      context.scale(scale, scale);
      context.fillStyle = background;
      context.fillRect(0, 0, rect.width, rect.height);
      context.drawImage(image, 0, 0, rect.width, rect.height);
      canvas.toBlob((blob) => {
        if (blob) downloadBlob(blob, safeFilename("png"));
        URL.revokeObjectURL(url);
      }, "image/png");
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      setStatus("PNG export failed", true);
    };
    image.src = url;
  };

  const rebuildNeighbours = () => {
    neighbours.clear();
    graph.nodes.forEach((node) => neighbours.set(node.data.id, new Set()));
    graph.edges.forEach((edge) => {
      neighbours.get(edge.data.source)?.add(edge.data.target);
      neighbours.get(edge.data.target)?.add(edge.data.source);
    });
  };

  const loadGraph = async () => {
    try {
      const response = await fetch(config.data_url, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`graph request failed (${response.status})`);
      graph = await response.json();
      graph.nodes.forEach((node) => {
        nodeById.set(node.data.id, node);
        if (node.position) positions.set(node.data.id, { x: Number(node.position.x), y: Number(node.position.y) });
      });
      graph.edges.forEach((edge) => edgeById.set(edge.data.id, edge));
      rebuildNeighbours();
      if (positions.size !== graph.nodes.length) {
        const saved = new Map(positions);
        radialLayout();
        saved.forEach((position, nodeId) => positions.set(nodeId, position));
      }
      loading.hidden = true;
      render();
      window.requestAnimationFrame(fitGraph);
      setStatus(positions.size ? `${graph.summary.saved_position_count} saved positions loaded · overview filters active` : "Overview mode · subject-centred layout");
    } catch (error) {
      loading.textContent = error.message || "The investigation graph could not be loaded.";
      loading.classList.add("error-text");
    }
  };

  svg.addEventListener("pointerdown", (event) => {
    const nodeElement = event.target.closest?.("[data-node-id]");
    const point = graphPoint(event);
    if (nodeElement) {
      const nodeId = nodeElement.dataset.nodeId;
      const position = positions.get(nodeId);
      if (!position) return;
      pointerState = { type: "node", id: nodeId, offsetX: point.x - position.x, offsetY: point.y - position.y, moved: false };
      nodeElement.classList.add("dragging");
    } else {
      pointerState = { type: "pan", startX: event.clientX, startY: event.clientY, originX: transform.x, originY: transform.y, moved: false };
      svg.classList.add("panning");
    }
    svg.setPointerCapture(event.pointerId);
  });

  svg.addEventListener("pointermove", (event) => {
    if (!pointerState) return;
    if (pointerState.type === "node") {
      const point = graphPoint(event);
      positions.set(pointerState.id, { x: point.x - pointerState.offsetX, y: point.y - pointerState.offsetY });
      pointerState.moved = true;
      render();
    } else {
      transform.x = pointerState.originX + event.clientX - pointerState.startX;
      transform.y = pointerState.originY + event.clientY - pointerState.startY;
      pointerState.moved = true;
      applyTransform();
    }
  });

  const finishPointer = (event) => {
    if (!pointerState) return;
    const wasNode = pointerState.type === "node";
    const nodeId = wasNode ? pointerState.id : null;
    const moved = pointerState.moved;
    svg.querySelector(".graph-node.dragging")?.classList.remove("dragging");
    svg.classList.remove("panning");
    pointerState = null;
    if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
    if (wasNode && moved) markDirty();
    if (wasNode && !moved && nodeId) inspectNode(nodeId);
  };
  svg.addEventListener("pointerup", finishPointer);
  svg.addEventListener("pointercancel", finishPointer);

  svg.addEventListener("click", (event) => {
    if (pointerState) return;
    const edgeElement = event.target.closest?.("[data-edge-id]");
    if (edgeElement && !event.target.closest?.("[data-node-id]")) inspectEdge(edgeElement.dataset.edgeId);
  });

  svg.addEventListener("wheel", (event) => {
    event.preventDefault();
    const rect = svg.getBoundingClientRect();
    const cursorX = event.clientX - rect.left;
    const cursorY = event.clientY - rect.top;
    const oldScale = transform.scale;
    const factor = event.deltaY < 0 ? 1.12 : 0.89;
    const newScale = Math.min(3.5, Math.max(0.08, oldScale * factor));
    const graphX = (cursorX - transform.x) / oldScale;
    const graphY = (cursorY - transform.y) / oldScale;
    transform.scale = newScale;
    transform.x = cursorX - graphX * newScale;
    transform.y = cursorY - graphY * newScale;
    applyTransform();
  }, { passive: false });

  svg.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && event.target.dataset?.nodeId) {
      event.preventDefault();
      inspectNode(event.target.dataset.nodeId);
      return;
    }
    if (["+", "="].includes(event.key)) {
      transform.scale = Math.min(3.5, transform.scale * 1.15);
      applyTransform();
    } else if (event.key === "-") {
      transform.scale = Math.max(0.08, transform.scale / 1.15);
      applyTransform();
    } else if (selectedKind === "node" && ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) {
      event.preventDefault();
      const position = positions.get(selectedId);
      if (!position) return;
      const step = event.shiftKey ? 40 : 12;
      if (event.key === "ArrowUp") position.y -= step;
      if (event.key === "ArrowDown") position.y += step;
      if (event.key === "ArrowLeft") position.x -= step;
      if (event.key === "ArrowRight") position.x += step;
      markDirty();
      render();
    }
  });

  const includeSelectedOnly = () => {
    if (selectedKind !== "node" || !selectedId) return;
    focusNodeId = selectedId;
    render();
    fitGraph();
    setStatus("Include-only view active · evidence is unchanged");
  };

  const excludeSelected = () => {
    if (selectedKind !== "node" || !selectedId || selectedId === "subject") return;
    const excludedId = selectedId;
    excludedNodeIds.add(excludedId);
    if (focusNodeId === excludedId) focusNodeId = null;
    selectedId = null;
    selectedKind = null;
    inspector.replaceChildren();
    const eyebrow = document.createElement("p");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = "GRAPH VISIBILITY";
    const heading = document.createElement("h2");
    heading.textContent = "Entity excluded";
    const copy = document.createElement("p");
    copy.textContent = "The entity is hidden from this graph session only. Its finding, provenance and report evidence remain unchanged.";
    inspector.append(eyebrow, heading, copy);
    render();
    fitGraph();
    setStatus(`${excludedNodeIds.size} entit${excludedNodeIds.size === 1 ? "y" : "ies"} excluded from this view`);
  };

  const restoreExcluded = () => {
    excludedNodeIds.clear();
    render();
    fitGraph();
    setStatus("All manually excluded entities restored");
  };

  const rerenderAndFit = () => {
    render();
    fitGraph();
  };
  [showAllFindings, showCandidates, showContradicted, showCollectors, showImages, showLocations, showArchives, showEvents].forEach((control) => control.addEventListener("change", rerenderAndFit));
  searchInput.addEventListener("input", render);
  applyLayoutButton.addEventListener("click", () => applyLayout(layoutSelect.value));
  fitButton.addEventListener("click", fitGraph);
  oneHopButton.addEventListener("click", includeSelectedOnly);
  excludeSelectedButton.addEventListener("click", excludeSelected);
  showAllButton.addEventListener("click", () => {
    focusNodeId = null;
    render();
    fitGraph();
    setStatus("Include-only view cleared");
  });
  restoreExcludedButton.addEventListener("click", restoreExcluded);
  saveButton.addEventListener("click", saveLayout);
  resetButton.addEventListener("click", resetLayout);
  exportJsonButton.addEventListener("click", exportGraphJson);
  exportPngButton.addEventListener("click", exportGraphPng);
  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  loadGraph();
})();
