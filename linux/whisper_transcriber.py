#!/usr/bin/env python3
"""
Linux 本地 Whisper 极速双阶语音转写引擎
1. 流式低延迟转写 (base 模型, ~0.2s 推理)：在用户说话期间实时输出文字到打字框
2. 终态高精度转写 (small 模型, 极高准确率)：松手后对整句进行语义校准与润色
"""

import os
import sys
import time
from typing import List, Optional

# 清理可能干扰 HuggingFace 的失效代理环境变量
for k in ["ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
    os.environ.pop(k, None)
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import numpy as np

try:
    from faster_whisper import WhisperModel
except ImportError:
    print("错误: 未安装 faster-whisper。请运行: python3 -m pip install faster-whisper")
    sys.exit(1)

DEFAULT_PROMPT = "简体中文。"

class LocalWhisperTranscriber:
    def __init__(self, model_size: str = "small", prompt: str = DEFAULT_PROMPT):
        self.final_model_name = model_size
        self.stream_model_name = "base"
        self.prompt = prompt

        print(f"正在加载 Whisper 语音模型 [流式: {self.stream_model_name}, 终态: {self.final_model_name}]...", flush=True)
        t0 = time.time()
        
        # 流式快速模型 (base, CPU int8 加速，延迟约 0.2s)
        self.stream_model = WhisperModel(self.stream_model_name, device="cpu", compute_type="int8")
        
        # 终态高精度模型
        if self.final_model_name == self.stream_model_name:
            self.final_model = self.stream_model
        else:
            self.final_model = WhisperModel(self.final_model_name, device="cpu", compute_type="int8")

        # 启动预热 (消除第一次识别的冷启动延迟)
        dummy_audio = np.zeros(1600, dtype=np.float32)  # 0.1s 静音
        try:
            list(self.stream_model.transcribe(dummy_audio, language="zh", beam_size=1))
            if self.final_model != self.stream_model:
                list(self.final_model.transcribe(dummy_audio, language="zh", beam_size=1))
        except Exception:
            pass

        t1 = time.time()
        print(f"Whisper 双阶模型加载与预热完成 (耗时 {t1-t0:.2f}s, CPU int8)", flush=True)

    def _is_hallucination(self, text: str) -> bool:
        if not text:
            return True
        # 连续重复单字过滤 (如 "语语语语语...")
        for i in range(len(text) - 2):
            if text[i] == text[i+1] == text[i+2]:
                return True
        # 常见 Whisper 纯幻觉关键词
        if text in ["发送。", "发送。发送。", "测试。发送。", "发送", "谢谢。"]:
            return True
        return False

    def transcribe_stream_samples(self, samples: List[int]) -> str:
        """流式阶段快速推断（低 beam_size，优先响应速度，至少 0.6s 音频）"""
        if not samples or len(samples) < 16000 * 0.6:
            return ""

        float_audio = np.array(samples, dtype=np.int16).astype(np.float32) / 32768.0

        try:
            segments, _ = self.stream_model.transcribe(
                float_audio,
                language="zh",
                initial_prompt=self.prompt,
                beam_size=1,
                condition_on_previous_text=False,
                compression_ratio_threshold=2.2,
                no_speech_threshold=0.5,
                vad_filter=True
            )
            raw_text = "".join([s.text for s in segments]).strip()
            # 流式过程中去掉末尾句号，保证视觉输入连贯
            raw_text = raw_text.rstrip("。，,. ")
            if self._is_hallucination(raw_text):
                return ""
            return raw_text
        except Exception as e:
            return ""

    def transcribe_final_samples(self, samples: List[int]) -> str:
        """终态阶段高精度推断（更高精度模型与 beam_size）"""
        if not samples or len(samples) < 16000 * 0.25:
            return ""

        float_audio = np.array(samples, dtype=np.int16).astype(np.float32) / 32768.0
        start_t = time.time()

        try:
            segments, _ = self.final_model.transcribe(
                float_audio,
                language="zh",
                initial_prompt=self.prompt,
                beam_size=2,
                condition_on_previous_text=False,
                vad_filter=True
            )
            text = "".join([s.text for s in segments]).strip()
            if self._is_hallucination(text):
                text = ""
            elapsed = time.time() - start_t
            if text:
                print(f">> [转写完成] 耗时: {elapsed:.3f}s | 结果: \"{text}\"", flush=True)
            return text
        except Exception as e:
            print(f">> [转写异常] {e}", flush=True)
            return ""
