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
import gql.transport.exceptions as exceptions
from datetime import datetime, timedelta
from gql import gql, Client
from gql.transport.aiohttp import AIOHTTPTransport
from gql.transport.websockets import WebsocketsTransport
from prometheus_client import start_http_server, Gauge
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY

PORT = 9110
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
    def __init__(self, token, id, websocketsubscriptionurl):
        self.id = id
        self.token = token
        self.last_live_measurement = None
        self.last_live_measurement_update = None
        self.subscription_client_transport = WebsocketsTransport(
            url=websocketsubscriptionurl,
            headers = { 'Authorization': 'Bearer ' + self.token })
        self.subscription_client = Client(transport=self.subscription_client_transport)
        self.subscription_task = None
        self.subscription_start = None
        self.connect_count = 0

    async def live_subscription(self, client):
        query = gql(f"""subscription {{
                liveMeasurement(homeId:"{self.id}") {{
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
        """)
        async for data in client.subscribe(document=query):
            logging.info(f'Got live measurement update for homeId {self.id}')
            try:
                self.last_live_measurement = data['liveMeasurement'].copy()
                self.last_live_measurement_update = datetime.now()
            except (KeyError, TypeError) as e:
                logging.warning(f'Failed to parse live measurement update: {str(e)}')

    async def subscribe_live_measurements(self):
        logging.info(f'Starting subscription for homeId {self.id}')
        await self.void_subscription()
        self.subscription_start = datetime.now()
        self.connect_count += 1

        await self.subscription_client.connect_async()

        self.subscription_task = asyncio.create_task(self.live_subscription(self.subscription_client))
        def _task_done(future):
            logging.debug(f'Task done callback, voiding subscription homeId {self.id}')
            self.void_subscription()

        self.subscription_task.add_done_callback(_task_done)
        return self.subscription_task

    async def void_subscription(self):
        if self.subscription_client_transport.websocket is not None:
            await self.subscription_client_transport.close()
        if hasattr(self.subscription_client, "session"):
            await self.subscription_client.close()

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

    def request_subscription_stop(self):
        if self.is_subscribed():
            if self.subscription_task.done():
                logging.info(f'Task {self.id} done.')
                self.void_subscription()
            else:
                logging.warning(f'Flagging for exit for task {self.id}')
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
            logging.debug(f'Data is stale, we are not starting, we are subscribed and last live measurement was {str(self.last_live_measurement_update)}')
            return True

        return False

class TibberHome(object):
    def __init__(self, token, data, websocketsubscriptionurl):
        self.id = data['id']
        self.token = token
        self.app_nickname = data.get('appNickname')
        self.features = data.get('features')
        self.websocketsubscriptionurl = websocketsubscriptionurl
        self.realtime_consumption_enabled = False
        if self.features is not None:
            self.realtime_consumption_enabled = self.features.get('realTimeConsumptionEnabled')
        self.last_price = None
        self.last_price_update = None
        self.subscription_rt = None
        
        headers = { 'Authorization': 'Bearer ' + self.token }
        transport = AIOHTTPTransport(url=QUERY_ENDPOINT,
                    headers=headers)
        self.query_client = Client(transport=transport)

    def get_name(self):
        if self.app_nickname is not None:
            return self.app_nickname
        return self.id

    def get_last_live_measurement(self):
        if not self.realtime_consumption_enabled or self.subscription_rt is None:
            return None

        if self.subscription_rt.is_stale():
            logging.warning(f'Stale data for homeId {self.id}')
            self.subscription_rt.request_subscription_stop()

        return self.subscription_rt.get_last_live_measurement()

    def create_live_subscription_handlers(self):
        if not self.realtime_consumption_enabled:
            return

        logging.debug(f'Setting up real time subscription at {self.websocketsubscriptionurl}')
        self.subscription_rt = TibberHomeRT(self.token, self.id, self.websocketsubscriptionurl)
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
            logging.info(f'Fetching current priceinfo for homeId {self.id}')
            data = self.query_client.execute(document=gql(f"""
            {{
                viewer {{
                    home(id: "{self.id}") {{
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
            """))
            try:
                if data['viewer']['home']['currentSubscription'] is not None:
                    self.last_price = data['viewer']['home']['currentSubscription']['priceInfo']['current']
            except TypeError as e:
                logging.warning(f'Failed to get price from response {data}: {str(e)}')

        if self.last_price is not None:
            return self.last_price.copy()
        return None

class TibberCollector(object):
    def __init__(self):
        self.token = os.environ.get('TIBBER_TOKEN')
        self.checkconfig()

        headers = { 'Authorization': 'Bearer ' + self.token }
        transport = AIOHTTPTransport(url=QUERY_ENDPOINT,
                    headers=headers)
        self.query_client = Client(transport=transport)
        self.homes = {}

    def checkconfig(self):
        if self.token is None:
            raise AssertionError('TIBBER_TOKEN environment is not set')

    def setup_subscriptions(self):
        homes = []
        try:
            homes, websocketsubscriptionurl = self.get_homes()
        except requests.exceptions.HTTPError as e:
            logging.error(f'Failed to query homes: {str(e)}')

        for home in homes:
            tibberhome = TibberHome(self.token, home, websocketsubscriptionurl)
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
        metrics['live_reconnect_count']          = CounterMetricFamily('tibber_live_reconnect_count',      'Number of reconnects to live service',    labels=['id', 'home'])

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
        if data is not None:
            if data.get('lastMeterConsumption') is not None:
                metrics['accumulated_consumption'].add_metric(labels, float(data['lastMeterConsumption']))
            if data.get('accumulatedConsumption') is not None:
                metrics['today_accumulated_consumption'].add_metric(labels, float(data['accumulatedConsumption']))
            if data.get('accumulatedCost') is not None:
                metrics['today_accumulated_cost'].add_metric([home.id, home.get_name(), data['currency']], float(data['accumulatedCost']))
            metrics['today_avg_power'].add_metric(labels, float(data['averagePower']))
            metrics['power'].add_metric(labels, float(data['power']))

            if data.get('powerFactor') is not None:
                metrics['power_factor'].add_metric(labels, float(data['powerFactor']))
                metrics['power_reactive'].add_metric(labels, float(data['powerReactive']))

            if data['currentL1'] is not None:
                metrics['current'].add_metric([home.id, home.get_name(), '1'], float(data['currentL1']))

            if data['currentL2'] is not None:
                metrics['current'].add_metric([home.id, home.get_name(), '2'], float(data['currentL2']))

            if data['currentL3'] is not None:
                metrics['current'].add_metric([home.id, home.get_name(), '3'], float(data['currentL3']))

            if data['voltagePhase1'] is not None:
                metrics['potential'].add_metric([home.id, home.get_name(), '1'], float(data['voltagePhase1']))

            if data['voltagePhase2'] is not None:
                metrics['potential'].add_metric([home.id, home.get_name(), '2'], float(data['voltagePhase2']))

            if data['voltagePhase3'] is not None:
                metrics['potential'].add_metric([home.id, home.get_name(), '3'], float(data['voltagePhase3']))

            if data['signalStrength'] is not None:
                metrics['signal_strength'].add_metric(labels, float(data['signalStrength']))

        if home.subscription_rt is not None:
            metrics['live_reconnect_count'].add_metric(labels, home.subscription_rt.connect_count)

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
                logging.warning(f'Failed to query home {home.id} for price: {str(e)}')
                price = home.get_cached_price()
            except Exception as e:
                logging.warning(f'Unknown error processing home {home.id} for price: {str(e)}')

            if price is not None:
                self.add_metrics_price(metrics, home, price)

            live_measurement = home.get_last_live_measurement()
            self.add_metrics_live_measurement(metrics, home, live_measurement)

        for key, val in metrics.items():
            yield val
        logging.info('Collect DONE')


    def get_homes(self):
        data = self.query_client.execute(document=gql("""
        {
            viewer {
                homes {
                    id
                    appNickname
                    features {
                        realTimeConsumptionEnabled
                    }
                }
                websocketSubscriptionUrl
            }
        }
        """))
        try:
            return data['viewer']['homes'], data['viewer']['websocketSubscriptionUrl']
        except TypeError as e:
            logging.warning(f'Failed to get price from response {data}: {str(e)}')

        return None


async def subscriptions():
    while True:
        loop_start = datetime.now()
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
        except (asyncio.CancelledError, TimeoutError) as e:
            logging.warning(f'Async operation cancelled ({str(e)}) restarting operations')
        except (exceptions.TransportError,
                exceptions.TransportClosed,
                exceptions.TransportQueryError) as e:

            logging.error(f'Subscription connection error ({str(e)})')
        except (websockets.exceptions.ConnectionClosedError) as e:
            logging.info(f'Subscription connection closed ({str(e)})')

        for rt in RT_HOMES.values():
            if rt.subscription_task is not None:
                try:
                    exception = rt.subscription_task.exception()
                    if exception is not None:
                        raise exception
                except Exception as e:
                    logging.error(f'Subscription task exception ({str(e)})')
                    rt.request_subscription_stop()

            if rt.is_subscribed() and rt.subscription_task.done():
                logging.info(f'Voiding subscription for {rt.id}')
                await rt.void_subscription()

        # If we power through the loop in a short time, backoff and wait before we reconnect
        # to play nice
        if datetime.now() - loop_start < timedelta(seconds=RT_DATA_CONNECT_TIMEOUT_SECONDS):
            time.sleep(10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Tibber Prometheus exporter')
    parser.add_argument('--port', dest='port', default=PORT, help='Port to listen to')
    parser.add_argument('--log', dest='log', default='INFO', help='Loglevel, one of DEBUG, INFO, WARNING, ERROR in decreasing detail')

    args = parser.parse_args()

    numeric_loglevel = getattr(logging, args.log.upper(), None)
    if not isinstance(numeric_loglevel, int):
        raise ValueError(f'Invalid log level: {args.log}')

    logging.basicConfig(
        format='%(asctime)s %(levelname)-8s %(message)s',
        level=numeric_loglevel,
        datefmt='%Y-%m-%d %H:%M:%S')

    REGISTRY.register(TibberCollector())
    start_http_server(args.port)
    logging.info(f'HTTP server started on {args.port}')
    try:
        asyncio.run(subscriptions())
    except KeyboardInterrupt:
        print("Break")

exit(0)
