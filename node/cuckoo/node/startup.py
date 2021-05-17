# Copyright (C) 2020 - 2021 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.
import logging

from cuckoo.common.startup import load_configurations
import os
import time
from threading import Thread

from cuckoo.common import config, shutdown
from cuckoo.common.log import CuckooGlobalLogger, get_global_loglevel
from cuckoo.common.packages import enumerate_plugins
from cuckoo.common.startup import StartupError
from cuckoo.common.storage import Paths, UnixSocketPaths, cuckoocwd

log = CuckooGlobalLogger(__name__)
from cuckoo.node.node import Node


def start_taskrunner():
    from cuckoo.node.taskrunner import TaskRunner
    from multiprocessing import Process

    sockpath = UnixSocketPaths.task_runner()
    if sockpath.exists():
        raise StartupError(
            f"Task runner socket path already exists: {sockpath}"
        )

    taskrunner = TaskRunner(
        sockpath, cuckoocwd.root, loglevel=get_global_loglevel()
    )
    runner_proc = Process(target=taskrunner.start)

    def _taskrunner_stopper():
        runner_proc.terminate()
        runner_proc.join(timeout=30)

    # This should be stopped as one of the first components. This way, tasks
    # that are stopped during a run can still more cleanly stop.
    shutdown.register_shutdown(_taskrunner_stopper, order=2)
    runner_proc.start()

    waited = 0
    MAXWAIT = 5
    while not os.path.exists(sockpath):
        if waited >= MAXWAIT:
            raise StartupError(
                f"Task runner was not started after {MAXWAIT} seconds."
            )

        if not runner_proc.is_alive():
            raise StartupError("Task runner stopped unexpectedly")
        waited += 0.5
        time.sleep(0.5)

def start_nodestatecontrol(node, threaded=False):
    from cuckoo.node.control import NodeTaskController
    sockpath = UnixSocketPaths.node_state_controller()
    if sockpath.exists():
        raise StartupError(
            f"Failed to start state controller: "
            f"Unix socket path already exists: {sockpath}"
        )

    state_controller = NodeTaskController(sockpath, node)
    shutdown.register_shutdown(state_controller.stop)

    if threaded:
        state_th = Thread(target=state_controller.start)
        state_th.start()
    else:
        state_controller.start()

def start_machinerymanager(nodectx):
    from cuckoo.common.machines import read_machines_dump
    from cuckoo.machineries.abstracts import Machinery
    from cuckoo.node.machinery import MachineryManager, MachineryManagerError

    all_machineries = enumerate_plugins(
        "cuckoo.machineries.modules", globals(), Machinery
    )
    enabled = config.cfg("cuckoo", "machineries")

    machinery_classes = []
    for machinery_class in all_machineries:
        if not machinery_class.name:
            continue

        if machinery_class.name.lower() in enabled:
            machinery_classes.append(machinery_class)

    # Load the machine states file if it is present.
    machine_states = {}
    dump_path = Paths.machinestates()
    if os.path.isfile(dump_path):
        machine_states = read_machines_dump(dump_path)

    sockpath = UnixSocketPaths.machinery_manager()
    if sockpath.exists():
        raise StartupError(
            f"Machinery manager socket path already exists: {sockpath}"
        )

    manager = MachineryManager(sockpath)
    nodectx.machinery_manager = manager
    shutdown.register_shutdown(manager.stop)
    shutdown.register_shutdown(manager.shutdown_all, order=999)


    try:
        manager.load_machineries(
            machinery_classes, previous_machinelist=machine_states
        )
    except MachineryManagerError as e:
        raise StartupError(f"Machinery loading failure: {e}")

    # Register the machinery stopping method as one that must be called last
    # to ensure any machines started during shutdown is still stopped.
    manager_th = Thread(target=manager.start)
    manager_th.start()

def start_resultserver():
    from cuckoo.node.resultserver import ResultServer, servers
    from multiprocessing import Process

    sockpath = UnixSocketPaths.result_server()
    if sockpath.exists():
        raise StartupError(
            "Resultserver unix socket already/still exists. "
            "Remove it there is no other Cuckoo instance running"
            f" using the specified Cuckoo CWD. {sockpath}"
        )

    ip = config.cfg("cuckoo", "resultserver", "listen_ip")
    port = config.cfg("cuckoo", "resultserver", "listen_port")
    rs = ResultServer(
        sockpath, cuckoocwd.root, ip, port, loglevel=get_global_loglevel()
    )
    log.debug(
        "Starting resultserver.", listenip=ip, listenport=port,
        sockpath=sockpath, cwd=cuckoocwd.root
    )
    rs_proc = Process(target=rs.start)

    def _rs_stopper():
        rs_proc.terminate()

    shutdown.register_shutdown(_rs_stopper)
    rs_proc.start()

    waited = 0
    MAXWAIT = 5
    while not sockpath.exists():
        if waited >= MAXWAIT:
            raise StartupError(
                f"Resultserver was not started after {MAXWAIT} seconds."
            )

        if not rs_proc.is_alive():
            raise StartupError("Resultserver stopped unexpectedly")
        waited += 0.5
        time.sleep(0.5)

    log.debug("Resultserver process started.", pid=rs_proc.pid)

    servers.add(sockpath, ip, port)

# def start_node(loglevel):
#     from multiprocessing import set_start_method
#     from cuckoo.common.startup import init_global_logging
#
#     set_start_method("spawn")
#     cuckoocwd.set(cuckoocwd.DEFAULT)
#     print("asd")
#
#     init_global_logging(loglevel, Paths.log("node.log"))
#     load_configurations()
#     n = Node()
#     start_resultserver()
#     start_machinerymanager()
#     start_taskrunner()
#     shutdown.register_shutdown(n.stop)
#     n.add_work("20210318-96A6FD_1", "windows10x64_1")
#     n.start()
#     start_statecontroller(n)

class NodeCtx:

    def __init__(self):
        self.node = None
        self.machinery_manager = None
        self.state_control = None
        self.zip_results = False

def start_local(stream_receiver):
    ctx = NodeCtx()
    start_resultserver()
    start_machinerymanager(ctx)
    start_taskrunner()
    node = Node(ctx.machinery_manager, stream_receiver)
    ctx.node = node
    shutdown.register_shutdown(node.stop)
    node.start()
    start_nodestatecontrol(ctx.node, threaded=True)
    return ctx

def start_remote(loglevel=None):
    from cuckoo.node.webapi import make_api_runner
    from multiprocessing import set_start_method
    from cuckoo.common.startup import init_global_logging
    import threading

    set_start_method("spawn")
    cuckoocwd.set(cuckoocwd.DEFAULT, analyses_dir="nodework")

    init_global_logging(loglevel or logging.DEBUG, Paths.log("node.log"))
    load_configurations()


    ctx = NodeCtx()
    ctx.zip_results = True
    start_resultserver()
    start_machinerymanager(ctx)
    start_taskrunner()

    runner = make_api_runner(ctx)
    shutdown.register_shutdown(runner.stop)
    node = Node(ctx, runner.statesse)
    shutdown.register_shutdown(node.stop)

    ctx.node = node

    node.start()

    try:
        runner.create_site()
    except OSError as e:
        raise StartupError(e)

    threading.Thread(target=runner.run_forever).start()
    start_nodestatecontrol(ctx)

# if __name__ == "__main__":
#
#     try:
#         start_remote()
#     except StartupError as e:
#         print(f"Startup failed: {e}")
#     finally:
#         shutdown.call_registered_shutdowns()
