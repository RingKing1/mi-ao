#!/usr/bin/env python3
"""
Linux 文本提交器 (Text Submitter)
负责将识别到的文本提交给目标程序：
1. 写入本地最新文件 (latest_transcript.txt) 与 JSONL 历史日志
2. 写入系统剪贴板 (优先 wl-copy，其次 xclip，再次 pyperclip)
3. 可选：通过 /dev/uinput 发送快捷键粘贴 (Ctrl+V) 并回车 (Enter)
"""

import os
import sys
import json
import shutil
import subprocess
from datetime import datetime

class LinuxSubmitter:
    def __init__(self, base_dir: str = None, auto_paste: bool = False, auto_enter: bool = False):
        if not base_dir:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        self.base_dir = base_dir
        self.recordings_dir = os.path.join(base_dir, "recordings")
        os.makedirs(self.recordings_dir, exist_ok=True)
        
        self.latest_txt_path = os.path.join(self.recordings_dir, "latest_transcript.txt")
        self.history_log_path = os.path.join(self.recordings_dir, "history.jsonl")
        
        self.auto_paste = auto_paste
        self.auto_enter = auto_enter
        
        self.uinput_device = None
        if self.auto_paste or self.auto_enter:
            self._init_uinput()

    def _init_uinput(self):
        try:
            import evdev
            from evdev import UInput, ecodes as e
            capabilities = {
                e.EV_KEY: [e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_V, e.KEY_ENTER]
            }
            self.uinput_device = UInput(capabilities, name="Mi-Ao-Submitter", version=0x1)
        except Exception as e:
            # print(f"初始化虚拟按键失败 (需权限): {e}")
            self.uinput_device = None

    def copy_to_clipboard(self, text: str) -> bool:
        """尝试将文本复制到剪贴板 (Wayland / X11)"""
        # 1. 优先使用 Wayland 原生 wl-copy
        if shutil.which("wl-copy"):
            try:
                proc = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE)
                proc.communicate(input=text.encode("utf-8"))
                return True
            except Exception:
                pass

        # 2. 其次使用 X11 xclip
        if shutil.which("xclip"):
            try:
                proc = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE)
                proc.communicate(input=text.encode("utf-8"))
                return True
            except Exception:
                pass

        # 3. 降级使用 pyperclip
        try:
            import pyperclip
            pyperclip.copy(text)
            return True
        except Exception:
            pass

        return False

    def paste_and_enter(self):
        """通过虚拟键盘模拟粘贴与回车"""
        if not self.uinput_device:
            return
        import time
        from evdev import ecodes as e
        
        time.sleep(0.15)
        # Ctrl + V
        self.uinput_device.write(e.EV_KEY, e.KEY_LEFTCTRL, 1)
        self.uinput_device.write(e.EV_KEY, e.KEY_V, 1)
        self.uinput_device.syn()
        time.sleep(0.05)
        self.uinput_device.write(e.EV_KEY, e.KEY_V, 0)
        self.uinput_device.write(e.EV_KEY, e.KEY_LEFTCTRL, 0)
        self.uinput_device.syn()
        
        if self.auto_enter:
            time.sleep(0.2)
            # Enter
            self.uinput_device.write(e.EV_KEY, e.KEY_ENTER, 1)
            self.uinput_device.syn()
            time.sleep(0.05)
            self.uinput_device.write(e.EV_KEY, e.KEY_ENTER, 0)
            self.uinput_device.syn()

    def submit(self, text: str, audio_path: str = None) -> dict:
        text = text.strip()
        if not text:
            return {"status": "empty"}

        # 1. 写入最新文件
        with open(self.latest_txt_path, "w", encoding="utf-8") as f:
            f.write(text)

        # 2. 追加到历史记录
        record = {
            "timestamp": datetime.now().isoformat(),
            "text": text,
            "audio_file": audio_path
        }
        with open(self.history_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # 3. 复制到剪贴板
        clipboard_ok = self.copy_to_clipboard(text)

        # 4. 模拟按键（如果启用）
        if self.auto_paste:
            self.paste_and_enter()

        return {
            "status": "success",
            "text": text,
            "clipboard": clipboard_ok,
            "latest_file": self.latest_txt_path
        }
