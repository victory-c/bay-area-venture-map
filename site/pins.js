// Sand Hill Road VCs — map pins drawn on a single canvas.
//
// Why a canvas: the previous implementation gave every firm its own
// L.divIcon (a box-shadowed flex <div>). With ~700 firms, every zoom frame
// made Leaflet rewrite ~700 inline transforms and the browser re-raster ~700
// composited layers, and each filter change created or removed hundreds of
// DOM nodes. That was the map's lag (measured: 54–61 ms per wheel step,
// ~26–30 ms per flyTo frame, ~240 ms to add or remove all pins). One canvas
// costs one transform write per frame plus a draw that stays cheap at any
// zoom and any firm count.
//
// What it draws: one "site" per distinct coordinate. The SEC-sourced lite
// firms are geocoded to city centroids, so 695 firms collapse to ~53 points
// and 642 of them used to sit invisibly under another pin. A site is a dot
// coloured by its top firm's AUM bucket — solid for hand-curated rich firms,
// a ring for lite ones — that grows with log2(n) and shows the firm count.
// Labels are placed by priority with collision avoidance so they never
// overlap, and every site in view is labelled once zoomed in. Hover shows one
// reusable DOM tooltip; a click selects a single firm or hands a multi-firm
// site to the app.
//
// Nothing here renders firm-controlled strings as HTML: labels go through
// ctx.fillText and the tooltip through textContent.

(function () {
  "use strict";

  if (typeof L === "undefined") {
    // Leaflet failed to load (CDN outage, SRI mismatch): leave the table
    // working and let app.js report the missing map.
    console.error("pins.js: Leaflet is not loaded; the map is unavailable.");
    return;
  }

  const DPR_MAX = 2;
  const DOT_R_RICH = 5;
  const DOT_R_LITE = 4;
  const CHIP_H = 18;
  const CHIP_PAD_X = 7;
  const CHIP_GAP = 4; // px between the dot's edge and its chip
  const LABEL_MAX_W = 150; // px of label text before it is cut with an ellipsis
  const GRID = 48; // collision grid cell, px
  const HUB_MIN = 10; // sites with at least this many firms are labelled "City · N"
  const CLUSTER_PX = 30; // sites whose dots would overlap at this distance merge
  const CLUSTER_MAX_ZOOM = 16.5; // beyond this every site stands alone
  const FAN_MAX = 12; // same-address stacks up to this size fan out on click
  const FAN_PITCH = 24; // vertical spacing of fanned chips
  // Scratch space reused by label placement so a dense frame allocates nothing
  // per candidate: eight candidate spots and two rectangles.
  const SPOTS = Array.from({ length: 8 }, () => [0, 0]);
  const DOT_RECT = { x: 0, y: 0, w: 0, h: 0 };
  const TRY_RECT = { x: 0, y: 0, w: 0, h: 0 };

  // Group key for coincident coordinates. Rounded so floating-point noise in
  // a future firms.json refresh cannot split a stack into two sites.
  function siteKey(lat, lng) {
    return Number(lat).toFixed(5) + "," + Number(lng).toFixed(5);
  }

  // The city segment of a US address ("2800 Sand Hill Rd, Menlo Park, CA
  // 94025" → "Menlo Park"). Empty when the address does not have that shape.
  function cityOf(address) {
    const parts = String(address || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (parts.length < 2) return "";
    const city = parts[parts.length - 2];
    return /^[A-Za-z][A-Za-z .'-]{1,40}$/.test(city) ? city : "";
  }

  // Higher sorts first when choosing which labels to place. Rich firms beat
  // lite hubs beat lite singles; within a tier, bigger stacks then bigger AUM.
  function priority(s) {
    const tier = s.rich ? 3 : s.n > 1 ? 2 : 1;
    return tier * 1e14 + Math.min(s.n, 999) * 1e11 + Math.min(s.aum || 0, 1e11 - 1);
  }

  function roundRectPath(ctx, x, y, w, h, r) {
    if (typeof ctx.roundRect === "function") {
      ctx.roundRect(x, y, w, h, r);
      return;
    }
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function sameFocus(a, b) {
    if (!a || !b) return a === b;
    return a.kind === b.kind && a.value === b.value;
  }

  function intersects(a, b) {
    return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
  }

  // Uniform grid over the canvas so each label candidate is tested against a
  // handful of neighbours instead of every placed rectangle.
  function gridCells(rect, fn) {
    const x0 = Math.floor(rect.x / GRID);
    const x1 = Math.floor((rect.x + rect.w) / GRID);
    const y0 = Math.floor(rect.y / GRID);
    const y1 = Math.floor((rect.y + rect.h) / GRID);
    for (let cy = y0; cy <= y1; cy++) {
      for (let cx = x0; cx <= x1; cx++) {
        if (fn(((cy + 4096) << 13) | (cx + 4096))) return true;
      }
    }
    return false;
  }

  function gridOverlaps(grid, rect, ignore) {
    return gridCells(rect, (key) => {
      const list = grid.get(key);
      if (!list) return false;
      for (let i = 0; i < list.length; i++) {
        if (intersects(list[i], rect) && !(ignore && ignore.has(list[i]))) return true;
      }
      return false;
    });
  }

  // Dot guards (not chips) that overlap `rect`.
  function gridCollectGuards(grid, rect, out) {
    gridCells(rect, (key) => {
      const list = grid.get(key);
      if (list) {
        for (let i = 0; i < list.length; i++) {
          if (list[i].guard && intersects(list[i], rect)) out.add(list[i]);
        }
      }
      return false;
    });
    return out;
  }

  function gridInsert(grid, rect) {
    gridCells(rect, (key) => {
      const list = grid.get(key);
      if (list) list.push(rect);
      else grid.set(key, [rect]);
      return false;
    });
  }

  L.FirmPinLayer = L.Layer.extend({
    options: {
      pane: "overlayPane",
      // Extra canvas on every side, as a fraction of the map size, so a drag
      // shows pins before the next redraw.
      padding: 0.3,
      // Zoom from which labels are eligible. Rich firms and big hubs are the
      // names a founder scans for at the bay-wide view; lite singles only get
      // chips once there is room for them.
      labelMinZoom: { rich: 8, lite: 11 },
      maxLabels: 140,
      onSelect: null, // (firm | null) => void — firm clicked, or background clicked
      // (site) => void — a same-address stack too big to fan out was clicked
      onSelectSite: null,
      aumBucket: () => "0",
      formatShort: (n) => String(n),
      prettyStage: (s) => s,
    },

    initialize(options) {
      L.setOptions(this, options);
      this._sites = [];
      this._siteByKey = new Map();
      this._ids = null;
      this._focusKey = null;
      this._selectedId = null;
      this._selectedKey = null;
      this._hover = null; // the hit object under the cursor, refreshed every draw
      this._hoverKey = null; // its stable key (cluster nodes are rebuilt per frame)
      this._focus = null; // { kind: "city" | "site", value } — everything else is dimmed
      this._focusActive = false;
      this._fan = null; // { key, firms } — an expanded same-address stack
      this._hits = [];
      // Site keys whose chip was placed in the previous frame. Re-placing
      // them first keeps labels from flickering while the map moves.
      this._sticky = new Set();
      this._measureCache = new Map();
      // Zoom-0 world pixel per site key: invariant for the page, so a filter
      // change never re-projects.
      this._world = new Map();
      this._ignore = new Set();
      this._zooming = false;
      // any-pointer: a touch laptop whose primary pointer is a trackpad still
      // gets finger-sized hit targets.
      this._coarse = window.matchMedia("(any-pointer: coarse)").matches;
    },

    onAdd(map) {
      const canvas = (this._canvas = L.DomUtil.create(
        "canvas",
        "firm-pins leaflet-zoom-animated",
        this.getPane(),
      ));
      canvas.setAttribute("aria-hidden", "true");
      this._ctx = canvas.getContext("2d");

      const tip = (this._tip = L.DomUtil.create("div", "firm-tip", map.getContainer()));
      tip.setAttribute("role", "tooltip");
      tip.hidden = true;
      this._tipName = L.DomUtil.create("strong", "", tip);
      this._tipMeta = L.DomUtil.create("span", "", tip);

      this._readTheme();
      this._armDprWatch();
      // A re-added layer gets a fresh canvas; forget the previous bitmap size.
      this._w = this._h = this._dpr = undefined;
      map.on("mousemove", this._onMouseMove, this);
      map.on("mouseout", this._onMouseOut, this);
      map.on("click", this._onClick, this);
      map.on("zoomstart", this._onZoomStart, this);
      L.DomEvent.on(map.getContainer(), "touchstart", this._onTouchStart, this);
      this._resize();
    },

    onRemove(map) {
      map.off("mousemove", this._onMouseMove, this);
      map.off("mouseout", this._onMouseOut, this);
      map.off("click", this._onClick, this);
      map.off("zoomstart", this._onZoomStart, this);
      L.DomEvent.off(map.getContainer(), "touchstart", this._onTouchStart, this);
      if (this._dprQuery) this._dprQuery.removeEventListener("change", this._dprHandler);
      if (this._hoverRaf) cancelAnimationFrame(this._hoverRaf);
      if (this._drawRaf) cancelAnimationFrame(this._drawRaf);
      this._setHover(null);
      L.DomUtil.remove(this._canvas);
      L.DomUtil.remove(this._tip);
    },

    getEvents() {
      const events = {
        viewreset: this._reset,
        zoom: this._onZoom,
        move: this._onMove,
        movestart: this._onMoveStart,
        moveend: this._onMoveEnd,
        zoomend: this._onZoomEnd,
        resize: this._resize,
      };
      if (this._zoomAnimated) events.zoomanim = this._onAnimZoom;
      return events;
    },

    // ── Public API ──────────────────────────────────────────────────────

    // Replace the firm set. `focus` ({ kind: "city" | "site", value }) dims every
    // other site: used while a location filter is active so the rest of the Bay
    // stays visible for context.
    setFirms(firms, focus = null) {
      const ids = new Set();
      for (const f of firms) ids.add(f.id);
      let same = !!this._ids && this._ids.size === ids.size;
      if (same) for (const id of ids) if (!this._ids.has(id)) { same = false; break; }
      const focusChanged = !sameFocus(focus, this._focus);
      this._focus = focus;
      if (same && !focusChanged) return;
      if (!same) {
        this._ids = ids;
        this._buildSites(firms);
        this._selectedKey = this._keyOfFirmId(this._selectedId);
        if (this._fan && !this._siteByKey.has(this._fan.key)) this._fan = null;
      }
      this._focusActive =
        !!focus &&
        (focus.kind === "site"
          ? this._siteByKey.has(focus.value)
          : this._sites.some((site) => site.city === focus.value));
      // The tooltip's second line depends on the focus state.
      if (this._hover) this._showTip(this._hover);
      this._sticky.clear();
      this._requestDraw(true);
    },

    setSelected(id) {
      if (id === this._selectedId) return;
      this._selectedId = id;
      this._selectedKey = this._keyOfFirmId(id);
      this._requestDraw(true);
    },

    siteOf(firm) {
      return this._siteByKey.get(siteKey(firm.lat, firm.lng)) || null;
    },

    // ── Data ────────────────────────────────────────────────────────────

    _keyOfFirmId(id) {
      if (id == null) return null;
      for (const s of this._sites) for (const f of s.firms) if (f.id === id) return s.key;
      return null;
    },

    _buildSites(firms) {
      const map = this._map;
      const byKey = new Map();
      for (const f of firms) {
        if (!f.lat || !f.lng) continue;
        const key = siteKey(f.lat, f.lng);
        let s = byKey.get(key);
        if (!s) {
          // World pixel coordinates at zoom 0; every zoom is a pure scale of
          // these, so per-frame projection is one multiply-add per axis.
          let w = this._world.get(key);
          if (!w) {
            const p = map.project([f.lat, f.lng], 0);
            w = { x: p.x, y: p.y };
            this._world.set(key, w);
          }
          s = { key, lat: f.lat, lng: f.lng, wx: w.x, wy: w.y, firms: [], x: 0, y: 0, chip: null };
          byKey.set(key, s);
        }
        s.firms.push(f);
      }
      const sites = [];
      byKey.forEach((s) => {
        // Rich first, then AUM descending, unknown AUM last: the order a
        // founder would scan them in, and the firm a click lands on.
        s.firms.sort((a, b) => {
          const ra = a.tier === "lite" ? 1 : 0;
          const rb = b.tier === "lite" ? 1 : 0;
          if (ra !== rb) return ra - rb;
          return (b.aum_usd ?? -1) - (a.aum_usd ?? -1);
        });
        const top = s.firms[0];
        s.top = top;
        s.n = s.firms.length;
        s.rich = top.tier !== "lite";
        s.aum = s.firms.reduce((m, f) => Math.max(m, f.aum_usd || 0), 0);
        s.bucket = String(this.options.aumBucket(s.aum || null));
        s.r = s.n === 1 ? (s.rich ? DOT_R_RICH : DOT_R_LITE) : Math.min(16, Math.round(6 + 1.4 * Math.log2(s.n)));
        s.city = cityOf(top.address);
        s.label =
          s.n === 1 ? top.name : s.n >= HUB_MIN && s.city ? `${s.city} · ${s.n}` : `${top.name} +${s.n - 1}`;
        s.pri = priority(s);
        sites.push(s);
      });
      // Ascending priority: important sites are drawn last, so on top.
      sites.sort((a, b) => a.pri - b.pri);
      this._sites = sites;
      this._siteByKey = byKey;
    },

    // ── Geometry (mirrors L.Renderer: a padded canvas positioned in layer
    //    space, CSS-scaled during Leaflet's zoom animation) ────────────────

    _readTheme() {
      const family = getComputedStyle(document.body).fontFamily || "sans-serif";
      this._theme = {
        bucket: {
          0: cssVar("--muted", "#8b949e"),
          1: cssVar("--bucket-1", "#4a5568"),
          2: cssVar("--bucket-2", "#5b9dd9"),
          3: cssVar("--bucket-3", "#b87bff"),
          4: cssVar("--bucket-4", "#f78166"),
        },
        text: cssVar("--text", "#e6edf3"),
        chipBg: cssVar("--panel-2", "#1c2129"),
        bg: cssVar("--bg", "#0e1116"),
        accent: cssVar("--accent-2", "#58a6ff"),
      };
      this._fontRich = `600 11px ${family}`;
      this._fontLite = `500 11px ${family}`;
      this._fontCount = `700 10px ${family}`;
      this._fontCountSmall = `700 8px ${family}`;
      this._measureCache.clear();
    },

    // Re-arm on every change so a window dragged between 1x and 2x displays
    // keeps crisp text.
    _armDprWatch() {
      if (this._dprQuery) this._dprQuery.removeEventListener("change", this._dprHandler);
      const dpr = window.devicePixelRatio || 1;
      this._dprQuery = window.matchMedia(`(resolution: ${dpr}dppx)`);
      this._dprHandler = () => {
        this._armDprWatch();
        this._resize();
      };
      this._dprQuery.addEventListener("change", this._dprHandler);
    },

    _resize() {
      const map = this._map;
      if (!map) return;
      const size = map.getSize();
      const p = this.options.padding;
      const dpr = Math.min(DPR_MAX, window.devicePixelRatio || 1);
      const canvas = this._canvas;
      // Grow-only bitmap in 256-px steps. The splitter resizes the map on
      // every pointer move, and reallocating a ~20 MB backing store per move
      // is what would make that drag stutter; a canvas that is a bit larger
      // than the padded area just pre-draws a little more margin.
      const bw = Math.ceil((size.x * (1 + 2 * p) * dpr) / 256) * 256;
      const bh = Math.ceil((size.y * (1 + 2 * p) * dpr) / 256) * 256;
      if (dpr !== this._dpr || canvas.width < bw || canvas.height < bh) {
        const fresh = dpr !== this._dpr;
        canvas.width = fresh ? bw : Math.max(canvas.width, bw);
        canvas.height = fresh ? bh : Math.max(canvas.height, bh);
        this._dpr = dpr;
      }
      this._w = canvas.width / dpr;
      this._h = canvas.height / dpr;
      canvas.style.width = this._w + "px";
      canvas.style.height = this._h + "px";
      this._update(true);
    },

    _reset() {
      this._update(true);
    },

    _update(final) {
      const map = this._map;
      if (!map || !map._loaded) return;
      const size = map.getSize();
      const p = this.options.padding;
      const min = map.containerPointToLayerPoint(size.multiplyBy(-p)).round();
      this._bounds = new L.Bounds(min, min.add([this._w, this._h]));
      this._center = map.getCenter();
      this._zoom = map.getZoom();
      L.DomUtil.setPosition(this._canvas, this._bounds.min);
      this._draw(final);
    },

    // One CSS transform for the whole canvas during Leaflet's zoom
    // animation; the post-transition 'zoom'/'moveend' redraws it crisp.
    _onAnimZoom(e) {
      const map = this._map;
      const scale = map.getZoomScale(e.zoom, this._zoom);
      const viewHalf = map.getSize().multiplyBy(0.5 + this.options.padding);
      const currentCenterPoint = map.project(this._center, e.zoom);
      // Same quantity as Leaflet's private _getNewPixelOrigin, built from
      // public calls: layerPointToContainerPoint([0,0]) is the map pane offset.
      const newOrigin = map
        .project(e.center, e.zoom)
        .subtract(map.getSize().divideBy(2))
        .add(map.layerPointToContainerPoint([0, 0]))
        .round();
      const topLeft = viewHalf.multiplyBy(-scale).add(currentCenterPoint).subtract(newOrigin);
      L.DomUtil.setTransform(this._canvas, topLeft, scale);
    },

    // Fired every frame of a flyTo, pinch or continuous wheel zoom: redraw
    // synchronously so pins move in lock-step with the tiles.
    _onZoom() {
      this._update(false);
    },

    _onZoomStart() {
      this._zooming = true;
      this._setHover(null);
    },

    _onZoomEnd() {
      this._zooming = false;
    },

    _onMoveStart() {
      this._setHover(null);
    },

    // Drag / pan frames: the canvas rides along with the map pane; redraw
    // only when the viewport gets close to the padded edge.
    _onMove() {
      if (this._zooming || !this._bounds) return;
      const map = this._map;
      const size = map.getSize();
      const slack = size.multiplyBy(this.options.padding * 0.15);
      const inner = new L.Bounds(this._bounds.min.add(slack), this._bounds.max.subtract(slack));
      const vp = new L.Bounds(map.containerPointToLayerPoint([0, 0]), map.containerPointToLayerPoint(size));
      if (!inner.contains(vp)) this._update(false);
    },

    _onMoveEnd() {
      // Every view change ends with moveend, but a flyTo cancelled by a drag
      // never sends its zoomend — so this is where the flag is cleared.
      this._zooming = false;
      this._update(true);
      this._rehover();
    },

    _requestDraw(final) {
      if (this._drawRaf) {
        this._drawFinal = this._drawFinal || final;
        return;
      }
      this._drawFinal = final;
      this._drawRaf = requestAnimationFrame(() => {
        this._drawRaf = null;
        const f = this._drawFinal;
        this._drawFinal = false;
        // Mid-tween the map already reports the target zoom/origin while the
        // canvas is still positioned for the old view; moveend redraws.
        if (this._map && this._map._animatingZoom) return;
        this._draw(f);
      });
    },

    // ── Drawing ─────────────────────────────────────────────────────────

    // Text and width of a chip for `text`, cut with an ellipsis to fit
    // LABEL_MAX_W minus `reserve` px.
    _fit(text, rich, reserve) {
      const key = (rich ? "r|" : "l|") + reserve + "|" + text;
      let m = this._measureCache.get(key);
      if (m) return m;
      const ctx = this._ctx;
      ctx.font = rich ? this._fontRich : this._fontLite;
      const budget = LABEL_MAX_W - reserve;
      let out = text;
      if (ctx.measureText(text).width > budget) {
        const cps = Array.from(text); // code points, so a surrogate pair is never split
        let lo = 1;
        let hi = cps.length;
        while (lo < hi) {
          const mid = (lo + hi + 1) >> 1;
          if (ctx.measureText(cps.slice(0, mid).join("").trimEnd() + "…").width <= budget) lo = mid;
          else hi = mid - 1;
        }
        out = cps.slice(0, lo).join("").trimEnd() + "…";
      }
      m = { text: out, w: Math.ceil(ctx.measureText(out).width) };
      this._measureCache.set(key, m);
      return m;
    },

    // Chip text and width for a node. A long name is cut with an ellipsis,
    // but the " +N" / " · N" count suffix always survives the cut.
    _measure(s) {
      const parts = /^([\s\S]*?)(\s(?:·\s|\+)\d+)$/.exec(s.label);
      if (!parts) return this._fit(s.label, s.rich, 0);
      const ctx = this._ctx;
      ctx.font = s.rich ? this._fontRich : this._fontLite;
      const sufW = Math.ceil(ctx.measureText(parts[2]).width);
      const name = this._fit(parts[1], s.rich, sufW);
      return { text: name.text + parts[2], w: name.w + sufW };
    },

    // Try right of the dot, then left, above, below and the four diagonals;
    // the first spot that collides with nothing wins. Dot guards the dot
    // itself already overlaps (a rich dot sitting on a city hub at low zoom)
    // do not count, otherwise a dense cluster could never be labelled; chips
    // always count, so two labels never overlap. Selected / hovered chips are
    // forced.
    _placeLabel(s, grid, W, H, force) {
      const m = this._measure(s);
      const w = m.w + CHIP_PAD_X * 2;
      const h = CHIP_H;
      const gx = s.x + s.r + CHIP_GAP;
      const lx = s.x - s.r - CHIP_GAP - w;
      const cx = s.x - w / 2;
      const my = s.y - h / 2;
      SPOTS[0][0] = gx; SPOTS[0][1] = my;
      SPOTS[1][0] = lx; SPOTS[1][1] = my;
      SPOTS[2][0] = cx; SPOTS[2][1] = s.y - s.r - CHIP_GAP - h;
      SPOTS[3][0] = cx; SPOTS[3][1] = s.y + s.r + CHIP_GAP;
      SPOTS[4][0] = gx; SPOTS[4][1] = s.y - s.r - h;
      SPOTS[5][0] = gx; SPOTS[5][1] = s.y + s.r;
      SPOTS[6][0] = lx; SPOTS[6][1] = s.y - s.r - h;
      SPOTS[7][0] = lx; SPOTS[7][1] = s.y + s.r;
      DOT_RECT.x = s.x - s.r;
      DOT_RECT.y = s.y - s.r;
      DOT_RECT.w = DOT_RECT.h = 2 * s.r;
      const ignore = this._ignore;
      ignore.clear();
      gridCollectGuards(grid, DOT_RECT, ignore);
      TRY_RECT.w = w;
      TRY_RECT.h = h;
      let firstX = NaN;
      let firstY = 0;
      for (let i = 0; i < SPOTS.length; i++) {
        const x = Math.round(SPOTS[i][0]);
        const y = Math.round(SPOTS[i][1]);
        if (x + w < 0 || y + h < 0 || x > W || y > H) continue;
        if (Number.isNaN(firstX)) {
          firstX = x;
          firstY = y;
        }
        TRY_RECT.x = x;
        TRY_RECT.y = y;
        if (!gridOverlaps(grid, TRY_RECT, ignore)) return { x, y, w, h, text: m.text };
      }
      return force && !Number.isNaN(firstX) ? { x: firstX, y: firstY, w, h, text: m.text } : null;
    },

    // Sites whose dots would overlap at this zoom merge into one node.
    // Grid-cell grouping: O(n), stable enough frame to frame for sticky
    // labels because a node's key is its highest-priority member's key.
    _cluster(vis, zoom) {
      if (zoom >= CLUSTER_MAX_ZOOM) return vis;
      const cells = new Map();
      for (let i = 0; i < vis.length; i++) {
        const s = vis[i];
        const key = ((Math.floor(s.y / CLUSTER_PX) + 4096) << 13) | (Math.floor(s.x / CLUSTER_PX) + 4096);
        const list = cells.get(key);
        if (list) list.push(s);
        else cells.set(key, [s]);
      }
      const nodes = [];
      cells.forEach((list) => nodes.push(list.length === 1 ? list[0] : this._makeCluster(list)));
      nodes.sort((a, b) => a.pri - b.pri);
      return nodes;
    },

    _makeCluster(sites) {
      // `sites` arrive in ascending priority: the last one leads.
      const lead = sites[sites.length - 1];
      let n = 0;
      let aum = 0;
      let rich = false;
      let dim = true;
      let isSel = false;
      let sx = 0;
      let sy = 0;
      let sameCity = true;
      const firms = [];
      for (let i = 0; i < sites.length; i++) {
        const s = sites[i];
        n += s.n;
        aum = Math.max(aum, s.aum);
        rich = rich || s.rich;
        dim = dim && s.dim;
        isSel = isSel || s.isSel;
        sx += s.x * s.n;
        sy += s.y * s.n;
        if (s.city !== lead.city) sameCity = false;
        for (let j = 0; j < s.firms.length; j++) firms.push(s.firms[j]);
      }
      const city = sameCity ? lead.city : "";
      const tier = rich ? 3 : 2;
      return {
        key: "c:" + lead.key,
        isCluster: true,
        sites,
        firms,
        top: lead.top,
        n,
        aum,
        rich,
        dim,
        isSel,
        isHov: false,
        city,
        bucket: String(this.options.aumBucket(aum || null)),
        r: Math.min(16, Math.round(6 + 1.4 * Math.log2(n))),
        x: sx / n,
        y: sy / n,
        label: city && n >= HUB_MIN ? `${city} · ${n}` : n >= HUB_MIN ? `${n} firms` : `${lead.top.name} +${n - 1}`,
        pri: tier * 1e14 + Math.min(n, 999) * 1e11 + Math.min(aum || 0, 1e11 - 1),
        chip: null,
      };
    },

    _draw(final) {
      const map = this._map;
      const ctx = this._ctx;
      if (!map || !this._bounds || !ctx) return;
      const W = this._w;
      const H = this._h;
      const dpr = this._dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      this._hits.length = 0;
      if (!this._sites.length) {
        this._sticky.clear();
        this._hover = null;
        return;
      }

      const zoom = map.getZoom();
      const scale = map.getZoomScale(zoom, 0);
      const origin = map.getPixelOrigin();
      const ox = origin.x + this._bounds.min.x;
      const oy = origin.y + this._bounds.min.y;
      const focus = this._focusActive ? this._focus : null;
      const selKey = this._selectedKey;
      const hoverKey = this._hoverKey;
      const minZ = this.options.labelMinZoom;
      const margin = LABEL_MAX_W + 2 * CHIP_PAD_X + 24;

      // 1. Project and cull sites.
      const vis = [];
      for (let i = 0; i < this._sites.length; i++) {
        const s = this._sites[i];
        const x = s.wx * scale - ox;
        const y = s.wy * scale - oy;
        s.chip = null;
        s.onScreen = false;
        if (x < -margin || y < -margin || x > W + margin || y > H + margin) continue;
        s.x = x;
        s.y = y;
        s.onScreen = true;
        s.dim = !!focus && (focus.kind === "city" ? s.city !== focus.value : s.key !== focus.value);
        s.isSel = s.key === selKey;
        vis.push(s);
      }

      // 2. Merge overlapping sites into cluster nodes. Dots block labels, but
      // not all equally: a hub's count must stay readable, a rich firm's dot
      // may lose only its rim, and a lite single's 4 px dot may sit under a
      // chip — otherwise a dense cluster gets no names at all.
      const nodes = this._cluster(vis, zoom);
      let hover = null;
      const grid = new Map();
      const cands = [];
      for (let i = 0; i < nodes.length; i++) {
        const s = nodes[i];
        s.chip = null;
        s.isHov = s.key === hoverKey;
        if (s.isHov) hover = s;
        if (s.dim) continue;
        const guard = s.n > 1 ? s.r + 2 : s.rich ? Math.max(2, s.r - 2) : 0;
        if (guard) gridInsert(grid, { x: s.x - guard, y: s.y - guard, w: 2 * guard, h: 2 * guard, guard: true });
        const bigHub = s.n >= HUB_MIN;
        if (s.isSel || s.isHov || zoom >= (s.rich || bigHub ? minZ.rich : minZ.lite)) cands.push(s);
      }

      // 3. Place labels: selected, hovered, then (while moving) last frame's
      // chips so nothing flickers, then by priority. A final draw at rest
      // ignores stickiness so the resting placement is priority-pure.
      const sticky = this._sticky;
      const rank = (s) => (s.isSel ? 3 : s.isHov ? 2 : !final && sticky.has(s.key) ? 1 : 0);
      cands.sort((a, b) => rank(b) - rank(a) || b.pri - a.pri);
      const placed = [];
      const next = new Set();
      const maxTries = this.options.maxLabels * 4;
      for (let i = 0; i < cands.length; i++) {
        const s = cands[i];
        const force = s.isSel || s.isHov;
        if (!force && (placed.length >= this.options.maxLabels || i >= maxTries)) break;
        const rect = this._placeLabel(s, grid, W, H, force);
        if (!rect) continue;
        s.chip = rect;
        gridInsert(grid, rect);
        placed.push(s);
        next.add(s.key);
      }
      this._sticky = next;

      // 4. Paint: plain dots batched per style, dimmed and highlighted dots
      // individually, then chips, highlights last, then the fan.
      const batches = new Map();
      for (let i = 0; i < nodes.length; i++) {
        const s = nodes[i];
        this._hits.push(s);
        if (s.dim || s.isSel || s.isHov) continue;
        const key = (s.rich ? "r" : "l") + s.bucket + (s.n > 1 ? "h" : "s");
        let list = batches.get(key);
        if (!list) {
          list = [];
          batches.set(key, list);
        }
        list.push(s);
      }
      batches.forEach((list) => this._drawDotBatch(list));
      for (let i = 0; i < nodes.length; i++) {
        const s = nodes[i];
        if (s.dim || s.isSel || s.isHov) this._drawDot(s);
      }
      for (let i = placed.length - 1; i >= 0; i--) {
        const s = placed[i];
        if (!s.isSel && !s.isHov) this._drawChip(s);
      }
      for (let i = 0; i < placed.length; i++) {
        const s = placed[i];
        if (s.isSel || s.isHov) this._drawChip(s);
      }
      if (this._fan) hover = this._drawFan(hoverKey) || hover;

      this._hover = hover;
      if (hover) this._positionTip(hover);
      else if (hoverKey) this._clearHover();
    },

    // An expanded same-address stack: one chip per firm in a column beside
    // the dot (right first, then left), each with its own hit rectangle.
    _drawFan(hoverKey) {
      const fan = this._fan;
      const site = this._siteByKey.get(fan.key);
      if (!site || !site.onScreen) return null;
      const ctx = this._ctx;
      const t = this._theme;
      const firms = fan.firms;
      const rightCount = Math.min(firms.length, 6);
      const leftCount = firms.length - rightCount;
      let hovered = null;
      for (let i = 0; i < firms.length; i++) {
        const f = firms[i];
        const right = i < 6;
        const idx = right ? i : i - 6;
        const count = right ? rightCount : leftCount;
        const rich = f.tier !== "lite";
        const m = this._fit(f.name, rich, 0);
        const w = m.w + 2 * CHIP_PAD_X;
        const cy = site.y - (count * FAN_PITCH) / 2 + idx * FAN_PITCH + FAN_PITCH / 2;
        const x = right ? site.x + site.r + 14 : site.x - site.r - 14 - w;
        const rect = { x: Math.round(x), y: Math.round(cy - CHIP_H / 2), w, h: CHIP_H, text: m.text };
        const hit = {
          kind: "fan",
          key: "f:" + f.id,
          firm: f,
          top: f,
          n: 1,
          rich,
          bucket: String(this.options.aumBucket(f.aum_usd)),
          chip: rect,
          x: rect.x + w / 2,
          y: rect.y + CHIP_H / 2,
          r: CHIP_H / 2,
          isSel: f.id === this._selectedId,
          isHov: false,
          dim: false,
        };
        hit.isHov = hit.key === hoverKey;
        if (hit.isHov) hovered = hit;
        ctx.globalAlpha = 0.7;
        ctx.strokeStyle = t.accent;
        ctx.lineWidth = 1;
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.moveTo(site.x, site.y);
        ctx.lineTo(right ? rect.x : rect.x + w, cy);
        ctx.stroke();
        ctx.globalAlpha = 1;
        this._drawChip(hit);
        this._hits.push(hit);
      }
      ctx.beginPath();
      ctx.arc(site.x, site.y, site.r + 3, 0, Math.PI * 2);
      ctx.lineWidth = 2;
      ctx.strokeStyle = t.accent;
      ctx.stroke();
      return hovered;
    },

    // One fill + one stroke for every plain dot of a style: dot cost stays
    // flat as the firm count grows (street-level data makes a site per firm).
    _drawDotBatch(list) {
      const ctx = this._ctx;
      const t = this._theme;
      const s0 = list[0];
      const col = t.bucket[s0.bucket] || t.bucket[0];
      ctx.globalAlpha = 1;
      ctx.setLineDash([]);
      ctx.beginPath();
      for (let i = 0; i < list.length; i++) {
        const s = list[i];
        ctx.moveTo(s.x + s.r, s.y);
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      }
      this._fillDotStyle(s0, col);
      for (let i = 0; i < list.length; i++) this._drawCount(list[i]);
    },

    _fillDotStyle(s, col) {
      const ctx = this._ctx;
      const t = this._theme;
      if (s.rich) {
        ctx.fillStyle = col;
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = t.bg;
        ctx.stroke();
      } else {
        ctx.fillStyle = t.chipBg;
        ctx.fill();
        ctx.lineWidth = s.n > 1 ? 2 : 1.5;
        ctx.strokeStyle = col;
        ctx.stroke();
      }
    },

    _drawCount(s) {
      if (!(s.n > 1 && s.r >= 7)) return;
      const ctx = this._ctx;
      const t = this._theme;
      ctx.font = s.r >= 9 ? this._fontCount : this._fontCountSmall;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = s.rich ? t.bg : t.text;
      ctx.fillText(String(s.n), s.x, s.y + 0.5);
    },

    _drawDot(s) {
      const ctx = this._ctx;
      const t = this._theme;
      const col = t.bucket[s.bucket] || t.bucket[0];
      ctx.globalAlpha = s.dim ? 0.4 : 1;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      this._fillDotStyle(s, col);
      this._drawCount(s);
      if (s.isSel || s.isHov) {
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r + 3, 0, Math.PI * 2);
        ctx.lineWidth = 2;
        ctx.strokeStyle = t.accent;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    },

    _drawChip(s) {
      const ctx = this._ctx;
      const t = this._theme;
      const r = s.chip;
      const lite = !s.rich;
      const hot = s.isSel || s.isHov;
      ctx.globalAlpha = lite && !hot ? 0.86 : 0.97;
      ctx.beginPath();
      roundRectPath(ctx, r.x + 0.5, r.y + 0.5, r.w - 1, r.h - 1, 9);
      ctx.fillStyle = t.chipBg;
      ctx.fill();
      ctx.lineWidth = hot ? 2 : 1;
      ctx.strokeStyle = hot ? t.accent : t.bucket[s.bucket] || t.bucket[0];
      ctx.setLineDash(lite && !hot ? [3, 2] : []);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = lite ? this._fontLite : this._fontRich;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillStyle = Number(s.bucket) >= 2 ? t.bucket[s.bucket] : t.text;
      ctx.fillText(r.text, r.x + CHIP_PAD_X, r.y + r.h / 2 + 0.5);
      ctx.globalAlpha = 1;
    },

    // ── Interaction ─────────────────────────────────────────────────────

    // A chip wins outright (chips never overlap); among dots, the nearest
    // centre wins, so a dot sitting next to a bigger one is still pickable.
    _hitTest(layerPoint) {
      if (!this._bounds || !layerPoint) return null;
      const px = layerPoint.x - this._bounds.min.x;
      const py = layerPoint.y - this._bounds.min.y;
      const slop = this._coarse ? 10 : 4;
      let best = null;
      let bestD = Infinity;
      for (let i = this._hits.length - 1; i >= 0; i--) {
        const s = this._hits[i];
        const c = s.chip;
        if (c && px >= c.x && px <= c.x + c.w && py >= c.y && py <= c.y + c.h) return s;
        if (s.kind === "fan") continue;
        const dx = px - s.x;
        const dy = py - s.y;
        const r = s.r + slop;
        const d2 = dx * dx + dy * dy;
        if (d2 <= r * r && d2 < bestD) {
          best = s;
          bestD = d2;
        }
      }
      return best;
    },

    _onMouseMove(e) {
      // Browsers synthesise mouse events after a tap; those must not start a
      // hover that then sticks to the screen.
      const oe = e.originalEvent;
      if (oe && oe.sourceCapabilities && oe.sourceCapabilities.firesTouchEvents) return;
      this._lastMouse = e.containerPoint;
      const map = this._map;
      if (this._zooming || (map.dragging && map.dragging.moving())) return;
      if (this._hoverRaf) return;
      this._hoverRaf = requestAnimationFrame(() => {
        this._hoverRaf = null;
        if (!this._map || !this._lastMouse) return;
        this._setHover(this._hitTest(this._map.containerPointToLayerPoint(this._lastMouse)));
      });
    },

    _onMouseOut() {
      this._lastMouse = null;
      this._setHover(null);
    },

    _onTouchStart() {
      this._lastMouse = null;
      this._setHover(null);
    },

    // After a drag or zoom settles, re-evaluate what is under the cursor.
    _rehover() {
      if (!this._lastMouse || this._zooming) return;
      this._setHover(this._hitTest(this._map.containerPointToLayerPoint(this._lastMouse)));
    },

    _setHover(hit) {
      const key = hit ? hit.key : null;
      if (key === this._hoverKey) {
        this._hover = hit;
        return;
      }
      this._hoverKey = key;
      this._hover = hit;
      const map = this._map;
      if (!map) return;
      const container = map.getContainer();
      if (hit) {
        L.DomUtil.addClass(container, "firm-pin-hover");
        // A double-click on a pin should not zoom the map.
        if (map.doubleClickZoom) map.doubleClickZoom.disable();
        this._showTip(hit);
      } else {
        L.DomUtil.removeClass(container, "firm-pin-hover");
        if (map.doubleClickZoom) map.doubleClickZoom.enable();
        this._tip.hidden = true;
      }
      this._requestDraw(false);
    },

    // The hovered thing no longer exists (filtered away, cluster dissolved).
    _clearHover() {
      this._hoverKey = null;
      this._hover = null;
      const map = this._map;
      if (!map) return;
      L.DomUtil.removeClass(map.getContainer(), "firm-pin-hover");
      if (map.doubleClickZoom) map.doubleClickZoom.enable();
      this._tip.hidden = true;
    },

    _showTip(s) {
      const o = this.options;
      const f = s.top;
      let name;
      let meta;
      if (s.kind === "fan" || s.n === 1) {
        name = f.name;
        const parts = [];
        if (f.aum_usd) parts.push("$" + o.formatShort(f.aum_usd));
        if (f.stages && f.stages.length) parts.push(f.stages.map((st) => o.prettyStage(st)).join(", "));
        meta = parts.join(" · ");
      } else if (s.isCluster) {
        name = s.city ? `${s.n} firms · ${s.city}` : `${s.n} firms`;
        meta = `${s.sites.length} addresses · click to zoom in`;
      } else {
        // Every firm here shares one address.
        name = `${s.n} firms at this address`;
        const others = `${f.name} and ${s.n - 1} ${s.n === 2 ? "other" : "others"}`;
        if (s.n <= FAN_MAX) {
          meta = this._fan && this._fan.key === s.key ? `${others} · click to collapse` : `${others} · click to expand`;
        } else {
          const listed = this._focus && this._focus.kind === "site" && this._focus.value === s.key;
          meta = listed ? "Listed in the table · click to clear" : `${others} · click to list them`;
        }
      }
      this._tipName.textContent = name;
      this._tipMeta.textContent = meta;
      this._tipMeta.hidden = !meta;
      this._tip.hidden = false;
      this._positionTip(s);
    },

    _positionTip(s) {
      if (this._tip.hidden) return;
      const cp = this._map.layerPointToContainerPoint(
        L.point(s.x + this._bounds.min.x, s.y + this._bounds.min.y),
      );
      const x = Math.round(cp.x);
      const above = cp.y - s.r - 8 > 64;
      this._tip.style.transform = above
        ? `translate(${x}px, ${Math.round(cp.y - s.r - 8)}px) translate(-50%, -100%)`
        : `translate(${x}px, ${Math.round(cp.y + s.r + 8)}px) translate(-50%, 0)`;
    },

    _onClick(e) {
      // The second click of a double-click must not toggle anything back.
      if (e.originalEvent && e.originalEvent.detail > 1) return;
      const hit = this._hitTest(e.layerPoint);
      const o = this.options;
      if (!hit) {
        this._collapseFan();
        if (o.onSelect) o.onSelect(null);
        return;
      }
      if (hit.kind === "fan") {
        if (o.onSelect) o.onSelect(hit.firm);
        return;
      }
      if (hit.isCluster) {
        // Several addresses drawn as one dot: zoom until they come apart.
        this._collapseFan();
        this._zoomTo(hit);
        return;
      }
      if (hit.n === 1) {
        this._collapseFan();
        if (o.onSelect) o.onSelect(hit.top);
        return;
      }
      // One address, several firms: fan them out, or list them when there
      // are too many chips to fit.
      if (hit.n <= FAN_MAX) {
        this._fanOut(hit);
        return;
      }
      this._collapseFan();
      if (o.onSelectSite) o.onSelectSite(hit);
    },

    _zoomTo(node) {
      const map = this._map;
      const bounds = L.latLngBounds(node.sites.map((site) => [site.lat, site.lng]));
      const opts = { padding: [60, 60], maxZoom: 17 };
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        map.fitBounds(bounds, Object.assign({ animate: false }, opts));
      } else {
        map.flyToBounds(bounds, Object.assign({ duration: 0.6 }, opts));
      }
    },

    _fanOut(site) {
      if (this._fan && this._fan.key === site.key) {
        this._collapseFan();
        return;
      }
      this._fan = { key: site.key, firms: site.firms.slice(0, FAN_MAX) };
      if (this._hover) this._showTip(this._hover);
      this._requestDraw(true);
    },

    _collapseFan() {
      if (!this._fan) return;
      this._fan = null;
      if (this._hover) this._showTip(this._hover);
      this._requestDraw(true);
    },
  });

  L.FirmPinLayer.siteKey = siteKey;
  L.FirmPinLayer.cityOf = cityOf;
  L.FirmPinLayer.HUB_MIN = HUB_MIN;
  L.firmPinLayer = (options) => new L.FirmPinLayer(options);

  // Continuous wheel / trackpad zoom.
  //
  // Leaflet's stock handler waits 40 ms, rounds every gesture UP to a whole
  // zoom level, runs a 250 ms CSS transition and silently drops all wheel
  // input while it runs — which reads as sticky. This handler eases a
  // fractional target zoom per animation frame through the same `_move`
  // path Leaflet's own pinch-zoom and flyTo use, so the map tracks the
  // finger. It touches four private Map methods (_stop, _moveStart, _move,
  // _moveEnd, plus the _animatingZoom flag) that Leaflet's TouchZoom handler
  // relies on too; Leaflet is SRI-pinned to 1.9.4 in index.html, so they
  // cannot drift silently. app.js can fall back to the stock handler by
  // turning SMOOTH_WHEEL off.
  L.Map.SmoothWheelZoom = L.Handler.extend({
    addHooks() {
      L.DomEvent.on(this._map.getContainer(), "wheel", this._onWheel, this);
      // Leaflet fires movestart before dragstart, and before any pan.
      this._map.on("movestart", this._onOtherMoveStart, this);
      this._map.on("zoomstart", this._onOtherZoomStart, this);
    },

    removeHooks() {
      L.DomEvent.off(this._map.getContainer(), "wheel", this._onWheel, this);
      this._map.off("movestart", this._onOtherMoveStart, this);
      this._map.off("zoomstart", this._onOtherZoomStart, this);
      this._end("full");
    },

    _onWheel(e) {
      const map = this._map;
      L.DomEvent.stop(e);
      // A ± / double-click tween is running; let it finish.
      if (map._animatingZoom) return;
      const delta = L.DomEvent.getWheelDelta(e);
      if (!delta) return;
      // Same unit as Leaflet's own wheelPxPerZoomLevel (normalised px per
      // level; getWheelDelta already divides out the platform factor).
      const perLevel = map.options.smoothWheelPxPerZoomLevel || map.options.wheelPxPerZoomLevel || 60;
      const base = this._active ? this._target : map.getZoom();
      const next = Math.max(map.getMinZoom(), Math.min(map.getMaxZoom(), base + delta / perLevel));
      // At a zoom limit there is nothing to animate; don't start a cycle.
      if (!this._active && next === map.getZoom()) return;
      this._pos = map.mouseEventToContainerPoint(e);
      if (!this._active) {
        map._stop();
        this._active = true;
        this._zoom = map.getZoom();
        this._starting = true;
        map._moveStart(true, false);
        this._starting = false;
      }
      this._target = next;
      this._snapped = false;
      this._lastWheel = performance.now();
      if (!this._frame) this._frame = L.Util.requestAnimFrame(this._step, this);
    },

    _step() {
      this._frame = null;
      const map = this._map;
      if (!this._active) return;
      const idle = performance.now() - this._lastWheel >= 120;
      if (idle && !this._snapped) {
        // Input has stopped: glide to the nearest zoomSnap multiple so raster
        // tiles rest crisp and the ± buttons step from a clean level.
        const snap = map.options.zoomSnap;
        if (snap) {
          const snapped = Math.round(this._target / snap) * snap;
          this._target = Math.max(map.getMinZoom(), Math.min(map.getMaxZoom(), snapped));
        }
        this._snapped = true;
      }
      let z = this._zoom + (this._target - this._zoom) * 0.3;
      if (Math.abs(this._target - z) < 0.001) z = this._target;
      if (z !== this._zoom) {
        // Keep the point under the cursor fixed (same math as setZoomAround).
        const scale = map.getZoomScale(z, this._zoom);
        const viewHalf = map.getSize().divideBy(2);
        const centerOffset = this._pos.subtract(viewHalf).multiplyBy(1 - 1 / scale);
        const newCenter = map.containerPointToLatLng(viewHalf.add(centerOffset));
        this._zoom = z;
        map._move(newCenter, z, { pinch: true, round: false });
      }
      if (z !== this._target || !idle) {
        this._frame = L.Util.requestAnimFrame(this._step, this);
      } else {
        this._end("full");
      }
    },

    // Stop the per-frame loop and close the zoom the way Leaflet's own
    // transition end does: a plain 'zoom' (so the tile layer prunes the
    // levels it kept during the pinch-style frames), then the end events.
    //   "full"     – nothing else is in progress: fire move, zoomend, moveend.
    //   "zoomOnly" – a drag or pan owns moveend; close only the zoom half.
    //   "none"     – a flyTo took over and fires everything itself.
    _end(mode) {
      if (!this._active) return;
      this._active = false;
      if (this._frame) {
        L.Util.cancelAnimFrame(this._frame);
        this._frame = null;
      }
      if (mode === "none") return;
      const map = this._map;
      map.fire("zoom");
      if (mode === "full") {
        map.fire("move");
        map._moveEnd(true);
      } else {
        map.fire("zoomend");
      }
    },

    _onOtherMoveStart() {
      if (!this._starting) this._end("zoomOnly");
    },

    _onOtherZoomStart() {
      if (!this._starting) this._end("none");
    },
  });
})();
