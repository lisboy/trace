"""桌面原生交互：用「系统文件管理器那种」窗口选文件夹。

为什么要这个（而不是前端自绘目录树）：
  · 这是个跑在用户自己电脑上的单机应用，选目录就该是资源管理器那一套——左侧快速访问、
    地址栏能直接粘贴路径、能进别的盘/网络位置/U盘；
  · 自绘目录树只能看到「服务进程读得到的路径」，映射盘、权限差异、新建文件夹、
    路径补全全都要自己重做一遍，做出来还只是像；
  · 用户原话就是「选择文件夹用电脑文件管理器那种打开选择」。

实现：Windows 走 COM 的 IFileOpenDialog + FOS_PICKFOLDERS —— Vista 起系统标准的
「选择文件夹」对话框，和资源管理器同款；不在 Windows / 拿不到 COM 时回退 tkinter 的
askdirectory；再不行抛异常，由上层（storage 路由）告诉前端「用内置目录浏览器兜底」。

调用约定：**必须在线程里调用**（COM 对话框是模态阻塞的，会一直卡到用户点确定/取消）。
"""
from __future__ import annotations

import os
import sys
import threading

# 同一时刻只允许一个选择窗口：连点两下会叠出两个模态框，用户点掉上面那个
# 还以为没反应（下面那个被挡住），体验上就是「点了没反应」。
_PICK_LOCK = threading.Lock()


class PickerBusy(RuntimeError):
    """已经有一个选择窗口开着。"""


class PickerUnavailable(RuntimeError):
    """这台机器上弹不出原生窗口（无桌面 / 无 tkinter）。"""


# --------------------------------------------------------------- Windows 原生
_CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
_IID_IFileOpenDialog = "{D57C7288-D4AD-4768-BE02-9D969532D960}"

_FOS_PICKFOLDERS = 0x00000020
_FOS_FORCEFILESYSTEM = 0x00000040
_FOS_PATHMUSTEXIST = 0x00000800
_FOS_DONTADDTORECENT = 0x02000000
_SIGDN_FILESYSPATH = 0x80058000
_CLSCTX_INPROC_SERVER = 1
_ERROR_CANCELLED = -2147023673  # HRESULT_FROM_WIN32(1222) 用户点了取消

# IFileDialog / IFileOpenDialog 的 vtable 下标（IUnknown 占 0..2）
_VT_RELEASE = 2
_VT_SHOW = 3            # IModalWindow::Show
_VT_SET_OPTIONS = 9     # IFileDialog::SetOptions
_VT_GET_OPTIONS = 10
_VT_SET_TITLE = 17      # IFileDialog::SetTitle
_VT_SET_OK_LABEL = 19   # IFileDialog::SetOkButtonLabel
_VT_GET_RESULT = 21     # IFileDialog::GetResult
_VT_ITEM_GET_DISPLAY_NAME = 5  # IShellItem::GetDisplayName


def _guid(text: str):
    import ctypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    g = GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    return g


def _method(ptr: int, index: int, *argtypes):
    """按 vtable 下标取 COM 方法并绑定签名（HRESULT 用 c_long 手动判，避免抛 OSError）。"""
    import ctypes

    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])


def _release(ptr: int) -> None:
    try:
        _method(ptr, _VT_RELEASE)(ptr)
    except Exception:  # noqa: BLE001 - 释放失败不影响结果
        pass


def _pick_windows(title: str, *, selftest: bool = False) -> str:
    """弹系统「选择文件夹」对话框。返回绝对路径；取消返回 ""。selftest 只验证 COM 链路不开窗。"""
    import ctypes

    ole32 = ctypes.oledll.ole32
    co_inited = False
    dialog = ctypes.c_void_p()
    item = ctypes.c_void_p()
    try:
        try:
            ole32.CoInitialize(None)
            co_inited = True
        except OSError:
            # RPC_E_CHANGED_MODE：这个线程已经是别的套间模型，继续用即可
            pass

        clsid, iid = _guid(_CLSID_FileOpenDialog), _guid(_IID_IFileOpenDialog)
        ole32.CoCreateInstance(ctypes.byref(clsid), None, _CLSCTX_INPROC_SERVER,
                               ctypes.byref(iid), ctypes.byref(dialog))
        dlg = dialog.value
        if not dlg:
            raise PickerUnavailable("创建系统文件夹选择窗口失败")

        opts = ctypes.c_uint(0)
        if _method(dlg, _VT_GET_OPTIONS, ctypes.POINTER(ctypes.c_uint))(dlg, ctypes.byref(opts)) < 0:
            raise PickerUnavailable("读取系统对话框选项失败")
        new_opts = opts.value | _FOS_PICKFOLDERS | _FOS_FORCEFILESYSTEM | \
            _FOS_PATHMUSTEXIST | _FOS_DONTADDTORECENT
        _method(dlg, _VT_SET_OPTIONS, ctypes.c_uint)(dlg, new_opts)
        _method(dlg, _VT_SET_TITLE, ctypes.c_wchar_p)(dlg, title)
        _method(dlg, _VT_SET_OK_LABEL, ctypes.c_wchar_p)(dlg, "选择此文件夹")

        if selftest:
            return "__selftest_ok__"

        hr = _method(dlg, _VT_SHOW, ctypes.c_void_p)(dlg, None)
        if hr == _ERROR_CANCELLED:
            return ""
        if hr < 0:
            raise PickerUnavailable(f"系统对话框返回错误 0x{hr & 0xFFFFFFFF:08X}")

        if _method(dlg, _VT_GET_RESULT, ctypes.POINTER(ctypes.c_void_p))(dlg, ctypes.byref(item)) < 0:
            raise PickerUnavailable("读取所选文件夹失败")
        buf = ctypes.c_wchar_p()
        if _method(item.value, _VT_ITEM_GET_DISPLAY_NAME, ctypes.c_uint,
                   ctypes.POINTER(ctypes.c_wchar_p))(item.value, _SIGDN_FILESYSPATH,
                                                     ctypes.byref(buf)) < 0:
            raise PickerUnavailable("无法把所选位置转成本地路径")
        path = buf.value or ""
        try:
            ctypes.oledll.ole32.CoTaskMemFree(buf)
        except Exception:  # noqa: BLE001
            pass
        return path
    finally:
        if item.value:
            _release(item.value)
        if dialog.value:
            _release(dialog.value)
        if co_inited:
            try:
                ole32.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------- tkinter 兜底
def _pick_tk(title: str) -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:  # noqa: BLE001
        raise PickerUnavailable(f"没有可用的图形界面库：{e}") from e
    root = tk.Tk()
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        path = filedialog.askdirectory(title=title, mustexist=True)
        return path or ""
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------- 对外入口
def pick_directory(title: str = "选择数据存放位置") -> str:
    """弹原生「选择文件夹」窗口；用户取消返回 ""。阻塞到用户操作完成为止。"""
    if not _PICK_LOCK.acquire(blocking=False):
        raise PickerBusy("已经有一个选择窗口开着，请先在上面选完")
    try:
        selftest = os.environ.get("GA_PICKER_SELFTEST") == "1"
        if sys.platform.startswith("win"):
            try:
                return _pick_windows(title, selftest=selftest)
            except PickerUnavailable:
                if selftest:
                    raise
                # COM 弹不出来再退 tkinter（老系统/被安全软件拦 COM 时实测会走到这里）
                return _pick_tk(title)
        return _pick_tk(title)
    finally:
        _PICK_LOCK.release()


def picker_supported() -> bool:
    """前端据此决定「浏览…」是按原生窗口还是回退内置目录浏览器。"""
    if sys.platform.startswith("win"):
        return True
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False
