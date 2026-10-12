#!/usr/bin/env python3
"""
Google ATV Voice over BLE (ATVV) 协议解析与 ADPCM 音频处理流水线
基于米遥 (MI-AO) 官方 Swift 逻辑 1:1 移植至 Linux / Python。
"""

import math
import struct
from typing import Optional, Tuple, List

# IMA/DVI ADPCM 静态查找表
INDEX_TABLE = [
    -1, -1, -1, -1, 2, 4, 6, 8,
]

STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2494, 2740, 3008, 3307, 3638, 4002, 4402, 4842, 5327,
    5860, 6446, 7091, 7800, 8580, 9438, 10382, 11420, 12562, 13818,
    15200, 16720, 18392, 20231, 22254, 24479, 26927, 29620, 32767,
]

class ADPCMDecoder:
    def __init__(self):
        self.predictor = 0
        self.step_index = 0

    def reset(self, predictor: int = 0, step_index: int = 0):
        self.predictor = predictor
        self.step_index = min(88, max(0, step_index))

    def decode_nibble(self, nibble: int) -> int:
        step = STEP_TABLE[self.step_index]
        diff = step >> 3
        if nibble & 4:
            diff += step
        if nibble & 2:
            diff += step >> 1
        if nibble & 1:
            diff += step >> 2

        if nibble & 8:
            self.predictor -= diff
        else:
            self.predictor += diff

        # 截断到 16-bit 有符号整数范围 [-32768, 32767]
        self.predictor = max(-32768, min(32767, self.predictor))

        self.step_index += INDEX_TABLE[nibble & 7]
        self.step_index = max(0, min(88, self.step_index))
        return self.predictor

    def decode(self, data: bytes) -> List[int]:
        samples = []
        for b in data:
            # 高 4 位优先
            samples.append(self.decode_nibble((b >> 4) & 0x0F))
            # 低 4 位
            samples.append(self.decode_nibble(b & 0x0F))
        return samples


class ATVVCapabilities:
    def __init__(self, version: str, codecs: int, interaction_model: int, frame_size: int):
        self.version = version  # "0.4" 或 "1.0"
        self.codecs = codecs
        self.interaction_model = interaction_model
        self.frame_size = frame_size

    @property
    def selected_codec(self) -> Optional[str]:
        if self.codecs & 0x02 != 0:
            return "adpcm16k"
        if self.codecs & 0x01 != 0:
            return "adpcm8k"
        return None

    def __eq__(self, other):
        if not isinstance(other, ATVVCapabilities):
            return False
        return (self.version == other.version and
                self.codecs == other.codecs and
                self.interaction_model == other.interaction_model and
                self.frame_size == other.frame_size)

    def __repr__(self):
        return f"ATVVCapabilities(version={self.version}, codecs=0x{self.codecs:02x}, frame_size={self.frame_size})"


class ATVVProtocol:
    SERVICE_UUID = "ab5e0001-5a21-4f05-bc7d-af01f617b664"
    TX_UUID      = "ab5e0002-5a21-4f05-bc7d-af01f617b664"
    RX_UUID      = "ab5e0003-5a21-4f05-bc7d-af01f617b664"
    CONTROL_UUID = "ab5e0004-5a21-4f05-bc7d-af01f617b664"

    # 控制命令
    GET_CAPS_COMMAND = bytes([0x0A, 0x01, 0x00, 0x00, 0x03, 0x03])

    def __init__(self):
        self.capabilities: Optional[ATVVCapabilities] = None
        self.codec: Optional[str] = None
        self.decoder = ADPCMDecoder()
        self.v10_sequence = 0

    def accept_capabilities(self, capabilities: ATVVCapabilities):
        selected = capabilities.selected_codec
        if not selected:
            raise ValueError("遥控器不支持 ADPCM 8/16 kHz")
        self.capabilities = capabilities
        self.codec = selected
        self.decoder.reset(0, 0)
        self.v10_sequence = 0

    def mic_open_command(self) -> bytes:
        if not self.capabilities or not self.codec:
            raise ValueError("尚未完成 ATVV capabilities 协商")
        if self.capabilities.version == "0.4":
            codec_val = 0x02 if self.codec == "adpcm16k" else 0x01
            return bytes([0x0C, 0x00, codec_val])
        else:
            return bytes([0x0C, 0x00])

    def mic_close_command(self, stream_id: int = 0) -> bytes:
        if not self.capabilities:
            raise ValueError("尚未完成 ATVV capabilities 协商")
        if self.capabilities.version == "0.4":
            return bytes([0x0D])
        else:
            return bytes([0x0D, stream_id & 0xFF])

    @staticmethod
    def parse_capabilities(data: bytes) -> Optional[ATVVCapabilities]:
        if len(data) < 3 or data[0] != 0x0B:
            return None
        version_num = (data[1] << 8) | data[2]
        if version_num == 0x0004:
            if len(data) < 9:
                return None
            frame_size = (data[5] << 8) | data[6]
            return ATVVCapabilities("0.4", data[4], 0, frame_size)
        elif version_num == 0x0100:
            if len(data) < 7:
                return None
            std_codecs = data[3]
            std_interaction = data[4]
            # 小米遥控器固件 2671 偶现字段对调的兼容处理
            has_std = (std_codecs & 0x03) != 0
            has_swapped = (std_interaction & 0x03) != 0
            if not has_std and has_swapped:
                codecs = std_interaction
                interaction = std_codecs
            else:
                codecs = std_codecs
                interaction = std_interaction
            frame_size = (data[5] << 8) | data[6]
            return ATVVCapabilities("1.0", codecs, interaction, frame_size)
        return None

    def parse_control(self, data: bytes) -> dict:
        if not data:
            return {"type": "unknown", "raw": data}
        opcode = data[0]
        if opcode == 0x00:
            reason = data[1] if len(data) > 1 else 0
            return {"type": "audio_stop", "reason": reason}
        elif opcode == 0x04:
            if self.capabilities and self.capabilities.version == "1.0" and len(data) >= 4:
                codec = "adpcm16k" if data[2] == 0x02 else "adpcm8k"
                return {"type": "audio_start", "reason": data[1], "codec": codec, "stream_id": data[3]}
            return {"type": "audio_start", "reason": 0, "codec": self.codec or "adpcm16k", "stream_id": 0}
        elif opcode == 0x08:
            return {"type": "start_search"}
        elif opcode == 0x0A:
            if self.capabilities and self.capabilities.version == "1.0" and len(data) >= 7:
                codec = "adpcm16k" if data[1] == 0x02 else "adpcm8k"
                seq = (data[2] << 8) | data[3]
                pred_bits = (data[4] << 8) | data[5]
                # 转换为有符号 16-bit
                predictor = struct.unpack(">h", struct.pack(">H", pred_bits))[0]
                step_idx = data[6]
                return {
                    "type": "audio_sync",
                    "codec": codec,
                    "sequence": seq,
                    "predictor": predictor,
                    "step_index": step_idx
                }
        elif opcode == 0x0B:
            caps = self.parse_capabilities(data)
            if caps:
                return {"type": "capabilities", "capabilities": caps}
        return {"type": "unknown", "raw": data}

    def decode_audio(self, data: bytes) -> Optional[Tuple[int, List[int]]]:
        if not self.capabilities or not self.codec or not data:
            return None
        if self.capabilities.version == "0.4":
            if len(data) != self.capabilities.frame_size or len(data) < 6:
                return None
            seq = (data[0] << 8) | data[1]
            pred_bits = (data[3] << 8) | data[4]
            predictor = struct.unpack(">h", struct.pack(">H", pred_bits))[0]
            self.decoder.reset(predictor, data[5])
            samples = [predictor] + self.decoder.decode(data[6:])
            return seq, samples
        else:
            seq = self.v10_sequence
            self.v10_sequence = (self.v10_sequence + 1) & 0xFFFF
            return seq, self.decoder.decode(data)


class AudioPipeline:
    @staticmethod
    def root_mean_square(samples: List[int]) -> float:
        if not samples:
            return 0.0
        power = sum(s * s for s in samples)
        return math.sqrt(power / len(samples))

    @staticmethod
    def prepare_for_whisper(samples: List[int], sample_rate: int, gain_db: float = 0.0) -> List[int]:
        gain = 10.0 ** (gain_db / 20.0)
        amplified = []
        for s in samples:
            val = round(s * gain)
            amplified.append(max(-32768, min(32767, int(val))))

        if sample_rate != 16000 and len(amplified) > 1:
            return AudioPipeline.linear_resample(amplified, sample_rate, 16000)
        return amplified

    @staticmethod
    def linear_resample(input_samples: List[int], source_rate: int, dest_rate: int) -> List[int]:
        if source_rate <= 0 or dest_rate <= 0 or len(input_samples) <= 1:
            return input_samples
        ratio = source_rate / dest_rate
        output_count = round(len(input_samples) * dest_rate / source_rate)
        output = []
        for i in range(output_count):
            src_pos = i * ratio
            low = min(len(input_samples) - 1, int(src_pos))
            high = min(len(input_samples) - 1, low + 1)
            frac = src_pos - low
            val = round(input_samples[low] * (1.0 - frac) + input_samples[high] * frac)
            output.append(max(-32768, min(32767, int(val))))
        return output

    @staticmethod
    def write_wav(samples: List[int], sample_rate: int, filename: str):
        data_bytes = struct.pack(f"<{len(samples)}h", *samples)
        data_size = len(data_bytes)
        # 构造标准 RIFF WAVE 头 (44 字节)
        header = bytearray()
        header.extend(b"RIFF")
        header.extend(struct.pack("<I", 36 + data_size))
        header.extend(b"WAVE")
        header.extend(b"fmt ")
        header.extend(struct.pack("<I", 16))          # Subchunk1Size
        header.extend(struct.pack("<H", 1))           # AudioFormat (PCM)
        header.extend(struct.pack("<H", 1))           # NumChannels (Mono)
        header.extend(struct.pack("<I", sample_rate)) # SampleRate
        header.extend(struct.pack("<I", sample_rate * 2)) # ByteRate (16-bit mono = 2 bytes/sec)
        header.extend(struct.pack("<H", 2))           # BlockAlign
        header.extend(struct.pack("<H", 16))          # BitsPerSample
        header.extend(b"data")
        header.extend(struct.pack("<I", data_size))
        
        with open(filename, "wb") as f:
            f.write(header)
            f.write(data_bytes)


if __name__ == "__main__":
    # 单元测试自检
    print("运行 ATVVProtocol 与音频流水线自测...")
    
    # 测试 1: 小米固件 2671 capabilities
    caps_data = bytes([0x0B, 0x01, 0x00, 0x00, 0x03, 0x00, 0x78, 0x00, 0x00])
    caps = ATVVProtocol.parse_capabilities(caps_data)
    assert caps is not None
    assert caps.version == "1.0"
    assert caps.codecs == 0x03
    assert caps.frame_size == 120
    assert caps.selected_codec == "adpcm16k"
    print("  [通过] 小米固件 2671 Capabilities 解析正常")

    # 测试 2: 线性重采样
    input_smps = [0, 100, 200, 300]
    out_smps = AudioPipeline.linear_resample(input_smps, 8000, 16000)
    assert len(out_smps) == 8
    assert out_smps[0] == 0
    print("  [通过] 线性重采样算法正常")

    # 测试 3: ADPCM 解码与静音帧
    proto = ATVVProtocol()
    proto.accept_capabilities(caps)
    silent_frame = bytes(120)
    seq, decoded = proto.decode_audio(silent_frame)
    assert seq == 0
    assert len(decoded) == 240  # 120 字节 = 240 个 4-bit nibbles
    print("  [通过] ADPCM 音频帧解码正常")
    print("所有自测通过！")
