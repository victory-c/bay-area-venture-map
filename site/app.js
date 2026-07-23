// Sand Hill Road VCs — front-end app.
// Loads firms.json, renders Leaflet map, drives table & filter reactivity.

const STAGE_LABELS = {
  pre_seed: "Pre-seed",
  seed: "Seed",
  series_a: "Series A",
  series_b: "Series B",
  series_c: "Series C",
  series_d: "Series D",
  series_e: "Series E",
  growth: "Growth",
  late: "Late-stage",
  buyout: "Buyout",
};

const SECTOR_LABELS = {
  enterprise_saas: "Enterprise SaaS",
  consumer: "Consumer",
  fintech: "Fintech",
  ai_infra: "AI / Infra",
  crypto: "Crypto",
  healthcare: "Healthcare",
  bio: "Bio",
  therapeutics: "Therapeutics",
  climate: "Climate",
  deep_tech: "Deep tech",
  dev_tools: "Dev tools",
  security: "Security",
  data: "Data",
  infra: "Infra",
  industrial: "Industrial",
  marketplace: "Marketplace",
  consumer_social: "Consumer / Social",
  defense: "Defense",
  gaming: "Gaming",
  mobile: "Mobile",
  cross_border: "Cross-border",
  cloud: "Cloud",
  generalist: "Generalist",
  governance: "Governance",
};

// Escape untrusted text before interpolating into HTML strings (Leaflet
// divIcon `html` and bindTooltip render their content as HTML, not text).
// Firm names originate from the SEC Form ADV bulk scrape and are
// filer-controlled, so they must never be treated as trusted markup.
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

document.addEventListener("alpine:init", () => {
  Alpine.data("vcApp", () => ({
    firms: [],
    // Computed once per relevant state change by recomputeVisible(), instead
    // of being a getter that the table, map, header count, CSV export, etc.
    // would each independently re-run (re-filtering + re-sorting all firms
    // every time any of them was read during a reactive tick).
    visibleFirms: [],
    tableLimit: 75,
    search: "",
    stageFilter: new Set(),
    sectorFilter: new Set(),
    aumMin: 0,
    aumMax: 100_000_000_000,
    sortKey: "aum_usd",
    sortDir: -1,
    selectedFirm: null,
    loading: true,
    loadError: null,
    map: null,
    markers: {},

    async init() {
      this.initSplitters();

      // Re-render markers exactly once per recompute (see recomputeVisible),
      // not on every template read of visibleFirms.
      this.$watch("selectedFirm", (firm) => {
        if (firm && this.map && firm.lat && firm.lng) {
          this.map.flyTo([firm.lat, firm.lng], 16, { duration: 0.6 });
        }
      });

      // Filter-affecting inputs reset the table's paging window; sorting
      // just re-orders the same set, so it leaves the window alone.
      const onFilterChange = () => {
        this.tableLimit = 75;
        this.recomputeVisible();
      };
      this.$watch("search", onFilterChange);
      this.$watch("stageFilter", onFilterChange);
      this.$watch("sectorFilter", onFilterChange);
      this.$watch("aumMin", onFilterChange);
      this.$watch("aumMax", onFilterChange);
      this.$watch("sortKey", () => this.recomputeVisible());
      this.$watch("sortDir", () => this.recomputeVisible());

      await this.loadData();
    },

    async loadData() {
      this.loading = true;
      this.loadError = null;
      try {
        const resp = await fetch("firms.json");
        if (!resp.ok) throw new Error(`request failed (HTTP ${resp.status})`);
        const data = await resp.json();
        // Stamp a lowercase search string on each firm once, so the visible-
        // firms filter is a single .includes() call instead of rebuilding +
        // lowercasing an array of strings for every firm on every keystroke.
        data.firms.forEach((f) => {
          f._hay = [
            f.name,
            f.notes,
            ...(f.sectors || []).map((s) => SECTOR_LABELS[s] || s),
            ...(f.partners || []).map((p) => p.name),
            ...(f.recent_portfolio_sample || []).map((d) => d.company),
          ].filter(Boolean).join(" ").toLowerCase();
        });
        this.firms = data.firms;
        this.tableLimit = 75;
        this.recomputeVisible();
        if (this.map) {
          this.fitVisibleBounds();
        } else {
          this.renderMap();
        }
      } catch (err) {
        this.loadError = err instanceof Error ? err.message : String(err);
      } finally {
        this.loading = false;
      }
    },

    // Wire every .splitter / .panel-resizer element. Each one resizes its
    // *previous* sibling on the relevant axis, persists the size in
    // localStorage, and tells Leaflet to redraw on horizontal changes so
    // tiles fill the new map area.
    initSplitters() {
      const panel = document.querySelector(".panel");
      const handles = [
        ...document.querySelectorAll(".splitter"),
        ...document.querySelectorAll(".panel-resizer"),
      ];
      handles.forEach((handle) => {
        const isPanel = handle.classList.contains("panel-resizer");
        const axis = handle.classList.contains("splitter-y") ? "y" : "x";
        const dim = axis === "x" ? "width" : "height";
        const offDim = axis === "x" ? "offsetWidth" : "offsetHeight";
        const clientCoord = axis === "x" ? "clientX" : "clientY";
        // Panel grows to the LEFT (resizing its width by dragging its left edge),
        // so dragging right SHRINKS it. All other splitters resize the previous
        // sibling, where dragging in the +axis direction GROWS it.
        const sign = isPanel ? -1 : 1;
        const target = isPanel ? panel : handle.previousElementSibling;
        if (!target) return;
        const key = `splitter:${handle.dataset.resize || "anon"}`;

        // Restore saved size on load.
        const saved = localStorage.getItem(key);
        if (saved) {
          if (isPanel) {
            target.style[dim] = saved;
          } else {
            target.style.flex = `0 0 ${saved}`;
          }
        }

        const onDown = (e) => {
          if (e.button !== undefined && e.button !== 0) return;
          e.preventDefault();
          const start = e[clientCoord];
          const startSize = target[offDim];
          handle.classList.add("is-active");
          document.body.classList.add("is-resizing", `is-resizing-${axis}`);
          try { handle.setPointerCapture(e.pointerId); } catch {}

          const limit = axis === "x" ? window.innerWidth : window.innerHeight;
          const onMove = (ev) => {
            const delta = (ev[clientCoord] - start) * sign;
            const newSize = Math.max(120, Math.min(limit - 160, startSize + delta));
            if (isPanel) {
              target.style[dim] = newSize + "px";
            } else {
              target.style.flex = `0 0 ${newSize}px`;
              target.style[dim] = newSize + "px";
            }
            if (axis === "x" && this.map) this.map.invalidateSize();
          };
          const onUp = () => {
            document.removeEventListener("pointermove", onMove);
            document.removeEventListener("pointerup", onUp);
            handle.classList.remove("is-active");
            document.body.classList.remove("is-resizing", `is-resizing-${axis}`);
            const finalSize = target.style[dim];
            if (finalSize) localStorage.setItem(key, finalSize);
            if (axis === "x" && this.map) this.map.invalidateSize();
          };
          document.addEventListener("pointermove", onMove);
          document.addEventListener("pointerup", onUp);
        };
        handle.addEventListener("pointerdown", onDown);

        // Double-click to reset to the default size.
        handle.addEventListener("dblclick", () => {
          target.style[dim] = "";
          target.style.flex = "";
          localStorage.removeItem(key);
          if (axis === "x" && this.map) this.map.invalidateSize();
        });
      });
    },

    get allStages() {
      const seen = new Set();
      this.firms.forEach((f) => (f.stages || []).forEach((s) => seen.add(s)));
      return Array.from(seen).sort((a, b) =>
        Object.keys(STAGE_LABELS).indexOf(a) - Object.keys(STAGE_LABELS).indexOf(b),
      );
    },

    get allSectors() {
      const counts = new Map();
      this.firms.forEach((f) => (f.sectors || []).forEach((s) => counts.set(s, (counts.get(s) || 0) + 1)));
      return Array.from(counts.entries())
        .sort((a, b) => b[1] - a[1])
        .map(([s]) => s);
    },

    // Filters + sorts the full firm list exactly once per relevant state
    // change (called from the $watch handlers set up in init(), and once
    // right after data loads). Writes the result into the plain
    // `visibleFirms` array property rather than returning it, so every
    // consumer (table, map, header count, CSV/JSON export) reads a cached
    // array instead of re-running this work themselves.
    recomputeVisible() {
      const q = this.search.trim().toLowerCase();
      const stageF = this.stageFilter;
      const sectorF = this.sectorFilter;
      const aumMin = this.aumMin;
      const aumMax = this.aumMax;

      // The default AUM range covers the full slider extent; treat any
      // narrowing as "the user wants only firms with AUM in this range",
      // which means firms with unknown AUM (lite SEC records) get hidden.
      // At the default extent we keep them visible.
      const aumNarrowed = aumMin > 0 || aumMax < 100_000_000_000;
      let firms = this.firms.filter((f) => {
        if (stageF.size && !(f.stages || []).some((s) => stageF.has(s))) return false;
        if (sectorF.size && !(f.sectors || []).some((s) => sectorF.has(s))) return false;
        if (f.aum_usd == null) {
          if (aumNarrowed) return false;
        } else if (f.aum_usd < aumMin || f.aum_usd > aumMax) {
          return false;
        }
        if (q && !(f._hay || "").includes(q)) return false;
        return true;
      });

      const k = this.sortKey;
      const d = this.sortDir;
      firms.sort((a, b) => {
        const av = a[k] ?? -Infinity;
        const bv = b[k] ?? -Infinity;
        if (typeof av === "string") return d * av.localeCompare(bv);
        return d * (av - bv);
      });

      this.visibleFirms = firms;
      // Single marker update per state change (diffs by id; see refreshMarkers).
      this.refreshMarkers();
    },

    // Bounded slice for the table, independent of how many firms match the
    // filters — keeps initial render + re-render cost flat instead of
    // scaling with the full result set.
    get visibleRows() {
      return this.visibleFirms.slice(0, this.tableLimit);
    },

    loadMore() {
      this.tableLimit += 75;
    },

    sortBy(key) {
      if (this.sortKey === key) {
        this.sortDir *= -1;
      } else {
        this.sortKey = key;
        this.sortDir = key === "name" ? 1 : -1;
      }
    },

    toggleStage(stage) {
      const next = new Set(this.stageFilter);
      next.has(stage) ? next.delete(stage) : next.add(stage);
      this.stageFilter = next;
    },

    toggleSector(sector) {
      const next = new Set(this.sectorFilter);
      next.has(sector) ? next.delete(sector) : next.add(sector);
      this.sectorFilter = next;
    },

    resetFilters() {
      this.search = "";
      this.stageFilter = new Set();
      this.sectorFilter = new Set();
      this.aumMin = 0;
      this.aumMax = 100_000_000_000;
    },

    select(firm) {
      this.selectedFirm = firm;
    },

    formatShort(n) {
      if (n == null || isNaN(n)) return "—";
      const abs = Math.abs(n);
      if (abs >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + "B";
      if (abs >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
      if (abs >= 1e3) return (n / 1e3).toFixed(0) + "k";
      return n.toString();
    },

    aumBucket(aum) {
      if (aum == null) return "0";
      if (aum >= 50e9) return "4";
      if (aum >= 10e9) return "3";
      if (aum >= 1e9) return "2";
      return "1";
    },

    prettyStage(s) {
      return STAGE_LABELS[s] || s;
    },

    prettySector(s) {
      return SECTOR_LABELS[s] || s;
    },

    renderMap() {
      // Centered on Sand Hill Rd, zoomed to show the whole cluster.
      this.map = L.map("map", { scrollWheelZoom: true }).setView([37.65, -122.30], 10);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "© OpenStreetMap contributors",
      }).addTo(this.map);
      this.refreshMarkers();
      this.fitVisibleBounds();
    },

    fitVisibleBounds() {
      const markers = Object.values(this.markers);
      if (!markers.length) return;
      const bounds = L.featureGroup(markers).getBounds();
      if (bounds.isValid()) {
        this.map.fitBounds(bounds, { padding: [40, 40], maxZoom: 13 });
      }
    },

    refreshMarkers() {
      if (!this.map) return;
      const visibleIds = new Set(this.visibleFirms.map((f) => f.id));

      // Remove markers for firms no longer visible
      Object.entries(this.markers).forEach(([id, marker]) => {
        if (!visibleIds.has(id)) {
          this.map.removeLayer(marker);
          delete this.markers[id];
        }
      });

      // Add markers for visible firms
      this.visibleFirms.forEach((f) => {
        if (!f.lat || !f.lng) return;
        if (this.markers[f.id]) return;
        const bucket = this.aumBucket(f.aum_usd);
        const label = f.name.length > 14 ? f.name.slice(0, 13) + "…" : f.name;
        const liteCls = f.tier === "lite" ? " pin-lite" : "";
        const icon = L.divIcon({
          className: `pin pin-${bucket}${liteCls}`,
          html: `<span>${escapeHtml(label)}</span>`,
          iconSize: [110, 22],
          iconAnchor: [55, 11],
        });
        // Pin already shows the (possibly truncated) name. Only bind a tooltip
        // when it actually adds info — full name if truncated, AUM, or stages.
        // Skip Leaflet's `title:` option entirely so the native browser tooltip
        // doesn't fire a third copy of the name after a 1s hover.
        const parts = [];
        if (f.name.length > 14) parts.push(`<strong>${escapeHtml(f.name)}</strong>`);
        if (f.aum_usd) parts.push(`$${this.formatShort(f.aum_usd)}`);
        if (f.stages && f.stages.length) {
          parts.push(f.stages.map((s) => this.prettyStage(s)).join(", "));
        }
        const marker = L.marker([f.lat, f.lng], { icon });
        if (parts.length) {
          marker.bindTooltip(parts.join(" · "), { direction: "top", opacity: 0.95 });
        }
        marker.on("click", () => this.select(f));
        marker.addTo(this.map);
        this.markers[f.id] = marker;
      });
    },

    downloadJson() {
      // Strip the internal _hay search index before exporting.
      const cleanFirms = this.firms.map(({ _hay, ...rest }) => rest);
      const blob = new Blob(
        [JSON.stringify({ firm_count: cleanFirms.length, firms: cleanFirms }, null, 2)],
        { type: "application/json" },
      );
      const url = URL.createObjectURL(blob);
      const a = Object.assign(document.createElement("a"), {
        href: url,
        download: "sand-hill-vcs.json",
      });
      a.click();
      URL.revokeObjectURL(url);
    },

    copyCsv() {
      const rows = [
        ["name", "address", "website", "aum_usd", "stages", "sectors", "typical_check", "deal_velocity"],
        ...this.visibleFirms.map((f) => [
          f.name,
          f.address,
          f.website ?? "",
          f.aum_usd ?? "",
          (f.stages || []).join("|"),
          (f.sectors || []).join("|"),
          f.check_size?.typical ?? "",
          f.deal_velocity_per_year ?? "",
        ]),
      ];
      const csv = rows
        .map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(","))
        .join("\n");
      navigator.clipboard.writeText(csv).then(() => {
        // Soft visual feedback via title
        document.title = "✓ Copied · " + document.title;
        setTimeout(() => (document.title = document.title.replace(/^✓ Copied · /, "")), 1500);
      });
    },
  }));
});
