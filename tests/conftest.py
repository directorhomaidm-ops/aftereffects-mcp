import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aftereffects_mcp as ae_mcp  # noqa: E402


class Host:
    """A Node process running the real jsx/aemcp.jsx against tests/fake_ae.js, one per test."""

    def __init__(self):
        self.proc = subprocess.Popen(["node", str(ROOT / "tests" / "host.js")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, encoding="utf-8")
        self.scripts = []

    def _ask(self, req):
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        if "exception" in reply:
            raise RuntimeError("script raised outside aemcp.run: " + reply["exception"])
        return reply["result"]

    def run(self, script, timeout):
        self.scripts.append((script, timeout))
        return self._ask({"script": script})

    def inspect(self, expr):
        """Evaluate a JavaScript expression against the fake (`ae`), e.g. "ae.undo.length"."""
        return self._ask({"inspect": expr})

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)


@pytest.fixture
def ae(monkeypatch):
    host = Host()
    monkeypatch.setattr(ae_mcp, "RUNNER", host.run)
    yield host
    assert host.inspect("ae.undo.length") == 0, "an undo group was left open"
    host.close()
