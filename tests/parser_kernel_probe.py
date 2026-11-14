"""Exercise the production kernel limits in a separate fixture process."""

import errno
import mmap
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrofit.parser_worker import MEMORY_BYTES, install_filter, isolate


mode, parent = sys.argv[1:]
if mode in {"invalid-action", "unknown-syscall"}:
    try:
        if mode == "invalid-action":
            install_filter(0xDEADBEEF, ("read",))
        else:
            install_filter(0x00050001, ("not_a_real_syscall",))
    except RuntimeError as exc:
        print(exc)
        sys.exit(0)
    raise AssertionError("libseccomp unexpectedly accepted an invalid policy")
isolate(int(parent))
if mode == "address-space":
    with mmap.mmap(-1, 1024 * 1024) as below_limit:
        below_limit[0:4] = b"test"
        assert below_limit[0:4] == b"test"
    try:
        with mmap.mmap(-1, MEMORY_BYTES + 4096):
            raise AssertionError("The kernel allowed mapping beyond the address-space limit.")
    except OSError as exc:
        assert exc.errno == errno.ENOMEM
        os.write(1, b"below-limit mapping succeeded; over-limit mapping denied\n")
elif mode == "cpu":
    os.write(1, b"running under parser CPU limit\n")
    while True:
        pass
else:
    raise ValueError("Unknown kernel fixture")
