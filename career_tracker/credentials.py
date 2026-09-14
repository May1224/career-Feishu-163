"""Windows Credential Manager only. Never fall back to plaintext secrets."""
import ctypes
import os
from ctypes import wintypes
from .core import ROOT, digest

PREFIX = 'CodexCareerTracker/' + digest(str(ROOT).casefold())[:16] + '/'


class CREDENTIAL(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


def api():
    if os.name != 'nt':
        raise RuntimeError('凭据存储需要 Windows')
    dll = ctypes.WinDLL('advapi32', use_last_error=True)
    dll.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    dll.CredWriteW.restype = wintypes.BOOL
    dll.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    dll.CredReadW.restype = wintypes.BOOL
    dll.CredFree.argtypes = [ctypes.c_void_p]
    return dll


def put(name, value):
    dll = api()
    blob = value.encode('utf-16-le')
    buf = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIAL(Type=1, TargetName=PREFIX + name, CredentialBlobSize=len(blob),
                      CredentialBlob=buf, Persist=2, UserName='career-tracker')
    if not dll.CredWriteW(ctypes.byref(cred), 0):
        raise RuntimeError('Windows 凭据保存失败')


def get(name):
    dll = api()
    pointer = ctypes.POINTER(CREDENTIAL)()
    if not dll.CredReadW(PREFIX + name, 1, 0, ctypes.byref(pointer)):
        raise RuntimeError('缺少本机凭据，请运行配置窗口')
    try:
        c = pointer.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode('utf-16-le')
    finally:
        dll.CredFree(pointer)
