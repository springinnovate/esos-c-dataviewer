// static/app.js
/* global L */
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
}

async function loadConfig() {
  const res = await fetch('api/config')
  if (!res.ok) throw new Error('Failed to load config')
  return res.json()
}

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

function latLngBoundsForSquareKilometers(centerLatLng, windowSizeKm) {
  const crs = state.map.options.crs || L.CRS.EPSG3857
  const halfSizeM = (Number(windowSizeKm) || 0) * 1000 / 2
  const p = crs.project(centerLatLng)
  const sw = crs.unproject(L.point(p.x - halfSizeM, p.y - halfSizeM))
  const ne = crs.unproject(L.point(p.x + halfSizeM, p.y + halfSizeM))
  return L.latLngBounds(sw, ne)
}

function _latlngsFromPoly(polyGeoJSON) {
  return polyGeoJSON.geometry.coordinates[0].map(([lng, lat]) => [lat, lng])
}

function _ensureOutlineLayer() {
  if (state.outlineLayer) return state.outlineLayer
  state.outlineLayer = L.polygon([], {
    color: '#1e90ff',
    weight: 2,
    fill: false,
    interactive: false,
  })
  // do not add yet; added on first update
  return state.outlineLayer
}

function _updateOutline(polyGeoJSON) {
  const latlngs = _latlngsFromPoly(polyGeoJSON)
  const layer = _ensureOutlineLayer()
  layer.setLatLngs(latlngs)
  if (!state.map.hasLayer(layer)) layer.addTo(state.map)
}

function _hideOutline() {
  if (state.outlineLayer && state.map.hasLayer(state.outlineLayer)) {
    state.map.removeLayer(state.outlineLayer)
  }
}

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
}

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

function wireOpacity() {
  const r = document.getElementById('opacityRange')
  r.addEventListener('input', () => {
    if (state.wmsLayer) state.wmsLayer.setOpacity(parseFloat(r.value))
  })
}

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
  })
}

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

function () {
  const btn = document.getElementById('overlayClose')
  btn.addEventListener('click', (e) => {
    e.preventDefault()
    e.stopPropagation()
    document.getElementById('statsOverlay').classList.add('hidden')
    document.getElementById('overlayBody').innerHTML = ''
    _hideOutline()
  })

  // belt & suspenders: swallow clicks anywhere in the overlay
  const overlay = document.getElementById('statsOverlay')
  ;['mousedown','mouseup','click','dblclick','contextmenu','touchstart','pointerdown','pointerup']
    .forEach(evt => overlay.addEventListener(evt, ev => ev.stopPropagation()))
}

function showOverlayError(msg) {
  const overlay = document.getElementById('statsOverlay')
  const body = document.getElementById('overlayBody')
  overlay.classList.remove('hidden')
  body.innerHTML = `<pre>${msg}</pre>`
}

function renderAreaStatsOverlay({ rasterId, centerLng, centerLat, boxKm, statsObj, units }) {
  const overlay = document.getElementById('statsOverlay')
  const body = document.getElementById('overlayBody')
  overlay.classList.remove('hidden')

  const s = statsObj || {}
  const lines = [
    `Layer: ${rasterId}`,
    `Center: ${centerLng.toFixed(6)}, ${centerLat.toFixed(6)}`,
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

function buildHistogramSVG(hist, binEdges, opts = {}) {
  const width = opts.width ?? 360
  const height = opts.height ?? 120
  const pad = opts.pad ?? 28
  const w = width, h = height
  const innerW = w - pad * 2
  const innerH = h - pad * 2

  const maxCount = Math.max(1, ...hist)
  const bins = hist.length
  const barW = innerW / bins

  const svgNS = 'http://www.w3.org/2000/svg'
  const svg = document.createElementNS(svgNS, 'svg')
  svg.setAttribute('width', String(w))
  svg.setAttribute('height', String(h))
  svg.style.background = '#11151c'

  const axisColor = '#666'
  const xAxis = document.createElementNS(svgNS, 'line')
  xAxis.setAttribute('x1', String(pad))
  xAxis.setAttribute('y1', String(h - pad))
  xAxis.setAttribute('x2', String(w - pad))
  xAxis.setAttribute('y2', String(h - pad))
  xAxis.setAttribute('stroke', axisColor)
  xAxis.setAttribute('stroke-width', '1')
  svg.appendChild(xAxis)

  const yAxis = document.createElementNS(svgNS, 'line')
  yAxis.setAttribute('x1', String(pad))
  yAxis.setAttribute('y1', String(pad))
  yAxis.setAttribute('x2', String(pad))
  yAxis.setAttribute('y2', String(h - pad))
  yAxis.setAttribute('stroke', axisColor)
  yAxis.setAttribute('stroke-width', '1')
  svg.appendChild(yAxis)

  for (let i = 0; i < bins; i++) {
    const v = hist[i]
    const barH = (v / maxCount) * innerH
    const x = pad + i * barW + 1
    const y = h - pad - barH
    const rect = document.createElementNS(svgNS, 'rect')
    rect.setAttribute('x', String(x))
    rect.setAttribute('y', String(y))
    rect.setAttribute('width', String(Math.max(0, barW - 2)))
    rect.setAttribute('height', String(Math.max(0, barH)))
    rect.setAttribute('fill', '#1e90ff')
    rect.setAttribute('opacity', '0.85')
    svg.appendChild(rect)
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
  svg.appendChild(mkText(String(maxCount), pad - 4, pad + 3))

  return svg
}

/* -------- bootstrap -------- */
;(async function main() {
  initMap()
  wireOpacity()
  wireRadiusControls()
  wireAreaSamplerClick()

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
