# -*- coding: utf-8 -*-
"""
  statsv
  ~~~~~~
  A simple web request -> kafka -> statsd gateway.

  Copyright 2014 Ori Livneh <ori@wikimedia.org>

  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.

"""
import sys
import json
import logging
import multiprocessing
import os
import re
import socket
import argparse

import urllib.parse as urlparse

from kafka import KafkaConsumer

ap = argparse.ArgumentParser(
    description='statsv - consumes from varnishkafka Kafka topic and writes metrics to statsd'
)
ap.add_argument(
    '--topics',
    help='Comma separated list of Kafka topics from which to consume.  Default: statsv',
    default='statsv'
)
ap.add_argument(
    '--brokers',
    help='Comma separated string of kafka brokers: '
    'Default: localhost:9092 or localhost:9093 (based on --security-protocol)',
    default=None
)
ap.add_argument(
    '--security-protocol',
    help=' Protocol used to communicate with brokers. '
    'Valid values are: PLAINTEXT, SSL, SASL_PLAINTEXT, SASL_SSL. Default: PLAINTEXT',
    choices=('PLAINTEXT', 'SSL', 'SASL_PLAINTEXT', 'SASL_SSL'),
    default='PLAINTEXT'
)
ap.add_argument(
    '--ssl-cafile',
    help='Optional filename of certificate authority file to use in certificate verification.',
    default=None
)
ap.add_argument(
    '--consumer-group',
    help='Consumer group to register with Kafka. Default: statsv',
    default='statsv'
)
ap.add_argument(
    '--statsd',
    help='statsd host:port. Default: statsd:8125',
    default='statsd:8125'
)
ap.add_argument(
    '--verbose',
    help='If true, statsd metrics will be logged at INFO level. Default: False',
    action='store_true',
    default=False
)
ap.add_argument(
    '--dry-run',
    help='If true, metrics will not be sent to statsd. Default: False',
    action='store_true',
    default=False
)
ap.add_argument(
    '--log-level',
    help='Logging level. Default: INFO',
    default='INFO'
)
ap.add_argument(
    '--consumer-timeout-seconds',
    help='If the Kafka consumer does not receive a message in this amount of time, '
    'it will timeout and this process will exit. Default: 60',
    type=int,
    default=60
)
ap.add_argument(
    '--workers',
    help='Number of processes to spawn that will process the consumed messages '
    'and send to statsd.  Default: half the number of CPUs, or 1.',
    type=int,
    default=max(1, multiprocessing.cpu_count() // 2)
)
ap.add_argument(
    '--api-version',
    default=None
)

args = ap.parse_args()

#  Setup logging
logging.basicConfig(stream=sys.stderr, level=args.log_level,
                    format='%(asctime)s %(message)s')
# Set kafka module logging level to INFO
logging.getLogger("kafka").setLevel(logging.INFO)

verbose = args.verbose
dry_run = args.dry_run

# parse args for configuration
statsd_addr = args.statsd.split(':')
if len(statsd_addr) > 1:
    statsd_addr[1] = int(statsd_addr[1])
statsd_addr = tuple(statsd_addr)

worker_count = args.workers

if args.brokers is None:
    if args.security_protocol in ("SSL", "SASL_SSL"):
        kafka_bootstrap_servers = ("localhost:9093",)
    else:
        kafka_bootstrap_servers = ("localhost:9092",)
else:
    kafka_bootstrap_servers = tuple(args.brokers.split(','))

kafka_topics = args.topics.split(',')
kafka_consumer_group = args.consumer_group
kafka_consumer_timeout_seconds = args.consumer_timeout_seconds

SUPPORTED_METRIC_TYPES = ('c', 'g', 'ms')


SOCK_CLOEXEC = getattr(socket, 'SOCK_CLOEXEC', 0x80000)


class Watchdog:
    """
    Simple notifier for systemd's process watchdog.

    You can use this in message- or request-processing scripts that are
    managed by systemd and that are under constant load, where the
    absence of work is an abnormal condition.

    Make sure the unit file contains `WatchdogSec=1` (or some other
    value) and `Restart=always`. Then you can write something like:

        watchdog = Watchdog()
        while 1:
            handle_request()
            watchdog.notify()

    This way, if the script spends a full second without handling a
    request, systemd will restart it.

    See https://www.freedesktop.org/software/systemd/man/systemd.service.html#WatchdogSec=
    for more details about systemd's watchdog capabilities.
    """

    def __init__(self):
        # Get and clear NOTIFY_SOCKET from the environment to prevent
        # subprocesses from inheriting it.
        self.addr = os.environ.pop('NOTIFY_SOCKET', None)
        if not self.addr:
            self.sock = None
            return

        # If the first character of NOTIFY_SOCKET is "@", the string is
        # understood as an abstract socket address.
        if self.addr.startswith('@'):
            self.addr = '\0' + self.addr[1:]

        self.sock = socket.socket(
            socket.AF_UNIX, socket.SOCK_DGRAM | SOCK_CLOEXEC)

    def notify(self):
        if not self.sock:
            return
        self.sock.sendto(b'WATCHDOG=1', self.addr)


def process_queue(q):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while 1:
        raw_data = q.get()
        try:
            data = json.loads(raw_data.decode('utf-8'))
        except:  # noqa: E722
            logging.exception(raw_data)
        try:
            query_string = data['uri_query'].lstrip('?')
            for metric_name, value in urlparse.parse_qsl(query_string):
                metric_value, metric_type = re.search(
                        r'^(\d+)([a-z]+)$', value).groups()
                assert metric_type in SUPPORTED_METRIC_TYPES
                statsd_message = '%s:%s|%s' % (
                        metric_name, metric_value, metric_type)

                if (verbose):
                    logging.info(statsd_message)

                if (not dry_run):
                    sock.sendto(statsd_message.encode('utf-8'), statsd_addr)

        except (AssertionError, AttributeError, KeyError):
            pass


# Spawn worker_count workers to process incoming varnshkafka statsv messages.
queue = multiprocessing.Queue()

logging.info('Spawning %d workers to process statsv messages' % worker_count)
for _ in range(worker_count):
    worker = multiprocessing.Process(target=process_queue, args=(queue,))
    worker.daemon = True
    worker.start()


if args.api_version is not None:
    # If api_version is given, don't try to autodetect the api version. If the
    # consumer supports higher versions than what the broker is running, it
    # ends up throwing errors on the server when probing.
    kafka_api_version = tuple([int(i) for i in args.api_version.split('.')])
else:
    kafka_api_version = None

# Create our Kafka Consumer instance.
consumer = KafkaConsumer(
    bootstrap_servers=kafka_bootstrap_servers,
    security_protocol=args.security_protocol,
    ssl_cafile=args.ssl_cafile,
    # Our Kafka brokers currently use the cluster name instead of hostname as
    # CN in their TLS certificates.
    ssl_check_hostname=False,
    group_id=kafka_consumer_group,
    auto_offset_reset='latest',
    # statsd metrics don't make sense if they lag, so disable commits to avoid
    # resuming at historical committed offset.
    enable_auto_commit=False,
    api_version=kafka_api_version,
    consumer_timeout_ms=kafka_consumer_timeout_seconds * 1000
)
consumer.subscribe(kafka_topics)

watchdog = Watchdog()

logging.info('Starting statsv Kafka consumer.')
# Consume messages from Kafka and put them onto the queue.
try:
    for message in consumer:
        if message is not None:
            queue.put(message.value)
            watchdog.notify()
    # If we reach this line, kafka_consumer_timeout_seconds elapsed with no events received.
    raise RuntimeError('No messages received in %d seconds.' % kafka_consumer_timeout_seconds)
except Exception:
    logging.exception("Caught exception, aborting.")
finally:
    queue.close()
    consumer.close()
