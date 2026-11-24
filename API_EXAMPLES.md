# NadiTwin API Examples

## Base URL

Replace `http://localhost:8080` with your deployed URL:
- Railway: `https://naditwin-production.up.railway.app`
- Render: `https://naditwin.onrender.com`
- Cloud Run: `https://naditwin-xxxxx.run.app`

## Quick API Reference

### 1. List All Rivers

```bash
curl http://localhost:8080/api/rivers
```

**Response:**
```json
["Mula-Mutha", "Mithi", "Mandovi"]
```

---

### 2. Get River Metadata

```bash
curl "http://localhost:8080/api/meta?river=Mithi"
```

**Response:**
```json
{
  "river": "Mithi",
  "length_km": 18.5,
  "num_stations": 185,
  "num_locations": 7,
  "locations": ["Kurla", "Kalina", "Vakola", "BKC", "Dharavi", "N–Mahim", "Mahim Creek mouth"],
  "time_range": {
    "start": "2021-01-01T00:00:00Z",
    "end": "2024-11-24T15:00:00Z",
    "forecast_hours": 384
  }
}
```

---

### 3. Get Live River State

```bash
curl "http://localhost:8080/api/state?river=Mithi"
```

**Response includes:**
- Current timestamp
- Upstream inflow (GloFAS)
- Total discharge
- Average stage
- Alert status

---

### 4. Get Live Discharge and Weather Data

```bash
curl "http://localhost:8080/api/live?river=Mithi"
```

**Response includes:**
- Hourly discharge for every km
- Storm runoff components
- Urban inflow
- Evaporation
- Weather data (rain, temp, humidity, wind)
- Forecast ensemble (P10, P25, median, P75, P90)

---

### 5. Get Discharge Profile (Every 10m)

```bash
curl "http://localhost:8080/api/discharge_profile?river=Mithi"
```

**Response:**
```json
{
  "stations": [
    {
      "chainage_m": 0,
      "width_m": 45.2,
      "discharge_m3s": 12.5,
      "velocity_ms": 0.85,
      "depth_m": 1.2,
      "stage_m": 3.5,
      "bank_crest_m": 7.2,
      "margin_m": 2.9,
      "zone": "SAFE"
    },
    // ... 185 stations
  ]
}
```

---

### 6. Get Hydrograph (Time Series)

```bash
# Last 72 hours
curl "http://localhost:8080/api/hydrograph?river=Mithi&cell=5&hours=72"

# Last 7 days
curl "http://localhost:8080/api/hydrograph?river=Mithi&cell=5&hours=168"

# Specific year
curl "http://localhost:8080/api/hydrograph?river=Mithi&cell=5&year=2024"

# All history
curl "http://localhost:8080/api/hydrograph?river=Mithi&cell=5&hours=all"
```

**Response:**
```json
{
  "times": ["2024-11-21T00:00:00Z", "2024-11-21T01:00:00Z", ...],
  "discharge_m3s": [10.2, 10.5, 11.1, ...],
  "stage_m": [3.2, 3.3, 3.4, ...],
  "rain_mm": [0, 0, 2.5, ...],
  "levels": {
    "watch": 5.5,
    "warning": 6.2,
    "danger": 6.9
  }
}
```

---

### 7. Get Rating Curve

```bash
curl "http://localhost:8080/api/rating?river=Mithi&cell=5"
```

**Response:**
```json
{
  "discharge_m3s": [0, 5, 10, 15, 20, 25, 30, ...],
  "stage_m": [2.1, 2.5, 2.9, 3.2, 3.5, 3.8, 4.1, ...],
  "levels": {
    "watch": { "discharge": 45, "stage": 5.5 },
    "warning": { "discharge": 60, "stage": 6.2 },
    "danger": { "discharge": 75, "stage": 6.9 }
  }
}
```

---

### 8. Get Forecast Profile

```bash
# Current
curl "http://localhost:8080/api/forecast/profile?river=Mithi&lead=0"

# 24 hours ahead
curl "http://localhost:8080/api/forecast/profile?river=Mithi&lead=24"

# 72 hours ahead
curl "http://localhost:8080/api/forecast/profile?river=Mithi&lead=72"
```

---

### 9. Get Stations in Range

```bash
# By location
curl "http://localhost:8080/api/stations?river=Mithi&loc=Kurla"

# By chainage range
curl "http://localhost:8080/api/stations?river=Mithi&from_km=5&to_km=10"

# Download as Excel
curl "http://localhost:8080/api/stations.xlsx?river=Mithi&loc=Kurla" -o kurla.xlsx

# Download all locations
curl "http://localhost:8080/api/stations.xlsx?river=Mithi&loc=all" -o mithi_all.xlsx
```

---

### 10. Get Margin Board (Flood Risk)

```bash
# By locations
curl "http://localhost:8080/api/margins_locations?river=Mithi"

# By chainage (every 1 km)
curl "http://localhost:8080/api/margins_chainage?river=Mithi&step_km=1"
```

**Response:**
```json
[
  {
    "location": "Kurla",
    "chainage_m": 2500,
    "current_stage_m": 3.5,
    "bank_crest_m": 7.2,
    "margin_m": 2.9,
    "zone": "SAFE",
    "forecast_24h": {
      "median_margin_m": 2.5,
      "zone": "SAFE"
    },
    "forecast_72h": {
      "median_margin_m": 1.8,
      "zone": "WATCH",
      "exceedance_prob": 0.15
    }
  },
  // ...
]
```

---

### 11. Get Alerts

```bash
curl "http://localhost:8080/api/alerts?river=Mithi"
```

**Response:**
```json
{
  "current_alerts": [
    {
      "location": "Dharavi",
      "zone": "WARNING",
      "margin_m": 0.5,
      "message": "Water level 0.5m from bank crest"
    }
  ],
  "forecast_alerts": [
    {
      "location": "Mahim Creek mouth",
      "lead_hours": 48,
      "zone": "WATCH",
      "probability": 0.35,
      "message": "35% chance of WATCH level in 48h"
    }
  ]
}
```

---

### 12. Upload Custom Localities

```bash
curl -X POST "http://localhost:8080/api/upload_localities?river=Mithi" \
  -H "Content-Type: application/json" \
  -d '{
    "filename": "new_locations.xlsx",
    "data_b64": "..."
  }'
```

---

### 13. Rebuild River from Folder

```bash
curl -X POST "http://localhost:8080/api/rebuild_river?river=Mithi"
```

---

### 14. Refresh Live Data

```bash
curl -X POST "http://localhost:8080/api/refresh_live?river=Mithi"
```

---

### 15. Upload Custom River KML

```bash
curl -X POST "http://localhost:8080/api/upload_kml?name=MyRiver" \
  -H "Content-Type: application/xml" \
  --data-binary @river_polygon.kml
```

Or upload multiple files as JSON:

```bash
curl -X POST "http://localhost:8080/api/upload_kml?name=MyRiver" \
  -H "Content-Type: application/json" \
  -d '{
    "polygon": "<kml>...</kml>",
    "chainage": "<kml>...</kml>",
    "depth": "<kml>...</kml>"
  }'
```

---

### 16. Historical Data Query

Add `?at=TIMESTAMP` to any endpoint:

```bash
# July 2021 floods
curl "http://localhost:8080/api/state?river=Mithi&at=2021-07-22T12:00:00Z"

# Specific date discharge profile
curl "http://localhost:8080/api/discharge_profile?river=Mithi&at=2024-08-15T06:00:00Z"
```

---

## JavaScript Examples

### Fetch River List

```javascript
fetch('http://localhost:8080/api/rivers')
  .then(res => res.json())
  .then(rivers => console.log(rivers));
```

### Get Live Data with Axios

```javascript
import axios from 'axios';

const getLiveData = async (river) => {
  const response = await axios.get(`http://localhost:8080/api/live`, {
    params: { river }
  });
  return response.data;
};

getLiveData('Mithi').then(data => console.log(data));
```

### WebSocket-like Polling

```javascript
const pollLiveData = (river, interval = 60000) => {
  const fetchData = async () => {
    const response = await fetch(`http://localhost:8080/api/state?river=${river}`);
    const data = await response.json();
    console.log('Updated:', data);
  };
  
  fetchData(); // Initial fetch
  return setInterval(fetchData, interval); // Poll every minute
};

const pollId = pollLiveData('Mithi');
// Stop polling: clearInterval(pollId);
```

---

## Python Examples

### Basic Request

```python
import requests

response = requests.get('http://localhost:8080/api/rivers')
rivers = response.json()
print(rivers)
```

### Get Hydrograph Data

```python
import requests
import pandas as pd

response = requests.get('http://localhost:8080/api/hydrograph', params={
    'river': 'Mithi',
    'cell': 5,
    'hours': 168  # Last 7 days
})

data = response.json()
df = pd.DataFrame({
    'time': pd.to_datetime(data['times']),
    'discharge': data['discharge_m3s'],
    'stage': data['stage_m'],
    'rain': data['rain_mm']
})

print(df.head())
```

### Download Excel Report

```python
import requests

response = requests.get('http://localhost:8080/api/stations.xlsx', params={
    'river': 'Mithi',
    'loc': 'all'
})

with open('mithi_report.xlsx', 'wb') as f:
    f.write(response.content)
```

---

## Rate Limits

No rate limits currently enforced. However:
- Historical data downloads can take 12+ minutes on first run
- Live data refreshes every 25 minutes
- Be respectful with polling frequency

---

## Error Responses

**404 - River Not Found**
```json
{
  "error": "River 'Unknown' not found",
  "available": ["Mula-Mutha", "Mithi", "Mandovi"]
}
```

**400 - Bad Request**
```json
{
  "error": "Invalid parameter",
  "message": "hours must be one of: 72, 168, 720, 2160, 8760, all"
}
```

---

## CORS

CORS is enabled for all origins in development. For production, configure allowed origins in your deployment.

---

## Need More?

- Full API list: `GET /api/endpoints?river=Mithi`
- Dashboard: `http://localhost:8080/`
- API documentation is embedded in each endpoint response
