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
  geoserverBaseUrl: '',
  baseStatsUrl: '',
  // two display layers
  wmsLayerA: null, // primary
  wmsLayerB: null, // secondary
  layers: [],
  activeLayerIdxA: 0,
  activeLayerIdxB: null,
  hoverRect: null,
  boxSizeKm: 10,
  lastMouseLatLng: null,
  outlineLayer: null,
  lastStats: null,
  didInitialCenter: false
}

// helper: center on centroid of Layer A only once
async function _centerOnLayerACentroidOnce(qualifiedName) {
  if (state.didInitialCenter || !state.map || !state.geoserverBaseUrl) return
  try {
    const url = `${state.geoserverBaseUrl}/wms?service=WMS&request=GetCapabilities&version=1.1.1`
    const res = await fetch(url, { method: 'GET' })
    if (!res.ok) throw new Error('capabilities fetch failed')
    const text = await res.text()
    const doc = new DOMParser().parseFromString(text, 'application/xml')

    // find the matching <Layer><Name>qualifiedName</Name></Layer>
    const names = Array.from(doc.getElementsByTagName('Name'))
    const nameNode = names.find(n => ((n.textContent || '').split(':').pop().trim()) === qualifiedName)
    if (!nameNode) throw new Error('layer name not found in capabilities')

    let layerEl = nameNode.parentElement
    // climb to the nearest Layer element if needed
    while (layerEl && layerEl.tagName !== 'Layer') layerEl = layerEl.parentElement
    if (!layerEl) throw new Error('layer element not found')

    // WMS 1.1.1 LatLonBoundingBox or WMS 1.3.0 EX_GeographicBoundingBox fallback
    const llbb = layerEl.getElementsByTagName('LatLonBoundingBox')[0]
    let minx, miny, maxx, maxy
    if (llbb) {
      minx = parseFloat(llbb.getAttribute('minx'))
      miny = parseFloat(llbb.getAttribute('miny'))
      maxx = parseFloat(llbb.getAttribute('maxx'))
      maxy = parseFloat(llbb.getAttribute('maxy'))
    } else {
      const exg = layerEl.getElementsByTagName('EX_GeographicBoundingBox')[0]
      if (!exg) throw new Error('no geographic bbox')
      const west = exg.getElementsByTagName('westBoundLongitude')[0]
      const east = exg.getElementsByTagName('eastBoundLongitude')[0]
      const south = exg.getElementsByTagName('southBoundLatitude')[0]
      const north = exg.getElementsByTagName('northBoundLatitude')[0]
      minx = parseFloat(west.textContent)
      maxx = parseFloat(east.textContent)
      miny = parseFloat(south.textContent)
      maxy = parseFloat(north.textContent)
    }

    if (
      [minx, miny, maxx, maxy].some(v => !Number.isFinite(v)) ||
      minx >= maxx || miny >= maxy
    ) throw new Error('invalid bbox')

    const lon = (minx + maxx) / 2
    const lat = (miny + maxy) / 2
    state.map.setView([lat, lon], Math.max(state.map.getZoom(), 6))
    state.didInitialCenter = true
  } catch (_e) {
    // no-op on failure; leave map as-is
  }
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

const CRS4326 = new L.Proj.CRS(
  'EPSG:4326',
  '+proj=longlat +datum=WGS84 +no_defs',
  {
    origin: [-180, 90],
    resolutions: [
      1.40625, 0.703125, 0.3515625, 0.17578125,
      0.087890625, 0.0439453125, 0.02197265625,
      0.010986328125, 0.0054931640625, 0.00274658203125
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
    center: [55, -96.9], //[37.8, -96.9],
    zoom: 0,
    zoomControl: false,
  })
  /*L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    crs: CRS3347,
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
  }).addTo(map)*/
  state.map = map
  initMouseFollowBox()
  wireOverlayClose()

  const overlay = document.getElementById('statsOverlay')
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
function wireSquareSamplerControls() {
  const rRange = document.getElementById('windowSize')
  const rNum = document.getElementById('windowSizeNumber')

  // Configure the log mapping range
  const min = 1
  const max = 1000

  function sliderToLog(val) {
    const exp = Math.pow(val / 100, 0.5)
    return min * Math.pow(max / min, exp)
  }

  const setVal = (v) => {
    const vAsInt = Number.parseInt(v, 10)
    const logValue = sliderToLog(vAsInt)
    rRange.value = String(vAsInt)
    rNum.value = String(logValue)
    state.boxSizeKm = logValue
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
 * Populate both layer <select> elements with available WMS layers and wire change handlers.
 * Reads state.layers and updates the DOM.
 */
function populateLayerSelects() {
  const fill = (selEl) => {
    selEl.innerHTML = ''
    state.layers.forEach((lyr, idx) => {
      const opt = document.createElement('option')
      opt.value = idx.toString()
      opt.textContent = lyr.name
      selEl.appendChild(opt)
    })
  }

  const selA = document.getElementById('layerSelectA')
  const selB = document.getElementById('layerSelectB')
  fill(selA)
  fill(selB)

  selA.addEventListener('change', (e) => onLayerChange(e, 'A'))
  selB.addEventListener('change', (e) => onLayerChange(e, 'B'))
}

/**
 * Add a WMS layer to the map for the given qualified layer name and slot.
 * Replaces any existing layer in that slot. Slot 'A' is above 'B'.
 * @param {string} qualifiedName
 * @param {'A'|'B'} slot
 */
function addWmsLayer(qualifiedName, slot) {
  const wmsUrl = `${state.geoserverBaseUrl}/wms`
  const params = {
    layers: qualifiedName,
    format: 'image/png',
    transparent: true,
    tiled: true,
    version: '1.1.1',
    crs: CRS3347,
    className: slot === 'A' ? 'blend-screen' : 'blend-base',
  }
  const l = L.tileLayer.wms(wmsUrl, params)

  // remove old
  if (slot === 'A') {
    if (state.wmsLayerA) state.map.removeLayer(state.wmsLayerA)
    state.wmsLayerA = l.addTo(state.map)
    //state.wmsLayerA.setOpacity(1.0)
    // keep A on top
    if (state.wmsLayerB) state.wmsLayerA.bringToFront()
  } else {
    if (state.wmsLayerB) state.map.removeLayer(state.wmsLayerB)
    state.wmsLayerB = l.addTo(state.map)
    // keep A on top if present
    if (state.wmsLayerA) state.wmsLayerA.bringToFront()
    //state.wmsLayerB.setOpacity(1.0)
  }
}

/**
 * Handle layer change from a <select>.
 * Updates stats + dynamic styling for layer A or B.
 * @param {Event & {target: HTMLSelectElement}} e
 * @param {'A'|'B'} layerId
 */
async function onLayerChange(e, layerId) {
  const idx = parseInt(e.target.value, 10)
  const lyr = state.layers[idx]
  document.getElementById('statsOverlay').classList.add('hidden')
  document.getElementById('overlayBody').innerHTML = ''
  _hideOutline()

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
    addWmsLayer(lyr.name, layerId)

    // apply style for this layer
    _applyDynamicStyle(layerId)
  } catch (err) {
    console.error(`Failed to fetch min/max for layer ${layerId}`, err)
  }
}

/**
 * Wire map click to request raster statistics for the window around the click.
 * On success, renders the overlay; on failure, shows an error message.
 * Uses the primary active layer (A).
 */
async function wireAreaSamplerClick() {
  state.map.on('click', async (evt) => {
    const lyrA = state.layers[state.activeLayerIdxA]
    const lyrB = state.layers[state.activeLayerIdxB]
    if (!lyrA || !lyrB) return

    const poly = squarePolygonGeoJSON(evt.latlng, state.boxSizeKm)
    _updateOutline(poly)

    // show placeholder overlay immediately
    renderScatterOverlay({
      rasterX: lyrA.name,
      rasterY: lyrB.name,
      centerLng: evt.latlng.lng,
      centerLat: evt.latlng.lat,
      boxKm: state.boxSizeKm,
      scatterObj: null,
    })

    let scatter
    try {
      scatter = await fetchScatterStats(lyrA.name, lyrB.name, poly)
    } catch (e) {
      showOverlayError(`Scatter error: ${e.message || String(e)}`)
      return
    }

    // update overlay with real data
    renderScatterOverlay({
      rasterX: lyrA.name,
      rasterY: lyrB.name,
      centerLng: evt.latlng.lng,
      centerLat: evt.latlng.lat,
      boxKm: state.boxSizeKm,
      scatterObj: scatter,
    })
  })
}

/**
 * Render multiple stats blocks one after another in the overlay.
 * @param {{centerLng:number,centerLat:number,boxKm:number,blocks:Array<{rasterId:string,statsObj?:object,units?:string,error?:string}>}} args
 */
function renderAreaStatsOverlayMulti({ centerLng, centerLat, boxKm, blocks }) {
  const overlay = document.getElementById('statsOverlay')
  const body = document.getElementById('overlayBody')
  overlay.classList.remove('hidden')
  body.innerHTML = ''

  // top row with center/zoom control
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
  body.appendChild(centerRow)

  // blocks
  blocks.forEach((blk, i) => {
    if (i > 0) {
      const hr = document.createElement('hr')
      hr.style.border = '0'
      hr.style.borderTop = '1px solid var(--border)'
      hr.style.margin = '8px 0'
      body.appendChild(hr)
    }
    body.appendChild(buildStatsBlock(blk.rasterId, boxKm, blk.statsObj, blk.units, blk.error))
  })
}

/**
 * Build a single stats block element (title, summary lines, optional histogram or error).
 * @param {string} rasterId
 * @param {number} boxKm
 * @param {object|undefined} statsObj
 * @param {string|undefined} units
 * @param {string|undefined} error
 * @returns {HTMLElement}
 */
function buildStatsBlock(rasterId, boxKm, statsObj, units, error) {
  const wrap = document.createElement('div')

  const title = document.createElement('div')
  title.style.fontWeight = '600'
  title.style.marginBottom = '4px'
  title.textContent = rasterId
  wrap.appendChild(title)

  if (error) {
    const pre = document.createElement('pre')
    pre.textContent = `Box size: ${boxKm} km\nError: ${error}`
    wrap.appendChild(pre)
    return wrap
  }

  const s = statsObj || {}
  const lines = [
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
  wrap.appendChild(pre)

  if (Array.isArray(s.hist) && Array.isArray(s.bin_edges) && s.hist.length > 0 && s.bin_edges.length === s.hist.length + 1) {
    const histTitle = document.createElement('div')
    histTitle.style.marginTop = '0.5rem'
    histTitle.textContent = 'Histogram'
    wrap.appendChild(histTitle)

    const svg = buildHistogramSVG(s.hist, s.bin_edges, { width: 420, height: 140, pad: 30 })
    wrap.appendChild(svg)

    const label = document.createElement('div')
    label.style.display = 'flex'
    label.style.justifyContent = 'space-between'
    label.style.fontSize = '12px'
    label.style.color = '#aaa'
    label.style.marginTop = '2px'
    label.innerHTML = `<span>${numFmt(s.bin_edges[0])}</span><span>${numFmt(s.bin_edges[s.bin_edges.length - 1])}</span>`
    wrap.appendChild(label)
  }

  return wrap

  function numFmt(v) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(3) : '—' }
  function pctFmt(v) { return (typeof v === 'number' && isFinite(v)) ? (v * 100).toFixed(1) + '%' : '—' }
  function areaFmt(m2){ return (typeof m2 === 'number' && isFinite(m2)) ? (m2 / 1e6).toFixed(3) + ' km²' : '—' }
}


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
      from_crs: 'EPSG:4326',
      bins: 50,
      max_points: 20000,
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
 * Normalize a color string to ensure it begins with a '#' prefix.
 * @param {string|number|null|undefined} v
 * @returns {string}
 */
function _normColor(v) {
  if (v == null) return ''
  const s = String(v).trim()
  return s ? (s.startsWith('#') ? s : '#' + s) : ''
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
    if (['cmin','cmed','cmax','ncolor'].includes(k)) v = _normColor(v)
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
  const toNum = (el) => Number(el?.value)
  const toCol = (el) => _normColor(el?.value)
  return {
    min: toNum(get('MinInput')),
    med: toNum(get('MedInput')),
    max: toNum(get('MaxInput')),
    cmin: toCol(get('CminInput')),
    cmed: toCol(get('CmedInput')),
    cmax: toCol(get('CmaxInput')),
  }
}

/**
 * Apply a dynamic style to WMS layer A or B using the current UI values.
 * @param {'A'|'B'} layerId
 */
function _applyDynamicStyle(layerId) {
  const layer = state[`wmsLayer${layerId}`]
  if (!layer) return

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
  const number = document.getElementById('windowSizeNumber')

  const clamp = (v) => {
    const min = parseFloat(slider.min) || 0
    const max = parseFloat(slider.max) || 1000
    return Math.max(min, Math.min(max, v))
  }

  const apply = (v) => {
    const vv = clamp(v)
    slider.value = String(vv)
    slider.dispatchEvent(new Event('input', { bubbles: true }))
    if (number) number.value = String(vv)
  }

  const onKeyDown = (e) => {
    if (e.altKey && window.state?.map) window.state.map.scrollWheelZoom.disable()
  }
  const onKeyUp = () => {
    if (window.state?.map) window.state.map.scrollWheelZoom.enable()
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
 * Render a scatterplot of two rasters’ values within a polygon.
 * @param {{rasterX:string,rasterY:string,centerLng:number,centerLat:number,boxKm:number,scatterObj:object}} args
 */
function renderScatterOverlay(opts) {
  const {
    rasterX, rasterY,
    centerLng, centerLat,
    boxKm,
    scatterObj // optional
  } = opts

  const overlay = document.getElementById('statsOverlay')
  const body = document.getElementById('overlayBody')
  if (!overlay || !body) return

  const hasData = !!scatterObj

  // derive stats (optional keys guarded)
  const s = scatterObj || {}
  const stats = {
    n: s.n ?? s.n_pairs ?? null,
    r: s.r ?? s.pearson_r ?? null,
    slope: s.slope ?? null,
    intercept: s.intercept ?? null,
    window_mask_pixels: s.window_mask_pixels ?? null,
    valid_pixels: s.valid_pixels ?? null,
    coverage_ratio: s.coverage_ratio ?? null,

  }

  const fmt = (v, digits = 3) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(digits))

  // replace the inline grid div with class="overlay-content"
   body.innerHTML = `
     <div class='overlay-header'>
       <div>
         <div class='overlay-title'>${rasterX} <span class='muted'>vs</span> ${rasterY}</div>
         <div class='small-mono'>center: ${centerLng.toFixed(4)}, ${centerLat.toFixed(4)} • box: ${boxKm} km</div>
       </div>
     </div>

     <div class='overlay-content'>

       <div>
         <div class='muted' style='margin-bottom:6px;'>Summary</div>
         <div class='stats-grid'>
           <div class='label'>n</div><div class='value' data-stat='n'>${hasData ? fmt(stats.n, 0) : '—'}</div>
           <div class='label'>r</div><div class='value' data-stat='r'>${hasData ? fmt(stats.r) : '—'}</div>
           <div class='label'>slope</div><div class='value' data-stat='slope'>${hasData ? fmt(stats.slope) : '—'}</div>
           <div class='label'>intercept</div><div class='value' data-stat='intercept'>${hasData ? fmt(stats.intercept) : '—'}</div>
           <div class='label'>window_mask_pixels</div><div class='value' data-stat='window_mask_pixels'>${hasData ? fmt(stats.window_mask_pixels) : '-'}</div>
           <div class='label'>valid_pixels</div><div class='value' data-stat='valid_pixels'>${hasData ? fmt(stats.valid_pixels) : '-'}</div>
           <div class='label'>coverage_ratio</div><div class='value' data-stat='coverage_ratio'>${hasData ? fmt(stats.coverage_ratio) : '-'}</div>
         </div>
       </div>

       <div>
         <div class='muted' style='margin-bottom:6px;'>Scatter</div>
         <div id='scatterPlot' class='plot-holder'>
           ${hasData ? '' : '<div class="spinner" aria-label="loading"></div>'}
         </div>
       </div>

     </div>
   `

  overlay.classList.remove('hidden')

  if (hasData && scatterObj.hist2d && scatterObj.x_edges && scatterObj.y_edges) {
      const svg = buildScatterSVG(
        scatterObj.x_edges,
        scatterObj.y_edges,
        scatterObj.hist2d,
        { width: 420, height: 320, pad: 40 }
      )
      const plotEl = document.getElementById('scatterPlot')
      plotEl.innerHTML = ''
      plotEl.appendChild(svg)
    }
}

/**
 * Build a simple 2D scatter/heatmap SVG from histogram2d data.
 * @param {number[]} xEdges
 * @param {number[]} yEdges
 * @param {number[][]} hist2d
 * @param {{width?:number,height?:number,pad?:number}} opts
 * @returns {SVGSVGElement}
 */
function buildScatterSVG(xEdges, yEdges, hist2d, opts = {}) {
  const w = opts.width ?? 400
  const h = opts.height ?? 300
  const pad = opts.pad ?? 40
  const innerW = w - pad * 2
  const innerH = h - pad * 2

  const xMin = Math.min(...xEdges)
  const xMax = Math.max(...xEdges)
  const yMin = Math.min(...yEdges)
  const yMax = Math.max(...yEdges)
  const nx = hist2d.length
  const ny = hist2d[0].length
  const maxCount = Math.max(1, ...hist2d.flat())

  const svgNS = 'http://www.w3.org/2000/svg'
  const svg = document.createElementNS(svgNS, 'svg')
  svg.setAttribute('width', String(w))
  svg.setAttribute('height', String(h))
  svg.style.background = '#11151c'

  const scaleX = (v) => pad + ((v - xMin) / (xMax - xMin)) * innerW
  const scaleY = (v) => h - pad - ((v - yMin) / (yMax - yMin)) * innerH

  for (let i = 0; i < nx; i++) {
    for (let j = 0; j < ny; j++) {
      const val = hist2d[i][j]
      if (val <= 0) continue
      const x0 = scaleX(xEdges[i])
      const x1 = scaleX(xEdges[i + 1])
      const y0 = scaleY(yEdges[j])
      const y1 = scaleY(yEdges[j + 1])
      const rect = document.createElementNS(svgNS, 'rect')
      rect.setAttribute('x', String(x0))
      rect.setAttribute('y', String(y1))
      rect.setAttribute('width', String(x1 - x0))
      rect.setAttribute('height', String(y0 - y1))
      const intensity = val / maxCount
      const color = `rgba(30,144,255,${Math.min(1, 0.2 + 0.8 * intensity)})`
      rect.setAttribute('fill', color)
      svg.appendChild(rect)
    }
  }

  // axes
  const axisColor = '#666'
  const mkLine = (x1, y1, x2, y2) => {
    const l = document.createElementNS(svgNS, 'line')
    l.setAttribute('x1', x1)
    l.setAttribute('y1', y1)
    l.setAttribute('x2', x2)
    l.setAttribute('y2', y2)
    l.setAttribute('stroke', axisColor)
    l.setAttribute('stroke-width', '1')
    return l
  }
  svg.appendChild(mkLine(pad, h - pad, w - pad, h - pad))
  svg.appendChild(mkLine(pad, pad, pad, h - pad))

  const mkText = (txt, x, y, anchor = 'middle') => {
    const t = document.createElementNS(svgNS, 'text')
    t.textContent = txt
    t.setAttribute('x', x)
    t.setAttribute('y', y)
    t.setAttribute('fill', '#aaa')
    t.setAttribute('font-size', '10')
    t.setAttribute('text-anchor', anchor)
    return t
  }
  svg.appendChild(mkText(xMin.toFixed(2), pad, h - pad + 12, 'start'))
  svg.appendChild(mkText(xMax.toFixed(2), w - pad, h - pad + 12, 'end'))
  svg.appendChild(mkText(yMin.toFixed(2), pad - 6, h - pad, 'end'))
  svg.appendChild(mkText(yMax.toFixed(2), pad - 6, pad + 4, 'end'))

  return svg
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

  // Red vs Green axis
  /*document.getElementById('layerACminInput').value = '#000000'
  document.getElementById('layerACmedInput').value = '#7f0000'
  document.getElementById('layerACmaxInput').value = '#ff0000'
  document.getElementById('layerBCminInput').value = '#000000'
  document.getElementById('layerBCmedInput').value = '#007f00'
  document.getElementById('layerBCmaxInput').value = '#00ff00'*/
  initMap()
  wireSquareSamplerControls()
  wireAreaSamplerClick()
  ;['A', 'B'].forEach(layerId => wireDynamicStyleControls(layerId))
  enableAltWheelSlider()
  disableLeafletScrollOnAlt()


  const cfg = await loadConfig()
  state.geoserverBaseUrl = cfg.geoserver_base_url
  state.layers = cfg.layers
  state.baseStatsUrl = cfg.rstats_base_url

  populateLayerSelects()

  if (state.layers.length > 0) {
    const selA = document.getElementById('layerSelectA')
    selA.value = '0'
    selA.dispatchEvent(new Event('change', { bubbles: true }))
  }
  if (state.layers.length > 1) {
    const selB = document.getElementById('layerSelectB')
    selB.value = '1'
    selB.dispatchEvent(new Event('change', { bubbles: true }))
  }
})()
