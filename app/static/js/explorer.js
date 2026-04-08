/**
 * Estonian Legal Ontology — D3.js Force-Directed Graph Explorer
 *
 * Lazy-loading graph backed by /api/explorer/ endpoints.
 * Follows the category overview → drill-down → entity detail pattern.
 */

/* global d3 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CATEGORY_COLORS = {
  'EnactedLaw':       '#38bdf8',
  'DraftLegislation': '#a78bfa',
  'CourtDecision':    '#fb923c',
  'EULegislation':    '#34d399',
  'EUCourtDecision':  '#f472b6',
};

// Human-readable labels for categories (Estonian)
const CATEGORY_LABELS = {
  'EnactedLaw':       'Kehtiv seadus',
  'DraftLegislation': 'Eeln\u00f5u',
  'CourtDecision':    'Kohtulahend',
  'EULegislation':    'EL \u00f5igusakt',
  'EUCourtDecision':  'EL kohtulahend',
};

// English labels used in legend (matching demo)
const CATEGORY_LABELS_EN = {
  'EnactedLaw':       'Enacted Law',
  'DraftLegislation': 'Draft Legislation',
  'CourtDecision':    'Court Decisions',
  'EULegislation':    'EU Legislation',
  'EUCourtDecision':  'EU Court Decisions',
};

const CATEGORY_POSITIONS = {
  'EnactedLaw':       { x: -200, y: -150 },
  'DraftLegislation': { x:  200, y: -150 },
  'CourtDecision':    { x: -200, y:  150 },
  'EULegislation':    { x:  200, y:  150 },
  'EUCourtDecision':  { x:    0, y:  250 },
};

const MAX_NODES = 500;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  view: 'overview',           // 'overview' | 'category' | 'entity'
  expandedCategory: null,     // currently expanded category name
  nodes: [],
  links: [],
  pinnedNodes: new Set(),
  showEdgeLabels: true,
  selectedEntity: null,       // URI of selected entity for detail panel
  timelineActive: false,      // whether the timeline filter is applied
  timelineYear: 2026,         // currently selected year on the timeline slider
  selectedEntityData: null,   // full entity detail data (metadata, outgoing, incoming)
};

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------

const tooltip = document.getElementById('tooltip');
const detailPanel = document.getElementById('detail-panel');
const loadingOverlay = document.getElementById('loading-overlay');
const breadcrumb = document.getElementById('breadcrumb');

// ---------------------------------------------------------------------------
// SVG setup
// ---------------------------------------------------------------------------

let width = window.innerWidth;
let height = window.innerHeight;

const svg = d3.select('#canvas')
  .attr('width', width)
  .attr('height', height);

const defs = svg.append('defs');

// Glow filters per category
Object.entries(CATEGORY_COLORS).forEach(([cat, color]) => {
  const filter = defs.append('filter').attr('id', `glow-${cat}`);
  filter.append('feGaussianBlur').attr('stdDeviation', '4').attr('result', 'blur');
  filter.append('feFlood').attr('flood-color', color).attr('flood-opacity', '0.4').attr('result', 'color');
  filter.append('feComposite').attr('in', 'color').attr('in2', 'blur').attr('operator', 'in').attr('result', 'shadow');
  const merge = filter.append('feMerge');
  merge.append('feMergeNode').attr('in', 'shadow');
  merge.append('feMergeNode').attr('in', 'SourceGraphic');
});

// Arrow markers per category
Object.entries(CATEGORY_COLORS).forEach(([cat, color]) => {
  defs.append('marker')
    .attr('id', `arrow-${cat}`)
    .attr('viewBox', '0 -4 8 8')
    .attr('refX', 8).attr('refY', 0)
    .attr('markerWidth', 6).attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M0,-3L8,0L0,3')
    .attr('fill', color)
    .attr('opacity', 0.5);
});

// Cross-category arrow
defs.append('marker')
  .attr('id', 'arrow-cross')
  .attr('viewBox', '0 -4 8 8')
  .attr('refX', 8).attr('refY', 0)
  .attr('markerWidth', 6).attr('markerHeight', 6)
  .attr('orient', 'auto')
  .append('path')
  .attr('d', 'M0,-3L8,0L0,3')
  .attr('fill', '#fbbf24')
  .attr('opacity', 0.5);

const g = svg.append('g');

// Layers (order matters for z-index)
const linkLayer = g.append('g').attr('class', 'links');
const edgeLabelLayer = g.append('g').attr('class', 'edge-labels');
const nodeLayer = g.append('g').attr('class', 'nodes');

// Zoom
const zoomBehavior = d3.zoom()
  .scaleExtent([0.2, 4])
  .on('zoom', (event) => g.attr('transform', event.transform));

svg.call(zoomBehavior);
svg.call(zoomBehavior.transform, d3.zoomIdentity.translate(width / 2, height / 2).scale(0.9));

// Force simulation (created once, updated with data)
const simulation = d3.forceSimulation()
  .force('link', d3.forceLink().id(d => d.id).distance(100).strength(0.4))
  .force('charge', d3.forceManyBody().strength(-600))
  .force('center', d3.forceCenter(0, 0))
  .force('collision', d3.forceCollide().radius(d => (d.r || 20) + 10))
  .on('tick', ticked);

simulation.stop();

// D3 selections (updated by render)
let linkSel = linkLayer.selectAll('line');
let edgeLabelSel = edgeLabelLayer.selectAll('text');
let nodeSel = nodeLayer.selectAll('g.node');

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

function showLoading() {
  loadingOverlay.classList.add('visible');
}

function hideLoading() {
  loadingOverlay.classList.remove('visible');
}

async function apiFetch(url) {
  showLoading();
  try {
    const resp = await fetch(url);
    if (!resp.ok) {
      console.error(`API error: ${resp.status} ${resp.statusText} for ${url}`);
      return null;
    }
    return await resp.json();
  } catch (err) {
    console.error('API fetch failed:', err);
    return null;
  } finally {
    hideLoading();
  }
}

// ---------------------------------------------------------------------------
// Category color resolution
// ---------------------------------------------------------------------------

function categoryFromUri(typeUri) {
  if (!typeUri) return 'EnactedLaw';
  const short = typeUri.includes('#') ? typeUri.split('#').pop() : typeUri.split('/').pop();
  // Try direct match
  if (CATEGORY_COLORS[short]) return short;
  // Fuzzy match
  const lower = short.toLowerCase();
  if (lower.includes('enacted') || lower.includes('provision') || lower.includes('legalprovision')) return 'EnactedLaw';
  if (lower.includes('draft')) return 'DraftLegislation';
  if (lower.includes('eucourt') || lower.includes('eu_court')) return 'EUCourtDecision';
  if (lower.includes('court') || lower.includes('decision')) return 'CourtDecision';
  if (lower.includes('eu') || lower.includes('directive') || lower.includes('regulation')) return 'EULegislation';
  return 'EnactedLaw';
}

function colorFor(category) {
  return CATEGORY_COLORS[category] || '#94a3b8';
}

// ---------------------------------------------------------------------------
// Data loading strategies
// ---------------------------------------------------------------------------

async function loadOverview() {
  const json = await apiFetch('/api/explorer/overview');
  if (!json || !json.data) {
    // Fallback: show default 5 category nodes even if API unavailable
    return buildFallbackOverview();
  }

  const categories = json.data;
  // If the API returned category data from SPARQL, build overview nodes
  if (Array.isArray(categories) && categories.length > 0) {
    return buildOverviewFromApi(categories);
  }
  return buildFallbackOverview();
}

function buildOverviewFromApi(categories) {
  const nodes = [];
  const links = [];
  const catMap = {};

  categories.forEach(cat => {
    const catKey = categoryFromUri(cat.uri || cat.name);
    if (catMap[catKey]) {
      // Merge counts for same category
      catMap[catKey].count += cat.count;
      return;
    }
    const node = {
      id: `cat:${catKey}`,
      label: CATEGORY_LABELS_EN[catKey] || catKey,
      category: catKey,
      desc: `${cat.count.toLocaleString('et-EE')} \u00fcksust`,
      count: cat.count,
      r: Math.max(20, Math.min(40, 15 + Math.log10(cat.count + 1) * 8)),
      isCategory: true,
      uri: cat.uri,
    };
    catMap[catKey] = node;
    nodes.push(node);
  });

  // Add cross-category links between all pairs (visual)
  const catKeys = Object.keys(catMap);
  for (let i = 0; i < catKeys.length; i++) {
    for (let j = i + 1; j < catKeys.length; j++) {
      links.push({
        source: `cat:${catKeys[i]}`,
        target: `cat:${catKeys[j]}`,
        label: '',
        isCross: true,
      });
    }
  }

  return { nodes, links };
}

function buildFallbackOverview() {
  const fallbackData = [
    { key: 'EnactedLaw', count: 615, desc: 'Kehtivad seadused ja nende s\u00e4tted' },
    { key: 'DraftLegislation', count: 22832, desc: 'Seaduseeln\u00f5ud ja nende menetlusk\u00e4ik' },
    { key: 'CourtDecision', count: 12137, desc: 'Riigikohtu lahendid' },
    { key: 'EULegislation', count: 33242, desc: 'Euroopa Liidu \u00f5igusaktid' },
    { key: 'EUCourtDecision', count: 22290, desc: 'EL Kohtu lahendid' },
  ];

  const nodes = fallbackData.map(d => ({
    id: `cat:${d.key}`,
    label: CATEGORY_LABELS_EN[d.key],
    category: d.key,
    desc: d.desc,
    count: d.count,
    r: Math.max(20, Math.min(40, 15 + Math.log10(d.count + 1) * 8)),
    isCategory: true,
    uri: null,
  }));

  const links = [];
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      links.push({
        source: nodes[i].id,
        target: nodes[j].id,
        label: '',
        isCross: true,
      });
    }
  }

  return { nodes, links };
}

async function loadCategory(categoryKey) {
  // The API expects the URI-encoded type URI. For overview nodes that came
  // from the API, we stored the URI. For fallback nodes, we need to construct
  // a reasonable URI.
  const catNode = state.nodes.find(n => n.id === `cat:${categoryKey}`);
  const uri = catNode && catNode.uri
    ? encodeURIComponent(catNode.uri)
    : encodeURIComponent(categoryKey);

  const json = await apiFetch(`/api/explorer/category/${uri}?size=50`);
  if (!json || !json.data) return null;

  const entities = Array.isArray(json.data) ? json.data : (json.data.entities || []);
  const total = json.meta ? json.meta.total : entities.length;

  const nodes = entities.map((e, i) => ({
    id: e.uri,
    label: e.label || e.uri.split('/').pop().split('#').pop(),
    category: categoryKey,
    desc: '',
    count: 0,
    r: 12 + Math.random() * 6,
    isCategory: false,
    uri: e.uri,
    // Place near the category center for animation
    x: (CATEGORY_POSITIONS[categoryKey]?.x || 0) + (Math.random() - 0.5) * 80,
    y: (CATEGORY_POSITIONS[categoryKey]?.y || 0) + (Math.random() - 0.5) * 80,
  }));

  // Create links from category node to each entity
  const links = nodes.map(n => ({
    source: `cat:${categoryKey}`,
    target: n.id,
    label: 'hasInstance',
    isCross: false,
  }));

  return { nodes, links, total };
}

async function loadEntity(entityUri) {
  const json = await apiFetch(`/api/explorer/entity/${encodeURIComponent(entityUri)}`);
  if (!json || !json.data) return null;

  const data = json.data;
  const neighbors = [];
  const links = [];

  // Process outgoing connections
  if (data.outgoing) {
    data.outgoing.forEach(rel => {
      const targetId = rel.object;
      if (!targetId || targetId === entityUri) return;
      if (!neighbors.find(n => n.id === targetId)) {
        neighbors.push({
          id: targetId,
          label: rel.objectLabel || targetId.split('/').pop().split('#').pop(),
          category: categoryFromUri(targetId),
          desc: '',
          count: 0,
          r: 10,
          isCategory: false,
          uri: targetId,
        });
      }
      links.push({
        source: entityUri,
        target: targetId,
        label: rel.predicateName || '',
        isCross: false,
      });
    });
  }

  // Process incoming connections
  if (data.incoming) {
    data.incoming.forEach(rel => {
      const sourceId = rel.subject;
      if (!sourceId || sourceId === entityUri) return;
      if (!neighbors.find(n => n.id === sourceId)) {
        neighbors.push({
          id: sourceId,
          label: rel.subjectLabel || sourceId.split('/').pop().split('#').pop(),
          category: categoryFromUri(sourceId),
          desc: '',
          count: 0,
          r: 10,
          isCategory: false,
          uri: sourceId,
        });
      }
      links.push({
        source: sourceId,
        target: entityUri,
        label: rel.predicateName || '',
        isCross: false,
      });
    });
  }

  return { entity: data, neighbors, links };
}

async function searchEntities(query) {
  if (!query || !query.trim()) return null;
  const json = await apiFetch(`/api/explorer/search?q=${encodeURIComponent(query.trim())}`);
  if (!json || !json.data) return null;

  const results = Array.isArray(json.data) ? json.data : (json.data.results || []);
  return results.map(r => ({
    id: r.uri,
    label: r.label || r.uri.split('/').pop().split('#').pop(),
    category: categoryFromUri(r.type),
    desc: '',
    count: 0,
    r: 14,
    isCategory: false,
    uri: r.uri,
  }));
}

// ---------------------------------------------------------------------------
// Graph rendering (enter/update/exit pattern)
// ---------------------------------------------------------------------------

function isCrossCategory(d) {
  const src = typeof d.source === 'object' ? d.source : state.nodes.find(n => n.id === d.source);
  const tgt = typeof d.target === 'object' ? d.target : state.nodes.find(n => n.id === d.target);
  if (d.isCross) return true;
  if (src && tgt) return src.category !== tgt.category;
  return false;
}

function render() {
  // --- Links ---
  linkSel = linkLayer.selectAll('line')
    .data(state.links, d => `${typeof d.source === 'object' ? d.source.id : d.source}-${typeof d.target === 'object' ? d.target.id : d.target}`);

  linkSel.exit().transition().duration(300).attr('stroke-opacity', 0).remove();

  const linkEnter = linkSel.enter().append('line')
    .attr('stroke-opacity', 0);

  linkSel = linkEnter.merge(linkSel)
    .attr('stroke', d => isCrossCategory(d) ? '#fbbf24' : colorFor(
      (typeof d.source === 'object' ? d.source.category : '') || ''
    ))
    .attr('stroke-width', d => isCrossCategory(d) ? 1.8 : 1.2)
    .attr('stroke-dasharray', d => isCrossCategory(d) ? '6,3' : 'none')
    .attr('marker-end', d => {
      if (isCrossCategory(d)) return 'url(#arrow-cross)';
      const cat = typeof d.source === 'object' ? d.source.category : '';
      return cat ? `url(#arrow-${cat})` : '';
    });

  linkSel.transition().duration(400).attr('stroke-opacity', d => isCrossCategory(d) ? 0.3 : 0.25);

  // --- Edge labels ---
  edgeLabelSel = edgeLabelLayer.selectAll('text')
    .data(state.links, d => `label-${typeof d.source === 'object' ? d.source.id : d.source}-${typeof d.target === 'object' ? d.target.id : d.target}`);

  edgeLabelSel.exit().remove();

  const edgeLabelEnter = edgeLabelSel.enter().append('text')
    .attr('font-size', '7px')
    .attr('font-family', 'Inter, sans-serif')
    .attr('fill', 'rgba(148,163,184,0.5)')
    .attr('text-anchor', 'middle')
    .attr('dy', '-4')
    .attr('opacity', 0);

  edgeLabelSel = edgeLabelEnter.merge(edgeLabelSel)
    .text(d => d.label || '');

  edgeLabelSel.transition().duration(400)
    .attr('opacity', state.showEdgeLabels ? 1 : 0);

  // --- Nodes ---
  nodeSel = nodeLayer.selectAll('g.node')
    .data(state.nodes, d => d.id);

  // Exit
  nodeSel.exit()
    .transition().duration(300)
    .style('opacity', 0)
    .attr('transform', d => `translate(${d.x || 0},${d.y || 0}) scale(0.3)`)
    .remove();

  // Enter
  const nodeEnter = nodeSel.enter().append('g')
    .attr('class', 'node')
    .style('cursor', 'pointer')
    .style('opacity', 0)
    .call(d3.drag()
      .on('start', dragstarted)
      .on('drag', dragged)
      .on('end', dragended));

  // Outer glow circle
  nodeEnter.append('circle')
    .attr('class', 'outer')
    .attr('r', d => d.r)
    .attr('fill', d => colorFor(d.category))
    .attr('fill-opacity', 0.2)
    .attr('stroke', d => colorFor(d.category))
    .attr('stroke-width', 2)
    .attr('stroke-opacity', 0.7)
    .attr('filter', d => `url(#glow-${d.category})`);

  // Inner filled circle
  nodeEnter.append('circle')
    .attr('class', 'inner')
    .attr('r', d => d.r * 0.55)
    .attr('fill', d => colorFor(d.category))
    .attr('fill-opacity', 0.5);

  // Count badge for category nodes
  nodeEnter.filter(d => d.isCategory)
    .append('text')
    .attr('class', 'count-label')
    .attr('text-anchor', 'middle')
    .attr('dy', '0.35em')
    .attr('font-size', '9px')
    .attr('font-family', 'Inter, -apple-system, sans-serif')
    .attr('font-weight', '600')
    .attr('fill', '#f1f5f9')
    .text(d => typeof d.count === 'number' ? d.count.toLocaleString('et-EE') : '');

  // Label below node
  nodeEnter.append('text')
    .attr('class', 'node-label')
    .attr('text-anchor', 'middle')
    .attr('dy', d => d.r + 14)
    .attr('font-size', '10px')
    .attr('font-family', 'Inter, -apple-system, sans-serif')
    .attr('font-weight', '500')
    .attr('fill', '#cbd5e1')
    .text(d => {
      const maxLen = d.isCategory ? 30 : 25;
      return d.label.length > maxLen ? d.label.slice(0, maxLen) + '\u2026' : d.label;
    });

  // Attach event handlers to enter selection
  nodeEnter
    .on('mouseenter', onNodeMouseEnter)
    .on('mousemove', onNodeMouseMove)
    .on('mouseleave', onNodeMouseLeave)
    .on('click', onNodeClick);

  // Merge
  nodeSel = nodeEnter.merge(nodeSel);

  // Transition enter nodes to visible
  nodeSel.transition().duration(400).style('opacity', 1);

  // Update pinned visual
  nodeSel.selectAll('circle.outer')
    .attr('stroke-dasharray', d => state.pinnedNodes.has(d.id) ? '4,2' : null);

  // --- Restart simulation ---
  simulation.nodes(state.nodes);
  simulation.force('link').links(state.links);
  simulation.alpha(0.8).restart();
}

function ticked() {
  linkSel
    .attr('x1', d => d.source.x)
    .attr('y1', d => d.source.y)
    .attr('x2', d => d.target.x)
    .attr('y2', d => d.target.y);

  edgeLabelSel
    .attr('x', d => (d.source.x + d.target.x) / 2)
    .attr('y', d => (d.source.y + d.target.y) / 2);

  nodeSel.attr('transform', d => `translate(${d.x},${d.y})`);
}

// ---------------------------------------------------------------------------
// Interaction handlers
// ---------------------------------------------------------------------------

function onNodeMouseEnter(event, d) {
  tooltip.classList.add('visible');
  document.getElementById('tt-title').textContent = d.label;

  const catEl = document.getElementById('tt-cat');
  catEl.textContent = CATEGORY_LABELS_EN[d.category] || d.category;
  catEl.style.background = colorFor(d.category) + '22';
  catEl.style.color = colorFor(d.category);

  document.getElementById('tt-desc').textContent = d.desc || '';
  document.getElementById('tt-stat').textContent = d.isCategory
    ? `${(d.count || 0).toLocaleString('et-EE')} \u00fcksust`
    : (d.uri || '');

  // Highlight connected nodes
  const connectedIds = new Set();
  connectedIds.add(d.id);
  state.links.forEach(l => {
    const sid = typeof l.source === 'object' ? l.source.id : l.source;
    const tid = typeof l.target === 'object' ? l.target.id : l.target;
    if (sid === d.id) connectedIds.add(tid);
    if (tid === d.id) connectedIds.add(sid);
  });

  nodeSel.transition().duration(150).style('opacity', n => connectedIds.has(n.id) ? 1 : 0.12);
  linkSel.transition().duration(150).attr('stroke-opacity', l => {
    const sid = typeof l.source === 'object' ? l.source.id : l.source;
    const tid = typeof l.target === 'object' ? l.target.id : l.target;
    return (sid === d.id || tid === d.id) ? 0.7 : 0.04;
  });
  edgeLabelSel.transition().duration(150).attr('fill-opacity', l => {
    const sid = typeof l.source === 'object' ? l.source.id : l.source;
    const tid = typeof l.target === 'object' ? l.target.id : l.target;
    return (sid === d.id || tid === d.id) ? 1 : 0.05;
  });

  d3.select(this).select('circle.outer').transition().duration(150).attr('stroke-width', 3);
}

function onNodeMouseMove(event) {
  const x = event.clientX + 16;
  const y = event.clientY - 10;
  tooltip.style.left = (x + 320 > width ? x - 340 : x) + 'px';
  tooltip.style.top = y + 'px';
}

function onNodeMouseLeave() {
  tooltip.classList.remove('visible');
  nodeSel.transition().duration(200).style('opacity', 1);
  linkSel.transition().duration(200).attr('stroke-opacity', d => isCrossCategory(d) ? 0.3 : 0.25);
  edgeLabelSel.transition().duration(200).attr('fill-opacity', 1);
  d3.select(this).select('circle.outer').transition().duration(150).attr('stroke-width', 2);
}

async function onNodeClick(event, d) {
  event.stopPropagation();

  if (d.isCategory) {
    // Category node click: expand entities for this category
    await expandCategory(d.category);
  } else {
    // Entity node click: toggle pin + show detail
    if (state.pinnedNodes.has(d.id)) {
      state.pinnedNodes.delete(d.id);
      d.fx = null;
      d.fy = null;
    } else {
      state.pinnedNodes.add(d.id);
      d.fx = d.x;
      d.fy = d.y;
    }
    // Update pin visual
    d3.select(this).selectAll('circle.outer')
      .attr('stroke-dasharray', state.pinnedNodes.has(d.id) ? '4,2' : null);

    // Show detail panel
    await showEntityDetail(d);
  }
}

// ---------------------------------------------------------------------------
// View transitions
// ---------------------------------------------------------------------------

async function expandCategory(categoryKey) {
  if (state.expandedCategory === categoryKey) return;

  // Collapse previously expanded entities (keep overview nodes + new category)
  collapseToOverview(false);

  state.expandedCategory = categoryKey;
  state.view = 'category';
  updateBreadcrumb();

  const result = await loadCategory(categoryKey);
  if (!result) return;

  // Cap total nodes
  const available = MAX_NODES - state.nodes.length;
  const newNodes = result.nodes.slice(0, available);

  // Add new nodes (avoid duplicates)
  const existingIds = new Set(state.nodes.map(n => n.id));
  newNodes.forEach(n => {
    if (!existingIds.has(n.id)) {
      state.nodes.push(n);
      existingIds.add(n.id);
    }
  });

  // Add links
  result.links.forEach(l => {
    const sourceId = typeof l.source === 'object' ? l.source.id : l.source;
    const targetId = typeof l.target === 'object' ? l.target.id : l.target;
    if (existingIds.has(sourceId) && existingIds.has(targetId)) {
      state.links.push(l);
    }
  });

  render();
}

async function showEntityDetail(d) {
  state.selectedEntity = d.uri || d.id;
  state.view = 'entity';
  updateBreadcrumb();

  // Try to load detail from API
  const detail = await loadEntity(d.uri || d.id);

  // Populate panel
  const panelTitle = document.getElementById('panel-title');
  const panelCategory = document.getElementById('panel-category');
  const panelMeta = document.getElementById('panel-meta');
  const panelNeighbors = document.getElementById('panel-neighbors');
  const panelLink = document.getElementById('panel-link');

  panelTitle.textContent = d.label;
  panelCategory.textContent = CATEGORY_LABELS_EN[d.category] || d.category;
  panelCategory.style.background = colorFor(d.category) + '22';
  panelCategory.style.color = colorFor(d.category);

  // Metadata
  panelMeta.innerHTML = '';
  if (detail && detail.entity && detail.entity.metadata) {
    Object.entries(detail.entity.metadata).forEach(([key, val]) => {
      const row = document.createElement('div');
      row.className = 'meta-row';
      row.innerHTML = `<span class="meta-key">${escapeHtml(key)}</span><span class="meta-val">${escapeHtml(String(val))}</span>`;
      panelMeta.appendChild(row);
    });
  } else {
    const row = document.createElement('div');
    row.className = 'meta-row';
    row.innerHTML = `<span class="meta-key">URI</span><span class="meta-val">${escapeHtml(d.uri || d.id)}</span>`;
    panelMeta.appendChild(row);
  }

  // Neighbors
  panelNeighbors.innerHTML = '';
  if (detail && detail.neighbors && detail.neighbors.length > 0) {
    // Also add them to the graph
    const existingIds = new Set(state.nodes.map(n => n.id));
    let added = 0;
    detail.neighbors.forEach(nb => {
      if (!existingIds.has(nb.id) && state.nodes.length < MAX_NODES) {
        nb.x = d.x + (Math.random() - 0.5) * 60;
        nb.y = d.y + (Math.random() - 0.5) * 60;
        state.nodes.push(nb);
        existingIds.add(nb.id);
        added++;
      }
    });

    detail.links.forEach(l => {
      const sourceId = typeof l.source === 'object' ? l.source.id : l.source;
      const targetId = typeof l.target === 'object' ? l.target.id : l.target;
      if (existingIds.has(sourceId) && existingIds.has(targetId)) {
        // Avoid duplicate links
        const exists = state.links.some(existing => {
          const es = typeof existing.source === 'object' ? existing.source.id : existing.source;
          const et = typeof existing.target === 'object' ? existing.target.id : existing.target;
          return es === sourceId && et === targetId;
        });
        if (!exists) {
          state.links.push(l);
        }
      }
    });

    if (added > 0) render();

    // Populate neighbor list in panel
    detail.neighbors.forEach(nb => {
      const li = document.createElement('li');
      li.className = 'neighbor-item';
      li.innerHTML = `<span class="neighbor-dot" style="background:${colorFor(nb.category)}"></span>
        <span>${escapeHtml(nb.label)}</span>`;
      li.addEventListener('click', () => {
        const nodeData = state.nodes.find(n => n.id === nb.id);
        if (nodeData) showEntityDetail(nodeData);
      });
      panelNeighbors.appendChild(li);
    });
  } else {
    panelNeighbors.innerHTML = '<li style="color:#64748b;font-size:12px;list-style:none;">Seoseid ei leitud</li>';
  }

  // External link
  const uri = d.uri || d.id;
  if (uri && uri.startsWith('http')) {
    panelLink.href = uri;
    panelLink.textContent = 'Ava allikas';
    panelLink.style.display = '';
  } else {
    panelLink.style.display = 'none';
  }

  detailPanel.classList.add('open');
}

function closeDetail() {
  detailPanel.classList.remove('open');
}

function collapseToOverview(resetView) {
  // Remove all non-category nodes and their links
  state.nodes = state.nodes.filter(n => n.isCategory);
  state.links = state.links.filter(l => {
    const sid = typeof l.source === 'object' ? l.source.id : l.source;
    const tid = typeof l.target === 'object' ? l.target.id : l.target;
    return state.nodes.some(n => n.id === sid) && state.nodes.some(n => n.id === tid);
  });
  state.expandedCategory = null;
  state.pinnedNodes.clear();

  if (resetView !== false) {
    state.view = 'overview';
    closeDetail();
    updateBreadcrumb();
    render();
  }
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

let searchDebounceTimer = null;

async function performSearch() {
  const input = document.getElementById('search-input');
  const query = input.value.trim();
  if (!query) return;

  const results = await searchEntities(query);
  if (!results || results.length === 0) return;

  // Start from overview
  collapseToOverview(false);
  state.view = 'overview';

  // Add search result nodes
  const existingIds = new Set(state.nodes.map(n => n.id));
  results.forEach(r => {
    if (!existingIds.has(r.id) && state.nodes.length < MAX_NODES) {
      r.x = (Math.random() - 0.5) * 200;
      r.y = (Math.random() - 0.5) * 200;
      state.nodes.push(r);
      existingIds.add(r.id);

      // Link to matching category node
      const catNodeId = `cat:${r.category}`;
      if (existingIds.has(catNodeId)) {
        state.links.push({
          source: catNodeId,
          target: r.id,
          label: 'otsing',
          isCross: false,
        });
      }
    }
  });

  updateBreadcrumb();
  render();
}

// ---------------------------------------------------------------------------
// Breadcrumb
// ---------------------------------------------------------------------------

function updateBreadcrumb() {
  if (!breadcrumb) return;
  breadcrumb.innerHTML = '';

  const overview = document.createElement('span');
  overview.textContent = '\u00dclevaade';
  overview.addEventListener('click', () => {
    collapseToOverview(true);
  });
  breadcrumb.appendChild(overview);

  if (state.view === 'category' || state.view === 'entity') {
    const sep = document.createElement('span');
    sep.className = 'separator';
    sep.textContent = ' \u203a ';
    breadcrumb.appendChild(sep);

    const cat = document.createElement('span');
    const catKey = state.expandedCategory || (state.selectedEntity ? '' : '');
    cat.textContent = CATEGORY_LABELS_EN[catKey] || catKey || 'Kategooria';
    if (state.view === 'entity' && catKey) {
      cat.addEventListener('click', () => {
        closeDetail();
        state.view = 'category';
        updateBreadcrumb();
      });
    } else {
      cat.className = 'current';
    }
    breadcrumb.appendChild(cat);
  }

  if (state.view === 'entity' && state.selectedEntity) {
    const sep2 = document.createElement('span');
    sep2.className = 'separator';
    sep2.textContent = ' \u203a ';
    breadcrumb.appendChild(sep2);

    const entity = document.createElement('span');
    entity.className = 'current';
    const node = state.nodes.find(n => n.id === state.selectedEntity || n.uri === state.selectedEntity);
    entity.textContent = node ? (node.label.length > 30 ? node.label.slice(0, 30) + '\u2026' : node.label) : 'Olem';
    breadcrumb.appendChild(entity);
  }
}

// ---------------------------------------------------------------------------
// Controls (global functions called from HTML buttons)
// ---------------------------------------------------------------------------

// Expose to window for HTML onclick handlers
window.explorerReheat = function() {
  simulation.alpha(1).restart();
};

window.explorerToggleLabels = function() {
  state.showEdgeLabels = !state.showEdgeLabels;
  edgeLabelSel.transition().duration(200).attr('opacity', state.showEdgeLabels ? 1 : 0);
};

window.explorerGroupByCategory = function() {
  simulation
    .force('x', d3.forceX(d => (CATEGORY_POSITIONS[d.category]?.x) || 0).strength(0.6))
    .force('y', d3.forceY(d => (CATEGORY_POSITIONS[d.category]?.y) || 0).strength(0.6))
    .alpha(1).restart();
  setTimeout(() => {
    simulation.force('x', null).force('y', null);
  }, 3000);
};

window.explorerResetView = function() {
  collapseToOverview(true);
  // Reset zoom
  svg.transition().duration(500)
    .call(zoomBehavior.transform, d3.zoomIdentity.translate(width / 2, height / 2).scale(0.9));
};

window.explorerCollapseToOverview = function() {
  collapseToOverview(true);
};

window.explorerCloseDetail = closeDetail;

window.explorerSearch = performSearch;

// ---------------------------------------------------------------------------
// Drag handlers
// ---------------------------------------------------------------------------

function dragstarted(event, d) {
  if (!event.active) simulation.alphaTarget(0.3).restart();
  d.fx = d.x;
  d.fy = d.y;
}

function dragged(event, d) {
  d.fx = event.x;
  d.fy = event.y;
}

function dragended(event, d) {
  if (!event.active) simulation.alphaTarget(0);
  // Keep pinned if in pinned set, otherwise release
  if (!state.pinnedNodes.has(d.id)) {
    d.fx = null;
    d.fy = null;
  }
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function escapeHtml(str) {
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(str));
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Window resize
// ---------------------------------------------------------------------------

window.addEventListener('resize', () => {
  width = window.innerWidth;
  height = window.innerHeight;
  svg.attr('width', width).attr('height', height);
});

// ---------------------------------------------------------------------------
// Keyboard shortcut: Escape closes panel
// ---------------------------------------------------------------------------

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeDetail();
  }
});

// ---------------------------------------------------------------------------
// Search input: Enter key to search
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  const searchInput = document.getElementById('search-input');
  if (searchInput) {
    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        performSearch();
      }
    });
  }
});

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------

async function init() {
  const overview = await loadOverview();
  state.nodes = overview.nodes;
  state.links = overview.links;
  state.view = 'overview';
  updateBreadcrumb();
  render();
}

init();
