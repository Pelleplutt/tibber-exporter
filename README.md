# tibber-exporter
Tibber exporter for Prometheus. Exposes the current prices as well as the real time information from your connected tibber-pulse. 

## Building and running
Running requires a token from Tibber. You can get this in their online portal here: https://developer.tibber.com/settings/accesstoken

Container needs this token passed in the environment under `TIBBER_TOKEN`. 

Typical build and run:
```
$ docker build -t tibber-exporter . 
$ docker run -e TIBBER_TOKEN=TOP_SECRET_TOKEN -it -p 9110:9110 -t tibber-exporter
```

The exporter should in theory handle multiple homes including multiple tibbe-pulse readers. I have not however had the luxury to test this. Pull requests welcome if this does not work. 

## Metrics
The following metrics are exposed:

| metric | type | description |
| ------ | ---- | ----------- |
| tibber_price_energy | gauge | Current energy price |
| tibber_price_tax | gauge | Current energy tax |
| tibber_price_total | gauge | Current total price |
| tibber_total_consumption_kwh_total | counter | Last meter active import register state |
| tibber_today_consumption_kwh_total | counter | Accumulated consumption since midnight |
| tibber_today_consumption_cost_total | counter | Accumulated cost since midnight |
| tibber_today_avg_power_watt | gauge | Average power since midnight |
| tibber_power_watt | gauge | Current power draw |
| tibber_power_factor | gauge | Current power factor |
| tibber_power_reactive_kvar | gauge | Current reactive consumption |
| tibber_current_a | gauge | Current power draw |
| tibber_potential_v | gauge | Current electric potential |
| tibber_pulse_signal_strength_db | gauge | Pulse Device signal strength |
