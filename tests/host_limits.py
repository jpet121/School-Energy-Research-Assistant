"""Install additional real seccomp restrictions inside a test process only."""

import ctypes as c


class ArgCompare(c.Structure):
    _fields_ = [("index", c.c_uint), ("operator", c.c_int), ("a", c.c_uint64), ("b", c.c_uint64)]


def restrict_host(operation):
    denied = {
        "parent-death": [("prctl", 1)],
        "no-new-privileges": [("prctl", 38)],
        "install-filter": [("prctl", 22), ("seccomp", 1)],
    }[operation]
    libc = c.CDLL(None, use_errno=True)
    assert libc.prctl(38, 1, 0, 0, 0) == 0
    seccomp = c.CDLL("libseccomp.so.2")
    seccomp.seccomp_init.argtypes = [c.c_uint32]
    seccomp.seccomp_init.restype = c.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [c.c_char_p]
    seccomp.seccomp_rule_add_array.argtypes = [c.c_void_p, c.c_uint32, c.c_int, c.c_uint, c.POINTER(ArgCompare)]
    seccomp.seccomp_load.argtypes = [c.c_void_p]
    seccomp.seccomp_release.argtypes = [c.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)
    assert context
    try:
        for syscall, value in denied:
            number = seccomp.seccomp_syscall_resolve_name(syscall.encode())
            comparison = ArgCompare(0, 4, value, 0)
            assert number >= 0 and seccomp.seccomp_rule_add_array(context, 0x00050001, number, 1, c.byref(comparison)) == 0
        assert seccomp.seccomp_load(context) == 0
    finally:
        seccomp.seccomp_release(context)
