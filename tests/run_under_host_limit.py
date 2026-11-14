"""Launch the real service on a host that rejects a parser isolation control."""

from pathlib import Path
import runpy
import sys

from host_limits import restrict_host


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
host_limit, *args = sys.argv[1:]
restrict_host(host_limit)
sys.argv = ["retrofit", *args]
runpy.run_module("retrofit", run_name="__main__")
