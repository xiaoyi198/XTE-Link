"""Current-user LAN proxy settings via Windows WinINet, without registry edits."""
import ctypes as ct
from ctypes import wintypes as wt


class Value(ct.Union):
    _fields_ = [('number', wt.DWORD), ('string', ct.c_void_p), ('time', wt.FILETIME)]


class Option(ct.Structure):
    _fields_ = [('option', wt.DWORD), ('value', Value)]


class OptionList(ct.Structure):
    _fields_ = [('size', wt.DWORD), ('connection', wt.LPWSTR), ('count', wt.DWORD),
                ('error', wt.DWORD), ('options', ct.POINTER(Option))]


def api():
    dll = ct.WinDLL('wininet', use_last_error=True)
    dll.InternetQueryOptionW.argtypes = [wt.HANDLE, wt.DWORD, ct.c_void_p, ct.POINTER(wt.DWORD)]
    dll.InternetQueryOptionW.restype = wt.BOOL
    dll.InternetSetOptionW.argtypes = [wt.HANDLE, wt.DWORD, ct.c_void_p, wt.DWORD]
    dll.InternetSetOptionW.restype = wt.BOOL
    return dll


def read():
    # INTERNET_PER_CONN_FLAGS_UI, PROXY_SERVER, PROXY_BYPASS, AUTOCONFIG_URL.
    options = (Option * 4)()
    for option, code in zip(options, (10, 2, 3, 4)):
        option.option = code
    values = OptionList(ct.sizeof(OptionList), None, 4, 0, options)
    size = wt.DWORD(ct.sizeof(values))
    free = ct.WinDLL('kernel32', use_last_error=True).GlobalFree
    free.argtypes = [ct.c_void_p]
    free.restype = ct.c_void_p
    try:
        if not api().InternetQueryOptionW(None, 75, ct.byref(values), ct.byref(size)):
            raise ct.WinError(ct.get_last_error())
        return {'flags': int(options[0].value.number),
                **{key: ct.wstring_at(option.value.string) if option.value.string else ''
                   for key, option in zip(('server', 'bypass', 'pac'), options[1:])}}
    finally:
        for option in options[1:]:
            if option.value.string:
                free(option.value.string)


def write(settings):
    if (set(settings) != {'flags', 'server', 'bypass', 'pac'} or
            type(settings['flags']) is not int or not 0 <= settings['flags'] <= 15 or
            any(not isinstance(settings[key], str) or '\0' in settings[key] or len(settings[key]) > 8192
                for key in ('server', 'bypass', 'pac'))):
        raise ValueError('invalid_proxy_settings')
    options = (Option * 4)()
    for option, code in zip(options, (1, 2, 3, 4)):
        option.option = code
    options[0].value.number = settings['flags']
    buffers = [ct.create_unicode_buffer(settings[key]) for key in ('server', 'bypass', 'pac')]
    for option, buffer in zip(options[1:], buffers):
        option.value.string = ct.cast(buffer, ct.c_void_p).value
    values = OptionList(ct.sizeof(OptionList), None, 4, 0, options)
    dll = api()
    if not dll.InternetSetOptionW(None, 75, ct.byref(values), ct.sizeof(values)):
        raise ct.WinError(ct.get_last_error())
    # Notify applications and refresh cached settings; never replay the write.
    for code in (39, 37):
        if not dll.InternetSetOptionW(None, code, None, 0):
            raise ct.WinError(ct.get_last_error())
