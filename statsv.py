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
import argparse
import json
import logging
import multiprocessing
import os
import re
import socket
import sys
import time

import urllib.parse as urlparse

from kafka import KafkaConsumer

ap = argparse.ArgumentParser(
    description='statsv - consumes from varnishkafka Kafka topic and writes metrics to statsd'
)
ap.add_argument(
    '--topics',
    help='Kafka topics from which to consume, comma-separated.  Default: statsv',
    default='statsv'
)
ap.add_argument(
    '--consumer-group',
    help='Kafka consumer group. Default: statsv',
    default='statsv'
)
ap.add_argument(
    '--brokers',
    help='Kafka brokers, comma-separated.'
    'Default: localhost:9092 or localhost:9093 (based on --security-protocol)',
    default=None
)
ap.add_argument(
    '--security-protocol',
    help='Kafka broker protocol. '
    'Valid values are: PLAINTEXT, SSL, SASL_PLAINTEXT, SASL_SSL. Default: PLAINTEXT',
    choices=('PLAINTEXT', 'SSL', 'SASL_PLAINTEXT', 'SASL_SSL'),
    default='PLAINTEXT'
)
ap.add_argument(
    '--ssl-cafile',
    help='Optional certificate authority file to use with Kafka connection.',
    default=None
)
ap.add_argument(
    '--api-version',
    help='Kafka API version override. Defaults to autodetection.',
    default=None
)
ap.add_argument(
    '--kafka-fixture',
    help='Optional fixture file to read instead of consuming from Kafka.',
    default=None
)
ap.add_argument(
    '--statsd',
    help='Target host:port for StatsD format. Intended to support Graphite/statsite. Default: statsd:8125',
    default='statsd:8125'
)
ap.add_argument(
    '--dogstatsd',
    help='Target host:port for DogStatsD format. Intended to support Prometheus via statsd-exporter.',
    default=None
)
ap.add_argument(
    '--workers',
    help='Number of processes to spawn that will process the consumed messages '
    'and send to statsd.  Default: half the number of CPUs, or 1.',
    type=int,
    default=max(1, multiprocessing.cpu_count() // 2)
)
ap.add_argument(
    '--consumer-timeout-seconds',
    help='Exit the process after the Kafka consumer does not receive a message in this amount of. '
    'Default: 60',
    type=int,
    default=60
)
ap.add_argument(
    '--socket-timeout-seconds',
    help='Timeout for all socket instances. '
         'Default: 0.5',
    type=float,
    default=0.5
)
ap.add_argument(
    '--dry-run',
    help='If true, nothing will be sent to statsd. '
    'Instead, each statsd output line is logged at the INFO level. Default: False',
    action='store_true',
    default=False
)
ap.add_argument(
    '--log-level',
    help='Logging level. Default: INFO',
    default='INFO'
)

args = ap.parse_args()

#  Setup logging
logging.basicConfig(stream=sys.stderr, level=args.log_level,
                    format='%(asctime)s %(message)s')
# Set kafka module logging level to INFO
logging.getLogger("kafka").setLevel(logging.INFO)

# T389469 - assign a timeout for all new sockets created so the process doesn't hang indefinitely
socket.setdefaulttimeout(args.socket_timeout_seconds)

dry_run = args.dry_run

# parse args for configuration
statsd_addr = args.statsd.split(':')
if len(statsd_addr) > 1:
    statsd_addr[1] = int(statsd_addr[1])
statsd_addr = tuple(statsd_addr)

dogstatsd_addr = args.dogstatsd.split(':') if args.dogstatsd else None
if dogstatsd_addr:
    dogstatsd_addr[1] = int(dogstatsd_addr[1])
    dogstatsd_addr = tuple(dogstatsd_addr)

worker_count = args.workers

SUPPORTED_METRIC_TYPES = ('c', 'g', 'ms')

ACCEPTED_DOGSTATSD_COUNTER = r'^mediawiki_[A-Za-z0-9_]+_total:[0-9]+\|c(\|#[A-Za-z0-9_:,]+)?$'
ACCEPTED_DOGSTATSD_TIMING = r'^mediawiki_[A-Za-z0-9_]+_seconds:[0-9]+\|ms(\|#[A-Za-z0-9_:,]+)?$'
ACCEPTED_DOGSTATSD_HISTOGRAM = \
    r'^mediawiki_[A-Za-z0-9_]+_distribution_(bucket|count|sum):-?[0-9]+\|c(\|#[A-Za-z0-9_:,.+-]+)?$'

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


def emit(sock, addr, payload):
    if dry_run:
        logging.info(payload)
    else:
        try:
            sock.sendto(payload.encode('utf-8'), addr)
        except socket.gaierror:
            # log the target name
            target = ':'.join(addr)  # convert tuple back to <host>:<port>
            logging.error(f"socket.gaierror - Name or service not known: '{target}'")
        # not catching socket.timeout here to allow process to exit and systemd to restart it


def process_queue(q):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # set up utilization tracking
    dogstatsd_lines_handled = statsd_lines_handled = 0
    while 1:
        raw_data = q.get()
        try:
            data = json.loads(raw_data)
        except:  # noqa: E722
            logging.exception(raw_data)
        try:
            query_string = data['uri_query'].lstrip('?')
            # == DogStatsD/Prometheus ==
            #
            # This only allows metrics in the  `mediawiki_` namespace, and strictly requires
            # valid metric/label names per the Prometheus/OpenMetrics spec (A-Z, a-z, 0-9, _).
            #
            # * https://prometheus.io/docs/instrumenting/exposition_formats/#openmetrics-text-format
            # * https://github.com/prometheus/OpenMetrics/blob/v1.0.0/specification/OpenMetrics.md#abnf
            # * https://www.rfc-editor.org/rfc/rfc5234#appendix-B.1
            #
            # Example:
            # ```
            # mediawiki_example_total:1|c%0Amediawiki_example_total:42|c|%23key1:value1,key2:value2
            #
            # (
            #   'mediawiki_example_total:1|c'
            #   'mediawiki_example_total:42|c|#key1:value1,key2:value2'
            # )
            # ```
            #
            # == Legacy StatsD/Graphite ==
            #
            # This has no enforced naming scheme and no enforced illegal characters,
            # although they generally start with "MediaWiki.", and can be relied upon
            # to include at least `=` as separator between each name and value,
            # as parsed by `urlparse.parse_qsl`.
            #
            # Example:
            # ```
            # MediaWiki.example=1c&MediaWiki.example.value1.value2=42c
            # (
            #   ('MediaWiki.example', '1c'),
            #   ('MediaWiki.example.value1.value2', '42c')
            # )
            # ```
            if '=' not in query_string:
                lines = urlparse.unquote(query_string).split('\n')
                for dogstatsd_message in lines:
                    # Discard data that obviously bypassed statsd.js with unexpected chars
                    # to prevent pollution, and avoid injecting other lines/instructions.
                    # No full grammar validation as Prometheus statsd_exporter already
                    # normalizes/discards for us as-needed.
                    if (
                        re.match(ACCEPTED_DOGSTATSD_COUNTER, dogstatsd_message)
                        or re.match(ACCEPTED_DOGSTATSD_TIMING, dogstatsd_message)
                        or re.match(ACCEPTED_DOGSTATSD_HISTOGRAM, dogstatsd_message)
                    ):
                        dogstatsd_lines_handled += 1
                        emit(sock, dogstatsd_addr, dogstatsd_message)
                    else:
                        logging.debug('Discarded dogstatsd message: "%s"' % dogstatsd_message)
            else:
                lines = urlparse.parse_qsl(query_string)
                for metric_name, value in lines:
                    metric_value, metric_type = re.search(
                            r'^(\d+)([a-z]+)$', value).groups()
                    assert metric_type in SUPPORTED_METRIC_TYPES
                    statsd_message = '%s:%s|%s' % (
                            metric_name, metric_value, metric_type)
                    statsd_lines_handled += 1
                    emit(sock, statsd_addr, statsd_message)

        except (AssertionError, AttributeError, KeyError):
            pass

        # track lines handled
        if dogstatsd_lines_handled > 0:
            payload = f'statsv_messages_handled_total:{dogstatsd_lines_handled}|c|#format:dogstatsd'
            emit(sock, dogstatsd_addr, payload)
            dogstatsd_lines_handled = 0
        if statsd_lines_handled > 0:
            payload = f'statsv_messages_handled_total:{statsd_lines_handled}|c|#format:statsd'
            emit(sock, dogstatsd_addr, payload)
            statsd_lines_handled = 0


def get_statsv_reader(args):
    '''
    Returns: a string generator where each message is a wmf.webrequest JSON dictionary
    '''
    if args.kafka_fixture:
        with open(args.kafka_fixture) as fixture_file:
            for line in fixture_file:
                yield line

        time.sleep(1)
        logging.info('Reached end of fixture file.')
        return

    if args.brokers is None:
        if args.security_protocol in ("SSL", "SASL_SSL"):
            kafka_bootstrap_servers = ("localhost:9093",)
        else:
            kafka_bootstrap_servers = ("localhost:9092",)
    else:
        kafka_bootstrap_servers = tuple(args.brokers.split(','))

    kafka_topics = args.topics.split(',')

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
        group_id=args.consumer_group,
        auto_offset_reset='latest',
        # statsd metrics don't make sense if they lag, so disable commits to avoid
        # resuming at historical committed offset.
        enable_auto_commit=False,
        api_version=kafka_api_version,
        consumer_timeout_ms=args.consumer_timeout_seconds * 1000
    )
    consumer.subscribe(kafka_topics)

    logging.info('Starting statsv Kafka consumer.')

    try:
        for message in consumer:
            if message is not None:
                yield message.value.decode('utf-8')
    finally:
        consumer.close()


def statsv_main(args):
    # Spawn worker_count workers to process incoming varnshkafka statsv messages.
    queue = multiprocessing.Queue()

    logging.info('Spawning %d workers to process statsv messages' % worker_count)
    for _ in range(worker_count):
        worker = multiprocessing.Process(target=process_queue, args=(queue,))
        worker.daemon = True
        worker.start()

    watchdog = Watchdog()

    logging.info('Starting statsv Kafka consumer.')
    # Consume messages from Kafka and put them onto the queue.
    try:
        for message in get_statsv_reader(args):
            queue.put(message)
            watchdog.notify()

        # If we arrive here, consumer_timeout_seconds elapsed with no events received.
        raise RuntimeError('No messages received in %d seconds.' % args.consumer_timeout_seconds)
    except Exception:
        logging.exception("Caught exception, aborting.")
    finally:
        queue.close()


if __name__ == '__main__':
    statsv_main(args)
