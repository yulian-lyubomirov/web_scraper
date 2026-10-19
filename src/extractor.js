// extractor.js
async function extractPropertyData(id) {
  // 1. Scroll to the map to trigger lazy-loading and map initialization.
  const mapElement = document.querySelector('#mapContainer, .map-container, .map-link, [id*="map"]');
  if (mapElement) {
    mapElement.scrollIntoView({ behavior: 'auto', block: 'center' });
  } else {
    window.scrollBy(0, 800);
  }
  await new Promise(resolve => setTimeout(resolve, 500));

  // The map is initially collapsed on some listings; activate its visible control.
  const mapTrigger = Array.from(document.querySelectorAll('a, button, [role="button"]'))
    .find(element => /^(ver mapa|ampliar mapa)$/i.test(element.innerText?.trim() || ''));
  if (mapTrigger) {
    mapTrigger.click();
    await new Promise(resolve => setTimeout(resolve, 1500));
  }

  // 2. Extract Title
  const title = document.querySelector('h1.main-info__title-main')?.innerText?.trim() || '';
  const locationText = document.querySelector('span.main-info__title-minor')?.innerText?.trim() || '';
  const locationParts = locationText.split(',').map(part => part.trim()).filter(Boolean);
  const neighborhood = locationParts.length > 1 ? locationParts[0] : null;
  const city = locationParts.length > 1 ? locationParts.slice(1).join(', ') : (locationParts[0] || null);
  
  // 3. Multi-selector fallback strategy for price
  const priceElement = 
    document.querySelector('.info-data-price .txt-bold') ||
    document.querySelector('.info-data-price') ||
    document.querySelector('.info-data_price') ||
    document.querySelector('span.info-data-price');

  let price = priceElement?.innerText?.trim() || '';

  if (!price) {
    const mainHeader = document.querySelector('.main-info');
    if (mainHeader) {
      const matchPrice = mainHeader.innerText.match(/([\d.,]+)\s*€/);
      if (matchPrice) price = matchPrice[0];
    }
  }

  // 4. Extract Features (rooms, bathrooms, size, floor, etc.)
  const features = Array.from(document.querySelectorAll('.info-features span'))
    .map(el => el.innerText.trim())
    .filter(Boolean);

  // 5. Extract Advertiser Name
  const advertiser = document.querySelector('.professional-name')?.innerText?.trim() || '';

  // 6. Location / Privacy check text
  const privacyNoticeElement = Array.from(document.querySelectorAll('div, span, p'))
    .filter(el => /por privacidad.{0,100}(ubicaci[oó]n|localizaci[oó]n) exacta|ubicaci[oó]n aproximada/i.test(el.innerText || ''))
    .sort((a, b) => (a.innerText || '').trim().length - (b.innerText || '').trim().length)[0];
  const locationNotice = privacyNoticeElement?.innerText?.trim() || null;
  const isApproximateLocation = Boolean(locationNotice);

  // 7. Coordinate Harvesting
  let latitude = null;
  let longitude = null;
  let coordinateSource = null;

  const addCoordinates = (latValue, lonValue, source) => {
    if (latValue === '' || lonValue == null || lonValue === '') return;
    const lat = Number(String(latValue).replace(',', '.'));
    const lon = Number(String(lonValue).replace(',', '.'));
    if (Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180 && lat !== 0 && lon !== 0) {
      latitude = lat;
      longitude = lon;
      coordinateSource = source;
    }
  };

  // Inspect mapConfig and other map/geo globals. Coordinates may be nested under
  // marker, center, location, or map objects rather than directly on mapConfig.
  const scanCoordinates = (value, source, seen = new WeakSet(), depth = 0) => {
    if (!value || typeof value !== 'object' || depth > 10 || seen.has(value)) return;
    seen.add(value);
    const entries = Object.entries(value);
    const get = (...names) => entries.find(([key]) => names.includes(key.toLowerCase()))?.[1];
    const lat = get('latitude', 'lat');
    const lon = get('longitude', 'lon', 'lng', 'long');
    if (lat != null && lon != null) addCoordinates(lat, lon, source);
    for (const [key, child] of entries) scanCoordinates(child, `${source}.${key}`, seen, depth + 1);
  };

  const mapGlobalNames = Object.keys(window).filter(name => /map|geo|coord/i.test(name));
  const mapGlobals = {};
  for (const name of mapGlobalNames) {
    try {
      const value = window[name];
      if (value && typeof value === 'object') {
        mapGlobals[name] = value;
        scanCoordinates(value, `window.${name}`);
      }
    } catch {}
  }

  // Map services encode approximate marker positions in several URL formats.
  const mapUrls = Array.from(document.querySelectorAll('iframe[src], a[href]'))
    .map(el => el.src || el.href)
    .filter(url => url && /map|google|geo|location|lat/i.test(url));
  const mapUrlCoordinateMatches = [];
  if (!latitude || !longitude) {
    for (const url of mapUrls) {
      let decodedUrl = url;
      try { decodedUrl = decodeURIComponent(url); } catch {}

      const match = decodedUrl.match(/@(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)/i)
        // Google Maps embed URLs commonly encode marker positions as !3dLAT!4dLON.
        || decodedUrl.match(/!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)/i)
        || decodedUrl.match(/[?&](?:center|ll|sll|q|query)=(?:loc:)?(-?\d+(?:\.\d+)?)[,;\s]+(-?\d+(?:\.\d+)?)/i)
        || decodedUrl.match(/[?&](?:latitude|lat)=(-?\d+(?:\.\d+)?)[^&]*&(?:longitude|lon|lng)=(-?\d+(?:\.\d+)?)/i);

      if (match) {
        mapUrlCoordinateMatches.push({ url, latitude: match[1], longitude: match[2] });
        addCoordinates(match[1], match[2], 'map-url-pattern');
        break;
      }
    }
  }

  // Final JSON-LD fallback scan
  if (!latitude || !longitude) {
    const jsonLd = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
      .map((script) => {
        try { return JSON.parse(script.textContent); } catch { return null; }
      })
      .filter(Boolean);

    jsonLd.forEach((block, index) => scanCoordinates(block, `json-ld[${index}]`));
  }

  return {
    id,
    url: window.location.href,
    title,
    locationText,
    neighborhood,
    city,
    price,
    features,
    advertiser,
    latitude,
    longitude,
    coordinateSource,
    isApproximateLocation,
    locationNotice,
    mapDiagnostics: {
      mapConfigPresent: Boolean(window.mapConfig),
      mapConfigCoordinateValues: window.mapConfig ? {
        latitude: window.mapConfig.latitude ?? null,
        longitude: window.mapConfig.longitude ?? null,
        markerVisible: window.mapConfig.markerVisible ?? null,
      } : null,
      mapGlobalNames,
      mapGlobalKeys: Object.fromEntries(Object.entries(mapGlobals).map(([name, value]) => [
        name,
        Object.keys(value).slice(0, 50),
      ])),
      mapUrls,
      mapUrlCoordinateMatches,
      mapElementFound: Boolean(mapElement),
      mapTriggerFound: Boolean(mapTrigger),
      mapTriggerText: mapTrigger?.innerText?.trim() || null,
      mapElementAttributes: mapElement
        ? Array.from(mapElement.attributes).reduce((result, attribute) => {
            result[attribute.name] = attribute.value;
            return result;
          }, {})
        : null,
    },
    scrapedAt: new Date().toISOString()
  };
}
