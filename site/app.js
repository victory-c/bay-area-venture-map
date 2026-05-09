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

document.addEventListener("alpine:init", () => {
  Alpine.data("vcApp", () => ({
    firms: [],
    search: "",
    stageFilter: new Set(),
    sectorFilter: new Set(),
    aumMin: 0,
    aumMax: 100_000_000_000,
    sortKey: "aum_usd",
    sortDir: -1,
    selectedFirm: null,
    map: null,
    markers: {},

    async init() {
      this.initSplitters();
      const resp = await fetch("firms.json");
      const data = await resp.json();
      // Precompute the searchable "hay" string per firm once at load time.
      // Drops per-keystroke string-build cost from O(N × per-firm-build) to
      // a single .includes() lookup against a precomputed string. The leading
      // "_" marks it as a non-data field; we strip it from the JSON download.
      data.firms.forEach((f) => {
        f._hay = [
          f.name,
          f.notes,
          ...(f.sectors || []).map((s) => SECTOR_LABELS[s] || s),
          ...(f.partners || []).map((p) => p.name),
          ...(f.recent_portfolio_sample || []).map((d) => d.company),
        ].filter(Boolean).join(" ").toLowerCase();
      });
      // City-centroid fallback geocoding stamps dozens of firms onto the
      // exact same lat/lng (e.g. ~200 firms at the SF centroid). Without
      // spreading them, those pins render as one unreadable pile of stacked
      // names. Distribute colocated firms in a deterministic spiral so the
      // layout is stable across reloads. Radius scales with group size so
      // a SF pile-up gets ~2 km of breathing room while a small Marin
      // cluster only spreads ~150 m.
      this.spreadColocatedFirms(data.firms);
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

    // Spread firms that share the exact same lat/lng (an artefact of the
    // scraper's city-centroid geocoding fallback) into a deterministic
    // spiral pattern around their original point. Radius grows with group
    // size: a 200-firm SF pile-up spreads over ~2 km, a 5-firm Marin one
    // over ~250 m. Order is stable per reload because we sort the group
    // by sec_crd before laying out. Real street-geocoded firms (groups of
    // size 1) are untouched.
    spreadColocatedFirms(firms) {
      const groups = new Map();
      firms.forEach((f) => {
        if (f.lat == null || f.lng == null) return;
        const key = `${f.lat.toFixed(6)},${f.lng.toFixed(6)}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(f);
      });
      groups.forEach((group) => {
        if (group.length < 2) return;
        group.sort((a, b) => (a.sec_crd || a.id).localeCompare(b.sec_crd || b.id));
        const centerLat = group[0].lat;
        const centerLng = group[0].lng;
        // ~0.001° lat ≈ 111 m. Cap radius at ~2 km even for huge groups.
        const radius = Math.min(0.018, 0.0015 * Math.sqrt(group.length));
        // Latitude correction so spiral looks circular, not elongated, at
        // SF/Bay Area latitudes (cos(37.7°) ≈ 0.79).
        const lngScale = 1 / Math.cos((centerLat * Math.PI) / 180);
        group.forEach((f, i) => {
          if (i === 0) return; // anchor stays at the centroid
          // Sunflower / phyllotaxis spiral keeps points evenly spaced.
          const angle = i * 2.39996;            // golden angle in radians
          const r = radius * Math.sqrt(i / group.length);
          f.lat = centerLat + Math.sin(angle) * r;
          f.lng = centerLng + Math.cos(angle) * r * lngScale;
        });
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
          // rAF-throttle: pointermove fires at the input device's poll rate
          // (often >120 Hz on modern mice/trackpads). Each fire calls
          // map.invalidateSize() which redraws all visible tiles. Coalesce
          // them into one update per animation frame.
          let rafPending = false;
          let latestEv = null;
          const onMove = (ev) => {
            latestEv = ev;
            if (rafPending) return;
            rafPending = true;
            requestAnimationFrame(() => {
              rafPending = false;
              if (!latestEv) return;
              const delta = (latestEv[clientCoord] - start) * sign;
              const newSize = Math.max(120, Math.min(limit - 160, startSize + delta));
              if (isPanel) {
                target.style[dim] = newSize + "px";
              } else {
                target.style.flex = `0 0 ${newSize}px`;
                target.style[dim] = newSize + "px";
              }
              if (axis === "x" && this.map) this.map.invalidateSize();
            });
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
      // Guard: lite firms may lack stages if firms.json predates the schema
      // fix. Without this, Alpine throws on every reactive read and the page
      // locks up under exception spam.
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

    get visibleFirms() {
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
      this.map = L.map("map", { scrollWheelZoom: true, preferCanvas: true })
        .setView([37.65, -122.30], 10);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "© OpenStreetMap contributors",
      }).addTo(this.map);
      this.refreshMarkers();
      this.fitVisibleBounds();
      // Re-render pins on every pan/zoom so only those inside the visible
      // viewport are kept in the DOM. Without this, all 678 named pins
      // stayed mounted regardless of where the user was looking and the
      // page slowed under its own marker count. moveend fires once after
      // the gesture settles, not every frame, so the cost is bounded.
      this.map.on("moveend", () => this.refreshMarkers());
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
      // Viewport culling: only keep pins for firms inside the current map
      // bounds. This is what stops 678 named pins from all mounting at once.
      // Padded by 20% so a small pan doesn't churn pins at the edges.
      const bounds = this.map.getBounds().pad(0.2);
      const inViewport = this.visibleFirms.filter(
        (f) => f.lat != null && f.lng != null && bounds.contains([f.lat, f.lng])
      );
      const visibleIds = new Set(inViewport.map((f) => f.id));

      // Remove markers for firms that scrolled out of view or were filtered.
      Object.entries(this.markers).forEach(([id, marker]) => {
        if (!visibleIds.has(id)) {
          this.map.removeLayer(marker);
          delete this.markers[id];
        }
      });

      // Add markers for firms newly entering the viewport.
      inViewport.forEach((f) => {
        if (this.markers[f.id]) return;
        const bucket = this.aumBucket(f.aum_usd);
        const label = f.name.length > 14 ? f.name.slice(0, 13) + "…" : f.name;
        const liteCls = f.tier === "lite" ? " pin-lite" : "";
        const icon = L.divIcon({
          className: `pin pin-${bucket}${liteCls}`,
          html: `<span>${label}</span>`,
          iconSize: [110, 22],
          iconAnchor: [55, 11],
        });
        const tooltipBody = f.aum_usd
          ? `<strong>${f.name}</strong><br>$${this.formatShort(f.aum_usd)}`
          : `<strong>${f.name}</strong>`;
        const stagesPart = (f.stages && f.stages.length)
          ? ` · ${f.stages.map(s => this.prettyStage(s)).join(", ")}`
          : "";
        const marker = L.marker([f.lat, f.lng], { icon, title: f.name })
          .bindTooltip(tooltipBody + stagesPart, { direction: "top", opacity: 0.95 })
          .on("click", () => this.select(f));
        marker.addTo(this.map);
        this.markers[f.id] = marker;
      });
    },

    downloadJson() {
      // Strip the precomputed _hay search index from the export; it's an
      // implementation detail that bloats the file and isn't part of the
      // schema users expect.
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
