/**
 * app.js — Ego-graph frontend for rlm-music.
 *
 * Interaction model:
 *   1. Search for an artist → results dropdown
 *   2. Tap a result → fetch their ego-graph (1-hop neighborhood)
 *   3. Tap a neighbor node → that node becomes the new center, graph transitions
 *   4. Breadcrumb trail tracks navigation history (back-navigation)
 *
 * Desktop: graph on the left, detail panel on the right.
 * Mobile (<=640px): graph full-screen, detail in a bottom sheet.
 * Auto-detected via window.innerWidth, rechecked on resize.
 */

// ── DOM refs ──

const searchInput   = document.getElementById("search-input");
const searchBtn     = document.getElementById("search-btn");
const searchResults = document.getElementById("search-results");
const graphSvg      = document.getElementById("graph-svg");
const emptyState    = document.getElementById("empty-state");
const detailPanel   = document.getElementById("detail-panel");
const detailClose   = document.getElementById("detail-close");
const detailContent = document.getElementById("detail-content");
const bottomSheet   = document.getElementById("bottom-sheet");
const bottomSheetContent = document.getElementById("bottom-sheet-content");
const statsBar      = document.getElementById("stats-bar");
const overlay       = document.getElementById("overlay");
const overlayText   = document.getElementById("overlay-text");
const legendEl      = document.getElementById("legend");

// ── State ──

let simulation = null;       // D3 force simulation
let egoData    = null;       // current {center, nodes, links}


// ── Responsive helpers ──

/** True if the viewport is phone-sized. */
function isMobile() { return window.innerWidth <= 640; }


// ── Overlay helpers ──

function showOverlay(text) {
  overlayText.textContent = text;
  overlay.classList.remove("hidden");
}
function hideOverlay() { overlay.classList.add("hidden"); }


// ── Edge type visibility (collaboration hidden by default) ──
const edgeVisibility = {
  influenced_by: true,
  member_of: true,
  collaboration: false,
  teacher_of: true,
  subgroup: true,
};


// ══════════════════════════════════════════════════════════════
// Search
// ══════════════════════════════════════════════════════════════

async function doSearch() {
  const q = searchInput.value.trim();
  if (!q) return;

  showOverlay(`Searching "${q}"...`);

  try {
    const resp = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const data = await resp.json();
    hideOverlay();

    if (!data.length) {
      searchResults.innerHTML = '<div class="search-item" style="color:var(--text-muted)">No results</div>';
      searchResults.classList.remove("hidden");
      return;
    }

    searchResults.innerHTML = data.map(a => `
      <div class="search-item" data-mbid="${a.mbid}" data-name="${esc(a.name)}">
        <div class="search-item-name">${esc(a.name)}</div>
        <div class="search-item-meta">${a.type || "Artist"}${a.country ? " · " + a.country : ""}${a.disambiguation ? " · " + esc(a.disambiguation) : ""}</div>
      </div>
    `).join("");

    searchResults.classList.remove("hidden");

    // Tap a result → navigate to that artist's ego-graph
    searchResults.querySelectorAll(".search-item").forEach(el => {
      el.addEventListener("click", () => {
        searchResults.classList.add("hidden");
        searchInput.value = el.dataset.name;
        navigateTo(el.dataset.mbid, el.dataset.name);
      });
    });

  } catch (err) {
    hideOverlay();
    searchResults.innerHTML = `<div class="search-item" style="color:var(--red)">Error: ${err.message}</div>`;
    searchResults.classList.remove("hidden");
  }
}

searchBtn.addEventListener("click", doSearch);
searchInput.addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });

// Close dropdown on outside click
document.addEventListener("click", e => {
  if (!searchResults.contains(e.target) && e.target !== searchInput && e.target !== searchBtn) {
    searchResults.classList.add("hidden");
  }
});


// ══════════════════════════════════════════════════════════════
// Ego-graph navigation
// ══════════════════════════════════════════════════════════════

/**
 * Navigate to an artist: fetch their ego-graph and render it.
 */
async function navigateTo(mbid, name) {
  showOverlay(`Loading ${name || "artist"}...`);

  try {
    const resp = await fetch(`/api/ego/${mbid}?hops=2`);
    if (!resp.ok) throw new Error("Failed to load artist");
    egoData = await resp.json();
  } catch (err) {
    hideOverlay();
    alert("Error: " + err.message);
    return;
  }

  // Remember this artist so we reload here next time
  const displayName = egoData.center?.name || name || mbid;
  localStorage.setItem("rlm-music-last", JSON.stringify({ mbid, name: displayName }));
  searchInput.value = displayName;

  hideOverlay();
  emptyState.classList.add("hidden");
  renderEgoGraph(egoData);
  showDetail(egoData.center);
  loadStats();
}

// ══════════════════════════════════════════════════════════════
// D3 ego-graph rendering
// ══════════════════════════════════════════════════════════════

function renderEgoGraph(data) {
  const svg = d3.select("#graph-svg");
  svg.selectAll("*").remove();

  const width  = graphSvg.clientWidth;
  const height = graphSvg.clientHeight;
  const mobile = isMobile();

  if (!data.nodes || !data.nodes.length) return;

  const centerMbid = data.center?.mbid;
  const nodeById = new Map(data.nodes.map(n => [n.mbid, n]));

  // All links where both endpoints exist in the node set
  const allLinks = data.links.filter(l =>
    nodeById.has(l.source) && nodeById.has(l.target)
  );

  // Container group for zoom/pan
  const g = svg.append("g");
  const zoom = d3.zoom()
    .scaleExtent([0.3, 5])
    .on("zoom", e => g.attr("transform", e.transform));
  svg.call(zoom);

  // Disable double-tap zoom on mobile (conflicts with node tap)
  svg.on("dblclick.zoom", null);

  // ── Links ──
  const link = g.append("g").selectAll("line")
    .data(allLinks).join("line")
    .attr("class", d => `link ${d.rel_type}`)
    .attr("stroke-width", 1.2);

  // ── Nodes ──
  const node = g.append("g").selectAll("g")
    .data(data.nodes).join("g")
    .attr("class", d => {
      let cls = "node";
      if (d.mbid === centerMbid) cls += " center";
      const t = (d.artist_type || "").toLowerCase();
      if (["group", "orchestra", "choir"].includes(t)) cls += " group";
      else if (t === "person") cls += " person";
      else cls += " other";
      return cls;
    })
    .call(d3.drag()
      .on("start", dragStarted)
      .on("drag", dragged)
      .on("end", dragEnded));

  // Circle — center node is larger
  const baseR = mobile ? 12 : 8;
  const centerR = mobile ? 18 : 16;
  node.append("circle")
    .attr("r", d => d.mbid === centerMbid ? centerR : (d.name ? baseR : 5));

  // Label
  node.filter(d => d.name).append("text")
    .attr("dy", d => (d.mbid === centerMbid ? centerR + 10 : baseR + 10))
    .text(d => d.name.length > 20 ? d.name.slice(0, 18) + "..." : d.name);

  // Edge-count badge for nodes that have more edges than shown here
  node.filter(d => {
    const visibleCount = allLinks.filter(l => {
      const src = typeof l.source === "object" ? l.source.mbid : l.source;
      const tgt = typeof l.target === "object" ? l.target.mbid : l.target;
      return src === d.mbid || tgt === d.mbid;
    }).length;
    return d.total_edges && d.total_edges > visibleCount && d.mbid !== centerMbid;
  }).append("text")
    .attr("class", "edge-badge")
    .attr("dy", -baseR - 4)
    .text(d => `+${d.total_edges}`);

  // ── Node interactions ──
  // Click → show Wikipedia inline first; back button re-centers graph on that artist.
  // For the center node, just show Wikipedia (already centered).
  node.on("click", (event, d) => {
    event.stopPropagation();
    if (d.mbid === centerMbid) {
      openWikipedia(d.name);
    } else {
      openWikipediaAndNavigate(d.name, d.mbid);
    }
  });

  // Background click → close detail (desktop only).
  // On mobile the bottom sheet is the primary UI — don't close it on background tap,
  // otherwise there's no way to reopen it without tapping the center node.
  svg.on("click", () => {
    if (!isMobile()) closeDetail();
  });

  // ── Force simulation ──
  const centerNode = data.nodes.find(n => n.mbid === centerMbid);
  if (centerNode) {
    centerNode.fx = width / 2;
    centerNode.fy = height / 2;
  }

  if (simulation) simulation.stop();

  simulation = d3.forceSimulation(data.nodes)
    .force("link", d3.forceLink(allLinks).id(d => d.mbid).distance(mobile ? 90 : 110))
    .force("charge", d3.forceManyBody().strength(mobile ? -250 : -350))
    .force("center", d3.forceCenter(width / 2, height / 2).strength(0.05))
    .force("collision", d3.forceCollide().radius(d => d.mbid === centerMbid ? centerR + 8 : baseR + 6))
    .on("tick", () => {
      link
        .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
      node.attr("transform", d => `translate(${d.x},${d.y})`);
    });

  // Apply initial edge-type visibility (hides collaboration by default)
  applyVisibility(data, centerMbid, link, node);

  // Render legend
  renderLegend();
}


// ── Drag ──

function dragStarted(event, d) {
  if (!event.active) simulation.alphaTarget(0.3).restart();
  d.fx = d.x; d.fy = d.y;
}
function dragged(event, d) { d.fx = event.x; d.fy = event.y; }
function dragEnded(event, d) {
  if (!event.active) simulation.alphaTarget(0);
  // Keep center node pinned, release others
  if (egoData && d.mbid === egoData.center?.mbid) return;
  d.fx = null; d.fy = null;
}


// ══════════════════════════════════════════════════════════════
// Detail panel / bottom sheet
// ══════════════════════════════════════════════════════════════

/** Show artist detail in the appropriate container (panel or sheet). */
function showDetail(artistNode) {
  if (!artistNode) return;

  const html = buildDetailHtml(artistNode);

  if (isMobile()) {
    bottomSheetContent.innerHTML = html;
    bottomSheet.classList.remove("hidden");
    document.getElementById("info-btn")?.classList.add("hidden");
    wireDetailLinks(bottomSheetContent);
  } else {
    detailContent.innerHTML = html;
    detailPanel.classList.remove("hidden");
    wireDetailLinks(detailContent);
  }
}

/** Close both detail containers. On mobile, show the reopen button. */
function closeDetail() {
  detailPanel.classList.add("hidden");
  bottomSheet.classList.add("hidden");
  if (isMobile()) {
    document.getElementById("info-btn")?.classList.remove("hidden");
  }
}

detailClose.addEventListener("click", closeDetail);

// "i" button on mobile to reopen the bottom sheet after dismissal
document.getElementById("info-btn")?.addEventListener("click", () => {
  if (egoData?.center) {
    showDetail(egoData.center);
    document.getElementById("info-btn")?.classList.add("hidden");
  }
});

// Swipe down anywhere on the bottom sheet to collapse it.
// Tracks whether the gesture is a downward swipe vs normal scroll —
// only triggers if the sheet is scrolled to the top (can't scroll further up).
let sheetTouchY = 0;
let sheetScrolledToTop = false;
bottomSheet.addEventListener("touchstart", e => {
  sheetTouchY = e.touches[0].clientY;
  // Check if the sheet content is scrolled to the top — if so, a downward
  // drag should collapse the sheet rather than trying to scroll further
  sheetScrolledToTop = bottomSheet.scrollTop <= 0;
});
bottomSheet.addEventListener("touchend", e => {
  const dy = e.changedTouches[0].clientY - sheetTouchY;
  if (sheetScrolledToTop && dy > 40) closeDetail();
});

/** Build the detail HTML for an artist node from the ego-graph data. */
function buildDetailHtml(node) {
  if (!node) return "";

  let years = "";
  if (node.begin_year) years = `${node.begin_year}`;
  if (node.end_year) years += `–${node.end_year}`;
  else if (years) years += "–present";

  // Center artist header with album expand chevron
  let html = `<div class="detail-item-wrapper">`;
  html += `<div class="detail-header detail-item" data-mbid="${node.mbid}" data-name="${esc(node.name || node.mbid)}">`;
  html += `<button class="album-toggle" data-mbid="${node.mbid}" data-name="${esc(node.name || node.mbid)}" aria-label="Show albums" title="Show albums">&#9654;</button>`;
  html += `<h2><span class="detail-item-name">${esc(node.name || node.mbid)}</span></h2>`;
  html += `<button class="wiki-btn" data-name="${esc(node.name || node.mbid)}" title="Wikipedia">W</button>`;
  html += `</div>`;
  html += `<div class="album-list hidden" data-mbid="${node.mbid}"></div>`;
  html += `</div>`;

  html += `<div class="detail-meta">`;
  if (node.artist_type) html += node.artist_type;
  if (node.country) html += ` · ${node.country}`;
  if (years) html += ` · ${years}`;
  html += `</div>`;

  if (node.genres?.length) {
    html += `<div class="detail-genres">${node.genres.map(g => `<span class="genre-tag">${esc(g)}</span>`).join("")}</div>`;
  }

  // Group edges by relationship type and direction
  if (egoData) {
    const mbid = node.mbid;
    const sections = groupEdges(mbid, egoData);
    for (const [title, items] of sections) {
      if (!items.length) continue;
      html += `<div class="detail-section"><h3>${title}</h3>`;
      for (const item of items) {
        const yr = item.begin_year ? ` (${item.begin_year})` : "";
        // Orange dot for unexpanded nodes to hint "tap to explore"
        const dot = (!item.expanded && item.mbid !== mbid) ? '<span class="expand-dot"></span>' : '';
        // Each artist gets a wrapper: the artist row (with album chevron) + a collapsible album sub-list
        html += `<div class="detail-item-wrapper">`;
        html += `<div class="detail-item" data-mbid="${item.mbid}" data-name="${esc(item.name)}">`;
        html += `<button class="album-toggle" data-mbid="${item.mbid}" data-name="${esc(item.name)}" aria-label="Show albums" title="Show albums">&#9654;</button>`;
        html += `${dot}<span class="detail-item-name">${esc(item.name)}${yr}</span>`;
        html += `</div>`;
        html += `<div class="album-list hidden" data-mbid="${item.mbid}"></div>`;
        html += `</div>`;
      }
      html += `</div>`;
    }
  }

  return html;
}

/**
 * Group the ego-graph edges around a specific artist into labeled sections.
 * Returns an array of [title, items[]] pairs.
 */
function groupEdges(mbid, data) {
  const nodeMap = new Map(data.nodes.map(n => [n.mbid, n]));
  const groups = {
    "Influenced by": [],
    "Influenced": [],
    "Member of": [],
    "Members": [],
    "Collaborators": [],
    "Taught by": [],
    "Students": [],
  };

  for (const link of data.links) {
    const src = typeof link.source === "object" ? link.source.mbid : link.source;
    const tgt = typeof link.target === "object" ? link.target.mbid : link.target;
    const rt  = link.rel_type;

    let otherMbid = null;
    let direction = null;

    if (src === mbid) { otherMbid = tgt; direction = "outgoing"; }
    else if (tgt === mbid) { otherMbid = src; direction = "incoming"; }
    else continue;

    const other = nodeMap.get(otherMbid);
    if (!other) continue;

    const item = {
      mbid: other.mbid,
      name: other.name || other.mbid,
      begin_year: other.begin_year,
      expanded: other.expanded,
    };

    if (rt === "influenced_by") {
      if (direction === "outgoing") groups["Influenced by"].push(item);
      else groups["Influenced"].push(item);
    } else if (rt === "member_of") {
      if (direction === "outgoing") groups["Member of"].push(item);
      else groups["Members"].push(item);
    } else if (rt === "collaboration") {
      groups["Collaborators"].push(item);
    } else if (rt === "teacher_of") {
      if (direction === "outgoing") groups["Taught by"].push(item);
      else groups["Students"].push(item);
    }
  }

  return Object.entries(groups);
}

/** Wire click handlers on detail items: name navigates, chevron toggles albums, W opens Wikipedia. */
function wireDetailLinks(container) {
  // Wikipedia button opens the artist's page inline
  container.querySelectorAll(".wiki-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openWikipedia(btn.dataset.name);
    });
  });

  // Clicking the artist name shows Wikipedia first, then re-centers on back
  container.querySelectorAll(".detail-item-name").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = el.closest(".detail-item");
      openWikipediaAndNavigate(item.dataset.name, item.dataset.mbid);
    });
  });

  // Clicking the chevron toggles the album sub-list
  container.querySelectorAll(".album-toggle").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const mbid = btn.dataset.mbid;
      const name = btn.dataset.name;
      const wrapper = btn.closest(".detail-item-wrapper");
      const albumList = wrapper.querySelector(".album-list");

      // Toggle open/closed
      const isOpen = !albumList.classList.contains("hidden");
      if (isOpen) {
        albumList.classList.add("hidden");
        btn.classList.remove("open");
        return;
      }

      // Open it
      albumList.classList.remove("hidden");
      btn.classList.add("open");

      // If already loaded, don't re-fetch
      if (albumList.dataset.loaded) return;

      // Lazy-load albums from the API
      albumList.innerHTML = '<div class="album-loading">Loading albums...</div>';
      fetch(`/api/artist/${mbid}/releases`)
        .then(resp => {
          if (!resp.ok) throw new Error("Failed to load");
          return resp.json();
        })
        .then(releases => {
          albumList.dataset.loaded = "1";
          if (!releases.length) {
            albumList.innerHTML = '<div class="album-loading">No albums found</div>';
            return;
          }
          albumList.innerHTML = releases.map(r => {
            const yr = r.year ? `<span class="album-year">${r.year}</span>` : "";
            if (r.wikipedia_url) {
              return `<div class="album-item"><a class="album-wiki-link" href="${esc(r.wikipedia_url)}">${esc(r.title)}</a>${yr}</div>`;
            }
            return `<div class="album-item"><span>${esc(r.title)}</span>${yr}</div>`;
          }).join("");

          // Wire album links to open Wikipedia inline (same back-button UX)
          albumList.querySelectorAll(".album-wiki-link").forEach(a => {
            a.addEventListener("click", (e) => {
              e.preventDefault();
              showWikipediaInline(a.href);
            });
          });
        })
        .catch(() => {
          albumList.innerHTML = '<div class="album-loading">Could not load albums</div>';
        });
    });
  });
}


// ══════════════════════════════════════════════════════════════
// Legend
// ══════════════════════════════════════════════════════════════

/**
 * Hide/show links and orphaned nodes based on edgeVisibility state.
 * A node is hidden if it's not the center and ALL its links are hidden.
 */
function applyVisibility(data, centerMbid, linkSel, nodeSel) {
  // Hide links whose rel_type is toggled off
  linkSel.classed("edge-hidden", d => !edgeVisibility[d.rel_type]);

  // Determine which nodes are connected by at least one visible edge
  const connectedMbids = new Set();
  connectedMbids.add(centerMbid);

  data.links.forEach(l => {
    if (!edgeVisibility[l.rel_type]) return;
    const src = typeof l.source === "object" ? l.source.mbid : l.source;
    const tgt = typeof l.target === "object" ? l.target.mbid : l.target;
    connectedMbids.add(src);
    connectedMbids.add(tgt);
  });

  // Hide nodes that have no visible edges (except center)
  nodeSel.classed("edge-hidden", d => d.mbid !== centerMbid && !connectedMbids.has(d.mbid));
}

function renderLegend() {
  const items = [
    { type: "influenced_by", label: "Influenced by", color: "var(--edge-influenced)", dashed: false },
    { type: "member_of",     label: "Member of",     color: "var(--edge-member)",     dashed: false },
    { type: "collaboration", label: "Collaboration", color: "var(--edge-collab)",      dashed: true },
    { type: "teacher_of",    label: "Teacher",       color: "var(--edge-teacher)",     dashed: false },
  ];

  legendEl.innerHTML = items.map(it => {
    const on = edgeVisibility[it.type];
    const swatchClass = it.dashed ? "legend-swatch dashed" : "legend-swatch";
    const swatchStyle = it.dashed ? "" : `style="background:${it.color}"`;
    return `<div class="legend-toggle${on ? "" : " off"}" data-type="${it.type}">
      <div class="${swatchClass}" ${swatchStyle}></div>${it.label}
    </div>`;
  }).join("");

  // Click handlers — toggle visibility and re-apply
  legendEl.querySelectorAll(".legend-toggle").forEach(el => {
    el.addEventListener("click", () => {
      const type = el.dataset.type;
      edgeVisibility[type] = !edgeVisibility[type];
      el.classList.toggle("off", !edgeVisibility[type]);

      // Re-apply visibility to existing graph elements
      if (egoData) {
        const svg = d3.select("#graph-svg");
        const linkSel = svg.selectAll(".link");
        const nodeSel = svg.selectAll(".node");
        applyVisibility(egoData, egoData.center?.mbid, linkSel, nodeSel);
      }
    });
  });
}


// ══════════════════════════════════════════════════════════════
// Stats
// ══════════════════════════════════════════════════════════════

async function loadStats() {
  try {
    const resp = await fetch("/api/stats");
    const data = await resp.json();
    if (data.has_data) {
      statsBar.textContent = `${data.artists_named} artists · ${data.edges} edges`;
    }
  } catch { /* ignore */ }
}


// ══════════════════════════════════════════════════════════════
// Utility
// ══════════════════════════════════════════════════════════════

function esc(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

/**
 * Show a Wikipedia URL inline in the detail panel / bottom sheet.
 *
 * Replaces the panel content with an iframe and a back button.
 * The back button either restores the previous detail view, or runs
 * an optional onBack callback (used to navigate to a new artist).
 *
 * Uses Wikipedia's mobile-optimized domain (en.m.wikipedia.org) for
 * cleaner rendering inside the narrow panel/sheet.
 *
 * @param {string} url - The Wikipedia URL to display.
 * @param {Function|null} onBack - Optional callback to run when back is pressed.
 *                                  If null, restores the previous panel content.
 */
function showWikipediaInline(url, onBack) {
  // Rewrite to mobile Wikipedia for better fit in narrow panel
  const mobileUrl = url.replace("//en.wikipedia.org/", "//en.m.wikipedia.org/");
  const desktopUrl = url.replace("//en.m.wikipedia.org/", "//en.wikipedia.org/");

  const iframeHtml = `
    <div class="wiki-view">
      <div class="wiki-toolbar">
        <button class="wiki-back" aria-label="Back to detail">&#8592; Back</button>
        <a class="wiki-open-ext" href="${esc(desktopUrl)}" target="_blank" rel="noopener" title="Open in new tab">&#8599;</a>
      </div>
      <iframe class="wiki-iframe" src="${esc(mobileUrl)}" sandbox="allow-scripts allow-same-origin allow-popups"></iframe>
    </div>
  `;

  const container = isMobile() ? bottomSheetContent : detailContent;
  const savedHtml = container.innerHTML;

  container.innerHTML = iframeHtml;

  if (isMobile()) {
    bottomSheet.classList.remove("hidden");
    bottomSheet.classList.add("wiki-fullscreen");
  } else {
    detailPanel.classList.remove("hidden");
  }

  container.querySelector(".wiki-back").addEventListener("click", () => {
    if (isMobile()) {
      bottomSheet.classList.remove("wiki-fullscreen");
    }
    if (onBack) {
      // Custom back action (e.g. navigate to a new artist)
      onBack();
    } else {
      // Default: restore the previous panel content
      container.innerHTML = savedHtml;
      wireDetailLinks(container);
    }
  });
}

/**
 * Resolve an artist name to a Wikipedia URL via opensearch, then show it inline.
 * Back button restores the current detail view.
 */
async function openWikipedia(name) {
  if (!name) return;
  const url = await resolveWikipediaUrl(name);
  showWikipediaInline(url, null);
}

/**
 * Show Wikipedia for an artist, then navigate to their ego-graph on back.
 * This is the main click flow: click node → read about them → back → graph re-centers.
 */
async function openWikipediaAndNavigate(name, mbid) {
  if (!name) return;
  const url = await resolveWikipediaUrl(name);
  showWikipediaInline(url, () => {
    navigateTo(mbid, name);
  });
}

/**
 * Resolve an artist/topic name to a Wikipedia URL via the opensearch API.
 */
async function resolveWikipediaUrl(name) {
  const fallback = `https://en.wikipedia.org/wiki/Special:Search/${encodeURIComponent(name)}`;
  try {
    const resp = await fetch(
      `https://en.wikipedia.org/w/api.php?action=opensearch&search=${encodeURIComponent(name)}&limit=1&namespace=0&format=json&origin=*`
    );
    const data = await resp.json();
    return data[3]?.[0] || fallback;
  } catch {
    return fallback;
  }
}


// ══════════════════════════════════════════════════════════════
// Init — if the DB has a seed artist, navigate to it on load
// ══════════════════════════════════════════════════════════════

// Default artist to load on first visit (before any search)
const DEFAULT_ARTIST = { name: "Crosby, Stills, Nash & Young", mbid: "46a782ea-4308-476b-abd1-a91b197f3037" };

async function init() {
  loadStats();

  // Restore last-viewed artist from localStorage
  try {
    const saved = JSON.parse(localStorage.getItem("rlm-music-last"));
    if (saved?.mbid) {
      navigateTo(saved.mbid, saved.name);
      return;
    }
  } catch { /* bad or missing data */ }

  // No saved state — start with the default artist
  navigateTo(DEFAULT_ARTIST.mbid, DEFAULT_ARTIST.name);
}

init();
