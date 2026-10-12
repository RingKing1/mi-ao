#!/usr/bin/env python3
"""
小米蓝牙遥控器 2 Pro - Linux BLE 语音桥接 (完整状态机版本)
严格对齐官方 Swift 状态机：
- 严格的状态控制：DISCONNECTED -> READY -> OPENING -> STREAMING -> READY
- 忽略空闲态的多余 AUDIO_STOP 通知，防止刷屏和产生垃圾文件
- 自动过滤时长 < 0.25 秒的误触抖动
- 按住说话，松手保存为高质量 16kHz WAV 音频
"""

import sys
import os
import time
import asyncio
from datetime import datetime

# 兼容 sudo 环境下加载普通用户的 site-packages
for user in [os.environ.get("SUDO_USER"), os.environ.get("USER"), "nvidia"]:
    if user:
        user_site = f"/home/{user}/.local/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        if os.path.isdir(user_site) and user_site not in sys.path:
            sys.path.insert(0, user_site)

try:
    from bleak import BleakClient
    from bleak.backends.device import BLEDevice
except ImportError:
    print("错误: 未安装 bleak 库。请运行: python3 -m pip install bleak")
    sys.exit(1)

from atvv_protocol import ATVVProtocol, AudioPipeline

REMOTE_NAME_KEYWORDS = ["小米", "xiaomi", "remote", "遥控器"]

class BLEVoiceBridge:
    STATE_DISCONNECTED = "disconnected"
    STATE_READY        = "ready"
    STATE_OPENING      = "opening"
    STATE_STREAMING    = "streaming"

    def __init__(self, target_address: str = None):
        self.target_address = target_address
        self.client: BleakClient = None
        self.protocol = ATVVProtocol()
        self.state = self.STATE_DISCONNECTED
        self.stream_id = 0
        self.audio_frames = []
        self.recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
        os.makedirs(self.recordings_dir, exist_ok=True)
        self.lock = asyncio.Lock()

    async def find_device(self) -> BLEDevice:
        mac = self.target_address
        name = "小米蓝牙语音遥控器"

        if not mac:
            # 1. 检查已连接设备
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
                            print(f">> 自动定位到已连接的遥控器: {name} [{mac}]")
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
        event_type = event.get("type")

        # 调度到异步协程中做严格的状态机处理
        asyncio.create_task(self.handle_control_event(event, raw))

    async def handle_control_event(self, event: dict, raw: bytes):
        event_type = event.get("type")

        async with self.lock:
            if event_type == "capabilities":
                caps = event["capabilities"]
                self.protocol.accept_capabilities(caps)
                self.state = self.STATE_READY
                print(f">> [能力协商成功] ATVV v{caps.version}, Codec={self.protocol.codec}, 帧长={caps.frame_size} 字节")
                print(">> 【随时就绪】请按住遥控器上的【语音键】说话，松手后自动保存语音！\n")

            elif event_type == "start_search":
                # 按下语音键
                if self.state in (self.STATE_STREAMING, self.STATE_OPENING):
                    print(">> 再次收到 START_SEARCH，结束当前语音会话")
                    await self.finalize_recording(reason="second-press")
                elif self.state == self.STATE_READY:
                    await self.begin_opening()
                else:
                    # 尚未就绪或正在连接，忽略
                    pass

            elif event_type == "audio_start":
                if self.state in (self.STATE_OPENING, self.STATE_READY):
                    self.stream_id = event.get("stream_id", 0)
                    self.state = self.STATE_STREAMING
                    self.audio_frames = []
                    print(f">> 麦克风已开启 (AUDIO_START)，流 ID={self.stream_id}，正在录音...")

            elif event_type == "audio_stop":
                # 关键防护：只有在正在录音 (STREAMING 或 OPENING) 时才响应！
                # 在空闲状态下接收到的多余 STOP 通知直接忽略，彻底杜绝刷屏与空文件。
                if self.state not in (self.STATE_STREAMING, self.STATE_OPENING):
                    return

                print(f">> 遥控器松开语音键 (AUDIO_STOP, reason={event.get('reason')})")
                await self.finalize_recording(reason="remote-release")

            elif event_type == "audio_sync":
                self.protocol.decoder.reset(event["predictor"], event["step_index"])

    def on_audio_notification(self, sender, data: bytearray):
        # 仅在正在开麦或流式传输状态下收集音频
        if self.state in (self.STATE_STREAMING, self.STATE_OPENING):
            self.audio_frames.append(bytes(data))

    async def begin_opening(self):
        try:
            self.audio_frames = []
            self.state = self.STATE_OPENING
            cmd = self.protocol.mic_open_command()
            await self.client.write_gatt_char(ATVVProtocol.TX_UUID, cmd, response=True)
            print(">> 按下语音键 (START_SEARCH) -> 已发送 MIC_OPEN 命令")
        except Exception as e:
            print(f"打开麦克风异常: {e}")
            self.state = self.STATE_READY

    async def finalize_recording(self, reason: str):
        # 立即切回 READY 状态，阻断重复处理
        self.state = self.STATE_READY
        captured_frames = self.audio_frames
        self.audio_frames = []

        # 异步通知遥控器关闭麦克风
        try:
            cmd = self.protocol.mic_close_command(self.stream_id)
            await self.client.write_gatt_char(ATVVProtocol.TX_UUID, cmd, response=True)
        except Exception:
            pass

        if not captured_frames:
            print(">> 录音结束（未收到音频数据帧，已忽略）")
            return

        # 解码所有收到的音频帧
        all_samples = []
        for frame in captured_frames:
            res = self.protocol.decode_audio(frame)
            if res:
                _, samples = res
                all_samples.extend(samples)

        sample_rate = 16000
        # 过滤过短的误触按压 (< 0.25 秒)
        if len(all_samples) < sample_rate // 4:
            print(f">> 录音时间过短 ({len(all_samples)} 采样点 < 0.25s)，已忽略")
            return

        duration = len(all_samples) / sample_rate
        rms = AudioPipeline.root_mean_square(all_samples)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_path = os.path.join(self.recordings_dir, f"speech_{timestamp}.wav")
        AudioPipeline.write_wav(all_samples, sample_rate, wav_path)

        print("--------------------------------------------------")
        print(f">> [语音录制成功！]")
        print(f"   时长: {duration:.2f} 秒")
        print(f"   采样点数: {len(all_samples)}")
        print(f"   音量响度 (RMS): {rms:.1f}")
        print(f"   文件已保存至: {wav_path}")
        print("--------------------------------------------------\n")
        print(">> 遥控器已重回就绪状态：可再次【按住语音键】说话，或按 Ctrl+C 退出。\n")

    async def run(self):
        device = await self.find_device()
        print(f"正在连接遥控器 GATT 语音服务 [{device.address}]...")
        async with BleakClient(device, timeout=10.0) as client:
            self.client = client
            print(f"已成功建立 BLE 连接到 {device.name} [{device.address}]！")

            # 订阅 Control 和 RX
            print("正在订阅 ATVV Control 与 RX 音频特征...")
            await client.start_notify(ATVVProtocol.CONTROL_UUID, self.on_control_notification)
            await client.start_notify(ATVVProtocol.RX_UUID, self.on_audio_notification)

            # 发送能力协商请求 GET_CAPS
            print("正在向遥控器发送能力协商请求 (GET_CAPS)...")
            await client.write_gatt_char(ATVVProtocol.TX_UUID, ATVVProtocol.GET_CAPS_COMMAND, response=True)

            print("等待能力协商完成...")
            while True:
                await asyncio.sleep(1)

def main():
    target = None
    if len(sys.argv) > 1:
        target = sys.argv[1]
    bridge = BLEVoiceBridge(target)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\n已安全退出语音桥接。")
    except Exception as e:
        print(f"\n运行异常: {e}")

if __name__ == "__main__":
    main()
