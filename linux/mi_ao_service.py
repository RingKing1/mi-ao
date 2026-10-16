#!/usr/bin/env python3
"""
米遥 (MI-AO) Linux 核心整合守护服务
同时集成两大核心链路：
1. BLE 实时语音打字链路：
   - 按住语音键说话：以 ~0.2s 延迟在当前打字框内实时逐字流式涌现，松手终态精准校准。
2. HID 飞鼠与快捷控制链路：
   - 默认【实时打字与确认发送模式】：
     * OK / 确认键：模拟 Enter 回车，一键发送打字框内容！
     * 方向环：平滑移动鼠标指针（按住平滑加速）
     * 返回键：ESC 取消
     * TV 键：在【打字与确认发送】、【飞鼠点击】与【键盘方向键】模式之间切换
"""

import os
import sys
import time
import glob
import asyncio
import threading
from datetime import datetime
from typing import Optional, List

# 确保所有终端输出即时刷新到 systemd journal 日志
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

# 兼容加载用户环境包
for user in [os.environ.get("SUDO_USER"), os.environ.get("USER"), "nvidia"]:
    if user:
        user_site = f"/home/{user}/.local/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        if os.path.isdir(user_site) and user_site not in sys.path:
            sys.path.insert(0, user_site)

# 清理可能干扰 HuggingFace 的失效环境变量
for k in ["ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
    os.environ.pop(k, None)
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

try:
    from bleak import BleakClient
    from bleak.backends.device import BLEDevice
except ImportError:
    print("错误: 未安装 bleak 库。请运行: python3 -m pip install bleak", flush=True)
    sys.exit(1)

try:
    import evdev
    from evdev import InputDevice, UInput, ecodes as e
except ImportError:
    print("错误: 未安装 evdev 库。请运行: python3 -m pip install evdev", flush=True)
    sys.exit(1)

from atvv_protocol import ATVVProtocol, AudioPipeline
from whisper_transcriber import LocalWhisperTranscriber
from submitter import LinuxSubmitter

XIAOMI_VENDOR_ID = 0x2717
XIAOMI_PRODUCT_ID = 0x32B8
REMOTE_NAME_KEYWORDS = ["小米", "xiaomi", "remote", "遥控器"]

# 按键代码列表
DPAD_UP = [e.KEY_UP, 103]
DPAD_DOWN = [e.KEY_DOWN, 108]
DPAD_LEFT = [e.KEY_LEFT, 105]
DPAD_RIGHT = [e.KEY_RIGHT, 106]
CENTER_KEYS = [e.KEY_ENTER, e.KEY_OK, e.KEY_KPENTER, e.KEY_SELECT, 28, 96, 352, 353]
BACK_KEYS = [e.KEY_BACK, e.KEY_ESC, 158, 1]
HOME_KEYS = [e.KEY_HOMEPAGE, e.KEY_HOME, 172, 102]
VOL_UP_KEYS = [e.KEY_VOLUMEUP, 115]
VOL_DOWN_KEYS = [e.KEY_VOLUMEDOWN, 114]
TV_KEYS = [e.KEY_TV, e.KEY_PROG1, e.KEY_GRAVE, 377, 148, 41]

class IntegratedHIDController:
    """HID 独占飞鼠手柄与快捷控制控制器"""
    def __init__(self, uinput_keyboard: Optional[UInput], uinput_mouse: Optional[UInput], submitter: LinuxSubmitter, mode: str = "typing"):
        self.ui_kb = uinput_keyboard
        self.ui_mouse = uinput_mouse
        self.submitter = submitter
        self.running = False
        self.mode = mode  # "typing" (默认: OK为Enter发送), "mouse" (OK为鼠标左键), "directional" (方向键)
        self.active_direction = None
        self.direction_pressed_at = 0.0
        self.lock = threading.Lock()
        self.remote_dev = None

    def find_hid_device(self) -> Optional[InputDevice]:
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
                if self.mode not in ("typing", "mouse") or not self.active_direction or not self.ui_mouse:
                    continue
                held = now - self.direction_pressed_at
                distance = self.pointer_speed(held) * delta

                dx, dy = 0, 0
                if self.active_direction == 'up':    dy = -int(distance)
                elif self.active_direction == 'down':  dy = int(distance)
                elif self.active_direction == 'left':  dx = -int(distance)
                elif self.active_direction == 'right': dx = int(distance)

                if dx != 0 or dy != 0:
                    if dx != 0: self.ui_mouse.write(e.EV_REL, e.REL_X, dx)
                    if dy != 0: self.ui_mouse.write(e.EV_REL, e.REL_Y, dy)
                    self.ui_mouse.syn()

    def handle_direction(self, direction: str, is_down: bool):
        with self.lock:
            if is_down:
                if self.mode in ("typing", "mouse"):
                    self.active_direction = direction
                    self.direction_pressed_at = time.time()
                elif self.ui_kb:
                    key_map = {'up': e.KEY_UP, 'down': e.KEY_DOWN, 'left': e.KEY_LEFT, 'right': e.KEY_RIGHT}
                    self.ui_kb.write(e.EV_KEY, key_map[direction], 1)
                    self.ui_kb.syn()
            else:
                if self.mode in ("typing", "mouse"):
                    if self.active_direction == direction:
                        self.active_direction = None
                elif self.ui_kb:
                    key_map = {'up': e.KEY_UP, 'down': e.KEY_DOWN, 'left': e.KEY_LEFT, 'right': e.KEY_RIGHT}
                    self.ui_kb.write(e.EV_KEY, key_map[direction], 0)
                    self.ui_kb.syn()

    def cycle_mode(self):
        modes = ["typing", "mouse", "directional"]
        with self.lock:
            idx = modes.index(self.mode) if self.mode in modes else 0
            self.mode = modes[(idx + 1) % len(modes)]
            self.active_direction = None

        if self.mode == "typing":
            print("[HID] 模式切换为: 【打字与确认发送模式 (TYPING)】- OK键为回车发送，方向环移动鼠标", flush=True)
        elif self.mode == "mouse":
            print("[HID] 模式切换为: 【飞鼠点击模式 (MOUSE)】- OK键为鼠标左键，返回键为右键", flush=True)
        else:
            print("[HID] 模式切换为: 【键盘方向键模式 (DIRECTIONAL)】- 方向环为方向键，OK键为回车", flush=True)

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
                print(f"[HID] 已独占遥控器按键外设: {dev.name} ({dev.path})", flush=True)
                for event in dev.read_loop():
                    if not self.running:
                        break
                    if event.type != e.EV_KEY:
                        continue
                    code, val = event.code, event.value
                    is_down = (val == 1)

                    if code in DPAD_UP:
                        self.handle_direction('up', is_down)
                    elif code in DPAD_DOWN:
                        self.handle_direction('down', is_down)
                    elif code in DPAD_LEFT:
                        self.handle_direction('left', is_down)
                    elif code in DPAD_RIGHT:
                        self.handle_direction('right', is_down)
                    elif code in CENTER_KEYS:
                        if self.mode == "typing":
                            if is_down:
                                print(">> [按键] 确认发送 -> 模拟按压 Enter", flush=True)
                                self.submitter.send_enter()
                        elif self.mode == "mouse" and self.ui_mouse:
                            self.ui_mouse.write(e.EV_KEY, e.BTN_LEFT, 1 if is_down else 0)
                            self.ui_mouse.syn()
                        else:
                            if is_down:
                                self.submitter.send_enter()
                    elif code in BACK_KEYS:
                        if self.mode == "mouse" and self.ui_mouse:
                            self.ui_mouse.write(e.EV_KEY, e.BTN_RIGHT, 1 if is_down else 0)
                            self.ui_mouse.syn()
                        else:
                            if is_down:
                                print(">> [按键] 取消 / 返回 -> 模拟按压 ESC", flush=True)
                                self.submitter.send_esc()
                    elif code in VOL_UP_KEYS and is_down and self.ui_mouse:
                        self.ui_mouse.write(e.EV_REL, e.REL_WHEEL, 1)
                        self.ui_mouse.syn()
                    elif code in VOL_DOWN_KEYS and is_down and self.ui_mouse:
                        self.ui_mouse.write(e.EV_REL, e.REL_WHEEL, -1)
                        self.ui_mouse.syn()
                    elif code in TV_KEYS and is_down:
                        self.cycle_mode()
            except Exception as err:
                print(f"[HID] 连接断开或异常: {err}", flush=True)
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

    def __init__(self, model_size="small", enable_mouse=True, mode="typing"):
        self.protocol = ATVVProtocol()
        self.state = self.STATE_DISCONNECTED
        self.stream_id = 0
        self.client: Optional[BleakClient] = None
        self.lock = asyncio.Lock()
        self.enable_mouse = enable_mouse

        self.recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
        os.makedirs(self.recordings_dir, exist_ok=True)

        # 完整虚拟键盘设备 (配置 1~255 完整键盘键位，确保 udev 赋予 ID_INPUT_KEYBOARD=1 属性)
        self.ui_kb = None
        try:
            full_keys = [k for k in range(1, 256)]
            self.ui_kb = UInput({e.EV_KEY: full_keys}, name="Mi-Ao-Virtual-Keyboard", version=0x1)
            print("[系统] 虚拟键盘设备就绪 (完整键位 ID_INPUT_KEYBOARD=1 /dev/uinput)", flush=True)
        except Exception as err:
            print(f"[系统] 虚拟键盘初始化失败: {err}", flush=True)

        # 独立虚拟鼠标设备
        self.ui_mouse = None
        try:
            mouse_caps = {
                e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
                e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]
            }
            self.ui_mouse = UInput(mouse_caps, name="Mi-Ao-Virtual-Mouse", version=0x1)
            print("[系统] 虚拟鼠标设备就绪 (/dev/uinput -> Mi-Ao-Virtual-Mouse)", flush=True)
        except Exception as err:
            print(f"[系统] 虚拟鼠标初始化失败: {err}", flush=True)

        # 提交器与双阶 Whisper 转写器
        self.submitter = LinuxSubmitter(
            base_dir=os.path.dirname(__file__),
            uinput_keyboard=self.ui_kb
        )
        self.transcriber = LocalWhisperTranscriber(model_size=model_size)

        # 原始音频数据缓存与解码状态隔离
        self.raw_frames = []
        self.sync_predictor = 0
        self.sync_step = 0
        self.stream_typed_text = ""
        self.is_recording = False
        self.streaming_thread = None
        self.stream_lock = threading.Lock()

        # 飞鼠与按键控制器
        self.hid_controller = None
        if self.enable_mouse:
            self.hid_controller = IntegratedHIDController(
                uinput_keyboard=self.ui_kb,
                uinput_mouse=self.ui_mouse,
                submitter=self.submitter,
                mode=mode
            )

    def decode_raw_frames(self, frames: List[bytes], predictor: int, step_index: int) -> List[int]:
        """每次基于独立全新的解码器状态从第 0 帧解码，彻底杜绝跨会话状态累积污染"""
        if not frames:
            return []
        proto = ATVVProtocol()
        if self.protocol.capabilities:
            proto.accept_capabilities(self.protocol.capabilities)
        proto.decoder.reset(predictor, step_index)
        samples = []
        for f in frames:
            res = proto.decode_audio(f)
            if res and res[1]:
                samples.extend(res[1])
        return samples

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
                print(f"[语音] ATVV v{caps.version} 协商成功 (Codec={self.protocol.codec})，随时就绪！", flush=True)

            elif event_type == "start_search":
                if self.state in (self.STATE_STREAMING, self.STATE_OPENING):
                    await self.finalize_recording(reason="second-press")
                elif self.state == self.STATE_READY:
                    await self.begin_opening()

            elif event_type == "audio_start":
                if self.state in (self.STATE_OPENING, self.STATE_READY):
                    self.stream_id = event.get("stream_id", 0)
                    self.state = self.STATE_STREAMING
                    with self.stream_lock:
                        self.raw_frames = []
                        self.sync_predictor = 0
                        self.sync_step = 0
                        self.stream_typed_text = ""
                        self.is_recording = True
                    print("\n[语音] 按下语音键，正在倾听并实时打字...", flush=True)
                    self.streaming_thread = threading.Thread(target=self._streaming_worker, daemon=True)
                    self.streaming_thread.start()

            elif event_type == "audio_stop":
                if self.state not in (self.STATE_STREAMING, self.STATE_OPENING):
                    return
                print("[语音] 松开语音键，正在进行终态校准...", flush=True)
                await self.finalize_recording(reason="remote-release")

            elif event_type == "audio_sync":
                with self.stream_lock:
                    self.sync_predictor = event.get("predictor", 0)
                    self.sync_step = event.get("step_index", 0)

    def on_audio_notification(self, sender, data: bytearray):
        if self.state in (self.STATE_STREAMING, self.STATE_OPENING):
            with self.stream_lock:
                self.raw_frames.append(bytes(data))

    async def begin_opening(self):
        try:
            with self.stream_lock:
                self.raw_frames = []
                self.sync_predictor = 0
                self.sync_step = 0
                self.stream_typed_text = ""
            self.state = self.STATE_OPENING
            cmd = self.protocol.mic_open_command()
            await self.client.write_gatt_char(ATVVProtocol.TX_UUID, cmd, response=True)
        except Exception:
            self.state = self.STATE_READY

    def _streaming_worker(self):
        """流式后台线程：每隔约 0.3s 将已录入的原始帧快速推断，并差量注入打字框"""
        last_process_time = time.time()
        # 16kHz ADPCM 每帧 120 字节 = 240 samples (15ms)。40 帧约为 0.6s
        min_frames = 40

        while self.is_recording:
            time.sleep(0.06)
            now = time.time()
            if now - last_process_time < 0.28:
                continue

            with self.stream_lock:
                frames_snapshot = list(self.raw_frames)
                pred = self.sync_predictor
                step = self.sync_step

            if len(frames_snapshot) < min_frames:
                continue

            last_process_time = now
            samples = self.decode_raw_frames(frames_snapshot, pred, step)
            partial_text = self.transcriber.transcribe_stream_samples(samples)
            partial_text = partial_text.strip()

            if not self.is_recording:
                break

            if partial_text and partial_text != self.stream_typed_text:
                # 计算公共前缀
                k = 0
                while (k < len(self.stream_typed_text) and 
                       k < len(partial_text) and 
                       self.stream_typed_text[k] == partial_text[k]):
                    k += 1

                backspaces = len(self.stream_typed_text) - k
                delta = partial_text[k:]

                if backspaces > 0 or delta:
                    self.submitter.type_diff(backspaces, delta)
                    self.stream_typed_text = partial_text
                    print(f"  [实时打字] {partial_text}", flush=True)

    async def finalize_recording(self, reason: str):
        self.state = self.STATE_READY
        self.is_recording = False

        try:
            cmd = self.protocol.mic_close_command(self.stream_id)
            await self.client.write_gatt_char(ATVVProtocol.TX_UUID, cmd, response=True)
        except Exception:
            pass

        if self.streaming_thread:
            self.streaming_thread.join(timeout=0.6)

        with self.stream_lock:
            all_frames = list(self.raw_frames)
            pred = self.sync_predictor
            step = self.sync_step
            currently_typed = self.stream_typed_text
            self.raw_frames = []

        all_samples = self.decode_raw_frames(all_frames, pred, step)
        sample_rate = 16000

        if len(all_samples) < sample_rate * 0.25:
            # 误触抖动，清理可能已打出的临时文字
            if currently_typed:
                self.submitter.type_diff(len(currently_typed), "")
            return

        duration = len(all_samples) / sample_rate
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_path = os.path.join(self.recordings_dir, f"speech_{timestamp}.wav")
        AudioPipeline.write_wav(all_samples, sample_rate, wav_path)

        # 终态高精度 Whisper 识别
        final_text = self.transcriber.transcribe_final_samples(all_samples)
        if not final_text:
            if currently_typed:
                self.submitter.type_diff(len(currently_typed), "")
            return

        # 计算并更新终态与流式打字的差量
        k = 0
        while (k < len(currently_typed) and 
               k < len(final_text) and 
               currently_typed[k] == final_text[k]):
            k += 1

        backspaces = len(currently_typed) - k
        delta = final_text[k:]

        if backspaces > 0 or delta:
            self.submitter.type_diff(backspaces, delta)

        # 归档并全量同步剪贴板
        self.submitter.record_submission(final_text, audio_path=wav_path)
        with self.stream_lock:
            self.stream_typed_text = ""

        print("--------------------------------------------------", flush=True)
        print(f"🎉 语音输入完毕: {final_text}", flush=True)
        print(f"   [已实时输入到打字框 | 按遥控器【OK/确认键】即可发送]", flush=True)
        print("--------------------------------------------------", flush=True)

    async def run_ble(self):
        while True:
            try:
                device = await self.find_ble_device()
                print(f"[语音] 正在连接 GATT 语音通道 [{device.address}]...", flush=True)
                async with BleakClient(device, timeout=10.0) as client:
                    self.client = client
                    print(f"[语音] BLE 通道连接成功！", flush=True)

                    await client.start_notify(ATVVProtocol.CONTROL_UUID, self.on_control_notification)
                    await client.start_notify(ATVVProtocol.RX_UUID, self.on_audio_notification)
                    await client.write_gatt_char(ATVVProtocol.TX_UUID, ATVVProtocol.GET_CAPS_COMMAND, response=True)

                    while client.is_connected:
                        await asyncio.sleep(1)
            except Exception as err:
                print(f"[语音] BLE 连接异常或断开: {err}，5秒后重试...", flush=True)
                await asyncio.sleep(5)

    def start(self):
        if self.enable_mouse and self.hid_controller:
            threading.Thread(target=self.hid_controller.start_loop, daemon=True).start()

        mode_desc = "打字与确认发送模式 (OK键 = Enter发送)" if self.hid_controller and self.hid_controller.mode == "typing" else self.hid_controller.mode
        print("==================================================", flush=True)
        print(">> 【米遥 (MI-AO) Linux 一体化守护服务启动】", flush=True)
        print(f">> - 模式：{mode_desc}", flush=True)
        print(">> - 语音：按住【语音键】说话，文字实时流式输出到打字框", flush=True)
        print(">> - 发送：松手后按遥控器【OK/确认键】，直接回车发送！", flush=True)
        print(">> - 切换：按【TV键】可在 打字发送 / 飞鼠点击 / 方向键 间切换", flush=True)
        print("==================================================", flush=True)
        asyncio.run(self.run_ble())


def main():
    import argparse
    parser = argparse.ArgumentParser(description="米遥 Linux 一体化服务")
    parser.add_argument("--model", default="small", help="终态 Whisper 模型规格 (默认: small，可选: base)")
    parser.add_argument("--mode", default="typing", choices=["typing", "mouse", "directional"], help="初始遥控器模式 (默认: typing)")
    parser.add_argument("--no-mouse", action="store_true", help="禁用飞鼠按键控制")
    args = parser.parse_args()

    daemon = MiAoDaemon(
        model_size=args.model,
        enable_mouse=not args.no_mouse,
        mode=args.mode
    )
    try:
        daemon.start()
    except KeyboardInterrupt:
        print("\n米遥服务安全退出。", flush=True)

if __name__ == "__main__":
    main()
