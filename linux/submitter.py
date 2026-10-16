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

    def send_key(self, keycode: int, duration: float = 0.035):
        """发送单键按下与释放"""
        if not self.ui_kb:
            return
        self.ui_kb.write(e.EV_KEY, keycode, 1)
        self.ui_kb.syn()
        time.sleep(duration)
        self.ui_kb.write(e.EV_KEY, keycode, 0)
        self.ui_kb.syn()

    def send_combo(self, *keycodes: int, duration: float = 0.035):
        """
        发送组合键（按顺序依次按下所有修饰键与主键，随后按相反顺序依次释放）
        例如: send_combo(e.KEY_LEFTCTRL, e.KEY_C)
        """
        if not self.ui_kb or not keycodes:
            return
        for kc in keycodes:
            self.ui_kb.write(e.EV_KEY, kc, 1)
            self.ui_kb.syn()
            time.sleep(0.01)
        time.sleep(duration)
        for kc in reversed(keycodes):
            self.ui_kb.write(e.EV_KEY, kc, 0)
            self.ui_kb.syn()
            time.sleep(0.005)

    def send_enter(self):
        """模拟按下 Enter 回车键 (用于确认发送/执行命令)"""
        self.send_key(e.KEY_ENTER)

    def send_shift_enter(self):
        """模拟按下 Shift + Enter (换行不发送，适用于ChatGPT/微信/聊天框)"""
        self.send_combo(e.KEY_LEFTSHIFT, e.KEY_ENTER)

    def send_esc(self):
        """模拟按下 ESC 键 (用于取消/退出全屏/Vim常规模式)"""
        self.send_key(e.KEY_ESC)

    def send_backspace(self):
        """模拟按下 Backspace 退格键 (用于回退删除字符)"""
        self.send_key(e.KEY_BACKSPACE)

    def send_ctrl_c(self):
        """模拟按下 Ctrl + C (终端中断当前命令/终止任务)"""
        self.send_combo(e.KEY_LEFTCTRL, e.KEY_C)

    def send_ctrl_u(self):
        """模拟按下 Ctrl + U (终端清空整行命令)"""
        self.send_combo(e.KEY_LEFTCTRL, e.KEY_U)

    def send_ctrl_l(self):
        """模拟按下 Ctrl + L (终端一键清屏)"""
        self.send_combo(e.KEY_LEFTCTRL, e.KEY_L)

    def send_ctrl_z(self):
        """模拟按下 Ctrl + Z (撤销上次输入)"""
        self.send_combo(e.KEY_LEFTCTRL, e.KEY_Z)

    def send_ctrl_a(self):
        """模拟按下 Ctrl + A (全选当前输入框内容)"""
        self.send_combo(e.KEY_LEFTCTRL, e.KEY_A)

    def send_ctrl_r(self):
        """模拟按下 Ctrl + R (终端反向历史搜索)"""
        self.send_combo(e.KEY_LEFTCTRL, e.KEY_R)

    def send_alt_tab(self):
        """模拟按下 Alt + Tab (快速切换前台应用)"""
        self.send_combo(e.KEY_LEFTALT, e.KEY_TAB, duration=0.05)

    def send_super_d(self):
        """模拟按下 Super + D (快速回到桌面/最小化所有窗口)"""
        self.send_combo(e.KEY_LEFTMETA, e.KEY_D)

    def send_super_key(self):
        """模拟按下 Super 键 (呼出应用启动器 / GNOME 搜索)"""
        self.send_key(e.KEY_LEFTMETA)

    def send_tab(self):
        """模拟按下 Tab 键 (命令行自动补全 / 表单跳格)"""
        self.send_key(e.KEY_TAB)

    def send_space(self):
        """模拟按下 Space 空格键 (PPT 下一页 / 视频暂停)"""
        self.send_key(e.KEY_SPACE)

    def send_page_up(self):
        """模拟按下 PageUp (向上翻页)"""
        self.send_key(e.KEY_PAGEUP)

    def send_page_down(self):
        """模拟按下 PageDown (向下翻页)"""
        self.send_key(e.KEY_PAGEDOWN)

    def send_shift_page_up(self):
        """模拟按下 Shift + PageUp (终端缓冲区向上翻屏)"""
        self.send_combo(e.KEY_LEFTSHIFT, e.KEY_PAGEUP)

    def send_shift_page_down(self):
        """模拟按下 Shift + PageDown (终端缓冲区向下翻屏)"""
        self.send_combo(e.KEY_LEFTSHIFT, e.KEY_PAGEDOWN)

    def send_next_terminal_tab(self):
        """模拟按下 Ctrl + PageDown (同一终端内向后切换标签页)"""
        self.send_combo(e.KEY_LEFTCTRL, e.KEY_PAGEDOWN)

    def send_new_terminal_tab(self):
        """模拟按下 Ctrl + Shift + T (终端新建标签页)"""
        self.send_combo(e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_T)

    def send_switch_terminal_window(self):
        """模拟按下 Alt + ` (在不同终端独立窗口之间快速切换)"""
        self.send_combo(e.KEY_LEFTALT, e.KEY_GRAVE, duration=0.05)

    def send_new_terminal_window(self):
        """模拟按下 Ctrl + Shift + N (新建终端独立窗口)"""
        self.send_combo(e.KEY_LEFTCTRL, e.KEY_LEFTSHIFT, e.KEY_N)

    def send_f5(self):
        """模拟按下 F5 (PPT 从头放映 / 网页刷新)"""
        self.send_key(e.KEY_F5)

    def send_shift_f5(self):
        """模拟按下 Shift + F5 (PPT 从当前页放映)"""
        self.send_combo(e.KEY_LEFTSHIFT, e.KEY_F5)

    def send_f11(self):
        """模拟按下 F11 (全屏切换)"""
        self.send_key(e.KEY_F11)

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
