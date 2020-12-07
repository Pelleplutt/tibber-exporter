#!/usr/bin/env python3

import argparse
import asyncio
import json
import logging
import os
import pprint
import requests
import socket
import sys
import threading
import time
import urllib3
import websockets
from datetime import datetime, timedelta
from python_graphql_client import GraphqlClient
from prometheus_client import start_http_server, Gauge
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY

PORT = 9110
SUBSCRIPTION_ENDPOINT = 'wss://api.tibber.com/v1-beta/gql/subscriptions'
QUERY_ENDPOINT = 'https://api.tibber.com/v1-beta/gql'
RT_HOMES = {}

# Number of seconds to wait for data after the initial connection
# attempt for real time updates before considering the connection
# dead
RT_DATA_CONNECT_TIMEOUT_SECONDS=60
# Number of seconds to allow inbetween real time consumption updates
# before considering the connection dead
RT_DATA_TIMEOUT_SECONDS=60
# Allow fallback to price cache for this long
PRICE_CACHE_TTL_SECONDS=90
# Time between refresh of price cache, used to reduce hit on tibber API
PRICE_CACHE_REFRESH_SECONDS=30

class TibberHomeRT(object):
    def __init__(self, token, id):
        self.id = id
        self.token = token
        self.last_live_measurement = None
        self.last_live_measurement_update = None
        self.subscription_client = GraphqlClient(endpoint=SUBSCRIPTION_ENDPOINT)
        self.subscription_task = None
        self.subscription_start = None

    def handle_live_measurement(self, data):
        logging.info('Got live measurement update for homeId {homeid}'.format(homeid=self.id))
        try:
            self.last_live_measurement = data['data']['liveMeasurement'].copy()
            self.last_live_measurement_update = datetime.now()
        except KeyError as e:
            logging.warning('Failed to parse live measurement update: {err}'.format(err=str(e)))

    def subscribe_live_measurements(self):
        logging.info('Starting subscription for homeId {homeid}'.format(homeid=self.id))
        query = """
        subscription {{
            liveMeasurement(homeId:"{homeid}") {{
                timestamp
                power
                powerFactor
                powerReactive
                averagePower
                lastMeterConsumption
                accumulatedConsumption
                accumulatedCost
                currency
                currentL1
                currentL2
                currentL3
                voltagePhase1
                voltagePhase2
                voltagePhase3
                signalStrength
            }}
        }}
        """.format(homeid=self.id)
        self.subscription_task = asyncio.create_task(self.subscription_client.subscribe(query=query,
            handle=self.handle_live_measurement,
            init_payload={'token': self.token}))
        self.subscription_start = datetime.now()

        return self.subscription_task

    def void_subscription(self):
        self.subscription_start = None
        self.subscription_task = None
        self.last_live_measurement_update = None

    def is_subscribed(self):
        return self.subscription_task is not None

    def is_subscription_starting(self):
        if self.is_subscribed() and self.last_live_measurement_update is None and\
            datetime.now() - self.subscription_start < timedelta(seconds=RT_DATA_CONNECT_TIMEOUT_SECONDS):
                return True

        return False

    def stop_subscription(self):
        if self.is_subscribed():
            if self.subscription_task.done():
                logging.info('Task {homeid} done.'.format(homeid=self.id))
                self.subscription_task = None
            else:
                logging.warning('Flagging for exit for task {homeid}'.format(homeid=self.id))
                self.subscription_task.cancel()

    def get_last_live_measurement(self):
        if self.last_live_measurement_update is None:
            return None

        return self.last_live_measurement.copy()

    def is_stale(self):
        if self.is_subscription_starting():
            logging.debug('Data is sort of stale, but OK, we are starting up')
            return False
        elif not self.is_subscribed():
            logging.debug('Data is stale, subscription is not starting and we are not subscribed')
            return True
        elif self.last_live_measurement_update is None:
            logging.debug('Data is stale, we are not starting, we are subscribed and no live measurement update')
            return True
        elif datetime.now() - self.last_live_measurement_update > timedelta(seconds=RT_DATA_TIMEOUT_SECONDS):
            logging.debug('Data is stale, we are not starting, we are subscribed and last live measurement was {last}'.format(last=str(self.last_live_measurement_update)))
            return True

        return False

class TibberHome(object):
    def __init__(self, token, data):
        self.id = data['id']
        self.token = token
        self.app_nickname = data.get('appNickname')
        self.features = data.get('features')
        self.realtime_consumption_enabled = False
        if self.features is not None:
            self.realtime_consumption_enabled = self.features.get('realTimeConsumptionEnabled')
        self.last_price = None
        self.last_price_update = None
        self.subscription_rt = None
        
        headers = { 'Authorization': 'Bearer ' + self.token }
        self.query_client = GraphqlClient(endpoint=QUERY_ENDPOINT, headers=headers, timeout=3.0)

    def get_name(self):
        if self.app_nickname is not None:
            return self.app_nickname
        return self.id

    def get_last_live_measurement(self):
        if not self.realtime_consumption_enabled or self.subscription_rt is None:
            return None

        if self.subscription_rt.is_stale():
            logging.warning('Stale data for homeId {homeid}'.format(homeid=self.id))
            self.subscription_rt.stop_subscription()

        return self.subscription_rt.get_last_live_measurement()

    def create_live_subscription_handlers(self):
        if not self.realtime_consumption_enabled:
            return

        self.subscription_rt = TibberHomeRT(self.token, self.id)
        RT_HOMES[self.id] = self.subscription_rt

    def get_cached_price(self):
        if self.last_price is not None and self.last_price_update is not None and\
            datetime.now() - self.last_price_update < timedelta(seconds=PRICE_CACHE_TTL_SECONDS):
            return self.last_price.copy()
        return None

    def get_price(self):
        if self.last_price_update is None or\
            datetime.now() - self.last_price_update > timedelta(seconds=PRICE_CACHE_REFRESH_SECONDS):
            self.last_price_update = datetime.now()
            logging.info('Fetching current priceinfo for homeId {homeid}'.format(homeid=self.id))
            data = self.query_client.execute(query="""
            {{
                viewer {{
                    home(id: "{homeid}") {{
                        id
                        currentSubscription {{
                            priceInfo {{
                                current {{
                                    total
                                    energy
                                    tax
                                    currency
                                    level
                                }}
                            }}
                        }}
                    }}
                }}
            }}
            """.format(homeid=self.id))
            try:
                self.last_price = data['data']['viewer']['home']['currentSubscription']['priceInfo']['current']
            except TypeError as e:
                logging.warning('Failed to get price from response {response}: {err}'.format(response=data, err=str(e)))
                return None
        
        return self.last_price.copy()

class TibberCollector(object):
    def __init__(self):
        self.token = os.environ.get('TIBBER_TOKEN')
        self.checkconfig()

        headers = { 'Authorization': 'Bearer ' + self.token }
        self.query_client = GraphqlClient(endpoint=QUERY_ENDPOINT, headers=headers)

        self.homes = {}

    def checkconfig(self):
        if self.token is None:
            raise AssertionError('TIBBER_TOKEN environment is not set')

    def setup_subscriptions(self):
        homes = []
        try:
            homes = self.get_homes()
        except requests.exceptions.HTTPError as e:
            logging.error('Failed to query homes: {err}'.format(err=str(e)))

        for home in homes:
            tibberhome = TibberHome(self.token, home)
            self.homes[tibberhome.id] = tibberhome
            tibberhome.create_live_subscription_handlers()

    def setup_metrics_price(self, metrics):
        metrics['current_price_energy'] = GaugeMetricFamily('tibber_price_energy','Current energy price', labels=['id', 'home', 'currency'])
        metrics['current_price_tax']    = GaugeMetricFamily('tibber_price_tax',   'Current energy tax',   labels=['id', 'home', 'currency'])
        metrics['current_price_total']  = GaugeMetricFamily('tibber_price_total', 'Current total price',  labels=['id', 'home', 'currency'])

    def setup_metrics_live_measurement(self, metrics):
        metrics['accumulated_consumption']       = CounterMetricFamily('tibber_total_consumption_kwh',     'Last meter active import register state', labels=['id', 'home'])
        metrics['today_accumulated_consumption'] = CounterMetricFamily('tibber_today_consumption_kwh',     'Accumulated consumption since midnight',  labels=['id', 'home'])
        metrics['today_accumulated_cost']        = CounterMetricFamily('tibber_today_consumption_cost',    'Accumulated cost since midnight',         labels=['id', 'home', 'currency'])
        metrics['today_avg_power']               = GaugeMetricFamily('tibber_today_avg_power_watt',        'Average power since midnight',            labels=['id', 'home'])
        metrics['power']                         = GaugeMetricFamily('tibber_power_watt',                  'Current power draw',                      labels=['id', 'home'])
        metrics['power_factor']                  = GaugeMetricFamily('tibber_power_factor',                'Current power factor',                    labels=['id', 'home'])
        metrics['power_reactive']                = GaugeMetricFamily('tibber_power_reactive_kvar',         'Current reactive consumption',            labels=['id', 'home'])
        metrics['current']                       = GaugeMetricFamily('tibber_current_a',                   'Current power draw',                      labels=['id', 'home', 'phase'])
        metrics['potential']                     = GaugeMetricFamily('tibber_potential_v',                 'Current electric potential',              labels=['id', 'home', 'phase'])
        metrics['signal_strength']               = GaugeMetricFamily('tibber_pulse_signal_strength_db',    'Pulse Device signal strength',            labels=['id', 'home'])

    def add_metrics_price(self, metrics, home, data):
        labels = [
            home.id,
            home.get_name(),
            data['currency']
        ]
        metrics['current_price_energy'].add_metric(labels, float(data['energy']))
        metrics['current_price_tax'].add_metric(labels, float(data['tax']))
        metrics['current_price_total'].add_metric(labels, float(data['total']))

    def add_metrics_live_measurement(self, metrics, home, data):
        labels = [
            home.id,
            home.get_name(),
        ]
        metrics['accumulated_consumption'].add_metric(labels, float(data['lastMeterConsumption']))
        metrics['today_accumulated_consumption'].add_metric(labels, float(data['accumulatedConsumption']))
        metrics['today_accumulated_cost'].add_metric([home.id, home.get_name(), data['currency']], float(data['accumulatedCost']))
        metrics['today_avg_power'].add_metric(labels, float(data['averagePower']))
        metrics['power'].add_metric(labels, float(data['power']))
        metrics['power_factor'].add_metric(labels, float(data['powerFactor']))
        metrics['power_reactive'].add_metric(labels, float(data['powerReactive']))
        metrics['current'].add_metric([home.id, home.get_name(), '1'], float(data['currentL1']))
        metrics['current'].add_metric([home.id, home.get_name(), '2'], float(data['currentL2']))
        metrics['current'].add_metric([home.id, home.get_name(), '3'], float(data['currentL3']))
        metrics['potential'].add_metric([home.id, home.get_name(), '1'], float(data['voltagePhase1']))
        metrics['potential'].add_metric([home.id, home.get_name(), '2'], float(data['voltagePhase2']))
        metrics['potential'].add_metric([home.id, home.get_name(), '3'], float(data['voltagePhase3']))
        if data['signalStrength'] is not None:
            metrics['signal_strength'].add_metric(labels, float(data['signalStrength']))

    def collect(self):
        logging.info('Collect')

        if not self.homes:
            self.setup_subscriptions()

        metrics = {}

        self.setup_metrics_price(metrics)
        self.setup_metrics_live_measurement(metrics)
        for id, home in self.homes.items():
            price = None
            try:
                price = home.get_price()
            except (requests.exceptions.HTTPError, BrokenPipeError,
                requests.exceptions.Timeout, socket.timeout,
                urllib3.exceptions.ReadTimeoutError) as e:
                logging.warning('Failed to query home {homeid} for price: {err}'.format(homeid=home.id, err=str(e)))
                price = home.get_cached_price()
            except Exception as e:
                logging.warning('Unknown error processing home {homeid} for price: {err}'.format(homeid=home.id, err=str(e)))

            if price is not None:
                self.add_metrics_price(metrics, home, price)

            live_measurement = home.get_last_live_measurement()

            if live_measurement is not None:
                self.add_metrics_live_measurement(metrics, home, live_measurement)

        for key, val in metrics.items():
            yield val
        logging.info('Collect DONE')


    def get_homes(self):
        data = self.query_client.execute(query="""
        {
            viewer {
                homes {
                    id
                    appNickname
                    features {
                        realTimeConsumptionEnabled
                    }
                }
            }
        }
        """)
        try:
            return data['data']['viewer']['homes']
        except TypeError as e:
            logging.warning('Failed to get price from response {response}: {err}'.format(response=data, err=str(e)))

        return None


async def subscriptions():
    while True:
        tasks = []
        for rt in RT_HOMES.values():
            if rt.subscription_task is None:
                tasks.append(rt.subscribe_live_measurements())
            else:
                tasks.append(rt.subscription_task)
                
        if not tasks:
            time.sleep(1)
            continue

        logging.info('Suspending thread waiting for task Gather')
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError as e:
            logging.warning('Async operation cancelled ({err}) restarting operations'.format(err=str(e)))
        except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.InvalidStatusCode) as e:
            logging.error('Connection error ({err})'.format(err=str(e)))

        for rt in RT_HOMES.values():
            if rt.is_subscribed() and rt.subscription_task.done():
                logging.info("Voiding subscription for {homeid}".format(homeid=rt.id))
                rt.void_subscription()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Tibber Prometheus exporter')
    parser.add_argument('--port', dest='port', default=PORT, help='Port to listen to')

    logging.basicConfig(
        format='%(asctime)s %(levelname)-8s %(message)s',
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S')

    args = parser.parse_args()
    port = args.port

    REGISTRY.register(TibberCollector())
    start_http_server(port)
    logging.info('HTTP server started on {port}'.format(port=port))
    try:
        asyncio.run(subscriptions())
    except KeyboardInterrupt:
        print("Break")

exit(0)
