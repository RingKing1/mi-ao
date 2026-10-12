#!/usr/bin/env python3
"""
米遥 (MI-AO) Linux 核心整合守护服务
同时集成两大核心链路：
1. BLE 语音链路：按住遥控器语音键说话，松手 0.3s 本地 Whisper 极速转写，写入剪贴板与历史记录
2. HID 飞鼠手柄：独占遥控器按键，方向环平滑移动鼠标、OK 为左键、Back 为右键、音量为滚轮、TV 切换模式
"""

import os
import sys
import time
import glob
import asyncio
import threading
from datetime import datetime

# 兼容加载用户环境包
for user in [os.environ.get("SUDO_USER"), os.environ.get("USER"), "nvidia"]:
    if user:
        user_site = f"/home/{user}/.local/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        if os.path.isdir(user_site) and user_site not in sys.path:
            sys.path.insert(0, user_site)

# 清理失效环境变量
for k in ["ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
    os.environ.pop(k, None)
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

try:
    from bleak import BleakClient
    from bleak.backends.device import BLEDevice
except ImportError:
    print("错误: 未安装 bleak 库")
    sys.exit(1)

try:
    import evdev
    from evdev import InputDevice, UInput, ecodes as e
except ImportError:
    print("错误: 未安装 evdev 库")
    sys.exit(1)

from atvv_protocol import ATVVProtocol, AudioPipeline
from whisper_transcriber import LocalWhisperTranscriber
from submitter import LinuxSubmitter

XIAOMI_VENDOR_ID = 0x2717
XIAOMI_PRODUCT_ID = 0x32B8
REMOTE_NAME_KEYWORDS = ["小米", "xiaomi", "remote", "遥控器"]

# 按键代码列表
DPAD_UP = [e.KEY_UP]
DPAD_DOWN = [e.KEY_DOWN]
DPAD_LEFT = [e.KEY_LEFT]
DPAD_RIGHT = [e.KEY_RIGHT]
CENTER_KEYS = [e.KEY_ENTER, e.KEY_OK, 28, 352]
BACK_KEYS = [e.KEY_BACK, e.KEY_ESC, 158, 1]
HOME_KEYS = [e.KEY_HOMEPAGE, e.KEY_HOME, 172, 102]
VOL_UP_KEYS = [e.KEY_VOLUMEUP, 115]
VOL_DOWN_KEYS = [e.KEY_VOLUMEDOWN, 114]
TV_KEYS = [e.KEY_TV, e.KEY_PROG1, 377, 148]

class IntegratedHIDController:
    """HID 独占飞鼠手柄控制器"""
    def __init__(self, uinput_dev: UInput):
        self.ui = uinput_dev
        self.running = False
        self.mode = "pointer"
        self.active_direction = None
        self.direction_pressed_at = 0.0
        self.lock = threading.Lock()
        self.remote_dev = None

    def find_hid_device(self) -> InputDevice:
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                d = InputDevice(path)
                if d.info.vendor == XIAOMI_VENDOR_ID and d.info.product == XIAOMI_PRODUCT_ID:
                    return d
                if any(kw in d.name.lower() for kw in REMOTE_NAME_KEYWORDS):
                    return d
            except Exception:
                continue
        return None

    def pointer_speed(self, held_seconds: float) -> float:
        if held_seconds < 0.2:
            return 120.0
        elif held_seconds < 1.0:
            progress = (held_seconds - 0.2) / 0.8
            return 120.0 + progress * (520.0 - 120.0)
        else:
            return min(1100.0, 520.0 + (held_seconds - 1.0) * 420.0)

    def motion_loop(self):
        tick_interval = 1.0 / 60.0
        last_tick = time.time()
        while self.running:
            time.sleep(tick_interval)
            now = time.time()
            delta = min(0.05, now - last_tick)
            last_tick = now

            with self.lock:
                if self.mode != "pointer" or not self.active_direction or not self.ui:
                    continue
                held = now - self.direction_pressed_at
                distance = self.pointer_speed(held) * delta

                dx, dy = 0, 0
                if self.active_direction == 'up':    dy = -int(distance)
                elif self.active_direction == 'down':  dy = int(distance)
                elif self.active_direction == 'left':  dx = -int(distance)
                elif self.active_direction == 'right': dx = int(distance)

                if dx != 0 or dy != 0:
                    if dx != 0: self.ui.write(e.EV_REL, e.REL_X, dx)
                    if dy != 0: self.ui.write(e.EV_REL, e.REL_Y, dy)
                    self.ui.syn()

    def handle_direction(self, direction: str, is_down: bool):
        with self.lock:
            if is_down:
                if self.mode == "pointer":
                    self.active_direction = direction
                    self.direction_pressed_at = time.time()
                elif self.ui:
                    key_map = {'up': e.KEY_UP, 'down': e.KEY_DOWN, 'left': e.KEY_LEFT, 'right': e.KEY_RIGHT}
                    self.ui.write(e.EV_KEY, key_map[direction], 1)
                    self.ui.syn()
            else:
                if self.mode == "pointer":
                    if self.active_direction == direction:
                        self.active_direction = None
                elif self.ui:
                    key_map = {'up': e.KEY_UP, 'down': e.KEY_DOWN, 'left': e.KEY_LEFT, 'right': e.KEY_RIGHT}
                    self.ui.write(e.EV_KEY, key_map[direction], 0)
                    self.ui.syn()

    def start_loop(self):
        self.running = True
        threading.Thread(target=self.motion_loop, daemon=True).start()

        while self.running:
            dev = self.find_hid_device()
            if not dev:
                time.sleep(2)
                continue

            self.remote_dev = dev
            try:
                dev.grab()
                print(f"[HID] 已独占遥控器按键外设: {dev.name} ({dev.path})")
                for event in dev.read_loop():
                    if not self.running:
                        break
                    if event.type != e.EV_KEY:
                        continue
                    code, val = event.code, event.value
                    is_down = (val == 1)
                    is_up = (val == 0)

                    if code in DPAD_UP:
                        self.handle_direction('up', is_down)
                    elif code in DPAD_DOWN:
                        self.handle_direction('down', is_down)
                    elif code in DPAD_LEFT:
                        self.handle_direction('left', is_down)
                    elif code in DPAD_RIGHT:
                        self.handle_direction('right', is_down)
                    elif code in CENTER_KEYS and self.ui:
                        if self.mode == "pointer":
                            self.ui.write(e.EV_KEY, e.BTN_LEFT, 1 if is_down else 0)
                        else:
                            self.ui.write(e.EV_KEY, e.KEY_ENTER, 1 if is_down else 0)
                        self.ui.syn()
                    elif code in BACK_KEYS and self.ui:
                        if self.mode == "pointer":
                            self.ui.write(e.EV_KEY, e.BTN_RIGHT, 1 if is_down else 0)
                        else:
                            self.ui.write(e.EV_KEY, e.KEY_ESC, 1 if is_down else 0)
                        self.ui.syn()
                    elif code in VOL_UP_KEYS and is_down and self.ui:
                        self.ui.write(e.EV_REL, e.REL_WHEEL, 1)
                        self.ui.syn()
                    elif code in VOL_DOWN_KEYS and is_down and self.ui:
                        self.ui.write(e.EV_REL, e.REL_WHEEL, -1)
                        self.ui.syn()
                    elif code in TV_KEYS and is_down:
                        with self.lock:
                            self.mode = "directional" if self.mode == "pointer" else "pointer"
                            self.active_direction = None
                        print(f"[HID] 遥控器模式已切换为: {self.mode.upper()}")
            except Exception as err:
                print(f"[HID] 连接断开或异常: {err}")
            finally:
                try:
                    dev.ungrab()
                except Exception:
                    pass
                time.sleep(2)


class MiAoDaemon:
    """米遥一体化服务守护进程"""
    STATE_DISCONNECTED = "disconnected"
    STATE_READY        = "ready"
    STATE_OPENING      = "opening"
    STATE_STREAMING    = "streaming"

    def __init__(self, model_size="base", auto_paste=False, auto_enter=False, enable_mouse=True):
        self.protocol = ATVVProtocol()
        self.state = self.STATE_DISCONNECTED
        self.stream_id = 0
        self.audio_frames = []
        self.client: BleakClient = None
        self.lock = asyncio.Lock()
        self.enable_mouse = enable_mouse

        self.recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
        os.makedirs(self.recordings_dir, exist_ok=True)

        # 统一的虚拟输入设备 (支持鼠标位移与按键模拟)
        self.ui = None
        try:
            capabilities = {
                e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
                e.EV_KEY: [
                    e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE,
                    e.KEY_UP, e.KEY_DOWN, e.KEY_LEFT, e.KEY_RIGHT,
                    e.KEY_ENTER, e.KEY_ESC, e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_V
                ]
            }
            self.ui = UInput(capabilities, name="Mi-Ao-Virtual-Input", version=0x1)
            print("[系统] 虚拟输入设备初始化成功 (/dev/uinput)")
        except Exception as err:
            print(f"[系统] 虚拟输入设备未能初始化 (如需飞鼠控制请确认 uinput 权限): {err}")

        # 提交器与 Whisper 转写器
        self.submitter = LinuxSubmitter(
            base_dir=os.path.dirname(__file__),
            auto_paste=auto_paste,
            auto_enter=auto_enter
        )
        self.transcriber = LocalWhisperTranscriber(model_size=model_size)

        # 飞鼠控制器
        self.hid_controller = None
        if self.enable_mouse:
            self.hid_controller = IntegratedHIDController(self.ui)

    async def find_ble_device(self) -> BLEDevice:
        mac = None
        name = "小米蓝牙语音遥控器"

        try:
            import subprocess
            out = subprocess.check_output(["bluetoothctl", "devices", "Connected"], text=True)
            for line in out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 3 and parts[0] == "Device":
                    candidate_name = " ".join(parts[2:])
                    if any(kw in candidate_name.lower() for kw in REMOTE_NAME_KEYWORDS):
                        mac = parts[1]
                        name = candidate_name
                        break
        except Exception:
            pass

        if not mac:
            mac = "C0:5D:39:F8:84:F8"

        dbus_mac = mac.replace(":", "_").upper()
        dbus_path = f"/org/bluez/hci0/dev_{dbus_mac}"
        return BLEDevice(address=mac, name=name, details={"path": dbus_path, "props": {}})

    def on_control_notification(self, sender, data: bytearray):
        raw = bytes(data)
        event = self.protocol.parse_control(raw)
        asyncio.create_task(self.handle_control_event(event))

    async def handle_control_event(self, event: dict):
        event_type = event.get("type")

        async with self.lock:
            if event_type == "capabilities":
                caps = event["capabilities"]
                self.protocol.accept_capabilities(caps)
                self.state = self.STATE_READY
                print(f"[语音] ATVV v{caps.version} 协商成功 (Codec={self.protocol.codec})，随时就绪！")

            elif event_type == "start_search":
                if self.state in (self.STATE_STREAMING, self.STATE_OPENING):
                    await self.finalize_recording(reason="second-press")
                elif self.state == self.STATE_READY:
                    await self.begin_opening()

            elif event_type == "audio_start":
                if self.state in (self.STATE_OPENING, self.STATE_READY):
                    self.stream_id = event.get("stream_id", 0)
                    self.state = self.STATE_STREAMING
                    self.audio_frames = []
                    print("[语音] 正在录音...")

            elif event_type == "audio_stop":
                if self.state not in (self.STATE_STREAMING, self.STATE_OPENING):
                    return
                print("[语音] 松开语音键，正在识别...")
                await self.finalize_recording(reason="remote-release")

            elif event_type == "audio_sync":
                self.protocol.decoder.reset(event["predictor"], event["step_index"])

    def on_audio_notification(self, sender, data: bytearray):
        if self.state in (self.STATE_STREAMING, self.STATE_OPENING):
            self.audio_frames.append(bytes(data))

    async def begin_opening(self):
        try:
            self.audio_frames = []
            self.state = self.STATE_OPENING
            cmd = self.protocol.mic_open_command()
            await self.client.write_gatt_char(ATVVProtocol.TX_UUID, cmd, response=True)
        except Exception:
            self.state = self.STATE_READY

    async def finalize_recording(self, reason: str):
        self.state = self.STATE_READY
        captured = self.audio_frames
        self.audio_frames = []

        try:
            cmd = self.protocol.mic_close_command(self.stream_id)
            await self.client.write_gatt_char(ATVVProtocol.TX_UUID, cmd, response=True)
        except Exception:
            pass

        if not captured:
            return

        all_samples = []
        for frame in captured:
            res = self.protocol.decode_audio(frame)
            if res:
                all_samples.extend(res[1])

        sample_rate = 16000
        if len(all_samples) < sample_rate // 4:
            return

        duration = len(all_samples) / sample_rate
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_path = os.path.join(self.recordings_dir, f"speech_{timestamp}.wav")
        AudioPipeline.write_wav(all_samples, sample_rate, wav_path)

        # Whisper 识别
        transcript = self.transcriber.transcribe_samples(all_samples, language="zh")
        if not transcript:
            return

        res = self.submitter.submit(transcript, audio_path=wav_path)
        print("--------------------------------------------------")
        print(f"🎉 语音输入: {transcript}")
        print(f"   [已保存并写入剪贴板, 耗时 {duration:.2f}s]")
        print("--------------------------------------------------")

    async def run_ble(self):
        while True:
            try:
                device = await self.find_ble_device()
                print(f"[语音] 正在连接 GATT 语音通道 [{device.address}]...")
                async with BleakClient(device, timeout=10.0) as client:
                    self.client = client
                    print(f"[语音] BLE 通道连接成功！")

                    await client.start_notify(ATVVProtocol.CONTROL_UUID, self.on_control_notification)
                    await client.start_notify(ATVVProtocol.RX_UUID, self.on_audio_notification)
                    await client.write_gatt_char(ATVVProtocol.TX_UUID, ATVVProtocol.GET_CAPS_COMMAND, response=True)

                    while client.is_connected:
                        await asyncio.sleep(1)
            except Exception as err:
                print(f"[语音] BLE 连接异常或断开: {err}，5秒后重试...")
                await asyncio.sleep(5)

    def start(self):
        if self.enable_mouse and self.hid_controller:
            threading.Thread(target=self.hid_controller.start_loop, daemon=True).start()

        print("==================================================")
        print(">> 【米遥 (MI-AO) Linux 一体化守护服务启动】")
        print(">> - 语音：随时按住【语音键】说话，松手转写并同步剪贴板")
        print(">> - 飞鼠：方向环移动鼠标、OK为左键、返回为右键、TV切模式")
        print("==================================================")
        asyncio.run(self.run_ble())


def main():
    import argparse
    parser = argparse.ArgumentParser(description="米遥 Linux 一体化服务")
    parser.add_argument("--model", default="base", help="Whisper 模型规格")
    parser.add_argument("--auto-paste", action="store_true", help="识别后是否自动按 Ctrl+V 粘贴")
    parser.add_argument("--auto-enter", action="store_true", help="粘贴后是否自动按 Enter 回车")
    parser.add_argument("--no-mouse", action="store_true", help="禁用飞鼠按键控制")
    args = parser.parse_args()

    daemon = MiAoDaemon(
        model_size=args.model,
        auto_paste=args.auto_paste,
        auto_enter=args.auto_enter,
        enable_mouse=not args.no_mouse
    )
    try:
        daemon.start()
    except KeyboardInterrupt:
        print("\n米遥服务安全退出。")

if __name__ == "__main__":
    main()
