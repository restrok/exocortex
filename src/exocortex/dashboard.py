# ruff: noqa: E501
"""Ultra-lightweight embedded dashboard for Exocortex with dynamic maps."""

from __future__ import annotations

import collections
import json
from typing import Any

from exocortex.service import BrainService


def get_dashboard_data(service: BrainService) -> dict[str, Any]:
    """Extract real-time vault metrics, component health, and recent memories."""
    notes = list(service.vault.iter_notes())
    by_type = dict(collections.Counter(n.metadata.type for n in notes))
    by_state = dict(collections.Counter(n.metadata.recommendation_state for n in notes))

    sorted_notes = sorted(
        notes,
        key=lambda n: n.metadata.updated_at or n.metadata.created_at,
        reverse=True,
    )

    recent = [
        {
            "id": str(n.metadata.id),
            "title": n.metadata.title,
            "type": n.metadata.type,
            "state": n.metadata.recommendation_state,
            "confidence": round(n.metadata.confidence, 2) if n.metadata.confidence is not None else 0.0,
            "labels": n.metadata.labels[:6],
            "created_at": n.metadata.created_at.isoformat() if n.metadata.created_at else None,
            "updated_at": n.metadata.updated_at.isoformat() if n.metadata.updated_at else None,
            "preview": (n.content.strip().split("\n\n")[0][:180] + "...") if len(n.content.strip()) > 180 else n.content.strip(),
        }
        for n in sorted_notes[:40]
    ]

    doctor_report = service.doctor()
    is_healthy = (
        doctor_report.vault == "ok"
        and doctor_report.gateway == "ok"
        and doctor_report.neo4j == "ok"
    )

    return {
        "status": "ok" if is_healthy else "degraded",
        "vault": {
            "total_notes": len(notes),
            "by_type": by_type,
            "by_state": by_state,
        },
        "components": {
            "vault": {
                "status": doctor_report.vault,
                "path": str(service.settings.vault_dir),
                "label": "Markdown Vault",
            },
            "neo4j": {
                "status": doctor_report.neo4j,
                "uri": service.settings.neo4j_uri,
                "label": "Neo4j Graph Store",
            },
            "ollama_local": {
                "status": "online" if doctor_report.gateway == "ok" else doctor_report.gateway,
                "base_url": service.settings.embedding_base_url or "default",
                "model": service.settings.embedding_model,
                "label": "Local Embeddings (Vector)",
            },
            "ollama_cloud": {
                "status": "online" if doctor_report.gateway == "ok" else doctor_report.gateway,
                "base_url": service.settings.llm_base_url,
                "model": service.settings.llm_model,
                "label": "Cognitive LLM (Inference)",
            },
        },
        "recent_notes": recent,
    }


def render_dashboard_html(service: BrainService) -> str:
    """Generate modern, dark-mode standalone HTML with real-time auto-refresh."""
    initial_data = get_dashboard_data(service)
    initial_json = json.dumps(initial_data)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Exocortex // Cognitive Dashboard</title>
  <style>
    :root {{
      --bg: #09090b;
      --card-bg: #121215;
      --card-hover: #18181b;
      --border: #27272a;
      --border-subtle: #1f1f23;
      --text: #f4f4f5;
      --text-muted: #a1a1aa;
      --text-dim: #71717a;
      --emerald: #10b981;
      --emerald-glow: rgba(16, 185, 129, 0.15);
      --indigo: #6366f1;
      --indigo-glow: rgba(99, 102, 241, 0.15);
      --amber: #f59e0b;
      --amber-glow: rgba(245, 158, 11, 0.15);
      --blue: #3b82f6;
      --purple: #8b5cf6;
      --rose: #f43f5e;
      --cyan: #06b6d4;
      --pink: #ec4899;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Inter", sans-serif;
      line-height: 1.5;
      padding: 32px 24px;
      min-height: 100vh;
    }}
    .container {{
      max-width: 1200px;
      margin: 0 auto;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 28px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .logo-badge {{
      width: 40px;
      height: 40px;
      background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      box-shadow: 0 0 20px rgba(99, 102, 241, 0.35);
    }}
    .brand-title {{
      font-size: 1.15rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: #fff;
    }}
    .brand-sub {{
      font-size: 0.8rem;
      color: var(--text-muted);
    }}
    .header-actions {{
      display: flex;
      align-items: center;
      gap: 16px;
    }}
    .pulse-badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 12px;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      background: rgba(16, 185, 129, 0.1);
      border: 1px solid rgba(16, 185, 129, 0.2);
      color: var(--emerald);
    }}
    .pulse-badge.degraded {{
      background: rgba(244, 63, 94, 0.1);
      border-color: rgba(244, 63, 94, 0.2);
      color: var(--rose);
    }}
    .dot {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 8px currentColor;
    }}
    .btn {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 6px 12px;
      border-radius: 8px;
      font-size: 0.8rem;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s ease;
    }}
    .btn:hover {{
      background: var(--card-hover);
      border-color: #3f3f46;
    }}
    .meta-time {{
      font-size: 0.75rem;
      color: var(--text-dim);
    }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
      margin-bottom: 28px;
    }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .card-label {{
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
    }}
    .card-value {{
      font-size: 2.2rem;
      font-weight: 800;
      letter-spacing: -0.03em;
      margin: 8px 0;
      color: #fff;
    }}
    .card-sub {{
      font-size: 0.75rem;
      color: var(--text-dim);
    }}
    .section-header-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 32px;
      margin-bottom: 14px;
    }}
    .section-title {{
      font-size: 0.82rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-dim);
    }}
    .view-toggles {{
      display: flex;
      background: #18181b;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 2px;
      gap: 2px;
    }}
    .view-btn {{
      background: transparent;
      border: none;
      color: var(--text-dim);
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s ease;
    }}
    .view-btn:hover {{
      color: var(--text);
    }}
    .view-btn.active {{
      background: #27272a;
      color: #fff;
      box-shadow: 0 1px 3px rgba(0,0,0,0.4);
    }}
    .services-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }}
    .service-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 16px;
    }}
    .service-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 6px;
    }}
    .service-name {{
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--text);
    }}
    .badge {{
      font-size: 0.68rem;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 6px;
    }}
    .badge-ok {{
      background: rgba(16, 185, 129, 0.1);
      color: var(--emerald);
      border: 1px solid rgba(16, 185, 129, 0.2);
    }}
    .badge-degraded {{
      background: rgba(244, 63, 94, 0.1);
      color: var(--rose);
      border: 1px solid rgba(244, 63, 94, 0.2);
    }}
    .badge-type {{
      background: rgba(99, 102, 241, 0.1);
      color: var(--indigo);
      border: 1px solid rgba(99, 102, 241, 0.2);
    }}
    .badge-state {{
      background: rgba(245, 158, 11, 0.1);
      color: var(--amber);
      border: 1px solid rgba(245, 158, 11, 0.2);
    }}
    .service-details {{
      font-size: 0.72rem;
      color: var(--text-dim);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    /* Proportional Bar / Treemap Card */
    .breakdown-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px 20px;
      margin-bottom: 24px;
    }}
    .treemap-bar {{
      display: flex;
      height: 18px;
      border-radius: 8px;
      overflow: hidden;
      background: #1c1c21;
      gap: 2px;
      margin-bottom: 14px;
    }}
    .treemap-seg {{
      height: 100%;
      cursor: pointer;
      position: relative;
      transition: opacity 0.15s ease, transform 0.15s ease;
    }}
    .treemap-seg:hover {{
      opacity: 0.85;
      transform: scaleY(1.15);
      z-index: 2;
    }}
    .chip-container {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .chip {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 12px;
      font-size: 0.78rem;
      color: var(--text-muted);
      display: inline-flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      transition: all 0.15s ease;
      user-select: none;
    }}
    .chip:hover {{
      border-color: #52525b;
      background: var(--card-hover);
      color: #fff;
    }}
    .chip.active {{
      background: rgba(99, 102, 241, 0.15);
      border-color: var(--indigo);
      color: #fff;
      box-shadow: 0 0 12px rgba(99, 102, 241, 0.25);
    }}
    .chip-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      display: inline-block;
    }}
    .chip-count {{
      background: #18181b;
      padding: 2px 6px;
      border-radius: 6px;
      font-size: 0.72rem;
      font-weight: 600;
      color: var(--text);
    }}
    /* Constellation Graph Card */
    .graph-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      position: relative;
      margin-bottom: 24px;
      overflow: hidden;
    }}
    .graph-instructions {{
      position: absolute;
      top: 14px;
      right: 18px;
      font-size: 0.72rem;
      color: var(--text-dim);
      pointer-events: none;
      background: rgba(18, 18, 21, 0.85);
      padding: 4px 8px;
      border-radius: 6px;
      border: 1px solid var(--border-subtle);
    }}
    #graph-canvas {{
      display: block;
      width: 100%;
      height: 320px;
      border-radius: 8px;
      background: radial-gradient(circle at center, #18181f 0%, #0c0c0e 100%);
      cursor: grab;
    }}
    #graph-canvas:active {{
      cursor: grabbing;
    }}
    /* Notes list & filter banner */
    .filter-banner {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: rgba(99, 102, 241, 0.1);
      border: 1px solid rgba(99, 102, 241, 0.3);
      padding: 8px 14px;
      border-radius: 8px;
      margin-bottom: 14px;
      font-size: 0.78rem;
      color: var(--text);
    }}
    .clear-btn {{
      background: none;
      border: none;
      color: var(--indigo);
      cursor: pointer;
      font-size: 0.75rem;
      font-weight: 600;
      padding: 2px 6px;
      border-radius: 4px;
    }}
    .clear-btn:hover {{
      text-decoration: underline;
    }}
    .notes-list {{
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .note-item {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px 20px;
      transition: all 0.15s ease;
    }}
    .note-item:hover {{
      border-color: #3f3f46;
      background: var(--card-hover);
    }}
    .note-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 8px;
    }}
    .note-title {{
      font-size: 1rem;
      font-weight: 600;
      color: var(--text);
    }}
    .note-meta {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .note-preview {{
      font-size: 0.82rem;
      color: var(--text-muted);
      line-height: 1.5;
      margin-top: 6px;
      font-family: inherit;
    }}
    .labels-row {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 10px;
    }}
    .label-tag {{
      font-size: 0.7rem;
      background: #18181b;
      border: 1px solid var(--border-subtle);
      color: var(--text-dim);
      padding: 2px 8px;
      border-radius: 4px;
    }}
    .time-tag {{
      font-size: 0.72rem;
      color: var(--text-dim);
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <div class="logo-badge">🧠</div>
        <div>
          <div class="brand-title">EXOCORTEX // COGNITIVE LAB</div>
          <div class="brand-sub">Antigravity Persistent Memory & Knowledge Graph</div>
        </div>
      </div>
      <div class="header-actions">
        <div id="status-badge" class="pulse-badge">
          <div class="dot"></div>
          <span id="status-text">SYSTEM OPERATIONAL</span>
        </div>
        <button class="btn" onclick="refreshData()">
          <span>↻</span> Refresh
        </button>
        <span class="meta-time" id="last-updated"></span>
      </div>
    </header>

    <!-- Top KPIs -->
    <div class="kpi-grid">
      <div class="card">
        <div class="card-label">Total Memories</div>
        <div class="card-value" id="kpi-total">-</div>
        <div class="card-sub" id="kpi-total-sub">Durable notes in Markdown Vault</div>
      </div>
      <div class="card">
        <div class="card-label">Active State</div>
        <div class="card-value" id="kpi-active" style="color: var(--emerald);">-</div>
        <div class="card-sub">Directly promoted for inference</div>
      </div>
      <div class="card">
        <div class="card-label">Vector Embeddings</div>
        <div class="card-value" style="font-size: 1.25rem; margin-top: 8px;" id="kpi-emb-model">-</div>
        <div class="card-sub" id="kpi-emb-sub">768-dim • Local Ollama</div>
      </div>
      <div class="card">
        <div class="card-label">Cognitive Reasoning</div>
        <div class="card-value" style="font-size: 1.25rem; margin-top: 8px;" id="kpi-llm-model">-</div>
        <div class="card-sub" id="kpi-llm-sub">JSON Object Mode • Ollama Cloud</div>
      </div>
    </div>

    <!-- Infrastructure Status -->
    <div class="section-title">Infrastructure & Services</div>
    <div class="services-grid" id="services-grid"></div>

    <!-- Knowledge Taxonomy & Dynamic Maps -->
    <div class="section-header-row">
      <div class="section-title">Memory Breakdown & Dynamic Maps</div>
      <div class="view-toggles">
        <button id="btn-view-bar" class="view-btn active" onclick="switchView('bar')">📊 Proportional Bar</button>
        <button id="btn-view-graph" class="view-btn" onclick="switchView('graph')">🕸️ Constellation Graph</button>
      </div>
    </div>

    <!-- View 1: Proportional Treemap Bar -->
    <div id="view-bar-container" class="breakdown-card">
      <div class="treemap-bar" id="treemap-bar" title="Click any category to filter"></div>
      <div class="chip-container" id="types-container"></div>
    </div>

    <!-- View 2: Constellation Graph Canvas -->
    <div id="view-graph-container" class="graph-card" style="display: none;">
      <div class="graph-instructions">Drag nodes to explore • Click category node to filter</div>
      <canvas id="graph-canvas"></canvas>
    </div>

    <!-- Filter Banner (Conditional) -->
    <div id="filter-banner" class="filter-banner" style="display: none;">
      <span>Filtering notes by <strong id="filter-name"></strong></span>
      <button class="clear-btn" onclick="clearFilter()">Clear filter ✕</button>
    </div>

    <!-- Recent Memories -->
    <div class="section-title" style="margin-top: 24px;">Recent Vault Activity</div>
    <div class="notes-list" id="notes-list"></div>
  </div>

  <script>
    let currentData = {initial_json};
    let activeFilter = null;
    let activeView = 'bar';

    const TYPE_COLORS = {{
      'task': '#3b82f6',        // Blue
      'pattern': '#8b5cf6',     // Violet
      'decision': '#10b981',    // Emerald
      'incident': '#f43f5e',    // Rose
      'project': '#f59e0b',     // Amber
      'system': '#06b6d4',      // Cyan
      'command': '#ec4899',     // Pink
      'repository': '#64748b',  // Slate
      'penalized': '#a1a1aa',   // Zinc
      'active': '#10b981',      // Emerald
      'quarantined': '#eab308', // Yellow
      'superseded': '#71717a'   // Zinc dark
    }};

    function getColor(key) {{
      return TYPE_COLORS[key] || '#6366f1';
    }}

    function switchView(view) {{
      activeView = view;
      const barContainer = document.getElementById('view-bar-container');
      const graphContainer = document.getElementById('view-graph-container');
      const btnBar = document.getElementById('btn-view-bar');
      const btnGraph = document.getElementById('btn-view-graph');

      if (view === 'bar') {{
        barContainer.style.display = 'block';
        graphContainer.style.display = 'none';
        btnBar.className = 'view-btn active';
        btnGraph.className = 'view-btn';
      }} else {{
        barContainer.style.display = 'none';
        graphContainer.style.display = 'block';
        btnBar.className = 'view-btn';
        btnGraph.className = 'view-btn active';
        initCanvasGraph();
      }}
    }}

    function toggleFilter(type) {{
      if (activeFilter === type) {{
        activeFilter = null;
      }} else {{
        activeFilter = type;
      }}
      renderFilterBanner();
      renderBreakdown(currentData);
      renderNotes(currentData);
      if (activeView === 'graph') {{
        drawGraph();
      }}
    }}

    function clearFilter() {{
      activeFilter = null;
      renderFilterBanner();
      renderBreakdown(currentData);
      renderNotes(currentData);
      if (activeView === 'graph') {{
        drawGraph();
      }}
    }}

    function renderFilterBanner() {{
      const banner = document.getElementById('filter-banner');
      const filterName = document.getElementById('filter-name');
      if (activeFilter) {{
        banner.style.display = 'flex';
        filterName.textContent = '#' + activeFilter;
      }} else {{
        banner.style.display = 'none';
      }}
    }}

    function renderBreakdown(data) {{
      const bar = document.getElementById('treemap-bar');
      const typesContainer = document.getElementById('types-container');
      bar.innerHTML = '';
      typesContainer.innerHTML = '';

      const total = data.vault.total_notes || 1;
      const types = data.vault.by_type || {{}};
      const states = data.vault.by_state || {{}};

      // Render Proportional Bar
      for (const [type, count] of Object.entries(types)) {{
        const pct = ((count / total) * 100).toFixed(1);
        const seg = document.createElement('div');
        seg.className = 'treemap-seg';
        seg.style.flex = count;
        seg.style.background = getColor(type);
        seg.title = `${{type}}: ${{count}} (${{pct}}%) - Click to filter`;
        if (activeFilter && activeFilter !== type) {{
          seg.style.opacity = '0.25';
        }}
        seg.onclick = () => toggleFilter(type);
        bar.appendChild(seg);
      }}

      // Render Chips for Types
      for (const [type, count] of Object.entries(types)) {{
        const chip = document.createElement('div');
        const isActive = activeFilter === type;
        chip.className = isActive ? 'chip active' : 'chip';
        const color = getColor(type);
        chip.innerHTML = `
          <span class="chip-dot" style="background: ${{color}};"></span>
          <span>${{type}}</span>
          <span class="chip-count">${{count}}</span>
        `;
        chip.onclick = () => toggleFilter(type);
        typesContainer.appendChild(chip);
      }}

      // Render Chips for States
      for (const [state, count] of Object.entries(states)) {{
        const chip = document.createElement('div');
        const isActive = activeFilter === state;
        chip.className = isActive ? 'chip active' : 'chip';
        chip.innerHTML = `
          <span>⚡ ${{state}}</span>
          <span class="chip-count">${{count}}</span>
        `;
        chip.onclick = () => toggleFilter(state);
        typesContainer.appendChild(chip);
      }}
    }}

    function renderNotes(data) {{
      const notesList = document.getElementById('notes-list');
      notesList.innerHTML = '';

      let notes = data.recent_notes || [];
      if (activeFilter) {{
        notes = notes.filter(n => n.type === activeFilter || n.state === activeFilter);
      }}

      if (notes.length === 0) {{
        const msg = activeFilter 
          ? `No recent memories found with filter: #${{activeFilter}}`
          : 'No memories recorded yet.';
        notesList.innerHTML = `<div style="color: var(--text-dim); padding: 16px;">${{msg}}</div>`;
        return;
      }}

      notes.forEach(note => {{
        const item = document.createElement('div');
        item.className = 'note-item';
        const labelsHtml = (note.labels || []).map(l => `<span class="label-tag">#${{l}}</span>`).join(' ');
        const dateStr = note.updated_at ? new Date(note.updated_at).toLocaleDateString() : '';

        item.innerHTML = `
          <div class="note-header">
            <div class="note-title">${{note.title}}</div>
            <div class="note-meta">
              <span class="badge badge-type" style="background: ${{getColor(note.type)}}18; color: ${{getColor(note.type)}}; border-color: ${{getColor(note.type)}}33;">${{note.type}}</span>
              <span class="badge badge-state">${{note.state}}</span>
              <span class="badge" style="background: rgba(255,255,255,0.05); color: var(--text-muted);">${{note.confidence}}</span>
              <span class="time-tag">${{dateStr}}</span>
            </div>
          </div>
          <div class="note-preview">${{note.preview || 'No preview available.'}}</div>
          <div class="labels-row">${{labelsHtml}}</div>
        `;
        notesList.appendChild(item);
      }});
    }}

    function render(data) {{
      currentData = data;
      const isOk = data.status === 'ok';
      const statusBadge = document.getElementById('status-badge');
      const statusText = document.getElementById('status-text');
      statusBadge.className = isOk ? 'pulse-badge' : 'pulse-badge degraded';
      statusText.textContent = isOk ? 'SYSTEM OPERATIONAL' : 'SYSTEM DEGRADED';

      const now = new Date();
      document.getElementById('last-updated').textContent = 'Synced ' + now.toLocaleTimeString();

      // KPIs
      document.getElementById('kpi-total').textContent = data.vault.total_notes;
      const activeCount = data.vault.by_state['active'] || 0;
      document.getElementById('kpi-active').textContent = activeCount;

      const emb = data.components.ollama_local;
      document.getElementById('kpi-emb-model').textContent = emb.model || 'nomic-embed-text';
      document.getElementById('kpi-emb-sub').textContent = 'Local Docker • ' + (emb.base_url || '192.168.89.32');

      const llm = data.components.ollama_cloud;
      document.getElementById('kpi-llm-model').textContent = llm.model || 'deepseek-v4-flash';
      document.getElementById('kpi-llm-sub').textContent = 'Ollama Cloud • json_object';

      // Services Grid
      const servicesGrid = document.getElementById('services-grid');
      servicesGrid.innerHTML = '';
      for (const [key, comp] of Object.entries(data.components)) {{
        const isCompOk = comp.status === 'ok' || comp.status === 'online';
        const card = document.createElement('div');
        card.className = 'service-card';
        card.innerHTML = `
          <div class="service-header">
            <div class="service-name">${{comp.label || key}}</div>
            <span class="badge ${{isCompOk ? 'badge-ok' : 'badge-degraded'}}">${{comp.status}}</span>
          </div>
          <div class="service-details">${{comp.uri || comp.base_url || comp.path || ''}}</div>
        `;
        servicesGrid.appendChild(card);
      }}

      renderBreakdown(data);
      renderNotes(data);
      if (activeView === 'graph') {{
        initCanvasGraph();
      }}
    }}

    // ==========================================
    // Interactive HTML5 Constellation Graph (Zero Dependencies)
    // ==========================================
    let canvas, ctx;
    let graphNodes = [];
    let hoveredNode = null;
    let draggedNode = null;
    let graphAnimId = null;

    function initCanvasGraph() {{
      canvas = document.getElementById('graph-canvas');
      if (!canvas) return;
      ctx = canvas.getContext('2d');

      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = rect.width * dpr;
      canvas.height = 320 * dpr;
      ctx.scale(dpr, dpr);

      const width = rect.width;
      const height = 320;
      const centerX = width / 2;
      const centerY = height / 2;

      const types = currentData.vault.by_type || {{}};
      const keys = Object.keys(types);
      const angleStep = (Math.PI * 2) / (keys.length || 1);

      graphNodes = [];
      // Central Core Node
      graphNodes.push({{
        id: 'core',
        label: 'EXOCORTEX',
        count: currentData.vault.total_notes,
        x: centerX,
        y: centerY,
        vx: 0,
        vy: 0,
        radius: 28,
        color: '#6366f1',
        isCenter: true,
        type: null
      }});

      // Category Satellite Nodes
      keys.forEach((key, idx) => {{
        const count = types[key];
        const dist = 110 + (idx % 2 === 0 ? 15 : -15);
        const angle = idx * angleStep;
        const rad = Math.max(14, Math.min(34, 12 + Math.sqrt(count) * 1.5));
        graphNodes.push({{
          id: key,
          label: key,
          count: count,
          x: centerX + Math.cos(angle) * dist,
          y: centerY + Math.sin(angle) * dist,
          targetDist: dist,
          angle: angle,
          vx: (Math.random() - 0.5) * 2,
          vy: (Math.random() - 0.5) * 2,
          radius: rad,
          color: getColor(key),
          isCenter: false,
          type: key
        }});
      }});

      setupGraphEvents(canvas, rect);
      if (!graphAnimId) {{
        animateGraph();
      }}
    }}

    function setupGraphEvents(c, rect) {{
      c.onmousemove = (e) => {{
        const b = c.getBoundingClientRect();
        const mx = e.clientX - b.left;
        const my = e.clientY - b.top;

        if (draggedNode) {{
          draggedNode.x = mx;
          draggedNode.y = my;
          return;
        }}

        hoveredNode = null;
        for (let i = graphNodes.length - 1; i >= 0; i--) {{
          const n = graphNodes[i];
          const dist = Math.hypot(n.x - mx, n.y - my);
          if (dist <= n.radius + 4) {{
            hoveredNode = n;
            c.style.cursor = 'pointer';
            return;
          }}
        }}
        c.style.cursor = 'grab';
      }};

      c.onmousedown = (e) => {{
        if (hoveredNode) {{
          draggedNode = hoveredNode;
          c.style.cursor = 'grabbing';
        }}
      }};

      window.onmouseup = () => {{
        draggedNode = null;
      }};

      c.onclick = (e) => {{
        if (hoveredNode && !hoveredNode.isCenter) {{
          toggleFilter(hoveredNode.type);
        }} else if (hoveredNode && hoveredNode.isCenter) {{
          clearFilter();
        }}
      }};
    }}

    function animateGraph() {{
      const width = canvas.width / (window.devicePixelRatio || 1);
      const height = canvas.height / (window.devicePixelRatio || 1);
      const centerNode = graphNodes[0];

      if (centerNode && !draggedNode) {{
        centerNode.x += (width / 2 - centerNode.x) * 0.1;
        centerNode.y += (height / 2 - centerNode.y) * 0.1;
      }}

      // Physics Simulation (Springs + Repulsion + Centering)
      for (let i = 1; i < graphNodes.length; i++) {{
        const node = graphNodes[i];
        if (node === draggedNode) continue;

        // Attract toward center target distance
        const dx = centerNode.x - node.x;
        const dy = centerNode.y - node.y;
        const dist = Math.hypot(dx, dy) || 1;
        const targetDist = node.targetDist || 110;
        const force = (dist - targetDist) * 0.015;
        node.vx += (dx / dist) * force;
        node.vy += (dy / dist) * force;

        // Repulsion between satellites
        for (let j = 1; j < graphNodes.length; j++) {{
          if (i === j) continue;
          const other = graphNodes[j];
          const ox = other.x - node.x;
          const oy = other.y - node.y;
          const odist = Math.hypot(ox, oy) || 1;
          const minDist = node.radius + other.radius + 15;
          if (odist < minDist) {{
            const rep = (minDist - odist) * 0.04;
            node.vx -= (ox / odist) * rep;
            node.vy -= (oy / odist) * rep;
          }}
        }}

        // Damping
        node.vx *= 0.88;
        node.vy *= 0.88;
        node.x += node.vx;
        node.y += node.vy;

        // Bounds
        node.x = Math.max(node.radius, Math.min(width - node.radius, node.x));
        node.y = Math.max(node.radius, Math.min(height - node.radius, node.y));
      }}

      drawGraph();
      graphAnimId = requestAnimationFrame(animateGraph);
    }}

    function drawGraph() {{
      if (!ctx || !canvas) return;
      const width = canvas.width / (window.devicePixelRatio || 1);
      const height = canvas.height / (window.devicePixelRatio || 1);
      ctx.clearRect(0, 0, width, height);

      const centerNode = graphNodes[0];
      if (!centerNode) return;

      // Draw tension links
      for (let i = 1; i < graphNodes.length; i++) {{
        const node = graphNodes[i];
        const isLinkedActive = !activeFilter || activeFilter === node.type;

        ctx.beginPath();
        ctx.moveTo(centerNode.x, centerNode.y);
        ctx.lineTo(node.x, node.y);
        ctx.strokeStyle = isLinkedActive ? 'rgba(99, 102, 241, 0.28)' : 'rgba(255, 255, 255, 0.04)';
        ctx.lineWidth = isLinkedActive ? 1.5 : 0.8;
        ctx.stroke();
      }}

      // Draw Nodes
      graphNodes.forEach(node => {{
        const isSelected = activeFilter === node.type;
        const isHovered = hoveredNode === node;

        // Outer Glow for Selected or Hovered
        if (isSelected || isHovered) {{
          ctx.beginPath();
          ctx.arc(node.x, node.y, node.radius + 6, 0, Math.PI * 2);
          ctx.fillStyle = node.color + '33';
          ctx.fill();
        }}

        // Main Node Body
        ctx.beginPath();
        ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
        ctx.fillStyle = node.color;
        ctx.shadowColor = node.color;
        ctx.shadowBlur = isHovered || isSelected ? 18 : 6;
        ctx.fill();
        ctx.shadowBlur = 0;

        // Inner Border
        ctx.strokeStyle = '#ffffff44';
        ctx.lineWidth = 1.5;
        ctx.stroke();

        // Node Label
        ctx.fillStyle = '#ffffff';
        ctx.font = node.isCenter ? 'bold 10px monospace' : '600 10px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        const labelText = node.isCenter ? 'CORE' : node.label;
        ctx.fillText(labelText, node.x, node.y - (node.isCenter ? 0 : 5));

        if (!node.isCenter) {{
          ctx.font = '9px monospace';
          ctx.fillStyle = '#ffffffcc';
          ctx.fillText(node.count, node.x, node.y + 7);
        }}
      }});
    }}

    async function refreshData() {{
      try {{
        const res = await fetch('/api/dashboard');
        if (res.ok) {{
          const data = await res.json();
          render(data);
        }}
      }} catch (err) {{
        console.error('Failed to sync dashboard:', err);
      }}
    }}

    // Initial render
    render(currentData);

    // Auto-refresh every 10 seconds
    setInterval(refreshData, 10000);
  </script>
</body>
</html>
"""
