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
    def __init__(self, base_dir: str = None, uinput_keyboard: Optional[UInput] = None, paste_mode: str = "gui"):
        if not base_dir:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        self.base_dir = base_dir
        self.recordings_dir = os.path.join(base_dir, "recordings")
        os.makedirs(self.recordings_dir, exist_ok=True)

        self.latest_txt_path = os.path.join(self.recordings_dir, "latest_transcript.txt")
        self.history_log_path = os.path.join(self.recordings_dir, "history.jsonl")

        self.ui_kb = uinput_keyboard
        self.paste_mode = paste_mode  # "gui" (Ctrl+V), "terminal" (Ctrl+Shift+V), "universal" (Shift+Insert)

    def set_paste_mode(self, mode: str):
        """设置粘贴按键模式"""
        if mode in ("gui", "terminal", "universal"):
            self.paste_mode = mode

    def set_keyboard_device(self, ui: UInput):
        self.ui_kb = ui

    def copy_to_clipboard(self, text: str) -> bool:
        """非阻塞安全写入系统剪贴板 (支持 Wayland wl-copy 与 X11 xclip)"""
        if not text:
            return False

        # 1. 优先使用 Wayland 原生 wl-copy (开启独立会话与 DEVNULL 避免阻塞)
        if shutil.which("wl-copy"):
            try:
                # 同步写入常规剪贴板 (Ctrl+V) 与 Primary 选择区
                p = subprocess.Popen(
                    ["wl-copy", text],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                p.wait(timeout=0.08)
                # 顺便设置一次 primary selection
                subprocess.Popen(
                    ["wl-copy", "-p", text],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                return True
            except subprocess.TimeoutExpired:
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
        """根据当前模式模拟粘贴按键组合，保持足够按键时长确保 Compositor 与目标应用准确识别"""
        if not self.ui_kb:
            return
        time.sleep(0.015)

        if self.paste_mode == "terminal":
            # 终端模式: 模拟 Ctrl + Shift + V (Linux 终端通用粘贴规范)
            self.ui_kb.write(e.EV_KEY, e.KEY_LEFTCTRL, 1)
            self.ui_kb.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
            self.ui_kb.syn()
            time.sleep(0.01)
            self.ui_kb.write(e.EV_KEY, e.KEY_V, 1)
            self.ui_kb.syn()
            time.sleep(0.035)
            self.ui_kb.write(e.EV_KEY, e.KEY_V, 0)
            self.ui_kb.syn()
            time.sleep(0.01)
            self.ui_kb.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
            self.ui_kb.write(e.EV_KEY, e.KEY_LEFTCTRL, 0)
            self.ui_kb.syn()
        elif self.paste_mode == "universal":
            # 通用模式: 模拟 Shift + Insert (经典终端与 CUA 规范)
            self.ui_kb.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
            self.ui_kb.syn()
            time.sleep(0.01)
            self.ui_kb.write(e.EV_KEY, e.KEY_INSERT, 1)
            self.ui_kb.syn()
            time.sleep(0.035)
            self.ui_kb.write(e.EV_KEY, e.KEY_INSERT, 0)
            self.ui_kb.syn()
            time.sleep(0.01)
            self.ui_kb.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
            self.ui_kb.syn()
        else:
            # 标准 GUI 模式: 模拟 Ctrl + V (Chrome / 浏览器 / 微信 / 编辑器等)
            self.ui_kb.write(e.EV_KEY, e.KEY_LEFTCTRL, 1)
            self.ui_kb.syn()
            time.sleep(0.01)
            self.ui_kb.write(e.EV_KEY, e.KEY_V, 1)
            self.ui_kb.syn()
            time.sleep(0.035)
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
                time.sleep(0.004)
                self.ui_kb.write(e.EV_KEY, e.KEY_BACKSPACE, 0)
                self.ui_kb.syn()
                time.sleep(0.004)

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
        time.sleep(0.035)
        self.ui_kb.write(e.EV_KEY, e.KEY_ENTER, 0)
        self.ui_kb.syn()

    def send_esc(self):
        """模拟按下 ESC 键 (用于取消)"""
        if not self.ui_kb:
            return
        self.ui_kb.write(e.EV_KEY, e.KEY_ESC, 1)
        self.ui_kb.syn()
        time.sleep(0.035)
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
