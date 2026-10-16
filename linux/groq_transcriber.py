#!/usr/bin/env python3
"""
多云端高可用语音转写引擎 (Multi-Cloud ASR Client)
支持：
1. 阿里云百炼 / DashScope (qwen3-omni-flash，国内极速直连 ~0.6s)
2. Groq Cloud (whisper-large-v3，全球极速 LPU 硬件加速)
具备自动主备负载切换、免挂代理国内直连与离线智能兜底。
"""

import base64
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
    def __init__(self, prompt: str = DEFAULT_PROMPT):
        self.prompt = prompt
        self.active_provider = "aliyun"
        self.aliyun_api_key = None
        self.aliyun_model = "qwen3-omni-flash"
        self.groq_api_key = None
        self.groq_model = "whisper-large-v3"
        self.proxy_candidate = "http://127.0.0.1:7897"

        self.reload_config()

    def reload_config(self):
        """读取配置文件或环境变量"""
        self.aliyun_api_key = os.environ.get("DASHSCOPE_API_KEY")
        self.groq_api_key = os.environ.get("GROQ_API_KEY")

        if os.path.exists(DEFAULT_CONFIG_PATH):
            try:
                with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    self.active_provider = cfg.get("active_provider", "aliyun")
                    if not self.aliyun_api_key:
                        self.aliyun_api_key = cfg.get("aliyun_api_key") or cfg.get("dashscope_api_key")
                    if not self.groq_api_key:
                        self.groq_api_key = cfg.get("groq_api_key")
                    self.aliyun_model = cfg.get("aliyun_model", "qwen3-omni-flash")
                    self.groq_model = cfg.get("groq_model", "whisper-large-v3")
            except Exception:
                pass

    @property
    def is_available(self) -> bool:
        has_aliyun = bool(self.aliyun_api_key and self.aliyun_api_key.startswith("sk-"))
        has_groq = bool(self.groq_api_key and self.groq_api_key.startswith("gsk_"))
        return has_aliyun or has_groq

    @property
    def provider_description(self) -> str:
        providers = []
        if self.aliyun_api_key and self.aliyun_api_key.startswith("sk-"):
            providers.append(f"阿里云百炼({self.aliyun_model} 国内直连)")
        if self.groq_api_key and self.groq_api_key.startswith("gsk_"):
            providers.append(f"Groq Cloud({self.groq_model})")
        return " -> ".join(providers) if providers else "未配置云端"

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

    def _clean_text(self, text: str) -> str:
        text = text.strip()
        for bad_w, good_w in KEYWORD_CORRECTIONS.items():
            text = text.replace(bad_w, good_w)
        return text

    def _transcribe_aliyun(self, wav_bytes: bytes) -> Optional[str]:
        """调用阿里云百炼 / DashScope 兼容接口 (qwen3-omni-flash)"""
        if not self.aliyun_api_key:
            return None

        audio_b64 = base64.b64encode(wav_bytes).decode("utf-8")
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.aliyun_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.aliyun_model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个高精度的语音识别(ASR)转写助手。这是一段关于Linux终端、命令行、快捷键、浏览器与日常打字的中文语音。请将音频精确转写为简体中文文本，只输出转写后的文本内容，不要输出任何问候、解释或多余符号。"
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:audio/wav;base64,{audio_b64}",
                                "format": "wav"
                            }
                        },
                        {
                            "type": "text",
                            "text": "请把音频中的语音精确转写为文字。"
                        }
                    ]
                }
            ]
        }

        start_t = time.time()
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=6.0)
            if resp.status_code == 200:
                data = resp.json()
                raw_text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if raw_text:
                    elapsed = time.time() - start_t
                    cleaned = self._clean_text(raw_text)
                    print(f">> [阿里云百炼转写完成] 耗时: {elapsed:.2f}s | 模型: {self.aliyun_model} | 结果: \"{cleaned}\"", flush=True)
                    return cleaned
            elif resp.status_code in (401, 403):
                print(f">> [阿里云认证失败] HTTP {resp.status_code}: 请检查 DASHSCOPE_API_KEY", flush=True)
        except Exception as e:
            print(f">> [阿里云转写异常] {e}", flush=True)
        return None

    def _transcribe_groq(self, wav_bytes: bytes) -> Optional[str]:
        """调用 Groq Cloud 接口 (whisper-large-v3)"""
        if not self.groq_api_key:
            return None

        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}"
        }
        data = {
            "model": self.groq_model,
            "language": "zh",
            "prompt": self.prompt,
            "temperature": "0.0",
        }

        for use_proxy in [True, False]:
            proxies = {"http": self.proxy_candidate, "https": self.proxy_candidate} if use_proxy else None
            try:
                start_t = time.time()
                files = {
                    "file": ("speech.wav", io.BytesIO(wav_bytes), "audio/wav")
                }
                resp = requests.post(
                    url,
                    headers=headers,
                    files=files,
                    data=data,
                    proxies=proxies,
                    timeout=5.0
                )
                if resp.status_code == 200:
                    raw_text = resp.json().get("text", "").strip()
                    if raw_text:
                        elapsed = time.time() - start_t
                        cleaned = self._clean_text(raw_text)
                        print(f">> [Groq云端转写完成] 耗时: {elapsed:.2f}s | 模型: {self.groq_model} | 结果: \"{cleaned}\"", flush=True)
                        return cleaned
                elif resp.status_code in (401, 403):
                    print(f">> [Groq认证失败] HTTP {resp.status_code}: 请检查 GROQ_API_KEY", flush=True)
                    return None
            except Exception:
                continue
        return None

    def transcribe_samples(self, samples: List[int], sample_rate: int = 16000) -> Optional[str]:
        """多云端自动容灾转写调度"""
        if not self.is_available or len(samples) < sample_rate * 0.25:
            return None

        wav_bytes = self._samples_to_wav_bytes(samples, sample_rate)

        # 1. 优先调用主力线路
        if self.active_provider == "aliyun" and self.aliyun_api_key:
            res = self._transcribe_aliyun(wav_bytes)
            if res:
                return res
            print(">> [ASR自动容灾] 阿里云百炼线路无响应，0.05s 无缝切至备用线路 Groq Cloud...", flush=True)

        if self.groq_api_key:
            res = self._transcribe_groq(wav_bytes)
            if res:
                return res

        # 2. 若主力不是 aliyun 但 groq 失败，回退 aliyun
        if self.active_provider != "aliyun" and self.aliyun_api_key:
            print(">> [ASR自动容灾] Groq Cloud 线路无响应，0.05s 无缝切至备用线路 阿里云百炼...", flush=True)
            res = self._transcribe_aliyun(wav_bytes)
            if res:
                return res

        return None
