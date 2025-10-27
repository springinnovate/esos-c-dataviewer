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
  geoserverBaseUrl: null,
  baseStatsUrl: null,
  wmsLayerA: null,
  wmsLayerB: null,
  availableLayers: null,
  activeLayerIdxA: null,
  activeLayerIdxB: null,
  hoverRect: null,
  boxSizeKm: null,
  lastMouseLatLng: null,
  outlineLayer: null,
  lastStats: null,
  didInitialCenter: false,
  visibility: { A: true, B: true },
  lastScatterOpts: null,
  scatterObj: null,
  percentiles: null,
}

/**
 * Set the visibility of a specified WMS layer and synchronize its checkbox state.
 * @param {'A'|'B'} layerId - The identifier of the layer ('A' or 'B').
 * @param {boolean} visible - Whether the layer should be visible (true) or hidden (false).
 */
function setLayerVisibility(layerId, visible) {
  const layer = layerId === 'A' ? state.wmsLayerA : state.wmsLayerB
  if (layer) layer.setOpacity(visible ? 1 : 0)
  state.visibility[layerId] = visible
  const cb = document.getElementById(`layerVisible${layerId}`)
  if (cb) cb.checked = !!visible
}

/**
 * Apply the stored visibility state to a layer when it is created or reinitialized.
 * @param {'A'|'B'} layerId - The identifier of the layer ('A' or 'B').
 */
function attachInitialOpacity(layerId) {
  const layer = layerId === 'A' ? state.wmsLayerA : state.wmsLayerB
  if (!layer) return
  const visible = state.visibility[layerId] ?? true
  layer.setOpacity(visible ? 1 : 0)
}

/**
 * Initialize visibility checkboxes for layers A and B and wire their change events.
 * When a checkbox is toggled, it updates the corresponding layer's visibility.
 */
function wireVisibilityCheckboxes() {
  ;['A', 'B'].forEach(id => {
    const cb = document.getElementById(`layerVisible${id}`)
    if (!cb) return
    cb.checked = state.visibility[id]
    cb.addEventListener('change', () => setLayerVisibility(id, cb.checked))
  })
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

const MAX_HISTOGRAM_BINS = 50
const MAX_HISTOGRAM_POINTS = 20000
const CANADA_CENTER = [55, -96.9]
const INITIAL_ZOOM = 0
const GLOBAL_CRS = 'EPSG:3347'
const CRS3347 = new L.Proj.CRS(
  'EPSG:3347',
  '+proj=lcc +lat_1=49 +lat_2=77 +lat_0=63.390675 +lon_0=-91.8666666666667 +x_0=6200000 +y_0=3000000 +datum=NAD83 +units=m +no_defs',
  {
    origin: [0, 0],
    resolutions: [
      4096, 2048, 1024, 512, 256, 128, 64, 32, 16, 8, 4, 2, 1
    ]
  }
)

/**
 * Initialize the Leaflet map and overlay event swallowing.
 * Side effects: sets state.map and wires overlay interactions.
 */
function initMap() {
  const mapDiv = document.getElementById('map')
  const map = L.map(mapDiv, {
    crs: CRS3347,
    center: CANADA_CENTER,
    zoom: INITIAL_ZOOM,
    zoomControl: false,
  })
  state.map = map
  initMouseFollowBox()
  wireOverlayClose()
}

/**
 * Creates a non-interactive square polygon centered at a given geographic coordinate.
 *
 * The function projects the given latitude/longitude into `state.map`'s CRS, constructs
 * a square of the specified size in meters around that projected center, and converts
 * the corners back to latitude/longitude coordinates.
 *
 * @param {L.LatLng} centerLatLng - The geographic center of the square.
 * @param {number} windowSizeKm - The desired side length of the square in kilometers.
 * @returns {L.Polygon} A Leaflet polygon representing the square, styled with an orange outline
 *   and no fill, non-interactive.
 */
function squarePolygonAt(centerLatLng, windowSizeKm) {
  const crs = state.map.options.crs
  const half = windowSizeKm * 1000 / 2
  const p = crs.project(centerLatLng)
  const corners = [
    L.point(p.x - half, p.y - half),
    L.point(p.x + half, p.y - half),
    L.point(p.x + half, p.y + half),
    L.point(p.x - half, p.y + half),
  ].map(pt => crs.unproject(pt))
  // strong orange color to follow the cursor
  return L.polygon(corners, { color: '#ff6b00', weight: 2, fill: false, interactive: false })
}

/**
 * Compute a square LatLngBounds of given size (km) centered at a point.
 * @param {L.LatLng} centerLatLng
 * @param {number|string} windowSizeKm
 * @returns {L.LatLngBounds}
 */
function latLngBoundsForSquareKilometers(centerLatLng, windowSizeKm) {
  const crs = state.map.options.crs
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
    //colors are shades of blues
    color: '#0c63b8',
    fillColor: '#1e90ff',
    fillOpacity: 0.15,
    weight: 2,
    fill: true,
    interactive: false,
  })
  return state.outlineLayer
}

/**
 * Update and display the outline polygon on the map.
 * @param {{type:'Feature',geometry:{type:'Polygon',coordinates:number[][][]}}} polyGeoJSON
 * @private
 */
function _updateOutline(poly) {
 const layer = _ensureOutlineLayer()
 layer.setLatLngs(poly.getLatLngs())
 if (!state.map.hasLayer(layer)) layer.addTo(state.map)
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
  state.hoverRect = squarePolygonAt(state.map.getCenter(), state.boxSizeKm).addTo(state.map)
  state.map.on('mousemove', (e) => {
    state.lastMouseLatLng = e.latlng
    const poly = squarePolygonAt(e.latlng, state.boxSizeKm)
    state.hoverRect.setLatLngs(poly.getLatLngs())
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
function wireSquareSamplerControls() {
  const rRange = document.getElementById('windowSize')
  const rNum = document.getElementById('windowSizeNumber')

  const minKm = 1
  const maxKm = 1000

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))

  // pos in [0..100]  ->  km in [minKm..maxKm]
  function sliderToKm(pos) {
    const p = clamp(Number(pos) || 0, 0, 100)
    const exp = Math.sqrt(p / 100)
    return minKm * Math.pow(maxKm / minKm, exp)
  }

  // km in [minKm..maxKm] -> pos in [0..100]
  function kmToSlider(km) {
    const k = clamp(Number(km) || minKm, minKm, maxKm)
    const exp = Math.log(k / minKm) / Math.log(maxKm / minKm) // [0..1]
    return clamp(Math.pow(exp, 2) * 100, 0, 100)
  }

  function updateHoverRect() {
    if (!state.map || !state.hoverRect) return
    const ll = state.lastMouseLatLng || state.map.getCenter()
    const poly = squarePolygonAt(ll, state.boxSizeKm)
    state.hoverRect.setLatLngs(poly.getLatLngs())
  }

  function setFromSlider(pos) {
    const km = sliderToKm(pos)
    rRange.value = String(Math.round(clamp(pos, 0, 100)))
    rNum.value = km.toFixed(1)
    state.boxSizeKm = km
    updateHoverRect()
  }

  function setFromKm(km) {
    const pos = kmToSlider(km)
    setFromSlider(pos)
  }

  rRange.addEventListener('input', () => setFromSlider(Number(rRange.value)))
  rNum.addEventListener('input', () => setFromKm(Number(rNum.value)))

  const kmFromNum = parseFloat(rNum.value)
  if (Number.isFinite(kmFromNum)) {
    setFromKm(kmFromNum)        // honors the number input's value (e.g., 150)
  } else {
    setFromSlider(Number(rRange.value)) // fallback to slider's value (e.g., 50)
  }
}

/**
 * Populate both layer <select> elements with available WMS layers and wire change handlers.
 * Reads state.availableLayers and updates the DOM.
 */
function populateLayerSelects() {
  const fill = (selEl) => {
    selEl.innerHTML = ''
    state.availableLayers.forEach((lyr, idx) => {
      const opt = document.createElement('option')
      opt.value = idx.toString()
      opt.textContent = lyr.name
      selEl.appendChild(opt)
    })
  }
  ;['A', 'B'].forEach(layerId => {
    const sel = document.getElementById(`layerSelect${layerId}`)
    fill(sel)
    sel.addEventListener('change', e => onLayerChange(e, layerId))
  })
}

/**
 * Add a WMS layer to the map for the given qualified layer name and slot.
 * Replaces any existing layer in that slot. Slot 'A' is above 'B', className
 * adds any additional class to the layer probalby for styling.
 * @param {string} qualifiedName
 * @param {'A'|'B'} slot
 * @param {string} className
 */
function addWmsLayer(qualifiedName, slot, className) {
  const wmsUrl = `${state.geoserverBaseUrl}/wms`
  const params = {
    layers: qualifiedName,
    format: 'image/png',
    transparent: true,
    tiled: true,
    version: '1.1.1',
    className: className ?? (slot === 'A' ? 'blend-screen' : 'blend-base'),
  }
  const l = L.tileLayer.wms(wmsUrl, params)
  ;['A', 'B'].forEach(layerSlot => {
    const key = `wmsLayer${layerSlot}`
    if (slot === layerSlot) {
      if (state[key]) state.map.removeLayer(state[key])
      state[key] = l.addTo(state.map)
    }
  })

  // keep A on top if present
  if (state.wmsLayerA) state.wmsLayerA.bringToFront()

}

/**
 * Handle layer change from a <select>.
 * Updates stats + dynamic styling for layer A or B.
 * @param {Event & {target: HTMLSelectElement}} e
 * @param {'A'|'B'} layerId
 */
async function onLayerChange(e, layerId) {
  const idx = parseInt(e.target.value, 10)
  const lyr = state.availableLayers[idx]
  document.getElementById('statsOverlay').classList.add('hidden')
  document.getElementById('overlayBody').innerHTML = ''
  try {
    const res = await fetch(`${state.baseStatsUrl}/stats/minmax`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ raster_id: lyr.name }),
    })
    if (!res.ok) throw new Error(await res.text())
    const { min_, max_ } = await res.json()
    const med = (max_ + min_) / 2

    // write values into the correct panel (A or B)
    document.getElementById(`layer${layerId}MinInput`).value = min_
    document.getElementById(`layer${layerId}MedInput`).value = med
    document.getElementById(`layer${layerId}MaxInput`).value = max_

    // update state and map layer
    state[`activeLayerIdx${layerId}`] = idx
    const className = layerId === 'A' ? 'blend-screen' : 'blend-base'
    addWmsLayer(lyr.name, layerId, className)

    // apply style for this layer
    _applyDynamicStyle(layerId)
  } catch (err) {
    console.error(`Failed to fetch min/max for layer ${layerId}`, err)
  }
}

/**
 * Wire map click to request raster statistics for the window around the click.
 * On success, renders the overlay; on failure, shows an error message.
 */
async function wireAreaSamplerClick() {
  state.map.on('click', async (evt) => {
    const lyrA = state.availableLayers[state.activeLayerIdxA]
    const lyrB = state.availableLayers[state.activeLayerIdxB]
    if (!lyrA || !lyrB) return

    const poly = squarePolygonAt(evt.latlng, state.boxSizeKm)
    _updateOutline(poly)
    renderScatterOverlay({
      rasterX: lyrA.name,
      rasterY: lyrB.name,
      centerLng: evt.latlng.lng,
      centerLat: evt.latlng.lat,
      boxKm: state.boxSizeKm,
      scatterObj: null,
    })

    let scatterStats
    try {
      scatterStats = await fetchScatterStats(lyrA.name, lyrB.name, poly.toGeoJSON())
    } catch (e) {
      showOverlayError(`area sampler error: ${e.message || String(e)}`)
      return
    }
    renderScatterOverlay({
      rasterX: lyrA.name,
      rasterY: lyrB.name,
      centerLng: evt.latlng.lng,
      centerLat: evt.latlng.lat,
      boxKm: state.boxSizeKm,
      scatterObj: scatterStats,
    })
  })
}


/**
 * POST a geometry to the rstats service and return scatter data for two rasters.
 * @param {string} rasterIdX
 * @param {string} rasterIdY
 * @param {{type:'Feature'|'Polygon',geometry?:object}} geojson Feature or bare geometry in EPSG:4326
 * @returns {Promise<{x:number[],y:number[],hist2d:number[][],x_edges:number[],y_edges:number[],corr:number,slope:number,intercept:number}>}
 * @throws {Error} if the request fails
 */
async function fetchScatterStats(rasterIdX, rasterIdY, geojson) {
  const res = await fetch(`${state.baseStatsUrl}/stats/scatter`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      raster_id_x: rasterIdX,
      raster_id_y: rasterIdY,
      geometry: geojson.geometry ? geojson.geometry : geojson,
      from_crs: 'EPSG:4326', //the poly should be in lat/lng
      histogram_bins: MAX_HISTOGRAM_BINS,
      max_points: MAX_HISTOGRAM_POINTS,
      all_touched: true,
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
  })

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
    rect.setAttribute('stroke', '#0c63b8')
    rect.setAttribute('stroke-width', '0.5')
    svg.appendChild(rect)

    const lo = binEdges[i]
    const hi = binEdges[i + 1]
    const fmt = (n) => (typeof n === 'number' && isFinite(n)) ? n.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(n)
    const text = `Range: [${fmt(lo)}, ${fmt(hi)}) Count: ${v.toLocaleString()}`

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


/**
 * Build a GeoServer env string from a dict, skipping undefined values.
 * @param {Record<string, string|number|boolean>} obj
 * @returns {string}
 * @private
 */
function _buildEnvString(obj) {
  const entries = Object.entries(obj || {}).map(([k, v]) => {
    if (v == null) return null
    return `${k}:${v}`
  }).filter(Boolean)
  return entries.join(';')
}

/**
 * Read current style parameter values from the UI controls for a given layer.
 * @param {'A'|'B'} layerId
 * @returns {{min:number,med:number,max:number,cmin:string,cmed:string,cmax:string,ncolor:string}}
 */
function _readStyleInputsFromUI(layerId) {
  const get = (suffix) => document.getElementById(`layer${layerId}${suffix}`)
  return {
    min: get('MinInput')?.value,
    med: get('MedInput')?.value,
    max: get('MaxInput')?.value,
    cmin: get('CminInput')?.value,
    cmed: get('CmedInput')?.value,
    cmax: get('CmaxInput')?.value,
  }
}

/**
 * Apply a dynamic style to WMS layer A or B using the current UI values.
 * @param {'A'|'B'} layerId
 */
function _applyDynamicStyle(layerId) {
  const layer = state[`wmsLayer${layerId}`]
  if (!layer) return

  //adding a new style does not clear the old ones, so we do it manually
  delete layer.wmsParams?.sld
  delete layer.wmsParams?.sld_body

  const styleVars = _readStyleInputsFromUI(layerId)
  const env = _buildEnvString(styleVars)

  layer.setParams({ styles: 'esosc:dynamic_style', env, _t: Date.now() })
}

/**
 * Wire UI controls that manage dynamic raster styling parameters for layer A or B.
 * @param {'A'|'B'} layerId
 */
function wireDynamicStyleControls(layerId) {
  const update = () => _applyDynamicStyle(layerId)

  const suffixes = ['MinInput', 'MedInput', 'MaxInput', 'CminInput', 'CmedInput', 'CmaxInput']
  suffixes.forEach(suffix => {
    const el = document.getElementById(`layer${layerId}${suffix}`)
    if (el) el.addEventListener('input', update)
  })
  update()
}

/**
 * Enable Alt+mouse-wheel adjustment for the sampling window size slider.
 * @returns {void}
 */
function enableAltWheelSlider() {
  const slider = document.getElementById('windowSize')

  const clamp = (v) => {
    const min = parseFloat(slider.min) || 0
    const max = parseFloat(slider.max) || 100
    return Math.max(min, Math.min(max, v))
  }

  const apply = (v) => {
    const vv = clamp(v)
    slider.value = String(vv)
    // let wireSquareSamplerControls' 'input' handler drive boxSize + number display
    slider.dispatchEvent(new Event('input', { bubbles: true }))
  }

  const onKeyDown = (e) => {
    if (e.altKey && state?.map) state.map.scrollWheelZoom.disable()
  }
  const onKeyUp = () => {
    if (state?.map) state.map.scrollWheelZoom.enable()
  }

  const onWheel = (e) => {
    if (!e.altKey) return
    e.preventDefault()
    const delta = e.deltaY > 0 ? 1 : -1
    const step = parseFloat(slider.step) || 1
    const cur = parseFloat(slider.value)
    apply(cur - delta * step)
  }

  window.addEventListener('keydown', onKeyDown, true)
  window.addEventListener('keyup', onKeyUp, true)
  window.addEventListener('wheel', onWheel, { passive: false, capture: true })
}

/**
 * Render a scatterplot of two rasters' values within a polygon.
 * @param {{rasterX:string,rasterY:string,centerLng:number,centerLat:number,boxKm:number,scatterObj:object}} args
 */
function renderScatterOverlay(opts) {
  if (!opts) {
    return
  }
  state.lastScatterOpts = opts
  const visA = document.getElementById('layerVisibleA')?.checked ?? true;
  const visB = document.getElementById('layerVisibleB')?.checked ?? true;
  const {
    rasterX, rasterY,
    centerLng, centerLat,
    boxKm,
    scatterObj // normal behavior to be null if not generated yet
  } = opts

  const overlay = document.getElementById('statsOverlay')
  const body = document.getElementById('overlayBody')
  if (!overlay || !body) return

  const hasData = !!scatterObj

  const s = scatterObj || {}
  const stats = {
    n: parseInt(s.n_pairs) ?? null,
    r: s.pearson_r ?? null,
    slope: s.slope ?? null,
    intercept: s.intercept ?? null,
    window_mask_pixels: parseInt(s.window_mask_pixels) ?? null,
    valid_pixels: parseInt(s.valid_pixels) ?? null,
    coverage_ratio: s.coverage_ratio ?? null,
  }

  const fmt = (v, digits = 3) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(digits))
  body.innerHTML = `
    <div class='overlay-header'>
      <div>
        <div class='overlay-title'>${rasterX} <span class='muted'>vs</span> ${rasterY}</div>
        <div class='small-mono'>center: ${centerLng.toFixed(4)}, ${centerLat.toFixed(4)} - box: ${fmt(boxKm)} km</div>
      </div>
    </div>

    <div class='overlay-content'>
      <div>
        <div class='muted' style='margin-bottom:6px;'>Summary</div>
          <div class='stats-grid'>
            <div class='label'>n</div><div class='value' data-stat='n'>${hasData ? fmt(stats.n, 0) : '-'}</div>
            <div class='label'>r</div><div class='value' data-stat='r'>${hasData ? fmt(stats.r) : '-'}</div>
            <div class='label'>slope</div><div class='value' data-stat='slope'>${hasData ? fmt(stats.slope) : '-'}</div>
            <div class='label'>intercept</div><div class='value' data-stat='intercept'>${hasData ? fmt(stats.intercept) : '-'}</div>
            <div class='label'>window_mask_pixels</div><div class='value' data-stat='window_mask_pixels'>${hasData ? fmt(stats.window_mask_pixels, 0) : '-'}</div>
            <div class='label'>valid_pixels</div><div class='value' data-stat='valid_pixels'>${hasData ? fmt(stats.valid_pixels, 0) : '-'}</div>
            <div class='label'>coverage_ratio</div><div class='value' data-stat='coverage_ratio'>${hasData ? fmt(stats.coverage_ratio) : '-'}</div>
        </div>
      </div>

      <div>
        <div id='scatterPlot' class='plot-holder'>
          ${hasData ? '' : '<div class="spinner" aria-label="loading"></div>'}
        </div>
      </div>
    </div>
   `
  const plotEl = document.getElementById('scatterPlot');
  overlay.classList.remove('hidden');
  if (!visA && !visB) {
      plotEl.innerHTML = `<div class="no-layers-msg">
        <span> No layers selected</span>
      </div>`;
      return
  }
  if (!scatterObj) {
    return;
  }
  plotEl.innerHTML = '';
  const has2D =
    !!scatterObj &&
    Array.isArray(scatterObj.hist2d) &&
    Array.isArray(scatterObj.x_edges) &&
    Array.isArray(scatterObj.y_edges);

  const has1DX =
    Array.isArray(scatterObj.hist1d_x) && Array.isArray(scatterObj.x_edges);
  const has1DY =
    Array.isArray(scatterObj.hist1d_y) && Array.isArray(scatterObj.y_edges);

  // if either A or B is turned off, show a 1D histogram
  if (!visA && visB && has1DY) {
    // A off -> show histogram along Y axis
    const svg = buildHistogram1D(scatterObj.y_edges, scatterObj.hist1d_y, 'y', {
      width: 420,
      height: 320,
      pad: 40,
      percentiles: state.percentiles,
      layerId: 'B'
    });
    plotEl.appendChild(svg);
  }
  if (!visB && visA && has1DX) {
    // B off -> show histogram along X axis
    const svg = buildHistogram1D(scatterObj.x_edges, scatterObj.hist1d_x, 'x', {
      width: 420,
      height: 320,
      pad: 40,
      percentiles: state.percentiles,
      layerId: 'A'
    });
    plotEl.appendChild(svg);
  }

  // otherwise show 2D heatmap
  if (has2D) {
    const svg = buildScatterSVG(
      scatterObj.x_edges,
      scatterObj.y_edges,
      scatterObj.hist2d,
      { width: 420, height: 320, pad: 40, percentiles: state.percentiles,
       layerIdX: 'A', layerIdY: 'B' },

    );
    plotEl.appendChild(svg);
  }
  state.scatterObj = scatterObj;
}

// do i actually want this?
['layerVisibleA', 'layerVisibleB'].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('change', () => renderScatterOverlay(state.lastScatterOpts));
});


/**
 * Build a simple 1D histogram SVG using provided bin edges and counts.
 * axis: 'x' -> bars grow upward from bottom (x-axis bins)
 *       'y' -> bars grow rightward from left (y-axis bins)
 * @param {number[]} edges
 * @param {number[]} counts
 * @param {'x'|'y'} axis
 * @param {{width?:number,height?:number,pad?:number}} opts
 * @returns {SVGSVGElement}
 */
// use layer-based coloring in 1D histograms
function buildHistogram1D(edges, counts, axis = 'x', opts = {}) {
  const w = opts.width ?? 400;
  const h = opts.height ?? 300;
  const pad = opts.pad ?? 40;
  const layerId = opts.layerId; // 'A' | 'B' (optional)
  const innerW = w - pad * 2;
  const innerH = h - pad * 2;

  const n = Math.max(0, edges.length - 1);
  const maxCount = Math.max(1, ...counts.map(c => (Number.isFinite(c) ? c : 0)));
  const minVal = Math.min(...edges);
  const maxVal = Math.max(...edges);
  const domainSpan = Math.max(1e-9, maxVal - minVal);

  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('width', String(w));
  svg.setAttribute('height', String(h));
  svg.style.background = '#11151c';

  const axisColor = '#666';
  const mkLine = (x1, y1, x2, y2) => {
    const l = document.createElementNS(svgNS, 'line');
    l.setAttribute('x1', x1); l.setAttribute('y1', y1);
    l.setAttribute('x2', x2); l.setAttribute('y2', y2);
    l.setAttribute('stroke', axisColor); l.setAttribute('stroke-width', '1');
    return l;
  };
  const mkText = (txt, x, y, anchor = 'middle') => {
    const t = document.createElementNS(svgNS, 'text');
    t.textContent = txt;
    t.setAttribute('x', x); t.setAttribute('y', y);
    t.setAttribute('fill', '#aaa'); t.setAttribute('font-size', '10');
    t.setAttribute('text-anchor', anchor);
    return t;
  };

  if (axis === 'x') {
    svg.appendChild(mkLine(pad, h - pad, w - pad, h - pad));
    svg.appendChild(mkLine(pad, pad, pad, h - pad));
    const scaleX = v => pad + ((v - minVal) / domainSpan) * innerW;
    const scaleH = c => ((Number.isFinite(c) ? c : 0) / maxCount) * innerH;

    for (let i = 0; i < n; i++) {
      const x0 = scaleX(edges[i]);
      const x1 = scaleX(edges[i + 1]);
      const barW = Math.max(1, x1 - x0);
      const hPix = scaleH(counts[i] ?? 0);
      const mid = (edges[i] + edges[i + 1]) / 2;
      const fill = layerId ? _styleColorForValue(layerId, mid) : '#22c55e';

      const rect = document.createElementNS(svgNS, 'rect');
      rect.setAttribute('x', String(x0));
      rect.setAttribute('y', String(h - pad - hPix));
      rect.setAttribute('width', String(barW));
      rect.setAttribute('height', String(hPix));
      rect.setAttribute('fill', fill);
      rect.setAttribute('fill-opacity', '0.85');
      svg.appendChild(rect);
    }

    svg.appendChild(mkText(minVal.toFixed(2), pad, h - pad + 12, 'start'));
    svg.appendChild(mkText(maxVal.toFixed(2), w - pad, h - pad + 12, 'end'));
    svg.appendChild(mkText('0', pad - 6, h - pad, 'end'));
    svg.appendChild(mkText(String(maxCount), pad - 6, pad + 4, 'end'));
  } else {
    svg.appendChild(mkLine(pad, h - pad, w - pad, h - pad));
    svg.appendChild(mkLine(pad, pad, pad, h - pad));

    const scaleY = v => h - pad - ((v - minVal) / domainSpan) * innerH;
    const scaleW = c => ((Number.isFinite(c) ? c : 0) / maxCount) * innerW;

    for (let i = 0; i < n; i++) {
      const y0 = scaleY(edges[i]);
      const y1 = scaleY(edges[i + 1]);
      const barH = Math.max(1, y0 - y1);
      const wPix = scaleW(counts[i] ?? 0);
      const mid = (edges[i] + edges[i + 1]) / 2;
      const fill = layerId ? _styleColorForValue(layerId, mid) : '#eab308';

      const rect = document.createElementNS(svgNS, 'rect');
      rect.setAttribute('x', String(pad));
      rect.setAttribute('y', String(y1));
      rect.setAttribute('width', String(wPix));
      rect.setAttribute('height', String(barH));
      rect.setAttribute('fill', fill);
      rect.setAttribute('fill-opacity', '0.85');
      svg.appendChild(rect);
    }

    svg.appendChild(mkText('0', pad, h - pad + 12, 'middle'));
    svg.appendChild(mkText(minVal.toFixed(2), pad - 6, h - pad, 'end'));
    svg.appendChild(mkText(maxVal.toFixed(2), pad - 6, pad + 4, 'end'));
  }
  return svg;
}

/**
 * Build a simple 2D scatter/heatmap SVG from histogram2d data.
 * @param {number[]} xEdges
 * @param {number[]} yEdges
 * @param {number[][]} hist2d
 * @param {{width?:number,height?:number,pad?:number}} opts
 * @returns {SVGSVGElement}
 */
// color top/right histograms in scatter using layer styles
function buildScatterSVG(xEdges, yEdges, hist2d, opts = {}) {
  const w = opts.width ?? 400;
  const h = opts.height ?? 300;
  const pad = opts.pad ?? 40;
  const mSize = opts.marginalSize ?? 48;
  const percentileColor = opts.percentileColor ?? '#60a5fa';
  const percentileDecimals = Number.isFinite(opts.percentileDecimals) ? opts.percentileDecimals : 2;
  const percentilesRaw = Array.isArray(opts.percentiles) ? opts.percentiles : [];
  const blendMode = opts.blend || 'plus-lighter';
  const layerIdX = opts.layerIdX || 'A'; // which layer colors the top histogram
  const layerIdY = opts.layerIdY || 'B'; // which layer colors the right histogram

  const parsePercent = p => {
    if (typeof p === 'number' && Number.isFinite(p)) return p > 1 ? p / 100 : p;
    if (typeof p === 'string') {
      const s = p.trim(); if (!s) return null;
      const num = parseFloat(s);
      if (!Number.isFinite(num)) return null;
      return (s.endsWith('%') || num > 1) ? num / 100 : num;
    }
    return null;
  };
  const percentiles = [...new Set(percentilesRaw.map(parsePercent).filter(p => p !== null && p >= 0 && p <= 1))].sort((a,b)=>a-b);

  const innerW = Math.max(1, w - pad * 2 - mSize);
  const innerH = Math.max(1, h - pad * 2 - mSize);

  const xMin = Math.min(...xEdges), xMax = Math.max(...xEdges);
  const yMin = Math.min(...yEdges), yMax = Math.max(...yEdges);
  const nx = hist2d.length, ny = hist2d[0].length;

  const xCounts = new Array(nx).fill(0);
  const yCounts = new Array(ny).fill(0);
  let maxCount2d = 1;
  for (let i = 0; i < nx; i++) {
    let rowSum = 0;
    for (let j = 0; j < ny; j++) {
      const v = Number(hist2d[i][j]) || 0;
      rowSum += v; yCounts[j] += v; if (v > maxCount2d) maxCount2d = v;
    }
    xCounts[i] = rowSum;
  }
  const maxCountTop = Math.max(1, ...xCounts);
  const maxCountRight = Math.max(1, ...yCounts);

  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('width', String(w)); svg.setAttribute('height', String(h));
  svg.style.background = '#11151c';

  const axisColor = '#666';
  const mkLine = (x1,y1,x2,y2, stroke=axisColor, sw='1') => {
    const l = document.createElementNS(svgNS, 'line');
    l.setAttribute('x1', x1); l.setAttribute('y1', y1);
    l.setAttribute('x2', x2); l.setAttribute('y2', y2);
    l.setAttribute('stroke', stroke); l.setAttribute('stroke-width', sw);
    return l;
  };
  const mkText = (txt, x, y, anchor='middle') => {
    const t = document.createElementNS(svgNS, 'text');
    t.textContent = txt; t.setAttribute('x', x); t.setAttribute('y', y);
    t.setAttribute('fill', '#aaa'); t.setAttribute('font-size', '10'); t.setAttribute('text-anchor', anchor);
    return t;
  };

  const plotX0 = pad, plotY0 = pad + mSize;
  const plotX1 = pad + innerW, plotY1 = pad + mSize + innerH;
  const scaleX = v => plotX0 + ((v - xMin) / (xMax - xMin)) * innerW;
  const scaleY = v => plotY1 - ((v - yMin) / (yMax - yMin)) * innerH;

  for (let i = 0; i < nx; i++) {
    for (let j = 0; j < ny; j++) {
      const binCount = Number(hist2d[i][j]) || 0;
      if (binCount <= 0) continue;

      const x0 = scaleX(xEdges[i]), x1 = scaleX(xEdges[i + 1]);
      const y0 = scaleY(yEdges[j]), y1 = scaleY(yEdges[j + 1]);

      // midpoint values for color sampling
      const xMid = (xEdges[i] + xEdges[i + 1]) / 2;
      const yMid = (yEdges[j] + yEdges[j + 1]) / 2;

      // colors from current UI styles for A (x) and B (y)
      const colA = _styleColorArrForValue(layerIdX, xMid);
      const colB = _styleColorArrForValue(layerIdY, yMid);

      // blend
      const blended =
        blendMode === 'screen' ? _blendScreenRGB(colA, colB) : _blendPlusLighterRGB(colA, colB);

      const rect = document.createElementNS(svgNS, 'rect');
      rect.setAttribute('x', String(x0));
      rect.setAttribute('y', String(y1));
      rect.setAttribute('width', String(x1 - x0));
      rect.setAttribute('height', String(y0 - y1));

      const t = Math.log1p(binCount) / Math.log1p(maxCount2d); // [0,1]
      const alpha = 0.05 + 0.95 * Math.pow(t, 1.2);

      rect.setAttribute('fill', `rgb(${blended[0]},${blended[1]},${blended[2]})`);
      rect.setAttribute('fill-opacity', alpha.toFixed(3));
      svg.appendChild(rect);
    }
  }

  // axes + labels
  svg.appendChild(mkLine(plotX0, plotY1, plotX1, plotY1));
  svg.appendChild(mkLine(plotX0, plotY0, plotX0, plotY1));
  svg.appendChild(mkText(xMin.toFixed(2), plotX0, plotY1 + 12, 'start'));
  svg.appendChild(mkText(xMax.toFixed(2), plotX1, plotY1 + 12, 'end'));
  svg.appendChild(mkText(yMin.toFixed(2), plotX0 - 6, plotY1, 'end'));
  svg.appendChild(mkText(yMax.toFixed(2), plotX0 - 6, plotY0 + 4, 'end'));

  // top histogram (x)
  const topY1 = pad + mSize, topY0 = pad;
  const topInnerH = Math.max(1, mSize - 6);
  const scaleTopH = c => ((Number.isFinite(c) ? c : 0) / maxCountTop) * topInnerH;
  for (let i = 0; i < nx; i++) {
    const x0 = scaleX(xEdges[i]), x1 = scaleX(xEdges[i + 1]);
    const barW = Math.max(1, x1 - x0);
    const hPix = scaleTopH(xCounts[i]);
    const mid = (xEdges[i] + xEdges[i + 1]) / 2;
    const fill = _styleColorForValue(layerIdX, mid);
    const rect = document.createElementNS(svgNS, 'rect');
    rect.setAttribute('x', String(x0)); rect.setAttribute('y', String(topY1 - hPix));
    rect.setAttribute('width', String(barW)); rect.setAttribute('height', String(hPix));
    rect.setAttribute('fill', fill); rect.setAttribute('fill-opacity', '0.85');
    svg.appendChild(rect);
  }

  // right histogram (y)
  const rightX0 = pad + innerW, rightX1 = pad + innerW + mSize;
  const rightInnerW = Math.max(1, mSize - 6);
  const scaleRightW = c => ((Number.isFinite(c) ? c : 0) / maxCountRight) * rightInnerW;
  for (let j = 0; j < ny; j++) {
    const y0 = scaleY(yEdges[j]), y1 = scaleY(yEdges[j + 1]);
    const barH = Math.max(1, y0 - y1);
    const wPix = scaleRightW(yCounts[j]);
    const mid = (yEdges[j] + yEdges[j + 1]) / 2;
    const fill = _styleColorForValue(layerIdY, mid);
    const rect = document.createElementNS(svgNS, 'rect');
    rect.setAttribute('x', String(rightX1 - wPix)); rect.setAttribute('y', String(y1));
    rect.setAttribute('width', String(wPix)); rect.setAttribute('height', String(barH));
    rect.setAttribute('fill', fill); rect.setAttribute('fill-opacity', '0.85');
    svg.appendChild(rect);
  }

  const totalX = xCounts.reduce((a,b)=>a+(Number.isFinite(b)?b:0),0);
  const totalY = yCounts.reduce((a,b)=>a+(Number.isFinite(b)?b:0),0);
  const getQuantileX = q => {
    if (totalX <= 0) return xMin;
    const target = q * totalX; let cum = 0;
    for (let i = 0; i < nx; i++) {
      const c = Number.isFinite(xCounts[i]) ? xCounts[i] : 0;
      const next = cum + c; if (target <= next) {
        const e0 = xEdges[i], e1 = xEdges[i+1]; const f = c>0 ? (target-cum)/c : 0;
        return e0 + f * (e1 - e0);
      } cum = next;
    } return xMax;
  };
  const getQuantileY = q => {
    if (totalY <= 0) return yMin;
    const target = q * totalY; let cum = 0;
    for (let j = 0; j < ny; j++) {
      const c = Number.isFinite(yCounts[j]) ? yCounts[j] : 0;
      const next = cum + c; if (target <= next) {
        const e0 = yEdges[j], e1 = yEdges[j+1]; const f = c>0 ? (target-cum)/c : 0;
        return e0 + f * (e1 - e0);
      } cum = next;
    } return yMax;
  };
  const pctLabel = (p, val) => `${Math.round(p * 100)}% (${val.toFixed(percentileDecimals)})`;
  const attachPctHover = (guideEl, lblEl, text) => {
    [guideEl, lblEl].forEach(el => {
      el.style.cursor = 'pointer';
      el.addEventListener('mouseenter', e => {
        guideEl.setAttribute('stroke-width', '2'); guideEl.setAttribute('opacity', '1');
        if (typeof _showPctTooltip === 'function') _showPctTooltip(text, e.clientX, e.clientY);
      });
      el.addEventListener('mousemove', e => {
        if (typeof _showPctTooltip === 'function') _showPctTooltip(text, e.clientX, e.clientY);
      });
      el.addEventListener('mouseleave', () => {
        guideEl.setAttribute('stroke-width', '1'); guideEl.setAttribute('opacity', '0.9');
        if (typeof _hidePctTooltip === 'function') _hidePctTooltip();
      });
    });
  };

  for (const p of percentiles) {
    const xv = getQuantileX(p), x = scaleX(xv);
    const gx = mkLine(x, pad, x, plotY1, percentileColor);
    gx.setAttribute('stroke-dasharray', '4,3'); gx.setAttribute('opacity', '0.9');
    svg.appendChild(gx);
    const lx = mkText(pctLabel(p, xv), x, pad - 6, 'middle');
    lx.setAttribute('fill', percentileColor); svg.appendChild(lx);
    attachPctHover(gx, lx, `${Math.round(p*100)}% • ${xv.toFixed(percentileDecimals)}`);
  }
  for (const p of percentiles) {
    const yv = getQuantileY(p), y = scaleY(yv);
    const gy = mkLine(plotX0, y, pad + innerW + mSize, y, percentileColor);
    gy.setAttribute('stroke-dasharray', '4,3'); gy.setAttribute('opacity', '0.9');
    svg.appendChild(gy);
    const ly = mkText(pctLabel(p, yv), pad + innerW + mSize + 4, y + 3, 'start');
    ly.setAttribute('fill', percentileColor); svg.appendChild(ly);
    attachPctHover(gy, ly, `${Math.round(p*100)}% • ${yv.toFixed(percentileDecimals)}`);
  }

  return svg;
}

/**
 * Prevent Leaflet map zooming when the Alt key is held during scroll.
 * @returns {void}
 */
function disableLeafletScrollOnAlt() {
  const mapEl = state.map.getContainer()
  mapEl.addEventListener('wheel', e => {
    if (e.altKey) {
      e.preventDefault()
      e.stopImmediatePropagation()
    }
  }, { passive: false, capture: true })
}

/**
 * Wire the "flip layers" button to toggle visibility between Layer A and Layer B.
 *
 * When the button with ID 'flipLayersBtn' is clicked, this function inverts
 * the visibility state of the two layer checkboxes ('layerVisibleA' and
 * 'layerVisibleB') so that only one layer is visible at a time.
 *
 * It then:
 * - Dispatches 'change' events for both checkboxes to trigger any external listeners.
 * - Calls `setLayerVisibility()` if available to update map layers directly.
 * - Otherwise, adjusts layer opacity and `state.visibility` manually.
 *
 * This ensures the UI checkboxes, visibility state, and rendered layers
 * remain synchronized when the user flips layers.
 */
function wireLayerFlipper() {
  document.getElementById('flipLayersBtn')?.addEventListener('click', () => {
    const cbA = document.getElementById('layerVisibleA');
    const cbB = document.getElementById('layerVisibleB');
    if (!cbA || !cbB) return;

    const aOn = !!cbA.checked;
    const bOn = !!cbB.checked;

    const nextAOn = !(aOn && !bOn);
    cbA.checked = nextAOn;
    cbB.checked = !nextAOn;

    cbA.dispatchEvent(new Event('change', { bubbles: true }));
    cbB.dispatchEvent(new Event('change', { bubbles: true }));

    setLayerVisibility('A', cbA.checked);
    setLayerVisibility('B', cbB.checked);
  });
document.getElementById('bothLayersOnBtn')?.addEventListener('click', () => {
    const cbA = document.getElementById('layerVisibleA');
    const cbB = document.getElementById('layerVisibleB');
    cbA.checked = true
    cbB.checked = true

    cbA.dispatchEvent(new Event('change', { bubbles: true }));
    cbB.dispatchEvent(new Event('change', { bubbles: true }));

    setLayerVisibility('A', cbA.checked);
    setLayerVisibility('B', cbB.checked);
  });
}

/**
 * Wire the "Set Min/Med/Max from Histogram" button to automatically
 * populate layer style value inputs based on the current histogram ranges.
 *
 * When the button with ID 'applyAutoStyleBtn' is clicked:
 * - It reads `state.scatterObj.x_edges` and `state.scatterObj.y_edges`, which
 *   represent the histogram bin edges for Layer A (x-axis) and Layer B (y-axis).
 * - For each layer, it computes the minimum, median, and maximum edge values.
 * - It fills the corresponding input fields:
 *   `layer{A,B}MinInput`, `layer{A,B}MedInput`, and `layer{A,B}MaxInput`.
 * - Each updated input dispatches a 'change' event so downstream listeners update.
 * - Finally, it calls `renderScatterOverlay()` to refresh the plot.
 *
 */
function wireAutoStyleFromHistogram() {
  const btn = document.getElementById('applyAutoStyleBtn');
  if (!btn) return;

  const getMinMedMaxFromEdges = (edges) => {
    if (!edges || !edges.length) return null;
    const min = edges[0];
    const max = edges[edges.length - 1];
    const mid = (edges.length - 1) / 2;
    const med = Number.isInteger(mid) ? edges[mid] : (edges[Math.floor(mid)] + edges[Math.ceil(mid)]) / 2;
    return { min, med, max };
  };

  const setTriple = (layerId, triple) => {
    if (!triple) return;
    const fmt = (v) => Number.isFinite(v) ? +v.toPrecision(6) : '';
    const minEl = document.getElementById(`layer${layerId}MinInput`);
    const medEl = document.getElementById(`layer${layerId}MedInput`);
    const maxEl = document.getElementById(`layer${layerId}MaxInput`);
    if (minEl) { minEl.value = fmt(triple.min); minEl.dispatchEvent(new Event('input', { bubbles: true }))};
    if (medEl) { medEl.value = fmt(triple.med); medEl.dispatchEvent(new Event('input', { bubbles: true }))};
    if (maxEl) { maxEl.value = fmt(triple.max); maxEl.dispatchEvent(new Event('input', { bubbles: true }))};
  };

  btn.addEventListener('click', () => {
    const so = state?.scatterObj;
    const xEdges = so?.x_edges;
    const yEdges = so?.y_edges;
    const a = getMinMedMaxFromEdges(xEdges);
    const b = getMinMedMaxFromEdges(yEdges);
    setTriple('A', a);
    setTriple('B', b);

    if (typeof renderScatterOverlay === 'function') {
      renderScatterOverlay(state?.lastScatterOpts);
    }
  });
}

function wirePercentiles() {
  const percentilesInput = document.getElementById('percentiles')
  if (!percentilesInput) return

  let raf = null
  const rerender = () => {
    // ensure we pass a scatterObj so it renders immediately (1D or 2D as appropriate)
    if (state?.lastScatterOpts && state?.scatterObj) {
      const opts = { ...state.lastScatterOpts, scatterObj: state.scatterObj }
      renderScatterOverlay(opts)
    }
  }

  percentilesInput.addEventListener('input', () => {
    const raw = percentilesInput.value
    const nums = raw
      .split(/[,\s]+/)
      .map(s => parseInt(s.trim(), 10))
      .filter(n => Number.isFinite(n))
      .sort((a, b) => a - b)

    state.percentiles = nums

    // throttle to next frame to avoid redraw per keystroke burst
    if (raf) cancelAnimationFrame(raf)
    raf = requestAnimationFrame(rerender)
  })
}

/**
 * Ensure that a single reusable DOM element for displaying percentile tooltips exists.
 * Creates a fixed-position, styled <div> appended to the document body if none exists.
 * @returns {HTMLDivElement} The tooltip element.
 */
function _ensurePctTooltip() {
  let tip = document.querySelector('.pct-tooltip')
  if (!tip) {
    tip = document.createElement('div')
    tip.className = 'pct-tooltip'
    Object.assign(tip.style, {
      position: 'fixed',
      background: '#111',
      color: '#fff',
      padding: '4px 6px',
      borderRadius: '4px',
      fontSize: '11px',
      pointerEvents: 'none',
      display: 'none',
      zIndex: 9999
    })
    document.body.appendChild(tip)
  }
  return tip
}


/**
 * Display the percentile tooltip near the specified screen coordinates.
 * Updates its text and position relative to the mouse pointer.
 * @param {string} text - Tooltip content text.
 * @param {number} x - Mouse X coordinate (in client space).
 * @param {number} y - Mouse Y coordinate (in client space).
 */
function _showPctTooltip(text, x, y) {
  const tip = _ensurePctTooltip()
  tip.textContent = text
  tip.style.left = `${x + 8}px`
  tip.style.top = `${y + 8}px`
  tip.style.display = 'block'
}

/**
 * Hide the percentile tooltip if currently visible.
 * Clears its display without removing the element from the DOM.
 */
function _hidePctTooltip() {
  const tip = document.querySelector('.pct-tooltip')
  if (tip) tip.style.display = 'none'
}

/**
 * Convert a hexadecimal color string (e.g., '#ffcc00' or 'fc0') to an RGB array.
 * @param {string} hex - Hexadecimal color string.
 * @returns {[number, number, number]} Array of [r, g, b] values (0–255).
 */
function _hexToRgb(hex) {
  const s = String(hex || '').trim();
  const m = s.match(/^#?([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (!m) return [136, 136, 136];
  let h = m[1];
  if (h.length === 3) h = h.split('').map(ch => ch + ch).join('');
  const n = parseInt(h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/**
 * Interpolate between two RGB colors and return a CSS 'rgb(...)' string.
 * @param {[number, number, number]} c1 - Starting RGB color.
 * @param {[number, number, number]} c2 - Ending RGB color.
 * @param {number} t - Interpolation fraction (0–1).
 * @returns {string} CSS color string in 'rgb(r,g,b)' format.
 */
function _interpRgb(c1, c2, t) {
  const u = 1 - t;
  const r = Math.round(u * c1[0] + t * c2[0]);
  const g = Math.round(u * c1[1] + t * c2[1]);
  const b = Math.round(u * c1[2] + t * c2[2]);
  return `rgb(${r},${g},${b})`;
}

/**
 * Compute an interpolated CSS RGB color for a given numeric value based on
 * the active style inputs (min/med/max and their colors) for a specified layer.
 * @param {'A'|'B'} layerId - Layer identifier.
 * @param {number} v - Numeric value to colorize.
 * @returns {string} CSS color string ('rgb(r,g,b)').
 */
function _styleColorForValue(layerId, v) {
  const s = _readStyleInputsFromUI(layerId);
  const min = parseFloat(s.min), med = parseFloat(s.med), max = parseFloat(s.max);
  const cmin = _hexToRgb(s.cmin || '#000000');
  const cmed = _hexToRgb(s.cmed || '#888888');
  const cmax = _hexToRgb(s.cmax || '#ffffff');
  if (!Number.isFinite(v) || !Number.isFinite(min) || !Number.isFinite(med) || !Number.isFinite(max)) return 'rgb(136,136,136)';
  if (v <= min) return `rgb(${cmin[0]},${cmin[1]},${cmin[2]})`;
  if (v >= max) return `rgb(${cmax[0]},${cmax[1]},${cmax[2]})`;
  if (v <= med) {
    const t = (v - min) / Math.max(1e-9, (med - min));
    return _interpRgb(cmin, cmed, t);
  } else {
    const t = (v - med) / Math.max(1e-9, (max - med));
    return _interpRgb(cmed, cmax, t);
  }
}

/**
 * Interpolate between two RGB color arrays and return a new [r,g,b] array.
 * @param {[number, number, number]} c1 - Starting color.
 * @param {[number, number, number]} c2 - Ending color.
 * @param {number} t - Interpolation fraction (0–1).
 * @returns {[number, number, number]} Interpolated RGB array.
 */
function _interpRgbArr(c1, c2, t) {
  const u = 1 - t;
  return [
    Math.round(u * c1[0] + t * c2[0]),
    Math.round(u * c1[1] + t * c2[1]),
    Math.round(u * c1[2] + t * c2[2]),
  ];
}

/**
 * Compute an interpolated RGB array for a numeric value given the current
 * style parameters (min/med/max and their colors) for a specific layer.
 * @param {'A'|'B'} layerId - Layer identifier ('A' or 'B').
 * @param {number} v - Numeric value to colorize.
 * @returns {[number, number, number]} RGB array representing the color.
 */
function _styleColorArrForValue(layerId, v) {
  const s = _readStyleInputsFromUI(layerId);
  const min = parseFloat(s.min), med = parseFloat(s.med), max = parseFloat(s.max);
  const cmin = _hexToRgb(s.cmin || '#000000');
  const cmed = _hexToRgb(s.cmed || '#888888');
  const cmax = _hexToRgb(s.cmax || '#ffffff');
  if (!Number.isFinite(v) || !Number.isFinite(min) || !Number.isFinite(med) || !Number.isFinite(max)) return [136,136,136];
  if (v <= min) return cmin.slice();
  if (v >= max) return cmax.slice();
  if (v <= med) {
    const t = (v - min) / Math.max(1e-9, (med - min));
    return _interpRgbArr(cmin, cmed, t);
  } else {
    const t = (v - med) / Math.max(1e-9, (max - med));
    return _interpRgbArr(cmed, cmax, t);
  }
}

/**
 * Combine two RGB colors using additive blending (approximation of CSS 'plus-lighter').
 * Each channel is summed and clamped to 255.
 * @param {[number, number, number]} a - First RGB color.
 * @param {[number, number, number]} b - Second RGB color.
 * @returns {[number, number, number]} Blended RGB color array.
 */
function _blendPlusLighterRGB(a, b) {
  return [
    Math.min(255, a[0] + b[0]),
    Math.min(255, a[1] + b[1]),
    Math.min(255, a[2] + b[2]),
  ];
}

/**
 * Combine two RGB colors using screen blending mode.
 * Equivalent to CSS 'screen' mix-blend-mode calculation.
 * @param {[number, number, number]} a - First RGB color.
 * @param {[number, number, number]} b - Second RGB color.
 * @returns {[number, number, number]} Blended RGB color array.
 */
function _blendScreenRGB(a, b) {
  return [
    255 - Math.round((255 - a[0]) * (255 - b[0]) / 255),
    255 - Math.round((255 - a[1]) * (255 - b[1]) / 255),
    255 - Math.round((255 - a[2]) * (255 - b[2]) / 255),
  ];
}


/**
 * App entrypoint.
 */
;(async function main() {

  // Orange vs turquoise axis
  document.getElementById('layerACminInput').value = '#000000'
  document.getElementById('layerACmedInput').value = '#ff8000'
  document.getElementById('layerACmaxInput').value = '#ffcc00'
  document.getElementById('layerBCminInput').value = '#000000'
  document.getElementById('layerBCmedInput').value = '#00b3b3'
  document.getElementById('layerBCmaxInput').value = '#00ffff'
  initMap()
  wireSquareSamplerControls()
  wireLayerFlipper()
  wireAreaSamplerClick()
  enableAltWheelSlider()
  disableLeafletScrollOnAlt()
  wireVisibilityCheckboxes()
  wireAutoStyleFromHistogram()
  wirePercentiles()

  const cfg = await loadConfig()
  state.geoserverBaseUrl = cfg.geoserver_base_url
  state.availableLayers = cfg.layers
  state.baseStatsUrl = cfg.rstats_base_url

  ;['A', 'B'].forEach(layerId => wireDynamicStyleControls(layerId))
  populateLayerSelects()
  ;['A', 'B'].forEach((layerId, idx) => {
    const sel = document.getElementById(`layerSelect${layerId}`)
    if (state.availableLayers.length > idx) {
      sel.value = String(idx)
      sel.dispatchEvent(new Event('change', { bubbles: true }))
    }
  })

  // rounding the displayed number down so it fits
  const numInput = document.getElementById('windowSizeNumber');
  numInput.addEventListener('change', () => {
    numInput.value = parseFloat(numInput.value).toFixed(1);
  });
})()
