"""Record the production isolation self-check on a deliberately unconfined process."""

import json
from pathlib import Path
import sys

from coverage import Coverage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrofit.parser_worker import probe


if len(sys.argv) == 1:
    print(json.dumps(probe({})))
else:
    measurement = Coverage(data_file=sys.argv[1], data_suffix=True, config_file=False, branch=True, source=["retrofit"])
    measurement.set_option("run:patch", ["_exit"])
    measurement.start()
    try:
        print(json.dumps(probe({})))
    finally:
        measurement.stop()
        measurement.save()
