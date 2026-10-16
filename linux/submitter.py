#!/usr/bin/env python3
"""
Linux 文本提交器与输入框实时注入器 (Text Submitter & Real-Time Injector)
负责：
1. 实时文本增量/退格注入当前活动输入框 (通过 uinput 独立键盘与剪贴板 Ctrl+V)
2. 确认键一键发送 (通过 uinput 模拟 Enter 回车)
3. 写入最新文件 (latest_transcript.txt) 与 JSONL 历史日志
4. 同步至系统剪贴板 (Wayland / X11)
"""

import os
import sys
import json
import time
import shutil
import subprocess
from datetime import datetime
from typing import Optional

try:
    import evdev
    from evdev import UInput, ecodes as e
except ImportError:
    pass

class LinuxSubmitter:
    def __init__(self, base_dir: str = None, uinput_keyboard: Optional[UInput] = None):
        if not base_dir:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        self.base_dir = base_dir
        self.recordings_dir = os.path.join(base_dir, "recordings")
        os.makedirs(self.recordings_dir, exist_ok=True)

        self.latest_txt_path = os.path.join(self.recordings_dir, "latest_transcript.txt")
        self.history_log_path = os.path.join(self.recordings_dir, "history.jsonl")

        self.ui_kb = uinput_keyboard

    def set_keyboard_device(self, ui: UInput):
        self.ui_kb = ui

    def copy_to_clipboard(self, text: str) -> bool:
        """非阻塞安全写入系统剪贴板 (支持 Wayland wl-copy 与 X11 xclip)"""
        if not text:
            return False

        # 1. 优先使用 Wayland 原生 wl-copy (开启独立会话与 DEVNULL 避免阻塞)
        if shutil.which("wl-copy"):
            try:
                p = subprocess.Popen(
                    ["wl-copy", text],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                p.wait(timeout=0.08)
                return True
            except subprocess.TimeoutExpired:
                # 进程在后台常驻服务剪贴板，属正常情况
                return True
            except Exception:
                pass

        # 2. 其次使用 X11 xclip
        if shutil.which("xclip"):
            try:
                p = subprocess.Popen(
                    ["xclip", "-selection", "clipboard"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                p.communicate(input=text.encode("utf-8"), timeout=0.2)
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

    def paste(self):
        """模拟按下 Ctrl+V 粘贴内容，保持足够保持时间确保 Compositor 识别"""
        if not self.ui_kb:
            return
        time.sleep(0.015)
        # 按下 Ctrl + V
        self.ui_kb.write(e.EV_KEY, e.KEY_LEFTCTRL, 1)
        self.ui_kb.syn()
        time.sleep(0.01)
        self.ui_kb.write(e.EV_KEY, e.KEY_V, 1)
        self.ui_kb.syn()
        time.sleep(0.03)  # 保持 30ms
        self.ui_kb.write(e.EV_KEY, e.KEY_V, 0)
        self.ui_kb.syn()
        time.sleep(0.01)
        self.ui_kb.write(e.EV_KEY, e.KEY_LEFTCTRL, 0)
        self.ui_kb.syn()

    def type_diff(self, backspaces: int, delta: str):
        """
        向当前活动打字框注入差量文字：
        - 如果有需要撤销的字数，快速发送 Backspace 退格键
        - 如果有新文字增量，写入剪贴板并模拟 Ctrl+V 粘贴
        """
        if not self.ui_kb:
            return

        # 1. 处理退格
        if backspaces > 0:
            for _ in range(backspaces):
                self.ui_kb.write(e.EV_KEY, e.KEY_BACKSPACE, 1)
                self.ui_kb.syn()
                time.sleep(0.003)
                self.ui_kb.write(e.EV_KEY, e.KEY_BACKSPACE, 0)
                self.ui_kb.syn()
                time.sleep(0.003)

        # 2. 处理增量文字粘贴
        if delta:
            if self.copy_to_clipboard(delta):
                time.sleep(0.02)  # 给系统剪贴板 20ms 建立通知
                self.paste()

    def send_enter(self):
        """模拟按下 Enter 回车键 (用于确认发送)"""
        if not self.ui_kb:
            return
        self.ui_kb.write(e.EV_KEY, e.KEY_ENTER, 1)
        self.ui_kb.syn()
        time.sleep(0.03)
        self.ui_kb.write(e.EV_KEY, e.KEY_ENTER, 0)
        self.ui_kb.syn()

    def send_esc(self):
        """模拟按下 ESC 键 (用于取消)"""
        if not self.ui_kb:
            return
        self.ui_kb.write(e.EV_KEY, e.KEY_ESC, 1)
        self.ui_kb.syn()
        time.sleep(0.03)
        self.ui_kb.write(e.EV_KEY, e.KEY_ESC, 0)
        self.ui_kb.syn()

    def record_submission(self, text: str, audio_path: str = None) -> dict:
        """归档最终识别文本并保存到历史与剪贴板"""
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

        # 3. 最终全文同步至剪贴板供用户备用
        clipboard_ok = self.copy_to_clipboard(text)

        return {
            "status": "success",
            "text": text,
            "clipboard": clipboard_ok,
            "latest_file": self.latest_txt_path
        }
