# PV Battery Optimizer (Home Assistant custom integration)

Plans when to charge the home battery from the grid, and how much, using
dynamic prices (from evcc/Tibber), a corrected PV forecast and a load forecast
built from recorder history. It carries out the plan through evcc's external
battery mode.

## How it works

- **Planning (every 5 min, and immediately when an EV starts or stops charging)**
  - Prices: `GET /api/tariff/grid` and `/api/tariff/feedin` from evcc (15-min slots, up to 48 h).
  - PV: hourly forecast from Forecast.Solar (or Solcast / Open-Meteo Solar Forecast), corrected by learned per-hour factors.
  - Load: exponentially weighted average of the same hour on the same day type (weekday/weekend) from long-term statistics. EV energy is subtracted.
  - EV: running sessions (from live charge power) and evcc charging plans.
  - Optimizer: dynamic program (SoC resolution configurable, default 0.05 kWh; charge slots may be used partially) over battery SoC choosing `normal` / `hold` / `charge` for each slot to minimise grid cost, including efficiency losses and battery wear. Energy left at the end of the horizon is valued only at the cheapest price, so it never charges "just in case".
- **Control (every 20 s, only when the *Active control* switch is on)**
  - `POST /api/batterymode/{mode}`. evcc resets this after 60 s, so if HA stops, evcc takes control back automatically.
  - In a `charge` slot it switches to `hold` as soon as the planned SoC is reached, so the battery is never charged past the plan.
  - Turning the switch off calls `DELETE /api/batterymode`.
  - The external mode overrides evcc's "prevent discharge during fast charging". Option `ev_discharge_policy` decides whether the optimizer may use the battery for the EV (`optimize`) or never does (`block`).
- **PV forecast learning:** each evening the day-ahead forecast is stored. The next day it's compared against the actual PV production statistics. Correction factors start at 1.0 and improve over the following weeks.

## Installation

1. Copy `custom_components/pv_battery_optimizer` into the `custom_components` folder of your HA config and restart HA.
2. Add the integration (Settings → Devices & services → Add integration → *PV Battery Optimizer*).
3. Adjust the options (see below). Pay particular attention to **Grid charge power**.
4. Leave **Active control** off at first (dry run, see below).

## Configuration

### Step 1: evcc

- **evcc URL**: e.g. `http://192.168.1.10:7070`.
- **evcc admin password**: needed if evcc protects its API, which is required for setting the battery mode.

evcc must already be configured to control the battery (for sonnen: JSON Write API enabled and the token set in the evcc battery config). evcc must also provide a grid tariff (e.g. Tibber).

### Step 2: Forecast and history sources

All energy sensors must be cumulative kWh sensors with long-term statistics (`state_class` `total` or `total_increasing`, same requirement as the Energy dashboard).

- **PV forecast**: a Forecast.Solar, Solcast or Open-Meteo Solar Forecast entry.
- **PV production energy sensor**: actual PV production. Compared with the day-ahead forecast to learn the forecast's systematic error.
- **House consumption energy sensor**: total household consumption. The load forecast is built from its history.
- **EV charging energy sensor** (optional): total energy the wallbox has charged into the car. This can come from the wallbox's own integration, from evcc in HA (total charged energy per charging point), or be the sensor you added to the Energy dashboard as an individual device.

#### Why the EV charging sensor is needed

The load forecast is built from your house consumption history. A typical house consumption sensor includes EV charging. Without subtracting it, the forecast would treat past 11 kW charging sessions as normal household load. It would then expect a large load on the evenings you usually charge, and keep battery energy back for a car that might not be plugged in.

Subtracting the wallbox energy leaves the pure household load. EV charging is then added separately from what evcc actually reports: a session that is running right now (assumed to continue for the next 30 min, and re-planned when charging starts or stops), or an evcc charging plan.

**When to leave it empty:** if your house consumption sensor already excludes the EV. That is the case if it's based on evcc's home power, which leaves the charging points out. If you're unsure, check whether the house consumption history shows the typical charging spikes. If it does, configure the EV sensor.

The EV sensor's history should cover at least the load history window (option *Days of history for load forecast*, default 42 days).

### Options (battery and optimization parameters)

| Option | Default | Notes |
|---|---|---|
| Usable battery capacity | from evcc, else 20 kWh | |
| Minimum / maximum SoC | 10 % / 100 % | Range the optimizer may use. |
| **Grid charge power** | 3.3 kW | **Must match `maxchargepower` of the battery in evcc** (in kW, e.g. 4600 W → 4.6). evcc doesn't report this value through its API, so it has to be entered here. When the optimizer says "charge now", evcc decides how fast; the optimizer uses this value to work out how many cheap slots to book. Too low: more charge slots are planned than needed (harmless, charging stops at the target). Too high: too few slots are booked and the battery is short when expensive hours begin. |
| Max. charge power (PV) | 3.3 kW | The battery's rated charge power. Used for how fast surplus PV is stored. |
| Max. discharge power | 3.3 kW | The battery's rated discharge power. |
| Charge / discharge efficiency | 0.95 / 0.95 | One-way efficiencies. Round trip ≈ their product. |
| Battery wear cost | 0.03 /kWh | Added per kWh discharged. Grid charging only happens if the price spread covers losses + wear. |
| Feed-in price (fallback) | 0.08 /kWh | Used if evcc has no feed-in tariff configured. |
| EV charge power | 11 kW | Assumed power for evcc charging plans. |
| Battery discharge while EV charges | Let the optimizer decide | The optimizer's battery mode overrides evcc's "prevent discharge during fast charging". *Let the optimizer decide*: the battery may feed the EV when that is cheaper overall. *Never discharge into the EV*: battery is held whenever an EV is charging. |
| PV forecast safety factor | 1.0 | Applied on top of the learned correction. Below 1 = plan more conservatively (more grid charging). |
| Days of history for load forecast | 42 | |
| Planning resolution | 0.05 kWh | SoC step of the optimizer. Smaller = more precise charge amounts, more CPU (0.05: ~1.5 s per 48 h plan on a fast machine, several seconds on a Raspberry Pi; 0.02: ~5× that). |

## Dry run and going live

With **Active control** off, the plan is calculated and published but evcc keeps full control of the battery. Watch `sensor.pv_battery_optimizer_planned_battery_mode`:

- `schedule`: the planned modes as blocks (start, end, mode, expected SoC at the end).
- `slots`: every 15-min slot with price, PV, load, EV, expected SoC and grid energy.

Other sensors: charge target, planned grid charge (kWh), estimated savings compared with plain self-consumption, PV and load forecast for the next 24 h (with the learned PV correction factors as an attribute).

Once the plans look reasonable, turn on **Active control**. Turning it off hands control back to evcc immediately.

For problems, filter the HA log for `pv_battery_optimizer`.

## Notes and limitations

- **PV correction starts from zero.** HA doesn't store past Forecast.Solar forecasts, so the correction can only learn from installation onwards and needs a few weeks to become meaningful.
- **Forecast.Solar timestamps:** the raw API labels each value with the end of its period. HA's Forecast.Solar integration converts this to period start, which is what this integration expects (partial sunrise/sunset periods are handled).
- **Planning horizon:** prices for the next day are published in the early afternoon. Before that, the plan ends at midnight. The plan is recalculated every 5 min and picks up new prices automatically.
- **No grid power limit:** EV and battery may charge from the grid at the same time.
- **Possible extension:** a PV model trained on Open-Meteo's archived weather forecasts against your actual production. It would use the full production history immediately and could learn weather-dependent errors and system quirks (shading, snow, inverter clipping). This requires sending the location (latitude/longitude) to Open-Meteo.

## Tests

```
uv venv -p python3.14 .venv        # Python >= 3.14.2 (HA 2026.11 requires it)
uv pip install -p .venv -r requirements_test.txt
.venv/bin/pytest
```

- `tests/test_optimizer.py`, `tests/test_forecast.py`: optimizer and forecasting (pure Python, also run with `python3 -m unittest discover -s tests -p 'test_[of]*.py'`).
- `tests/test_integration.py`: config/options flow, setup, sensors and the control loop against a real HA core with mocked evcc API, PV forecast and recorder statistics.

### HA version used for testing

Target: **HA 2026.11.1 or newer**. `pytest-homeassistant-custom-component` pins an exact HA version, and its newest release (0.13.375) is built for HA 2026.10.4. It does not run on 2026.11: it imports `respx`/`httpx` before HA aliases `httpx` to `httpx2`, and it uses internals removed in 2026.11 (`homeassistant.components.logger._clear_logger_overwrites`). Until a 2026.11-compatible release is published, tests run against **HA 2026.10.4**. Once it is out, upgrade with:

```
uv pip install -p .venv -U -r requirements_test.txt
```

and check that `.venv/bin/python -c "import homeassistant.const as c; print(c.__version__)"` reports 2026.11.x.
