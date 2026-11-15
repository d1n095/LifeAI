from __future__ import annotations

import os
import subprocess
import uuid

import pytest


@pytest.mark.skipif(
    os.environ.get("RUN_PARSER_CONTAINER_SECURITY_TESTS") != "1",
    reason="set RUN_PARSER_CONTAINER_SECURITY_TESTS=1 to build and exercise the isolated parser container",
)
def test_parser_container_is_secretless_networkless_and_pid_isolated() -> None:
    suffix = uuid.uuid4().hex[:12]
    image = f"lifeai-parser-boundary-test:{suffix}"
    volume = f"lifeai-parser-boundary-{suffix}"
    parser_name = f"lifeai-parser-{suffix}"
    caller_name = f"lifeai-parser-caller-{suffix}"
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))

    def docker(*args: str, capture_output: bool = True) -> str:
        result = subprocess.run(
            ["docker", *args],
            check=False,
            capture_output=capture_output,
            text=True,
            timeout=180,
        )
        if result.returncode:
            raise RuntimeError(f"docker command failed: {args!r}\n{result.stderr}")
        return result.stdout.strip()

    try:
        docker("build", "--quiet", "-t", image, backend_dir)
        docker(
            "volume",
            "create",
            "--driver",
            "local",
            "--opt",
            "type=tmpfs",
            "--opt",
            "device=tmpfs",
            "--opt",
            "o=size=1m,uid=10001,gid=10001,mode=0700",
            volume,
        )
        docker(
            "run",
            "-d",
            "--name",
            parser_name,
            "--user",
            "10001:10001",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--cpus",
            "0.5",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--volume",
            f"{volume}:/run/lifeai-parser",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "LC_ALL=C.UTF-8",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PARSER_SOCKET_PATH=/run/lifeai-parser/parser.sock",
            "--entrypoint",
            "python",
            image,
            "-m",
            "app.rag.parser_service",
        )
        docker(
            "run",
            "-d",
            "--name",
            caller_name,
            "--user",
            "10001:10001",
            "--network",
            "none",
            "--volume",
            f"{volume}:/run/lifeai-parser:ro",
            "--env",
            "PARSER_TEST_CANARY=synthetic-parent-value",
            "--entrypoint",
            "python",
            image,
            "-c",
            "import time; time.sleep(180)",
        )

        caller_host_pid = docker("inspect", "-f", "{{.State.Pid}}", caller_name)
        docker(
            "exec",
            caller_name,
            "python",
            "-c",
            "import os,stat,time; p='/run/lifeai-parser/parser.sock'; "
            "deadline=time.monotonic()+15; "
            "exec('while (not os.path.exists(p) or stat.S_IMODE(os.stat(p).st_mode)!=0o600) "
            "and time.monotonic()<deadline: time.sleep(.05)'); "
            "assert os.path.exists(p) and stat.S_IMODE(os.stat(p).st_mode)==0o600, "
            "'parser socket did not start with restricted permissions'",
        )

        environment = docker("inspect", "-f", "{{json .Config.Env}}", parser_name)
        assert "PARSER_TEST_CANARY" not in environment
        assert "DATABASE_URL" not in environment
        assert docker("inspect", "-f", "{{.HostConfig.NetworkMode}}", parser_name) == "none"
        assert docker("inspect", "-f", "{{.HostConfig.ReadonlyRootfs}}", parser_name) == "true"
        assert docker("inspect", "-f", "{{json .HostConfig.CapDrop}}", parser_name) == '["ALL"]'
        assert "no-new-privileges" in docker("inspect", "-f", "{{json .HostConfig.SecurityOpt}}", parser_name)
        assert docker("inspect", "-f", "{{.HostConfig.PidsLimit}}", parser_name) == "64"
        assert docker("inspect", "-f", "{{.HostConfig.Memory}}", parser_name) == str(256 * 1024 * 1024)
        assert docker("inspect", "-f", "{{.Config.User}}", parser_name) == "10001:10001"
        mounts = docker("inspect", "-f", "{{json .Mounts}}", parser_name)
        assert "/var/lib/lifeai/uploads" not in mounts

        socket_mode = (
            "import os,stat; d=os.stat('/run/lifeai-parser'); s=os.stat('/run/lifeai-parser/parser.sock'); "
            "assert stat.S_IMODE(d.st_mode)==0o700 and d.st_uid==10001 and d.st_gid==10001, "
            "f'dir_mode={oct(stat.S_IMODE(d.st_mode))} uid={d.st_uid} gid={d.st_gid}'; "
            "assert stat.S_IMODE(s.st_mode)==0o600 and s.st_uid==10001 and s.st_gid==10001, "
            "f'mode={oct(stat.S_IMODE(s.st_mode))} uid={s.st_uid} gid={s.st_gid}'"
        )
        docker("exec", caller_name, "python", "-c", socket_mode)

        probe = (
            "import os; "
            f"pid={int(caller_host_pid)}; "
            "assert not os.path.exists(f'/proc/{pid}'), 'caller process is visible in parser PID namespace'; "
            "assert b'synthetic-parent-value' not in open('/proc/1/environ','rb').read()"
        )
        docker("exec", parser_name, "python", "-c", probe)

        client = (
            "import httpx; "
            "transport=httpx.HTTPTransport(uds='/run/lifeai-parser/parser.sock'); "
            "client=httpx.Client(transport=transport, timeout=10); "
            "r=client.post('http://parser/v1/extract', content=b'isolated parser works', "
            "headers={'x-document-suffix':'.txt'}); "
            "assert r.status_code == 200, r.text; "
            "assert r.content == b'isolated parser works', r.content"
        )
        docker("exec", caller_name, "python", "-c", client)
    finally:
        subprocess.run(["docker", "rm", "-f", caller_name, parser_name], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)
        subprocess.run(["docker", "image", "rm", image], capture_output=True, check=False)
