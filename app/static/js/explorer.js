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

const DEFAULT_COLOR = '#94a3b8';

const CATEGORY_COLORS = {
  'EnactedLaw':       '#38bdf8',
  'DraftLegislation': '#a78bfa',
  'CourtDecision':    '#fb923c',
  'EULegislation':    '#34d399',
  'EUCourtDecision':  '#f472b6',
  'LegalProvision':   '#60a5fa',
  'TopicCluster':     '#c084fc',
  'LegalConcept':     '#fbbf24',
  // Structural sub-categories
  'Section':            '#7dd3fc',
  'Division':           '#6ee7b7',
  'Chapter':            '#93c5fd',
  'Subdivision':        '#a5b4fc',
  'LegalPart':          '#86efac',
  'CaseType':           '#fdba74',
  'LegislativePhase':   '#d8b4fe',
  'ProcedureStage':     '#cbd5e1',
};

// Human-readable labels for categories (Estonian — all displayed text)
const CATEGORY_LABELS = {
  'EnactedLaw':       'Kehtiv seadus',
  'DraftLegislation': 'Eeln\u00f5u',
  'CourtDecision':    'Kohtulahend',
  'EULegislation':    'EL \u00f5igusakt',
  'EUCourtDecision':  'EL kohtulahend',
  'LegalProvision':   '\u00d5igusnorm',
  'TopicCluster':     'Teemaklaster',
  'LegalConcept':     '\u00d5igusm\u00f5iste',
  // Structural sub-categories
  'Section':          'Jagu',
  'Division':         'Jaotis',
  'Chapter':          'Peat\u00fckk',
  'Subdivision':      'Alljaotis',
  'LegalPart':        '\u00d5igusosa',
  'CaseType':         'Kohtuasja liik',
  'LegislativePhase': 'Seadusloome etapp',
  'ProcedureStage':   'Menetlusetapp',
};

const CATEGORY_POSITIONS = {
  'EnactedLaw':       { x: -200, y: -150 },
  'DraftLegislation': { x:  200, y: -150 },
  'CourtDecision':    { x: -200, y:  150 },
  'EULegislation':    { x:  200, y:  150 },
  'EUCourtDecision':  { x:    0, y:  250 },
  'LegalProvision':   { x:    0, y: -250 },
  'TopicCluster':     { x: -300, y:    0 },
  'LegalConcept':     { x:  300, y:    0 },
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
  // #754: when true the page rendered the contextual start panel and we must
  // NOT auto-fetch the 90k category overview — the user picks what to load.
  startPanelMode: false,
  // Whether loadOverview() has run at least once (so explorerShowFullMap()
  // can no-op back to the existing overview instead of re-fetching it).
  overviewLoaded: false,
  // #756: the slug of the active legal-view preset, or null. Set by
  // explorerApplyPreset(); reflected into the URL (?vaade=<slug>) and the
  // active toolbar chip.
  activePreset: null,
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

// #746: the graph now lives inside the standard PageShell content area
// (`.main-content--full`), not full-viewport. Size everything off that box;
// fall back to the viewport defensively if the element is missing (e.g. in
// a stripped-down test DOM).
const mainEl = document.getElementById('main-content');

function _contentSize() {
  if (mainEl) {
    const r = mainEl.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) return { width: r.width, height: r.height };
  }
  return { width: window.innerWidth || 1200, height: window.innerHeight || 800 };
}

let { width, height } = _contentSize();

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

// Default glow filter for unknown categories
const defaultGlow = defs.append('filter').attr('id', 'glow-default');
defaultGlow.append('feGaussianBlur').attr('stdDeviation', '4').attr('result', 'blur');
defaultGlow.append('feFlood').attr('flood-color', DEFAULT_COLOR).attr('flood-opacity', '0.4').attr('result', 'color');
defaultGlow.append('feComposite').attr('in', 'color').attr('in2', 'blur').attr('operator', 'in').attr('result', 'shadow');
const defaultGlowMerge = defaultGlow.append('feMerge');
defaultGlowMerge.append('feMergeNode').attr('in', 'shadow');
defaultGlowMerge.append('feMergeNode').attr('in', 'SourceGraphic');

// Default arrow marker for unknown categories
defs.append('marker')
  .attr('id', 'arrow-default')
  .attr('viewBox', '0 -4 8 8')
  .attr('refX', 8).attr('refY', 0)
  .attr('markerWidth', 6).attr('markerHeight', 6)
  .attr('orient', 'auto')
  .append('path')
  .attr('d', 'M0,-3L8,0L0,3')
  .attr('fill', DEFAULT_COLOR)
  .attr('opacity', 0.5);

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
  if (lower.includes('legalprovision') || lower.includes('legal_provision')) return 'LegalProvision';
  if (lower.includes('topiccluster') || lower.includes('topic_cluster')) return 'TopicCluster';
  if (lower.includes('legalconcept') || lower.includes('legal_concept')) return 'LegalConcept';
  if (lower.includes('enacted')) return 'EnactedLaw';
  if (lower.includes('provision')) return 'LegalProvision';
  if (lower.includes('draft')) return 'DraftLegislation';
  if (lower.includes('eucourt') || lower.includes('eu_court')) return 'EUCourtDecision';
  if (lower.includes('court') || lower.includes('decision')) return 'CourtDecision';
  if (lower.includes('eu') || lower.includes('directive') || lower.includes('regulation')) return 'EULegislation';
  if (lower.includes('concept')) return 'LegalConcept';
  if (lower.includes('topic') || lower.includes('cluster')) return 'TopicCluster';
  // Return the raw short name — colorFor() will apply DEFAULT_COLOR
  return short;
}

function colorFor(category) {
  return CATEGORY_COLORS[category] || DEFAULT_COLOR;
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
      label: CATEGORY_LABELS[catKey] || catKey,
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
    label: CATEGORY_LABELS[d.key],
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
      if (!cat) return '';
      return CATEGORY_COLORS[cat] ? `url(#arrow-${cat})` : 'url(#arrow-default)';
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
    .attr('filter', d => CATEGORY_COLORS[d.category] ? `url(#glow-${d.category})` : 'url(#glow-default)');

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

  // --- Update legend to reflect current categories ---
  updateLegend();

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
  catEl.textContent = CATEGORY_LABELS[d.category] || d.category;
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
  // #746: #tooltip is `position: absolute` inside `.main-content--full`, so
  // translate the viewport-relative pointer coords into the content box.
  const rect = mainEl ? mainEl.getBoundingClientRect() : { left: 0, top: 0 };
  const x = event.clientX - rect.left + 16;
  const y = event.clientY - rect.top - 10;
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
// Dynamic legend — reflects categories present in current data
// ---------------------------------------------------------------------------

function updateLegend() {
  const legendEl = document.getElementById('legend');
  if (!legendEl) return;

  // Collect unique categories present in the current node set
  const presentCats = new Set();
  state.nodes.forEach(n => {
    if (n.category) presentCats.add(n.category);
  });

  // Clear existing items but keep the heading
  legendEl.innerHTML = '';
  const heading = document.createElement('h3');
  heading.textContent = 'Kategooriad';
  legendEl.appendChild(heading);

  if (presentCats.size === 0) return;

  // Render a legend item for each category in the data
  presentCats.forEach(cat => {
    const item = document.createElement('div');
    item.className = 'legend-item';

    const dot = document.createElement('div');
    dot.className = 'legend-dot';
    dot.style.background = colorFor(cat);

    item.appendChild(dot);
    item.appendChild(document.createTextNode(CATEGORY_LABELS[cat] || cat));
    legendEl.appendChild(item);
  });
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
  // #757: remember the entity the panel was showing *before* this click, so
  // the evidence card's "Seose liik" slot can name the relation from the
  // previously-focused node to the newly-selected one.
  const prevEntity = state.selectedEntity;
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
  // #757: stash the entity URI on the title element so _PANEL_ANNOTATION_SCRIPT
  // (the MutationObserver wiring the entity-level AnnotationButton) picks the
  // real URI rather than the human-readable label.
  panelTitle.dataset.entityUri = d.uri || d.id || '';
  panelCategory.textContent = CATEGORY_LABELS[d.category] || d.category;
  panelCategory.style.background = colorFor(d.category) + '22';
  panelCategory.style.color = colorFor(d.category);

  // #757: the evidence-card slots (Allikas / Kuupäev-versioon / Seose liik /
  // Miks see oluline on) + the four action buttons.
  populateEvidenceCard(d, detail ? detail.entity : null, prevEntity);

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

  // Version history
  state.selectedEntityData = detail ? detail.entity : null;
  renderVersionHistory(detail ? detail.entity : null);

  // Reset bookmark button (#757: relabelled "Lisa j\u00e4rjehoidja" to fit the
  // evidence card's "Tegevused" group; the XHR path itself is unchanged).
  const bookmarkBtn = document.getElementById('panel-bookmark-btn');
  if (bookmarkBtn) {
    bookmarkBtn.textContent = 'Lisa j\u00e4rjehoidja';
    bookmarkBtn.classList.remove('bookmarked');
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

// ---------------------------------------------------------------------------
// #757: evidence-card detail panel (epic #762, design doc
// docs/2026-05-12-oiguskaart-evidence-map.md, workstream D).
//
// Fills the panel's evidence-card slots — Allikas (source act / draft / court)
// · Kuupäev / versioon · Seose liik (relation type in legal language) · Miks
// see oluline on (a deterministic one-line note) — and wires the four action
// buttons (Küsi nõustajalt → POST /chat/seed · Ava analüüsikeskuses →
// /analyysikeskus/normi-mojuahel?sisend=<uri> · Lisa märkus → the entity-level
// annotation button · Lisa järjehoidja → the #743 XHR bookmark button).
//
// The relation-phrase + "why it matters" strings come from the entity-detail
// API (which derives them from a small static rule table — no LLM call); this
// function only picks the right one and renders it.
// ---------------------------------------------------------------------------

// The optional ?draft=<uuid> on /explorer — threaded into the "Küsi nõustajalt"
// form so the new conversation picks up the draft's impact context. UUID-only,
// so exposing it via location.search is harmless.
function _explorerDraftParam() {
  try {
    var d = new URLSearchParams(window.location.search).get('draft') || '';
    return /^[0-9a-fA-F-]{1,64}$/.test(d) ? d : '';
  } catch (e) {
    return '';
  }
}

function _showEvidenceSection(sectionId, show) {
  var el = document.getElementById(sectionId);
  if (el) el.style.display = show ? '' : 'none';
}

// Append a <span class="evidence-date-label">…</span><span class="evidence-date-value">…</span>
// pair to a container using DOM methods (no innerHTML).
function _appendKvRow(container, rowClass, keyClass, keyText, valClass, valText) {
  var row = document.createElement('div');
  row.className = rowClass;
  var k = document.createElement('span');
  k.className = keyClass;
  k.textContent = keyText;
  var v = document.createElement('span');
  v.className = valClass;
  v.textContent = valText;
  row.appendChild(k);
  row.appendChild(v);
  container.appendChild(row);
}

// Find the relation (predicateLabel + whyText) from the previously-focused
// entity to the now-selected one, by scanning the new entity's incoming /
// outgoing relations for one whose other endpoint is `prevEntity`.
function _relationToPrev(entityData, prevEntity, selfUri) {
  if (!entityData || !prevEntity || prevEntity === selfUri) return null;
  var incoming = Array.isArray(entityData.incoming) ? entityData.incoming : [];
  for (var i = 0; i < incoming.length; i++) {
    if (incoming[i] && incoming[i].subject === prevEntity) {
      return {
        label: incoming[i].predicateLabel || incoming[i].predicateName || '',
        why: incoming[i].whyText || '',
        otherLabel: incoming[i].subjectLabel || '',
      };
    }
  }
  var outgoing = Array.isArray(entityData.outgoing) ? entityData.outgoing : [];
  for (var j = 0; j < outgoing.length; j++) {
    if (outgoing[j] && outgoing[j].object === prevEntity) {
      return {
        label: outgoing[j].predicateLabel || outgoing[j].predicateName || '',
        why: outgoing[j].whyText || '',
        otherLabel: outgoing[j].objectLabel || '',
      };
    }
  }
  return null;
}

// Pick a "primary" relation for the entity when there's no previously-focused
// node to relate to — the first outgoing relation with a known legal phrase
// (so a freshly-opened ?focus= panel still shows *something* sensible).
function _primaryRelation(entityData) {
  if (!entityData) return null;
  var outgoing = Array.isArray(entityData.outgoing) ? entityData.outgoing : [];
  // Prefer relations the rule table actually phrases (predicateLabel + whyText
  // present) and skip the rdf:type / rdfs:label noise.
  for (var i = 0; i < outgoing.length; i++) {
    var rel = outgoing[i];
    if (!rel) continue;
    var name = (rel.predicateName || '').toLowerCase();
    if (name === 'type' || name === 'label') continue;
    if (rel.predicateLabel && rel.whyText) {
      return {
        label: rel.predicateLabel,
        why: rel.whyText,
        otherLabel: rel.objectLabel || '',
      };
    }
  }
  return null;
}

function populateEvidenceCard(d, entityData, prevEntity) {
  var selfUri = d.uri || d.id || '';

  // ---- Allikas (source: parent act / draft / court) -------------------------
  var sourceRow = document.getElementById('panel-source-row');
  var source = entityData && entityData.source ? entityData.source : null;
  if (sourceRow && source && source.uri) {
    while (sourceRow.firstChild) sourceRow.removeChild(sourceRow.firstChild);
    var kind = document.createElement('span');
    kind.className = 'evidence-source-kind';
    kind.textContent = (source.kindLabel || 'Allikas') + ': ';
    var link = document.createElement('a');
    link.className = 'evidence-source-link';
    // Deep-link the source itself into the map (the existing ?focus= contract).
    link.href = '/explorer?focus=' + encodeURIComponent(source.uri);
    link.textContent = source.label || source.uri;
    sourceRow.appendChild(kind);
    sourceRow.appendChild(link);
    _showEvidenceSection('evidence-source-section', true);
  } else {
    _showEvidenceSection('evidence-source-section', false);
  }

  // ---- Kuupäev / versioon ---------------------------------------------------
  var dateInfoEl = document.getElementById('panel-date-info');
  var dateInfo = (entityData && Array.isArray(entityData.dateInfo)) ? entityData.dateInfo : [];
  if (dateInfoEl && dateInfo.length) {
    while (dateInfoEl.firstChild) dateInfoEl.removeChild(dateInfoEl.firstChild);
    dateInfo.forEach(function(di) {
      _appendKvRow(
        dateInfoEl, 'evidence-date-row',
        'evidence-date-label', String(di.label || ''),
        'evidence-date-value', String(di.value || '')
      );
    });
    _showEvidenceSection('evidence-date-section', true);
  } else {
    _showEvidenceSection('evidence-date-section', false);
  }

  // ---- Seose liik + Miks see oluline on -------------------------------------
  var rel = _relationToPrev(entityData, prevEntity, selfUri) || _primaryRelation(entityData);
  var relationEl = document.getElementById('panel-relation');
  var whyEl = document.getElementById('panel-why');
  if (rel && rel.label) {
    if (relationEl) {
      relationEl.textContent = rel.otherLabel ? (rel.label + ' — ' + rel.otherLabel) : rel.label;
    }
    _showEvidenceSection('evidence-relation-section', true);
  } else {
    _showEvidenceSection('evidence-relation-section', false);
  }
  if (rel && rel.why) {
    if (whyEl) whyEl.textContent = rel.why;
    _showEvidenceSection('evidence-why-section', true);
  } else {
    _showEvidenceSection('evidence-why-section', false);
  }

  // ---- Tegevused (action buttons) -------------------------------------------
  // (1) Küsi nõustajalt selle kohta — fill the /chat/seed form's hidden inputs.
  var seedTextEl = document.getElementById('panel-chat-seed-text');
  var seedDraftEl = document.getElementById('panel-chat-seed-draft');
  if (seedTextEl) {
    var label = d.label || selfUri;
    var seedParts = ['«' + label + '»'];
    if (rel && rel.label && rel.otherLabel) {
      seedParts.push(rel.label + ' «' + rel.otherLabel + '»');
    } else if (rel && rel.label) {
      seedParts.push(rel.label);
    }
    var finding = seedParts.join(' ');
    seedTextEl.value = 'Selgita seda õiguskaardi leidu: ' + finding +
      '. Mida peaksin selle puhul tähele panema?';
  }
  if (seedDraftEl) seedDraftEl.value = _explorerDraftParam();

  // (2) Ava analüüsikeskuses — /analyysikeskus/normi-mojuahel?sisend=<uri>.
  var akLink = document.getElementById('panel-analyysikeskus-link');
  if (akLink) {
    if (selfUri && selfUri.indexOf('http') === 0) {
      akLink.href = '/analyysikeskus/normi-mojuahel?sisend=' + encodeURIComponent(selfUri);
      akLink.style.display = '';
    } else {
      akLink.style.display = 'none';
    }
  }
  // (3) Lisa märkus — handled by _PANEL_ANNOTATION_SCRIPT (it watches
  //     #panel-title and (un)hides #panel-annotation-btn). Nothing to do here.
  // (4) Lisa järjehoidja — the #743 XHR button; its reset happens below in
  //     showEntityDetail() (kept where it was).
}

// ---------------------------------------------------------------------------
// #719: open the explorer focused on a specific entity (from ?focus=<uri>,
// e.g. a link in an impact report / analysis). Loads the entity's
// neighbourhood, opens the detail panel on it, centers the view, and
// reveals the "back" link.
// ---------------------------------------------------------------------------
function _fallbackLabel(uri) {
  try {
    return decodeURIComponent(String(uri)).split('/').pop().split('#').pop() || String(uri);
  } catch (e) {
    return String(uri);
  }
}

function _backLabel() {
  var ref = document.referrer || '';
  if (ref.indexOf('/report') !== -1) return '← Tagasi aruandesse';
  if (ref.indexOf('/analyysikeskus') !== -1) return '← Tagasi analüüsi';
  return '← Tagasi';
}

function _wireBack(el) {
  if (!el) return;
  el.textContent = _backLabel();
  el.style.display = '';
  el.onclick = function(e) {
    e.preventDefault();
    if (document.referrer) {
      window.location.href = document.referrer;
    } else {
      window.history.back();
    }
  };
}

// Detail-panel "← Tagasi" link (#719) — unhidden + wired when the page was
// opened from a report/analysis (via ?focus=).
function _showBackLink() {
  _wireBack(document.getElementById('panel-back'));
}

// #746: the toolbar-level "← Tagasi aruandesse" link is server-rendered
// (visible) whenever the page carries back-context (?focus= or ?draft=);
// wire its label + handler on init.
function _wireToolbarBack() {
  _wireBack(document.getElementById('toolbar-back'));
}

// Pan/zoom so *d* sits at the centre of the viewport (#719 — the generic
// zoomToFit() only frames the bounding box of *all* nodes, which won't
// centre the focused entity when overview/neighbour nodes are present).
function centerOnNode(d, duration) {
  if (!d) return;
  duration = duration || 500;
  var x = (d.x != null) ? d.x : 0;
  var y = (d.y != null) ? d.y : 0;
  var scale = 1.1;
  var transform = d3.zoomIdentity
    .translate(width / 2, height / 2)
    .scale(scale)
    .translate(-x, -y);
  svg.transition().duration(duration).call(zoomBehavior.transform, transform);
}

function _entityHasContent(d) {
  if (!d) return false;
  var hasMeta = d.metadata && typeof d.metadata === 'object' &&
    Object.keys(d.metadata).length > 0;
  var hasOut = Array.isArray(d.outgoing) && d.outgoing.length > 0;
  var hasIn = Array.isArray(d.incoming) && d.incoming.length > 0;
  return !!(hasMeta || hasOut || hasIn);
}

async function focusOnEntity(uri) {
  if (!uri) return;
  // Pre-flight: confirm the entity actually exists in the graph before
  // touching the panel. /api/explorer/entity/{uri} returns a (non-null)
  // data object for *any* syntactically valid URI, with everything empty
  // when the URI isn't in the ontology — so "found" means the response
  // carries some metadata / a relation, not just HTTP 200. Raw fetch (not
  // apiFetch) so a stale URI from an old report link doesn't console.error.
  // #719 DoD: unknown URI → toast + the plain overview, no console noise.
  var found = false;
  try {
    var resp = await fetch('/api/explorer/entity/' + encodeURIComponent(uri));
    if (resp.ok) {
      var j = await resp.json();
      found = !!(j && _entityHasContent(j.data));
    }
  } catch (e) {
    found = false;
  }
  if (!found) {
    showToast('Üksust ei leitud — kuvan ülevaate.', 'warning');
    closeDetail();
    return;
  }

  // Reuse an existing overview node if it's already on the graph.
  var datum = state.nodes.find(function(n) { return (n.uri || n.id) === uri; });
  if (!datum) {
    datum = {
      id: uri,
      uri: uri,
      label: _fallbackLabel(uri),
      category: categoryFromUri(uri),
      desc: '',
      count: 0,
      r: 16,
      isCategory: false,
      x: width / 2,
      y: height / 2,
    };
    if (state.nodes.length < MAX_NODES) {
      state.nodes.push(datum);
      render();
    }
  }
  await showEntityDetail(datum);
  // Upgrade the panel title from real metadata when we have it (the
  // datum's fallback label is just the URI fragment).
  var meta = (state.selectedEntityData && state.selectedEntityData.metadata) || null;
  if (meta) {
    var labelKey = Object.keys(meta).find(function(k) {
      var kl = k.toLowerCase();
      return kl.indexOf('label') !== -1 || kl.indexOf('title') !== -1 ||
        kl.indexOf('pealkiri') !== -1 || kl.indexOf('nimetus') !== -1;
    });
    if (labelKey && meta[labelKey]) {
      var titleEl = document.getElementById('panel-title');
      if (titleEl) titleEl.textContent = String(meta[labelKey]);
      datum.label = String(meta[labelKey]);
    }
  }
  _showBackLink();
  // Centre on the focused node once the simulation has placed neighbours
  // (mirrors init()'s 800ms settle wait).
  setTimeout(function() { centerOnNode(datum, 600); }, 800);
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

  // Hide breadcrumb in overview — it's redundant when no drill-down
  if (state.view === 'overview') {
    breadcrumb.style.display = 'none';
    return;
  }
  breadcrumb.style.display = '';

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
    cat.textContent = CATEGORY_LABELS[catKey] || catKey || 'Kategooria';
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
  // Center on current nodes
  setTimeout(function() { zoomToFit(500); }, 300);
};

window.explorerCollapseToOverview = function() {
  collapseToOverview(true);
};

window.explorerCloseDetail = closeDetail;

window.explorerSearch = performSearch;

window.explorerResetTimeline = resetTimeline;

window.explorerBookmark = addBookmark;

// #754: leave the contextual start panel and load today's full category
// overview in place — wired to the start panel's "Sirvi liikide kaupa" /
// "Näita kogu kaarti" buttons and the toolbar's "Näita kogu kaarti" item.
// Idempotent: if the overview is already loaded, just (re)collapses to it.
window.explorerShowFullMap = function() {
  _dismissStartPanel();
  state.startPanelMode = false;
  if (state.overviewLoaded) {
    collapseToOverview(true);
    setTimeout(function() { zoomToFit(500); }, 300);
    return;
  }
  loadFullOverview().then(function() {
    setTimeout(function() { zoomToFit(600); }, 800);
  });
};

// ---------------------------------------------------------------------------
// #756 — legal-view presets: a named bundle of the knobs the explorer already
// has (which graph categories / relation types to keep, plus the timeline
// mode). The server hands the table (and the resolved ?vaade= slug) via
// window.__explorerPresets / window.__explorerVaade; the constant below is a
// fallback so the file is self-contained in a stripped test DOM.
// ---------------------------------------------------------------------------

const LEGAL_VIEW_PRESETS_FALLBACK = {
  'kehtiv-oigus': {
    categories: ['EnactedLaw', 'LegalProvision', 'Section', 'Division', 'Chapter', 'Subdivision', 'LegalPart'],
    relKeywords: ['contains', 'haspart', 'ispartof', 'hasprovision', 'hasinstance'],
    timeline: false,
  },
  'eelnou-mojud': {
    categories: ['DraftLegislation', 'LegalProvision', 'EnactedLaw'],
    relKeywords: ['reference', 'viit', 'affect', 'mojut', 'conflict', 'vastuolu', 'amend', 'muut'],
    timeline: false,
  },
  'el-seosed': {
    categories: ['EULegislation', 'LegalProvision', 'EnactedLaw'],
    relKeywords: ['transpos', 'ulevot', 'directive', 'direktiiv', 'harmonis', 'harmonee', 'implement'],
    timeline: false,
  },
  'kohtupraktika': {
    categories: ['CourtDecision', 'EUCourtDecision', 'LegalProvision'],
    relKeywords: ['interpret', 'tolgenda', 'appl', 'kohalda', 'cite', 'viit'],
    timeline: false,
  },
  'ajalugu': {
    categories: [],
    relKeywords: ['amend', 'muut', 'version', 'versioon', 'supersede', 'asend', 'replace'],
    timeline: true,
  },
};

function _presetTable() {
  var t = (typeof window !== 'undefined' && window.__explorerPresets) || null;
  if (t && typeof t === 'object' && Object.keys(t).length > 0) return t;
  return LEGAL_VIEW_PRESETS_FALLBACK;
}

function _presetConfig(slug) {
  if (!slug) return null;
  var t = _presetTable();
  return Object.prototype.hasOwnProperty.call(t, slug) ? t[slug] : null;
}

// Does this link's predicate label match any of the preset's relation
// keywords? Empty keyword list ⇒ "keep every relation" (the ajalugu preset
// relies on this for the cross-category overview links it has no name for).
function _linkMatchesPreset(link, relKeywords) {
  if (!relKeywords || relKeywords.length === 0) return true;
  var lbl = String((link && link.label) || '').toLowerCase();
  // The synthetic category→entity / cross-category overview edges carry
  // generic labels ('', 'hasInstance', 'otsing'); keep them so categories
  // don't end up as orphan dots after a node filter.
  if (lbl === '' || lbl === 'hasinstance' || lbl === 'otsing' || link.isCross) return true;
  for (var i = 0; i < relKeywords.length; i++) {
    if (lbl.indexOf(relKeywords[i]) !== -1) return true;
  }
  return false;
}

// Repaint the toolbar so the chip for *slug* (or none) is the active one.
function _paintActivePresetChip(slug) {
  var group = document.getElementById('explorer-presets');
  if (group) group.setAttribute('data-active-vaade', slug || '');
  var chips = document.querySelectorAll('#explorer-presets .preset-chip');
  for (var i = 0; i < chips.length; i++) {
    var c = chips[i];
    var on = c.getAttribute('data-vaade') === slug;
    c.classList.toggle('active', on);
    c.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
}

// Mirror the active preset into the URL (?vaade=<slug>) without a navigation
// or a history entry — deep-linkable, but the back button isn't littered.
function _reflectPresetInUrl(slug) {
  if (typeof history === 'undefined' || !history.replaceState) return;
  try {
    var url = new URL(window.location.href);
    if (slug) url.searchParams.set('vaade', slug);
    else url.searchParams.delete('vaade');
    history.replaceState(history.state, '', url.toString());
  } catch (e) { /* old browser without URL/searchParams — skip silently */ }
}

// Apply a preset's filter combo on top of the (full) overview graph: keep only
// nodes in the preset's categories (empty ⇒ all), keep links whose predicate
// matches the preset's relation keywords, and turn the timeline on/off. Then
// re-frame. Idempotent. Unknown slug → clear any active preset (back to the
// unfiltered overview), still graceful.
async function applyLegalViewPreset(slug, opts) {
  opts = opts || {};
  _dismissStartPanel();
  state.startPanelMode = false;

  var cfg = _presetConfig(slug);
  if (!cfg) {
    // Unknown / cleared: drop back to the plain overview.
    state.activePreset = null;
    _paintActivePresetChip(null);
    if (opts.reflectUrl !== false) _reflectPresetInUrl(null);
    if (!state.overviewLoaded) { await loadFullOverview(); }
    return;
  }

  // Make sure we have the full overview to filter (and start from it, not from
  // a previous preset's already-trimmed node set). ``freshOverview`` lets the
  // init() path skip a redundant re-fetch — it just loaded the overview.
  if (!state.overviewLoaded) {
    await loadFullOverview();
  } else if (!opts.freshOverview) {
    collapseToOverview(false);
    var fresh = await loadOverview();
    state.nodes = fresh.nodes;
    state.links = fresh.links;
    state.view = 'overview';
  }

  var cats = cfg.categories || [];
  if (cats.length > 0) {
    var keep = new Set(cats);
    state.nodes = state.nodes.filter(function(n) { return keep.has(n.category); });
    var nodeIds = new Set(state.nodes.map(function(n) { return n.id; }));
    state.links = state.links.filter(function(l) {
      var sid = typeof l.source === 'object' ? l.source.id : l.source;
      var tid = typeof l.target === 'object' ? l.target.id : l.target;
      return nodeIds.has(sid) && nodeIds.has(tid);
    });
  }
  // Relation-type filter (best-effort — explorer edges are predicate names).
  var nodeIds2 = new Set(state.nodes.map(function(n) { return n.id; }));
  state.links = state.links.filter(function(l) {
    var sid = typeof l.source === 'object' ? l.source.id : l.source;
    var tid = typeof l.target === 'object' ? l.target.id : l.target;
    if (!nodeIds2.has(sid) || !nodeIds2.has(tid)) return false;
    return _linkMatchesPreset(l, cfg.relKeywords);
  });

  state.expandedCategory = null;
  state.pinnedNodes.clear();
  closeDetail();
  updateBreadcrumb();
  render();

  // Timeline mode: presets that want it turn the existing temporal filter on;
  // the others make sure it's off (so switching presets is clean).
  if (cfg.timeline) {
    var slider = document.getElementById('timeline-slider');
    var year = slider ? parseInt(slider.value, 10) : state.timelineYear;
    if (!year || isNaN(year)) year = state.timelineYear;
    // applyTimelineFilter() replaces the graph with its own temporal layout —
    // fine; the categories shown are still driven by what's valid at that year.
    await applyTimelineFilter(year);
  } else if (state.timelineActive) {
    state.timelineActive = false;
    var sl = document.getElementById('timeline-slider');
    if (sl) sl.value = '2026';
    var ve = document.getElementById('timeline-value');
    if (ve) ve.textContent = 'Väljas';
  }

  state.activePreset = slug;
  _paintActivePresetChip(slug);
  if (opts.reflectUrl !== false) _reflectPresetInUrl(slug);
  setTimeout(function() { zoomToFit(600); }, 800);
}

// Toolbar chip onclick → apply (and reflect in the URL). Clicking the already
// active chip toggles it off (back to the unfiltered overview).
window.explorerApplyPreset = function(slug) {
  if (slug && state.activePreset === slug) {
    applyLegalViewPreset(null, {});
    return;
  }
  applyLegalViewPreset(slug, {});
};

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
// Toast notifications
// ---------------------------------------------------------------------------

function showToast(message, type) {
  type = type || 'info';
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = message;
  container.appendChild(toast);

  // Auto-remove after animation completes (3s total)
  setTimeout(function() {
    if (toast.parentNode) toast.parentNode.removeChild(toast);
  }, 3200);
}

// ---------------------------------------------------------------------------
// WebSocket — real-time sync notifications
// ---------------------------------------------------------------------------

function initWebSocket() {
  var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  var wsUrl = protocol + '//' + window.location.host + '/ws/explorer';
  var ws = null;
  var reconnectDelay = 2000;

  function connect() {
    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      // WebSocket construction can fail in test environments; ignore.
      return;
    }

    ws.onopen = function() {
      reconnectDelay = 2000;
    };

    ws.onmessage = function(event) {
      try {
        var data = JSON.parse(event.data);
        if (data.event === 'sync_complete') {
          showToast(data.message || 'Andmebaas uuendatud', 'success');
          // Optionally reload current view to reflect new data
          if (state.view === 'overview') {
            init();
          }
        }
      } catch (e) {
        // Non-JSON message or notification HTML from FastHTML; show as-is if it
        // looks like a sync notification.
        var text = event.data;
        if (text && text.indexOf('uuendatud') !== -1) {
          showToast('Andmebaas uuendatud', 'success');
        }
      }
    };

    ws.onclose = function() {
      // Attempt to reconnect with exponential backoff (max 30s).
      setTimeout(function() {
        reconnectDelay = Math.min(reconnectDelay * 1.5, 30000);
        connect();
      }, reconnectDelay);
    };

    ws.onerror = function() {
      // onerror is always followed by onclose; nothing extra needed.
    };
  }

  connect();
}

// ---------------------------------------------------------------------------
// Timeline — temporal filtering
// ---------------------------------------------------------------------------

let timelineDebounce = null;

async function loadTimeline(year) {
  var date = year + '-07-01';  // Mid-year as representative date
  var json = await apiFetch('/api/explorer/timeline?date=' + date + '&size=50');
  if (!json || !json.data) return null;
  return json;
}

async function applyTimelineFilter(year) {
  state.timelineActive = true;
  state.timelineYear = year;

  var valueEl = document.getElementById('timeline-value');
  if (valueEl) valueEl.textContent = year;

  var result = await loadTimeline(year);
  if (!result) return;

  // Replace current graph with timeline-filtered entities
  var entities = result.data || [];
  var total = (result.meta && result.meta.total) || entities.length;

  var nodes = [];
  var links = [];

  // Group entities by category for display
  var catCounts = {};
  entities.forEach(function(e) {
    var catKey = categoryFromUri(e.type);
    if (!catCounts[catKey]) catCounts[catKey] = { count: 0, entities: [] };
    catCounts[catKey].count++;
    catCounts[catKey].entities.push(e);
  });

  // Build category nodes with filtered counts
  Object.keys(catCounts).forEach(function(catKey) {
    var info = catCounts[catKey];
    nodes.push({
      id: 'cat:' + catKey,
      label: CATEGORY_LABELS[catKey] || catKey,
      category: catKey,
      desc: info.count + ' kehtivat ' + year + '. a.',
      count: info.count,
      r: Math.max(20, Math.min(40, 15 + Math.log10(info.count + 1) * 8)),
      isCategory: true,
      uri: null,
    });

    // Add individual entity nodes (up to first 10 per category)
    info.entities.slice(0, 10).forEach(function(e) {
      var nodeId = e.uri;
      nodes.push({
        id: nodeId,
        label: e.label || nodeId.split('/').pop().split('#').pop(),
        category: catKey,
        desc: (e.validFrom || '') + ' \u2013 ' + (e.validUntil || 'kehtiv'),
        count: 0,
        r: 12,
        isCategory: false,
        uri: e.uri,
      });
      links.push({
        source: 'cat:' + catKey,
        target: nodeId,
        label: 'kehtiv',
        isCross: false,
      });
    });
  });

  // Cross-category links
  var catKeys = Object.keys(catCounts);
  for (var i = 0; i < catKeys.length; i++) {
    for (var j = i + 1; j < catKeys.length; j++) {
      links.push({
        source: 'cat:' + catKeys[i],
        target: 'cat:' + catKeys[j],
        label: '',
        isCross: true,
      });
    }
  }

  state.nodes = nodes;
  state.links = links;
  state.view = 'overview';
  state.expandedCategory = null;
  state.pinnedNodes.clear();
  closeDetail();
  updateBreadcrumb();
  render();

  showToast('N\u00e4itan ' + total + ' kehtivat olemit aastal ' + year, 'info');
}

function resetTimeline() {
  state.timelineActive = false;
  state.timelineYear = 2026;

  var slider = document.getElementById('timeline-slider');
  if (slider) slider.value = '2026';

  var valueEl = document.getElementById('timeline-value');
  if (valueEl) valueEl.textContent = 'Väljas';

  // Reload default overview
  init();
}

// ---------------------------------------------------------------------------
// Bookmarking from explorer
// ---------------------------------------------------------------------------

async function addBookmark() {
  if (!state.selectedEntity) return;

  var entityUri = state.selectedEntity;
  var node = state.nodes.find(function(n) {
    return n.id === entityUri || n.uri === entityUri;
  });
  var label = node ? node.label : '';

  try {
    // #743: ask /api/bookmarks for a JSON response (X-Requested-With) instead
    // of following its 303 — a `redirect: 'manual'` fetch can't tell a save
    // (303 → /dashboard) from an expired session (303 → /auth/login), so the
    // old code marked the button "bookmarked" even when nothing was saved.
    var resp = await fetch('/api/bookmarks', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: 'entity_uri=' + encodeURIComponent(entityUri) + '&label=' + encodeURIComponent(label),
    });

    if (resp.ok) {
      showToast('Lisatud j\u00e4rjehoidjatesse: ' + (label || entityUri), 'success');
      var btn = document.getElementById('panel-bookmark-btn');
      if (btn) {
        btn.textContent = 'J\u00e4rjehoidjas \u2713';
        btn.classList.add('bookmarked');
      }
    } else if (resp.status === 401 || resp.status === 403) {
      showToast('Sessioon on aegunud \u2014 logige uuesti sisse, et lisada j\u00e4rjehoidjaid.', 'warning');
    } else {
      showToast('J\u00e4rjehoidja lisamine eba\u00f5nnestus', 'warning');
    }
  } catch (e) {
    showToast('J\u00e4rjehoidja lisamine eba\u00f5nnestus', 'warning');
  }
}

// ---------------------------------------------------------------------------
// #754 — contextual start panel: gate the 90k-graph load behind a choice
// ---------------------------------------------------------------------------

// Hide (and stop occupying) the start-panel overlay. Cheap + idempotent.
function _dismissStartPanel() {
  var panel = document.getElementById('explorer-start-panel');
  if (panel) panel.style.display = 'none';
}

// Load the category overview into state + render it. Does NOT touch the
// zoom/viewport — callers decide whether to fit-all afterwards (the plain
// init path does; the ?focus= / ?search= paths re-frame themselves).
// Factored out of init() so explorerShowFullMap() (the "Näita kogu kaarti" /
// "Sirvi liikide kaupa" buttons) can call it on demand.
async function loadFullOverview() {
  var overview = await loadOverview();
  state.nodes = overview.nodes;
  state.links = overview.links;
  state.view = 'overview';
  state.overviewLoaded = true;
  updateBreadcrumb();
  render();
}

// Wire the start panel's own search form: intercept submit and reuse the
// in-page search flow (the same path the toolbar "Otsi" button uses) instead
// of a full navigation. A no-JS submit still works — the <form> is
// method=GET action=/explorer, so it lands on /explorer?search=<term>, which
// the server then renders in graph mode with the search pre-run.
function _wireStartPanel() {
  var form = document.getElementById('start-panel-search-form');
  if (!form) return;
  form.addEventListener('submit', function(e) {
    var input = document.getElementById('start-panel-search-input');
    var term = input ? input.value.trim() : '';
    if (!term) { e.preventDefault(); return; }
    e.preventDefault();
    // Mirror the term into the toolbar search box (kept in sync for when the
    // user re-opens "Vaate seaded") and run the shared search.
    var toolbarInput = document.getElementById('search-input');
    if (toolbarInput) toolbarInput.value = term;
    _dismissStartPanel();
    state.startPanelMode = false;
    // performSearch() collapses to the overview first — make sure it exists.
    var run = function() { performSearch(); };
    if (!state.overviewLoaded) {
      loadFullOverview().then(run);
    } else {
      run();
    }
  });
}

// ---------------------------------------------------------------------------
// Version history rendering
// ---------------------------------------------------------------------------

function renderVersionHistory(entityData) {
  var section = document.getElementById('version-history-section');
  var container = document.getElementById('panel-versions');
  if (!section || !container) return;

  container.innerHTML = '';

  // Extract version-related metadata from the entity detail
  var versions = [];

  if (entityData && entityData.metadata) {
    var meta = entityData.metadata;

    // Collect version-related fields
    var validFrom = meta.validFrom || meta.kehtivAlates || meta.jõustumisKuupäev || '';
    var validUntil = meta.validUntil || meta.kehtivKuni || meta.kehtetuksKuupäev || '';
    var dateAdopted = meta.dateAdopted || meta.vastuvõtmisKuupäev || '';
    var datePublished = meta.datePublished || meta.avaldamisKuupäev || '';

    if (dateAdopted) {
      versions.push({ date: dateAdopted, label: 'Vastuv\u00f5etud' });
    }
    if (datePublished) {
      versions.push({ date: datePublished, label: 'Avaldatud' });
    }
    if (validFrom) {
      versions.push({ date: validFrom, label: 'J\u00f5ustunud' });
    }
    if (validUntil) {
      versions.push({ date: validUntil, label: 'Kehtetu' });
    }
  }

  // Check outgoing relations for amendments and versions
  if (entityData && entityData.outgoing) {
    entityData.outgoing.forEach(function(rel) {
      var pred = (rel.predicateName || '').toLowerCase();
      if (pred.indexOf('amend') !== -1 || pred.indexOf('muut') !== -1 ||
          pred.indexOf('version') !== -1 || pred.indexOf('versioon') !== -1) {
        versions.push({
          date: '',
          label: (rel.predicateName || 'Muudatus') + ': ' + (rel.objectLabel || rel.object || ''),
        });
      }
    });
  }

  // Check incoming relations for amendments
  if (entityData && entityData.incoming) {
    entityData.incoming.forEach(function(rel) {
      var pred = (rel.predicateName || '').toLowerCase();
      if (pred.indexOf('amend') !== -1 || pred.indexOf('muut') !== -1 ||
          pred.indexOf('version') !== -1 || pred.indexOf('versioon') !== -1) {
        versions.push({
          date: '',
          label: (rel.subjectLabel || rel.subject || '') + ' (' + (rel.predicateName || 'muudatus') + ')',
        });
      }
    });
  }

  if (versions.length === 0) {
    section.style.display = 'none';
    return;
  }

  // Sort by date (entries with dates first)
  versions.sort(function(a, b) {
    if (a.date && b.date) return a.date.localeCompare(b.date);
    if (a.date) return -1;
    if (b.date) return 1;
    return 0;
  });

  section.style.display = '';

  var ul = document.createElement('ul');
  ul.className = 'version-timeline';

  versions.forEach(function(v) {
    var li = document.createElement('li');
    li.className = 'version-entry';

    if (v.date) {
      var dateSpan = document.createElement('span');
      dateSpan.className = 'version-date';
      dateSpan.textContent = v.date;
      li.appendChild(dateSpan);
    }

    var labelSpan = document.createElement('span');
    labelSpan.className = 'version-label';
    labelSpan.textContent = v.label;
    li.appendChild(labelSpan);

    ul.appendChild(li);
  });

  container.appendChild(ul);
}

// ---------------------------------------------------------------------------
// Zoom-to-fit — centers graph in viewport after render
// ---------------------------------------------------------------------------

function zoomToFit(duration) {
  duration = duration || 500;
  if (state.nodes.length === 0) return;

  var padding = 80;
  var xMin = Infinity, xMax = -Infinity, yMin = Infinity, yMax = -Infinity;
  state.nodes.forEach(function(n) {
    var nx = n.x || 0;
    var ny = n.y || 0;
    var nr = n.r || 20;
    if (nx - nr < xMin) xMin = nx - nr;
    if (nx + nr > xMax) xMax = nx + nr;
    if (ny - nr < yMin) yMin = ny - nr;
    if (ny + nr > yMax) yMax = ny + nr;
  });

  var bw = xMax - xMin;
  var bh = yMax - yMin;
  if (bw <= 0 || bh <= 0) return;

  var midX = (xMin + xMax) / 2;
  var midY = (yMin + yMax) / 2;
  var scale = Math.min((width - padding * 2) / bw, (height - padding * 2) / bh, 1.5);
  scale = Math.max(0.3, Math.min(scale, 1.5));

  var transform = d3.zoomIdentity
    .translate(width / 2, height / 2)
    .scale(scale)
    .translate(-midX, -midY);

  svg.transition().duration(duration).call(zoomBehavior.transform, transform);
}

// ---------------------------------------------------------------------------
// Resize handling — the content area can change size without a window resize
// (sidebar toggling, devtools docking, etc.), so a ResizeObserver on
// `.main-content--full` is the right primitive. A `window.resize` listener is
// kept as a cheap fallback for environments without ResizeObserver.
// ---------------------------------------------------------------------------

function _handleResize() {
  const next = _contentSize();
  if (Math.abs(next.width - width) < 1 && Math.abs(next.height - height) < 1) return;
  width = next.width;
  height = next.height;
  svg.attr('width', width).attr('height', height);
  // Re-frame the graph in the resized box: zoomToFit re-derives a centered
  // transform from the current node bounding box. (forceCenter stays at the
  // origin — it's in graph space, independent of the viewport size.)
  zoomToFit(200);
}

if (typeof ResizeObserver !== 'undefined' && mainEl) {
  const _ro = new ResizeObserver(() => { _handleResize(); });
  _ro.observe(mainEl);
}
window.addEventListener('resize', _handleResize);

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

  // Timeline slider event listener
  const timelineSlider = document.getElementById('timeline-slider');
  if (timelineSlider) {
    timelineSlider.addEventListener('input', (e) => {
      const year = parseInt(e.target.value, 10);
      const valueEl = document.getElementById('timeline-value');
      if (valueEl) valueEl.textContent = year;

      // Debounce the API call
      if (timelineDebounce) clearTimeout(timelineDebounce);
      timelineDebounce = setTimeout(() => {
        applyTimelineFilter(year);
      }, 400);
    });
  }
});

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------

let wsInitialized = false;

async function init() {
  // #746: wire the toolbar-level "← Tagasi" link if the server rendered it
  // (it's present whenever the page carries back-context — ?focus= / ?draft=).
  _wireToolbarBack();
  // #754: the start panel has its own search form — always wire it (it's a
  // no-op when the panel isn't on the page).
  _wireStartPanel();

  // #754: a "cold" open rendered the contextual start panel — the server set
  // window.__explorerStartPanel. Do NOT fetch the 90k category overview; the
  // graph chrome is idle behind the panel until the user picks "Sirvi liikide
  // kaupa" / "Näita kogu kaarti" (→ explorerShowFullMap) or runs a search.
  if (window.__explorerStartPanel) {
    state.startPanelMode = true;
    // The detail panel can still be Escape-closed etc.; just no graph data.
    // WebSocket sync notifications are still useful — connect once.
    if (!wsInitialized) {
      wsInitialized = true;
      initWebSocket();
    }
    return;
  }

  await loadFullOverview();

  // #719: arrived via ?focus=<uri> (e.g. a link from an impact report)?
  // Jump straight to that entity instead of leaving the user on the
  // generic overview.
  var focusUri = window.__explorerFocus;
  if (!focusUri) {
    try { focusUri = new URLSearchParams(window.location.search).get('focus'); }
    catch (e) { focusUri = null; }
  }
  // #746: ?search=<term> deep link — pre-run the search on load. ?focus=
  // is more specific, so it wins when both are present.
  var searchTerm = focusUri ? null : window.__explorerSearch;
  if (!focusUri && !searchTerm) {
    try { searchTerm = new URLSearchParams(window.location.search).get('search'); }
    catch (e) { searchTerm = null; }
  }
  // #756: ?vaade=<slug> deep link — apply that legal-view preset on load.
  // ?focus= / ?search= are more specific, so they win when combined; an
  // unknown slug is ignored (applyLegalViewPreset() just falls back).
  var vaadeSlug = (focusUri || searchTerm) ? null : window.__explorerVaade;
  if (!focusUri && !searchTerm && !vaadeSlug) {
    try { vaadeSlug = new URLSearchParams(window.location.search).get('vaade'); }
    catch (e) { vaadeSlug = null; }
  }
  if (focusUri) {
    await focusOnEntity(focusUri);
  } else if (searchTerm && String(searchTerm).trim()) {
    var searchInput = document.getElementById('search-input');
    if (searchInput) searchInput.value = String(searchTerm);
    await performSearch();
    setTimeout(function() { zoomToFit(600); }, 800);
  } else if (vaadeSlug && _presetConfig(vaadeSlug)) {
    // Don't re-write the URL we were given — it's already ?vaade=<slug> — and
    // skip the redundant overview re-fetch (loadFullOverview just ran).
    await applyLegalViewPreset(vaadeSlug, { reflectUrl: false, freshOverview: true });
  } else {
    // Plain overview — centre the graph once the simulation settles.
    setTimeout(function() { zoomToFit(600); }, 800);
  }

  // Start WebSocket connection once
  if (!wsInitialized) {
    wsInitialized = true;
    initWebSocket();
  }
}

init();
