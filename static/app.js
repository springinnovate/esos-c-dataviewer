// static/app.js
/* global L */

/**
 * @file
 * Frontend map + stats overlay for rstats service.
 * Uses Leaflet WMS for visualization and fetches raster stats for a user-defined square window.
 * Docstrings use JSDoc so editors/TS can infer types.
 */

const state = {
  map: null,
  baseUrl: '',
  baseStatsUrl: '',
  wmsLayer: null,
  layers: [],
  activeLayerIdx: 0,
  hoverRect: null,
  boxSizeKm: 10,
  lastMouseLatLng: null,
  outlineLayer: null,
  lastStats: null,
}

/**
 * Load app configuration from the server.
 * @returns {Promise<{geoserver_base_url:string, rstats_base_url:string, layers:Array}>}
 * @throws {Error} if the request fails
 */
async function loadConfig() {
  const res = await fetch('api/config')
  if (!res.ok) throw new Error('Failed to load config')
  return res.json()
}

/**
 * Initialize the Leaflet map and overlay event swallowing.
 * Side effects: sets state.map and wires overlay interactions.
 */
function initMap() {
  const mapDiv = document.getElementById('map')
  const map = L.map(mapDiv, {
    center: [37.8, -96.9],
    zoom: 4,
    zoomControl: false,
  })
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
  }).addTo(map)
  state.map = map
  initMouseFollowBox()
  wireOverlayClose()

  const overlay = document.getElementById('statsOverlay')
  L.DomEvent.disableClickPropagation(overlay)
  L.DomEvent.disableScrollPropagation(overlay)
}

/**
 * Compute a square LatLngBounds of given size (km) centered at a point.
 * @param {L.LatLng} centerLatLng
 * @param {number|string} windowSizeKm
 * @returns {L.LatLngBounds}
 */
function latLngBoundsForSquareKilometers(centerLatLng, windowSizeKm) {
  const crs = state.map.options.crs || L.CRS.EPSG3857
  const halfSizeM = (Number(windowSizeKm) || 0) * 1000 / 2
  const p = crs.project(centerLatLng)
  const sw = crs.unproject(L.point(p.x - halfSizeM, p.y - halfSizeM))
  const ne = crs.unproject(L.point(p.x + halfSizeM, p.y + halfSizeM))
  return L.latLngBounds(sw, ne)
}

/**
 * Convert first ring of a GeoJSON Polygon to Leaflet [lat,lng] pairs.
 * @param {{type:'Feature',geometry:{type:'Polygon',coordinates:number[][][]}}} polyGeoJSON
 * @returns {Array<[number,number]>} Array of [lat,lng]
 * @private
 */
function _latlngsFromPoly(polyGeoJSON) {
  return polyGeoJSON.geometry.coordinates[0].map(([lng, lat]) => [lat, lng])
}

/**
 * Ensure a single outline polygon exists for the current selection.
 * @returns {L.Polygon} outline layer
 * @private
 */
function _ensureOutlineLayer() {
  if (state.outlineLayer) return state.outlineLayer
  state.outlineLayer = L.polygon([], {
    color: '#1e90ff',
    weight: 2,
    fill: false,
    interactive: false,
  })
  return state.outlineLayer
}

/**
 * Update and display the outline polygon on the map.
 * @param {{type:'Feature',geometry:{type:'Polygon',coordinates:number[][][]}}} polyGeoJSON
 * @private
 */
function _updateOutline(polyGeoJSON) {
  const latlngs = _latlngsFromPoly(polyGeoJSON)
  const layer = _ensureOutlineLayer()
  layer.setLatLngs(latlngs)
  if (!state.map.hasLayer(layer)) layer.addTo(state.map)
}

/**
 * Hide the outline layer if present.
 * @private
 */
function _hideOutline() {
  if (state.outlineLayer && state.map.hasLayer(state.outlineLayer)) {
    state.map.removeLayer(state.outlineLayer)
  }
}

/**
 * Build a square GeoJSON Polygon centered at a LatLng with edge length windowSizeKm.
 * @param {L.LatLng} centerLatLng
 * @param {number|string} windowSizeKm
 * @returns {{type:'Feature',geometry:{type:'Polygon',coordinates:number[][][]},properties:{kind:string,halfSizeM:number}}}
 */
function squarePolygonGeoJSON(centerLatLng, windowSizeKm) {
  const crs = state.map.options.crs || L.CRS.EPSG3857
  const halfSizeM = (Number(windowSizeKm) || 0) * 1000 / 2
  const p = crs.project(centerLatLng)
  const cornersM = [
    [p.x - halfSizeM, p.y - halfSizeM],
    [p.x + halfSizeM, p.y - halfSizeM],
    [p.x + halfSizeM, p.y + halfSizeM],
    [p.x - halfSizeM, p.y + halfSizeM],
  ]
  const ringLngLat = cornersM
    .map(([x, y]) => crs.unproject(L.point(x, y)))
    .map(ll => [ll.lng, ll.lat])
  ringLngLat.push(ringLngLat[0])
  return {
    type: 'Feature',
    geometry: { type: 'Polygon', coordinates: [ringLngLat] },
    properties: { kind: 'square', halfSizeM },
  }
}

/**
 * Create and keep a rectangle that follows the mouse to show the sampling window.
 * Side effects: sets state.hoverRect and mouse listeners that update it.
 */
function initMouseFollowBox() {
  state.hoverRect = L.rectangle(
    latLngBoundsForSquareKilometers(state.map.getCenter(), state.boxSizeKm),
    { color: '#ff6b00', weight: 2, fill: false, interactive: false }
  ).addTo(state.map)

  state.map.on('mousemove', (e) => {
    state.lastMouseLatLng = e.latlng
    state.hoverRect.setBounds(latLngBoundsForSquareKilometers(e.latlng, state.boxSizeKm))
  })

  state.map.on('mouseout', () => {
    if (state.hoverRect && state.map.hasLayer(state.hoverRect)) state.map.removeLayer(state.hoverRect)
  })
  state.map.on('mouseover', () => {
    if (state.hoverRect && !state.map.hasLayer(state.hoverRect)) state.hoverRect.addTo(state.map)
  })
}

/**
 * Wire UI controls that set the sampling window size (km).
 * Keeps range and numeric inputs in sync and updates hover rectangle.
 */
function wireRadiusControls() {
  const rRange = document.getElementById('windowSize')
  const rNum = document.getElementById('windowSizeNumber')

  const clamp = (v) => {
    const min = Number(rRange?.min) || 0
    const max = Number(rRange?.max) || 1000
    return Math.max(min, Math.min(max, v))
  }

  const setVal = (v) => {
    const vAsInt = Number.parseInt(v, 10)
    const vv = clamp(Number.isNaN(vAsInt) ? 0 : vAsInt)
    rRange.value = String(vv)
    rNum.value = String(vv)
    state.boxSizeKm = vv
    if (state.hoverRect) {
      const ll = state.lastMouseLatLng || state.map.getCenter()
      state.hoverRect.setBounds(latLngBoundsForSquareKilometers(ll, state.boxSizeKm))
    }
  }

  rRange.addEventListener('input', () => setVal(rRange.value))
  rNum.addEventListener('input', () => setVal(rNum.value))
  setVal(rRange.value)
}

/**
 * Populate the layer <select> with available WMS layers and wire change handler.
 * Reads state.layers and updates the DOM.
 */
function populateLayerSelect() {
  const sel = document.getElementById('layerSelect')
  sel.innerHTML = ''
  state.layers.forEach((lyr, idx) => {
    const opt = document.createElement('option')
    opt.value = idx.toString()
    opt.textContent = lyr.name
    sel.appendChild(opt)
  })
  sel.addEventListener('change', onLayerChange)
}

/**
 * Add a WMS layer to the map for the given qualified layer name.
 * Replaces any existing state.wmsLayer.
 * @param {string} qualifiedName
 */
function addWmsLayer(qualifiedName) {
  if (state.wmsLayer) {
    state.map.removeLayer(state.wmsLayer)
    state.wmsLayer = null
  }
  const wmsUrl = `${state.baseUrl}/wms`
  const params = {
    layers: qualifiedName,
    format: 'image/png',
    transparent: true,
    tiled: true,
    version: '1.1.1',
  }
  const l = L.tileLayer.wms(wmsUrl, params)
  l.addTo(state.map)
  state.wmsLayer = l
  applyDefaultDynamicStyle()
}


/**
 * Handle layer change from the <select>.
 * Sets active layer, updates WMS, hides overlay, and clears outline.
 * @param {Event & {target: HTMLSelectElement}} e
 */
function onLayerChange(e) {
  const idx = parseInt(e.target.value, 10)
  const lyr = state.layers[idx]
  if (!lyr) return
  state.activeLayerIdx = idx
  addWmsLayer(lyr.name)
  // close the stats window if open
  document.getElementById('statsOverlay').classList.add('hidden')
  document.getElementById('overlayBody').innerHTML = ''
  _hideOutline()
}

/**
 * Wire the opacity range control to the current WMS layer.
 * No-op if no WMS layer loaded.
 */
function wireOpacity() {
  const r = document.getElementById('opacityRange')
  r.addEventListener('input', () => {
    if (state.wmsLayer) state.wmsLayer.setOpacity(parseFloat(r.value))
  })
}

/**
 * Wire map click to request raster statistics for the window around the click.
 * On success, renders the overlay; on failure, shows an error message.
 */
function wireAreaSamplerClick() {
  state.map.on('click', async (evt) => {
    const lyr = state.layers[state.activeLayerIdx]
    if (!lyr) return
    const rasterId = lyr.raster_id || lyr.name

    const poly = squarePolygonGeoJSON(evt.latlng, state.boxSizeKm)
    _updateOutline(poly)

    let stats
    try {
      stats = await fetchGeometryStats(rasterId, poly)
    } catch (e) {
      showOverlayError(`Error: ${e.message || String(e)}`)
      return
    }

    renderAreaStatsOverlay({
      rasterId,
      centerLng: evt.latlng.lng,
      centerLat: evt.latlng.lat,
      boxKm: state.boxSizeKm,
      statsObj: stats.stats,
      units: stats.units
    })
    applyDefaultDynamicStyle(stats.stats)
})}


/**
 * POST a geometry to the rstats service and return statistics.
 * @param {string} rasterId
 * @param {{type:'Feature'|'Polygon',geometry?:object}} geojson Feature or bare geometry in EPSG:4326
 * @returns {Promise<{stats:object, units?:string}>}
 * @throws {Error} if the request fails
 */
async function fetchGeometryStats(rasterId, geojson) {
  const res = await fetch(`${state.baseStatsUrl}/stats/geometry`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      raster_id: rasterId,
      geometry: (geojson.geometry ? geojson.geometry : geojson),
      from_crs: 'EPSG:4326',
    }),
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

/**
 * Wire close button and event swallowing for the stats overlay.
 * Side effects: registers multiple event listeners on #statsOverlay.
 */
function wireOverlayClose() {
  const btn = document.getElementById('overlayClose')
  btn.addEventListener('click', (e) => {
    e.preventDefault()
    e.stopPropagation()
    document.getElementById('statsOverlay').classList.add('hidden')
    document.getElementById('overlayBody').innerHTML = ''
    _hideOutline()
  })

  // Prevent overlay interactions from bubbling to the map
  const overlay = document.getElementById('statsOverlay')
  ;['mousedown','mouseup','click','dblclick','contextmenu','touchstart','pointerdown','pointerup']
    .forEach(evt => overlay.addEventListener(evt, ev => ev.stopPropagation()))
}

/**
 * Show an error message inside the stats overlay.
 * @param {string} msg
 */
function showOverlayError(msg) {
  const overlay = document.getElementById('statsOverlay')
  const body = document.getElementById('overlayBody')
  overlay.classList.remove('hidden')
  body.innerHTML = `<pre>${msg}</pre>`
}

/**
 * Zoom to the outline bounds if present; otherwise center and zoom near the given point.
 * @param {number} centerLng
 * @param {number} centerLat
 * @private
 */
function _zoomToOutline(centerLng, centerLat) {
  if (state.outlineLayer && state.map.hasLayer(state.outlineLayer)) {
    const b = state.outlineLayer.getBounds()
    if (b && b.isValid()) {
      state.map.fitBounds(b, { padding: [24, 24] })
      return
    }
  }
  state.map.setView([centerLat, centerLng], Math.max(state.map.getZoom(), 12))
}

/**
 * Render the stats overlay content, including summary lines and an optional histogram.
 * @param {{rasterId:string,centerLng:number,centerLat:number,boxKm:number,statsObj?:object,units?:string}} args
 */
function renderAreaStatsOverlay({ rasterId, centerLng, centerLat, boxKm, statsObj, units }) {
  const overlay = document.getElementById('statsOverlay')
  const body = document.getElementById('overlayBody')
  overlay.classList.remove('hidden')

  const s = statsObj || {}
  state.lastStats = s
  const setIf = (id, val) => {
    const el = document.getElementById(id)
    if (el && Number.isFinite(val)) el.value = String(val)
  }
  setIf('minInput', s.min)
  setIf('medInput', s.median)
  setIf('maxInput', s.max)

  const centerRow = document.createElement('div')
  centerRow.className = 'overlay-row'
  const centerBtn = document.createElement('button')
  centerBtn.className = 'link-btn'
  centerBtn.type = 'button'
  centerBtn.textContent = `Center: ${centerLng.toFixed(6)}, ${centerLat.toFixed(6)}`
  centerBtn.addEventListener('click', (e) => {
    e.preventDefault()
    e.stopPropagation()
    _zoomToOutline(centerLng, centerLat)
  })
  centerRow.appendChild(centerBtn)

  const lines = [
    `Layer: ${rasterId}`,
    `Box size: ${boxKm} km`,
    '',
    `Count: ${s.count ?? 0}`,
    `Mean: ${numFmt(s.mean)}`,
    `Median: ${numFmt(s.median)}`,
    `Min: ${numFmt(s.min)}`,
    `Max: ${numFmt(s.max)}`,
    `Sum: ${numFmt(s.sum)}`,
    `Std Dev: ${numFmt(s.std)}`,
    '',
    `Valid pixels: ${s.valid_pixels ?? 0}`,
    `Nodata pixels: ${s.nodata_pixels ?? 0}`,
    `Coverage: ${pctFmt(s.coverage_ratio)}`,
    '',
    `Valid area: ${areaFmt(s.valid_area_m2)}`,
    `Total mask area: ${areaFmt(s.window_mask_area_m2)}`,
    units ? `Units: ${units}` : null,
  ].filter(Boolean)

  const pre = document.createElement('pre')
  pre.textContent = lines.join('\n')

  body.innerHTML = ''
  body.appendChild(centerRow)
  body.appendChild(pre)

  if (Array.isArray(s.hist) && Array.isArray(s.bin_edges) && s.hist.length > 0 && s.bin_edges.length === s.hist.length + 1) {
    const histTitle = document.createElement('div')
    histTitle.style.marginTop = '0.5rem'
    histTitle.textContent = 'Histogram'
    body.appendChild(histTitle)

    const svg = buildHistogramSVG(s.hist, s.bin_edges, { width: 420, height: 140, pad: 30 })
    body.appendChild(svg)

    const label = document.createElement('div')
    label.style.display = 'flex'
    label.style.justifyContent = 'space-between'
    label.style.fontSize = '12px'
    label.style.color = '#aaa'
    label.style.marginTop = '2px'
    label.innerHTML = `<span>${numFmt(s.bin_edges[0])}</span><span>${numFmt(s.bin_edges[s.bin_edges.length - 1])}</span>`
    body.appendChild(label)
  }

  function numFmt(v) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(3) : '—' }
  function pctFmt(v) { return (typeof v === 'number' && isFinite(v)) ? (v * 100).toFixed(1) + '%' : '—' }
  function areaFmt(m2){ return (typeof m2 === 'number' && isFinite(m2)) ? (m2 / 1e6).toFixed(3) + ' km²' : '—' }
}

/**
 * Build a simple SVG histogram with per-bar tooltips.
 * @param {number[]|ArrayLike<number>} hist Bin counts
 * @param {number[]} binEdges Bin edges, length = hist.length + 1
 * @param {{width?:number,height?:number,pad?:number}} [opts]
 * @returns {SVGSVGElement}
 */
function buildHistogramSVG(hist, binEdges, opts = {}) {
  const width = opts.width ?? 420
  const height = opts.height ?? 140
  const pad = opts.pad ?? 30
  const w = width, h = height
  const innerW = Math.max(1, w - pad * 2)
  const innerH = Math.max(1, h - pad * 2)

  const counts = Array.from(hist, v => Math.max(0, Number(v) || 0))
  const maxCount = Math.max(1, ...counts)
  const bins = counts.length
  const barW = innerW / Math.max(1, bins)

  const svgNS = 'http://www.w3.org/2000/svg'
  // we need the ..NS one to create its own namespace because svg is xml not
  // html and this guards it. so any element that needs its own namespace
  // needs this
  const svg = document.createElementNS(svgNS, 'svg')
  svg.setAttribute('width', String(w))
  svg.setAttribute('height', String(h))
  svg.style.background = '#11151c'

  const axisColor = '#666'
  const mkLine = (x1, y1, x2, y2) => {
    const Ln = document.createElementNS(svgNS, 'line')
    Ln.setAttribute('x1', x1); Ln.setAttribute('y1', y1)
    Ln.setAttribute('x2', x2); Ln.setAttribute('y2', y2)
    Ln.setAttribute('stroke', axisColor)
    Ln.setAttribute('stroke-width', '1')
    return Ln
  }
  svg.appendChild(mkLine(String(pad), String(h - pad), String(w - pad), String(h - pad)))
  svg.appendChild(mkLine(String(pad), String(pad), String(pad), String(h - pad)))

  for (let i = 0; i < bins; i++) {
    const v = counts[i]
    const barH = (v / maxCount) * innerH
    const safeH = Math.max((v > 0 ? 1 : 0), barH)
    const x = pad + i * barW + 1
    const y = h - pad - safeH

    const rect = document.createElementNS(svgNS, 'rect')
    rect.setAttribute('x', String(x))
    rect.setAttribute('y', String(y))
    rect.setAttribute('width', String(Math.max(0, barW - 2)))
    rect.setAttribute('height', String(Math.max(0, safeH)))
    rect.setAttribute('fill', '#1e90ff')
    rect.setAttribute('opacity', '0.9')
    rect.setAttribute('stroke', '#0c63b8')
    rect.setAttribute('stroke-width', '0.5')
    svg.appendChild(rect)

    const lo = binEdges[i]
    const hi = binEdges[i + 1]
    const fmt = (n) => (typeof n === 'number' && isFinite(n)) ? n.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(n)
    const text = `Range: [${fmt(lo)}, ${fmt(hi)})\nCount: ${v.toLocaleString()}`

    rect.addEventListener('mouseenter', (ev) => {
      _showHistTooltip(text, ev.clientX, ev.clientY, document.getElementById('statsOverlay'))
    })
    rect.addEventListener('mousemove', (ev) => {
      _showHistTooltip(text, ev.clientX, ev.clientY, document.getElementById('statsOverlay'))
    })
    rect.addEventListener('mouseleave', () => _hideHistTooltip())
    rect.addEventListener('touchstart', (ev) => {
      const t = ev.touches[0]
      _showHistTooltip(text, t.clientX, t.clientY, document.getElementById('statsOverlay'))
    }, { passive: true })
    rect.addEventListener('touchend', () => _hideHistTooltip())
  }

  const mkText = (str, x, y) => {
    const t = document.createElementNS(svgNS, 'text')
    t.setAttribute('x', String(x))
    t.setAttribute('y', String(y))
    t.setAttribute('fill', '#aaa')
    t.setAttribute('font-size', '10')
    t.setAttribute('text-anchor', 'end')
    t.textContent = str
    return t
  }
  svg.appendChild(mkText('0', pad - 4, h - pad + 3))
  svg.appendChild(mkText(String(Math.max(...counts)), pad - 4, pad + 3))

  svg.addEventListener('mouseleave', () => _hideHistTooltip())

  return svg
}

/**
 * Ensure a singleton tooltip element exists for histogram tooltips.
 * @returns {HTMLDivElement}
 * @private
 */
function _ensureHistTooltip() {
  let tip = document.querySelector('.hist-tooltip')
  if (!tip) {
    tip = document.createElement('div')
    tip.className = 'hist-tooltip'
    tip.style.display = 'none'
    document.body.appendChild(tip)
  }
  return tip
}

/**
 * Show and position the histogram tooltip within the overlay.
 * @param {string} text
 * @param {number} clientX
 * @param {number} clientY
 * @param {HTMLElement} anchorEl Overlay element to position within
 * @private
 */
function _showHistTooltip(text, clientX, clientY, anchorEl) {
  const tip = _ensureHistTooltip()
  tip.textContent = text
  const r = anchorEl.getBoundingClientRect()
  tip.style.left = `${clientX - r.left}px`
  tip.style.top = `${clientY - r.top}px`
  tip.style.display = 'block'
}

/**
 * Hide the histogram tooltip if present.
 * @private
 */
function _hideHistTooltip() {
  const tip = document.querySelector('.hist-tooltip')
  if (tip) tip.style.display = 'none'
}

function _clearStyleParams(layer) {
  if (!layer) return
  delete layer.wmsParams.styles
  delete layer.wmsParams.sld
  delete layer.wmsParams.sld_body
  delete layer.wmsParams.env
}

/**
 * Build a GeoServer env string from a dict, skipping undefined values.
 * Colors may be passed with or without '#'.
 * @param {Record<string, string|number|boolean>} obj
 * @returns {string}
 * @private
 */
function _buildEnvString(obj) {
  const normColor = (v) => {
    if (v == null) return v
    const s = String(v).trim()
    return s.startsWith('#') ? s : '#' + s
  }
  const entries = Object.entries(obj || {}).map(([k, v]) => {
    if (v == null) return null
    if (['cmin','cmed','cmax','ncolor'].includes(k)) v = normColor(v)
    return `${k}:${v}`
  }).filter(Boolean)
  return entries.join(';')
}

/**
 * Apply a published GeoServer named style and pass dynamic env() vars.
 * The SLD must use env('min'), env('med'), env('max'), env('cmin'), env('cmed'),
 * env('cmax'), env('opacity'), and optionally env('nval'), env('nopacity'), env('ncolor')
 * @param {string} styleName e.g. 'workspace:agb_biomass_dynamic'
 * @param {{
 *   min:number, med:number, max:number,
 *   cmin:string, cmed:string, cmax:string, // '#rrggbb'
 *   opacity?:number, nval?:number, nopacity?:number, ncolor?:string
 * }} vars
 */
function setDynamicNamedStyle(styleName, vars) {
  if (!state.wmsLayer) return
  _clearStyleParams(state.wmsLayer)
  const env = _buildEnvString(vars)
  state.wmsLayer.setParams({ styles: styleName, env })
}

/**
 * Apply a dynamic style to the active WMS layer using stats for min/median/max.
 * Computes median or mid value from stats and updates GeoServer env vars.
 * @param {object} s Stats object returned by /stats/geometry
 */
function applyDefaultDynamicStyle(s) {
  if (!s || !state.wmsLayer) return

  // Derive numeric range from stats safely
  const minVal = Number.isFinite(s.min) ? s.min : 0
  const maxVal = Number.isFinite(s.max) ? s.max : minVal + 1
  const medVal = Number.isFinite(s.median)
    ? s.median
    : minVal + (maxVal - minVal) / 2

  const toCol = (el) => String(el?.value || '').trim()
  setDynamicNamedStyle('esosc:dynamic_style', {
    min: minVal,
    med: medVal,
    max: maxVal,
    cmin: toCol(document.getElementById('cminInput')),
    cmed: toCol(document.getElementById('cmedInput')),
    cmax: toCol(document.getElementById('cmaxInput')),
    opacity: 0.9,
    nval: -9999,
    nopacity: 0.0,
    ncolor: '#000000',
  })
}

// --- Optional: hook up a simple UI for live updates ---
// Example: sliders/inputs with ids: minInput, medInput, maxInput, cminInput, cmedInput, cmaxInput, opacityInput
function wireDynamicStyleControls() {
  const get = (id) => document.getElementById(id)

  const update = () => {
    const toNum = (el) => Number(el?.value)
    const toCol = (el) => String(el?.value || '').trim()
    setDynamicNamedStyle('esosc:dynamic_style', {
      min: toNum(get('minInput')),
      med: toNum(get('medInput')),
      max: toNum(get('maxInput')),
      cmin: toCol(get('cminInput')),
      cmed: toCol(get('cmedInput')),
      cmax: toCol(get('cmaxInput')),
      opacity: Number(get('opacityRange')?.value ?? 1),
    })
  }

  ;['minInput','medInput','maxInput','cminInput','cmedInput','cmaxInput','opacityRange']
    .forEach(id => get(id)?.addEventListener('input', update))

  // "Use stats" button wires stats -> inputs -> update
  const btn = get('styleFromStatsBtn')
  if (btn) {
    btn.addEventListener('click', () => {
      const s = state.lastStats || {}
      const minVal = Number.isFinite(s.min) ? s.min : 0
      const maxVal = Number.isFinite(s.max) ? s.max : minVal + 1
      const medVal = Number.isFinite(s.median) ? s.median : minVal + (maxVal - minVal) / 2

      if (get('minInput')) get('minInput').value = String(minVal)
      if (get('medInput')) get('medInput').value = String(medVal)
      if (get('maxInput')) get('maxInput').value = String(maxVal)
      update()
    })
  }

  // initialize once
  update()
}


/**
 * App entrypoint.
 * Initializes UI, loads config, and selects the first layer if available.
 * Self-invoking to avoid leaking names.
 */
;(async function main() {
  initMap()
  wireOpacity()
  wireRadiusControls()
  wireAreaSamplerClick()
  wireDynamicStyleControls()

  const cfg = await loadConfig()
  state.baseUrl = cfg.geoserver_base_url
  state.layers = cfg.layers
  state.baseStatsUrl = cfg.rstats_base_url

  populateLayerSelect()
  if (state.layers.length > 0) {
    document.getElementById('layerSelect').value = '0'
    addWmsLayer(state.layers[0].name)

  }
})()

