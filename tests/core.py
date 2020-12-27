import unittest
from functools import partial
from time import sleep
from threading import Thread, Lock
from socket import gethostname
from urllib.parse import urlparse

from kazoo.client import KazooClient
import requests
import waitress

from sfdc.core import SfdcCore
from sfdc.consistent import Consistent
from sfdc.topology.zk import ZkServiceDiscovery

def sfdc_consistent_zk(
  zk_client, 
  root_path, 
  this_host, 
  fetching_fn):
  """
  all of these are usually set before anything else
  and on main thread, when bootstrapping

  this function is just for test's simplicity
  """

  def strip_scheme(url):
    # taken from https://stackoverflow.com/questions/21687408/how-to-remove-scheme-from-url-in-python
    parsed = urlparse(url)
    scheme = "%s://" % parsed.scheme
    return parsed.geturl().replace(scheme, '', 1)

  wsgi_serve = partial(waitress.serve, listen=strip_scheme(this_host))

  requests_conn_pool = requests.Session()
  http_adapter = requests.adapters.HTTPAdapter(
    pool_connections=10, pool_maxsize=100)
  requests_conn_pool.mount('http://', http_adapter)

  c = Consistent(hosts=[this_host])
  zksd = ZkServiceDiscovery(
    zk_client,
    root_path,
    this_host,
    c.reset_with_new)

  return SfdcCore(this_host, c, wsgi_serve, requests_conn_pool, fetching_fn)

class TestSfdcCore(unittest.TestCase):
  def test_singlecall_over_network(self):
    print(f"Running test: `test_singlecall_over_network`")

    hosts = [
      f"http://{gethostname()}:7001", 
      f"http://{gethostname()}:7002", 
      f"http://{gethostname()}:7003"]
    zk_hosts = '127.0.0.1:2181,127.0.0.1:2182,127.0.0.1:2183'
    zk_clients = [
      KazooClient(hosts = zk_hosts) 
      for i in range(len(hosts))]

    cb_counter = 0
    def cb(host, params): 
      nonlocal cb_counter
      # emulate latency, so can coalesce
      # only 1 will reach this
      sleep(2)
      cb_counter += params['val']
      return {"status": "OK", "host": host}

    sc = []
    for host, zc in zip(hosts, zk_clients):
      sc.append(
        sfdc_consistent_zk(
          zk_client=zc,
          root_path="/",
          this_host=host,
          fetching_fn=partial(cb, host)
      ))

    # give time for clients to setup
    sleep(3)

    key = "test-key-for-unit-testing"
    params = {"val": 1}

    # setup our own consistent
    # so we can know where it fell to
    c = Consistent(hosts=hosts)
    result_url = c.locate(key)

    def working_thread(s, key, params):
      nonlocal result_url
      resp = s.fetch(key, params)
      self.assertEqual(resp['status'], "OK")
      self.assertEqual(resp['host'], result_url)

    ts = []
    for s in sc:
      t = Thread(
        target=working_thread, 
        args=(s, key, params,))
      t.start()
      ts.append(t)

    [t.join() for t in ts]
    self.assertEqual(cb_counter, 1)

    for zkc in zk_clients:
      zkc.stop()

  def test_singlecall_force_this_node(self):
    print(f"Running test: `test_singlecall_force_this_node`")

    hosts = [
      f"http://{gethostname()}:8001", 
      f"http://{gethostname()}:8002", 
      f"http://{gethostname()}:8003"]
    zk_hosts = '127.0.0.1:2181,127.0.0.1:2182,127.0.0.1:2183'
    zk_clients = [
      KazooClient(hosts = zk_hosts) 
      for i in range(len(hosts))]

    cb_counter = 0
    lock = Lock()
    def cb(host, params): 
      nonlocal cb_counter, lock
      with lock:
        cb_counter += params['val']
      return {"status": "OK", "host": host}

    sc = []
    for host, zc in zip(hosts, zk_clients):
      sc.append(
        sfdc_consistent_zk(
          zk_client=zc,
          root_path="/",
          this_host=host,
          fetching_fn=partial(cb, host)
      ))

    # give time for clients to setup
    sleep(3)

    key = "test-key-for-unit-testing-force-this-node"
    params = {"val": 1}

    def working_thread(s, key, params):
      resp = s.fetch(key, params, force_this_node=True)
      self.assertEqual(resp['status'], "OK")

    ts = []
    for s in sc:
      t = Thread(
        target=working_thread, 
        args=(s, key, params,))
      t.start()
      ts.append(t)

    [t.join() for t in ts]
    self.assertEqual(cb_counter, 3)

    for zkc in zk_clients:
      zkc.stop()
    