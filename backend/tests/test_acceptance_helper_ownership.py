"""Exercise actual harness cleanup routes without importing fixture patches into pytest."""

import os
import subprocess
import sys
from pathlib import Path


def test_cleanup_routes_preserve_unowned_listener(tmp_path: Path) -> None:
    script = r"""
import os
import socket
import subprocess
import sys
import time

# A genuinely independent listener, outside the harness's ownership registry.
neighbor = subprocess.Popen(
    [sys.executable, '-u', '-c',
     'import socket,time; s=socket.socket(); '
     's.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); '
     's.bind(("127.0.0.1",0)); '
     's.listen(); print(s.getsockname()[1], flush=True); '
     '\nwhile True:\n c,a=s.accept(); c.close()'],
    stdout=subprocess.PIPE, text=True, start_new_session=True,
)
try:
    port = int(neighbor.stdout.readline())
    os.environ['ACCEPTANCE_HELPER_PORT'] = str(port)
    from acceptance import backend_harness as harness
    from fastapi.testclient import TestClient

    assert harness.FakeHelperServer().port == port
    with TestClient(harness.app, base_url="http://127.0.0.1:8000",
                    headers={"X-Lyra-Client": "acceptance-test"},
                    raise_server_exceptions=False) as client:
        for endpoint in ['helper/cleanup', 'scenario/kill-port', 'scenario/spawn-foreign']:
            response = client.post('/_acceptance/' + endpoint, json={})
            assert response.status_code == 500, (endpoint, response.status_code, response.text)
            assert neighbor.poll() is None, endpoint
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                pass

        # Once the neighbor vacates, a captured fixture on the SAME ephemeral port is
        # reclaimed by the exact same route and returns a truthful successful result.
        neighbor.kill()
        neighbor.wait(timeout=5)
        owned = harness._spawn_foreign_sync('owned-fixture')
        try:
            if not harness._wait_healthy_sync():
                detail = (
                    owned.stderr.read().decode() if owned.poll() is not None else 'still running'
                )
                raise AssertionError('owned fixture did not start: ' + detail)
            response = client.post('/_acceptance/helper/cleanup')
            assert response.status_code == 200, response.text
            assert owned.poll() is not None
        finally:
            if owned.poll() is None:
                owned.kill()
                owned.wait(timeout=5)
finally:
    if neighbor.poll() is None:
        neighbor.kill()
        neighbor.wait(timeout=5)
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        env={**os.environ, "LYRA_DATA_DIR": str(tmp_path / "isolated-harness")},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
