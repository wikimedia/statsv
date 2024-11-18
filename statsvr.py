# -*- coding: utf-8 -*-
"""
  statsvr
  ~~~~~~~
  A simple statsd -> web request gateway.

  Copyright 2015 Ori Livneh <ori@wikimedia.org>

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
import importlib
import logging
import re
import socket
import urllib
import urllib2

importlib.reload(sys)
sys.setdefaultencoding('utf-8')

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind('', 8125)

BUFFER_SIZE = 2 ** 16
STATSV_URL = 'https://www.wikimedia.org/beacon/statsv'


def sanitize_key(key):
    key = key.replace('/', '-')
    key = re.sub(r'\s+', '_', key)
    key = re.sub(r'[^\w.-]', '', key)
    return key


def url_encode_metric(key, value, type):
    return '%s=%s%s' % (urllib.quote(key), value, type)


def dispatch(uri):
    logging.info(uri)
    urllib2.urlopen(uri)


while 1:
    data, _ = sock.recvfrom(BUFFER_SIZE)
    query = []
    for metric in data.rstrip('\n').split('\n'):
        match = re.match(r'\A([^:]+):([^|]+)\|(.+)', metric)
        if not match:
            logging.error('Discarding invalid metric %s', metric)
            continue
        key, value, type = match.groups()
        key = sanitize_key(key)
        query.append(url_encode_metric(key, value, type))
        if len(query) >= 10:
            dispatch('%s?%s' % (STATSV_URL, '&'.join(query)))
