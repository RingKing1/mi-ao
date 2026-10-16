#!/usr/bin/env python3
"""
Groq Cloud Whisper API 转写客户端
基于 Groq LPU 极速硬件运行 OpenAI whisper-large-v3 模型。
支持内存音频直传、自动代理检测与离线故障降级。
"""

import io
import json
import os
import struct
import time
from typing import List, Optional
import requests

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/mi-ao/config.json")
DEFAULT_PROMPT = "这是一段关于操作系统、Linux终端、命令行、快捷键、浏览器、窗口、音量键、退格删除、撤销与文本编辑的中文普通话语音。"

KEYWORD_CORRECTIONS = {
    "中端迷你好": "终端命令行",
    "中端迷你行": "终端命令行",
    "战断迷你行": "终端命令行",
    "中端命令行": "终端命令行",
    "战断命令行": "终端命令行",
    "旧战器": "浏览器",
    "纽纳机": "浏览器",
    "留言节": "浏览器",
    "牛点器": "浏览器",
    "车效": "撤销",
    "车销": "撤销",
    "control力": "Ctrl-Z",
    "裂表": "列表",
}

class GroqCloudTranscriber:
    def __init__(self, api_key: Optional[str] = None, model: str = "whisper-large-v3", prompt: str = DEFAULT_PROMPT):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.model = model
        self.prompt = prompt
        self.url = "https://api.groq.com/openai/v1/audio/transcriptions"

        if not self.api_key and os.path.exists(DEFAULT_CONFIG_PATH):
            try:
                with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    self.api_key = cfg.get("groq_api_key")
                    if "groq_model" in cfg:
                        self.model = cfg["groq_model"]
            except Exception:
                pass

        # 常见本地代理端口检测 (Clash Verge / V2Ray)
        self.proxy_candidate = "http://127.0.0.1:7897"

    @property
    def is_available(self) -> bool:
        return bool(self.api_key and self.api_key.startswith("gsk_"))

    def _samples_to_wav_bytes(self, samples: List[int], sample_rate: int = 16000) -> bytes:
        data_bytes = struct.pack(f"<{len(samples)}h", *samples)
        data_size = len(data_bytes)
        header = bytearray()
        header.extend(b"RIFF")
        header.extend(struct.pack("<I", 36 + data_size))
        header.extend(b"WAVE")
        header.extend(b"fmt ")
        header.extend(struct.pack("<I", 16))
        header.extend(struct.pack("<H", 1))
        header.extend(struct.pack("<H", 1))
        header.extend(struct.pack("<I", sample_rate))
        header.extend(struct.pack("<I", sample_rate * 2))
        header.extend(struct.pack("<H", 2))
        header.extend(struct.pack("<H", 16))
        header.extend(b"data")
        header.extend(struct.pack("<I", data_size))
        return bytes(header) + data_bytes

    def transcribe_samples(self, samples: List[int], sample_rate: int = 16000) -> Optional[str]:
        """将原始 PCM 采样序列直接通过 Groq API 转写为文字"""
        wav_bytes = self._samples_to_wav_bytes(samples, sample_rate)
        data = {
            "model": self.model,
            "language": "zh",
            "prompt": self.prompt,
            "temperature": "0.0",
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}"
        }

        for use_proxy in [True, False]:
            proxies = {"http": self.proxy_candidate, "https": self.proxy_candidate} if use_proxy else None
            try:
                start_t = time.time()
                files = {
                    "file": ("speech.wav", io.BytesIO(wav_bytes), "audio/wav")
                }
                resp = requests.post(
                    self.url,
                    headers=headers,
                    files=files,
                    data=data,
                    proxies=proxies,
                    timeout=5.0
                )
                if resp.status_code == 200:
                    result = resp.json().get("text", "").strip()
                    for bad_w, good_w in KEYWORD_CORRECTIONS.items():
                        result = result.replace(bad_w, good_w)
                    elapsed = time.time() - start_t
                    print(f">> [Groq云端转写完成] 耗时: {elapsed:.2f}s | 模型: {self.model} | 结果: \"{result}\"", flush=True)
                    return result
                elif resp.status_code in (401, 403):
                    print(f">> [Groq认证失败] HTTP {resp.status_code}: 请检查 API Key", flush=True)
                    return None
            except Exception as e:
                continue

        return None
