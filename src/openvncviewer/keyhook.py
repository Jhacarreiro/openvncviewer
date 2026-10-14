# OpenVNCViewer - a VNC viewer for macOS Screen Sharing with window scaling.
# Copyright (C) 2026 The OpenVNCViewer contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.
"""Send the Alt shortcuts Windows keeps for itself to the remote instead.

Alt+Tab, Alt+Esc, Alt+Space and Alt+F4 are acted on by Windows before any
window sees the key, so no amount of claiming events in Qt can reach them. A
low-level keyboard hook sees them first: while the remote view has focus it
swallows the key and hands it to the session.

Alt itself is not taken. It still reaches the view the ordinary way and goes
out as Alt or Command, so the remote sees the whole chord - Alt+Tab, or
Command+Tab on a Mac. Ctrl+Alt+Del cannot be hooked at all; Windows reserves
it.
"""

import sys

# Virtual-key code -> X11 keysym, for the keys Windows takes when Alt is down.
VK_TAB, VK_ESCAPE, VK_SPACE, VK_F4 = 0x09, 0x1B, 0x20, 0x73
SYSTEM_KEYS = {VK_TAB: 0xFF09, VK_ESCAPE: 0xFF1B, VK_SPACE: 0x20, VK_F4: 0xFFC1}

WH_KEYBOARD_LL = 13
WM_KEYUP, WM_SYSKEYUP = 0x0101, 0x0105
LLKHF_ALTDOWN = 0x20


class SystemKeyFilter:
    """Decides which key events to take from Windows. Free of any Windows API.

    A key taken on the way down must be taken on the way up as well, even if
    Alt was let go first or focus has moved since. Otherwise the remote holds
    Tab down forever, and Windows sees a release for a press it never had.
    """

    def __init__(self):
        self._taken = set()

    def take(self, vk, down, alt_down, active):
        """The keysym to send instead of letting Windows have the key, or None."""
        keysym = SYSTEM_KEYS.get(vk)
        if keysym is None:
            return None
        if down:
            if active and alt_down:
                self._taken.add(vk)
                return keysym
            return None
        if vk in self._taken:
            self._taken.discard(vk)
            return keysym
        return None


class KeyboardHook:
    """A WH_KEYBOARD_LL hook, installed only while it is wanted.

    The hook procedure runs on the thread that installed it, inside that
    thread's message loop - here Qt's, on the GUI thread - so `active` and
    `send_key` may touch Qt and the session directly. Windows drops a hook
    that is slow to answer, so neither should block.
    """

    def __init__(self, active, send_key):
        self._active = active
        self._send_key = send_key
        self._filter = SystemKeyFilter()
        self._handle = None
        self._procedure = None  # kept alive: Windows holds only a raw pointer

    @property
    def installed(self):
        return self._handle is not None

    def install(self):
        """Start taking keys. Reports whether the hook is in place."""
        if self._handle is not None:
            return True
        if sys.platform != "win32":
            return False
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class KeyInfo(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        # Spelled out: ctypes' default int would truncate 64-bit handles and
        # pointers.
        procedure_type = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int,
                                            wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, procedure_type,
                                             wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = wintypes.LPARAM
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        def procedure(code, message, info):
            if code == 0:  # HC_ACTION
                try:
                    key = ctypes.cast(info, ctypes.POINTER(KeyInfo)).contents
                    down = message not in (WM_KEYUP, WM_SYSKEYUP)
                    keysym = self._filter.take(
                        key.vkCode, down, bool(key.flags & LLKHF_ALTDOWN),
                        self._active())
                    if keysym is not None:
                        self._send_key(keysym, down)
                        return 1  # swallowed: Windows never sees it
                except Exception:
                    # An exception here would reach Windows as garbage. Let
                    # the key through rather than lose it.
                    pass
            return user32.CallNextHookEx(None, code, message, info)

        self._procedure = procedure_type(procedure)
        self._user32 = user32
        self._handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._procedure, kernel32.GetModuleHandleW(None), 0)
        if not self._handle:
            self._procedure = None
            return False
        return True

    def uninstall(self):
        if self._handle is not None:
            self._user32.UnhookWindowsHookEx(self._handle)
            self._handle = None
            self._procedure = None
