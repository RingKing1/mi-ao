#!/usr/bin/env python3
"""
米遥 (MI-AO) Linux 端到端全链路语音服务
链路：小米遥控器 2 Pro (BLE/ATVV) -> ADPCM 解码 -> 本地 Whisper 极速转写 -> 提交 (剪贴板 / 记录 / 终端)
"""

import sys
import os
import time
import asyncio
from datetime import datetime

# 兼容加载用户目录下的 Python 包
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
    print("错误: 未安装 bleak 库。请运行: python3 -m pip install bleak")
    sys.exit(1)

from atvv_protocol import ATVVProtocol, AudioPipeline
from whisper_transcriber import LocalWhisperTranscriber
from submitter import LinuxSubmitter

REMOTE_NAME_KEYWORDS = ["小米", "xiaomi", "remote", "遥控器"]

class MiAoBridge:
    STATE_DISCONNECTED = "disconnected"
    STATE_READY        = "ready"
    STATE_OPENING      = "opening"
    STATE_STREAMING    = "streaming"

    def __init__(self, model_size="base", auto_paste=False, auto_enter=False):
        self.protocol = ATVVProtocol()
        self.state = self.STATE_DISCONNECTED
        self.stream_id = 0
        self.audio_frames = []
        self.client: BleakClient = None
        self.lock = asyncio.Lock()
        
        # 记录目录
        self.recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
        os.makedirs(self.recordings_dir, exist_ok=True)
        
        # 加载 Whisper 引擎与提交器
        self.transcriber = LocalWhisperTranscriber(model_size=model_size)
        self.submitter = LinuxSubmitter(
            base_dir=os.path.dirname(__file__),
            auto_paste=auto_paste,
            auto_enter=auto_enter
        )

    async def find_device(self) -> BLEDevice:
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
        asyncio.create_task(self.handle_control_event(event))

    async def handle_control_event(self, event: dict):
        event_type = event.get("type")

        async with self.lock:
            if event_type == "capabilities":
                caps = event["capabilities"]
                self.protocol.accept_capabilities(caps)
                self.state = self.STATE_READY
                print(f">> [能力协商成功] ATVV v{caps.version}, Codec={self.protocol.codec}")
                print("==================================================")
                print(">> 【米遥 Linux 语音桥接已完全就绪！】")
                print(">> 操作指南: 按住遥控器【语音键】说话，松手立即自动识别并提交！")
                print("==================================================\n")

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
                    print(">> [正在倾听...] 正在接收语音流...")

            elif event_type == "audio_stop":
                if self.state not in (self.STATE_STREAMING, self.STATE_OPENING):
                    return
                print(">> [松开按键] 结束收音，立即开始 Whisper 转写...")
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
        except Exception as e:
            print(f"打开麦克风失败: {e}")
            self.state = self.STATE_READY

    async def finalize_recording(self, reason: str):
        self.state = self.STATE_READY
        captured_frames = self.audio_frames
        self.audio_frames = []

        try:
            cmd = self.protocol.mic_close_command(self.stream_id)
            await self.client.write_gatt_char(ATVVProtocol.TX_UUID, cmd, response=True)
        except Exception:
            pass

        if not captured_frames:
            return

        # 内存解码
        all_samples = []
        for frame in captured_frames:
            res = self.protocol.decode_audio(frame)
            if res:
                _, samples = res
                all_samples.extend(samples)

        sample_rate = 16000
        # 过滤 < 0.25 秒的误触
        if len(all_samples) < sample_rate // 4:
            print(f">> 录音过短 ({len(all_samples)} 采样点 < 0.25s)，已忽略")
            return

        duration = len(all_samples) / sample_rate
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_path = os.path.join(self.recordings_dir, f"speech_{timestamp}.wav")
        AudioPipeline.write_wav(all_samples, sample_rate, wav_path)

        # 本地 Whisper 转写
        transcript = self.transcriber.transcribe_samples(all_samples, language="zh")
        if not transcript:
            print(">> 未能识别出清晰文字\n")
            return

        # 提交结果
        res = self.submitter.submit(transcript, audio_path=wav_path)
        
        print("\n" + "="*50)
        print(f"🎉 语音输入成功:  {transcript}")
        print(f"   音频文件: {os.path.basename(wav_path)} (时长: {duration:.2f}s)")
        print(f"   最新文本: {res.get('latest_file')}")
        if res.get("clipboard"):
            print("   剪贴板: [已成功复制到系统剪贴板]")
        else:
            print("   提示: 若需自动同步到剪贴板，请运行: sudo apt install -y wl-clipboard")
        print("="*50 + "\n")
        print(">> 继续就绪：请再次按住遥控器【语音键】说话...\n")

    async def run(self):
        device = await self.find_device()
        print(f"正在连接遥控器 GATT 语音服务 [{device.address}]...")
        async with BleakClient(device, timeout=10.0) as client:
            self.client = client
            print(f"已成功建立 BLE 连接到 {device.name}！")

            await client.start_notify(ATVVProtocol.CONTROL_UUID, self.on_control_notification)
            await client.start_notify(ATVVProtocol.RX_UUID, self.on_audio_notification)
            await client.write_gatt_char(ATVVProtocol.TX_UUID, ATVVProtocol.GET_CAPS_COMMAND, response=True)

            while True:
                await asyncio.sleep(1)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="米遥 (MI-AO) Linux 全链路语音服务")
    parser.add_argument("--model", default="base", help="Whisper 模型规格 (tiny / base / small)，默认 base")
    parser.add_argument("--auto-paste", action="store_true", help="是否通过虚拟键盘自动触发 Ctrl+V 粘贴")
    parser.add_argument("--auto-enter", action="store_true", help="是否在粘贴后自动触发 Enter 回车发送")
    args = parser.parse_args()

    bridge = MiAoBridge(
        model_size=args.model,
        auto_paste=args.auto_paste,
        auto_enter=args.auto_enter
    )
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\n米遥语音服务已退出。")
    except Exception as e:
        print(f"\n运行异常: {e}")

if __name__ == "__main__":
    main()
