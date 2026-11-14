import sys

from coverage import Coverage


def python_launcher():
    launcher = [sys.executable]
    if Coverage.current() is not None:
        launcher.extend(["-m", "coverage", "run", "--parallel-mode"])
    return launcher


def python_module(name, *args):
    return [*python_launcher(), "-m", name, *map(str, args)]
