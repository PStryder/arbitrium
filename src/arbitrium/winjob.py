"""Windows 10+ owned process groups, atomically assigned at CreateProcess time.

No PID reopening, breakaway, post-launch assignment window, or uncontained fallback.
Job handles are unnamed and non-inheritable; worker death closes the last handle.
This is lifecycle containment for trusted programs, not a hostile-code sandbox.
"""
import ctypes as C
from ctypes import wintypes as W
import os
import subprocess
import time


class BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_longlong), ("job_time", C.c_longlong),
                ("flags", W.DWORD), ("min_ws", C.c_size_t), ("max_ws", C.c_size_t),
                ("active_limit", W.DWORD), ("affinity", C.c_size_t),
                ("priority", W.DWORD), ("scheduling", W.DWORD)]


class ExtendedLimits(C.Structure):
    _fields_ = [("basic", BasicLimits), ("io", C.c_ulonglong * 6),
                ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
                ("peak_process_memory", C.c_size_t), ("peak_job_memory", C.c_size_t)]


class Accounting(C.Structure):
    _fields_ = [("times", C.c_longlong * 4), ("page_faults", W.DWORD),
                ("total_processes", W.DWORD), ("active_processes", W.DWORD),
                ("terminated_processes", W.DWORD)]


class StartupInfo(C.Structure):
    _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR),
                ("title", W.LPWSTR), ("x", W.DWORD), ("y", W.DWORD),
                ("xsize", W.DWORD), ("ysize", W.DWORD), ("xchars", W.DWORD),
                ("ychars", W.DWORD), ("fill", W.DWORD), ("flags", W.DWORD),
                ("show", W.WORD), ("reserved_size", W.WORD), ("reserved_ptr", C.c_void_p),
                ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE)]


class StartupInfoEx(C.Structure):
    _fields_ = [("startup", StartupInfo), ("attributes", C.c_void_p)]


class ProcessInfo(C.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE),
                ("pid", W.DWORD), ("tid", W.DWORD)]


def _kernel():
    kernel = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([C.c_void_p, W.LPCWSTR], W.HANDLE),
        "SetInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        "QueryInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p], W.BOOL),
        "TerminateJobObject": ([W.HANDLE, W.UINT], W.BOOL),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "InitializeProcThreadAttributeList": ([C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)], W.BOOL),
        "UpdateProcThreadAttribute": ([C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p], W.BOOL),
        "DeleteProcThreadAttributeList": ([C.c_void_p], None),
        "CreateProcessW": ([W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p, W.LPCWSTR, C.c_void_p, C.POINTER(ProcessInfo)], W.BOOL),
        "GetExitCodeProcess": ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def _check(result):
    if not result:
        raise C.WinError(C.get_last_error())
    return result


class OwnedProcess:
    def __init__(self, argv, *, cwd, env):
        if os.name != "nt":
            raise OSError("Windows Job Objects required")
        import msvcrt
        self.kernel = _kernel()
        self.job = None
        self.process = None
        self.returncode = None
        self.pid = None
        attributes = None
        null_fd = None
        try:
            self.job = _check(self.kernel.CreateJobObjectW(None, None))
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _check(self.kernel.SetInformationJobObject(self.job, 9, C.byref(limits), C.sizeof(limits)))
            size = C.c_size_t()
            self.kernel.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
            if not size.value:
                raise C.WinError(C.get_last_error())
            storage = C.create_string_buffer(size.value)
            _check(self.kernel.InitializeProcThreadAttributeList(storage, 2, 0, C.byref(size)))
            attributes = storage
            jobs = (W.HANDLE * 1)(self.job)
            _check(self.kernel.UpdateProcThreadAttribute(attributes, 0, 0x2000D, jobs, C.sizeof(jobs), None, None))
            null_fd = os.open(os.devnull, os.O_RDWR)
            null_handle = msvcrt.get_osfhandle(null_fd)
            os.set_handle_inheritable(null_handle, True)
            handles = (W.HANDLE * 1)(null_handle)
            _check(self.kernel.UpdateProcThreadAttribute(attributes, 0, 0x20002, handles, C.sizeof(handles), None, None))
            startup = StartupInfoEx()
            startup.startup.cb = C.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin = startup.startup.stdout = startup.startup.stderr = null_handle
            startup.attributes = C.cast(attributes, C.c_void_p)
            info = ProcessInfo()
            command = C.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment = C.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items(), key=lambda item: item[0].upper())) + "\0")
            # JOB_LIST associates before any user code runs. Unsupported or denied
            # containment fails launch; never fall back to a naked subprocess.
            flags = 0x80000 | 0x08000000 | 0x400  # extended, no window, Unicode env
            _check(self.kernel.CreateProcessW(argv[0], command, None, None, True, flags,
                                             environment, cwd, C.byref(startup), C.byref(info)))
            self.process, self.pid = info.process, info.pid
            self.kernel.CloseHandle(info.thread)
        except BaseException:
            self.close()
            raise
        finally:
            if attributes is not None:
                self.kernel.DeleteProcThreadAttributeList(attributes)
            if null_fd is not None:
                os.close(null_fd)

    def active_count(self):
        accounting = Accounting()
        _check(self.kernel.QueryInformationJobObject(self.job, 1, C.byref(accounting), C.sizeof(accounting), None))
        return accounting.active_processes

    def poll(self):
        if self.returncode is None:
            result = self.kernel.WaitForSingleObject(self.process, 0)
            if result == 0:
                code = W.DWORD()
                _check(self.kernel.GetExitCodeProcess(self.process, C.byref(code)))
                self.returncode = code.value
            elif result != 258:  # WAIT_TIMEOUT
                raise C.WinError(C.get_last_error())
        return self.returncode

    def kill(self):
        _check(self.kernel.TerminateJobObject(self.job, 1))

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None or self.active_count():
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("managed process group", timeout)
            time.sleep(.01)
        return self.returncode

    def close(self):
        # Closing the non-inherited job handle kills any remaining descendants,
        # including when an exception prevents an orderly wait/registry update.
        for name in ("job", "process"):
            handle = getattr(self, name, None)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, name, None)

    def __del__(self):
        self.close()
