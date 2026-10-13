"""Read AMD driver telemetry; no fan, clock or voltage writes.

ADL layouts/constants follow LibreHardwareMonitor v0.9.6:
https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/v0.9.6/LibreHardwareMonitorLib/Interop/AtiAdlxx.cs
AMD API: https://github.com/GPUOpen-LibrariesAndSDKs/display-library
"""

import atexit
import ctypes
import os


class AdapterInfo(ctypes.Structure):
    _fields_ = [("size", ctypes.c_int), ("index", ctypes.c_int),
                ("udid", ctypes.c_char * 256), ("bus", ctypes.c_int),
                ("device", ctypes.c_int), ("function", ctypes.c_int),
                ("vendor", ctypes.c_int), ("name", ctypes.c_char * 256),
                ("display", ctypes.c_char * 256), ("present", ctypes.c_int),
                ("exist", ctypes.c_int), ("driver", ctypes.c_char * 256),
                ("driver_ext", ctypes.c_char * 256), ("pnp", ctypes.c_char * 256),
                ("display_index", ctypes.c_int)]


class SensorData(ctypes.Structure):
    _fields_ = [("supported", ctypes.c_int), ("value", ctypes.c_int)]


class PMLog(ctypes.Structure):
    _fields_ = [("size", ctypes.c_int), ("sensors", SensorData * 256)]


def select_gpu_reading(readings):
    # ADL lists an adapter once per display. Deduplicate by PCI address first.
    unique = {address: (load, temperature) for address, load, temperature in readings
              if temperature is not None}
    # Never silently average GPUs or pair one GPU's load with another's temperature.
    if len(unique) != 1:
        return None, None
    return next(iter(unique.values()))


class AmdTelemetry:
    def __init__(self):
        self.context = ctypes.c_void_p()
        self.allocations = []
        self.adl = ctypes.CDLL(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                          "System32", "atiadlxx.dll"))
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetProcessHeap.restype = ctypes.c_void_p
        self.kernel.HeapAlloc.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_size_t]
        self.kernel.HeapAlloc.restype = ctypes.c_void_p
        self.kernel.HeapFree.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        self.heap = self.kernel.GetProcessHeap()
        callback_type = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int)
        self.callback = callback_type(self._allocate)
        self.adl.ADL2_Main_Control_Create.argtypes = [callback_type, ctypes.c_int,
                                                     ctypes.POINTER(ctypes.c_void_p)]
        self.adl.ADL2_Main_Control_Destroy.argtypes = [ctypes.c_void_p]
        self.adl.ADL2_Adapter_NumberOfAdapters_Get.argtypes = [ctypes.c_void_p,
                                                             ctypes.POINTER(ctypes.c_int)]
        self.adl.ADL2_Adapter_AdapterInfo_Get.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                        ctypes.c_int]
        self.adl.ADL2_New_QueryPMLogData_Get.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                       ctypes.POINTER(PMLog)]
        try:
            if self.adl.ADL2_Main_Control_Create(self.callback, 1, ctypes.byref(self.context)) != 0:
                raise OSError("AMD ADL initialization failed")
            count = ctypes.c_int()
            if self.adl.ADL2_Adapter_NumberOfAdapters_Get(self.context, ctypes.byref(count)) != 0:
                raise OSError("AMD adapter enumeration failed")
            if not 0 <= count.value <= 64:
                raise OSError("invalid AMD adapter count")
            self.adapters = (AdapterInfo * count.value)()
            for adapter in self.adapters:
                adapter.size = ctypes.sizeof(AdapterInfo)
            if count.value and self.adl.ADL2_Adapter_AdapterInfo_Get(
                    self.context, self.adapters, ctypes.sizeof(self.adapters)) != 0:
                raise OSError("AMD adapter information unavailable")
        except Exception:
            self.close()
            raise

    def _allocate(self, size):
        pointer = self.kernel.HeapAlloc(self.heap, 0, size)
        if pointer:
            self.allocations.append(pointer)
        return pointer

    def read(self):
        readings = []
        seen = set()
        for adapter in self.adapters:
            address = (adapter.bus, adapter.device, adapter.function)
            if address in seen or not adapter.present:
                continue
            seen.add(address)
            log = PMLog()
            log.size = ctypes.sizeof(PMLog)
            if self.adl.ADL2_New_QueryPMLogData_Get(
                    self.context, adapter.index, ctypes.byref(log)) != 0:
                continue
            # EDGE is degrees C; GFX activity is percent. Unsupported zero is not a reading.
            edge, activity = log.sensors[8], log.sensors[19]
            temperature = float(edge.value) if edge.supported and -50 <= edge.value <= 150 else None
            load = float(activity.value) if activity.supported and 0 <= activity.value <= 100 else None
            readings.append((address, load, temperature))
        return select_gpu_reading(readings)

    def close(self):
        if self.context.value:
            self.adl.ADL2_Main_Control_Destroy(self.context)
            self.context = ctypes.c_void_p()
        for pointer in self.allocations:
            self.kernel.HeapFree(self.heap, 0, pointer)
        self.allocations.clear()


_amd = None
_attempted = False


def collect_amd_gpu():
    global _amd, _attempted
    if os.name != "nt":
        return None, None
    if not _attempted:
        _attempted = True
        try:
            _amd = AmdTelemetry()
            atexit.register(_amd.close)
        except (OSError, AttributeError):
            return None, None
    return _amd.read() if _amd is not None else (None, None)
