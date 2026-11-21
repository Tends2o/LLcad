"""Process limits and usage for the worker on every host.

Linux: the real `resource` module (rlimits, SIGXCPU). Windows has neither, so `resource`
is replaced by a small stand-in with the same calls: memory through a Job Object (which
also ends every child process with the worker), processor time through a watchdog thread
that raises the budget signal in the main thread at the soft limit and exits with 137
(as the kernel's SIGKILL would) at the hard limit. File-size and descriptor limits have
no Windows counterpart; the gateway checks every output's size itself."""
import os, signal, sys, threading, time

WINDOWS = os.name == 'nt'

if not WINDOWS:
    import resource
    # The processor limit is announced with SIGXCPU.
    CPU_SIGNAL = signal.SIGXCPU

    def nice(increment):
        try: os.nice(increment)
        except OSError: pass
else:
    import ctypes, _thread
    from ctypes import wintypes
    CPU_SIGNAL = signal.SIGINT  # raised in the main thread by the watchdog (_thread.interrupt_main)

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                                                      'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _BasicLimits), ('IoInfo', _IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    class _MemoryCounters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD), ('PeakWorkingSetSize', ctypes.c_size_t),
                    ('WorkingSetSize', ctypes.c_size_t), ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t), ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t), ('PagefileUsage', ctypes.c_size_t),
                    ('PeakPagefileUsage', ctypes.c_size_t)]

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _psapi = ctypes.WinDLL('psapi', use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MemoryCounters), wintypes.DWORD]
    _JOB_EXTENDED_LIMITS = 9
    _LIMIT_PROCESS_MEMORY = 0x100
    _LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x400
    _LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _BELOW_NORMAL_PRIORITY = 0x4000

    class _Usage(tuple):
        """getrusage() result: [0] user and [1] system seconds, ru_maxrss in KiB."""
        ru_maxrss = 0

    def _usage(utime, stime, maxrss):
        u = _Usage((utime, stime)); u.ru_maxrss = maxrss
        return u

    class _Resource:
        RUSAGE_SELF = 0
        RUSAGE_CHILDREN = -1
        RLIM_INFINITY = -1
        RLIMIT_CPU, RLIMIT_AS, RLIMIT_FSIZE, RLIMIT_NOFILE = range(4)

        def __init__(self):
            self._limits = {}
            self._job = None
            self._watchdog = None
            self._signalled = False

        def getrusage(self, who):
            if who != self.RUSAGE_SELF:
                return _usage(0.0, 0.0, 0)  # Windows workers never fork.
            times = os.times()
            counters = _MemoryCounters(); counters.cb = ctypes.sizeof(counters)
            _psapi.GetProcessMemoryInfo(_kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
            return _usage(times.user, times.system, counters.PeakWorkingSetSize // 1024)

        def getrlimit(self, which):
            return self._limits.get(which, (self.RLIM_INFINITY, self.RLIM_INFINITY))

        def setrlimit(self, which, limits):
            self._limits[which] = tuple(limits)
            if which == self.RLIMIT_AS:
                self._job_limits(memory=limits[0])
            elif which == self.RLIMIT_CPU and self._watchdog is None:
                self._job_limits()
                self._watchdog = threading.Thread(target=self._watch, name='cpu-budget', daemon=True)
                self._watchdog.start()

        def _job_limits(self, memory=None):
            if self._job is None:
                self._job = _kernel32.CreateJobObjectW(None, None)
                if not self._job:
                    raise OSError(ctypes.get_last_error(), 'CreateJobObject failed')
                self._memory = None
            if memory is not None:
                self._memory = memory
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = _LIMIT_KILL_ON_JOB_CLOSE | _LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            if self._memory not in (None, self.RLIM_INFINITY):
                info.BasicLimitInformation.LimitFlags |= _LIMIT_PROCESS_MEMORY
                info.ProcessMemoryLimit = self._memory
            if not _kernel32.SetInformationJobObject(self._job, _JOB_EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), 'SetInformationJobObject failed')
            # The handle stays open for the life of the worker: when it ends (or is
            # terminated by the gateway), the job closes and takes every child with it.
            _kernel32.AssignProcessToJobObject(self._job, _kernel32.GetCurrentProcess())

        def _watch(self):
            while True:
                time.sleep(0.25)
                soft, hard = self._limits.get(self.RLIMIT_CPU, (self.RLIM_INFINITY, self.RLIM_INFINITY))
                times = os.times(); used = times.user + times.system
                if hard != self.RLIM_INFINITY and used >= hard:
                    os._exit(137)
                if soft != self.RLIM_INFINITY and used >= soft and not self._signalled:
                    self._signalled = True
                    _thread.interrupt_main(CPU_SIGNAL)

    resource = _Resource()

    def nice(increment):
        # A preview rendered ahead yields the processor: below-normal priority class.
        if increment > 0:
            _kernel32.SetPriorityClass(_kernel32.GetCurrentProcess(), _BELOW_NORMAL_PRIORITY)


def available_mb():
    """Free physical memory in MiB, or None when unknown."""
    if WINDOWS:
        class _Status(ctypes.Structure):
            _fields_ = [('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD)] + \
                       [(n, ctypes.c_ulonglong) for n in ('ullTotalPhys', 'ullAvailPhys', 'ullTotalPageFile', 'ullAvailPageFile',
                                                         'ullTotalVirtual', 'ullAvailVirtual', 'ullAvailExtendedVirtual')]
        status = _Status(); status.dwLength = ctypes.sizeof(status)
        if _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys // (1024 * 1024))
        return None
    try:
        with open('/proc/meminfo') as h:
            for line in h:
                if line.startswith('MemAvailable:'): return int(line.split()[1]) // 1024
    except OSError: pass
    return None
