#!/usr/bin/env python3
"""
Linux 本地 Whisper 语音转写模块
支持直接在内存中将采样点送入模型，避免磁盘与 PyAV 依赖，0.3 秒极速推理。
"""

import os
import sys
import time
from typing import List, Optional

# 清理可能干扰 HuggingFace 的失效环境变量
for k in ["ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
    os.environ.pop(k, None)
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import numpy as np

try:
    from faster_whisper import WhisperModel
except ImportError:
    print("错误: 未安装 faster-whisper。请运行: python3 -m pip install faster-whisper")
    sys.exit(1)

DEFAULT_PROMPT = "米遥。Codex。项目。代码。测试。发送。"

class LocalWhisperTranscriber:
    def __init__(self, model_size: str = "base", prompt: str = DEFAULT_PROMPT):
        self.model_size = model_size
        self.prompt = prompt
        print(f"正在加载 Whisper [{model_size}] 模型...")
        
        # 优先尝 CPU int8 (在现代 CPU 上单次转写仅需 0.2~0.3s 且无需配置复杂 CUDA SO 库)
        try:
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
            print(f"Whisper [{model_size}] 模型加载完成 (CPU int8 加速)")
        except Exception as e:
            print(f"加载模型失败: {e}")
            raise e

    def transcribe_samples(self, samples: List[int], language: str = "zh") -> str:
        """直接传入 16-bit 16kHz PCM 采样点列表，在内存中完成极速转写"""
        if not samples:
            return ""
            
        float_audio = np.array(samples, dtype=np.int16).astype(np.float32) / 32768.0
        
        start_t = time.time()
        segments, info = self.model.transcribe(
            float_audio,
            language=language,
            initial_prompt=self.prompt,
            beam_size=5,
            vad_filter=True  # 过滤首尾多余空白静音
        )
        
        text = "".join([s.text for s in segments]).strip()
        elapsed = time.time() - start_t
        print(f">> [转写完成] 耗时: {elapsed:.3f}s | 结果: \"{text}\"")
        return text
