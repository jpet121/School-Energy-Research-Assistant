from importlib import import_module
import os
from pathlib import Path, PureWindowsPath
import subprocess
from tempfile import TemporaryDirectory

from coverage import Coverage, CoverageData
import pytest


def test_launcher_can_be_imported_without_starting_a_server():
    launcher = import_module("retrofit.__main__")
    assert callable(launcher.main)


def test_native_windows_launch_fails_before_importing_linux_runtime():
    python = os.environ.get("RETROFIT_WINDOWS_PYTHON")
    if not python:
        pytest.skip("Set RETROFIT_WINDOWS_PYTHON to exercise the real native Windows launcher.")
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        scratch = Path(directory)
        windows_path = subprocess.run(
            ["wslpath", "-w", directory], capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
        launch = [python]
        measurement = Coverage.current()
        if measurement is not None:
            launch.extend(["-m", "coverage", "run", f"--data-file={windows_path}\\launcher"])
        result = subprocess.run(
            [*launch, "-m", "retrofit", "--data", f"{windows_path}\\unsupported-data"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        assert result.returncode == 2 and "supported Linux parser-isolation runtime" in result.stderr
        assert "Traceback" not in result.stderr and not (scratch / "unsupported-data").exists()
        if measurement is not None:
            recorded = list(scratch.glob("launcher*"))
            assert len(recorded) == 1
            data = CoverageData(basename=str(recorded[0]))
            data.read()
            source = next(
                name for name in data.measured_files()
                if PureWindowsPath(name).parts[-2:] == ("retrofit", "__main__.py")
            )
            arcs = data.arcs(source)
            assert arcs
            measurement.get_data().add_arcs({str(Path.cwd() / "retrofit" / "__main__.py"): arcs})


def test_native_windows_store_preserves_original_across_reopen():
    python = os.environ.get("RETROFIT_WINDOWS_PYTHON")
    if not python:
        pytest.skip("Set RETROFIT_WINDOWS_PYTHON to exercise real native filesystem persistence.")
    with TemporaryDirectory(dir=Path.cwd()) as directory:
        scratch = Path(directory)
        windows_path = subprocess.run(
            ["wslpath", "-w", directory], capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
        launch = [python]
        measurement = Coverage.current()
        if measurement is not None:
            launch.extend(["-m", "coverage", "run", f"--data-file={windows_path}\\store"])
        result = subprocess.run(
            [*launch, "tests/native_store_probe.py", f"{windows_path}\\data"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "Native store reopened the identical content and intake identity."
        if measurement is not None:
            recorded = list(scratch.glob("store*"))
            assert len(recorded) == 1
            data = CoverageData(basename=str(recorded[0]))
            data.read()
            for name in data.measured_files():
                if PureWindowsPath(name).parts[-2:] == ("retrofit", "store.py"):
                    arcs = data.arcs(name)
                    assert arcs
                    measurement.get_data().add_arcs({str(Path.cwd() / "retrofit" / "store.py"): arcs})
                    break
            else:
                pytest.fail("The native SQLite store produced no measured source file.")
