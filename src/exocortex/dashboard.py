# ruff: noqa: E501
"""Ultra-lightweight embedded dashboard for Exocortex."""

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
        for n in sorted_notes[:10]
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
      --blue: #0ea5e9;
      --rose: #f43f5e;
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
      background: linear-gradient(135deg, #4f46e5, #06b6d4);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
    }}
    .brand-title {{
      font-size: 1.25rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }}
    .brand-sub {{
      font-size: 0.8rem;
      color: var(--text-dim);
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }}
    .header-actions {{
      display: flex;
      align-items: center;
      gap: 16px;
    }}
    .pulse-badge {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      background: rgba(16, 185, 129, 0.08);
      border: 1px solid rgba(16, 185, 129, 0.25);
      border-radius: 9999px;
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--emerald);
    }}
    .pulse-badge.degraded {{
      background: rgba(245, 158, 11, 0.08);
      border-color: rgba(245, 158, 11, 0.25);
      color: var(--amber);
    }}
    .dot {{
      width: 8px;
      height: 8px;
      background: var(--emerald);
      border-radius: 50%;
      box-shadow: 0 0 8px var(--emerald);
      animation: pulse 2s infinite ease-in-out;
    }}
    .pulse-badge.degraded .dot {{
      background: var(--amber);
      box-shadow: 0 0 8px var(--amber);
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; transform: scale(1); }}
      50% {{ opacity: 0.4; transform: scale(0.85); }}
    }}
    .btn {{
      background: var(--card-bg);
      color: var(--text);
      border: 1px solid var(--border);
      padding: 7px 14px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 0.82rem;
      font-weight: 500;
      transition: all 0.15s ease;
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .btn:hover {{
      background: var(--card-hover);
      border-color: var(--text-dim);
    }}
    .btn:active {{
      transform: scale(0.98);
    }}
    .meta-time {{
      font-size: 0.75rem;
      color: var(--text-dim);
    }}

    /* Stat Cards */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }}
    .card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      transition: border-color 0.15s ease;
    }}
    .card:hover {{
      border-color: #3f3f46;
    }}
    .card-label {{
      font-size: 0.78rem;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 8px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .card-value {{
      font-size: 2rem;
      font-weight: 700;
      letter-spacing: -0.03em;
      color: var(--text);
    }}
    .card-sub {{
      font-size: 0.8rem;
      color: var(--text-dim);
      margin-top: 6px;
      display: flex;
      align-items: center;
      gap: 6px;
    }}

    /* Components / Services */
    .section-title {{
      font-size: 0.95rem;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin: 32px 0 16px 0;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .services-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }}
    .service-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 12px;
    }}
    .service-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .service-name {{
      font-weight: 600;
      font-size: 0.9rem;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 0.72rem;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 6px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .badge-ok {{
      background: var(--emerald-glow);
      color: var(--emerald);
      border: 1px solid rgba(16, 185, 129, 0.3);
    }}
    .badge-degraded {{
      background: var(--amber-glow);
      color: var(--amber);
      border: 1px solid rgba(245, 158, 11, 0.3);
    }}
    .badge-type {{
      background: rgba(99, 102, 241, 0.12);
      color: #818cf8;
      border: 1px solid rgba(99, 102, 241, 0.25);
    }}
    .badge-state {{
      background: rgba(14, 165, 233, 0.12);
      color: #38bdf8;
      border: 1px solid rgba(14, 165, 233, 0.25);
    }}
    .service-details {{
      font-family: ui-monospace, monospace;
      font-size: 0.75rem;
      color: var(--text-dim);
      background: #0d0d10;
      padding: 8px 10px;
      border-radius: 6px;
      border: 1px solid var(--border-subtle);
      overflow-x: auto;
      white-space: nowrap;
    }}

    /* Tags / Chips */
    .chip-container {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 24px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--card-bg);
      border: 1px solid var(--border);
      padding: 6px 12px;
      border-radius: 8px;
      font-size: 0.8rem;
    }}
    .chip-count {{
      background: var(--border);
      color: var(--text);
      padding: 1px 6px;
      border-radius: 4px;
      font-weight: 600;
      font-size: 0.75rem;
    }}

    /* Notes List */
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

    <!-- Knowledge Taxonomy -->
    <div class="section-title">Memory Breakdown</div>
    <div class="chip-container" id="types-container"></div>

    <!-- Recent Memories -->
    <div class="section-title">Recent Vault Activity</div>
    <div class="notes-list" id="notes-list"></div>
  </div>

  <script>
    let currentData = {initial_json};

    function render(data) {{
      // Update status
      const isOk = data.status === 'ok';
      const statusBadge = document.getElementById('status-badge');
      const statusText = document.getElementById('status-text');
      statusBadge.className = isOk ? 'pulse-badge' : 'pulse-badge degraded';
      statusText.textContent = isOk ? 'SYSTEM OPERATIONAL' : 'SYSTEM DEGRADED';

      // Last updated
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

      // Types & States chips
      const typesContainer = document.getElementById('types-container');
      typesContainer.innerHTML = '';
      for (const [type, count] of Object.entries(data.vault.by_type)) {{
        const chip = document.createElement('div');
        chip.className = 'chip';
        chip.innerHTML = `<span>📂 ${{type}}</span><span class="chip-count">${{count}}</span>`;
        typesContainer.appendChild(chip);
      }}
      for (const [state, count] of Object.entries(data.vault.by_state)) {{
        const chip = document.createElement('div');
        chip.className = 'chip';
        chip.innerHTML = `<span>⚡ ${{state}}</span><span class="chip-count">${{count}}</span>`;
        typesContainer.appendChild(chip);
      }}

      // Recent Notes
      const notesList = document.getElementById('notes-list');
      notesList.innerHTML = '';
      if (!data.recent_notes || data.recent_notes.length === 0) {{
        notesList.innerHTML = '<div style="color: var(--text-dim); padding: 16px;">No memories recorded yet.</div>';
      }} else {{
        data.recent_notes.forEach(note => {{
          const item = document.createElement('div');
          item.className = 'note-item';
          const labelsHtml = (note.labels || []).map(l => `<span class="label-tag">#${{l}}</span>`).join(' ');
          const dateStr = note.updated_at ? new Date(note.updated_at).toLocaleDateString() : '';

          item.innerHTML = `
            <div class="note-header">
              <div class="note-title">${{note.title}}</div>
              <div class="note-meta">
                <span class="badge badge-type">${{note.type}}</span>
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
