"""Measure the parser CLI without granting its sandbox filesystem access."""

import os
from pathlib import Path
import runpy
import sqlite3.dump  # Load the stdlib serializer before seccomp prevents imports.
import sys

from coverage import Coverage

from host_limits import restrict_host

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
descriptor, host_limit, *args = sys.argv[1:]
sys.argv = ["retrofit.parser_worker", *args]
if host_limit != "none":
    restrict_host(host_limit)
if descriptor == "-1":
    runpy.run_module("retrofit.parser_worker", run_name="__main__")
else:
    measurement = Coverage(data_file=None, config_file=False, branch=True, source=["retrofit"])
    measurement.start()
    try:
        runpy.run_module("retrofit.parser_worker", run_name="__main__")
    finally:
        measurement.stop()
        os.write(int(descriptor), measurement.get_data().dumps())
