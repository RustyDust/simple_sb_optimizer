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

### Viewing the plan

Paste this into **Developer Tools → Template** to print the whole plan as a readable table. It finds the plan sensor by itself, so it works regardless of the sensor's entity ID:

```jinja
{% set ns = namespace(entity=none) %}
{% for e in integration_entities('pv_battery_optimizer') if state_attr(e, 'slots') is not none %}{% set ns.entity = e %}{% endfor %}
{% if ns.entity is none -%}
No plan found. Entities of the integration:
{% for e in integration_entities('pv_battery_optimizer') %}  {{ e }}: {{ states(e) }}
{% endfor %}
{%- else -%}
Plan from {{ ns.entity }}:
{% for s in state_attr(ns.entity, 'slots') -%}
{{ as_local(as_datetime(s.start)).strftime('%a %H:%M') }}  {{ s.mode.ljust(6) }}  price {{ '%.3f' % s.price }}  pv {{ '%.2f' % s.pv }}  load {{ '%.2f' % s.load }}  ev {{ '%.2f' % s.ev }}  soc {{ s.soc }}%
{% endfor %}
{%- endif %}
```

PV, load and EV are kWh per 15-minute slot. `soc` is the battery level expected at the end of the slot.

If it prints "No plan found", the list below it shows the integration's entities and their states. A plan sensor that is `unavailable` means planning failed; check the log for `pv_battery_optimizer`.

The first line of the output shows the plan sensor's entity ID. If it differs from `sensor.pv_battery_optimizer_planned_battery_mode`, use that ID in the dashboard chart as well.

### Dashboard chart

[`dashboard/battery-plan-card.yaml`](dashboard/battery-plan-card.yaml) is a chart of the current plan for the next 48 h:

- **PV forecast:** yellow area.
- **House load:** blue line, without the EV.
- **EV charging:** purple columns.
- **Grid charging:** red columns.
- **Planned battery level:** green line, right axis.
- **Price:** grey step line.

It needs the custom card [ApexCharts Card](https://github.com/RomRider/apexcharts-card), available through HACS. Add it via *Edit dashboard → Add card → Manual* and paste the file's content.

### Plan vs. reality (diagnostics)

The integration keeps two records for later comparison:

- **Evening plan:** the first plan after 21:00 that covers the whole next day, which includes tonight's charging decisions. One per day, kept for 30 days.
- **Battery level:** the measured battery level at the start of every 15-minute slot, kept for 31 days.

To compare, go to *Settings → Devices & services → PV Battery Optimizer → ⋮ → Download diagnostics*. The JSON file contains:

- `plan_vs_reality`: for each saved evening plan, keyed by the day it was made for:
  - `hours`: for each hour, the planned and actual values of `pv`, `house` (load without EV), `ev`, and the battery level at the end of the hour (`soc_end_plan` / `soc_end_actual`). Actual energies come from the recorder statistics of the configured sensors. The battery level comes from the integration's own log.
  - `summary`: totals over all complete hours that are over, plus the mean and largest deviation of the battery level. A positive deviation means the battery was fuller than planned.
  - Hours the plan covers only partly (e.g. the first hour of a plan made at 21:07) are listed with `partial: true` and left out of the summary.
- `current_plan`: the current plan, slot by slot.
- `pv_correction`: the learned PV correction factors and how many samples they're based on.
- Configuration and options (password removed), live evcc values, and the desired and applied battery mode.

What to look for:

- **Battery level consistently below plan before or during expensive hours:** it charged too little. Check whether load was higher or PV lower than forecast.
- **Battery level above plan and full around midday:** it charged too much, typically because PV was underestimated.

### Dry run checklist

**1. Is it running?**

- **Sensors:** none of the sensors is `unavailable`.
- **`planned_at`:** this attribute of "Planned battery mode" updates about every 5 minutes.
- **Log:** no errors or warnings from `pv_battery_optimizer`.

**2. Are the inputs right?**

- **Battery level:** the first slot's starting level matches what evcc or the sonnen app shows. If it's off, the capacity setting is probably wrong.
- **Prices:** they match the Tibber/evcc prices for the same 15-minute slots. Before about 13:00–14:00 the plan ends at midnight; after that it runs through the next day.
- **PV:**
  - The PV values peak around midday.
  - "PV forecast next 24h" roughly matches Forecast.Solar's own figures. Until the correction has learned something, it matches exactly (times the PV safety factor).
- **Load:**
  - "Load forecast next 24h" is in the range of your usual daily consumption without the EV.
  - Individual slots look like normal household use (around 0.1–0.3 kWh per slot), with no car-sized spikes.
  - If you see spikes, the EV energy sensor isn't subtracting properly (see *Why the EV charging sensor is needed*).
- **EV:** the `ev` column is only non-zero while the car is charging or has an evcc charging plan. When charging starts, the plan is recalculated within about 20 s.

**3. Do the plans make sense?**

- **Charging only when worth it.** The expensive hours must cost more than the cheap price ÷ ~0.9 (round-trip efficiency), plus the wear cost. Otherwise it doesn't charge at all, and on many days that's correct.
- **Charge target:**
  - It covers the expensive hours in the plan, not automatically up to 100 %.
  - Before a sunny day it charges little or nothing.
  - Before a dark, expensive day it charges more.
- **Hold before expensive periods:** it may hold the battery during cheap hours, saving it for the expensive ones. If the cheap window is longer than needed for charging, it holds first and charges in the last part of the window.
- **Stability:** the charge target and plan don't jump around much between recalculations when nothing has changed. The history graphs of "Charge target" and "Planned battery mode" show this.
- **Savings:** "Estimated savings" is positive but modest, from a few cents to around a euro per day. Much more than that usually points to a wrong input.

**4. Next day: plan versus reality**

This is the most important check. Look at:

- **Battery:** did it run empty before the expensive period ended (charged too little)? Or was it full at midday and exporting PV after grid charging (charged too much)?
- **Load:** actual consumption compared with the load forecast.
- **PV:** actual production compared with the forecast.

Use the diagnostics download for this (see *Plan vs. reality*). The `slots` and `schedule` attributes are excluded from the recorder because they're large, so past plans are only available through the saved evening plans in the diagnostics.

### Turning on Active control

Once the plans look reasonable, turn on **Active control**. Do it at a time when you can watch:

- **evcc:** the battery shows the externally set mode.
- **`charge`:** the battery charges at `maxchargepower` and switches to `hold` when the target is reached.
- **`hold`:**
  - The battery doesn't discharge.
  - Also note whether it still takes up surplus PV. The optimizer assumes it doesn't.
- **Turning the switch off:** evcc takes over again immediately.

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

- `tests/test_optimizer.py`, `tests/test_forecast.py`, `tests/test_evaluation.py`: optimizer, forecasting and plan evaluation (pure Python, also run with `python3 -m unittest discover -s tests -p 'test_[ofe]*.py'`).
- `tests/test_integration.py`: config/options flow, setup, sensors, control loop, evening plan snapshot and diagnostics against a real HA core with mocked evcc API, PV forecast and recorder statistics.

### HA version used for testing

Target: **HA 2026.11.1 or newer**. `pytest-homeassistant-custom-component` pins an exact HA version, and its newest release (0.13.375) is built for HA 2026.10.4. It does not run on 2026.11: it imports `respx`/`httpx` before HA aliases `httpx` to `httpx2`, and it uses internals removed in 2026.11 (`homeassistant.components.logger._clear_logger_overwrites`). Until a 2026.11-compatible release is published, tests run against **HA 2026.10.4**. Once it is out, upgrade with:

```
uv pip install -p .venv -U -r requirements_test.txt
```

and check that `.venv/bin/python -c "import homeassistant.const as c; print(c.__version__)"` reports 2026.11.x.
