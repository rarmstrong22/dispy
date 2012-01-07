#!/usr/bin/env python

# dispyscheduler: Schedule jobs to nodes running 'dispynode';
# needed when multiple processes may use same nodes simultaneously
# in which case SharedJobCluster should be used;
# see accompanying 'dispy' for more details.

# Copyright (C) 2011 Giridhar Pemmasani (pgiri@yahoo.com)

# This file is part of dispy.

# dispy is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# dispy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with dispy.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import time
import socket
import inspect
import stat
import cPickle
import threading
import select
import struct
import base64
import logging
import weakref
import re
import ssl
import hashlib
import atexit
import traceback
import itertools
import Queue
import collections

from dispy import _DispySocket, _Compute, DispyJob, _DispyJob_, _Node, _JobReply, \
     MetaSingleton, _xor_string, _parse_nodes, _node_name_ipaddr

class _Scheduler(object):
    """Internal use only.
    """
    __metaclass__ = MetaSingleton

    def __init__(self, loglevel, nodes=[], ip_addr=None, port=None, node_port=None,
                 scheduler_port=None, pulse_interval=None, ping_interval=None,
                 node_secret='', node_keyfile=None, node_certfile=None,
                 cluster_secret='', cluster_keyfile=None, cluster_certfile=None):
        if not hasattr(self, 'ip_addr'):
            atexit.register(self.shutdown)
            if not loglevel:
                loglevel = logging.WARNING
            logging.basicConfig(format='%(asctime)s %(message)s', level=loglevel)
            if ip_addr:
                ip_addr = _node_name_ipaddr(ip_addr)[1]
            else:
                ip_addr = socket.gethostbyname(socket.gethostname())
            if port is None:
                port = 51347
            if not node_port:
                node_port = 51348
            if scheduler_port is None:
                scheduler_port = 51349
            if not nodes:
                nodes = ['*']

            self.ip_addr = ip_addr
            self.port = port
            self.node_port = node_port
            self.scheduler_port = scheduler_port
            self.node_spec = nodes
            self._nodes = {}
            self.node_secret = node_secret
            self.node_keyfile = node_keyfile
            self.node_certfile = node_certfile
            self.cluster_secret = cluster_secret
            self.cluster_keyfile = cluster_keyfile
            self.cluster_certfile = cluster_certfile

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

            self._clusters = {}
            self.cluster_id = 1
            self.unsched_jobs = 0
            self.job_uid = 1
            self._sched_jobs = {}
            self._sched_cv = threading.Condition()
            self._terminate_scheduler = False
            self.sign = os.urandom(20).encode('hex')
            self.auth_code = hashlib.sha1(_xor_string(self.sign, self.cluster_secret)).hexdigest()
            logging.debug('auth_code: %s', self.auth_code)

            self.worker_Q = Queue.PriorityQueue()
            self.worker_thread = threading.Thread(target=self.worker)
            self.worker_thread.daemon = True
            self.worker_thread.start()

            self.cmd_sock = _DispySocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                                         auth_code=self.auth_code)
            self.cmd_sock.bind((self.ip_addr, 0))
            self.cmd_sock.listen(2)

            #self.select_job_node = self.fast_node_schedule
            self.select_job_node = self.load_balance_schedule
            self._scheduler = threading.Thread(target=self.__schedule)
            self._scheduler.daemon = True
            self._scheduler.start()
            self.start_time = time.time()

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            bc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            bc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

            ping_request = cPickle.dumps({'scheduler_ip_addr':self.ip_addr,
                                          'scheduler_port':self.port})
            node_spec = _parse_nodes(nodes)
            for node_spec, node_info in node_spec.iteritems():
                logging.debug('Node: %s, %s', node_spec, str(node_info))
                # TODO: broadcast only if node_spec is wildcard that
                # matches local network and only in that case, or if
                # node_spec is '.*'
                if node_spec.find('*') >= 0:
                    port = node_info['port']
                    if not port:
                        port = self.node_port
                    logging.debug('Broadcasting to %s', port)
                    bc_sock.sendto('PING:%s' % ping_request, ('<broadcast>', port))
                    continue
                ip_addr = node_info['ip_addr']
                port = node_info['port']
                if not port:
                    port = self.node_port
                sock.sendto('PING:%s' % ping_request, (ip_addr, port))
            bc_sock.close()
            sock.close()

    def send_ping_cluster(self, cluster):
        ping_request = cPickle.dumps({'scheduler_ip_addr':self.ip_addr,
                                      'scheduler_port':self.port})
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for node_spec, node_info in cluster._compute.node_spec.iteritems():
            if node_spec.find('*') >= 0:
                port = node_info['port']
                if not port:
                    port = self.node_port
                bc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                bc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                bc_sock.sendto('PING:%s' % ping_request, ('<broadcast>', port))
                bc_sock.close()
            else:
                ip_addr = node_info['ip_addr']
                if ip_addr in cluster._compute.nodes:
                    continue
                port = node_info['port']
                if not port:
                    port = self.node_port
                sock.sendto('PING:%s' % ping_request, (ip_addr, port))
        sock.close()

    def add_cluster(self, cluster):
        compute = cluster._compute
        # TODO: should we allow clients to add new nodes, or use only
        # the nodes initially created with command-line?
        self.send_ping_cluster(cluster)

        self._sched_cv.acquire()
        compute_nodes = []
        for node_spec, host in compute.node_spec.iteritems():
            for ip_addr, node in self._nodes.iteritems():
                if ip_addr in compute.nodes:
                    continue
                if re.match(node_spec, ip_addr):
                    compute_nodes.append(node)
                    break
        # self._sched_cv.notify()
        self._sched_cv.release()

        for node in compute_nodes:
            if node.setup(compute):
                logging.warning('Failed to setup %s for computation "%s"',
                                node.ip_addr, compute.name)
            else:
                self._sched_cv.acquire()
                if node.ip_addr not in compute.nodes:
                    compute.nodes[node.ip_addr] = node
                    node.clusters.append(compute.id)
                    self._sched_cv.notify()
                self._sched_cv.release()

    def worker(self):
        while True:
            item = self.worker_Q.get(block=True)
            if item is None:
                break
            priority, func, args = item
            logging.debug('Calling %s', func.__name__)
            try:
                func(*args)
            except:
                logging.debug('Running %s failed: %s', func.__name__, traceback.format_exc())

    def setup_node(self, node, computes):
        # called via worker
        for compute in computes:
            if node.setup(compute):
                logging.warning('Failed to setup %s for computation "%s"',
                                node.ip_addr, compute.name)
            else:
                self._sched_cv.acquire()
                if node.ip_addr not in compute.nodes:
                    compute.nodes[node.ip_addr] = node
                    node.clusters.append(compute.id)
                    self._sched_cv.notify()
                self._sched_cv.release()

    def run_job(self, _job, cluster):
        # called via worker
        try:
            _job.run()
        except EnvironmentError:
            logging.warning('Failed to run job %s on %s for computation %s; removing this node',
                            _job.uid, _job.node.ip_addr, cluster._compute.name)
            self._sched_cv.acquire()
            if cluster.compute.nodes.pop(_job.node.ip_addr, None) is not None:
                _job.node.clusters.remove(cluster.compute.id)
                # TODO: remove the node from all clusters and globally?
            if self._sched_jobs.pop(_job.uid, None) is not None:
                # this job might have been deleted already due to timeout
                cluster._jobs.append(_job)
                self.unsched_jobs += 1
                _job.node.busy -= 1
            self._sched_cv.notify()
            self._sched_cv.release()
        except Exception:
            logging.debug(traceback.format_exc())
            logging.warning('Failed to run job %s on %s for computation %s; rescheduling it',
                            _job.uid, _job.node.ip_addr, cluster._compute.name)
            # TODO: delay executing again for some time?
            self._sched_cv.acquire()
            # this job might have been deleted already due to timeout
            if self._sched_jobs.pop(_job.uid, None) is not None:
                cluster._jobs.append(_job)
                self.unsched_jobs += 1
                _job.node.busy -= 1
            self._sched_cv.notify()
            self._sched_cv.release()

    def send_job_result(self, uid, ip, port, result):
        if port is None:
            # when a computation is closed, port is set to None so we
            # don't send reply
            logging.debug('Ignoring result for job %s', uid)
            return
        logging.debug('Sending results for %s to %s, %s', uid, ip, port)
        sock = _DispySocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        sock.settimeout(2)
        try:
            sock.connect((ip, port))
            sock.write_msg(uid, cPickle.dumps(result))
        except Exception:
            logging.warning("Couldn't send results for job %s to %s (%s)",
                            uid, ip, str(sys.exc_info()))
        sock.close()

    def terminate_jobs(self, _jobs):
        for _job in _jobs:
            try:
                _job.node.send(_job.uid, 'TERMINATE_JOB:' + cPickle.dumps(_job), reply=False)
            except:
                logging.warning('Canceling job %s failed', _job.uid)

    def close_compute_nodes(self, compute, nodes):
        for node in nodes:
            try:
                node.close(compute)
            except:
                logging.warning('Closing node %s failed', node.ip_addr)

    def run(self):
        def reschedule_jobs(dead_jobs):
            # called with _sched_cv locked
            for _job in dead_jobs:
                cluster = self._clusters[_job.compute_id]
                del self._sched_jobs[_job.uid]
                if cluster._compute.resubmit:
                    logging.debug('Rescheduling job %s from %s',
                                  _job.uid, _job.node.ip_addr)
                    _job.job.status = DispyJob.Created
                    cluster._jobs.append(_job)
                    self.unsched_jobs += 1
                else:
                    logging.debug('Terminating job %s scheduled on %s',
                                  _job.uid, _job.node.ip_addr)
                    reply = _JobReply(_job, _job.node.ip_addr, status=DispyJob.Terminated)
                    cluster._pending_jobs -= 1
                    if cluster._pending_jobs == 0:
                        cluster._complete.set()
                        cluster.end_time = time.time()
                    self.worker_Q.put((5, self.send_job_result,
                                       (_job.uid, compute.node_ip,
                                        compute.client_job_result_port, reply)))

        ping_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ping_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ping_sock.bind(('', self.port))

        job_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        job_sock.bind((self.ip_addr, 0))
        job_sock.listen(2)

        sched_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sched_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sched_sock.bind((self.ip_addr, self.scheduler_port))
        sched_sock.listen(2)

        logging.info('Ping port is %s', self.port)
        logging.info('Scheduler port is %s:%s', self.ip_addr, self.scheduler_port)
        logging.info('Job results port is %s:%s', self.ip_addr, job_sock.getsockname()[1])

        if self.pulse_interval:
            pulse_timeout = 5.0 * self.pulse_interval
        else:
            pulse_timeout = None

        if pulse_timeout and self.ping_interval:
            timeout = min(pulse_timeout, self.ping_interval)
        else:
            timeout = max(pulse_timeout, self.ping_interval)

        last_pulse_time = time.time()
        last_ping_time = last_pulse_time
        while True:
            ready = select.select([sched_sock, self.cmd_sock.sock, ping_sock, job_sock],
                                  [], [], timeout)[0]
            for sock in ready:
                if sock == job_sock:
                    conn, addr = job_sock.accept()
                    if addr[0] not in self._nodes:
                        logging.warning('Ignoring results from %s', addr[0])
                        continue
                    conn = _DispySocket(conn, certfile=self.node_certfile,
                                        keyfile=self.node_keyfile, server=True)
                    try:
                        uid, msg = conn.read_msg()
                        conn.close()
                    except:
                        logging.warning('Failed to read job results from %s: %s',
                                        str(addr), traceback.format_exc())
                        continue
                    logging.debug('Received reply for job %s from %s' % (uid, addr[0]))
                    self._sched_cv.acquire()
                    node = self._nodes.get(addr[0], None)
                    if node is None:
                        self._sched_cv.release()
                        logging.warning('Ignoring invalid reply for job %s from %s',
                                        uid, addr[0])
                        continue
                    node.last_pulse = time.time()
                    _job = self._sched_jobs.get(uid, None)
                    if _job is None:
                        self._sched_cv.release()
                        logging.warning('Ignoring invalid job %s from %s', uid, addr[0])
                        continue
                    _job.job.end_time = time.time()
                    try:
                        reply = cPickle.loads(msg)
                        assert reply.uid == _job.uid
                        assert reply.hash == _job.hash
                        setattr(reply, 'cpus', node.cpus)
                        setattr(reply, 'start_time', _job.job.start_time)
                        setattr(reply, 'end_time', _job.job.end_time)
                        if reply.status == DispyJob.ProvisionalResult:
                            logging.debug('Receveid provisional result for %s', uid)
                            self.worker_Q.put((5, self.send_job_result,
                                               (_job.uid, compute.node_ip,
                                                compute.client_job_result_port, reply)))
                            self._sched_cv.release()
                            continue
                        else:
                            del self._sched_jobs[uid]
                    except:
                        self._sched_cv.release()
                        logging.warning('Invalid job result for %s from %s', uid, addr[0])
                        logging.debug(traceback.format_exc())
                        continue

                    _job.node.busy -= 1
                    cluster = self._clusters.get(_job.compute_id, None)
                    if cluster is None:
                        self._sched_cv.release()
                        logging.warning('Invalid cluster for job %s from %s', uid, addr[0])
                        continue
                    compute = cluster._compute
                    assert compute.nodes[addr[0]] == _job.node
                    if reply.status == DispyJob.Terminated:
                        assert _job.job.status in [DispyJob.Cancelled, DispyJob.Terminated]
                        logging.debug('Terminated job: %s', _job.uid)
                    else:
                        assert reply.status == DispyJob.Finished
                        assert _job.job.status == DispyJob.Running
                        _job.node.jobs += 1
                    _job.node.cpu_time += _job.job.end_time - _job.job.start_time
                    cluster._pending_jobs -= 1
                    if cluster._pending_jobs == 0:
                        cluster._complete.set()
                        cluster.end_time = time.time()
                    self.worker_Q.put((5, self.send_job_result,
                                       (_job.uid, compute.node_ip,
                                        compute.client_job_result_port, reply)))
                    self._sched_cv.notify()
                    self._sched_cv.release()
                elif sock == sched_sock:
                    conn, addr = sched_sock.accept()
                    conn = _DispySocket(conn, certfile=self.cluster_certfile,
                                        keyfile=self.cluster_keyfile, server=True)
                    try:
                        req = conn.read(len(self.auth_code))
                        if req != self.auth_code:
                            req = conn.read(len('CLUSTER'))
                            if req == 'CLUSTER':
                                resp = cPickle.dumps({'sign':self.sign})
                                conn.write_msg(0, resp)
                            else:
                                logging.warning('Invalid/unauthorized request ignored')
                            conn.close()
                            continue
                        uid, msg = conn.read_msg()
                        if not msg:
                            logging.info('Closing connection')
                            conn.close()
                            continue
                    except:
                        logging.warning('Failed to read message from %s: %s',
                                        str(addr), traceback.format_exc())
                        conn.close()
                        continue
                    if msg.startswith('JOB:'):
                        msg = msg[len('JOB:'):]
                        try:
                            _job = cPickle.loads(msg)
                            self._sched_cv.acquire()
                            cluster = self._clusters[_job.compute_id]
                            _job.uid = self.job_uid
                            self.job_uid += 1
                            if self.job_uid == sys.maxint:
                                # TODO: check if it is okay to reset
                                self.job_uid = 1
                            setattr(_job, 'node', None)
                            job = type('DispyJob', (), {'status':DispyJob.Created,
                                                        'start_time':None, 'end_time':None})
                            setattr(_job, 'job', job)
                            cluster._jobs.append(_job)
                            self.unsched_jobs += 1
                            cluster._pending_jobs += 1
                            self._sched_cv.notify()
                            self._sched_cv.release()
                            resp = _job.uid
                        except:
                            logging.debug('Ignoring job request from %s', addr[0])
                            resp = None
                        resp = cPickle.dumps(resp)
                    elif msg.startswith('COMPUTE:'):
                        msg = msg[len('COMPUTE:'):]
                        try:
                            compute = cPickle.loads(msg)
                            compute.job_result_port = job_sock.getsockname()[1]
                            setattr(compute, 'nodes', {})
                            cluster = _Cluster(self, compute)
                            compute = cluster._compute
                            self._sched_cv.acquire()
                            compute.id = cluster.id = self.cluster_id
                            for xf in compute.xfer_files:
                                xf.compute_id = compute.id
                            self._clusters[cluster.id] = cluster
                            self.cluster_id += 1
                            self.worker_Q.put((50, self.add_cluster, (cluster,)))
                            self._sched_cv.release()
                            resp = compute.id
                            logging.debug('New computation %s: %s', compute.id, compute.name)
                        except:
                            logging.debug('Ignoring compute request from %s', addr[0])
                            resp = None
                        resp = cPickle.dumps(resp)
                    elif msg.startswith('DEL_COMPUTE:'):
                        conn.close()
                        msg = msg[len('DEL_COMPUTE:'):]
                        try:
                            compute_id = cPickle.loads(msg)
                        except:
                            logging.warning('Invalid compuation for deleting')
                            continue
                        self._sched_cv.acquire()
                        cluster = self._clusters.get(compute_id, None)
                        if cluster is None:
                            # this cluster is closed
                            self._sched_cv.release()
                            continue
                        compute = cluster._compute
                        logging.debug('Deleting computation "%s"/%s', compute.name, compute.id)
                        _jobs = [_job for _job in self._sched_jobs.itervalues() \
                                 if _job.compute_id == compute.id]
                        self.unsched_jobs -= len(cluster._jobs)
                        nodes = compute.nodes.values()
                        compute.nodes = {}
                        # set client_job_result_port to None so result is not sent to client
                        compute.client_job_result_port = None
                        compute.resubmit = False
                        cluster._jobs = []
                        del self._clusters[compute_id]
                        if _jobs:
                            self.worker_Q.put((30, self.terminate_jobs, (_jobs,)))
                        self.worker_Q.put((40, self.close_compute_nodes, (compute, nodes)))
                        self._sched_cv.release()
                        continue
                    elif msg.startswith('TERMINATE_JOB:'):
                        conn.close()
                        msg = msg[len('TERMINATE_JOB:'):]
                        try:
                            job = cPickle.loads(msg)
                        except:
                            logging.warning('Invalid job cancel message')
                            continue
                        self._sched_cv.acquire()
                        cluster = self._clusters.get(_job.compute_id, None)
                        if not cluster:
                            logging.debug('Invalid job %s!', _job.uid)
                            self._sched_cv.release()
                            continue
                        _job = self._sched_jobs.get(job.uid, None)
                        if _job is None:
                            for i, _job in enumerate(cluster._jobs):
                                if _job.uid == job.uid:
                                    del cluster._jobs[i]
                                    self.unsched_jobs -= 1
                                    reply = _JobReply(_job, self.ip_addr, status=DispyJob.Cancelled)
                                    compute = cluster._compute
                                    self.worker_Q.put((5, self.send_job_result,
                                                       (_job.uid, compute.node_ip,
                                                        compute.client_job_result_port, reply)))
                                    break
                            else:
                                logging.debug('Invalid job %s!', _job.uid)
                        else:
                            _job.job.status = DispyJob.Cancelled
                            self.worker_Q.put((30, self.terminate_jobs, ([_job],)))
                        self._sched_cv.release()
                        continue
                    if resp:
                        try:
                            conn.write_msg(0, resp)
                        except:
                            logging.warning('Failed to send response to %s: %s',
                                            str(addr), traceback.format_exc())
                    conn.close()
                elif sock == ping_sock:
                    msg, addr = ping_sock.recvfrom(1024)
                    if msg.startswith('PULSE:'):
                        msg = msg[len('PULSE:'):]
                        try:
                            info = cPickle.loads(msg)
                            node = self._nodes[info['ip_addr']]
                            assert 0 <= info['cpus'] <= node.cpus
                            node.last_pulse = time.time()
                            logging.debug('pulse from %s at %s', info['ip_addr'], node.last_pulse)
                        except:
                            logging.warning('Ignoring pulse message from %s', addr[0])
                            #logging.debug(traceback.format_exc())
                            continue
                    elif msg.startswith('PONG:'):
                        try:
                            status = cPickle.loads(msg[len('PONG:'):])
                            assert status['port'] > 0 and status['cpus'] > 0
                        except:
                            logging.debug('Ignoring node %s', addr[0])
                            continue
                        logging.debug('Discovered %s:%s with %s cpus',
                                      status['ip_addr'], status['port'], status['cpus'])
                        if status['cpus'] <= 0:
                            logging.debug('Ignoring node %s', status['ip_addr'])
                            continue
                        self._sched_cv.acquire()
                        node = self._nodes.get(status['ip_addr'], None)
                        if node is None:
                            self._sched_cv.release()
                            try:
                                node = _Node(status['ip_addr'], status['port'], status['cpus'],
                                             status['sign'], self.node_secret,
                                             self.node_keyfile, self.node_certfile)
                                data = {'ip_addr':self.ip_addr, 'port':self.port,
                                        'cpus':node.cpus, 'pulse_interval':self.pulse_interval}
                                resp = node.send(0, 'RESERVE:' + cPickle.dumps(data))
                            except:
                                logging.warning("Couldn't setup node %s; ignoring it.",
                                                status['ip_addr'])
                                logging.debug(traceback.format_exc())
                                del node
                                continue
                            if resp != 'ACK':
                                logging.warning('Ignoring node %s', status['ip_addr'])
                                del node
                                continue
                            self._sched_cv.acquire()
                            self._nodes[node.ip_addr] = node
                        else:
                            node.last_pulse = time.time()
                            h = _xor_string(status['sign'], self.node_secret)
                            h = hashlib.sha1(h).hexdigest()
                            if node.port == status['port'] and node.auth_code == h:
                                self._sched_cv.release()
                                logging.debug('Node %s is already known', node.ip_addr)
                                continue
                            node.port = status['port']
                            node.auth_code = h
                        self._nodes[node.ip_addr] = node
                        node_computes = []
                        for cid, cluster in self._clusters.iteritems():
                            compute = cluster._compute
                            if node.ip_addr in compute.nodes:
                                continue
                            for node_spec, host in compute.node_spec.iteritems():
                                if re.match(node_spec, node.ip_addr):
                                    if host['name'] is None or host['name'].find('*') >= 0:
                                        node.name = node.ip_addr
                                    else:
                                        node.name = host['name']
                                    node_computes.append(compute)
                                    break
                        self._sched_cv.release()
                        if node_computes:
                            self.worker_Q.put((10, self.setup_node, (node, node_computes)))
                    elif msg.startswith('TERMINATED:'):
                        try:
                            data = cPickle.loads(msg[len('TERMINATED:'):])
                            self._sched_cv.acquire()
                            node = self._nodes.pop(data['ip_addr'], None)
                            if not node:
                                self._sched_cv.release()
                                continue
                            logging.debug('Removing node %s', node.ip_addr)
                            h = _xor_string(data['sign'], self.node_secret)
                            auth_code = hashlib.sha1(h).hexdigest()
                            if auth_code != node.auth_code:
                                logging.warning('Invalid signature from %s', node.ip_addr)
                            dead_jobs = [_job for _job in self._sched_jobs.itervalues() \
                                         if _job.node is not None and _job.node.ip_addr == node.ip_addr]
                            reschedule_jobs(dead_jobs)
                            for cid, cluster in self._clusters.iteritems():
                                cluster._compute.nodes.pop(node.ip_addr, None)
                            self._sched_cv.release()
                            del node
                        except:
                            logging.debug('Removing node failed: %s', traceback.format_exc())
                    else:
                        logging.debug('Ignoring PONG message %s from: %s',
                                      msg[:min(5, len(msg))], addr[0])
                        continue
                elif sock == self.cmd_sock.sock:
                    logging.debug('Listener terminating ...')
                    conn, addr = self.cmd_sock.accept()
                    conn = _DispySocket(conn)
                    req = conn.read(len(self.auth_code))
                    if req != self.auth_code:
                        logging.debug('invalid auth for cmd')
                        conn.close()
                        continue
                    uid, msg = conn.read_msg()
                    conn.close()
                    if msg == 'terminate':
                        logging.debug('Terminating all running jobs')
                        self._sched_cv.acquire()
                        self._terminate_scheduler = True
                        for uid, _job in self._sched_jobs.iteritems():
                            reply = _JobReply(_job, self.ip_addr, status=DispyJob.Terminated)
                            cluster = self._clusters[_job.compute_id]
                            compute = cluster._compute
                            self.worker_Q.put((5, self.send_job_result,
                                               (_job.uid, compute.node_ip,
                                                compute.client_job_result_port, reply)))
                        self._sched_jobs = {}
                        self._sched_cv.notify()
                        self._sched_cv.release()
                        logging.debug('Listener is terminated')
                        self.cmd_sock.close()
                        self.cmd_sock = None
                        return

            if timeout:
                now = time.time()
                if pulse_timeout and (now - last_pulse_time) >= pulse_timeout:
                    last_pulse_time = now
                    self._sched_cv.acquire()
                    dead_nodes = {}
                    for node in self._nodes.itervalues():
                        if node.busy and node.last_pulse + pulse_timeout < now:
                            logging.warning('Node %s is not responding; removing it (%s, %s, %s)',
                                            node.ip_addr, node.busy, node.last_pulse, now)
                            dead_nodes[node.ip_addr] = node
                    for ip_addr in dead_nodes:
                        del self._nodes[ip_addr]
                        for cluster in self._clusters.itervalues():
                            cluster._compute.nodes.pop(ip_addr, None)
                    dead_jobs = [_job for _job in self._sched_jobs.itervalues() \
                                 if _job.node is not None and _job.node.ip_addr in dead_nodes]
                    reschedule_jobs(dead_jobs)
                    if dead_nodes or dead_jobs:
                        self._sched_cv.notify()
                    self._sched_cv.release()
                if self.ping_interval and (now - last_ping_time) >= self.ping_interval:
                    last_ping_time = now
                    self._sched_cv.acquire()
                    for cluster in self._clusters.itervalues():
                        self.send_ping_cluster(cluster)
                    self._sched_cv.release()

    def load_balance_schedule(self):
        node = None
        load = None
        for ip_addr, host in self._nodes.iteritems():
            if host.busy >= host.cpus:
                continue
            if all(not self._clusters[cluster_id]._jobs for cluster_id in host.clusters):
                continue
            logging.debug('load: %s, %s, %s' % (host.ip_addr, host.busy, host.cpus))
            if (load is None) or ((float(host.busy) / host.cpus) < load):
                node = host
                load = float(node.busy) / node.cpus
        return node

    def fast_node_schedule(self):
        # as we eagerly schedule, this has limited advantages
        # (useful only when  we have data about all the nodes and more than one node
        # is currently available)
        # in addition, we assume all jobs take equal time to execute
        node = None
        secs_per_job = None
        for ip_addr, host in self._nodes.iteritems():
            if host.busy >= host.cpus:
                continue
            if all(not self._clusters[cluster_id]._jobs for cluster_id in host.clusters):
                continue
            if (secs_per_job is None) or (host.jobs == 0) or \
                   ((host.cpu_time / host.jobs) <= secs_per_job):
                node = host
                if host.jobs == 0:
                    secs_per_job = 0
                else:
                    secs_per_job = host.cpu_time / host.jobs
        return node

    def __schedule(self):
        while True:
            self._sched_cv.acquire()
            # n = sum(len(cluster._jobs) for cluster in self._clusters.itervalues())
            # assert self.unsched_jobs == n, '%s != %s' % (self.unsched_jobs, n)
            if self._terminate_scheduler:
                self._sched_cv.release()
                break
            start_time = time.time()
            while self.unsched_jobs:
                logging.debug('Pending jobs: %s', self.unsched_jobs)
                node = self.select_job_node()
                if node is None:
                    logging.debug('No nodes/jobs')
                    break
                # TODO: strategy to pick a cluster?
                for cid in node.clusters:
                    if self._clusters[cid]._jobs:
                        _job = self._clusters[cid]._jobs.pop(0)
                        break
                else:
                    break
                cluster = self._clusters[_job.compute_id]
                compute = cluster._compute
                if _job.job.start_time == start_time:
                    logging.warning('Job %s is rescheduled too quickly; ' \
                                    'scheduler is sleeping', _job.uid)
                    cluster._jobs.append(_job)
                    break
                _job.node = node
                logging.debug('Scheduling job %s on %s (load: %.3f)',
                              _job.uid, node.ip_addr, float(node.busy) / node.cpus)
                assert node.busy < node.cpus
                # _job.ip_addr = node.ip_addr
                self._sched_jobs[_job.uid] = _job
                self.unsched_jobs -= 1
                node.busy += 1
                self.worker_Q.put((1, self.run_job, (_job, cluster)))
            self._sched_cv.wait()
            self._sched_cv.release()
        self._sched_cv.acquire()
        logging.debug('Scheduler quitting (%s / %s)',
                      len(self._sched_jobs), self.unsched_jobs)
        for cid, cluster in self._clusters.iteritems():
            compute = cluster._compute
            for node in compute.nodes.itervalues():
                node.close(compute)
            for _job in cluster._jobs:
                reply = _JobReply(_job, self.ip_addr, status=DispyJob.Terminated)
                self.worker_Q.put((5, send_job_result,
                                   (_job.uid, compute.node_ip,
                                    compute.client_job_result_port, reply)))
            cluster._jobs = []
        logging.debug('Scheduler quit')
        self._sched_cv.release()

    def shutdown(self):
        for cid, cluster in self._clusters.iteritems():
            compute = cluster._compute
            for node in compute.nodes.itervalues():
                node.close(compute)

        if self._scheduler:
            logging.debug('Shutting down scheduler ...')
            self._sched_cv.acquire()
            self._terminate_scheduler = True
            self._sched_cv.notify()
            self._sched_cv.release()
            self._scheduler.join()
            self._scheduler = None
        if self.cmd_sock:
            sock = _DispySocket(socket.socket(socket.AF_INET, socket.SOCK_STREAM),
                                auth_code=self.auth_code)
            sock.settimeout(5)
            sock.connect((self.ip_addr, self.cmd_sock.sock.getsockname()[1]))
            sock.write_msg(0, 'terminate')
            sock.close()
        self._sched_cv.acquire()
        select_job_node = self.select_job_node
        self.select_job_node = None
        self._sched_cv.release()
        if select_job_node:
            self.worker_Q.put(None)
            self.worker_thread.join()

    def stats(self):
        print
        heading = '%020s  |  %05s  |  %05s  |  %010s  |  %13s' % \
                  ('Node', 'CPUs', 'Jobs', 'Sec/Job', 'Node Time Sec')
        print heading
        print '-' * len(heading)
        tot_cpu_time = 0
        for ip_addr in sorted(self._nodes, key=lambda addr: self._nodes[addr].cpu_time,
                              reverse=True):
            node = self._nodes[ip_addr]
            if node.jobs:
                secs_per_job = node.cpu_time / node.jobs
            else:
                secs_per_job = 0
            tot_cpu_time += node.cpu_time
            print '%020s  |  %05s  |  %05s  |  %10.3f  |  %13.3f' % \
                  (node.name, node.cpus, node.jobs, secs_per_job, node.cpu_time)
        wall_time = time.time() - self.start_time
        print
        print 'Total job time: %.3f sec, wall time: %.3f sec, speedup: %.3f' % \
              (tot_cpu_time, wall_time, tot_cpu_time / wall_time)
        print

    def close(self, compute_id):
        cluster = self._clusters.get(compute_id, None)
        if compute is not None:
            for ip_addr, node in self._nodes.iteritems():
                node.close(cluster._compute)
            del self._clusters[compute_id]

class _Cluster():
    """Internal use only.
    """
    def __init__(self, scheduler, compute):
        self._compute = compute
        compute.node_spec = _parse_nodes(compute.node_spec)
        logging.debug('node_spec: %s', str(compute.node_spec))
        self._lock = threading.Lock()
        self._pending_jobs = 0
        self._jobs = []
        self._complete = threading.Event()
        self.cpu_time = 0
        self.start_time = time.time()
        self.end_time = None
        self.scheduler = scheduler

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('-d', action='store_true', dest='loglevel', default=False,
                        help='if True, debug messages are printed')
    parser.add_argument('-n', '--nodes', action='append', dest='nodes', default=[],
                        help='name or IP address used for all computations; repeat for multiple nodes')
    parser.add_argument('-i', '--ip_addr', dest='ip_addr', default=None,
                        help='IP address to use (may be needed in case of multiple interfaces)')
    parser.add_argument('-p', '--port', dest='port', type=int, default=51347,
                        help='port number to use')
    parser.add_argument('--node_port', dest='node_port', type=int, default=51348,
                        help='port number used by nodes')
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
                        help='number of seconds between pulse messages to indicate whether node is alive')
    parser.add_argument('--ping_interval', dest='ping_interval', type=float, default=None,
                        help='number of seconds between ping messages to discover nodes')

    config = vars(parser.parse_args(sys.argv[1:]))
    if config['loglevel']:
        config['loglevel'] = logging.DEBUG
    else:
        config['loglevel'] = logging.INFO

    scheduler = _Scheduler(**config)
    while True:
        try:
            scheduler.run()
        except KeyboardInterrupt:
            logging.info('Interrupted; terminating')
            scheduler.shutdown()
            break
        except:
            logging.warning(traceback.format_exc())
            logging.warning('Scheduler terminated (possibly due to an error); restarting')
            time.sleep(2)
    scheduler.stats()
    exit(0)
