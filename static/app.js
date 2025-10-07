// static/app.js
/* global L */
const state = {
  map: null,
  baseUrl: '',
  baseStatsUrl: '',
  wmsLayer: null,
  layers: [],
  activeLayerIdx: 0,
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
    zoomControl: true,
  })
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
  }).addTo(map)
  state.map = map
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
}

function wireOpacity() {
  const r = document.getElementById('opacityRange')
  r.addEventListener('input', () => {
    if (state.wmsLayer) state.wmsLayer.setOpacity(parseFloat(r.value))
  })
}

function addWidget() {
  const pane = document.getElementById('widgetsPane')
  const tpl = document.getElementById('widgetTemplate')
  const node = tpl.content.firstElementChild.cloneNode(true)
  node.querySelector('.close-btn').addEventListener('click', () => node.remove())
  pane.appendChild(node)
}

function wireWidgets() {
  document.getElementById('addWidgetBtn').addEventListener('click', addWidget)
}

function wireMapClickForPixelStats() {
  state.map.on('click', async (evt) => {
    const lyr = state.layers[state.activeLayerIdx]
    if (!lyr) return
    const rasterId = lyr.raster_id || lyr.name
    const payload = {
      raster_id: rasterId,
      lon: evt.latlng.lng,
      lat: evt.latlng.lat,
      crs: 'EPSG:4326',
    }
    let res
    try {
      var url = `${state.baseStatsUrl}/stats/pixel`;
      console.log(url);
      res = await fetch(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
      });
      console.log(res);
    } catch (e) {
      renderResultCard({ title: 'Raster stats', body: 'Network error' })
      return
    }
    if (!res.ok) {
      const msg = await res.text()
      renderResultCard({ title: 'Raster stats', body: `Error: ${msg}` })
      return
    }
    const out = await res.json()
    const valStr = out.value === null ? '(nodata)' : out.value
    renderResultCard({
      title: 'Pixel value',
      body: `Layer: ${rasterId} Lon,Lat: ${payload.lon.toFixed(6)}, ${payload.lat.toFixed(6)} Value: ${valStr}${out.units ? ' ' + out.units : ''}`,
    })
  })

  async function fetchGeometryStats(rasterId, geojson) {
    const res = await fetch(`${state.baseStatsUrl}/stats/geometry`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        raster_id: rasterId,
        geometry: (geojson.geometry ? geojson.geometry : geojson),
        from_crs: 'EPSG:4326',
        reducer: 'mean',
      }),
    });
    if (!res.ok) throw new Error(await res.text())
    return res.json()
  }

  function renderResultCard({ title, body }) {
    const pane = document.getElementById('widgetsPane')
    const card = document.createElement('div')
    card.className = 'widget'
    card.innerHTML = `
      <div class='widget-header'>
        <span>${title}</span>
        <button class='close-btn' title='Close'>&times;</button>
      </div>
      <pre class='widget-body'></pre>
    `
    card.querySelector('.widget-body').textContent = body
    card.querySelector('.close-btn').addEventListener('click', () => card.remove())
    pane.prepend(card)
  }
}

;(async function main() {
  initMap()
  wireOpacity()
  wireWidgets()
  wireMapClickForPixelStats()

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
