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

// Related sectors, used to suggest "adjacent tags" when a stage+sector filter
// combination returns zero firms. Only the marquee (rich-tier) firms carry
// sector/stage tags today, so narrow combinations collapse to an empty set;
// these adjacencies give the founder a one-click way out. Suggestions are
// always re-counted against the live data, so an adjacency that would itself
// return nothing is never shown.
const SECTOR_ADJACENCY = {
  enterprise_saas: ["ai_infra", "dev_tools", "fintech", "data", "cloud", "security"],
  consumer: ["consumer_social", "marketplace", "mobile", "gaming", "fintech"],
  fintech: ["enterprise_saas", "consumer", "crypto", "marketplace"],
  ai_infra: ["dev_tools", "infra", "data", "deep_tech", "enterprise_saas", "cloud"],
  crypto: ["fintech", "ai_infra", "infra"],
  healthcare: ["bio", "therapeutics"],
  bio: ["healthcare", "therapeutics", "deep_tech"],
  therapeutics: ["bio", "healthcare"],
  climate: ["deep_tech", "industrial", "infra"],
  deep_tech: ["ai_infra", "industrial", "climate", "defense"],
  dev_tools: ["ai_infra", "enterprise_saas", "infra", "security"],
  security: ["enterprise_saas", "dev_tools", "ai_infra", "infra"],
  data: ["ai_infra", "enterprise_saas", "infra"],
  infra: ["ai_infra", "cloud", "dev_tools", "enterprise_saas"],
  cloud: ["infra", "ai_infra", "enterprise_saas", "dev_tools"],
  industrial: ["climate", "deep_tech", "defense"],
  defense: ["deep_tech", "industrial", "security", "ai_infra"],
  gaming: ["consumer", "consumer_social", "mobile"],
  marketplace: ["consumer", "fintech", "consumer_social"],
  consumer_social: ["consumer", "marketplace", "gaming", "mobile"],
  mobile: ["consumer", "consumer_social", "gaming"],
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
    search: "",
    stageFilter: new Set(),
    sectorFilter: new Set(),
    aumMin: 0,
    aumMax: 100_000_000_000,
    verifiedOnly: false,
    sortKey: "aum_usd",
    sortDir: -1,
    selectedFirm: null,
    map: null,
    markers: {},

    async init() {
      this.initSplitters();
      const resp = await fetch("firms.json");
      const data = await resp.json();
      // Stamp a lowercase search string on each firm once, so the visibleFirms
      // filter is a single .includes() call instead of rebuilding + lowercasing
      // an array of strings for every firm on every keystroke.
      data.firms.forEach((f) => {
        f._hay = [
          f.name,
          f.notes,
          ...(f.sectors || []).map((s) => SECTOR_LABELS[s] || s),
          ...(f.partners || []).map((p) => p.name),
          ...(f.recent_portfolio_sample || []).map((d) => d.company),
        ].filter(Boolean).join(" ").toLowerCase();
        // Stamp Form D fund-raising activity once (recency is "now"-relative
        // but stable for the page session), so table rows read a cached value.
        f._act = this.fundActivity(f);
      });
      this.firms = data.firms;
      this.renderMap();
      // Re-render markers whenever the visible set changes
      this.$watch("visibleFirms", () => this.refreshMarkers());
      this.$watch("selectedFirm", (firm) => {
        if (firm && this.map && firm.lat && firm.lng) {
          this.map.flyTo([firm.lat, firm.lng], 16, { duration: 0.6 });
        }
      });
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

    // Single source of truth for "does this firm pass a filter set?". Shared by
    // visibleFirms and the empty-state suggestion counters so the two can never
    // drift. `opts` overrides the live filter state (used to count hypothetical
    // "what if I swapped the sector" results).
    matches(f, opts = {}) {
      const stageF = opts.stageF ?? this.stageFilter;
      const sectorF = opts.sectorF ?? this.sectorFilter;
      const aumMin = opts.aumMin ?? this.aumMin;
      const aumMax = opts.aumMax ?? this.aumMax;
      const q = opts.q ?? this.search.trim().toLowerCase();

      if (stageF.size && !(f.stages || []).some((s) => stageF.has(s))) return false;
      if (sectorF.size && !(f.sectors || []).some((s) => sectorF.has(s))) return false;
      // "Verified only": AI-inferred firms don't satisfy sector/stage filters
      // (their tags aren't hand-checked), but still appear when neither filter
      // is active. Only the 25 hand-curated firms are treated as verified.
      const verifiedOnly = opts.verifiedOnly ?? this.verifiedOnly;
      if (verifiedOnly && f.inferred && (stageF.size || sectorF.size)) return false;
      // The default AUM range covers the full slider extent; treat any
      // narrowing as "the user wants only firms with AUM in this range",
      // which means firms with unknown AUM (lite SEC records) get hidden.
      // At the default extent we keep them visible.
      const aumNarrowed = aumMin > 0 || aumMax < 100_000_000_000;
      if (f.aum_usd == null) {
        if (aumNarrowed) return false;
      } else if (f.aum_usd < aumMin || f.aum_usd > aumMax) {
        return false;
      }
      if (q && !(f._hay || "").includes(q)) return false;
      return true;
    },

    get visibleFirms() {
      let firms = this.firms.filter((f) => this.matches(f));

      const k = this.sortKey;
      const d = this.sortDir;
      firms.sort((a, b) => {
        const av = a[k] ?? -Infinity;
        const bv = b[k] ?? -Infinity;
        if (typeof av === "string") return d * av.localeCompare(bv);
        return d * (av - bv);
      });
      return firms;
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
      this.verifiedOnly = false;
    },

    get hasActiveFilters() {
      return (
        this.search.trim() !== "" ||
        this.stageFilter.size > 0 ||
        this.sectorFilter.size > 0 ||
        this.aumMin > 0 ||
        this.aumMax < 100_000_000_000
      );
    },

    // Adjacent sectors that WOULD return firms if swapped in for the current
    // sector filter (keeping every other active filter). Powers the empty-state
    // "try adjacent tags" fallback. Never suggests a dead end — each candidate
    // is re-counted against live data and dropped if it yields nothing.
    get sectorSuggestions() {
      if (!this.sectorFilter.size) return [];
      const active = this.sectorFilter;
      const candidates = new Set();
      active.forEach((s) =>
        (SECTOR_ADJACENCY[s] || []).forEach((a) => {
          if (!active.has(a)) candidates.add(a);
        }),
      );
      const out = [];
      candidates.forEach((sector) => {
        const count = this.firms.filter((f) =>
          this.matches(f, { sectorF: new Set([sector]) }),
        ).length;
        if (count > 0) out.push({ sector, count });
      });
      return out.sort((a, b) => b.count - a.count).slice(0, 6);
    },

    // True when dropping just the stage (resp. sector) filter would surface at
    // least one firm — so the empty-state only offers the quick-fix that helps.
    get canClearStageHelp() {
      return (
        this.stageFilter.size > 0 &&
        this.firms.some((f) => this.matches(f, { stageF: new Set() }))
      );
    },

    get canClearSectorHelp() {
      return (
        this.sectorFilter.size > 0 &&
        this.firms.some((f) => this.matches(f, { sectorF: new Set() }))
      );
    },

    applySectorSuggestion(sector) {
      this.sectorFilter = new Set([sector]);
    },

    clearStage() {
      this.stageFilter = new Set();
    },

    clearSector() {
      this.sectorFilter = new Set();
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

    // Fund-raising activity derived from the firm's most recent Form D — the
    // notice a fund files when it closes/raises capital. Recency is a much
    // better "is this firm actively deploying?" proxy than the annual Form ADV
    // compliance filing. Returns null when the firm has no Form D on record.
    fundActivity(firm) {
      const iso = firm.form_d_latest_filing_date;
      if (!iso) return null;
      const d = new Date(iso);
      if (isNaN(d.getTime())) return null;
      const now = new Date();
      const months =
        (now.getFullYear() - d.getFullYear()) * 12 + (now.getMonth() - d.getMonth());
      let tone;
      if (months <= 18) tone = "hot";
      else if (months <= 48) tone = "warm";
      else tone = "cool";
      return {
        tone,
        months,
        date: d.toLocaleString("en-US", { month: "short", year: "numeric" }),
        count: firm.form_d_total_filings || 0,
      };
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
      const cleanFirms = this.firms.map(({ _hay, _act, ...rest }) => rest);
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
