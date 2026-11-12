#!/usr/bin/env python3
"""
Codigos: 
B=boot, 
C=calibrando
A=armado
M=movimento travado
E=falha do sensor.
pyusb e pywin32.
"""
from __future__ import annotations

import argparse
import ctypes
import platform
import queue
import sys
import threading
import time
from typing import Any

try:
    import usb.core
    import usb.util
    from usb.backend import libusb0
except ImportError:
    print("Dependencia ausente. Instale com: python -m pip install pyusb", file=sys.stderr)
    raise SystemExit(2)

VID = 0x16C0
PID = 0x05DF
SCAN_INTERVAL = 0.50
POLL_INTERVAL = 0.08
HEARTBEAT_TIMEOUT = 3.0
USB_TIMEOUT_MS = 180

# DigiUSB utiliza CLASS | RECIPIENT_DEVICE | ENDPOINT_IN = 0xA0.
# O firmware oficial espera wValue=0 e devolve exatamente um byte.
USB_BM_REQUEST_TYPE = 0xA0
USB_GET_REPORT = 0x01

try:
    USB_BACKEND = libusb0.get_backend()
except Exception:
    USB_BACKEND = None


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def require_backend() -> None:
    if USB_BACKEND is None:
        raise RuntimeError(
            "O PyUSB foi instalado, mas nao conseguiu carregar a biblioteca nativa "
            "libusb0.dll do driver libusb-win32. Verifique se a versao 64-bit do "
            "pacote/driver DigiUSB instalou libusb0.dll e se ela esta acessivel "
            "pelo Windows (por exemplo, C:\\Windows\\System32). "
            "Nao troque o driver no Gerenciador de Dispositivos por enquanto."
        )


def usb_devices() -> list[Any]:
    require_backend()
    found = usb.core.find(
        find_all=True,
        idVendor=VID,
        idProduct=PID,
        backend=USB_BACKEND,
    )
    return list(found or [])


def device_key(dev: Any) -> tuple[Any, Any]:
    # Bus + endereco identificam a instancia USB enquanto estiver conectada.
    return (getattr(dev, "bus", None), getattr(dev, "address", None))


def close_device(dev: Any | None) -> None:
    if dev is None:
        return
    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass


def read_status(dev: Any) -> str | None:
    """Consome um byte da fila TX da DigiUSB via HID GET_REPORT customizado."""
    try:
        data = dev.ctrl_transfer(
            USB_BM_REQUEST_TYPE,
            USB_GET_REPORT,
            0,                  # wValue: DigiUSB espera zero (nao 0x0300)
            0,                  # wIndex
            1,                  # wLength: um byte
            timeout=USB_TIMEOUT_MS,
        )
    except usb.core.USBError:
        # DigiUSB pode responder STALL quando a fila TX ainda esta vazia.
        return None

    if data is None or len(data) < 1:
        return None
    value = int(data[0])
    char = chr(value)
    return char if char in {"B", "C", "A", "M", "E"} else None


def list_target_devices() -> int:
    try:
        devices = usb_devices()
    except Exception as exc:
        print(f"Falha ao iniciar backend libusb0: {exc}", file=sys.stderr)
        return 2

    if not devices:
        print(f"Nenhum DigiUSB {VID:04X}:{PID:04X} encontrado pelo backend libusb0.")
        print("O Gerenciador de Dispositivos pode mostrar DigiUSB/libusb0, mas confira a DLL libusb0.dll.")
        return 1

    print(f"Encontrado(s) {len(devices)} dispositivo(s) DigiUSB:")
    for dev in devices:
        print(
            f"VID:PID={dev.idVendor:04X}:{dev.idProduct:04X} | "
            f"Bus={getattr(dev, 'bus', '?')} | "
            f"Endereco={getattr(dev, 'address', '?')}"
        )
    return 0


class WindowsSession:
    """Bloqueia a sessao e observa WTS_SESSION_UNLOCK apos autenticacao do Windows."""

    WM_WTSSESSION_CHANGE = 0x02B1
    WTS_SESSION_LOCK = 0x0007
    WTS_SESSION_UNLOCK = 0x0008

    def __init__(self) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("Esta versao do Guard foi preparada para Windows 10/11.")
        try:
            import win32api
            import win32con
            import win32gui
            import win32ts
        except ImportError as exc:
            raise RuntimeError("Instale pywin32: python -m pip install pywin32") from exc

        self.win32api = win32api
        self.win32con = win32con
        self.win32gui = win32gui
        self.win32ts = win32ts
        self.events: queue.Queue[int] = queue.Queue()
        self.class_name = f"DigisparkGuard_{int(time.time())}_{id(self)}"

        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = self.class_name
        wc.lpfnWndProc = {
            self.WM_WTSSESSION_CHANGE: self._on_session_change,
            win32con.WM_DESTROY: self._on_destroy,
        }
        atom = win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindow(
            atom, self.class_name, 0,
            0, 0, 0, 0,
            0, 0, wc.hInstance, None,
        )
        try:
            # NOTIFY_FOR_THIS_SESSION = 0
            win32ts.WTSRegisterSessionNotification(self.hwnd, 0)
        except Exception:
            win32gui.DestroyWindow(self.hwnd)
            raise

    def _on_session_change(self, hwnd, msg, wparam, lparam):
        self.events.put(int(wparam))
        return 0

    @staticmethod
    def _on_destroy(hwnd, msg, wparam, lparam):
        return 0

    def request_lock(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.LockWorkStation.restype = ctypes.c_int
        if not user32.LockWorkStation():
            error = ctypes.get_last_error()
            raise OSError(error, "LockWorkStation falhou")

    def poll_events(self) -> list[str]:
        self.win32gui.PumpWaitingMessages()
        result: list[str] = []
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event == self.WTS_SESSION_LOCK:
                result.append("locked")
            elif event == self.WTS_SESSION_UNLOCK:
                result.append("unlocked")
        return result

    def close(self) -> None:
        try:
            self.win32ts.WTSUnRegisterSessionNotification(self.hwnd)
        except Exception:
            pass
        try:
            self.win32gui.DestroyWindow(self.hwnd)
        except Exception:
            pass


class Alarm:
    def __init__(self) -> None:
        self.active = threading.Event()
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        import winsound
        while not self.finished.is_set():
            if not self.active.is_set():
                self.finished.wait(0.10)
                continue
            try:
                winsound.Beep(1900, 400)
            except Exception:
                print("\a", end="", flush=True)
                self.finished.wait(0.25)

    def start(self) -> None:
        self.active.set()

    def silence(self) -> None:
        self.active.clear()

    def close(self) -> None:
        self.active.clear()
        self.finished.set()
        self.thread.join(timeout=1.0)


def diagnostic(seconds: float) -> int:
    """Le estados sem bloquear o Windows; executar antes do modo de protecao."""
    try:
        require_backend()
    except Exception as exc:
        log(str(exc))
        return 2

    log(f"Diagnostico DigiUSB/libusb0: procurando {VID:04X}:{PID:04X}.")
    dev = None
    key = None
    last_state = None
    last_valid = time.monotonic()
    warned_no_data = False
    deadline = time.monotonic() + seconds
    next_scan = 0.0
    names = {
        "B": "boot / firmware iniciou",
        "C": "calibrando",
        "A": "armado / estavel",
        "M": "MOVIMENTO detectado",
        "E": "erro do sensor",
    }

    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if dev is None and now >= next_scan:
                next_scan = now + SCAN_INTERVAL
                try:
                    devices = usb_devices()
                except Exception as exc:
                    log(f"Falha ao enumerar USB: {exc}")
                    time.sleep(POLL_INTERVAL)
                    continue
                if devices:
                    dev = devices[0]
                    key = device_key(dev)
                    last_valid = now
                    log(f"DigiUSB aberto via libusb0 (bus={key[0]}, endereco={key[1]}).")
                    log("Aguardando relatorios do firmware...")

            if dev is not None:
                try:
                    state = read_status(dev)
                    if state is not None:
                        last_valid = now
                        warned_no_data = False
                        if state != last_state:
                            log(f"Recebido: {state} ({names[state]}).")
                            last_state = state
                except Exception as exc:
                    log(f"Erro na leitura USB: {exc}")

                if now - last_valid > 4.0 and not warned_no_data:
                    log("Dispositivo enumerado, mas nenhum byte valido foi lido. Verifique firmware DigiUSB/GET_REPORT.")
                    warned_no_data = True

                if now >= next_scan:
                    next_scan = now + SCAN_INTERVAL
                    try:
                        current = {device_key(d) for d in usb_devices()}
                    except Exception:
                        current = set()
                    if key not in current:
                        log("DigiUSB desconectado.")
                        close_device(dev)
                        dev = None
                        key = None

            time.sleep(POLL_INTERVAL)
    finally:
        close_device(dev)

    log("Diagnostico encerrado. Nenhum bloqueio foi solicitado.")
    return 0


def run_guard() -> int:
    try:
        require_backend()
        session = WindowsSession()
    except Exception as exc:
        log(f"Inicializacao impossivel: {exc}")
        return 2

    alarm = Alarm()
    dev = None
    active_key = None
    session_active = False
    got_armed = False
    lock_requested = False
    authorized_unlock = False
    alarm_latched = False
    last_valid_report = 0.0
    last_scan = 0.0
    no_report_warned = False
    last_state_seen: str | None = None

    def set_alarm(reason: str) -> None:
        nonlocal alarm_latched
        if not alarm_latched:
            alarm_latched = True
            log(f"ALARME: {reason}")
        alarm.start()

    def request_lock(reason: str) -> None:
        nonlocal lock_requested
        if lock_requested:
            return
        try:
            session.request_lock()
            lock_requested = True
            log(f"Bloqueio do Windows solicitado ({reason}).")
        except Exception as exc:
            set_alarm(f"nao foi possivel bloquear o Windows: {exc}")

    def reset_cycle() -> None:
        nonlocal session_active, got_armed, lock_requested
        nonlocal authorized_unlock, alarm_latched, last_valid_report
        nonlocal last_state_seen
        session_active = False
        got_armed = False
        lock_requested = False
        authorized_unlock = False
        alarm_latched = False
        last_valid_report = 0.0
        last_state_seen = None
        alarm.silence()
        log("Ciclo encerrado. Aguardando nova conexao do Digispark.")

    log("Digispark Guard iniciado (DigiUSB via libusb0/PyUSB).")
    log(f"Aguardando {VID:04X}:{PID:04X}; nenhuma porta COM sera criada.")
    log("O Windows sera bloqueado quando o firmware informar A (armado).")

    try:
        while True:
            now = time.monotonic()

            for event in session.poll_events():
                if event == "locked":
                    log("Windows confirmou o bloqueio da sessao.")
                elif event == "unlocked" and session_active and lock_requested:
                    authorized_unlock = True
                    alarm.silence()
                    log("Sessao desbloqueada pelo Windows. Alarme desarmado; remocao liberada.")
                    if dev is None:
                        reset_cycle()

            if now - last_scan >= SCAN_INTERVAL:
                last_scan = now
                try:
                    devices = usb_devices()
                except Exception as exc:
                    devices = []
                    log(f"Falha ao enumerar USB: {exc}")

                current_keys = {device_key(d) for d in devices}

                if dev is None and not session_active and devices:
                    try:
                        dev = devices[0]
                        active_key = device_key(dev)
                        session_active = True
                        got_armed = False
                        lock_requested = False
                        authorized_unlock = False
                        alarm_latched = False
                        last_valid_report = now
                        no_report_warned = False
                        alarm.silence()
                        log("DigiUSB conectado; aguardando calibracao (C) e armamento (A).")
                    except Exception as exc:
                        close_device(dev)
                        dev = None
                        active_key = None
                        log(f"Nao foi possivel abrir DigiUSB: {exc}")

                elif dev is not None and active_key not in current_keys:
                    close_device(dev)
                    dev = None
                    active_key = None
                    log("DigiUSB removido/desconectado.")
                    if session_active and not authorized_unlock:
                        set_alarm("Digispark removido antes da autenticacao do usuario")
                        request_lock("desconexao do dispositivo")
                    elif session_active and authorized_unlock:
                        reset_cycle()

            if dev is not None:
                try:
                    state = read_status(dev)
                    if state is not None:
                        last_valid_report = now
                        no_report_warned = False
                        if state != last_state_seen:
                            last_state_seen = state
                            names = {
                                "B": "Firmware iniciou.",
                                "C": "Calibrando o GY-273; mantenha o notebook parado.",
                                "A": "Sensor calibrado. Protecao armada.",
                                "M": "Movimento/alteracao magnetica detectada.",
                                "E": "Falha no sensor; verifique GY-273 e ligacoes.",
                            }
                            log(names[state])

                        if state == "A" and not got_armed:
                            got_armed = True
                            if not authorized_unlock:
                                request_lock("sensor armado")
                        elif state == "M" and not authorized_unlock:
                            set_alarm("movimento ou alteracao magnetica detectada")
                            request_lock("movimento detectado")
                        elif state == "E" and got_armed and not authorized_unlock:
                            set_alarm("falha/perda do sensor depois do armamento")
                            request_lock("falha do sensor")
                except Exception as exc:
                    log(f"Falha ao consultar DigiUSB: {exc}")

            if (
                session_active and dev is not None and last_valid_report
                and (now - last_valid_report) > HEARTBEAT_TIMEOUT
                and not authorized_unlock
            ):
                if not no_report_warned:
                    no_report_warned = True
                    log("Aviso: nenhum relatorio DigiUSB valido recebido ha 3 s.")
                if got_armed:
                    set_alarm("comunicacao USB interrompida com a protecao armada")
                    request_lock("perda de heartbeat USB")

            if session_active and authorized_unlock and dev is None:
                reset_cycle()

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log("Encerrando por Ctrl+C.")
    finally:
        close_device(dev)
        alarm.close()
        session.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Alarme temporario Digispark + GY-273 via DigiUSB/libusb0")
    parser.add_argument(
        "--listar-usb", "--listar-hid", dest="listar_usb", action="store_true",
        help="lista DigiUSB 16C0:05DF usando backend libusb0 e encerra",
    )
    parser.add_argument("--diagnostico", type=float, metavar="SEGUNDOS", help="le estados sem bloquear o Windows")
    args = parser.parse_args()

    if args.listar_usb:
        return list_target_devices()
    if args.diagnostico is not None:
        return diagnostic(max(1.0, args.diagnostico))
    return run_guard()


if __name__ == "__main__":
    raise SystemExit(main())
