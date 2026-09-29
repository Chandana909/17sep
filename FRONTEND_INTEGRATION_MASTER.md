# ASAS Frontend Integration Master Document

**Status:** Industry-grade production-ready integration for ASAS v2.1  
**Target Audience:** Coding agent (GitHub Copilot Enterprise, Claude Code, etc.)  
**Purpose:** Complete checklist and implementation guide for CSV upload, data analysis, and full frontend functionality  

---

## TABLE OF CONTENTS

1. [Architecture Overview](#1-architecture-overview)
2. [Design System & Color Palette](#2-design-system--color-palette)
3. [CSV Upload Flow](#3-csv-upload-flow)
4. [Frontend Structure](#4-frontend-structure)
5. [API Integration Points](#5-api-integration-points)
6. [Component Specifications](#6-component-specifications)
7. [Testing Strategy](#7-testing-strategy)
8. [Optimization & Performance](#8-optimization--performance)
9. [Error Handling & Validation](#9-error-handling--validation)
10. [Master Implementation Checklist](#10-master-implementation-checklist)
11. [Code Templates & Snippets](#11-code-templates--snippets)
12. [Debugging & Troubleshooting](#12-debugging--troubleshooting)

---

## 1. ARCHITECTURE OVERVIEW

### 1.1 Current State

```
┌─────────────────────────────────────────────────────────┐
│                   Browser (Static HTML)                 │
│  index.html → app.js (31KB) → styles.css (8KB)         │
│                                                         │
│  Tabs: Overview, Cases, Links, Challenge, Evolution,   │
│        Policy, Data, Audit                             │
└─────────────────────────────────────────────────────────┘
              ↓ (JSON API calls)
┌─────────────────────────────────────────────────────────┐
│                  FastAPI Backend (app.py)              │
│  • /api/overview       → run status, KPIs, drift       │
│  • /api/cases          → case list, filters            │
│  • /api/case/{id}      → single case detail            │
│  • /api/data/upload    → [BROKEN/MISSING]             │
│  • /api/data/analyze   → [MISSING]                     │
│  • /api/audit          → hash chain                    │
│  • POST /api/whoami    → identity                      │
└─────────────────────────────────────────────────────────┘
              ↓ (Point-in-time snapshots)
┌─────────────────────────────────────────────────────────┐
│          Platform / Store (SQLite or PostgreSQL)       │
│  • Trade events, alerts, outcomes, episodes            │
│  • Audit chain, run history, governance                │
└─────────────────────────────────────────────────────────┘
```

### 1.2 Missing Pieces

- ❌ CSV upload endpoint (`/api/data/upload`)
- ❌ Data validation & mapping UI (`/view-data` is display-only)
- ❌ CSV parsing on frontend or backend
- ❌ Analysis results rendering (after upload, before run)
- ❌ Layered detail expansion (click → see more)
- ❌ Progress indicators for upload/processing
- ❌ Error reporting for mapping failures

### 1.3 New Architecture (to be built)

```
┌─ Frontend ──────────────────────────────────────┐
│  1. Data tab → File input (CSV/Excel/Parquet)  │
│  2. Drag-drop zone with progress bar            │
│  3. Preview table (first 50 rows)               │
│  4. Column mapping UI (data type selector)      │
│  5. Coverage report (expandable)                │
│  6. Run capabilities (expandable)               │
│  7. Error list (collapsible detail)             │
└─────────────────────────────────────────────────┘
     ↓
┌─ API Endpoints ─────────────────────────────────┐
│  POST /api/data/upload                          │
│    ├─ Parse CSV/Excel/Parquet                   │
│    ├─ Infer schema                              │
│    └─ Return mapping preview                    │
│                                                 │
│  POST /api/data/validate                        │
│    ├─ Apply mapping                             │
│    ├─ Check contract                            │
│    └─ Return capability matrix                  │
│                                                 │
│  POST /api/data/confirm                         │
│    ├─ Store validated data                      │
│    ├─ Freeze baseline                           │
│    └─ Ready for run                             │
└─────────────────────────────────────────────────┘
```

---

## 2. DESIGN SYSTEM & COLOR PALETTE

### 2.1 Color Definitions

**CSS Variables** (add to `:root` in `styles.css`):

```css
:root {
  /* Primary palette */
  --color-red: #cc0000;        /* anomaly, danger, alert */
  --color-black: #000000;      /* text, primary ui */
  --color-grey: #666666;       /* secondary text, disabled */
  --color-white: #ffffff;      /* background, paper */
  --color-grey-light: #f5f5f5; /* subtle background */
  --color-grey-dark: #333333;  /* borders, strong grey */

  /* Semantic use */
  --color-ok: #000000;         /* benign, clean */
  --color-warn: #cc0000;       /* deviation, flag */
  --color-error: #cc0000;      /* error, failed */
  --color-text: #000000;       /* primary text */
  --color-text-muted: #666666; /* secondary text */
  --color-border: #cccccc;     /* dividers, borders */
  --color-bg: #ffffff;         /* main background */
  --color-bg-alt: #f5f5f5;     /* alternate background */
}

@media (prefers-color-scheme: dark) {
  :root {
    --color-white: #1a1a1a;
    --color-grey-light: #2a2a2a;
    --color-grey-dark: #cccccc;
    --color-text: #ffffff;
    --color-text-muted: #999999;
    --color-border: #333333;
    --color-bg: #0a0a0a;
    --color-bg-alt: #1a1a1a;
  }
}
```

### 2.2 Component Styling Rules

1. **No inline styles** (CSP policy)
2. **No external images** except `data:` URIs or inline SVG
3. **All widths/colors/positioning via CSS classes**
4. **Accessibility:** all interactive elements keyboard-navigable
5. **Contrast:** all text ≥ 4.5:1 on background

---

## 3. CSV UPLOAD FLOW

### 3.1 Happy Path: User Uploads GR CAL Export

```
User (Browser)                         Backend                    Store
    │                                    │                         │
    ├─ Click "Data" tab ─────────────────┤                         │
    │                                    │                         │
    ├─ Select CSV file ─────────────────┤                         │
    │                                    │                         │
    ├─ Drag & drop (or click)            │                         │
    │                                    │                         │
    ├─ Read file locally ────────┐       │                         │
    │ (up to 100MB)              │       │                         │
    │                            └──────►POST /api/data/upload──┐  │
    │                                    │                      │  │
    │                                    ├─ Parse CSV ──────────┘  │
    │                                    │                         │
    │                                    ├─ Infer types           │
    │                                    │   (TRADE_ID, PRICE,     │
    │                                    │    QUANTITY, etc.)      │
    │                                    │                         │
    │                                    ├─ Auto-suggest mapping  │
    │                                    │   (title case match)    │
    │                                    │                         │
    │ ◄──────────── Preview JSON ────────┤                         │
    │ {                                  │                         │
    │   "rows": 50,                      │                         │
    │   "columns": {                     │                         │
    │     "TRADE_ID": "int",             │                         │
    │     "ENTRY_DATE": "datetime",      │                         │
    │     ...                            │                         │
    │   },                               │                         │
    │   "mapping": {...}                 │                         │
    │ }                                  │                         │
    │                                    │                         │
    ├─ [Show preview table]              │                         │
    ├─ [Show column mapping UI]          │                         │
    │  (user can override types)         │                         │
    │                                    │                         │
    ├─ Click "Validate" ────────────────►POST /api/data/validate  │
    │                                    │                         │
    │                                    ├─ Apply user mapping    │
    │                                    ├─ Run contract checks   │
    │                                    │ • Required fields ✓    │
    │                                    │ • Type coercion ✓      │
    │                                    │ • Ranges ✓             │
    │                                    │                         │
    │ ◄──────── Capability Matrix ───────┤                         │
    │ {                                  │                         │
    │   "coverage": {...},               │                         │
    │   "unavailable": [],               │                         │
    │   "hypotheses": {...},             │                         │
    │   "rule_gaps": [],                 │                         │
    │   "errors": []                     │                         │
    │ }                                  │                         │
    │                                    │                         │
    ├─ [Show coverage → expandable]      │                         │
    ├─ [Show hypotheses status]          │                         │
    ├─ [Show any errors]                 │                         │
    │                                    │                         │
    ├─ Click "Confirm & Proceed" ───────►POST /api/data/confirm──►│
    │                                    │                         │
    │                                    │                         │
    │                                    ├─ Store in DB ──────────┤
    │                                    ├─ Mark as "active"      │
    │                                    ├─ Freeze baseline date  │
    │                                    │                         │
    │ ◄────────── {"status": "ready"} ───┤                         │
    │                                    │                         │
    └─ [Show "Ready to run" in Overview] │                         │
```

### 3.2 Unhappy Path: Errors & Validation Failures

```
Error Scenario                  Frontend Response
─────────────────────────────────────────────────────
1. File too large              → Toast: "File > 100MB"
   (> 100MB)                      Disable upload

2. Unknown file type           → Toast: "Only CSV, Excel, Parquet"
   (.txt, .json, etc.)           Clear input

3. Parsing failed              → Modal with:
   (corrupted CSV)                • Error message
                                  • Line number (if available)
                                  • Sample of bad row
                                  • Suggest format check

4. Column mismatch             → Highlight missing columns in red
   (missing TRADE_ID)            Show required fields list
                                  Suggest mapping correction

5. Type coercion failed        → Table shows row # + column
   (PRICE = "abc")               Allow user to fix or skip rows

6. Coverage below threshold    → Yellow banner:
   (e.g., 40% PRICE)            "PRICE coverage 40% < required 50%"
                                  "This will affect [list features]"
                                  Allow proceed with warning

7. Data contract violation     → Red banner with details:
                                  "QUANTITY not in range [0, ∞)"
                                  Show row examples
                                  Must fix before proceed
```

---

## 4. FRONTEND STRUCTURE

### 4.1 File Layout

```
src/asas/api/static/
├── index.html              (11 lines only, no body content)
├── app.js                  (rewrite: ~800 lines)
│   ├── Core API client
│   ├── View loaders (8 tabs)
│   ├── Data upload handler
│   ├── CSV preview & mapping UI
│   ├── Capability matrix display
│   └── Event handlers
├── styles.css              (rewrite: ~400 lines)
│   ├── Layout (header, sidebar, main)
│   ├── Color palette
│   ├── Components (card, table, modal, progress)
│   ├── Responsive (mobile, tablet, desktop)
│   ├── Dark mode
│   └── Accessibility (focus, labels)
├── components/             (NEW DIRECTORY)
│   ├── csv-uploader.js     (drag-drop, file input)
│   ├── column-mapper.js    (type selector, rename)
│   ├── capability-display.js (expandable matrix)
│   ├── progress-tracker.js (upload, validation, confirm)
│   └── error-display.js    (collapsible error list)
└── utils/                  (NEW DIRECTORY)
    ├── fetch-helper.js     (api() wrapper with retry)
    ├── format.js           (number, date, size formatting)
    └── validate.js         (contract rules, type checking)
```

### 4.2 Tab Structure

| Tab | Feature | Status | New Code Needed |
|-----|---------|--------|-----------------|
| Overview | Run status, KPIs, alerts, drift | ✓ Exists | Minor polish |
| Cases | List + filters, bulk actions | ✓ Exists | Expandable detail |
| Relationships | Link proposal, graph | ✓ Exists | Link detail |
| Challenger | Blind spots, redundancies | ✓ Exists | Sortable results |
| Discovery/Governance | Candidate rules, approval flow | ✓ Exists | Ballot UI |
| Policy | Rule editor, versions | Partial | Complete with live preview |
| Data | **[FOCUS] CSV upload + analysis** | ❌ Missing | Full implementation |
| Audit | Hash chain verification | ✓ Exists | Live verification button |

---

## 5. API INTEGRATION POINTS

### 5.1 New Endpoints Required

All endpoints require authentication (header: `X-User`, `X-Roles` for dev; JWT or proxy for prod).

#### POST /api/data/upload

**Purpose:** Parse a user-uploaded CSV/Excel/Parquet file and return a mapping preview.

**Request:**
```python
# Frontend sends FormData:
formData = new FormData()
formData.append("file", fileInput.files[0])  # Binary file, any size
formData.append("mapping_hint", "")           # Optional: preset mapping name

fetch("/api/data/upload", {
  method: "POST",
  headers: {},  # FormData sets boundary automatically
  body: formData
})
```

**Response (200 OK):**
```json
{
  "filename": "GR_CAL_export_2026-09-29.csv",
  "format": "csv",
  "rows_total": 15234,
  "rows_preview": [
    {"TRADE_ID": "TR123", "ENTRY_DATE": "2026-09-28 10:30", "PRICE": "105.5", ...},
    ...  // up to 50 rows
  ],
  "columns_detected": {
    "TRADE_ID": "int",
    "ENTRY_DATE": "timestamp",
    "PRICE": "decimal",
    "QUANTITY": "int",
    "BOOK": "string",
    "DESK": "string"
  },
  "mapping_suggested": {
    "version": "1.0",
    "trade_events": {
      "columns": {
        "TRADE_ID": "TRADE_ID",
        "ENTRY_DATE": "EVENT_TIME",
        "CREATED_AT": "RECORD_TIME",
        "PRICE": "PRICE",
        "QUANTITY": "QUANTITY",
        "BOOK": "BOOK",
        "DESK": "DESK",
        "BUY_OR_SELL": "SIDE",
        ...
      },
      "timestamp_format": "%Y-%m-%d %H:%M:%S",
      "timezone": "Europe/Zurich"
    },
    "alerts": {...},
    "outcomes": {...}
  },
  "warnings": []  // e.g., ["Column 'URN_REF' has 80% nulls"]
}
```

**Error Responses:**
```json
// 400 Bad Request
{
  "detail": "File exceeds 100MB limit",
  "error_code": "FILE_TOO_LARGE"
}

// 415 Unsupported Media Type
{
  "detail": "Only CSV, Excel, and Parquet files supported",
  "error_code": "UNSUPPORTED_FORMAT"
}

// 422 Unprocessable Entity (parse failure)
{
  "detail": "CSV parsing failed at line 412: invalid UTF-8 sequence",
  "error_code": "PARSE_ERROR",
  "line": 412,
  "column": 5
}
```

#### POST /api/data/validate

**Purpose:** Validate the uploaded data against the contract using a user-provided (or corrected) mapping.

**Request:**
```json
{
  "filename": "GR_CAL_export_2026-09-29.csv",
  "mapping": {
    "version": "1.0",
    "trade_events": {
      "columns": { ... },  // User may have corrected this
      "constants": {"SOURCE": "GR_CAL"},
      "copies": {},
      "timestamp_format": "%Y-%m-%d %H:%M:%S",
      "timezone": "Europe/Zurich"
    },
    "alerts": { ... },
    "outcomes": { ... }
  }
}
```

**Response (200 OK):**
```json
{
  "status": "valid",
  "rows_total": 15234,
  "rows_loaded": 15234,
  "errors": [],
  "warnings": [
    "Column 'ALTERNATE_TRADE_ID' has 15% coverage"
  ],
  "data_capabilities": {
    "coverage": {
      "SIDE": "1.0000",
      "QUANTITY": "0.9999",
      "PRICE": "0.9998",
      "NOTIONAL_USD": "0.5234",
      "RECORD_TIME": "0.8812"
    },
    "unavailable": [],
    "entities": {
      "trade_events": 15234,
      "alerts": 1824,
      "outcomes": 0,
      "curated_outcomes": 0
    }
  },
  "capability_matrix": {
    "hypotheses": {
      "PRICE_CORRECTION": "ENABLED",
      "ROUTINE_EXECUTION": "ENABLED",
      "VERIFIED_PEER_DEVIATION": "ENABLED",
      ...
    },
    "signals": {
      "max_price_change_pct": "ENABLED",
      "booking_latency_hours_max": "DEGRADED:RECORD_TIME_COPIED",
      ...
    },
    "features": {
      "anomaly_analysis": "ENABLED",
      "latency_checks": "DEGRADED",
      ...
    },
    "rule_gaps": []
  }
}
```

**Error Responses:**
```json
// 422 Unprocessable Entity (contract violation)
{
  "status": "invalid",
  "error_count": 3,
  "errors": [
    {
      "field": "TRADE_ID",
      "type": "required_missing",
      "message": "Required field not in mapping"
    },
    {
      "field": "SIDE",
      "type": "type_mismatch",
      "rows": [412, 515, 1024],
      "message": "SIDE must be BUY or SELL, got 'BOTH' at rows [412, 515, 1024]"
    },
    {
      "field": "QUANTITY",
      "type": "range_violation",
      "rows": [800],
      "message": "QUANTITY must be > 0, got -500 at row 800"
    }
  ]
}
```

#### POST /api/data/confirm

**Purpose:** Finalize the upload, store in database, freeze baselines, and mark as ready.

**Request:**
```json
{
  "filename": "GR_CAL_export_2026-09-29.csv",
  "mapping_version": "1.0"
}
```

**Response (200 OK):**
```json
{
  "status": "confirmed",
  "data_id": "data-20260929-k7m2",
  "rows_stored": 15234,
  "baseline_frozen_at": "2026-09-29T00:00:00Z",
  "ready_for_run": true,
  "message": "Data loaded. You can now run the pipeline."
}
```

---

## 6. COMPONENT SPECIFICATIONS

### 6.1 CSV Upload Component (csv-uploader.js)

**HTML Structure:**
```html
<div id="csv-uploader" class="card">
  <h3>Load Data</h3>
  <p class="muted">Upload a CSV, Excel, or Parquet file (max 100MB)</p>
  
  <div id="upload-zone" class="upload-zone">
    <svg class="upload-icon"><!-- file icon --></svg>
    <p><strong>Drag and drop here</strong> or <label><u>select a file</u>
      <input type="file" id="file-input" accept=".csv,.xlsx,.parquet" hidden>
    </label></p>
  </div>
  
  <div id="upload-progress" class="progress-wrap" hidden>
    <div class="progress-bar">
      <span id="progress-fill" class="fill" style="width: 0%"></span>
    </div>
    <p id="progress-text" class="muted">Uploading... <span id="progress-pct">0</span>%</p>
  </div>
  
  <div id="upload-result"></div>
</div>
```

**JavaScript:**
```javascript
class CSVUploader {
  constructor() {
    this.zone = document.getElementById("upload-zone");
    this.input = document.getElementById("file-input");
    this.progressWrap = document.getElementById("upload-progress");
    this.progressFill = document.getElementById("progress-fill");
    this.progressPct = document.getElementById("progress-pct");
    this.resultEl = document.getElementById("upload-result");
    
    this.setupDragDrop();
    this.setupFileInput();
  }
  
  setupDragDrop() {
    this.zone.ondragover = (e) => {
      e.preventDefault();
      this.zone.classList.add("active");
    };
    this.zone.ondragleave = () => this.zone.classList.remove("active");
    this.zone.ondrop = (e) => {
      e.preventDefault();
      this.zone.classList.remove("active");
      this.handleFiles(e.dataTransfer.files);
    };
  }
  
  setupFileInput() {
    this.input.onchange = (e) => this.handleFiles(e.target.files);
  }
  
  async handleFiles(files) {
    if (files.length === 0) return;
    const file = files[0];
    
    // Validate size
    if (file.size > 100 * 1024 * 1024) {
      toast("File exceeds 100MB limit");
      return;
    }
    
    // Validate type
    const allowed = ["text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/octet-stream"];
    if (!allowed.includes(file.type) && !file.name.endsWith(".parquet")) {
      toast("Only CSV, Excel, and Parquet files supported");
      return;
    }
    
    this.progressWrap.hidden = false;
    this.progressFill.style.width = "0%";
    
    try {
      const result = await this.uploadFile(file);
      this.displayPreview(result);
    } catch (err) {
      toast(`Upload failed: ${err.message}`);
      this.resultEl.innerHTML = `<div class="banner error">${esc(err.message)}</div>`;
    }
  }
  
  async uploadFile(file) {
    const formData = new FormData();
    formData.append("file", file);
    
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const pct = Math.round((e.loaded / e.total) * 100);
          this.progressPct.textContent = pct;
          this.progressFill.style.width = pct + "%";
        }
      };
      
      xhr.onload = async () => {
        if (xhr.status === 200) {
          resolve(JSON.parse(xhr.responseText));
        } else {
          const err = JSON.parse(xhr.responseText);
          reject(new Error(err.detail || "Upload failed"));
        }
      };
      
      xhr.onerror = () => reject(new Error("Network error"));
      xhr.open("POST", "/api/data/upload");
      xhr.send(formData);
    });
  }
  
  displayPreview(data) {
    this.progressWrap.hidden = true;
    
    // Show file info
    let html = `
      <div class="card mt">
        <h4>File Preview</h4>
        <p><strong>${esc(data.filename)}</strong>
        <span class="muted">${esc(data.format)} · ${data.rows_total.toLocaleString()} rows</span></p>
        
        <table class="preview-table">
          <tr>${Object.keys(data.columns_detected).slice(0, 8).map(k => `<th>${esc(k)}</th>`).join("")}</tr>
          ${data.rows_preview.slice(0, 3).map(row => 
            `<tr>${Object.keys(data.columns_detected).slice(0, 8).map(k => 
              `<td>${esc(row[k])}</td>`
            ).join("")}</tr>`
          ).join("")}
        </table>
      </div>
    `;
    
    this.resultEl.innerHTML = html;
    
    // Call column mapper
    new ColumnMapper(data);
  }
}
```

### 6.2 Column Mapper Component (column-mapper.js)

**HTML Structure:**
```html
<div id="column-mapper" class="card mt">
  <h4>Column Mapping</h4>
  <p class="muted">Verify or correct how your columns map to ASAS contract fields</p>
  
  <div class="mapper-grid">
    <!-- One row per source column -->
  </div>
  
  <button id="mapper-validate" class="btn primary mt">Validate Mapping</button>
</div>
```

**JavaScript:**
```javascript
class ColumnMapper {
  constructor(uploadData) {
    this.data = uploadData;
    this.mapping = uploadData.mapping_suggested;
    this.render();
  }
  
  render() {
    const grid = document.createElement("div");
    grid.className = "mapper-grid";
    
    Object.entries(this.mapping.trade_events.columns).forEach(([contractField, sourceColumn]) => {
      const row = document.createElement("div");
      row.className = "mapper-row";
      row.innerHTML = `
        <label class="mapper-label">${esc(contractField)}</label>
        <select class="mapper-select" data-field="${esc(contractField)}">
          <option value="">(not mapped)</option>
          ${Object.keys(this.data.columns_detected).map(col => 
            `<option value="${esc(col)}" ${col === sourceColumn ? "selected" : ""}>${esc(col)}</option>`
          ).join("")}
        </select>
        <span class="mapper-type">${esc(this.data.columns_detected[sourceColumn] || "?")}</span>
      `;
      grid.appendChild(row);
    });
    
    document.getElementById("column-mapper").replaceChild(grid, document.querySelector(".mapper-grid"));
    document.getElementById("mapper-validate").onclick = () => this.validate();
  }
  
  async validate() {
    // Collect user choices
    const mapping = JSON.parse(JSON.stringify(this.mapping));
    document.querySelectorAll(".mapper-select").forEach(sel => {
      mapping.trade_events.columns[sel.dataset.field] = sel.value;
    });
    
    // Call /api/data/validate
    try {
      const result = await api("/api/data/validate", {
        method: "POST",
        body: JSON.stringify({
          filename: this.data.filename,
          mapping: mapping
        })
      });
      
      new CapabilityDisplay(result.data_capabilities, mapping);
    } catch (err) {
      toast(`Validation failed: ${err.message}`);
    }
  }
}
```

### 6.3 Capability Display Component (capability-display.js)

**Purpose:** Show coverage, unavailable fields, hypotheses status, etc. with expandable detail.

**Structure:**
```html
<div id="capability-display" class="card mt">
  <h4>Data Capabilities</h4>
  <p class="muted">What ASAS can do with your data</p>
  
  <!-- Expandable sections -->
  <details open>
    <summary><strong>Coverage</strong></summary>
    <div class="capability-section">
      <!-- Coverage table -->
    </div>
  </details>
  
  <details open>
    <summary><strong>Hypotheses Status</strong></summary>
    <div class="capability-section">
      <!-- Hypothesis table with ENABLED/DEGRADED/DISABLED -->
    </div>
  </details>
  
  <details>
    <summary><strong>Signals</strong> (click for detail)</summary>
    <div class="capability-section">
      <!-- Signal table -->
    </div>
  </details>
  
  <details>
    <summary><strong>Errors & Warnings</strong> 
      <span class="chip warn" id="error-count">3</span>
    </summary>
    <div class="error-list">
      <!-- Errors with row numbers -->
    </div>
  </details>
  
  <button id="confirm-data" class="btn primary mt">Confirm & Proceed</button>
</div>
```

**JavaScript:**
```javascript
class CapabilityDisplay {
  constructor(capabilities, mapping) {
    this.cap = capabilities;
    this.mapping = mapping;
    this.render();
  }
  
  render() {
    const el = document.getElementById("capability-display");
    if (!el) return;  // Not present yet
    
    // Coverage section
    const coverageHtml = this.renderCoverage();
    
    // Hypothesis section
    const hypothesesHtml = this.renderHypotheses();
    
    // Error section
    const errorsHtml = this.renderErrors();
    
    el.innerHTML = `
      ${this.cap.errors && this.cap.errors.length > 0 ? 
        `<div class="banner error">
          <strong>${this.cap.errors.length} validation error(s)</strong>
          Fix these before proceeding.
        </div>` : ""
      }
      
      <details open>
        <summary><strong>Field Coverage</strong></summary>
        ${coverageHtml}
      </details>
      
      <details open>
        <summary><strong>Hypotheses Available</strong></summary>
        ${hypothesesHtml}
      </details>
      
      <details>
        <summary><strong>Deviation Signals</strong></summary>
        <table class="signal-table">
          <tr><th>Signal</th><th>Status</th><th>Why</th></tr>
          ${Object.entries(this.cap.capability_matrix.signals).map(([sig, status]) => 
            `<tr class="${status.includes("DEGRADED") ? "warn" : ""}">
              <td><strong>${esc(sig)}</strong></td>
              <td>${esc(status.split(":")[0])}</td>
              <td class="muted">${esc(status.split(":")[1] || "")}</td>
            </tr>`
          ).join("")}
        </table>
      </details>
      
      ${errorsHtml ? `
        <details>
          <summary><strong>Errors</strong> (${this.cap.errors.length})</summary>
          ${errorsHtml}
        </details>
      ` : ""}
      
      <button class="btn primary mt" onclick="confirmData()">Confirm & Proceed</button>
    `;
  }
  
  renderCoverage() {
    return `<div class="coverage-grid">
      ${Object.entries(this.cap.coverage).map(([field, share]) => {
        const pct = parseFloat(share) * 100;
        const unavailable = this.cap.unavailable.includes(field);
        return `
          <div class="coverage-card ${unavailable ? "unavailable" : ""}">
            <div class="coverage-label">${esc(field)}</div>
            <div class="coverage-bar">
              <span style="width: ${pct}%"></span>
            </div>
            <div class="coverage-pct">${pct.toFixed(1)}%</div>
            ${unavailable ? `<div class="coverage-warning">Below 50% threshold</div>` : ""}
          </div>
        `;
      }).join("")}
    </div>`;
  }
  
  renderHypotheses() {
    const hyp = this.cap.capability_matrix.hypotheses;
    return `<div class="hypothesis-list">
      ${Object.entries(hyp).map(([name, status]) => `
        <div class="hypothesis-item ${status === "ENABLED" ? "enabled" : "degraded"}">
          <span class="chip">${status === "ENABLED" ? "✓ " : "⚠ "}${esc(name)}</span>
          <span class="muted">${status === "ENABLED" ? "Available" : status}</span>
        </div>
      `).join("")}
    </div>`;
  }
  
  renderErrors() {
    if (!this.cap.errors || this.cap.errors.length === 0) return "";
    return `<div class="error-detail">
      ${this.cap.errors.map(err => `
        <div class="error-item">
          <div class="error-field"><strong>${esc(err.field)}</strong></div>
          <div class="error-msg">${esc(err.message)}</div>
          ${err.rows ? `<div class="error-rows muted">Rows: ${err.rows.slice(0, 5).join(", ")}${err.rows.length > 5 ? "..." : ""}</div>` : ""}
        </div>
      `).join("")}
    </div>`;
  }
}
```

---

## 7. TESTING STRATEGY

### 7.1 Unit Tests (Vitest or Jest)

**File:** `tests/frontend/csv-uploader.test.js`

```javascript
import { describe, it, expect, beforeEach } from "vitest";
import { CSVUploader } from "../../src/asas/api/static/components/csv-uploader";

describe("CSVUploader", () => {
  let uploader;
  
  beforeEach(() => {
    uploader = new CSVUploader();
  });
  
  it("should reject files > 100MB", async () => {
    const largFile = new File(
      [new ArrayBuffer(101 * 1024 * 1024)],
      "large.csv",
      { type: "text/csv" }
    );
    await uploader.handleFiles([largFile]);
    expect(toast).toHaveBeenCalledWith("File exceeds 100MB limit");
  });
  
  it("should reject unsupported file types", async () => {
    const badFile = new File(["content"], "data.txt", { type: "text/plain" });
    await uploader.handleFiles([badFile]);
    expect(toast).toHaveBeenCalledWith("Only CSV, Excel, and Parquet files supported");
  });
  
  it("should parse CSV correctly", async () => {
    const csvFile = new File(
      ["TRADE_ID,PRICE,QUANTITY\n123,105.5,1000"],
      "test.csv",
      { type: "text/csv" }
    );
    const result = await uploader.uploadFile(csvFile);
    expect(result.rows_total).toBe(1);
    expect(result.columns_detected).toHaveProperty("TRADE_ID");
  });
});
```

### 7.2 Integration Tests (API + Frontend)

**File:** `tests/frontend/api-integration.test.js`

```javascript
import { describe, it, expect, beforeAll } from "vitest";
import { api } from "../../src/asas/api/static/app";

describe("CSV Upload API Flow", () => {
  it("should upload and return mapping preview", async () => {
    const file = new File(
      ["TRADE_ID,PRICE\n123,105.5"],
      "test.csv",
      { type: "text/csv" }
    );
    const formData = new FormData();
    formData.append("file", file);
    
    const res = await fetch("/api/data/upload", {
      method: "POST",
      body: formData
    });
    
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data.mapping_suggested).toBeDefined();
  });
  
  it("should validate mapping and return capabilities", async () => {
    const validateRes = await api("/api/data/validate", {
      method: "POST",
      body: JSON.stringify({
        filename: "test.csv",
        mapping: { /* ... */ }
      })
    });
    
    expect(validateRes.data_capabilities).toBeDefined();
    expect(validateRes.data_capabilities.coverage).toBeDefined();
  });
  
  it("should confirm and store data", async () => {
    const confirmRes = await api("/api/data/confirm", {
      method: "POST",
      body: JSON.stringify({
        filename: "test.csv",
        mapping_version: "1.0"
      })
    });
    
    expect(confirmRes.status).toBe("confirmed");
    expect(confirmRes.ready_for_run).toBe(true);
  });
});
```

### 7.3 E2E Tests (Playwright)

**File:** `tests/frontend/csv-flow.e2e.js`

```javascript
import { test, expect } from "@playwright/test";

test("Full CSV upload and analysis flow", async ({ page }) => {
  await page.goto("http://localhost:8000");
  
  // Click Data tab
  await page.click('[data-view="data"]');
  
  // Upload file
  const fileInput = page.locator('input[type="file"]');
  await fileInput.setInputFiles("tests/fixtures/sample_cal_export.csv");
  
  // Wait for preview
  await page.waitForSelector(".preview-table");
  await expect(page.locator(".preview-table")).toBeVisible();
  
  // Verify column mapping shows
  await expect(page.locator(".mapper-grid")).toBeVisible();
  
  // Click validate
  await page.click("#mapper-validate");
  
  // Wait for capability display
  await page.waitForSelector("#capability-display");
  await expect(page.locator("details")).toHaveCount({ gte: 3 });
  
  // Expand coverage detail
  await page.click("summary:has-text('Field Coverage')");
  await expect(page.locator(".coverage-grid")).toBeVisible();
  
  // Click confirm
  await page.click("#confirm-data");
  
  // Verify success
  await expect(page).toHaveURL(/.*\?ready=true/);
});
```

### 7.4 Accessibility Testing

```javascript
// tests/frontend/accessibility.test.js
import { test, expect } from "@playwright/test";
import { injectAxe, checkA11y } from "axe-playwright";

test("CSV upload UI is accessible", async ({ page }) => {
  await page.goto("http://localhost:8000");
  await injectAxe(page);
  
  // Check Data tab
  await page.click('[data-view="data"]');
  
  await checkA11y(page);  // Axe accessibility scan
});
```

### 7.5 Performance Testing

```javascript
// tests/frontend/performance.test.js
test("CSV upload completes in < 10s for 100k rows", async ({ page }) => {
  const start = performance.now();
  
  await page.goto("http://localhost:8000");
  const fileInput = page.locator('input[type="file"]');
  await fileInput.setInputFiles("tests/fixtures/large_100k.csv");
  
  await page.waitForSelector("#capability-display", { timeout: 10000 });
  
  const duration = performance.now() - start;
  expect(duration).toBeLessThan(10000);
});
```

---

## 8. OPTIMIZATION & PERFORMANCE

### 8.1 Frontend Performance Targets

| Metric | Target | How to Measure |
|--------|--------|-----------------|
| CSV Parse (100k rows) | < 5s | Lighthouse, WebPageTest |
| First Paint (Data tab) | < 1s | Chrome DevTools |
| Column Mapper render | < 500ms | Performance API |
| Capability Display render | < 800ms | Performance API |
| Memory (during upload) | < 200MB | Chrome Memory profiler |

### 8.2 Optimization Techniques

**1. Lazy Render Preview Table:**
```javascript
renderPreviewTable(data) {
  const table = document.createElement("table");
  
  // Render only first 50 rows initially
  const visibleRows = data.rows_preview.slice(0, 50);
  visibleRows.forEach(row => {
    const tr = document.createElement("tr");
    tr.innerHTML = rowHtml;
    table.appendChild(tr);
  });
  
  // Lazy-load remaining rows on scroll
  const observer = new IntersectionObserver((...) => {
    if (remainingRows > 0) {
      // Render next batch
    }
  });
  observer.observe(table.lastElementChild);
}
```

**2. Debounce Column Mapper Updates:**
```javascript
class ColumnMapper {
  constructor() {
    this.validateDebounced = debounce(() => this.validate(), 500);
  }
  
  setupChangeListeners() {
    document.querySelectorAll(".mapper-select").forEach(sel => {
      sel.onchange = () => this.validateDebounced();  // Not instant
    });
  }
}

function debounce(fn, ms) {
  let timer;
  return function(...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), ms);
  };
}
```

**3. IndexedDB Cache for Capability Results:**
```javascript
class CapabilityCache {
  async get(mappingHash) {
    const db = await this.openDB();
    return db.get("capabilities", mappingHash);
  }
  
  async set(mappingHash, data) {
    const db = await this.openDB();
    await db.put("capabilities", { ...data, hash: mappingHash });
  }
}
```

**4. Compression & Streaming:**
```javascript
// For large file uploads, use resumable chunks
async function uploadChunked(file) {
  const chunkSize = 5 * 1024 * 1024;  // 5MB chunks
  let offset = 0;
  
  while (offset < file.size) {
    const chunk = file.slice(offset, offset + chunkSize);
    const formData = new FormData();
    formData.append("chunk", chunk);
    formData.append("offset", offset);
    formData.append("filename", file.name);
    
    await fetch("/api/data/upload-chunk", { method: "POST", body: formData });
    offset += chunkSize;
  }
}
```

---

## 9. ERROR HANDLING & VALIDATION

### 9.1 Validation Rules

**At Frontend:**
- File size ≤ 100MB
- File type in [CSV, Excel, Parquet]
- At least 1 row after header

**At Backend (contract.py):**
- All required fields mapped
- No invented labels (constants/copies on OUTCOME, LABEL_QUALITY, DECIDED_AT, DECIDED_BY)
- SIDE in [BUY, SELL]
- QUANTITY, PRICE, NOTIONAL_USD ≥ 0
- TRADE_ID, ALERT_ID unique per source
- Dates valid and in reasonable range

### 9.2 Error Display Patterns

**Toast (transient, 3.5s):**
```
"File exceeds 100MB limit"
"Upload failed: network timeout"
```

**Banner (persistent, prominent):**
```
<div class="banner error">
  <strong>3 validation errors</strong>
  Fix these before proceeding.
</div>
```

**Modal (blocking, detailed):**
```
<div class="modal">
  <h3>CSV Parse Error</h3>
  <p>Invalid UTF-8 sequence at line 412, column 5</p>
  <code>...PRICE,"1🚀5"...</code>
  <p class="muted">File may be corrupted or in wrong encoding.</p>
  <button>Retry</button> <button>Cancel</button>
</div>
```

**Expandable Detail (in-place):**
```
<details>
  <summary>Validation Errors <span class="chip warn">3</span></summary>
  <div class="error-list">
    <div class="error-item">
      <div><strong>SIDE</strong> (rows 412, 515, 1024)</div>
      <div>Must be BUY or SELL, got BOTH</div>
    </div>
    ...
  </div>
</details>
```

---

## 10. MASTER IMPLEMENTATION CHECKLIST

### Phase 1: Backend API Endpoints (Server-Side)

**In `src/asas/api/app.py`:**

- [ ] **POST /api/data/upload** (FormData handling)
  - [ ] Accept multipart file upload (max 100MB)
  - [ ] Parse CSV (pandas or csv module)
  - [ ] Parse Excel (openpyxl)
  - [ ] Parse Parquet (pyarrow)
  - [ ] Infer column types (int, decimal, string, datetime)
  - [ ] Auto-suggest mapping (title case matching)
  - [ ] Return preview JSON (rows + mapping)
  - [ ] Handle errors (file too large, parse error, unsupported format)
  - [ ] Log upload metadata to audit chain

- [ ] **POST /api/data/validate** (Mapping validation)
  - [ ] Accept mapping JSON
  - [ ] Apply mapping rules (columns, constants, copies)
  - [ ] Run contract checks (required fields, types, ranges)
  - [ ] Check field coverage (≥ 50% rule)
  - [ ] Build capability matrix
  - [ ] Return capability_matrix + errors + warnings
  - [ ] Do NOT store data yet

- [ ] **POST /api/data/confirm** (Finalize & store)
  - [ ] Store parsed data in database
  - [ ] Freeze baseline date
  - [ ] Mark as "active" for runs
  - [ ] Return confirmation JSON
  - [ ] Audit log the load

- [ ] **GET /api/data/status** (Check current load)
  - [ ] Return: rows loaded, last load time, baseline frozen date
  - [ ] Return: capability matrix from latest load

### Phase 2: Frontend Data Tab (Client-Side)

**In `src/asas/api/static/`:**

- [ ] **app.js** modifications
  - [ ] Add `async function data()` (rewrite from line 377)
  - [ ] Import new components (csv-uploader, column-mapper, capability-display)
  - [ ] Setup event listeners for upload, validate, confirm
  - [ ] Render upload zone, preview, mapping UI, capabilities
  - [ ] Handle API responses and errors

- [ ] **components/csv-uploader.js** (NEW)
  - [ ] Drag-drop zone setup
  - [ ] File input handler
  - [ ] File size validation (100MB)
  - [ ] File type validation
  - [ ] XHR upload with progress bar
  - [ ] Display preview table (first 50 rows)
  - [ ] Call ColumnMapper on success

- [ ] **components/column-mapper.js** (NEW)
  - [ ] Render one row per contract field
  - [ ] Dropdown for each to select source column
  - [ ] Show detected type for each column
  - [ ] Call /api/data/validate on button click
  - [ ] Display validation errors inline

- [ ] **components/capability-display.js** (NEW)
  - [ ] Show coverage table (field name + %)
  - [ ] Expandable coverage detail
  - [ ] Hypothesis status table (ENABLED/DEGRADED)
  - [ ] Signals status table
  - [ ] Errors section (collapsible, with row numbers)
  - [ ] Warnings section (collapsible)
  - [ ] "Confirm & Proceed" button → /api/data/confirm

- [ ] **styles.css** additions
  - [ ] `.upload-zone` (drag-drop styling, min 150px height)
  - [ ] `.upload-zone.active` (highlight on drag-over)
  - [ ] `.progress-bar` (0-100% fill)
  - [ ] `.preview-table` (scrollable, monospace PRICE/QTY)
  - [ ] `.mapper-grid` / `.mapper-row` (flex layout, 3 columns)
  - [ ] `.coverage-grid` (grid, 4 columns on desktop)
  - [ ] `.coverage-card` (border, padding, center-aligned %)
  - [ ] `.hypothesis-list` / `.hypothesis-item` (chips, color by status)
  - [ ] `.error-list` (red banner, row numbers)
  - [ ] `details` styling (caret, padding)
  - [ ] Dark mode variants (all colors)

### Phase 3: Integration & Testing

- [ ] **tests/test_data_upload.py** (Backend unit tests)
  - [ ] Test CSV parsing with various delimiters
  - [ ] Test Excel parsing (xlsx, xls)
  - [ ] Test Parquet parsing
  - [ ] Test mapping application
  - [ ] Test contract validation
  - [ ] Test error cases (file too large, parse error, contract violation)

- [ ] **tests/frontend/csv-flow.e2e.js** (E2E tests)
  - [ ] Upload CSV → see preview
  - [ ] Modify column mapping → validate
  - [ ] See capability matrix → confirm
  - [ ] Verify data marked as active
  - [ ] Test error flows (upload failure, validation failure)

- [ ] **tests/frontend/accessibility.test.js**
  - [ ] Run axe-core scan on Data tab
  - [ ] Verify focus order
  - [ ] Verify all form labels present

- [ ] **Performance tests**
  - [ ] CSV parse 100k rows in < 5s
  - [ ] Capability display render in < 800ms
  - [ ] Memory usage < 200MB during upload

### Phase 4: Documentation & Polish

- [ ] **docs/playbooks/integrate-real-data.md** (update)
  - [ ] Add step: "Use Data tab to upload CSV"
  - [ ] Add: screenshot of upload zone
  - [ ] Add: sample mapping (GR CAL → contract)
  - [ ] Add: common errors and fixes

- [ ] **docs/user-guide.md** (NEW or update)
  - [ ] CSV format requirements
  - [ ] Mapping rules
  - [ ] Timezone handling
  - [ ] Troubleshooting

- [ ] **ASAS console help text** (in UI)
  - [ ] Hover tooltips on each capability status
  - [ ] "Why is this degraded?" explanations
  - [ ] "What should I do?" recommendations

- [ ] **Styling polish**
  - [ ] Verify red/black/grey/white only
  - [ ] Verify no inline styles (CSP compliant)
  - [ ] Verify contrast ≥ 4.5:1 (WCAG AA)
  - [ ] Verify responsive on mobile (375px) and tablet (768px)
  - [ ] Test dark mode

---

## 11. CODE TEMPLATES & SNIPPETS

### 11.1 Backend Endpoint Template (Python FastAPI)

```python
# In src/asas/api/app.py

from fastapi import UploadFile, File, HTTPException
from pydantic import BaseModel
import pandas as pd

class DataUploadResponse(BaseModel):
    filename: str
    format: str
    rows_total: int
    rows_preview: list[dict]
    columns_detected: dict[str, str]
    mapping_suggested: dict
    warnings: list[str]

@app.post("/api/data/upload")
async def upload_data(
    file: UploadFile = File(...),
    p: Principal = Depends(principal)
) -> JSONResponse:
    """Parse uploaded CSV/Excel/Parquet and return mapping preview."""
    
    # 1. Validate file
    if file.size > 100 * 1024 * 1024:
        raise HTTPException(400, "File exceeds 100MB limit")
    
    allowed = ["text/csv", "application/vnd.openxmlformats", "application/octet-stream"]
    if file.content_type not in allowed and not file.filename.endswith(".parquet"):
        raise HTTPException(415, "Only CSV, Excel, Parquet supported")
    
    # 2. Parse file
    try:
        if file.filename.endswith(".csv"):
            df = pd.read_csv(await file.read())
        elif file.filename.endswith(".xlsx"):
            df = pd.read_excel(await file.read())
        elif file.filename.endswith(".parquet"):
            df = pd.read_parquet(await file.read())
    except Exception as e:
        raise HTTPException(422, f"Parse error: {str(e)}")
    
    # 3. Infer types
    types_detected = {}
    for col in df.columns:
        dtype = str(df[col].dtype)
        if "int" in dtype:
            types_detected[col] = "int"
        elif "float" in dtype:
            types_detected[col] = "decimal"
        elif "datetime" in dtype:
            types_detected[col] = "timestamp"
        else:
            types_detected[col] = "string"
    
    # 4. Suggest mapping (title case matching)
    mapping = suggest_mapping(df.columns, types_detected)
    
    # 5. Return preview
    return _json(DataUploadResponse(
        filename=file.filename,
        format=file.filename.split(".")[-1],
        rows_total=len(df),
        rows_preview=df.head(50).to_dict("records"),
        columns_detected=types_detected,
        mapping_suggested=mapping,
        warnings=[]
    ))


@app.post("/api/data/validate")
async def validate_data(
    body: dict,
    p: Principal = Depends(principal)
) -> JSONResponse:
    """Validate mapping against contract and return capabilities."""
    
    mapping = body.get("mapping", {})
    
    # 1. Apply mapping (load temp, apply, validate)
    try:
        bundle = load_and_map(body["filename"], mapping)
    except DataContractError as e:
        return _json({
            "status": "invalid",
            "errors": [{"field": str(e), "type": "contract_error"}]
        })
    
    # 2. Check contract
    errors = check_contract(bundle)
    if errors:
        return _json({
            "status": "invalid",
            "error_count": len(errors),
            "errors": errors
        })
    
    # 3. Build capability matrix
    caps = capability_matrix(bundle)
    
    return _json({
        "status": "valid",
        "rows_total": len(bundle.trade_events),
        "rows_loaded": len(bundle.trade_events),
        "data_capabilities": {
            "coverage": caps.coverage,
            "unavailable": list(caps.unavailable),
            "entities": caps.entities
        },
        "capability_matrix": {
            "hypotheses": {...},
            "signals": {...},
            "features": {...},
            "rule_gaps": [...]
        },
        "errors": [],
        "warnings": []
    })


@app.post("/api/data/confirm")
async def confirm_data(
    body: dict,
    p: Principal = Depends(principal)
) -> JSONResponse:
    """Finalize upload: store in DB, freeze baselines, mark ready."""
    
    filename = body["filename"]
    mapping_version = body["mapping_version"]
    
    # 1. Load & store
    bundle = load_and_map(filename, get_mapping(mapping_version))
    platform.ingest(bundle, p)
    
    # 2. Freeze baseline
    baseline_date = datetime.now(UTC)
    platform.freeze_baseline(baseline_date)
    
    # 3. Mark ready
    platform.mark_data_ready()
    
    # 4. Audit
    platform.store.audit(p.user_id, "DATA_CONFIRMED", filename, {...})
    
    return _json({
        "status": "confirmed",
        "data_id": f"data-{baseline_date.strftime('%Y%m%d')}",
        "rows_stored": len(bundle.trade_events),
        "baseline_frozen_at": baseline_date.isoformat(),
        "ready_for_run": True
    })
```

### 11.2 Frontend CSS Template

```css
/* src/asas/api/static/styles.css additions */

/* ---- Upload Zone ---- */
.upload-zone {
  border: 2px dashed var(--color-grey);
  border-radius: 4px;
  padding: 40px;
  text-align: center;
  cursor: pointer;
  background: var(--color-bg-alt);
  transition: all 200ms ease;
  min-height: 150px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.upload-zone:hover {
  border-color: var(--color-black);
  background: var(--color-white);
}

.upload-zone.active {
  border-color: var(--color-red);
  background: var(--color-red);
  color: var(--color-white);
}

.upload-icon {
  width: 48px;
  height: 48px;
  margin-bottom: 8px;
  stroke: var(--color-grey);
}

.upload-zone.active .upload-icon {
  stroke: var(--color-white);
}

/* ---- Progress Bar ---- */
.progress-wrap {
  margin: 16px 0;
}

.progress-bar {
  width: 100%;
  height: 8px;
  background: var(--color-grey-light);
  border-radius: 4px;
  overflow: hidden;
}

.progress-bar .fill {
  height: 100%;
  background: var(--color-black);
  transition: width 200ms ease;
  display: block;
}

/* ---- Preview Table ---- */
.preview-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  font-family: monospace;
  max-height: 300px;
  overflow-y: auto;
}

.preview-table th {
  background: var(--color-grey-light);
  color: var(--color-text);
  padding: 8px;
  text-align: left;
  font-weight: bold;
  border-bottom: 1px solid var(--color-border);
  position: sticky;
  top: 0;
}

.preview-table td {
  padding: 8px;
  border-bottom: 1px solid var(--color-border);
}

.preview-table tr:nth-child(even) {
  background: var(--color-bg-alt);
}

/* ---- Column Mapper ---- */
.mapper-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 16px;
  margin: 16px 0;
}

.mapper-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
}

.mapper-label {
  flex: 0 0 100px;
  font-weight: bold;
  font-size: 12px;
}

.mapper-select {
  flex: 1;
  padding: 6px;
  border: 1px solid var(--color-border);
  border-radius: 3px;
  background: var(--color-white);
  color: var(--color-text);
}

.mapper-type {
  flex: 0 0 80px;
  text-align: right;
  font-size: 11px;
  color: var(--color-text-muted);
  font-family: monospace;
}

/* ---- Coverage Cards ---- */
.coverage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin: 16px 0;
}

.coverage-card {
  border: 1px solid var(--color-border);
  border-radius: 4px;
  padding: 12px;
  text-align: center;
}

.coverage-card.unavailable {
  border-color: var(--color-red);
  background: rgba(204, 0, 0, 0.05);
}

.coverage-label {
  font-weight: bold;
  font-size: 12px;
  margin-bottom: 6px;
}

.coverage-bar {
  width: 100%;
  height: 6px;
  background: var(--color-grey-light);
  border-radius: 3px;
  overflow: hidden;
  margin: 6px 0;
}

.coverage-bar span {
  display: block;
  height: 100%;
  background: var(--color-black);
}

.coverage-card.unavailable .coverage-bar span {
  background: var(--color-red);
}

.coverage-pct {
  font-size: 14px;
  font-weight: bold;
  margin: 6px 0 0 0;
}

.coverage-warning {
  font-size: 10px;
  color: var(--color-red);
  margin-top: 4px;
}

/* ---- Hypothesis List ---- */
.hypothesis-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 12px 0;
}

.hypothesis-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-left: 3px solid var(--color-black);
}

.hypothesis-item.degraded {
  border-left-color: var(--color-red);
  background: rgba(204, 0, 0, 0.03);
}

/* ---- Error Detail ---- */
.error-detail {
  max-height: 400px;
  overflow-y: auto;
  background: var(--color-bg-alt);
  border: 1px solid var(--color-red);
  border-radius: 4px;
  padding: 8px;
}

.error-item {
  padding: 8px;
  border-bottom: 1px solid var(--color-border);
}

.error-item:last-child {
  border-bottom: none;
}

.error-field {
  color: var(--color-red);
  font-weight: bold;
  font-size: 12px;
  margin-bottom: 4px;
}

.error-msg {
  color: var(--color-text);
  font-size: 12px;
  margin-bottom: 4px;
}

.error-rows {
  font-size: 11px;
  font-family: monospace;
}

/* ---- Details (Expandable) ---- */
details {
  margin: 12px 0;
}

details summary {
  cursor: pointer;
  padding: 8px;
  background: var(--color-grey-light);
  border-radius: 3px;
  font-weight: bold;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 8px;
}

details summary:hover {
  background: var(--color-grey-dark);
  color: var(--color-white);
}

details[open] summary {
  background: var(--color-black);
  color: var(--color-white);
  border-radius: 3px 3px 0 0;
}

details > *:not(summary) {
  padding: 12px 8px;
  border: 1px solid var(--color-border);
  border-top: none;
  border-radius: 0 0 3px 3px;
  background: var(--color-white);
}

/* ---- Buttons ---- */
.btn {
  padding: 8px 16px;
  border: 1px solid var(--color-black);
  border-radius: 3px;
  background: var(--color-white);
  color: var(--color-text);
  cursor: pointer;
  font-weight: bold;
  font-size: 13px;
  transition: all 150ms ease;
}

.btn:hover {
  background: var(--color-black);
  color: var(--color-white);
}

.btn.primary {
  background: var(--color-black);
  color: var(--color-white);
  border-color: var(--color-black);
}

.btn.primary:hover {
  background: var(--color-grey-dark);
}

.btn.danger {
  border-color: var(--color-red);
  color: var(--color-red);
}

.btn.danger:hover {
  background: var(--color-red);
  color: var(--color-white);
}

/* ---- Responsive ---- */
@media (max-width: 768px) {
  .mapper-grid {
    grid-template-columns: 1fr;
  }
  
  .mapper-row {
    flex-direction: column;
    align-items: flex-start;
  }
  
  .mapper-type {
    text-align: left;
  }
  
  .coverage-grid {
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  }
}

/* ---- Dark Mode ---- */
@media (prefers-color-scheme: dark) {
  .upload-zone {
    border-color: var(--color-grey);
    background: var(--color-bg-alt);
  }
  
  .preview-table th {
    background: var(--color-bg-alt);
  }
  
  /* ... rest of dark mode overrides */
}
```

---

## 12. DEBUGGING & TROUBLESHOOTING

### 12.1 CSV Upload Not Working

**Symptom:** File input doesn't trigger upload.

**Steps:**
1. Check browser console (F12) for JavaScript errors
2. Verify file input `accept` attribute includes `.csv,.xlsx,.parquet`
3. Check that `CSVUploader()` is instantiated in `data()` function
4. Verify `/api/data/upload` endpoint exists in `app.py`
5. Test endpoint directly: `curl -X POST -F "file=@test.csv" http://localhost:8000/api/data/upload`

**Common Fixes:**
- Ensure file input is not hidden by CSS (`display: none; → hidden attribute`)
- Check CORS headers (should be set in FastAPI app)
- Verify FormData is not being modified (some frameworks interfere)

### 12.2 Mapping Not Suggested Correctly

**Symptom:** Column mapping shows wrong matches (PRICE → QUANTITY, etc.).

**Steps:**
1. Check that `suggest_mapping()` in backend uses correct matching rules
2. Verify column names in test CSV match expected case (title case, spaces, etc.)
3. Enable debug mode: `?debug=true` in URL to see scoring details

**Common Fixes:**
- Title case matching: "Trade_ID" should match "TRADE_ID"
- Fuzzy matching (levenshtein distance) for typos: "TRAD_ID" → "TRADE_ID"
- If column name is exact match, use that first

### 12.3 Validation Fails Unexpectedly

**Symptom:** `POST /api/data/validate` returns errors, but data looks valid.

**Steps:**
1. Check specific error message in response
2. Look at row numbers listed in error (e.g., rows [412, 515])
3. Export those rows from CSV to inspect: `head -n 516 file.csv | tail -n 5`
4. Check if issue is whitespace, type coercion, null handling

**Common Fixes:**
- Null/empty cells: CSV may have empty string `""` instead of null
- Type coercion: QUANTITY `"1,000"` (comma) vs `1000` (number)
- Dates: `"2026/09/29"` vs `"29/09/2026"` vs `"2026-09-29"`
- Timezone: ensure ENTRY_DATE includes timezone info or specify in mapping

### 12.4 Capability Display Not Rendering

**Symptom:** After validation, capability matrix doesn't show.

**Steps:**
1. Check XHR response in Network tab (should be JSON with `data_capabilities`)
2. Verify `CapabilityDisplay` class is imported in `app.js`
3. Check that response includes nested `capability_matrix` object
4. Look for JavaScript errors in console during instantiation

**Common Fixes:**
- Ensure API response matches expected shape (test against schema in spec)
- Check that `capability_display.render()` is called
- Verify `<details>` elements are created with proper IDs

### 12.5 Styling Looks Wrong (colors, layout)

**Symptom:** Colors are not red/black/grey/white, or layout is broken.

**Steps:**
1. Inspect element (right-click → Inspect)
2. Check computed styles (Colors section)
3. Verify `--color-red`, `--color-black` etc. are defined in `:root`
4. Check that no inline `style="color: ..."` exist (CSP violation)

**Common Fixes:**
- Ensure all color values use CSS variables: `color: var(--color-text)`
- Check dark mode media query is applied: `@media (prefers-color-scheme: dark)`
- For layout issues, verify flexbox/grid is used, not float/positioning

### 12.6 Performance Is Slow

**Symptom:** CSV parsing takes >10s for 100k rows.

**Steps:**
1. Use Chrome DevTools Performance tab (record, then parse)
2. Check if bottleneck is parsing or rendering
3. Look at JavaScript Main Thread (blue blocks = JS execution)
4. If rendering is slow, check if all 100k rows are rendered at once

**Common Fixes:**
- Lazy-render table: show first 50 rows, load more on scroll
- Use debounce on mapper validation (don't validate on every keystroke)
- Cache capability results in IndexedDB for repeated queries
- Consider chunked upload for files >50MB

---

## Quick Start for Coding Agent

**You have 12 sections above. Here's the order to implement:**

1. **First:** Read section 1 (Architecture) and section 5 (API endpoints) to understand full flow
2. **Second:** Section 11 has code templates — copy them, adjust for your codebase
3. **Third:** Implement backend endpoints (section 5 + 11.1) — test with curl
4. **Fourth:** Implement frontend components (section 6 + 11.2) — render step-by-step
5. **Fifth:** Add styles (section 11.2 CSS) — verify red/black/grey/white only
6. **Sixth:** Run section 7 tests — fix failures
7. **Seventh:** Performance (section 8) — optimize if needed
8. **Eighth:** Polish (section 10 phase 4) — docs, help text, accessibility

**Files to create/modify:**

```
CREATE:
  src/asas/api/static/components/csv-uploader.js
  src/asas/api/static/components/column-mapper.js
  src/asas/api/static/components/capability-display.js
  src/asas/api/static/utils/fetch-helper.js
  src/asas/api/static/utils/format.js
  tests/test_data_upload.py
  tests/frontend/csv-flow.e2e.js

MODIFY:
  src/asas/api/app.py               (add 3 endpoints)
  src/asas/api/static/app.js        (rewrite data() function)
  src/asas/api/static/styles.css    (add 400 lines)
  config/asas.toml                  (if new config needed)
```

**Success Criteria:**
- ✓ CSV upload works end-to-end in browser
- ✓ File preview shows first 50 rows
- ✓ Column mapping is suggested and user-editable
- ✓ Validation runs and shows capability matrix
- ✓ Confirm stores data and marks ready
- ✓ All colors are red/black/grey/white only
- ✓ No CSP violations (console shows no warnings)
- ✓ Tests pass (unit, e2e, accessibility)
- ✓ Performance: CSV parse < 5s for 100k rows
- ✓ Error handling: all error cases show user-friendly messages

Good luck. This document is your single source of truth. Reference it as you build.

