import asyncio

import pytest
from click.testing import CliRunner

pytest.importorskip("requests")

import os
from multiprocessing import cpu_count
from time import sleep

import requests

from dask.utils import tmpfile

import distributed.cli.dask_worker
from distributed import Client, Scheduler
from distributed.compatibility import LINUX
from distributed.deploy.utils import nprocesses_nthreads
from distributed.metrics import time
from distributed.utils import parse_ports, sync
from distributed.utils_test import (
    gen_cluster,
    popen,
    requires_ipv6,
    terminate_process,
    wait_for_port,
)


def test_nanny_worker_ports(loop):
    with popen(["dask-scheduler", "--port", "9359", "--no-dashboard"]):
        with popen(
            [
                "dask-worker",
                "127.0.0.1:9359",
                "--host",
                "127.0.0.1",
                "--worker-port",
                "9684",
                "--nanny-port",
                "5273",
                "--no-dashboard",
            ]
        ):
            with Client("127.0.0.1:9359", loop=loop) as c:
                start = time()
                while True:
                    d = sync(c.loop, c.scheduler.identity)
                    if d["workers"]:
                        break
                    else:
                        assert time() - start < 60
                        sleep(0.1)
                assert (
                    d["workers"]["tcp://127.0.0.1:9684"]["nanny"]
                    == "tcp://127.0.0.1:5273"
                )


@pytest.mark.slow
def test_nanny_worker_port_range(loop):
    with popen(["dask-scheduler", "--port", "9359", "--no-dashboard"]) as sched:
        nprocs = 3
        worker_port = "9684:9686"
        nanny_port = "9688:9690"
        with popen(
            [
                "dask-worker",
                "127.0.0.1:9359",
                "--nprocs",
                f"{nprocs}",
                "--host",
                "127.0.0.1",
                "--worker-port",
                worker_port,
                "--nanny-port",
                nanny_port,
                "--no-dashboard",
            ]
        ):
            with Client("127.0.0.1:9359", loop=loop) as c:
                start = time()
                while len(c.scheduler_info()["workers"]) < nprocs:
                    sleep(0.1)
                    assert time() - start < 60

                def get_port(dask_worker):
                    return dask_worker.port

                expected_worker_ports = set(parse_ports(worker_port))
                worker_ports = c.run(get_port)
                assert set(worker_ports.values()) == expected_worker_ports

                expected_nanny_ports = set(parse_ports(nanny_port))
                nanny_ports = c.run(get_port, nanny=True)
                assert set(nanny_ports.values()) == expected_nanny_ports


def test_nanny_worker_port_range_too_many_workers_raises(loop):
    with popen(["dask-scheduler", "--port", "9359", "--no-dashboard"]):
        with popen(
            [
                "dask-worker",
                "127.0.0.1:9359",
                "--nprocs",
                "3",
                "--host",
                "127.0.0.1",
                "--worker-port",
                "9684:9685",
                "--nanny-port",
                "9686:9687",
                "--no-dashboard",
            ]
        ) as worker:
            assert any(
                b"Could not start" in worker.stderr.readline() for _ in range(100)
            )


def test_memory_limit(loop):
    with popen(["dask-scheduler", "--no-dashboard"]):
        with popen(
            [
                "dask-worker",
                "127.0.0.1:8786",
                "--memory-limit",
                "2e3MB",
                "--no-dashboard",
            ]
        ):
            with Client("127.0.0.1:8786", loop=loop) as c:
                while not c.nthreads():
                    sleep(0.1)
                info = c.scheduler_info()
                [d] = info["workers"].values()
                assert isinstance(d["memory_limit"], int)
                assert d["memory_limit"] == 2e9


def test_no_nanny(loop):
    with popen(["dask-scheduler", "--no-dashboard"]):
        with popen(
            ["dask-worker", "127.0.0.1:8786", "--no-nanny", "--no-dashboard"]
        ) as worker:
            assert any(b"Registered" in worker.stderr.readline() for i in range(15))


@pytest.mark.slow
@pytest.mark.parametrize("nanny", ["--nanny", "--no-nanny"])
def test_no_reconnect(nanny, loop):
    with popen(["dask-scheduler", "--no-dashboard"]) as sched:
        wait_for_port(("127.0.0.1", 8786))
        with popen(
            [
                "dask-worker",
                "tcp://127.0.0.1:8786",
                "--no-reconnect",
                nanny,
                "--no-dashboard",
            ]
        ) as worker:
            sleep(2)
            terminate_process(sched)
        start = time()
        while worker.poll() is None:
            sleep(0.1)
            assert time() < start + 30


def test_resources(loop):
    with popen(["dask-scheduler", "--no-dashboard"]):
        with popen(
            [
                "dask-worker",
                "tcp://127.0.0.1:8786",
                "--no-dashboard",
                "--resources",
                "A=1 B=2,C=3",
            ]
        ):
            with Client("127.0.0.1:8786", loop=loop) as c:
                while not c.scheduler_info()["workers"]:
                    sleep(0.1)
                info = c.scheduler_info()
                worker = list(info["workers"].values())[0]
                assert worker["resources"] == {"A": 1, "B": 2, "C": 3}


@pytest.mark.parametrize("nanny", ["--nanny", "--no-nanny"])
def test_local_directory(loop, nanny):
    with tmpfile() as fn:
        with popen(["dask-scheduler", "--no-dashboard"]):
            with popen(
                [
                    "dask-worker",
                    "127.0.0.1:8786",
                    nanny,
                    "--no-dashboard",
                    "--local-directory",
                    fn,
                ]
            ):
                with Client("127.0.0.1:8786", loop=loop, timeout=10) as c:
                    start = time()
                    while not c.scheduler_info()["workers"]:
                        sleep(0.1)
                        assert time() < start + 8
                    info = c.scheduler_info()
                    worker = list(info["workers"].values())[0]
                    assert worker["local_directory"].startswith(fn)


@pytest.mark.parametrize("nanny", ["--nanny", "--no-nanny"])
def test_scheduler_file(loop, nanny):
    with tmpfile() as fn:
        with popen(["dask-scheduler", "--no-dashboard", "--scheduler-file", fn]):
            with popen(
                ["dask-worker", "--scheduler-file", fn, nanny, "--no-dashboard"]
            ):
                with Client(scheduler_file=fn, loop=loop) as c:
                    start = time()
                    while not c.scheduler_info()["workers"]:
                        sleep(0.1)
                        assert time() < start + 10


def test_scheduler_address_env(loop, monkeypatch):
    monkeypatch.setenv("DASK_SCHEDULER_ADDRESS", "tcp://127.0.0.1:8786")
    with popen(["dask-scheduler", "--no-dashboard"]):
        with popen(["dask-worker", "--no-dashboard"]):
            with Client(os.environ["DASK_SCHEDULER_ADDRESS"], loop=loop) as c:
                start = time()
                while not c.scheduler_info()["workers"]:
                    sleep(0.1)
                    assert time() < start + 10


def test_nprocs_requires_nanny(loop):
    with popen(["dask-scheduler", "--no-dashboard"]):
        with popen(
            ["dask-worker", "127.0.0.1:8786", "--nprocs=2", "--no-nanny"]
        ) as worker:
            assert any(
                b"Failed to launch worker" in worker.stderr.readline()
                for i in range(15)
            )


def test_nprocs_negative(loop):
    with popen(["dask-scheduler", "--no-dashboard"]):
        with popen(["dask-worker", "127.0.0.1:8786", "--nprocs=-1"]):
            with Client("tcp://127.0.0.1:8786", loop=loop) as c:
                c.wait_for_workers(cpu_count(), timeout="10 seconds")


def test_nprocs_auto(loop):
    with popen(["dask-scheduler", "--no-dashboard"]):
        with popen(["dask-worker", "127.0.0.1:8786", "--nprocs=auto"]):
            with Client("tcp://127.0.0.1:8786", loop=loop) as c:
                procs, _ = nprocesses_nthreads()
                c.wait_for_workers(procs, timeout="10 seconds")


def test_nprocs_expands_name(loop):
    with popen(["dask-scheduler", "--no-dashboard"]):
        with popen(["dask-worker", "127.0.0.1:8786", "--nprocs", "2", "--name", "0"]):
            with popen(["dask-worker", "127.0.0.1:8786", "--nprocs", "2"]):
                with Client("tcp://127.0.0.1:8786", loop=loop) as c:
                    start = time()
                    while len(c.scheduler_info()["workers"]) < 4:
                        sleep(0.2)
                        assert time() < start + 30

                    info = c.scheduler_info()
                    names = [d["name"] for d in info["workers"].values()]
                    foos = [n for n in names if n.startswith("0-")]
                    assert len(foos) == 2
                    assert len(set(names)) == 4


@pytest.mark.skipif(not LINUX, reason="Need 127.0.0.2 to mean localhost")
@pytest.mark.parametrize("nanny", ["--nanny", "--no-nanny"])
@pytest.mark.parametrize(
    "listen_address", ["tcp://0.0.0.0:39837", "tcp://127.0.0.2:39837"]
)
def test_contact_listen_address(loop, nanny, listen_address):
    with popen(["dask-scheduler", "--no-dashboard"]):
        with popen(
            [
                "dask-worker",
                "127.0.0.1:8786",
                nanny,
                "--no-dashboard",
                "--contact-address",
                "tcp://127.0.0.2:39837",
                "--listen-address",
                listen_address,
            ]
        ):
            with Client("127.0.0.1:8786") as client:
                while not client.nthreads():
                    sleep(0.1)
                info = client.scheduler_info()
                assert "tcp://127.0.0.2:39837" in info["workers"]

                # roundtrip works
                assert client.submit(lambda x: x + 1, 10).result() == 11

                def func(dask_worker):
                    return dask_worker.listener.listen_address

                assert client.run(func) == {"tcp://127.0.0.2:39837": listen_address}


@requires_ipv6
@pytest.mark.parametrize("nanny", ["--nanny", "--no-nanny"])
@pytest.mark.parametrize("listen_address", ["tcp://:39838", "tcp://[::1]:39838"])
def test_listen_address_ipv6(loop, nanny, listen_address):
    with popen(["dask-scheduler", "--no-dashboard"]):
        with popen(
            [
                "dask-worker",
                "127.0.0.1:8786",
                nanny,
                "--no-dashboard",
                "--listen-address",
                listen_address,
            ]
        ):
            # IPv4 used by default for name of global listener; IPv6 used by default when
            # listening only on IPv6.
            bind_all = "[::1]" not in listen_address
            expected_ip = "127.0.0.1" if bind_all else "[::1]"
            expected_name = f"tcp://{expected_ip}:39838"
            expected_listen = "tcp://0.0.0.0:39838" if bind_all else listen_address
            with Client("127.0.0.1:8786") as client:
                while not client.nthreads():
                    sleep(0.1)
                info = client.scheduler_info()
                assert expected_name in info["workers"]
                assert client.submit(lambda x: x + 1, 10).result() == 11

                def func(dask_worker):
                    return dask_worker.listener.listen_address

                assert client.run(func) == {expected_name: expected_listen}


@pytest.mark.skipif(not LINUX, reason="Need 127.0.0.2 to mean localhost")
@pytest.mark.parametrize("nanny", ["--nanny", "--no-nanny"])
@pytest.mark.parametrize("host", ["127.0.0.2", "0.0.0.0"])
def test_respect_host_listen_address(loop, nanny, host):
    with popen(["dask-scheduler", "--no-dashboard"]):
        with popen(
            ["dask-worker", "127.0.0.1:8786", nanny, "--no-dashboard", "--host", host]
        ) as worker:
            with Client("127.0.0.1:8786") as client:
                while not client.nthreads():
                    sleep(0.1)
                client.scheduler_info()

                # roundtrip works
                assert client.submit(lambda x: x + 1, 10).result() == 11

                def func(dask_worker):
                    return dask_worker.listener.listen_address

                listen_addresses = client.run(func)
                assert all(host in v for v in listen_addresses.values())


def test_dashboard_non_standard_ports(loop):
    pytest.importorskip("bokeh")
    try:
        import jupyter_server_proxy  # noqa: F401

        proxy_exists = True
    except ImportError:
        proxy_exists = False

    with popen(["dask-scheduler", "--port", "3449"]):
        with popen(
            [
                "dask-worker",
                "tcp://127.0.0.1:3449",
                "--dashboard-address",
                ":4833",
                "--host",
                "127.0.0.1",
            ]
        ):
            with Client("127.0.0.1:3449", loop=loop) as c:
                c.wait_for_workers(1)
                pass

                response = requests.get("http://127.0.0.1:4833/status")
                assert response.ok
                redirect_resp = requests.get("http://127.0.0.1:4833/main")
                redirect_resp.ok
                # TEST PROXYING WORKS
                if proxy_exists:
                    url = "http://127.0.0.1:8787/proxy/4833/127.0.0.1/status"
                    response = requests.get(url)
                    assert response.ok

        with pytest.raises(Exception):
            requests.get("http://localhost:4833/status/")


def test_version_option():
    runner = CliRunner()
    result = runner.invoke(distributed.cli.dask_worker.main, ["--version"])
    assert result.exit_code == 0


@pytest.mark.slow
@pytest.mark.parametrize("no_nanny", [True, False])
def test_worker_timeout(no_nanny):
    runner = CliRunner()
    args = ["192.168.1.100:7777", "--death-timeout=1"]
    if no_nanny:
        args.append("--no-nanny")
    result = runner.invoke(distributed.cli.dask_worker.main, args)
    assert result.exit_code != 0


def test_bokeh_deprecation():
    pytest.importorskip("bokeh")

    runner = CliRunner()
    with pytest.warns(UserWarning, match="dashboard"):
        try:
            runner.invoke(distributed.cli.dask_worker.main, ["--bokeh"])
        except ValueError:
            # didn't pass scheduler
            pass

    with pytest.warns(UserWarning, match="dashboard"):
        try:
            runner.invoke(distributed.cli.dask_worker.main, ["--no-bokeh"])
        except ValueError:
            # didn't pass scheduler
            pass


@gen_cluster(nthreads=[])
async def test_integer_names(s):
    with popen(["dask-worker", s.address, "--name", "123"]):
        while not s.workers:
            await asyncio.sleep(0.01)
        [ws] = s.workers.values()
        assert ws.name == 123


@pytest.mark.asyncio
@pytest.mark.parametrize("nanny", ["--nanny", "--no-nanny"])
async def test_worker_class(cleanup, tmp_path, nanny):
    # Create module with custom worker class
    WORKER_CLASS_TEXT = """
from distributed.worker import Worker

class MyWorker(Worker):
    pass
"""
    tmpdir = str(tmp_path)
    tmpfile = str(tmp_path / "myworker.py")
    with open(tmpfile, "w") as f:
        f.write(WORKER_CLASS_TEXT)

    # Put module on PYTHONPATH
    env = os.environ.copy()
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = tmpdir + ":" + env["PYTHONPATH"]
    else:
        env["PYTHONPATH"] = tmpdir

    async with Scheduler(dashboard_address=":0") as s:
        async with Client(s.address, asynchronous=True) as c:
            with popen(
                [
                    "dask-worker",
                    s.address,
                    nanny,
                    "--worker-class",
                    "myworker.MyWorker",
                ],
                env=env,
            ):
                await c.wait_for_workers(1)

                def worker_type(dask_worker):
                    return type(dask_worker).__name__

                worker_types = await c.run(worker_type)
                assert all(name == "MyWorker" for name in worker_types.values())


@gen_cluster(nthreads=[], client=True)
async def test_preload_config(c, s):
    # Ensure dask-worker pulls the preload from the Dask config if
    # not specified via a command line option
    preload_text = """
def dask_setup(worker):
    worker.foo = 'setup'
"""
    env = os.environ.copy()
    env["DASK_DISTRIBUTED__WORKER__PRELOAD"] = preload_text
    with popen(
        [
            "dask-worker",
            s.address,
        ],
        env=env,
    ):
        await c.wait_for_workers(1)
        [foo] = (await c.run(lambda dask_worker: dask_worker.foo)).values()
        assert foo == "setup"
