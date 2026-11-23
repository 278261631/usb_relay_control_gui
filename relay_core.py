"""LCUS / CUS 型 USB 继电器模块（CH340 串口）控制核心。

零第三方依赖：直接调用 Win32 串口 API（kernel32），因此本机没装 pyserial 也能用。

通信协议（官方文档）：
    波特率 9600, 8 数据位, 无校验, 1 停止位
    帧格式: 0xA0 | 地址码 | 操作码 | 校验码
        操作码 0x01 = 吸合(开), 0x00 = 释放(关)
        校验码 = 前三个字节之和 & 0xFF
    第 1 路开: A0 01 01 A2      第 1 路关: A0 01 00 A1
    第 2 路开: A0 02 01 A3      第 2 路关: A0 02 00 A2
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

# --------------------------------------------------------------------------- #
# 协议常量
# --------------------------------------------------------------------------- #

START_BYTE = 0xA0
CMD_ON = 0x01
CMD_OFF = 0x00
DEFAULT_BAUD = 9600


def build_frame(address: int = 1, on: bool = True, start: int = START_BYTE) -> bytes:
    """生成一帧开/关指令。"""
    if not 0 <= address <= 0xFF:
        raise ValueError("地址码必须是 0~255")
    cmd = CMD_ON if on else CMD_OFF
    checksum = (start + address + cmd) & 0xFF
    return bytes((start, address, cmd, checksum))


def on_frame(address: int = 1) -> bytes:
    return build_frame(address, True)


def off_frame(address: int = 1) -> bytes:
    return build_frame(address, False)


def checksum_ok(frame: bytes, start: int = START_BYTE) -> bool:
    """校验模块回传帧（若有）。"""
    return len(frame) >= 4 and frame[0] == start and (sum(frame[:3]) & 0xFF) == frame[3]


# --------------------------------------------------------------------------- #
# Win32 串口封装
# --------------------------------------------------------------------------- #

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

PURGE_TXABORT = 0x0001
PURGE_RXABORT = 0x0002
PURGE_TXCLEAR = 0x0004
PURGE_RXCLEAR = 0x0008

NOPARITY, ODDPARITY, EVENPARITY = 0, 1, 2
ONESTOPBIT, ONE5STOPBITS, TWOSTOPBITS = 0, 1, 2

SETDTR = 5
CLRDTR = 6
SETRTS = 3
CLRRTS = 4


class DCB(ctypes.Structure):
    """Win32 DCB（设备控制块），布局与 Windows SDK 一致，共 28 字节。"""

    _fields_ = [
        ("DCBlength", wintypes.DWORD),
        ("BaudRate", wintypes.DWORD),
        ("Flags", wintypes.DWORD),  # 位域整体，按位操作
        ("wReserved", wintypes.WORD),
        ("XonLim", wintypes.WORD),
        ("XoffLim", wintypes.WORD),
        ("ByteSize", wintypes.BYTE),
        ("Parity", wintypes.BYTE),
        ("StopBits", wintypes.BYTE),
        ("XonChar", ctypes.c_char),
        ("XoffChar", ctypes.c_char),
        ("ErrorChar", ctypes.c_char),
        ("EofChar", ctypes.c_char),
        ("EvtChar", ctypes.c_char),
        ("wReserved1", wintypes.WORD),
    ]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [
        ("ReadIntervalTimeout", wintypes.DWORD),
        ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
        ("ReadTotalTimeoutConstant", wintypes.DWORD),
        ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
        ("WriteTotalTimeoutConstant", wintypes.DWORD),
    ]


_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.GetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(DCB)]
_k32.GetCommState.restype = wintypes.BOOL
_k32.SetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(DCB)]
_k32.SetCommState.restype = wintypes.BOOL
_k32.SetCommTimeouts.argtypes = [wintypes.HANDLE, ctypes.POINTER(COMMTIMEOUTS)]
_k32.SetCommTimeouts.restype = wintypes.BOOL
_k32.PurgeComm.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.PurgeComm.restype = wintypes.BOOL
_k32.WriteFile.argtypes = [
    wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
_k32.WriteFile.restype = wintypes.BOOL
_k32.ReadFile.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
]
_k32.ReadFile.restype = wintypes.BOOL
_k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
_k32.FlushFileBuffers.restype = wintypes.BOOL
_k32.EscapeCommFunction.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.EscapeCommFunction.restype = wintypes.BOOL


def _err(msg: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{msg}（Win32 错误 {code}）")


class SerialError(OSError):
    """串口打开/读写失败。"""


class SerialPort:
    """极简阻塞式串口，接口对齐 pyserial 的常用子集。"""

    def __init__(self, port: str = "COM5", baudrate: int = DEFAULT_BAUD,
                 bytesize: int = 8, parity: int = NOPARITY,
                 stopbits: int = ONESTOPBIT, write_timeout: float = 2.0):
        self.port = port
        self.baudrate = int(baudrate)
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.write_timeout = write_timeout
        self._h = None

    # -- 生命周期 ---------------------------------------------------------- #
    @property
    def is_open(self) -> bool:
        return self._h is not None

    def open(self) -> "SerialPort":
        if self._h is not None:
            return self
        path = self.port if self.port.startswith("\\\\.\\") else "\\\\.\\" + self.port
        handle = _k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None,
                                  OPEN_EXISTING, 0, None)
        if handle == INVALID_HANDLE_VALUE or handle is None:
            code = ctypes.get_last_error()
            if code == 5:      # ERROR_ACCESS_DENIED
                raise SerialError(code, f"{self.port} 被其他程序占用"
                                        "（串口助手或另一个窗口正在使用，先断开占用方再重试）")
            if code == 2:      # ERROR_FILE_NOT_FOUND
                raise SerialError(code, f"找不到串口 {self.port}，请检查 USB 是否插好")
            raise SerialError(code, f"无法打开 {self.port}（Win32 错误 {code}）")
        self._h = handle

        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        if not _k32.GetCommState(self._h, ctypes.byref(dcb)):
            err = _err("读取串口参数失败")
            self.close()
            raise SerialError(*err.args)

        dcb.BaudRate = self.baudrate
        dcb.ByteSize = self.bytesize
        dcb.Parity = self.parity
        dcb.StopBits = self.stopbits

        f = dcb.Flags
        f |= 1 << 0              # fBinary
        f &= ~(1 << 1)           # fParity
        f &= ~(0b11 << 2)        # 关闭 CTS / DSR 输出流控
        f = (f & ~(0b11 << 4)) | (1 << 4)   # DTR 常有效
        f &= ~(1 << 6)           # 不敏感 DSR
        f &= ~(1 << 7)           # fTXContinueOnXoff
        f &= ~(1 << 8)           # 关闭 XON/XOFF 输出流控
        f &= ~(1 << 9)           # 关闭 XON/XOFF 输入流控
        f &= ~(1 << 10)          # fErrorChar
        f &= ~(1 << 11)          # fNull
        f = (f & ~(0b11 << 12)) | (1 << 12)  # RTS 常有效（不做握手）
        f &= ~(1 << 14)          # fAbortOnError 关闭
        dcb.Flags = f

        if not _k32.SetCommState(self._h, ctypes.byref(dcb)):
            err = _err(f"设置串口参数失败（{self.baudrate} 8N1）")
            self.close()
            raise SerialError(*err.args)

        # 读：给多少拿多少，不阻塞等待
        to = COMMTIMEOUTS(0xFFFFFFFF, 0xFFFFFFFF, 0, 0, max(1, int(self.write_timeout * 1000)))
        if not _k32.SetCommTimeouts(self._h, ctypes.byref(to)):
            err = _err("设置串口超时失败")
            self.close()
            raise SerialError(*err.args)

        _k32.PurgeComm(self._h, PURGE_RXCLEAR | PURGE_TXCLEAR | PURGE_RXABORT | PURGE_TXABORT)
        # CH340 插入瞬间可能拉低 DTR 复位继电器板 MCU，稍等一下再发指令
        time.sleep(0.12)
        return self

    def close(self) -> None:
        if self._h is not None:
            try:
                _k32.PurgeComm(self._h, PURGE_RXCLEAR | PURGE_TXCLEAR)
                _k32.CloseHandle(self._h)
            finally:
                self._h = None

    def __enter__(self) -> "SerialPort":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 读写 -------------------------------------------------------------- #
    def write(self, data: bytes) -> int:
        if self._h is None:
            raise SerialError(0, "串口未打开")
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        written = wintypes.DWORD(0)
        if not _k32.WriteFile(self._h, buf, len(data), ctypes.byref(written), None):
            raise SerialError(*_err("写串口失败").args)
        return written.value

    def read(self, timeout: float = 0.3, size: int = 256) -> bytes:
        """尽力读取：直到 timeout 用尽或没有更多数据。"""
        if self._h is None:
            raise SerialError(0, "串口未打开")
        deadline = time.monotonic() + max(0.0, timeout)
        buf = ctypes.create_string_buffer(size)
        chunks: list[bytes] = []
        while True:
            n = wintypes.DWORD(0)
            if not _k32.ReadFile(self._h, buf, size, ctypes.byref(n), None):
                raise SerialError(*_err("读串口失败").args)
            if n.value:
                chunks.append(buf.raw[: n.value])
                deadline = max(deadline, time.monotonic() + 0.05)  # 有数据就多等一点
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        return b"".join(chunks)

    def flush(self) -> None:
        if self._h is not None:
            _k32.FlushFileBuffers(self._h)

    def reset_input_buffer(self) -> None:
        if self._h is not None:
            _k32.PurgeComm(self._h, PURGE_RXCLEAR)

    def set_dtr(self, value: bool) -> None:
        if self._h is not None:
            _k32.EscapeCommFunction(self._h, SETDTR if value else CLRDTR)

    def set_rts(self, value: bool) -> None:
        if self._h is not None:
            _k32.EscapeCommFunction(self._h, SETRTS if value else CLRRTS)


# --------------------------------------------------------------------------- #
# 端口枚举
# --------------------------------------------------------------------------- #

def list_ports() -> list[str]:
    """读取注册表 SERIALCOMM，返回 ['COM1', 'COM5', ...]。"""
    import winreg

    ports: list[str] = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except OSError:
        return ports
    with key:
        i = 0
        while True:
            try:
                _name, value, _type = winreg.EnumValue(key, i)
            except OSError:
                break
            if isinstance(value, str) and value.upper().startswith("COM"):
                ports.append(value.upper())
            i += 1
    return sorted(set(ports), key=lambda p: int(p[3:]) if p[3:].isdigit() else 999)


# --------------------------------------------------------------------------- #
# 高层继电器对象
# --------------------------------------------------------------------------- #

class Relay:
    """一路（或多路中指定地址）继电器。"""

    def __init__(self, port: str = "COM5", address: int = 1,
                 baudrate: int = DEFAULT_BAUD):
        self.port = port
        self.address = address
        self.baudrate = baudrate
        self.state: bool | None = None   # None = 未知
        self.ser = SerialPort(port, baudrate)

    def open(self) -> "Relay":
        self.ser.open()
        return self

    def close(self) -> None:
        self.ser.close()

    def __enter__(self) -> "Relay":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def send_frame(self, frame: bytes, echo_timeout: float = 0.0) -> bytes:
        self.ser.write(frame)
        self.ser.flush()
        if echo_timeout > 0:
            return self.ser.read(echo_timeout)
        return b""

    def set(self, on: bool) -> bytes:
        frame = build_frame(self.address, on)
        reply = self.send_frame(frame)
        self.state = on
        return reply

    def on(self) -> bytes:
        return self.set(True)

    def off(self) -> bytes:
        return self.set(False)

    def toggle(self) -> bytes:
        target = not bool(self.state)
        return self.set(target)

    def pulse(self, seconds: float = 0.5, blocking: bool = True):
        """点动：吸合 seconds 秒后自动释放。"""
        if blocking:
            try:
                self.on()
                time.sleep(max(0.05, seconds))
            finally:
                self.off()
        else:
            import threading

            def _run():
                try:
                    self.on()
                    time.sleep(max(0.05, seconds))
                finally:
                    self.off()

            th = threading.Thread(target=_run, daemon=True)
            th.start()
            return th


# --------------------------------------------------------------------------- #
# 命令行自检
# --------------------------------------------------------------------------- #

def _main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="LCUS/CUS USB 继电器命令行工具")
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--addr", type=int, default=1, help="地址码/通道，默认 1")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true")
    g.add_argument("--off", action="store_true")
    g.add_argument("--pulse", type=float, metavar="秒", nargs="?", const=0.5)
    g.add_argument("--raw", metavar="HEX", help="发送原始十六进制，如 'A0 01 01 A2'")
    g.add_argument("--ports", action="store_true", help="列出串口")
    g.add_argument("--selftest", action="store_true", help="只打开端口并关闭（不动作继电器）")
    args = ap.parse_args(argv)

    if args.ports:
        for p in list_ports():
            print(p)
        return 0

    if not any([args.on, args.off, args.pulse, args.raw, args.selftest]):
        ap.print_help()
        return 2

    try:
        with SerialPort(args.port, args.baud) as ser:
            print(f"[OK] 已打开 {args.port} @ {args.baud} 8N1")
            if args.selftest:
                return 0
            if args.raw:
                data = bytes.fromhex(args.raw.replace(",", " ").strip())
            elif args.pulse is not None:
                data = on_frame(args.addr)
                print(f"[TX] {data.hex(' ').upper()}  (吸合 {args.pulse}s 后释放)")
                ser.write(data); ser.flush()
                time.sleep(max(0.05, args.pulse))
                data = off_frame(args.addr)
            else:
                data = build_frame(args.addr, bool(args.on))
            print(f"[TX] {data.hex(' ').upper()}")
            ser.write(data)
            ser.flush()
            reply = ser.read(0.25)
            print(f"[RX] {reply.hex(' ').upper() or '(无回传，该型号通常不回传)'}")
        return 0
    except SerialError as exc:
        print(f"[ERR] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
