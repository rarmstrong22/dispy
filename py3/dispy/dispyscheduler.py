#!/usr/bin/env python3


"""
dispyscheduler: Schedule jobs to nodes running 'dispynode'; needed
when multiple processes may use same nodes simultaneously with
SharedJobCluster; see accompanying 'dispy' for more details.
"""

__author__ = "Giridhar Pemmasani (pgiri@yahoo.com)"
__email__ = "pgiri@yahoo.com"
__copyright__ = "Copyright 2011, Giridhar Pemmasani"
__contributors__ = []
__maintainer__ = "Giridhar Pemmasani (pgiri@yahoo.com)"
__license__ = "MIT"
__url__ = "http://dispy.sourceforge.net"
__status__ = "Production"

import os
import sys
import time
import socket
import stat
import logging
import re
import ssl
import atexit
import traceback
import tempfile
import shutil
import glob
import pickle

# 'httpd' module may not be available at sys.path[0] as 'dispy.py' is
# installed in same directory as this script is; prepend directory
# where httpd.py module is installed to sys.path.
for path in sys.path:
    if os.path.isfile(os.path.join(path, 'dispy', 'httpd.py')):
        sys.path.insert(0, path)
        break
del path

from dispy import _Compute, DispyJob, _DispyJob_, _Function, _Node, DispyNode, NodeAllocate, \
    _JobReply, auth_code, num_min, _parse_node_allocs, _node_ipaddr, _XferFile, _dispy_version, \
    _same_file
import dispy.httpd

import asyncoro
from asyncoro import Coro, AsynCoro, AsyncSocket, MetaSingleton, serialize, unserialize

__version__ = _dispy_version
__all__ = []

MaxFileSize = 10*(1024**2)
MsgTimeout = 5

logger = logging.getLogger('dispyscheduler')
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(message)s'))
logger.addHandler(handler)
del handler


class _Cluster(object):
    """Internal use only.
    """
    def __init__(self, compute, node_allocs, scheduler):
        self._compute = compute
        # self.name = compute.name
        self.name = '%s @ %s' % (compute.name, compute.scheduler_ip_addr)
        self._node_allocs = _parse_node_allocs(node_allocs)
        self.scheduler = scheduler
        self.status_callback = None
        self.pending_jobs = 0
        self.pending_results = 0
        self._jobs = []
        self._dispy_nodes = {}
        self.cpu_time = 0
        self.start_time = time.time()
        self.end_time = None
        self.zombie = False
        self.last_pulse = time.time()
        self.client_ip_addr = None
        self.client_port = None
        self.client_job_result_port = None
        self.client_auth = None
        self.ip_addr = None
        self.dest_path = None

    def __getstate__(self):
        state = dict(self.__dict__)
        for var in ('_node_allocs', 'scheduler', 'status_callback', '_jobs', '_dispy_nodes'):
            state.pop(var, None)
        return state

    def node_jobs(self, node, from_node=False, coro=None):
        jobs = Coro(self.scheduler.node_jobs, self, node, from_node, get_uids=False).value()
        return jobs

    def cancel(self, job):
        return self.scheduler.cancel_job(self, job.id)

    def allocate_node(self, node_alloc):
        if not isinstance(node_alloc, list):
            node_alloc = [node_alloc]
        node_allocs = _parse_node_allocs(node_alloc)
        Coro(self.scheduler.allocate_node, self, node_allocs)

    def set_node_cpus(self, node, cpus, coro=None):
        Coro(self.scheduler.set_node_cpus, node, cpus)


class _Scheduler(object, metaclass=MetaSingleton):
    """Internal use only.

    See dispy's JobCluster and SharedJobCluster for documentation.
    """

    def __init__(self, nodes=[], ip_addr=None, ext_ip_addr=None,
                 port=None, node_port=None, scheduler_port=None,
                 pulse_interval=None, ping_interval=None,
                 node_secret='', node_keyfile=None, node_certfile=None,
                 cluster_secret='', cluster_keyfile=None, cluster_certfile=None,
                 dest_path_prefix=None, clean=False, zombie_interval=60, http_server=False):
        if not hasattr(self, 'ip_addr'):
            self.ip_addrs = set()
            if ip_addr:
                if not isinstance(ip_addr, list):
                    ip_addr = [ip_addr]
                for node in ip_addr:
                    addr = _node_ipaddr(node)
                    if addr:
                        self.ip_addrs.add(addr)
                    else:
                        logger.warning('ignoring invalid ip_addr "%s"' % node)
            if not self.ip_addrs:
                self.ip_addrs.add(None)
            self.ext_ip_addrs = set(self.ip_addrs)
            if ext_ip_addr:
                if not isinstance(ext_ip_addr, list):
                    ext_ip_addr = [ext_ip_addr]
                for node in ext_ip_addr:
                    addr = _node_ipaddr(node)
                    if addr:
                        self.ext_ip_addrs.add(addr)
                    else:
                        logger.warning('ignoring invalid ext_ip_addr "%s"' % node)
            if not port:
                port = 51347
            if not node_port:
                node_port = 51348
            if not scheduler_port:
                scheduler_port = 51349
            if not nodes:
                nodes = ['*']

            self.port = port
            self.node_port = node_port
            self.scheduler_port = scheduler_port
            self._node_allocs = _parse_node_allocs(nodes)
            self._nodes = {}
            self.node_secret = node_secret
            self.node_keyfile = node_keyfile
            self.node_certfile = node_certfile
            self.cluster_secret = cluster_secret
            self.cluster_keyfile = cluster_keyfile
            self.cluster_certfile = cluster_certfile
            if not dest_path_prefix:
                dest_path_prefix = os.path.join(tempfile.gettempdir(), 'dispy', 'scheduler')
            self.dest_path_prefix = os.path.abspath(dest_path_prefix.strip()).rstrip(os.sep)
            if clean:
                shutil.rmtree(self.dest_path_prefix, ignore_errors=True)
            if not os.path.isdir(self.dest_path_prefix):
                os.makedirs(self.dest_path_prefix)
                os.chmod(self.dest_path_prefix, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

            if pulse_interval:
                try:
                    self.pulse_interval = float(pulse_interval)
                    assert 1.0 <= self.pulse_interval <= 1000
                except:
                    raise Exception('Invalid pulse_interval; must be between 1 and 1000')
            else:
                self.pulse_interval = None

            if ping_interval:
                try:
                    self.ping_interval = float(ping_interval)
                    assert 1.0 <= self.ping_interval <= 1000
                except:
                    raise Exception('Invalid ping_interval; must be between 1 and 1000')
            else:
                self.ping_interval = None

            if zombie_interval:
                self.zombie_interval = 60 * zombie_interval
                if self.pulse_interval:
                    self.pulse_interval = min(self.pulse_interval, self.zombie_interval / 5.0)
                else:
                    self.pulse_interval = self.zombie_interval / 5.0
            else:
                self.zombie_interval = None

            self.asyncoro = AsynCoro()
            atexit.register(self.shutdown)

            self._clusters = {}
            self.unsched_jobs = 0
            self._sched_jobs = {}
            self._sched_event = asyncoro.Event()
            # once a _job is done (i.e., final result for it is
            # received from node), it is added to done_jobs, so same
            # object is not reused by Python (when a new job is
            # submitted) until the result is sent back to client
            # (otherwise, 'id' may be duplicate)
            self.done_jobs = {}
            self.terminate = False
            self.sign = ''.join(hex(x)[2:] for x in os.urandom(10))
            self.auth = auth_code(self.cluster_secret, self.sign)

            self.select_job_node = self.load_balance_schedule
            self.start_time = time.time()
            self.compute_id = int(1000 * self.start_time)

            if http_server:
                self.httpd = dispy.httpd.DispyHTTPServer(None)
            else:
                self.httpd = None
            self.timer_coro = Coro(self.timer_task)

            self.tcp_coros = []
            self.scheduler_coros = []
            for ip_addr in list(self.ip_addrs):
                self.tcp_coros.append(Coro(self.tcp_server, ip_addr))
                self.scheduler_coros.append(Coro(self.scheduler_server, ip_addr))

            fd = open(os.path.join(self.dest_path_prefix, 'config'), 'wb')
            config = {
                'port': self.port, 'sign': self.sign, 'cluster_secret': self.cluster_secret,
                'node_secret': self.node_secret, 'auth': self.auth
                }
            pickle.dump(config, fd)
            fd.close()

            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_sock.bind(('', self.port))
            self.udp_sock = AsyncSocket(self.udp_sock)
            self.udp_coro = Coro(self.udp_server)

            self.scheduler_coro = Coro(self._schedule_jobs)

    def udp_server(self, coro=None):
        # generator
        assert coro is not None
        coro.set_daemon()

        Coro(self.broadcast_ping)
        Coro(self.send_ping_cluster, self._node_allocs, set())

        while True:
            msg, addr = yield self.udp_sock.recvfrom(1000)
            if msg.startswith(b'PULSE:'):
                msg = msg[len(b'PULSE:'):]
                try:
                    info = unserialize(msg)
                except:
                    logger.warning('Ignoring pulse message from %s', addr[0])
                    continue
                if 'client_port' in info:
                    for cluster in self._clusters.values():
                        if cluster.client_ip_addr == addr[0] and \
                           cluster.client_port == info['client_port']:
                            cluster.last_pulse = time.time()
                else:
                    node = self._nodes.get(info['ip_addr'], None)
                    if node is not None:
                        # assert 0 <= info['cpus'] <= node.cpus
                        node.last_pulse = time.time()
                        pulse_msg = {'ip_addr': info['scheduler_ip_addr'], 'port': self.port}

                        def _send_pulse(self, pulse_msg, addr, coro=None):
                            sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                            sock.settimeout(MsgTimeout)
                            try:
                                yield sock.sendto(b'PULSE:' + serialize(pulse_msg), addr)
                            except:
                                pass
                            sock.close()

                        Coro(_send_pulse, self, pulse_msg, (info['ip_addr'], info['port']))
            elif msg.startswith(b'PING:'):
                try:
                    info = unserialize(msg[len(b'PING:'):])
                    if info['version'] != _dispy_version:
                        logger.warning('Ignoring %s due to version mismatch', addr[0])
                        continue
                    assert info['port'] > 0
                    assert info['ip_addr']
                    # socket.inet_aton(status['ip_addr'])
                except:
                    logger.debug('Ignoring node %s', addr[0])
                    logger.debug(traceback.format_exc())
                    continue
                auth = auth_code(self.node_secret, info['sign'])
                node = self._nodes.get(info['ip_addr'], None)
                if node:
                    if node.auth == auth:
                        continue
                sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                                   keyfile=self.node_keyfile, certfile=self.node_certfile)
                sock.settimeout(MsgTimeout)
                msg = {'port': self.port, 'sign': self.sign, 'version': _dispy_version}
                msg['ip_addrs'] = list(filter(lambda ip: bool(ip), self.ext_ip_addrs))
                try:
                    yield sock.connect((info['ip_addr'], info['port']))
                    yield sock.sendall(auth)
                    yield sock.send_msg(b'PING:' + serialize(msg))
                    info = yield sock.recv_msg()
                except:
                    logger.debug(traceback.format_exc())
                finally:
                    sock.close()
            else:
                # logger.debug('Ignoring UDP message %s from: %s', msg[:min(5, len(msg))], addr[0])
                pass

    def tcp_server(self, ip_addr, coro=None):
        # generator
        coro.set_daemon()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock = AsyncSocket(sock, keyfile=self.node_keyfile, certfile=self.node_certfile)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if not ip_addr:
            ip_addr = ''
        try:
            sock.bind((ip_addr, self.port))
        except:
            if ip_addr == '':
                ip_addr = None
            self.ip_addrs.discard(ip_addr)
            raise StopIteration
        logger.debug('tcp server at %s:%s' % (ip_addr, self.port))
        sock.listen(32)

        while True:
            try:
                conn, addr = yield sock.accept()
            except ssl.SSLError as err:
                logger.debug('SSL connection failed: %s', str(err))
                continue
            except GeneratorExit:
                break
            except:
                logger.debug(traceback.format_exc())
                continue
            # logger.debug('received job result from %s', str(addr))
            Coro(self.tcp_task, conn, addr)

    def tcp_task(self, conn, addr, coro=None):
        # generator
        conn.settimeout(MsgTimeout)
        msg = yield conn.recv_msg()
        if msg.startswith(b'JOB_REPLY:'):
            try:
                info = unserialize(msg[len(b'JOB_REPLY:'):])
            except:
                logger.warning('invalid job reply from %s:%s ignored' % (addr[0], addr[1]))
            else:
                yield self.job_reply_process(info, conn, addr)
        elif msg.startswith(b'PONG:'):
            try:
                info = unserialize(msg[len(b'PONG:'):])
                assert info['auth'] == self.auth
                yield self.add_node(info, coro=coro)
            except:
                logger.debug('Ignoring node %s', addr[0])
                logger.debug(traceback.format_exc())
        elif msg.startswith(b'PING:'):
            try:
                info = unserialize(msg[len(b'PING:'):])
                if info['version'] != _dispy_version:
                    logger.warning('Ignoring %s due to version mismatch', addr[0])
                    raise Exception('')
                assert info['port'] > 0
                assert info['ip_addr']
            except:
                logger.debug('Ignoring node %s', addr[0])
                logger.debug(traceback.format_exc())
                conn.close()
                raise StopIteration
            auth = auth_code(self.node_secret, info['sign'])
            node = self._nodes.get(info['ip_addr'], None)
            if node:
                if node.auth == auth:
                    conn.close()
                    raise StopIteration
            sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                               keyfile=self.node_keyfile, certfile=self.node_certfile)
            sock.settimeout(MsgTimeout)
            msg = {'port': self.port, 'sign': self.sign, 'version': _dispy_version}
            msg['ip_addrs'] = list(filter(lambda ip: bool(ip), self.ext_ip_addrs))
            try:
                yield sock.connect((info['ip_addr'], info['port']))
                yield sock.sendall(auth)
                yield sock.send_msg(b'PING:' + serialize(msg))
                info = yield sock.recv_msg()
            except:
                logger.debug(traceback.format_exc())
            finally:
                sock.close()
        elif msg.startswith(b'FILEXFER:'):
            try:
                xf = unserialize(msg[len(b'FILEXFER:'):])
                msg = yield conn.recv_msg()
                reply = unserialize(msg)
                yield self.xfer_to_client(reply, xf, conn, addr)
            except:
                logger.debug(traceback.format_exc())
        elif msg.startswith(b'TERMINATED:'):
            try:
                info = unserialize(msg[len(b'TERMINATED:'):])
                node = self._nodes.get(info['ip_addr'], None)
                if not node:
                    raise StopIteration
                auth = auth_code(self.node_secret, info['sign'])
                if auth != node.auth:
                    logger.warning('Invalid signature from %s', node.ip_addr)
                    raise StopIteration
                logger.debug('Removing node %s', node.ip_addr)
                del self._nodes[node.ip_addr]
                if node.clusters:
                    dead_jobs = [_job for _job in self._sched_jobs.values()
                                 if _job.node is not None and _job.node.ip_addr == node.ip_addr]
                    cids = list(node.clusters)
                    node.clusters = set()
                    for cid in cids:
                        cluster = self._clusters[cid]
                        cluster._dispy_nodes.pop(node.ip_addr, None)
                    for cid in cids:
                        cluster = self._clusters.get(cid, None)
                        if cluster is None:
                            continue
                        dispy_node = cluster._dispy_nodes[node.ip_addr]
                        yield self.send_node_status(cluster, dispy_node, DispyNode.Closed)
                    yield self.reschedule_jobs(dead_jobs)
            except:
                # logger.debug(traceback.format_exc())
                pass
        else:
            logger.warning('invalid message from %s:%s ignored' % addr)
            logger.debug(traceback.format_exc())
        conn.close()

    def scheduler_server(self, ip_addr, coro=None):
        coro.set_daemon()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock = AsyncSocket(sock, keyfile=self.cluster_keyfile, certfile=self.cluster_certfile)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if not ip_addr:
            ip_addr = ''
        try:
            sock.bind((ip_addr, self.scheduler_port))
        except:
            if ip_addr == '':
                ip_addr = None
            self.ip_addrs.discard(ip_addr)
            raise StopIteration
        logger.debug('scheduler at %s:%s' % (ip_addr, self.scheduler_port))
        sock.listen(32)
        while True:
            conn, addr = yield sock.accept()
            Coro(self.scheduler_task, conn, addr)

    def scheduler_task(self, conn, addr, coro=None):
        # generator
        def _job_request_task(self, cluster, _job):
            # function
            _job.uid = id(_job)
            setattr(_job, 'node', None)
            dest_path = os.path.join(self.dest_path_prefix, str(_job.compute_id))
            for xf in _job.xfer_files:
                xf.name = os.path.join(dest_path, os.path.basename(xf.name))
            job = DispyJob((), {})
            job.id = _job.uid
            delattr(job, 'finish')
            setattr(_job, 'job', job)
            cluster._jobs.append(_job)
            self.unsched_jobs += 1
            cluster.pending_jobs += 1
            cluster.last_pulse = time.time()
            self._sched_event.set()
            if cluster.status_callback:
                cluster.status_callback(DispyJob.Created, None, job)
            return serialize(_job.uid)

        def _compute_task(self, msg):
            # function
            try:
                req = unserialize(msg)
                compute = req['compute']
                node_allocs = req['node_allocs']
            except:
                logger.debug('Ignoring compute request from %s', addr[0])
                return b'NAK'
            for xf in compute.xfer_files:
                if MaxFileSize and xf.stat_buf.st_size > MaxFileSize:
                    logger.warning('transfer file "%s" is too big (%s)',
                                   xf.name, xf.stat_buf.st_size)
                    return b'NAK'
            setattr(compute, 'nodes', {})
            cluster = _Cluster(compute, node_allocs, self)
            cluster.ip_addr = conn.getsockname()[0]
            dest = os.path.join(self.dest_path_prefix, compute.scheduler_ip_addr)
            if not os.path.isdir(dest):
                try:
                    os.mkdir(dest)
                except:
                    return 'NAK'
            if compute.dest_path and isinstance(compute.dest_path, str):
                # TODO: get os.sep from client and convert (in case of mixed environments)?
                if compute.dest_path.startswith(os.sep):
                    cluster.dest_path = compute.dest_path
                else:
                    cluster.dest_path = os.path.join(dest, compute.dest_path)
                if not os.path.isdir(cluster.dest_path):
                    try:
                        os.makedirs(cluster.dest_path)
                    except:
                        return 'NAK'
            else:
                cluster.dest_path = tempfile.mkdtemp(prefix=compute.name + '_', dir=dest)

            compute.id = self.compute_id
            self.compute_id += 1
            self._clusters[compute.id] = cluster

            cluster.client_job_result_port = compute.job_result_port
            cluster.client_ip_addr = compute.scheduler_ip_addr
            cluster.client_port = compute.scheduler_port
            cluster.client_auth = compute.auth
            compute.job_result_port = self.port
            compute.scheduler_port = self.port
            compute.auth = ''.join(hex(x)[2:] for x in os.urandom(10))
            for xf in compute.xfer_files:
                xf.compute_id = compute.id
                xf.name = os.path.join(cluster.dest_path, os.path.basename(xf.name))

            fd = open(os.path.join(self.dest_path_prefix,
                                   '%s_%s' % (compute.id, cluster.client_auth)), 'wb')
            pickle.dump(cluster, fd)
            fd.close()
            logger.debug('New computation %s: %s, %s', compute.id, compute.name, cluster.dest_path)
            resp = {'compute_id': compute.id, 'pulse_interval': self.pulse_interval,
                    'job_result_port': compute.job_result_port}
            return serialize(resp)

        def xfer_from_client(self, msg):
            # generator
            try:
                xf = unserialize(msg)
            except:
                logger.debug('Ignoring file trasnfer request from %s', addr[0])
                raise StopIteration(b'NAK')
            if xf.compute_id not in self._clusters:
                logger.error('computation "%s" is invalid' % xf.compute_id)
                raise StopIteration(b'NAK')
            cluster = self._clusters[xf.compute_id]
            tgt = os.path.join(cluster.dest_path, os.path.basename(xf.name))
            if _same_file(tgt, xf):
                raise StopIteration(b'ACK')
            for cxf in cluster._compute.xfer_files:
                if tgt == cxf.name:
                    break
            else:
                raise StopIteration(b'NAK')
            logger.debug('Copying file %s to %s (%s)', xf.name, tgt, xf.stat_buf.st_size)
            try:
                yield conn.send_msg(b'NAK')
                fd = open(tgt, 'wb')
                n = 0
                while n < xf.stat_buf.st_size:
                    data = yield conn.recvall(min(xf.stat_buf.st_size-n, 1024000))
                    if not data:
                        break
                    fd.write(data)
                    n += len(data)
                    if MaxFileSize and n > MaxFileSize:
                        logger.warning('File "%s" is too big (%s); it is truncated', tgt, n)
                        break
                fd.close()
                if n < xf.stat_buf.st_size:
                    resp = bytes('NAK (read only %s bytes)' % n, 'ascii')
                else:
                    os.utime(tgt, (xf.stat_buf.st_atime, xf.stat_buf.st_mtime))
                    os.chmod(tgt, stat.S_IMODE(xf.stat_buf.st_mode))
                    logger.debug('Copied file %s', tgt)
                    resp = b'ACK'
            except:
                logger.warning('Copying file "%s" failed with "%s"',
                               xf.name, traceback.format_exc())
                resp = b'NAK'
                try:
                    os.remove(tgt)
                    if len(os.listdir(cluster.dest_path)) == 0:
                        os.rmdir(cluster.dest_path)
                except:
                    pass
            raise StopIteration(resp)

        # scheduler_task begins here
        conn.settimeout(MsgTimeout)
        resp = None
        try:
            req = yield conn.recvall(len(self.auth))
        except:
            logger.warning('Failed to read message from %s: %s',
                           str(addr), traceback.format_exc())
            conn.close()
            raise StopIteration

        if req != self.auth:
            msg = yield conn.recv_msg()
            if msg.startswith(b'CLIENT:'):
                try:
                    req = unserialize(msg[len(b'CLIENT:'):])
                    if req['version'] != _dispy_version:
                        logger.warning('Ignoring %s due to version mismatch', addr[0])
                        raise Exception('')
                    if not req['ip_addr']:
                        req['ip_addr'] = addr[0]
                    reply = {'ip_addr': req['ip_addr'], 'port': self.scheduler_port,
                             'sign': self.sign, 'version': _dispy_version}
                    yield conn.send_msg(serialize(reply))
                except:
                    pass
            else:
                logger.warning('Invalid/unauthorized request ignored')
            conn.close()
            raise StopIteration
        msg = yield conn.recv_msg()
        if not msg:
            logger.info('Closing connection')
            conn.close()
            raise StopIteration

        if msg.startswith(b'JOB:'):
            msg = msg[len(b'JOB:'):]
            try:
                req = unserialize(msg)
                _job = req['job']
                cluster = self._clusters[_job.compute_id]
                assert cluster.client_auth == req['auth']
            except:
                resp = None
            else:
                resp = _job_request_task(self, cluster, _job)
        elif msg.startswith(b'COMPUTE:'):
            msg = msg[len(b'COMPUTE:'):]
            resp = _compute_task(self, msg)
        elif msg.startswith(b'ADD_CLUSTER:'):
            msg = msg[len(b'ADD_CLUSTER:'):]
            try:
                req = unserialize(msg)
                cluster = self._clusters[req['compute_id']]
                assert cluster.client_auth == req['auth']
                for xf in cluster._compute.xfer_files:
                    assert os.path.isfile(xf.name)
                resp = serialize(cluster._compute.id)
            except:
                logger.debug('Ignoring compute request from %s', addr[0])
            else:
                Coro(self.add_cluster, cluster)
        elif msg.startswith(b'CLOSE:'):
            msg = msg[len(b'CLOSE:'):]
            try:
                req = unserialize(msg)
                auth = req['auth']
            except:
                logger.warning('Invalid compuation for deleting')
                conn.close()
                raise StopIteration
            cluster = self._clusters.get(req['compute_id'], None)
            if cluster is None or cluster.client_auth != auth:
                # this cluster is closed
                conn.close()
                raise StopIteration
            cluster.zombie = True
            Coro(self.cleanup_computation, cluster)
        elif msg.startswith(b'FILEXFER:'):
            msg = msg[len(b'FILEXFER:'):]
            resp = yield xfer_from_client(self, msg)
        elif msg.startswith(b'NODE_JOBS:'):
            msg = msg[len(b'NODE_JOBS:'):]
            try:
                req = unserialize(msg)
                cluster = self._clusters.get(req['compute_id'], None)
                if cluster is None or cluster.client_auth != req['auth']:
                    job_uids = []
                else:
                    node = req['node']
                    from_node = req['from_node']
                    job_uids = yield self.node_jobs(cluster, node, from_node, coro=coro)
            except:
                job_uids = []
            resp = serialize(job_uids)
        elif msg.startswith(b'TERMINATE_JOB:'):
            msg = msg[len(b'TERMINATE_JOB:'):]
            try:
                req = unserialize(msg)
                uid = req['uid']
                cluster = self._clusters[req['compute_id']]
                assert cluster.client_auth == req['auth']
            except:
                logger.warning('Invalid job cancel message')
                conn.close()
                raise StopIteration
            self.cancel_job(cluster, uid)
        elif msg.startswith(b'RESEND_JOB_RESULTS:'):
            msg = msg[len(b'RESEND_JOB_RESULTS:'):]
            try:
                info = unserialize(msg)
                compute_id = info['compute_id']
                auth = info['auth']
            except:
                resp = serialize(0)
            else:
                cluster = self._clusters.get(compute_id, None)
                if cluster is None:
                    fd = open(os.path.join(self.dest_path_prefix,
                                           '%s_%s' % (compute_id, auth)), 'wb')
                    cluster = pickle.load(fd)
                    fd.close()
                if cluster is None or cluster.client_auth != auth:
                    resp = serialize(0)
                else:
                    resp = serialize(cluster.pending_results + cluster.pending_jobs)
            yield conn.send_msg(resp)
            conn.close()
            if resp > 0:
                yield self.resend_job_results(cluster, coro=coro)
            raise StopIteration
        elif msg.startswith(b'PENDING_JOBS:'):
            msg = msg[len(b'PENDING_JOBS:'):]
            reply = {'done': [], 'pending': 0}
            try:
                info = unserialize(msg)
                compute_id = info['compute_id']
                auth = info['auth']
            except:
                pass
            else:
                cluster = self._clusters.get(compute_id, None)
                if cluster is None:
                    fd = open(os.path.join(self.dest_path_prefix,
                                           '%s_%s' % (compute_id, auth)), 'wb')
                    cluster = pickle.load(fd)
                    fd.close()
                if cluster is not None and cluster.client_auth == auth:
                    done = []
                    if cluster.pending_results:
                        for result_file in glob.glob(os.path.join(cluster.dest_path,
                                                                  '_dispy_job_reply_*')):
                            result_file = os.path.basename(result_file)
                            try:
                                uid = int(result_file[len('_dispy_job_reply_'):])
                            except:
                                pass
                            else:
                                done.append(uid)
                                # limit so as not to take up too much time
                                if len(done) > 50:
                                    break
                    reply['done'] = done
                    reply['pending'] = cluster.pending_jobs
            resp = serialize(reply)
        elif msg.startswith(b'RETRIEVE_JOB:'):
            msg = msg[len(b'RETRIEVE_JOB:'):]
            yield self.retrieve_job_task(conn, msg)
        elif msg.startswith(b'ALLOCATE_NODE:'):
            req = msg[len(b'ALLOCATE_NODE:'):]
            try:
                req = unserialize(req)
                cluster = self._clusters[req['compute_id']]
                assert cluster.client_auth == req['auth']
                resp = yield self.allocate_node(cluster, req['node_alloc'], coro=coro)
                resp = serialize(resp)
            except:
                resp = serialize(-1)
        elif msg.startswith(b'SET_NODE_CPUS:'):
            req = msg[len(b'SET_NODE_CPUS:'):]
            try:
                req = unserialize(req)
                cluster = self._clusters[req['compute_id']]
                assert cluster.client_auth == req['auth']
                resp = yield self.set_node_cpus(req['node'], coro=coro)
                resp = serialize(resp)
            except:
                resp = serialize(-1)
        else:
            logger.debug('Ignoring invalid command')

        if resp is not None:
            try:
                yield conn.send_msg(resp)
            except:
                logger.warning('Failed to send response to %s: %s',
                               str(addr), traceback.format_exc())
        conn.close()
        # end of scheduler_task

    def resend_job_results(self, cluster, coro=None):
        # TODO: limit number queued so as not to take up too much space/time
        result_files = [f for f in os.listdir(cluster.dest_path)
                        if f.startswith('_dispy_job_reply_')]
        result_files = result_files[:min(len(result_files), 64)]
        for result_file in result_files:
            result_file = os.path.join(cluster.dest_path, result_file)
            try:
                fd = open(result_file, 'rb')
                result = pickle.load(fd)
                fd.close()
            except:
                logger.debug('Could not load "%s"', result_file)
            else:
                status = yield self.send_job_result(
                    result.uid, cluster, result, resending=True, coro=coro)
                if status:
                    break

    def timer_task(self, coro=None):
        coro.set_daemon()
        reset = True
        last_ping_time = last_pulse_time = last_zombie_time = time.time()
        while True:
            if reset:
                timeout = num_min(self.pulse_interval, self.ping_interval, self.zombie_interval)

            reset = yield coro.suspend(timeout)
            if reset:
                continue

            now = time.time()
            if self.pulse_interval and (now - last_pulse_time) >= self.pulse_interval:
                last_pulse_time = now
                dead_nodes = {}
                for node in self._nodes.values():
                    if node.busy and (node.last_pulse + (5 * self.pulse_interval)) < now:
                        logger.warning('Node %s is not responding; removing it (%s, %s, %s)',
                                       node.ip_addr, node.busy, node.last_pulse, now)
                        dead_nodes[node.ip_addr] = node
                for ip_addr in dead_nodes:
                    node = self._nodes.pop(ip_addr, None)
                    for cid in node.clusters:
                        cluster = self._clusters[cid]
                        dispy_node = cluster._dispy_nodes.get(ip_addr, None)
                        if dispy_node:
                            dispy_node.busy = 0
                            yield self.send_node_status(cluster, dispy_node, DispyNode.Closed)

                dead_jobs = [_job for _job in self._sched_jobs.values()
                             if _job.node is not None and _job.node.ip_addr in dead_nodes]
                self.reschedule_jobs(dead_jobs)
                if dead_nodes or dead_jobs:
                    self._sched_event.set()
                resend = [resend_cluster for resend_cluster in self._clusters.values()
                          if resend_cluster.pending_results and not resend_cluster.zombie]
                for cluster in resend:
                    Coro(self.resend_job_results, cluster)

            if self.ping_interval and (now - last_ping_time) >= self.ping_interval:
                last_ping_time = now
                for cluster in self._clusters.values():
                    Coro(self.send_ping_cluster, cluster._node_allocs,
                         set(cluster._dispy_nodes.keys()))
            if self.zombie_interval and (now - last_zombie_time) >= self.zombie_interval:
                last_zombie_time = now
                for cluster in self._clusters.values():
                    if (now - cluster.last_pulse) > self.zombie_interval:
                        cluster.zombie = True
                zombies = [cluster for cluster in self._clusters.values()
                           if cluster.zombie and cluster.pending_jobs == 0]
                for cluster in zombies:
                    logger.debug('Deleting zombie computation "%s" / %s',
                                 cluster._compute.name, cluster._compute.id)
                    Coro(self.cleanup_computation, cluster)

    def xfer_to_client(self, reply, xf, sock, addr):
        _job = self._sched_jobs.get(reply.uid, None)
        if _job is None or _job.hash != reply.hash:
            logger.warning('Ignoring invalid file transfer from job %s at %s', reply.uid, addr[0])
            yield sock.send_msg(b'NAK')
            raise StopIteration
        node = self._nodes.get(reply.ip_addr, None)
        cluster = self._clusters.get(_job.compute_id, None)
        if not node or not cluster:
            logger.warning('Ignoring invalid file transfer from job %s at %s', reply.uid, addr[0])
            yield sock.send_msg(b'NAK')
            raise StopIteration
        yield sock.send_msg(b'ACK')
        node.last_pulse = time.time()
        client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_sock = AsyncSocket(client_sock,
                                  keyfile=self.cluster_keyfile, certfile=self.cluster_certfile)
        client_sock.settimeout(MsgTimeout)
        try:
            yield client_sock.connect((cluster.client_ip_addr, cluster.client_job_result_port))
            yield client_sock.send_msg(b'FILEXFER:' + serialize(xf))
            yield client_sock.send_msg(serialize(reply))
            ack = yield client_sock.recv_msg()
            assert ack == b'ACK'

            yield sock.send_msg(b'ACK')
            n = 0
            while n < xf.stat_buf.st_size:
                data = yield sock.recvall(min(xf.stat_buf.st_size-n, 1024000))
                if not data:
                    break
                yield client_sock.sendall(data)
                n += len(data)
            ack = yield client_sock.recv_msg()
            assert ack == b'ACK'
        except:
            yield sock.send_msg(b'NAK')
        else:
            yield sock.send_msg(b'ACK')
        finally:
            client_sock.close()
            sock.close()

    def send_ping_node(self, ip_addr, port=None, coro=None):
        ping_msg = {'version': _dispy_version, 'sign': self.sign, 'port': self.port}
        ping_msg['ip_addrs'] = list(filter(lambda ip: bool(ip), self.ext_ip_addrs))
        udp_sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        udp_sock.settimeout(MsgTimeout)
        if not port:
            port = self.node_port
        try:
            yield udp_sock.sendto(b'PING:' + serialize(ping_msg), (ip_addr, port))
        except:
            # logger.debug(traceback.format_exc())
            pass
        udp_sock.close()
        tcp_sock = AsyncSocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                               keyfile=self.node_keyfile, certfile=self.node_certfile)
        tcp_sock.settimeout(MsgTimeout)
        try:
            yield tcp_sock.connect((ip_addr, port))
            yield tcp_sock.sendall(b'x' * len(self.auth))
            yield tcp_sock.send_msg(b'PING:' + serialize(ping_msg))
            yield tcp_sock.recv_msg()
        except:
            # logger.debug(traceback.format_exc())
            pass
        tcp_sock.close()

    def broadcast_ping(self, port=None, coro=None):
        # generator
        if not port:
            port = self.node_port
        ping_msg = {'version': _dispy_version, 'sign': self.sign, 'port': self.port}
        ping_msg['ip_addrs'] = list(filter(lambda ip: bool(ip), self.ext_ip_addrs))
        bc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        bc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        bc_sock = AsyncSocket(bc_sock)
        bc_sock.settimeout(MsgTimeout)
        try:
            yield bc_sock.sendto(b'PING:' + serialize(ping_msg), ('<broadcast>', port))
        except:
            pass
        bc_sock.close()

    def send_ping_cluster(self, node_allocs, present_ip_addrs, coro=None):
        # generator
        for node_alloc in node_allocs:
            # TODO: we assume subnets are indicated by '*', instead of
            # subnet mask; this is a limitation, but specifying with
            # subnet mask a bit cumbersome.
            if node_alloc.ip_rex.find('*') >= 0:
                yield self.broadcast_ping(node_alloc.port)
            else:
                ip_addr = node_alloc.ip_addr
                if ip_addr in present_ip_addrs:
                    continue
                port = node_alloc.port
                Coro(self.send_ping_node, ip_addr, port)

    def add_cluster(self, cluster, coro=None):
        # generator
        assert coro is not None
        compute = cluster._compute
        compute.pulse_interval = self.pulse_interval
        if self.httpd and cluster.status_callback is None:
            self.httpd.add_cluster(cluster)
        # TODO: should we allow clients to add new nodes, or use only
        # the nodes initially created with command-line?
        yield self.send_ping_cluster(cluster._node_allocs,
                                     set(cluster._dispy_nodes.keys()), coro=coro)
        compute_nodes = []
        for ip_addr, node in self._nodes.items():
            if compute.id in node.clusters:
                continue
            for node_alloc in cluster._node_allocs:
                cpus = node_alloc.allocate(cluster, node.ip_addr, node.name, node.avail_cpus)
                if cpus > 0:
                    cluster._dispy_nodes.pop(node.ip_addr, None)
                    compute_nodes.append(node)
        for node in compute_nodes:
            yield self.setup_node(node, [compute], coro=coro)

    def cleanup_computation(self, cluster, coro=None):
        # generator
        if not cluster.zombie:
            raise StopIteration
        if cluster.pending_jobs:
            logger.debug('pedning jobs for "%s" / %s: %s', cluster._compute.name,
                         cluster._compute.id, cluster.pending_jobs)
            raise StopIteration

        compute = cluster._compute
        if self._clusters.pop(compute.id, None) is None:
            logger.warning('Invalid computation "%s" to cleanup ignored' % compute.id)
            raise StopIteration

        pkl_path = os.path.join(self.dest_path_prefix,
                                '%s_%s' % (compute.id, cluster.client_auth))
        if cluster.pending_results == 0:
            try:
                os.remove(pkl_path)
            except:
                logger.warning('Could not remove "%s"' % pkl_path)
        else:
            fd = open(pkl_path, 'wb')
            pickle.dump(compute, fd)
            fd.close()
        for xf in compute.xfer_files:
            try:
                os.remove(xf.name)
            except:
                logger.warning('Could not remove file "%s"', xf.name)

        if os.path.isdir(cluster.dest_path) and len(os.listdir(cluster.dest_path)) == 0:
            logger.debug('Removing "%s"', cluster.dest_path)
            try:
                os.rmdir(cluster.dest_path)
            except:
                logger.warning('Could not remove directory "%s"', cluster.dest_path)

        # remove cluster from all nodes before closing (which uses
        # yield); otherwise, scheduler may access removed cluster
        # through node.clusters
        for ip_addr in cluster._dispy_nodes:
            node = self._nodes.get(ip_addr, None)
            if not node:
                continue
            node.clusters.discard(compute.id)
        for ip_addr, dispy_node in cluster._dispy_nodes.items():
            node = self._nodes.get(ip_addr, None)
            if not node:
                continue
            try:
                yield node.close(compute)
            except:
                logger.warning('Closing node %s failed', node.ip_addr)
            yield self.send_node_status(cluster, dispy_node, DispyNode.Closed)
        cluster._dispy_nodes = {}
        if self.httpd:
            self.httpd.del_cluster(cluster)

    def setup_node(self, node, computes, coro=None):
        # generator
        assert coro is not None
        for compute in computes:
            # NB: to avoid computation being sent multiple times, we
            # add to cluster's _dispy_nodes before sending computation
            # to node
            cluster = self._clusters[compute.id]
            if node.ip_addr in cluster._dispy_nodes:
                continue
            dispy_node = DispyNode(node.ip_addr, node.name, node.cpus)
            dispy_node.avail_cpus = node.avail_cpus
            cluster._dispy_nodes[node.ip_addr] = dispy_node
            r = yield node.setup(compute, coro=coro)
            if r:
                cluster._dispy_nodes.pop(node.ip_addr, None)
                logger.warning('Failed to setup %s for computation "%s"',
                               node.ip_addr, compute.name)
                Coro(node.close, compute)
            else:
                node.clusters.add(compute.id)
                self._sched_event.set()
                dispy_node.update_time = time.time()
                Coro(self.send_node_status, cluster, dispy_node, DispyNode.Initialized)

    def add_node(self, info, coro=None):
        try:
            # assert info['version'] == _dispy_version
            assert info['port'] > 0 and info['cpus'] > 0
            socket.inet_aton(info['ip_addr'])
            # TODO: check if it is one of ext_ip_addr?
        except:
            # logger.debug(traceback.format_exc())
            # logger.debug('Ignoring node %s', addr[0])
            raise StopIteration
        node = self._nodes.get(info['ip_addr'], None)
        if node is None:
            logger.debug('Discovered %s:%s (%s) with %s cpus',
                         info['ip_addr'], info['port'], info['name'], info['cpus'])
            node = _Node(info['ip_addr'], info['port'], info['cpus'], info['sign'],
                         self.node_secret, keyfile=self.node_keyfile, certfile=self.node_certfile)
            node.name = info['name']
            self._nodes[node.ip_addr] = node
        else:
            node.last_pulse = time.time()
            auth = auth_code(self.node_secret, info['sign'])
            if info['cpus'] > 0:
                node.avail_cpus = info['cpus']
                node.cpus = min(node.cpus, node.avail_cpus)
            else:
                logger.warning('invalid "cpus" %s from %s ignored',
                               (info['cpus'], info['ip_addr']))
            if node.port == info['port'] and node.auth == auth:
                raise StopIteration
            logger.debug('node %s rediscovered' % info['ip_addr'])
            node.port = info['port']
            if node.auth is not None:
                dead_jobs = [_job for _job in self._sched_jobs.values()
                             if _job.node is not None and _job.node.ip_addr == node.ip_addr]
                for cid in node.clusters:
                    cluster = self._clusters.get(cid, None)
                    if cluster is None:
                        continue
                    dispy_node = cluster._dispy_nodes.get(node.ip_addr, None)
                    if dispy_node:
                        dispy_node.busy = 0
                        yield self.send_node_status(cluster, dispy_node, DispyNode.Closed)

                node.clusters = set()
                node.busy = 0
                node.auth = auth
                yield self.reschedule_jobs(dead_jobs)
            node.auth = auth
        node_computations = []
        node.name = info['name']
        node.scheduler_ip_addr = info['scheduler_ip_addr']
        for cid, cluster in self._clusters.items():
            if cid in node.clusters:
                continue
            compute = cluster._compute
            for node_alloc in cluster._node_allocs:
                cpus = node_alloc.allocate(cluster, node.ip_addr, node.name, node.avail_cpus)
                if cpus > 0:
                    cluster._dispy_nodes.pop(node.ip_addr, None)
                    node_computations.append(compute)
                    break
        if node_computations:
            Coro(self.setup_node, node, node_computations)

    def send_job_result(self, uid, cluster, result, resending=False, coro=None):
        # generator
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock = AsyncSocket(sock, keyfile=self.cluster_keyfile, certfile=self.cluster_certfile)
        sock.settimeout(MsgTimeout)
        try:
            yield sock.connect((cluster.client_ip_addr, cluster.client_job_result_port))
            yield sock.send_msg(b'JOB_REPLY:' + serialize(result))
            ack = yield sock.recv_msg()
            assert ack == b'ACK'
        except:
            status = -1
            if not resending:
                # store job result even if computation has not enabled
                # fault recovery; user may be able to access node and
                # retrieve result manually
                f = os.path.join(cluster.dest_path, '_dispy_job_reply_%s' % uid)
                logger.error('Could not send reply for job %s to %s:%s; saving it in "%s"',
                             uid, cluster.client_ip_addr, cluster.client_job_result_port, f)
                try:
                    fd = open(f, 'wb')
                    pickle.dump(result, fd)
                    fd.close()
                except:
                    logger.debug('Could not save reply for job %s', uid)
                else:
                    cluster.pending_results += 1
        else:
            status = 0
            cluster.last_pulse = time.time()
            if result.status != DispyJob.ProvisionalResult:
                if resending:
                    cluster.pending_results -= 1
                    f = os.path.join(cluster.dest_path, '_dispy_job_reply_%s' % uid)
                    if os.path.isfile(f):
                        try:
                            os.remove(f)
                        except:
                            logger.warning('Could not remove "%s"' % f)
                else:
                    self.done_jobs.pop(uid, None)
                    if cluster.pending_results:
                        Coro(self.resend_job_results, cluster)
            if cluster.pending_jobs == 0 and cluster.pending_results == 0 and cluster.zombie:
                Coro(self.cleanup_computation, cluster)
        finally:
            sock.close()

        raise StopIteration(status)

    def send_job_status(self, cluster, _job, coro=None):
        if cluster.status_callback:
            dispy_node = cluster._dispy_nodes.get(_job.node.ip_addr, None)
            # assert _job.job.status == DispyJob.Running
            if dispy_node:
                dispy_node.busy += 1
                dispy_node.update_time = time.time()
                cluster.status_callback(_job.job.status, dispy_node, _job.job)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock = AsyncSocket(sock, keyfile=self.cluster_keyfile, certfile=self.cluster_certfile)
        sock.settimeout(MsgTimeout)
        try:
            yield sock.connect((cluster.client_ip_addr, cluster.client_job_result_port))
            status = {'uid': _job.uid, 'status': _job.job.status, 'node': _job.node.ip_addr,
                      'hash': _job.hash}
            status['start_time'] = _job.job.start_time
            yield sock.send_msg(b'JOB_STATUS:' + serialize(status))
        except:
            logger.warning('Could not send job status to %s:%s',
                           cluster.client_ip_addr, cluster.client_job_result_port)
        sock.close()

    def send_node_status(self, cluster, dispy_node, status, coro=None):
        if cluster.status_callback:
            dispy_node.update_time = time.time()
            cluster.status_callback(status, dispy_node, None)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock = AsyncSocket(sock, keyfile=self.cluster_keyfile, certfile=self.cluster_certfile)
        sock.settimeout(MsgTimeout)
        try:
            status = {'compute_id': cluster._compute.id, 'dispy_node': dispy_node,
                      'status': status, 'auth': cluster.client_auth}
            yield sock.connect((cluster.client_ip_addr, cluster.client_job_result_port))
            yield sock.send_msg(b'NODE_STATUS:' + serialize(status))
        except:
            logger.warning('Could not send node status to %s:%s',
                           cluster.client_ip_addr, cluster.client_job_result_port)
        sock.close()

    def job_reply_process(self, reply, sock, addr):
        _job = self._sched_jobs.get(reply.uid, None)
        if _job is None:
            logger.warning('Ignoring invalid reply for job %s from %s', reply.uid, addr[0])
            yield sock.send_msg(b'ACK')
            raise StopIteration
        job = _job.job
        node = self._nodes.get(reply.ip_addr, None)
        cluster = self._clusters.get(_job.compute_id, None)
        if cluster is None:
            # job cancelled while closing computation?
            if node:
                assert node.busy > 0
                node.busy -= 1
            yield sock.send_msg(b'ACK')
            raise StopIteration
        if node is None:
            logger.warning('Ignoring invalid reply for job %s from %s', reply.uid, addr[0])
            yield sock.send_msg(b'ACK')
            raise StopIteration
        node._jobs.discard(_job.uid)
        node.last_pulse = time.time()
        logger.debug('Received reply for job %s from %s', _job.uid, addr[0])
        try:
            assert reply.uid == _job.uid
            assert reply.hash == _job.hash
            # assert _job.job.status not in [DispyJob.Created, DispyJob.Finished]
            setattr(reply, 'cpus', node.cpus)
            # assert reply.ip_addr == node.ip_addr
        except:
            logger.warning('Invalid job result for %s from %s', _job.uid, addr[0])
            # logger.debug('%s, %s', str(reply), traceback.format_exc())
            yield sock.send_msg(b'ACK')
            raise StopIteration

        yield sock.send_msg(b'ACK')
        job.start_time = reply.start_time
        job.end_time = reply.end_time
        if reply.status != DispyJob.ProvisionalResult:
            self.done_jobs[_job.uid] = _job
            del self._sched_jobs[_job.uid]
            node.busy -= 1
            node.cpu_time += reply.end_time - reply.start_time
            if cluster.status_callback:
                dispy_node = cluster._dispy_nodes.get(_job.node.ip_addr, None)
                if dispy_node:
                    dispy_node.busy -= 1
                    dispy_node.jobs_done += 1
                    dispy_node.cpu_time += reply.end_time - reply.start_time
                    dispy_node.update_time = time.time()
                    cluster.status_callback(reply.status, dispy_node, job)

            cluster.pending_jobs -= 1
            if cluster.pending_jobs == 0:
                cluster.end_time = time.time()
                if cluster.zombie:
                    Coro(self.cleanup_computation, cluster)
            self._sched_event.set()
            for xf in _job.xfer_files:
                try:
                    os.remove(xf.name)
                except:
                    logger.warning('Could not remove "%s"' % xf.name)
        Coro(self.send_job_result, _job.uid, cluster, reply, resending=False)

    def reschedule_jobs(self, dead_jobs):
        # non-generator
        for _job in dead_jobs:
            cluster = self._clusters[_job.compute_id]
            del self._sched_jobs[_job.uid]
            _job.node._jobs.discard(_job.uid)
            if cluster._compute.reentrant:
                logger.debug('Rescheduling job %s from %s', _job.uid, _job.node.ip_addr)
                _job.job.status = DispyJob.Created
                _job.hash = ''.join(hex(x)[2:] for x in os.urandom(10))
                cluster._jobs.append(_job)
                self.unsched_jobs += 1
            else:
                logger.debug('Terminating job %s scheduled on %s', _job.uid, _job.node.ip_addr)
                reply = _JobReply(_job, _job.node.ip_addr, status=DispyJob.Abandoned)
                cluster.pending_jobs -= 1
                if cluster.pending_jobs == 0:
                    cluster.end_time = time.time()
                self.done_jobs[_job.uid] = _job
                Coro(self.send_job_result, _job.uid, cluster, reply, resending=False)

    def load_balance_schedule(self):
        # TODO: maintain "available" sequence of nodes for better performance
        host = None
        load = 1.0
        for node in self._nodes.values():
            if node.busy >= node.cpus:
                continue
            if all((not self._clusters[cid]._jobs) for cid in node.clusters):
                continue
            # logger.debug('load: %s, %s, %s' % (node.ip_addr, node.busy, node.cpus))
            if (float(node.busy) / node.cpus) < load:
                host = node
                load = float(node.busy) / node.cpus
        return host

    def run_job(self, _job, cluster, coro=None):
        # generator
        # assert coro is not None
        node = _job.node
        node._jobs.add(_job.uid)
        try:
            yield _job.run(coro=coro)
        except EnvironmentError:
            logger.warning('Failed to run job %s on %s for computation %s; removing this node',
                           _job.uid, node.ip_addr, cluster._compute.name)
            node.clusters.discard(cluster._compute.id)
            # TODO: remove the node from all clusters and globally?
            # this job might have been deleted already due to timeout
            node._jobs.discard(_job.uid)
            if self._sched_jobs.pop(_job.uid, None) == _job:
                cluster._jobs.insert(0, _job)
                self.unsched_jobs += 1
                node.busy -= 1
            self._sched_event.set()
        except:
            logger.warning('Failed to run job %s on %s for computation %s; rescheduling it',
                           _job.uid, node.ip_addr, cluster._compute.name)
            # logger.debug(traceback.format_exc())
            # TODO: delay executing again for some time?
            # this job might have been deleted already due to timeout
            node._jobs.discard(_job.uid)
            if self._sched_jobs.pop(_job.uid, None) == _job:
                cluster._jobs.append(_job)
                self.unsched_jobs += 1
                node.busy -= 1
            self._sched_event.set()
        else:
            logger.debug('Running job %s on %s (busy: %s / %s)',
                         _job.uid, node.ip_addr, node.busy, node.cpus)
            _job.job.status = DispyJob.Running
            _job.job.start_time = time.time()
            cluster = self._clusters[_job.compute_id]
            # TODO/Note: It is likely that this job status may arrive at
            # the client before the job is done and the node's status
            # arrives. Either use queing for messages (ideally with
            # asyncoro's message passing) or tag messages with timestamps
            # so recipient can use temporal ordering to ignore prior
            # messages
            Coro(self.send_job_status, cluster, _job)

    def _schedule_jobs(self, coro=None):
        # generator
        assert coro is not None
        while not self.terminate:
            # n = sum(len(cluster._jobs) for cluster in self._clusters.values())
            # assert self.unsched_jobs == n, '%s != %s' % (self.unsched_jobs, n)
            logger.debug('Pending jobs: %s', self.unsched_jobs)
            node = self.select_job_node()
            if not node:
                self._sched_event.clear()
                yield self._sched_event.wait()
                continue
            # TODO: strategy to pick a cluster?
            _job = None
            for cid in node.clusters:
                if self._clusters[cid]._jobs:
                    _job = self._clusters[cid]._jobs.pop(0)
                    break
            if _job is None:
                self._sched_event.clear()
                yield self._sched_event.wait()
                continue
            cluster = self._clusters[_job.compute_id]
            _job.node = node
            assert node.busy < node.cpus
            self._sched_jobs[_job.uid] = _job
            self.unsched_jobs -= 1
            node.busy += 1
            Coro(self.run_job, _job, cluster)

        logger.debug('scheduler quitting (%s / %s)', len(self._sched_jobs), self.unsched_jobs)
        for uid, _job in self._sched_jobs.items():
            cluster = self._clusters[_job.compute_id]
            reply = _JobReply(_job, cluster.ip_addr, status=DispyJob.Terminated)
            compute = cluster._compute
            Coro(self.send_job_result, _job.uid, cluster, reply, resending=False)
        for cid, cluster in self._clusters.items():
            for _job in cluster._jobs:
                reply = _JobReply(_job, cluster.ip_addr, status=DispyJob.Terminated)
                Coro(self.send_job_result, _job.uid, cluster, reply, resending=False)
            cluster._jobs = []
        clusters = list(self._clusters.values())
        self._clusters = {}
        self._sched_jobs = {}
        self.done_jobs = {}
        for cluster in clusters:
            compute = cluster._compute
            if compute is None:
                continue
            cluster.pending_jobs = 0
            cluster.zombie = True
            Coro(self.cleanup_computation, cluster)
        logger.debug('scheduler quit')

    def retrieve_job_task(self, conn, msg):
        # generator

        def send_reply(reply):
            try:
                yield conn.send_msg(serialize(reply))
            except:
                raise StopIteration(-1)
            raise StopIteration(0)

        try:
            req = unserialize(msg)
            uid = req['uid']
            compute_id = req['compute_id']
            auth = req['auth']
            job_hash = req['hash']
        except:
            yield send_reply(None)
            raise StopIteration

        pkl_path = os.path.join(self.dest_path_prefix, '%s_%s' % (compute_id, auth))
        cluster = self._clusters.get(compute_id, None)
        if not cluster:
            fd = open(pkl_path, 'rb')
            cluster = pickle.load(fd)
            fd.close()
        if not cluster or cluster.client_auth != auth:
            yield send_reply(None)
            raise StopIteration

        info_file = os.path.join(cluster.dest_path, '_dispy_job_reply_%s' % uid)
        if not os.path.isfile(info_file):
            yield send_reply(None)
            raise StopIteration

        try:
            fd = open(info_file, 'rb')
            job_reply = pickle.load(fd)
            fd.close()
            assert job_reply.hash == job_hash
        except:
            yield send_reply(None)
            raise StopIteration

        try:
            yield conn.send_msg(serialize(job_reply))
            ack = yield conn.recv_msg()
            assert ack == b'ACK'
            cluster.pending_results -= 1
            fd = open(pkl_path, 'wb')
            pickle.dump(cluster, fd)
            fd.close()
        except:
            pass
        else:
            try:
                os.remove(info_file)
            except:
                pass

    def cancel_job(self, cluster, uid):
        # function
        cluster.last_pulse = time.time()
        _job = self._sched_jobs.get(uid, None)
        if _job is None:
            for i, _job in enumerate(cluster._jobs):
                if _job.uid == uid:
                    del cluster._jobs[i]
                    self.unsched_jobs -= 1
                    self.done_jobs[_job.uid] = _job
                    cluster.pending_jobs -= 1
                    reply = _JobReply(_job, cluster.ip_addr, status=DispyJob.Cancelled)
                    Coro(self.send_job_result, _job.uid, cluster, reply, resending=False)
                    break
            else:
                logger.debug('Invalid job %s!', uid)
                return -1
        else:
            _job.job.status = DispyJob.Cancelled
            Coro(_job.node.send, b'TERMINATE_JOB:' + serialize(_job), reply=False)
        return 0

    def allocate_node(self, cluster, node_alloc, coro=None):
        # generator
        if not isinstance(node_alloc, list):
            node_alloc = [node_alloc]
        cluster._node_allocs.extend(node_alloc)
        cluster._node_allocs = sorted(cluster._node_allocs,
                                      key=lambda node_alloc: node_alloc.ip_rex)
        cluster._node_allocs.reverse()
        present = set()
        cluster._node_allocs = [na for na in cluster._node_allocs
                                if na.ip_rex not in present and not present.add(na.ip_rex)]
        del present
        Coro(self.add_cluster, cluster)
        yield 0

    def node_jobs(self, cluster, ip_addr, from_node=False, get_uids=True, coro=None):
        # generator
        node = self._nodes.get(ip_addr, None)
        if not node or cluster._compute.id not in node.clusters:
            raise StopIteration([])
        if from_node:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock = AsyncSocket(sock, keyfile=self.node_keyfile, certfile=self.node_certfile)
            sock.settimeout(MsgTimeout)
            try:
                yield sock.connect((node.ip_addr, node.port))
                yield sock.sendall(node.auth)
                yield sock.send_msg(b'JOBS:')
                msg = yield sock.recv_msg()
                uids = [info['uid'] for info in unserialize(msg)]
            except:
                logger.debug(traceback.format_exc())
                uids = []
            sock.close()
        else:
            uids = list(node._jobs)

        if get_uids:
            jobs = uids
        else:
            _jobs = [self._sched_jobs.get(uid, None) for uid in uids]
            jobs = [_job.job for _job in _jobs if _job is not None
                    and _job.compute_id == cluster._compute.id
                    ]
        raise StopIteration(jobs)

    def set_node_cpus(self, node, cpus, coro=None):
        # generator

        # for shared cluster, changing cpus may not be valid, as we
        # don't maintain cpus per cluster
        node = _node_ipaddr(node)
        node = self._nodes.get(node, None)
        if node is None:
            cpus = -1
        else:
            cpus = node.cpus
        yield cpus

    def shutdown(self):
        def _shutdown(self, coro=None):
            # generator
            assert coro is not None
            # TODO: send shutdown notification to clients? Or wait for all
            # pending tasks to complete?
            if self.terminate is False:
                logger.debug('shutting down scheduler ...')
                self.terminate = True
                yield self._sched_event.set()

        if self.terminate is False:
            Coro(_shutdown, self).value()
            self.scheduler_coro.value()
            # self.asyncoro.join(True)
            self.asyncoro.finish()

    def print_status(self):
        print()
        heading = ' %30s | %5s | %13s' % ('Node', 'CPUs', 'Node Time Sec')
        print(heading)
        print('-' * len(heading))
        tot_cpu_time = 0
        for ip_addr in sorted(self._nodes, key=lambda addr: self._nodes[addr].cpu_time,
                              reverse=True):
            node = self._nodes[ip_addr]
            tot_cpu_time += node.cpu_time
            if node.name:
                name = ip_addr + ' (' + node.name + ')'
            else:
                name = ip_addr
            print(' %-30.30s | %5s | %13.3f' % (name, node.cpus, node.cpu_time))
        print()
        print('Total job time: %.3f sec' % (tot_cpu_time))
        print()

if __name__ == '__main__':
    import argparse

    logger.info('dispyscheduler version %s' % _dispy_version)

    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--debug', action='store_true', dest='loglevel', default=False,
                        help='if given, debug messages are printed')
    parser.add_argument('-n', '--nodes', action='append', dest='nodes', default=[],
                        help='name or IP address used for all computations; '
                        'repeat for multiple nodes')
    parser.add_argument('-i', '--ip_addr', action='append', dest='ip_addr', default=[],
                        help='IP address to use; repeat for multiple interfaces')
    parser.add_argument('--ext_ip_addr', action='append', dest='ext_ip_addr', default=[],
                        help='External IP address to use (needed in case of NAT firewall/gateway);'
                        ' repeat for multiple interfaces')
    parser.add_argument('-p', '--port', dest='port', type=int, default=51347,
                        help='port number for UDP data and job results')
    parser.add_argument('--node_port', dest='node_port', type=int, default=51348,
                        help='port number used by nodes')
    parser.add_argument('--scheduler_port', dest='scheduler_port', type=int, default=51349,
                        help='port number for scheduler')
    parser.add_argument('--node_secret', dest='node_secret', default='',
                        help='authentication secret for handshake with dispy clients')
    parser.add_argument('--node_keyfile', dest='node_keyfile', default=None,
                        help='file containing SSL key to be used with nodes')
    parser.add_argument('--node_certfile', dest='node_certfile', default=None,
                        help='file containing SSL certificate to be used with nodes')
    parser.add_argument('--cluster_secret', dest='cluster_secret', default='',
                        help='file containing SSL certificate to be used with dispy clients')
    parser.add_argument('--cluster_certfile', dest='cluster_certfile', default=None,
                        help='file containing SSL certificate to be used with dispy clients')
    parser.add_argument('--cluster_keyfile', dest='cluster_keyfile', default=None,
                        help='file containing SSL key to be used with dispy clients')
    parser.add_argument('--pulse_interval', dest='pulse_interval', type=float, default=None,
                        help='number of seconds between pulse messages to indicate '
                        'whether node is alive')
    parser.add_argument('--ping_interval', dest='ping_interval', type=float, default=None,
                        help='number of seconds between ping messages to discover nodes')
    parser.add_argument('--zombie_interval', dest='zombie_interval', default=60, type=float,
                        help='interval in minutes to presume unresponsive scheduler is zombie')
    parser.add_argument('--msg_timeout', dest='msg_timeout', default=MsgTimeout, type=float,
                        help='timeout used for messages to/from client/nodes in seconds')
    parser.add_argument('--dest_path_prefix', dest='dest_path_prefix', default=None,
                        help='path prefix where files sent by dispy are stored')
    parser.add_argument('--max_file_size', dest='max_file_size', default=str(MaxFileSize), type=str,
                        help='maximum file size of any file transferred')
    parser.add_argument('--clean', action='store_true', dest='clean', default=False,
                        help='if given, files copied from or generated by clients '
                        'will be removed')
    parser.add_argument('--httpd', action='store_true', dest='http_server', default=False,
                        help='if given, HTTP server is created so clusters can be '
                        'monitored and managed')

    config = vars(parser.parse_args(sys.argv[1:]))
    if config['loglevel']:
        logger.setLevel(logging.DEBUG)
        asyncoro.logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
    del config['loglevel']

    if config['zombie_interval']:
        config['zombie_interval'] = float(config['zombie_interval'])
        if config['zombie_interval'] < 1:
            raise Exception('zombie_interval must be at least 1')

    MsgTimeout = config['msg_timeout']
    del config['msg_timeout']

    m = re.match(r'(\d+)([kKmMgGtT]?)', config['max_file_size'])
    if m:
        MaxFileSize = int(m.group(1))
        if m.group(2):
            m = m.group(2).lower()
            if m == 'k':
                MaxFileSize *= 1024
            elif m == 'm':
                MaxFileSize *= 1024**2
            elif m == 'g':
                MaxFileSize *= 1024**3
            elif m == 't':
                MaxFileSize *= 1024**4
            else:
                raise Exception('invalid max_file_size option')
    else:
        raise Exception('max_file_size must be >= 0')
    del config['max_file_size']

    scheduler = _Scheduler(**config)
    while True:
        try:
            # sys.stdin.readline()
            time.sleep(300)
        except KeyboardInterrupt:
            # TODO: terminate even if jobs are scheduled?
            logger.info('Interrupted; terminating')
            scheduler.shutdown()
            break
        except:
            logger.debug(traceback.format_exc())
            continue
    scheduler.print_status()
    exit(0)
