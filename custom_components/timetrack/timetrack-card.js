/**
 * TimeTrack Card v2 — Custom Lovelace Card for Home Assistant
 * Full interactive dashboard with dropdowns, inline editing, batch push
 */
class TimeTrackCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._timerInterval = null;
    this._activeTab = "status";
    this._clockInExpanded = false;
    this._selectedClient = "";
    this._editingClient = null; // client name being edited
    this._createTicketExpanded = false;
  }

  static getConfigElement() {
    return document.createElement("timetrack-card-editor");
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    this._config = config;
    this.render();
  }

  set hass(hass) {
    const oldHass = this._hass;
    this._hass = hass;

    // Only re-render on state changes, not every hass update
    const entities = [
      "binary_sensor.timetrack_clocked_in",
      "sensor.timetrack_current_client",
      "sensor.timetrack_current_duration",
      "sensor.timetrack_hours_today",
      "sensor.timetrack_hours_this_week",
      "sensor.timetrack_pending_entries",
    ];

    const changed = !oldHass || entities.some(
      e => oldHass.states[e]?.state !== hass.states[e]?.state ||
           JSON.stringify(oldHass.states[e]?.attributes) !== JSON.stringify(hass.states[e]?.attributes)
    );

    if (changed) this.render();
    this._startTimer();
  }

  _startTimer() {
    if (this._timerInterval) return;
    this._timerInterval = setInterval(() => {
      const el = this.shadowRoot?.querySelector(".live-timer");
      if (el && this._hass) {
        const sensor = this._hass.states["sensor.timetrack_current_duration"];
        if (sensor) {
          const hours = parseFloat(sensor.state) || 0;
          el.textContent = this._fmtDur(hours);
        }
      }
    }, 10000);
  }

  disconnectedCallback() {
    if (this._timerInterval) {
      clearInterval(this._timerInterval);
      this._timerInterval = null;
    }
  }

  // ── Helpers ──

  _gs(id) { return this._hass?.states[id]; }

  _fmtDur(h) {
    if (!h || h <= 0) return "0m";
    const hrs = Math.floor(h);
    const min = Math.round((h - hrs) * 60);
    return hrs === 0 ? `${min}m` : `${hrs}h ${String(min).padStart(2, "0")}m`;
  }

  _fmtTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  _fmtDate(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });
  }

  _fmtDateFull(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  }

  _genMonthOptions() {
    const now = new Date();
    const opts = [];
    for (let i = 0; i < 6; i++) {
      const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, "0");
      const label = d.toLocaleDateString([], { month: "long", year: "numeric" });
      const last = new Date(y, d.getMonth() + 1, 0);
      const val = `${y}-${m}-01|${y}-${m}-${String(last.getDate()).padStart(2, "0")}`;
      opts.push(`<option value="${val}" ${i === 0 ? "selected" : ""}>${label}</option>`);
    }
    return opts.join("");
  }

  // ── Service Calls ──

  _svc(service, data = {}) {
    this._hass.callService("timetrack", service, data);
  }

  // ── Render ──

  render() {
    if (!this._hass) return;

    // User visibility guard — only show card to the person being tracked
    const pSensorForAuth = this._gs("sensor.timetrack_pending_entries");
    const personEntity = pSensorForAuth?.attributes?.person_entity || this._config.person_entity || "";
    const personState = personEntity ? this._hass.states[personEntity] : null;
    const authorizedUserId = personState?.attributes?.user_id;
    const currentUserId = this._hass.user?.id;
    if (authorizedUserId && currentUserId && currentUserId !== authorizedUserId) {
      this.shadowRoot.innerHTML = "";
      return;
    }
    const isClocked = this._gs("binary_sensor.timetrack_clocked_in")?.state === "on";
    const client = this._gs("sensor.timetrack_current_client")?.state || "";
    const durH = parseFloat(this._gs("sensor.timetrack_current_duration")?.state) || 0;
    const todayH = parseFloat(this._gs("sensor.timetrack_hours_today")?.state) || 0;
    const weekH = parseFloat(this._gs("sensor.timetrack_hours_this_week")?.state) || 0;
    const pSensor = this._gs("sensor.timetrack_pending_entries");
    const pCount = parseInt(pSensor?.state) || 0;

    const a = pSensor?.attributes || {};
    const entries = a.entries || [];
    const pendingEntries = a.pending_entries || [];
    const clients = a.clients || [];
    const customers = a.customers || [];
    const rawRates = a.rates || [];
    const seenRates = new Set();
    const rates = rawRates.filter(r => {
      if (r.rate <= 0) return false;
      if (seenRates.has(r.name)) return false;
      seenRates.add(r.name);
      return true;
    });
    const tickets = a.tickets || [];
    const aliases = a.zone_aliases || [];

    this.shadowRoot.innerHTML = `
      <style>${this._css()}</style>
      <ha-card>
        <div class="tt">
          ${this._hdr(isClocked, client, durH, customers)}
          ${this._stats(todayH, weekH, pCount)}
          ${this._tabs()}
          <div class="tc">
            ${this._activeTab === "status" ? this._tabStatus(entries) : ""}
            ${this._activeTab === "pending" ? this._tabPending(pendingEntries, clients, tickets, rates) : ""}
            ${this._activeTab === "clients" ? this._tabClients(clients, rates, customers, tickets, aliases) : ""}
          </div>
        </div>
      </ha-card>
    `;
    this._bind();
  }

  // ── Header ──

  _hdr(on, client, dur, customers) {
    return `
      <div class="hdr">
        <div class="hdr-l">
          <div class="badge ${on ? "on" : "off"}">
            <span class="dot"></span>
            <span>${on ? "CLOCKED IN" : "CLOCKED OUT"}</span>
          </div>
          ${on ? `<div class="cur-client">${client}</div>` : ""}
        </div>
        <div class="hdr-r">
          ${on ? `
            <div class="live-timer">${this._fmtDur(dur)}</div>
            <button class="btn btn-red" data-act="clock-out">Clock Out</button>
          ` : `
            <button class="btn btn-accent" data-act="toggle-clockin">⏱ Clock In</button>
          `}
        </div>
      </div>
      ${!on && this._clockInExpanded ? this._clockInPanel(customers) : ""}
    `;
  }

  _clockInPanel(customers) {
    return `
      <div class="panel clock-in-panel">
        <div class="panel-title">Clock In</div>
        <div class="form-row">
          <label>Client</label>
          <select class="sel" data-bind="clock-in-client">
            <option value="">— Select client —</option>
            ${customers.map(c => `
              <option value="${c.short}" ${this._selectedClient === c.short ? "selected" : ""}>
                ${c.short} — ${c.name}
              </option>
            `).join("")}
          </select>
        </div>
        <div class="form-actions">
          <button class="btn btn-sm btn-muted" data-act="cancel-clockin">Cancel</button>
          <button class="btn btn-sm btn-accent" data-act="do-clockin" ${!this._selectedClient ? "disabled" : ""}>
            Start Tracking
          </button>
        </div>
      </div>
    `;
  }

  // ── Stats ──

  _stats(today, week, pending) {
    return `
      <div class="stats">
        <div class="s"><div class="sv">${this._fmtDur(today)}</div><div class="sl">Today</div></div>
        <div class="s"><div class="sv">${this._fmtDur(week)}</div><div class="sl">This Week</div></div>
        <div class="s"><div class="sv ${pending > 0 ? "warn" : ""}">${pending}</div><div class="sl">Pending</div></div>
      </div>
    `;
  }

  // ── Tabs ──

  _tabs() {
    const t = [
      { id: "status", l: "Status", i: "📊" },
      { id: "pending", l: "Pending", i: "📋" },
      { id: "clients", l: "Setup", i: "⚙️" },
    ];
    return `<div class="tabs">${t.map(x => `
      <button class="tab ${this._activeTab === x.id ? "act" : ""}" data-tab="${x.id}">
        ${x.i} ${x.l}
      </button>
    `).join("")}</div>`;
  }

  // ── Tab: Status ──

  _tabStatus(entries) {
    // Date range filtering
    const range = this._statusRange || "month";
    const now = new Date();
    let start, end;

    if (range === "week") {
      const d = new Date(now); d.setDate(d.getDate() - d.getDay() + 1); // Monday
      d.setHours(0,0,0,0); start = d;
      end = new Date(now);
    } else if (range === "month") {
      start = new Date(now.getFullYear(), now.getMonth(), 1);
      end = new Date(now);
    } else if (range === "year") {
      start = new Date(now.getFullYear(), 0, 1);
      end = new Date(now);
    } else if (range === "custom" && this._statusStart && this._statusEnd) {
      start = new Date(this._statusStart + "T00:00:00");
      end = new Date(this._statusEnd + "T23:59:59");
    } else {
      start = new Date(0); end = new Date(now);
    }

    const dateFiltered = entries.filter(e => {
      const d = new Date(e.clock_in);
      return d >= start && d <= end;
    });

    // Client filter
    const clientFilter = this._statusClient || "";
    const uniqueClients = [...new Set(dateFiltered.map(e => e.client))].sort();
    const filtered = clientFilter
      ? dateFiltered.filter(e => e.client === clientFilter)
      : dateFiltered;

    const pushed = filtered.filter(e => e.push_status === "pushed");
    const pending = filtered.filter(e => e.push_status === "pending");
    const failed = filtered.filter(e => e.push_status === "failed");
    const pushedH = pushed.reduce((s, e) => s + (e.rounded_hours || 0), 0);
    const pendingH = pending.reduce((s, e) => s + (e.rounded_hours || 0), 0);
    const failedH = failed.reduce((s, e) => s + (e.rounded_hours || 0), 0);
    const totalH = pushedH + pendingH + failedH;

    const rangeBtns = [
      { id: "week", l: "This Week" },
      { id: "month", l: "This Month" },
      { id: "year", l: "This Year" },
      { id: "all", l: "All" },
      { id: "custom", l: "Custom" },
    ];

    return `
      <div class="sec">
        <div class="dr-bar">
          ${rangeBtns.map(b => `
            <button class="dr-btn ${range === b.id ? 'dr-act' : ''}" data-range="${b.id}">${b.l}</button>
          `).join("")}
        </div>
        ${range === "custom" ? `
          <div class="dr-custom">
            <input type="date" class="inp dr-inp" id="dr-start"
                   value="${this._statusStart || now.toISOString().slice(0,10)}" />
            <span class="dr-sep">→</span>
            <input type="date" class="inp dr-inp" id="dr-end"
                   value="${this._statusEnd || now.toISOString().slice(0,10)}" />
          </div>
        ` : ""}

        ${uniqueClients.length > 1 ? `
          <div class="dr-bar" style="margin-bottom: 10px;">
            <button class="dr-btn ${!clientFilter ? 'dr-act' : ''}" data-client-filter="">All Clients</button>
            ${uniqueClients.map(c => `
              <button class="dr-btn ${clientFilter === c ? 'dr-act' : ''}" data-client-filter="${c}">${c}</button>
            `).join("")}
          </div>
        ` : ""}

        <div class="st-totals">
          <div class="st-total-row">
            <span class="st-total-label">${clientFilter || "Total"}</span>
            <span class="st-total-hrs">${this._fmtDur(totalH)}</span>
          </div>
          <div class="st-badges">
            ${pushedH > 0 ? `<span class="st-badge st-badge-pushed">✅ ${this._fmtDur(pushedH)} pushed</span>` : ""}
            ${pendingH > 0 ? `<span class="st-badge st-badge-pending">⏳ ${this._fmtDur(pendingH)} pending</span>` : ""}
            ${failedH > 0 ? `<span class="st-badge st-badge-failed">❌ ${this._fmtDur(failedH)} failed</span>` : ""}
          </div>
        </div>

        ${filtered.length === 0 ? `<div class="empty">No entries in this period</div>` : ""}
        ${filtered.map(e => `
          <div class="erow">
            <div class="ei">
              <span class="ec">${e.client}</span>
              <span class="ed">${this._fmtDateFull(e.clock_in)}</span>
              ${e.ticket_number ? `<span class="e-ticket">#${e.ticket_number}</span>` : ""}
              ${!e.billable ? `<span class="e-nb">non-billable</span>` : ""}
            </div>
            <div class="et">${this._fmtTime(e.clock_in)} → ${this._fmtTime(e.clock_out)}</div>
            <div class="eh">${this._fmtDur(e.rounded_hours)}</div>
            <div class="es es-${e.push_status}">${e.push_status}</div>
          </div>
        `).join("")}

        <div class="gen-section">
          <div class="sec-t" style="margin-top:16px">Generate from History</div>
          <div class="gen-controls">
            <select class="sel" data-bind="gen-month">
              ${this._genMonthOptions()}
            </select>
            <button class="btn btn-sm btn-accent" data-act="generate-entries">⚡ Generate</button>
          </div>
          ${this._genResult ? `<div class="gen-result">${this._genResult}</div>` : ""}
        </div>
      </div>
    `;
  }

  // ── Tab: Pending ──

  _tabPending(entries, clients, tickets, rates) {
    const pending = entries.filter(e => e.push_status === "pending");
    const failed = entries.filter(e => e.push_status === "failed");
    const all = [...pending, ...failed];

    return `
      <div class="sec">
        <div class="sec-hdr">
          <div class="sec-t">Pending Entries (${all.length})</div>
          ${all.length > 0 ? `
            <button class="btn btn-sm btn-green" data-act="push-all">📤 Push All (${pending.length})</button>
          ` : ""}
        </div>
        ${all.length === 0 ? `<div class="empty">All caught up! 🎉</div>` : ""}
        ${all.map(e => this._pendingCard(e, tickets, rates)).join("")}
      </div>
    `;
  }

  _pendingCard(e, tickets, rates) {
    // Filter tickets to only show those matching this entry's client
    const clientTickets = tickets.filter(t => !t.customer || t.customer === e.client);
    const openTickets = clientTickets.filter(t => t.status === "open");
    const closedTickets = clientTickets.filter(t => t.status !== "open");
    // Determine current rate (per-entry override or client default)
    const currentRate = e.msp_rate_id || e.msp_service_item_rate_id || "";
    return `
      <div class="pcard ${e.push_status === 'failed' ? 'pcard-fail' : ''}">
        <div class="pc-top">
          <div class="pc-client-line">
            <span class="pc-client">${e.client}</span>
            <span class="pc-date">${this._fmtDateFull(e.clock_in)}</span>
          </div>
          <span class="pc-hrs">${this._fmtDur(e.rounded_hours)}</span>
        </div>
        <div class="pc-times">${this._fmtTime(e.clock_in)} → ${this._fmtTime(e.clock_out)}</div>

        <div class="pc-field">
          <label>Description</label>
          <input type="text" class="inp" value="${(e.description || "").replace(/"/g, "&quot;")}"
                 placeholder="Add description before push..."
                 data-eid="${e.id}" data-field="description" />
        </div>

        <div class="pc-field">
          <label>Ticket${e.ticket_from_default ? ' <span style="color:#66bb6a;font-size:0.8em;font-weight:normal">(from client default)</span>' : ''}</label>
          <select class="sel ticket-sel" data-eid="${e.id}">
            <option value="">— Select ticket —</option>
            ${openTickets.length > 0 ? `<optgroup label="Open Tickets">
              ${openTickets.map(t => `
                <option value="${t.id}" ${e.msp_ticket_id === t.id ? "selected" : ""}>
                  #${t.num} ${t.customer ? "[" + t.customer + "]" : ""} ${t.title}
                </option>
              `).join("")}
            </optgroup>` : ""}
            ${closedTickets.length > 0 ? `<optgroup label="Closed Tickets">
              ${closedTickets.slice(0, 15).map(t => `
                <option value="${t.id}" ${e.msp_ticket_id === t.id ? "selected" : ""}>
                  #${t.num} ${t.customer ? "[" + t.customer + "]" : ""} ${t.title}
                </option>
              `).join("")}
            </optgroup>` : ""}
          </select>
        </div>

        <div class="pc-field">
          <label>Rate</label>
          <select class="sel rate-sel" data-eid="${e.id}">
            ${rates.map(r => `
              <option value="${r.id}" ${currentRate === r.id ? "selected" : ""}>
                ${r.name} ($${r.rate})
              </option>
            `).join("")}
          </select>
        </div>

        <div class="pc-actions">
          <label class="nb-toggle" title="Toggle billable">
            <input type="checkbox" ${e.billable ? "checked" : ""}
                   data-act="toggle-billable" data-id="${e.id}" />
            <span>💰 Billable</span>
          </label>
          ${e.push_status === "failed" ? `<span class="fail-label">❌ Failed — retry?</span>` : ""}
          <button class="btn btn-sm btn-green" data-act="push-one" data-id="${e.id}"
                  ${!e.msp_ticket_id ? "disabled title='Assign a ticket first'" : ""}>
            📤 Push
          </button>
        </div>
      </div>
    `;
  }

  // ── Tab: Clients ──

  _tabClients(clients, rates, customers, tickets, aliases) {
    return `
      <div class="sec">
        <div class="sec-hdr">
          <div class="sec-t">Client Mapping</div>
          <div style="color:var(--secondary-text-color,#999);font-size:0.8em;margin-bottom:4px">Default ticket auto-applies to pending entries</div>
          <div style="display:flex;gap:6px">
            ${customers.length > 0 ? `
              <button class="btn btn-sm btn-muted" data-act="sync-tickets">🔄 Sync Tickets</button>
              <button class="btn btn-sm btn-green" data-act="toggle-create-ticket">🎫 New Ticket</button>
            ` : ""}
            <button class="btn btn-sm btn-accent" data-act="toggle-add-client">+ Add</button>
          </div>
        </div>

        ${this._createTicketExpanded ? this._createTicketPanel(customers, rates) : ""}
        ${this._addClientExpanded ? this._addClientPanel(customers, rates) : ""}

        ${clients.length === 0 ? `<div class="empty">No clients mapped yet.<br>Click "+ Add" above.</div>` : ""}
        ${clients.map(c => {
          const rate = rates.find(r => r.id === c.rate_id);
          const rateName = rate ? rate.name : "Default";
          const isEditing = this._editingClient === c.name;
          const ticketObj = tickets.find(t => t.id === c.ticket_id);
          const ticketLabel = ticketObj ? `#${ticketObj.num} ${ticketObj.title}` : c.ticket_id ? c.ticket_id.substring(0, 8) + "…" : null;

          return `
            <div class="crow ${isEditing ? 'crow-editing' : ''}">
              <div class="crow-top">
                <div class="crow-main">
                  <div class="crow-name">${c.name}</div>
                  <div class="crow-msp">${c.msp_name || "—"}</div>
                </div>
                <div class="crow-meta">
                  ${ticketLabel ?
                    `<span class="tbadge">🎫 ${ticketLabel}</span>` :
                    `<span class="tbadge tbadge-warn">⚠️ No ticket</span>`
                  }
                  <span class="crow-rate">${rateName}</span>
                  <button class="btn-icon" data-act="edit-client" data-client="${c.name}" title="Edit mapping">✏️</button>
                </div>
              </div>
              ${isEditing ? this._editClientForm(c, tickets, rates) : ""}
            </div>
          `;
        }).join("")}


        <div class="alias-section">
          <div class="sec-t" style="margin-top:16px">Zone Aliases</div>
          <div style="color:var(--secondary-text-color,#999);font-size:0.8em;margin-bottom:8px">Map HA zone states to clients for history generation</div>
          ${aliases.length === 0 ? `<div class="empty">No aliases configured</div>` : ""}
          ${aliases.map(a => `
            <div class="alias-row">
              <span class="alias-zone">${a.zone_state}</span>
              <span class="alias-arrow">→</span>
              <span class="alias-client">${a.client_name}</span>
              <button class="btn-icon" data-act="remove-alias" data-zone="${a.zone_state}" title="Remove">🗑️</button>
            </div>
          `).join("")}
          <div class="alias-add">
            <select class="sel" data-bind="alias-zone" style="flex:1">
              <option value="">— Select zone —</option>
              ${Object.keys(this._hass.states)
                .filter(e => e.startsWith("zone."))
                .map(e => {
                  const name = this._hass.states[e].attributes.friendly_name || e.replace("zone.", "");
                  return `<option value="${name}">${name}</option>`;
                }).join("")}
            </select>
            <select class="sel" data-bind="alias-client" style="flex:1">
              <option value="">— Client —</option>
              ${(clients || []).map(c => `<option value="${c.name}">${c.name}</option>`).join("")}
            </select>
            <button class="btn btn-sm btn-accent" data-act="add-alias">+ Add</button>
          </div>
        </div>
      </div>
    `;
  }

  _editClientForm(c, tickets, rates) {
    // Filter tickets to this client's customer short name
    const clientTickets = tickets.filter(t => t.customer === c.name);
    const pool = clientTickets.length > 0 ? clientTickets : tickets;
    const openTickets = pool.filter(t => t.status === "open");
    const closedTickets = pool.filter(t => t.status !== "open").slice(0, 15);
    return `
      <div class="panel edit-client-panel">
        <div class="form-row">
          <label>Ticket</label>
          <select class="sel" data-bind="edit-ticket">
            <option value="">— No ticket —</option>
            ${openTickets.length > 0 ? `<optgroup label="Open Tickets">
              ${openTickets.map(t => `
                <option value="${t.id}" ${c.ticket_id === t.id ? "selected" : ""}>
                  #${t.num} [${t.customer}] ${t.title}
                </option>
              `).join("")}
            </optgroup>` : ""}
            ${closedTickets.length > 0 ? `<optgroup label="Closed Tickets">
              ${closedTickets.map(t => `
                <option value="${t.id}" ${c.ticket_id === t.id ? "selected" : ""}>
                  #${t.num} [${t.customer}] ${t.title}
                </option>
              `).join("")}
            </optgroup>` : ""}
          </select>
        </div>

        <div class="form-row">
          <label>Rate</label>
          <select class="sel" data-bind="edit-rate">
            ${rates.map(r => `
              <option value="${r.id}" ${c.rate_id === r.id ? "selected" : ""}>
                ${r.name} ${r.default ? "(default)" : ""}
              </option>
            `).join("")}
          </select>
        </div>

        <div class="form-row">
          <label>Default Description</label>
          <input type="text" class="inp" data-bind="edit-desc" placeholder="e.g. Onsite support"
                 value="${c.default_description || ''}" />
        </div>

        <div class="form-actions">
          <button class="btn btn-sm btn-muted" data-act="cancel-edit-client">Cancel</button>
          <button class="btn btn-sm btn-accent" data-act="save-edit-client" data-client="${c.name}" data-msp="${c.msp_name || ''}">Save</button>
        </div>
      </div>
    `;
  }

  _createTicketPanel(customers, rates) {
    const now = new Date();
    const month = now.toLocaleString("en-US", { month: "long" });
    const year = now.getFullYear();
    const defaultTitle = `Monthly Onsite - ${month} ${year}`;
    return `
      <div class="panel create-ticket-panel">
        <div class="panel-title">🎫 Create New Ticket</div>

        <div class="form-row">
          <label>Customer</label>
          <select class="sel" data-bind="create-customer">
            <option value="">— Select customer —</option>
            ${customers.map(c => `
              <option value="${c.short}">
                ${c.short} — ${c.name}
              </option>
            `).join("")}
          </select>
        </div>

        <div class="form-row">
          <label>Service Item / Rate</label>
          <select class="sel" data-bind="create-rate">
            ${rates.map(r => `
              <option value="${r.id}">
                ${r.name} ($${r.rate})
              </option>
            `).join("")}
          </select>
        </div>

        <div class="form-row">
          <label>Title</label>
          <input class="inp" data-bind="create-title" value="${defaultTitle}"
                 placeholder="e.g. Monthly Onsite - March 2026" />
        </div>

        <div class="form-row">
          <label>Description (optional)</label>
          <input class="inp" data-bind="create-description" value=""
                 placeholder="e.g. Digest ticket of onsite visits" />
        </div>

        <div class="form-actions">
          <button class="btn btn-sm btn-muted" data-act="cancel-create-ticket">Cancel</button>
          <button class="btn btn-sm btn-green" data-act="do-create-ticket">Create Ticket</button>
        </div>
      </div>
    `;
  }
  _addClientPanel(customers, rates) {
    // No-API mode: simple text input for client name
    if (customers.length === 0) {
      return `
        <div class="panel add-client-panel">
          <div class="panel-title">Add Client</div>
          <div class="form-row">
            <label>Client Name</label>
            <input class="inp" data-bind="manual-client-name" placeholder="e.g. ACME Corp" />
          </div>
          <div class="form-actions">
            <button class="btn btn-sm btn-muted" data-act="cancel-add-client">Cancel</button>
            <button class="btn btn-sm btn-accent" data-act="do-add-manual-client">Add Client</button>
          </div>
        </div>
      `;
    }
    // Full mapping mode (API connected)
    const pSensor = this._gs("sensor.timetrack_pending_entries");
    const allTickets = pSensor?.attributes?.tickets || [];
    // Filter tickets by selected customer if one is chosen
    const sel = this._selectedMapCustomer || "";
    const tickets = sel
      ? allTickets.filter(t => t.customer === sel || !t.customer)
      : allTickets;
    const openTickets = tickets.filter(t => t.status === "open");
    return `
      <div class="panel add-client-panel">
        <div class="panel-title">Map Client → Ticket</div>

        <div class="form-row">
          <label>Customer</label>
          <select class="sel" data-bind="map-customer">
            <option value="">— Select customer —</option>
            ${customers.map(c => `
              <option value="${c.short}" data-name="${c.name}" ${sel === c.short ? "selected" : ""}>
                ${c.short} — ${c.name}
              </option>
            `).join("")}
          </select>
        </div>

        <div class="form-row">
          <label>Default Ticket</label>
          <select class="sel" data-bind="map-ticket">
            <option value="">— Select ticket —</option>
            ${openTickets.length > 0 ? `<optgroup label="Open Tickets">
              ${openTickets.map(t => `
                <option value="${t.id}">#${t.num} [${t.customer}] ${t.title}</option>
              `).join("")}
            </optgroup>` : ""}
            ${tickets.filter(t => t.status !== "open").slice(0, 10).map(t => `
              <option value="${t.id}">#${t.num} [${t.customer}] ${t.title}</option>
            `).join("")}
          </select>
        </div>

        <div class="form-row">
          <label>Rate</label>
          <select class="sel" data-bind="map-rate">
            ${rates.map(r => `
              <option value="${r.id}" ${r.default ? "selected" : ""}>
                ${r.name} ${r.default ? "(default)" : ""}
              </option>
            `).join("")}
          </select>
        </div>

        <div class="form-actions">
          <button class="btn btn-sm btn-muted" data-act="cancel-add-client">Cancel</button>
          <button class="btn btn-sm btn-accent" data-act="do-map-client">Map Client</button>
        </div>
      </div>
    `;
  }

  // ── Event Binding ──

  _bind() {
    const $ = s => this.shadowRoot.querySelectorAll(s);

    // Tabs
    $(".tab").forEach(t => t.addEventListener("click", () => {
      this._activeTab = t.dataset.tab;
      this.render();
    }));

    // Date range buttons
    $(".dr-btn[data-range]").forEach(b => b.addEventListener("click", () => {
      this._statusRange = b.dataset.range;
      this.render();
    }));
    // Client filter buttons
    $("[data-client-filter]").forEach(b => b.addEventListener("click", () => {
      this._statusClient = b.dataset.clientFilter || "";
      this.render();
    }));
    // Custom date inputs
    $("#dr-start").forEach(i => i.addEventListener("change", () => {
      this._statusStart = i.value;
      this.render();
    }));
    $("#dr-end").forEach(i => i.addEventListener("change", () => {
      this._statusEnd = i.value;
      this.render();
    }));

    // Clock out
    $("[data-act='clock-out']").forEach(b => b.addEventListener("click", () => {
      this._svc("clock_out");
    }));

    // Clock in toggle
    $("[data-act='toggle-clockin']").forEach(b => b.addEventListener("click", () => {
      this._clockInExpanded = !this._clockInExpanded;
      this._selectedClient = "";
      this.render();
    }));

    // Clock in - client select
    $("[data-bind='clock-in-client']").forEach(s => s.addEventListener("change", () => {
      this._selectedClient = s.value;
      this.render();
    }));

    // Clock in - submit
    $("[data-act='do-clockin']").forEach(b => b.addEventListener("click", () => {
      if (this._selectedClient) {
        this._svc("clock_in", { client: this._selectedClient });
        this._clockInExpanded = false;
        this._selectedClient = "";
      }
    }));

    // Cancel clock in
    $("[data-act='cancel-clockin']").forEach(b => b.addEventListener("click", () => {
      this._clockInExpanded = false;
      this.render();
    }));

    // Push all
    $("[data-act='push-all']").forEach(b => b.addEventListener("click", () => {
      this._svc("push_entries", {});
    }));

    // Push single
    $("[data-act='push-one']").forEach(b => b.addEventListener("click", () => {
      this._svc("push_entries", { entry_ids: [parseInt(b.dataset.id)] });
    }));

    // Description editing (blur = save)
    $(".inp[data-field='description']").forEach(inp => {
      inp.addEventListener("blur", () => {
        this._svc("edit_entry", {
          entry_id: parseInt(inp.dataset.eid),
          description: inp.value,
        });
      });
      inp.addEventListener("keypress", e => { if (e.key === "Enter") inp.blur(); });
    });

    // Ticket dropdown change on pending entries
    $(".ticket-sel").forEach(sel => {
      sel.addEventListener("change", () => {
        this._svc("edit_entry", {
          entry_id: parseInt(sel.dataset.eid),
          ticket_id: sel.value,
        });
      });
    });

    // Rate dropdown change on pending entries
    $(".rate-sel").forEach(sel => {
      sel.addEventListener("change", () => {
        this._svc("edit_entry", {
          entry_id: parseInt(sel.dataset.eid),
          rate_id: sel.value,
        });
      });
    });

    // Sync tickets
    $("[data-act='sync-tickets']").forEach(b => b.addEventListener("click", () => {
      this._svc("sync_tickets");
    }));

    // Create ticket toggle
    $("[data-act='toggle-create-ticket']").forEach(b => b.addEventListener("click", () => {
      this._createTicketExpanded = !this._createTicketExpanded;
      this.render();
    }));

    // Cancel create ticket
    $("[data-act='cancel-create-ticket']").forEach(b => b.addEventListener("click", () => {
      this._createTicketExpanded = false;
      this.render();
    }));

    // Submit create ticket
    $("[data-act='do-create-ticket']").forEach(b => b.addEventListener("click", () => {
      const customer = this.shadowRoot.querySelector("[data-bind='create-customer']")?.value;
      const title = this.shadowRoot.querySelector("[data-bind='create-title']")?.value;
      const description = this.shadowRoot.querySelector("[data-bind='create-description']")?.value;
      const rateId = this.shadowRoot.querySelector("[data-bind='create-rate']")?.value;

      if (!customer) { alert("Select a customer"); return; }
      if (!title) { alert("Enter a ticket title"); return; }

      const data = { customer, title, description: description || "" };
      if (rateId) data.service_item_rate_id = rateId;
      this._svc("create_ticket", data);
      this._createTicketExpanded = false;

      // Auto-sync after a short delay to pick up the new ticket
      setTimeout(() => this._svc("sync_tickets"), 2000);
      this.render();
    }));

    // Edit client toggle
    $("[data-act='edit-client']").forEach(b => b.addEventListener("click", () => {
      const name = b.dataset.client;
      this._editingClient = this._editingClient === name ? null : name;
      this.render();
    }));

    // Cancel edit client
    $("[data-act='cancel-edit-client']").forEach(b => b.addEventListener("click", () => {
      this._editingClient = null;
      this.render();
    }));

    // Save edit client
    $("[data-act='save-edit-client']").forEach(b => b.addEventListener("click", () => {
      const client = b.dataset.client;
      const mspName = b.dataset.msp;
      const ticket = this.shadowRoot.querySelector("[data-bind='edit-ticket']")?.value;
      const rate = this.shadowRoot.querySelector("[data-bind='edit-rate']")?.value;
      const desc = this.shadowRoot.querySelector("[data-bind='edit-desc']")?.value;

      this._svc("map_client", {
        client: client,
        ticket_id: ticket || "",
        service_item_rate_id: rate || "",
        msp_client_name: mspName,
        default_description: desc || "",
      });
      this._editingClient = null;
      this.render();
    }));

    // Add client toggle
    $("[data-act='toggle-add-client']").forEach(b => b.addEventListener("click", () => {
      this._addClientExpanded = !this._addClientExpanded;
      this._selectedMapCustomer = "";
      this.render();
    }));

    // Map client — customer dropdown filters tickets
    $("[data-bind='map-customer']").forEach(s => s.addEventListener("change", () => {
      this._selectedMapCustomer = s.value;
      this.render();
    }));

    // Cancel add client
    $("[data-act='cancel-add-client']").forEach(b => b.addEventListener("click", () => {
      this._addClientExpanded = false;
      this._selectedMapCustomer = "";
      this.render();
    }));

    // Map client submit (API mode)
    $("[data-act='do-map-client']").forEach(b => b.addEventListener("click", () => {
      const customer = this.shadowRoot.querySelector("[data-bind='map-customer']")?.value;
      const ticket = this.shadowRoot.querySelector("[data-bind='map-ticket']")?.value;
      const rate = this.shadowRoot.querySelector("[data-bind='map-rate']")?.value;
      const opt = this.shadowRoot.querySelector("[data-bind='map-customer'] option:checked");
      const mspName = opt?.dataset?.name || "";

      if (!customer) { alert("Select a customer"); return; }

      this._svc("map_client", {
        client: customer,
        ticket_id: ticket || "",
        service_item_rate_id: rate || "",
        msp_client_name: mspName,
      });
      this._addClientExpanded = false;
      this.render();
    }));

    // Add manual client (no-API mode)
    $("[data-act='do-add-manual-client']").forEach(b => b.addEventListener("click", () => {
      const name = this.shadowRoot.querySelector("[data-bind='manual-client-name']")?.value?.trim();
      if (!name) { alert("Enter a client name"); return; }
      this._svc("map_client", { client: name });
      this._addClientExpanded = false;
      this.render();
    }));

    // Generate entries from history
    $("[data-act='generate-entries']").forEach(b => b.addEventListener("click", () => {
      const sel = this.shadowRoot.querySelector("[data-bind='gen-month']");
      if (!sel || !sel.value) return;
      const [start_date, end_date] = sel.value.split("|");
      this._genResult = "⏳ Generating...";
      this.render();
      this._svc("generate_entries", { start_date, end_date });
      // Listen for result event
      const unsub = this._hass.connection.subscribeEvents((ev) => {
        const d = ev.data;
        this._genResult = `✅ Generated ${d.generated} entries (${d.skipped} skipped)`;
        this.render();
        unsub.then(u => u());
      }, "timetrack_entries_generated");
    }));

    // Billable toggle
    $("[data-act='toggle-billable']").forEach(cb => cb.addEventListener("change", () => {
      this._svc("edit_entry", {
        entry_id: parseInt(cb.dataset.id),
        billable: cb.checked,
      });
    }));

    // Add zone alias
    $("[data-act='add-alias']").forEach(b => b.addEventListener("click", () => {
      const zone = this.shadowRoot.querySelector("[data-bind='alias-zone']")?.value?.trim();
      const client = this.shadowRoot.querySelector("[data-bind='alias-client']")?.value;
      if (!zone || !client) { alert("Enter zone state and select client"); return; }
      this._svc("add_zone_alias", { zone_state: zone, client_name: client });
      setTimeout(() => this.render(), 500);
    }));

    // Remove zone alias
    $("[data-act='remove-alias']").forEach(b => b.addEventListener("click", () => {
      this._svc("remove_zone_alias", { zone_state: b.dataset.zone });
      setTimeout(() => this.render(), 500);
    }));
  }

  // ── Styles ──

  _css() {
    return `
      :host {
        --bg: var(--ha-card-background, var(--card-background-color, #1c1c1e));
        --sf: rgba(255,255,255,0.05);
        --sfh: rgba(255,255,255,0.08);
        --bd: rgba(255,255,255,0.08);
        --tx: var(--primary-text-color, #e0e0e0);
        --txd: var(--secondary-text-color, #999);
        --ac: #4fc3f7;
        --gn: #66bb6a;
        --rd: #ef5350;
        --or: #ffa726;
        --pr: #ab47bc;
      }
      ha-card { background: var(--bg); border: 1px solid var(--bd); overflow: hidden; }
      .tt { padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }

      /* Header */
      .hdr { display: flex; justify-content: space-between; align-items: center;
             padding: 20px 24px;
             background: linear-gradient(135deg, rgba(79,195,247,0.08), rgba(171,71,188,0.06));
             border-bottom: 1px solid var(--bd); }
      .hdr-l { display: flex; flex-direction: column; gap: 6px; }
      .hdr-r { display: flex; align-items: center; gap: 16px; }

      .badge { display: inline-flex; align-items: center; gap: 8px; padding: 4px 12px;
               border-radius: 20px; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; }
      .on { background: rgba(102,187,106,0.15); color: var(--gn); border: 1px solid rgba(102,187,106,0.3); }
      .off { background: rgba(239,83,80,0.12); color: var(--rd); border: 1px solid rgba(239,83,80,0.25); }
      .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
      .on .dot { animation: pulse 2s ease-in-out infinite; }
      @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

      .cur-client { font-size: 20px; font-weight: 600; color: var(--tx); padding-left: 4px; }
      .live-timer { font-size: 28px; font-weight: 300; color: var(--ac);
                    font-variant-numeric: tabular-nums; letter-spacing: 1px; }

      /* Stats */
      .stats { display: flex; justify-content: space-around; padding: 16px 24px;
               border-bottom: 1px solid var(--bd); }
      .s { text-align: center; }
      .sv { font-size: 18px; font-weight: 600; color: var(--tx); font-variant-numeric: tabular-nums; }
      .sl { font-size: 11px; color: var(--txd); text-transform: uppercase; letter-spacing: 1px; margin-top: 2px; }
      .warn { color: var(--or); }

      /* Tabs */
      .tabs { display: flex; border-bottom: 1px solid var(--bd); padding: 0 12px; }
      .tab { flex: 1; padding: 12px; background: none; border: none;
             border-bottom: 2px solid transparent; color: var(--txd);
             font-size: 13px; font-weight: 500; cursor: pointer;
             transition: all 0.2s; font-family: inherit; }
      .tab:hover { color: var(--tx); background: var(--sf); }
      .tab.act { color: var(--ac); border-bottom-color: var(--ac); }

      /* Content */
      .tc { padding: 16px; max-height: 520px; overflow-y: auto; }
      .sec-hdr { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
      .sec-t { font-size: 13px; font-weight: 600; color: var(--txd);
               text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
      .sec-hdr .sec-t { margin-bottom: 0; }
      .empty { text-align: center; padding: 32px; color: var(--txd); font-size: 14px; line-height: 1.6; }

      /* Entry Rows (status tab) */
      .erow { display: grid; grid-template-columns: 1.2fr 1fr auto auto;
              align-items: center; gap: 12px; padding: 10px 12px;
              border-radius: 8px; background: var(--sf); margin-bottom: 6px;
              transition: background 0.2s; }
      .erow:hover { background: var(--sfh); }
      .ec { font-weight: 600; color: var(--tx); }
      .ed { color: var(--txd); font-size: 12px; margin-left: 8px; }
      .et { color: var(--txd); font-size: 13px; }
      .eh { font-weight: 600; color: var(--ac); font-variant-numeric: tabular-nums; }
      .es { font-size: 11px; padding: 2px 8px; border-radius: 10px;
            text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; }
      .es-pending { background: rgba(255,167,38,0.15); color: var(--or); }
      .es-pushed { background: rgba(102,187,106,0.15); color: var(--gn); }
      .es-failed { background: rgba(239,83,80,0.15); color: var(--rd); }
      .e-ticket { font-size: 11px; color: var(--ac); opacity: 0.7;
                  margin-left: 6px; font-weight: 500; }

      /* Date Range Picker */
      .dr-bar { display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }
      .dr-btn { padding: 5px 10px; border: 1px solid var(--bd); border-radius: 6px;
                background: none; color: var(--txd); font-size: 12px; font-weight: 500;
                cursor: pointer; transition: all 0.2s; font-family: inherit; }
      .dr-btn:hover { border-color: var(--ac); color: var(--tx); }
      .dr-act { background: rgba(79,195,247,0.12); border-color: var(--ac);
                color: var(--ac); font-weight: 600; }
      .dr-custom { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
      .dr-inp { width: auto; flex: 1; font-size: 12px; padding: 6px 8px; }
      .dr-sep { color: var(--txd); font-size: 14px; }

      /* Status Totals */
      .st-totals { background: var(--sf); border-radius: 10px; padding: 12px 16px;
                   margin-bottom: 14px; border: 1px solid var(--bd); }
      .st-total-row { display: flex; justify-content: space-between; align-items: center;
                      margin-bottom: 6px; }
      .st-total-label { font-size: 13px; font-weight: 600; color: var(--txd);
                        text-transform: uppercase; letter-spacing: 0.5px; }
      .st-total-hrs { font-size: 18px; font-weight: 700; color: var(--tx); }
      .st-badges { display: flex; gap: 8px; flex-wrap: wrap; }
      .st-badge { font-size: 11px; padding: 3px 8px; border-radius: 8px; font-weight: 600; }
      .st-badge-pushed { background: rgba(102,187,106,0.12); color: var(--gn); }
      .st-badge-pending { background: rgba(255,167,38,0.12); color: var(--or); }
      .st-badge-failed { background: rgba(239,83,80,0.12); color: var(--rd); }

      /* Pending Cards */
      .pcard { background: var(--sf); border-radius: 10px; padding: 14px 16px;
               margin-bottom: 10px; border: 1px solid var(--bd); transition: border-color 0.2s; }
      .pcard:hover { border-color: rgba(79,195,247,0.3); }
      .pcard-fail { border-color: rgba(239,83,80,0.3); }
      .pc-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
      .pc-client-line { display: flex; align-items: baseline; gap: 10px; }
      .pc-client { font-weight: 700; font-size: 15px; color: var(--tx); }
      .pc-date { color: var(--txd); font-size: 12px; }
      .pc-hrs { font-weight: 600; color: var(--ac); font-size: 15px; }
      .pc-times { color: var(--txd); font-size: 13px; margin-bottom: 10px; }
      .pc-field { margin-bottom: 8px; }
      .pc-field label { display: block; font-size: 11px; color: var(--txd);
                        text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
      .pc-ticket-row { display: flex; align-items: center; gap: 8px; }
      .pc-actions { display: flex; gap: 8px; justify-content: flex-end; align-items: center; margin-top: 4px; }
      .fail-label { font-size: 12px; color: var(--rd); margin-right: auto; }

      /* Ticket Badge */
      .tbadge { display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px;
                border-radius: 12px; font-size: 11px;
                background: rgba(79,195,247,0.12); color: var(--ac); font-family: monospace; }
      .tbadge-warn { background: rgba(255,167,38,0.12); color: var(--or); font-family: inherit; }

      /* Client Rows */
      .crow { border-radius: 8px; background: var(--sf); margin-bottom: 6px;
              border: 1px solid transparent; transition: border-color 0.2s; }
      .crow-editing { border-color: rgba(79,195,247,0.3); }
      .crow-top { display: flex; justify-content: space-between; align-items: center; padding: 12px; }
      .crow-main { flex: 1; min-width: 0; }
      .crow-name { font-weight: 700; color: var(--tx); }
      .crow-msp { color: var(--txd); font-size: 12px; white-space: nowrap; overflow: hidden;
                  text-overflow: ellipsis; }
      .crow-meta { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
      .crow-rate { font-size: 11px; color: var(--pr); font-weight: 500; }
      .btn-icon { background: none; border: none; cursor: pointer; padding: 2px 4px;
                  font-size: 14px; opacity: 0.5; transition: opacity 0.2s; }
      .btn-icon:hover { opacity: 1; }
      .edit-client-panel { margin: 0 12px 12px; border-radius: 0 0 8px 8px; }

      /* Rates */
      .rates-section { border-top: 1px solid var(--bd); padding-top: 12px; margin-top: 8px; }
      .rate-row { display: flex; align-items: center; gap: 12px; padding: 8px 12px;
                  border-radius: 6px; margin-bottom: 4px; font-size: 13px; }
      .rate-default { background: rgba(79,195,247,0.06); }
      .rate-name { flex: 1; color: var(--tx); }
      .rate-val { font-weight: 600; color: var(--ac); font-variant-numeric: tabular-nums; }
      .rate-tag { font-size: 10px; padding: 2px 6px; border-radius: 4px;
                  background: rgba(79,195,247,0.15); color: var(--ac);
                  font-weight: 700; letter-spacing: 0.5px; }

      /* Panels (clock-in, add-client) */
      .panel { background: rgba(79,195,247,0.04); border: 1px solid rgba(79,195,247,0.15);
               border-radius: 10px; padding: 16px; margin: 0 16px 0; }
      .clock-in-panel { margin: 0; border-radius: 0; border-left: none; border-right: none;
                        border-bottom: 1px solid var(--bd); }
      .add-client-panel { margin-bottom: 16px; }
      .panel-title { font-size: 14px; font-weight: 600; color: var(--ac); margin-bottom: 12px; }

      .form-row { margin-bottom: 12px; }
      .form-row label { display: block; font-size: 11px; color: var(--txd);
                        text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
      .form-actions { display: flex; gap: 8px; justify-content: flex-end; }

      /* Inputs */
      .inp { width: 100%; background: rgba(255,255,255,0.04); border: 1px solid var(--bd);
             border-radius: 6px; padding: 8px 10px; color: var(--tx);
             font-size: 13px; font-family: inherit; transition: border-color 0.2s;
             box-sizing: border-box; }
      .inp:focus { outline: none; border-color: var(--ac); }
      .inp::placeholder { color: var(--txd); }

      .sel { width: 100%; background: rgba(255,255,255,0.04); border: 1px solid var(--bd);
             border-radius: 6px; padding: 8px 10px; color: var(--tx);
             font-size: 13px; font-family: inherit; cursor: pointer;
             appearance: none; -webkit-appearance: none;
             background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M3 5l3 3 3-3' stroke='%23999' fill='none' stroke-width='1.5'/%3E%3C/svg%3E");
             background-repeat: no-repeat; background-position: right 10px center; }
      .sel:focus { outline: none; border-color: var(--ac); }
      .sel option { color: var(--tx); }

      /* Buttons */
      .btn { padding: 8px 16px; border: none; border-radius: 8px;
             font-size: 13px; font-weight: 600; cursor: pointer;
             transition: all 0.2s; font-family: inherit;
             white-space: nowrap; }
      .btn:disabled { opacity: 0.4; cursor: not-allowed; }
      .btn-sm { padding: 5px 12px; font-size: 12px; border-radius: 6px; }
      .btn-accent { background: var(--ac); color: #000; }
      .btn-accent:hover:not(:disabled) { background: #81d4fa; transform: translateY(-1px); }
      .btn-red { background: rgba(239,83,80,0.15); color: var(--rd);
                 border: 1px solid rgba(239,83,80,0.3); }
      .btn-red:hover { background: rgba(239,83,80,0.25); }
      .btn-green { background: rgba(102,187,106,0.15); color: var(--gn);
                   border: 1px solid rgba(102,187,106,0.3); }
      .btn-green:hover:not(:disabled) { background: rgba(102,187,106,0.25); transform: translateY(-1px); }
      .btn-muted { background: var(--sf); color: var(--txd); }
      .btn-muted:hover { background: var(--sfh); color: var(--tx); }

      /* Scrollbar */
      .tc::-webkit-scrollbar { width: 4px; }
      .tc::-webkit-scrollbar-track { background: transparent; }
      .tc::-webkit-scrollbar-thumb { background: var(--bd); border-radius: 4px; }

      /* Mobile */
      @media (max-width: 500px) {
        .hdr { flex-direction: column; gap: 12px; align-items: flex-start; }
        .hdr-r { width: 100%; justify-content: space-between; }
        .erow { grid-template-columns: 1fr auto; gap: 8px; }
        .ei { grid-column: 1 / -1; }
        .stats { padding: 12px 16px; }
        .sv { font-size: 16px; }
        .crow { flex-direction: column; align-items: flex-start; gap: 8px; }
        .crow-meta { flex-direction: row; align-items: center; }
      }

      /* Generate section */
      .gen-section { border-top: 1px solid var(--bd); padding-top: 12px; margin-top: 8px; }
      .gen-controls { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
      .gen-result { margin-top: 8px; padding: 8px 12px; background: rgba(102,187,106,0.1);
                    border-radius: 8px; font-size: 0.85em; color: #66bb6a; }

      /* Zone alias section */
      .alias-section { border-top: 1px solid var(--bd); padding-top: 12px; }
      .alias-row { display: flex; align-items: center; gap: 8px; padding: 6px 0;
                   border-bottom: 1px solid var(--bd); font-size: 0.9em; }
      .alias-zone { font-family: monospace; color: var(--ac); flex: 1; }
      .alias-arrow { color: var(--ts); }
      .alias-client { font-weight: 600; }
      .alias-add { display: flex; gap: 8px; align-items: center; margin-top: 8px; }

      /* Non-billable toggle */
      .nb-toggle { display: flex; align-items: center; gap: 4px; font-size: 0.85em;
                   cursor: pointer; color: var(--ts); margin-right: auto; }
      .nb-toggle input { accent-color: var(--ac); }
      .e-nb { font-size: 0.7em; color: #ef5350; background: rgba(239,83,80,0.15);
              padding: 1px 6px; border-radius: 4px; }
    `;
  }

  getCardSize() { return 6; }
}

customElements.define("timetrack-card", TimeTrackCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "timetrack-card",
  name: "TimeTrack",
  description: "Time tracking dashboard with MSP Manager integration",
  preview: true,
});
