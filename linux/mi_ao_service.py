#!/usr/bin/env python3
"""
米遥 (MI-AO) Linux 核心整合守护服务
同时集成两大核心链路：
1. BLE 实时语音打字链路：
   - 按住语音键说话：以 ~0.2s 延迟在当前打字框内实时逐字流式涌现，松手终态精准校准。
2. HID 飞鼠与快捷控制链路：
   - 默认【实时打字与确认发送模式】：
     * OK / 确认键：模拟 Enter 回车，一键发送打字框内容！
     * 方向环：平滑移动鼠标指针（按住平滑加速）
     * 返回键：ESC 取消
     * TV 键：在【打字与确认发送】、【飞鼠点击】与【键盘方向键】模式之间切换
"""

import os
import sys
import time
import glob
import shutil
import subprocess
import asyncio
import threading
from datetime import datetime
from typing import Optional, List

# 确保所有终端输出即时刷新到 systemd journal 日志
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

# 兼容加载用户环境包
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
    print("错误: 未安装 bleak 库。请运行: python3 -m pip install bleak", flush=True)
    sys.exit(1)

try:
    import evdev
    from evdev import InputDevice, UInput, ecodes as e
except ImportError:
    print("错误: 未安装 evdev 库。请运行: python3 -m pip install evdev", flush=True)
    sys.exit(1)

from atvv_protocol import ATVVProtocol, AudioPipeline
from whisper_transcriber import LocalWhisperTranscriber
from submitter import LinuxSubmitter

XIAOMI_VENDOR_ID = 0x2717
XIAOMI_PRODUCT_ID = 0x32B8
REMOTE_NAME_KEYWORDS = ["小米", "xiaomi", "remote", "遥控器"]

# 按键代码列表
DPAD_UP = [e.KEY_UP, 103, e.KEY_PAGEUP, 104, 402, 177]
DPAD_DOWN = [e.KEY_DOWN, 108, e.KEY_PAGEDOWN, 109, 403, 178]
DPAD_LEFT = [e.KEY_LEFT, 105, 168]
DPAD_RIGHT = [e.KEY_RIGHT, 106, 208]
CENTER_KEYS = [e.KEY_ENTER, e.KEY_OK, e.KEY_KPENTER, e.KEY_SELECT, 28, 96, 352, 353]
BACK_KEYS = [e.KEY_BACK, e.KEY_ESC, 158, 1]
HOME_KEYS = [e.KEY_HOMEPAGE, e.KEY_HOME, 172, 102]
MENU_KEYS = [e.KEY_MENU, e.KEY_COMPOSE, e.KEY_PROPS, 139, 127, 130]
VOL_UP_KEYS = [e.KEY_VOLUMEUP, 115]
VOL_DOWN_KEYS = [e.KEY_VOLUMEDOWN, 114]
TV_KEYS = [e.KEY_TV, e.KEY_PROG1, e.KEY_GRAVE, 377, 148, 41]
POWER_KEYS = [e.KEY_POWER, e.KEY_SLEEP, 116, 142]

class IntegratedHIDController:
    """HID 独占飞鼠手柄与全能快捷按键控制器"""
    def __init__(self, uinput_keyboard: Optional[UInput], uinput_mouse: Optional[UInput], submitter: LinuxSubmitter, mode: str = "typing"):
        self.ui_kb = uinput_keyboard
        self.ui_mouse = uinput_mouse
        self.submitter = submitter
        self.running = False
        self.mode = mode  # "typing" (通用GUI: Ctrl+V), "terminal" (Linux终端: Ctrl+Shift+V), "mouse" (飞鼠), "directional" (方向键)
        self.last_input_mode = mode if mode in ("typing", "terminal") else "typing"
        if self.mode == "terminal":
            self.submitter.set_paste_mode("terminal")
        elif self.mode == "typing":
            self.submitter.set_paste_mode("gui")
        self.active_direction = None
        self.direction_pressed_at = 0.0
        self.lock = threading.Lock()
        self.remote_dev = None

        # 快捷键长按与双击状态跟踪
        self.key_timers = {}
        self.long_press_triggered = set()
        self.last_home_click = 0.0
        self.home_single_timer = None
        self.last_menu_click = 0.0
        self.menu_single_timer = None

    def cancel_all_timers(self):
        with self.lock:
            for t in list(self.key_timers.values()):
                try:
                    t.cancel()
                except Exception:
                    pass
            self.key_timers.clear()
            if self.home_single_timer:
                try:
                    self.home_single_timer.cancel()
                except Exception:
                    pass
                self.home_single_timer = None
            if self.menu_single_timer:
                try:
                    self.menu_single_timer.cancel()
                except Exception:
                    pass
                self.menu_single_timer = None
            self.long_press_triggered.clear()

    def mouse_click(self, btn: int = e.BTN_LEFT):
        if not self.ui_mouse:
            return
        self.ui_mouse.write(e.EV_KEY, btn, 1)
        self.ui_mouse.syn()
        time.sleep(0.02)
        self.ui_mouse.write(e.EV_KEY, btn, 0)
        self.ui_mouse.syn()

    def mouse_double_click(self, btn: int = e.BTN_LEFT):
        self.mouse_click(btn)
        time.sleep(0.08)
        self.mouse_click(btn)

    def mouse_scroll(self, steps: int):
        if not self.ui_mouse or steps == 0:
            return
        direction = 1 if steps > 0 else -1
        for _ in range(abs(steps)):
            self.ui_mouse.write(e.EV_REL, e.REL_WHEEL, direction)
            self.ui_mouse.syn()
            time.sleep(0.005)

    def find_hid_device(self) -> Optional[InputDevice]:
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                d = InputDevice(path)
                if d.info.vendor == XIAOMI_VENDOR_ID and d.info.product == XIAOMI_PRODUCT_ID:
                    return d
                if any(kw in d.name.lower() for kw in REMOTE_NAME_KEYWORDS):
                    return d
            except Exception:
                continue
        return None

    def pointer_speed(self, held_seconds: float) -> float:
        if held_seconds < 0.15:
            return 160.0
        elif held_seconds < 0.75:
            progress = (held_seconds - 0.15) / 0.60
            return 160.0 + progress * (720.0 - 160.0)
        else:
            return min(1800.0, 720.0 + (held_seconds - 0.75) * 700.0)

    def motion_loop(self):
        tick_interval = 1.0 / 60.0
        last_tick = time.time()
        accum_x, accum_y = 0.0, 0.0
        while self.running:
            time.sleep(tick_interval)
            now = time.time()
            delta = min(0.05, now - last_tick)
            last_tick = now

            with self.lock:
                if self.mode not in ("typing", "mouse") or not self.active_direction or not self.ui_mouse:
                    accum_x, accum_y = 0.0, 0.0
                    continue
                held = now - self.direction_pressed_at
                step_dist = self.pointer_speed(held) * delta

                if self.active_direction == 'up':
                    accum_y -= step_dist
                elif self.active_direction == 'down':
                    accum_y += step_dist
                elif self.active_direction == 'left':
                    accum_x -= step_dist
                elif self.active_direction == 'right':
                    accum_x += step_dist

                dx = int(accum_x)
                dy = int(accum_y)
                accum_x -= dx
                accum_y -= dy

                if dx != 0 or dy != 0:
                    if dx != 0:
                        self.ui_mouse.write(e.EV_REL, e.REL_X, dx)
                    if dy != 0:
                        self.ui_mouse.write(e.EV_REL, e.REL_Y, dy)
                    self.ui_mouse.syn()

    def handle_direction(self, direction: str, val: int):
        with self.lock:
            if val in (1, 2):
                # 按键按下 (val==1) 或 保持连发 (val==2)
                if self.mode in ("typing", "mouse"):
                    if val == 1 or not self.active_direction:
                        self.active_direction = direction
                        self.direction_pressed_at = time.time()
                elif self.ui_kb:
                    key_map = {'up': e.KEY_UP, 'down': e.KEY_DOWN, 'left': e.KEY_LEFT, 'right': e.KEY_RIGHT}
                    target_key = key_map.get(direction)
                    if target_key:
                        if self.mode == "terminal":
                            action_desc = {
                                'up': '翻查上一条历史命令 (Up)',
                                'down': '翻查下一条历史命令 (Down)',
                                'left': '光标左移 (Left)',
                                'right': '光标右移 (Right)'
                            }.get(direction, direction)
                            print(f">> [快捷键] 方向环 {direction.upper()} -> {action_desc}", flush=True)
                        elif self.mode == "directional":
                            action_desc = {
                                'up': '上一页 (Up)',
                                'down': '下一页 (Down)',
                                'left': '左翻页 (Left)',
                                'right': '右翻页 (Right)'
                            }.get(direction, direction)
                            print(f">> [快捷键] 方向环 {direction.upper()} -> {action_desc}", flush=True)
                        self.submitter.send_key(target_key, duration=0.02)
            elif val == 0:
                # 按键松开 (val==0)
                if self.mode in ("typing", "mouse"):
                    if self.active_direction == direction:
                        self.active_direction = None

    def on_center_long_press(self):
        with self.lock:
            self.long_press_triggered.add("center")
        if self.mode == "typing":
            print(">> [快捷键] OK 长按 (>0.45s) -> 换行不发送 (Shift+Enter)", flush=True)
            self.submitter.send_shift_enter()
        elif self.mode == "terminal":
            print(">> [快捷键] OK 长按 (>0.45s) -> 终端中断任务 (Ctrl+C)", flush=True)
            self.submitter.send_ctrl_c()
        elif self.mode == "mouse":
            print(">> [快捷键] OK 长按 (>0.45s) -> 鼠标左键双击", flush=True)
            self.mouse_double_click(e.BTN_LEFT)
        else:
            print(">> [快捷键] OK 长按 (>0.45s) -> 回车 (Enter)", flush=True)
            self.submitter.send_enter()

    def on_back_long_press(self):
        with self.lock:
            self.long_press_triggered.add("back")
        if self.mode == "typing":
            print(">> [快捷键] 返回 长按 (>0.45s) -> 撤销上次输入 (Ctrl+Z)", flush=True)
            self.submitter.send_ctrl_z()
        elif self.mode == "terminal":
            print(">> [快捷键] 返回 长按 (>0.45s) -> 清空整行命令 (Ctrl+U)", flush=True)
            self.submitter.send_ctrl_u()
        elif self.mode == "mouse":
            print(">> [快捷键] 返回 长按 (>0.45s) -> 鼠标右键", flush=True)
            self.mouse_click(e.BTN_RIGHT)
        else:
            print(">> [快捷键] 返回 长按 (>0.45s) -> 退出放映 (ESC)", flush=True)
            self.submitter.send_esc()

    def on_menu_long_press(self):
        with self.lock:
            self.long_press_triggered.add("menu")
        if self.mode == "typing":
            print(">> [快捷键] 菜单 长按 (>0.45s) -> 全选内容 (Ctrl+A)", flush=True)
            self.submitter.send_ctrl_a()
        elif self.mode == "terminal":
            print(">> [快捷键] 菜单 长按 (>0.45s) -> 命令行自动补全 (Tab)", flush=True)
            self.submitter.send_tab()
        elif self.mode == "mouse":
            print(">> [快捷键] 菜单 长按 (>0.45s) -> 鼠标中键点击", flush=True)
            self.mouse_click(e.BTN_MIDDLE)
        else:
            print(">> [快捷键] 菜单 长按 (>0.45s) -> 全屏切换 (F11)", flush=True)
            self.submitter.send_f11()

    def on_menu_single_click(self):
        if self.mode == "terminal":
            print(">> [快捷键] 菜单 单击 -> 切换不同终端独立窗口 (Alt+`)", flush=True)
            self.submitter.send_switch_terminal_window()
        elif self.mode == "typing":
            print(">> [快捷键] 菜单 单击 -> 鼠标右键菜单", flush=True)
            self.mouse_click(e.BTN_RIGHT)
        elif self.mode == "mouse":
            print(">> [快捷键] 菜单 单击 -> 鼠标中键点击", flush=True)
            self.mouse_click(e.BTN_MIDDLE)
        else:
            print(">> [快捷键] 菜单 单击 -> 全屏切换 (F11)", flush=True)
            self.submitter.send_f11()

    def on_menu_double_click(self):
        if self.mode == "terminal":
            print(">> [快捷键] 菜单 双击 -> 新建终端独立窗口 (Ctrl+Shift+N)", flush=True)
            self.submitter.send_new_terminal_window()
        elif self.mode == "typing":
            print(">> [快捷键] 菜单 双击 -> 全选内容 (Ctrl+A)", flush=True)
            self.submitter.send_ctrl_a()
        elif self.mode == "mouse":
            print(">> [快捷键] 菜单 双击 -> 鼠标中键双击", flush=True)
            self.mouse_double_click(e.BTN_MIDDLE)
        else:
            print(">> [快捷键] 菜单 双击 -> 全屏切换 (F11)", flush=True)
            self.submitter.send_f11()

    def on_power_long_press(self):
        with self.lock:
            self.long_press_triggered.add("power")
        print(">> [快捷键] 电源 长按 (>0.6s) -> 锁定屏幕 (Super+L)", flush=True)
        self.submitter.send_combo(e.KEY_LEFTMETA, e.KEY_L)

    def on_home_single_click(self):
        if self.mode == "typing":
            print(">> [快捷键] HOME 单击 -> 快速切换应用窗口 (Alt+Tab)", flush=True)
            self.submitter.send_alt_tab()
        elif self.mode == "terminal":
            print(">> [快捷键] HOME 单击 -> 同一终端切换标签页 (Ctrl+PageDown)", flush=True)
            self.submitter.send_next_terminal_tab()
        elif self.mode == "mouse":
            print(">> [快捷键] HOME 单击 -> 显示桌面 (Super+D)", flush=True)
            self.submitter.send_super_d()
        else:
            print(">> [快捷键] HOME 单击 -> PPT 从头放映 (F5)", flush=True)
            self.submitter.send_f5()

    def on_home_double_click(self):
        if self.mode == "terminal":
            print(">> [快捷键] HOME 双击 -> 新建终端标签页 (Ctrl+Shift+T)", flush=True)
            self.submitter.send_new_terminal_tab()
        elif self.mode in ("typing", "mouse"):
            print(">> [快捷键] HOME 双击 -> 显示桌面 (Super+D)", flush=True)
            self.submitter.send_super_d()
        else:
            print(">> [快捷键] HOME 双击 -> PPT 从当前页放映 (Shift+F5)", flush=True)
            self.submitter.send_shift_f5()

    def on_tv_short_press(self):
        """短按 TV 键：在两大常用模式（通用打字 TYPING ⇋ 终端命令行 TERMINAL）之间快速切换"""
        if self.mode == "typing":
            print(">> [快捷键] TV 短按 -> 切换至 终端命令行模式 (TERMINAL)", flush=True)
            self.switch_to_mode("terminal")
        elif self.mode == "terminal":
            print(">> [快捷键] TV 短按 -> 切换至 通用打字模式 (TYPING)", flush=True)
            self.switch_to_mode("typing")
        else:
            target = getattr(self, "last_input_mode", "typing")
            print(f">> [快捷键] TV 短按 -> 退出特殊模式，切回常用输入模式 ({target.upper()})", flush=True)
            self.switch_to_mode(target)

    def on_tv_long_press(self):
        """长按 TV 键 (>0.45s)：切换到【无线飞鼠模式 (MOUSE)】或【演讲演示模式 (DIRECTIONAL)】"""
        with self.lock:
            self.long_press_triggered.add("tv")
        if self.mode == "mouse":
            print(">> [快捷键] TV 长按 (>0.45s) -> 切换至 演讲演示模式 (DIRECTIONAL)", flush=True)
            self.switch_to_mode("directional")
        elif self.mode == "directional":
            print(">> [快捷键] TV 长按 (>0.45s) -> 切换至 无线飞鼠模式 (MOUSE)", flush=True)
            self.switch_to_mode("mouse")
        else:
            print(">> [快捷键] TV 长按 (>0.45s) -> 切换至 无线飞鼠模式 (MOUSE)", flush=True)
            self.switch_to_mode("mouse")

    def handle_action_key(self, code: int, val: int):
        is_down = (val == 1)
        is_up = (val == 0)
        is_repeat = (val == 2)

        # 1. 音量加减键 (短按单步，长按连发)
        if code in VOL_UP_KEYS:
            if is_down or is_repeat:
                if self.mode == "terminal":
                    print(">> [快捷键] 音量+ -> 终端平滑滚屏 (8 格)", flush=True)
                    self.mouse_scroll(8)
                elif self.mode == "typing":
                    print(">> [快捷键] 音量+ -> 页面平滑滚屏 (4 格)", flush=True)
                    self.mouse_scroll(4)
                elif self.mode == "mouse":
                    print(">> [快捷键] 音量+ -> 系统音量增加", flush=True)
                    self.submitter.send_key(e.KEY_VOLUMEUP)
                else:
                    self.submitter.send_page_up()
            return

        if code in VOL_DOWN_KEYS:
            if is_down or is_repeat:
                if self.mode == "terminal":
                    print(">> [快捷键] 音量- -> 终端平滑滚屏 (-8 格)", flush=True)
                    self.mouse_scroll(-8)
                elif self.mode == "typing":
                    print(">> [快捷键] 音量- -> 页面平滑滚屏 (-4 格)", flush=True)
                    self.mouse_scroll(-4)
                elif self.mode == "mouse":
                    print(">> [快捷键] 音量- -> 系统音量减小", flush=True)
                    self.submitter.send_key(e.KEY_VOLUMEDOWN)
                else:
                    self.submitter.send_page_down()
            return

        # 2. TV 模式切换键 (短按切换前两大常用模式，长按切换飞鼠/演示模式)
        if code in TV_KEYS:
            if is_down:
                with self.lock:
                    if "tv" in self.key_timers:
                        self.key_timers["tv"].cancel()
                    t = threading.Timer(0.45, self.on_tv_long_press)
                    self.key_timers["tv"] = t
                    t.start()
            elif is_up:
                with self.lock:
                    t = self.key_timers.pop("tv", None)
                    if t: t.cancel()
                    was_long = "tv" in self.long_press_triggered
                    if was_long:
                        self.long_press_triggered.remove("tv")
                if not was_long:
                    self.on_tv_short_press()
            return

        # 3. CENTER 确认键 (短按发送/执行, 长按换行/中断)
        if code in CENTER_KEYS:
            if is_down:
                with self.lock:
                    if "center" in self.key_timers:
                        self.key_timers["center"].cancel()
                    t = threading.Timer(0.45, self.on_center_long_press)
                    self.key_timers["center"] = t
                    t.start()
            elif is_up:
                with self.lock:
                    t = self.key_timers.pop("center", None)
                    if t: t.cancel()
                    was_long = "center" in self.long_press_triggered
                    if was_long:
                        self.long_press_triggered.remove("center")
                if not was_long:
                    if self.mode == "typing":
                        print(">> [快捷键] OK 短按 -> 回车发送 (Enter)", flush=True)
                        self.submitter.send_enter()
                    elif self.mode == "terminal":
                        print(">> [快捷键] OK 短按 -> 执行命令 (Enter)", flush=True)
                        self.submitter.send_enter()
                    elif self.mode == "mouse":
                        print(">> [快捷键] OK 短按 -> 鼠标左键点击", flush=True)
                        self.mouse_click(e.BTN_LEFT)
                    else:
                        print(">> [快捷键] OK 短按 -> 下一页 (Space)", flush=True)
                        self.submitter.send_space()
            return

        # 4. BACK 返回键 (短按ESC, 长按撤销/清行)
        if code in BACK_KEYS:
            if is_down:
                with self.lock:
                    if "back" in self.key_timers:
                        self.key_timers["back"].cancel()
                    t = threading.Timer(0.45, self.on_back_long_press)
                    self.key_timers["back"] = t
                    t.start()
            elif is_up:
                with self.lock:
                    t = self.key_timers.pop("back", None)
                    if t: t.cancel()
                    was_long = "back" in self.long_press_triggered
                    if was_long:
                        self.long_press_triggered.remove("back")
                if not was_long:
                    if self.mode == "mouse":
                        print(">> [快捷键] 返回 短按 -> 鼠标右键", flush=True)
                        self.mouse_click(e.BTN_RIGHT)
                    elif self.mode in ("typing", "terminal"):
                        print(">> [快捷键] 返回 短按 -> 回退删除字符 (Backspace)", flush=True)
                        self.submitter.send_backspace()
                    else:
                        print(">> [快捷键] 返回 短按 -> 取消 / ESC", flush=True)
                        self.submitter.send_esc()
            return

        # 5. HOME 主页键 (单击清屏/切应用, 双击新建标签/桌面)
        if code in HOME_KEYS:
            if is_up:
                now = time.time()
                with self.lock:
                    if now - self.last_home_click < 0.28:
                        if self.home_single_timer:
                            self.home_single_timer.cancel()
                            self.home_single_timer = None
                        self.last_home_click = 0.0
                        is_double = True
                    else:
                        self.last_home_click = now
                        self.home_single_timer = threading.Timer(0.28, self.on_home_single_click)
                        self.home_single_timer.start()
                        is_double = False
                if is_double:
                    self.on_home_double_click()
            return

        # 6. MENU 菜单键 (短按补全/右键, 长按反向搜索/全选)
        if code in MENU_KEYS:
            if is_down:
                with self.lock:
                    if "menu" in self.key_timers:
                        self.key_timers["menu"].cancel()
                    t = threading.Timer(0.45, self.on_menu_long_press)
                    self.key_timers["menu"] = t
                    t.start()
            elif is_up:
                with self.lock:
                    t = self.key_timers.pop("menu", None)
                    if t: t.cancel()
                    was_long = "menu" in self.long_press_triggered
                    if was_long:
                        self.long_press_triggered.remove("menu")
                if not was_long:
                    now = time.time()
                    with self.lock:
                        if now - self.last_menu_click < 0.28:
                            if self.menu_single_timer:
                                self.menu_single_timer.cancel()
                                self.menu_single_timer = None
                            self.last_menu_click = 0.0
                            is_double = True
                        else:
                            self.last_menu_click = now
                            self.menu_single_timer = threading.Timer(0.28, self.on_menu_single_click)
                            self.menu_single_timer.start()
                            is_double = False
                    if is_double:
                        self.on_menu_double_click()
            return

        # 7. POWER 电源键 (短按启动器, 长按锁屏)
        if code in POWER_KEYS:
            if is_down:
                with self.lock:
                    if "power" in self.key_timers:
                        self.key_timers["power"].cancel()
                    t = threading.Timer(0.60, self.on_power_long_press)
                    self.key_timers["power"] = t
                    t.start()
            elif is_up:
                with self.lock:
                    t = self.key_timers.pop("power", None)
                    if t: t.cancel()
                    was_long = "power" in self.long_press_triggered
                    if was_long:
                        self.long_press_triggered.remove("power")
                if not was_long:
                    print(">> [快捷键] 电源 短按 -> 呼出应用搜索 (Super)", flush=True)
                    self.submitter.send_super_key()
            return

    def notify_mode_change(self):
        """桌面 OSD 弹窗通知与音效提示 (Desktop Notification & Sound Prompt)"""
        mode_info = {
            "typing": {
                "title": "💬 通用打字模式 (TYPING)",
                "desc": "OK: 回车发送 | 返回: 退格删除\n短按TV: 切终端模式 | 长按TV: 切无线飞鼠",
                "icon": "input-keyboard"
            },
            "terminal": {
                "title": "💻 终端命令行模式 (TERMINAL)",
                "desc": "HOME: 切标签/双击新建标签\n菜单: 切终端/双击新建终端",
                "icon": "utilities-terminal"
            },
            "mouse": {
                "title": "🖱️ 无线飞鼠模式 (MOUSE)",
                "desc": "OK: 鼠标左键 | 返回: 鼠标右键\n短按TV: 退回打字 | 长按TV: 切演讲演示",
                "icon": "input-mouse"
            },
            "directional": {
                "title": "📊 演讲演示模式 (DIRECTIONAL)",
                "desc": "OK: 空格下一页 | 方向: 翻页\n短按TV: 退回打字 | 长按TV: 切无线飞鼠",
                "icon": "video-display"
            }
        }
        info = mode_info.get(self.mode, {
            "title": f"模式切换: {self.mode}",
            "desc": "",
            "icon": "dialog-information"
        })

        # 1. 桌面通知 (利用 x-canonical-private-synchronous 确保快速连按时无缝替换同一横幅)
        if shutil.which("notify-send"):
            try:
                subprocess.Popen(
                    [
                        "notify-send",
                        "-a", "米遥 MI-AO",
                        "-i", info["icon"],
                        "-t", "1800",
                        "-h", "string:x-canonical-private-synchronous:mi-ao-mode",
                        info["title"],
                        info["desc"]
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
            except Exception:
                pass

        # 2. 播放轻柔提示音
        if shutil.which("canberra-gtk-play"):
            try:
                subprocess.Popen(
                    ["canberra-gtk-play", "-i", "audio-volume-change"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
            except Exception:
                pass

    def switch_to_mode(self, new_mode: str):
        with self.lock:
            self.mode = new_mode
            if new_mode in ("typing", "terminal"):
                self.last_input_mode = new_mode
            self.active_direction = None
            if self.mode == "terminal":
                self.submitter.set_paste_mode("terminal")
            elif self.mode == "typing":
                self.submitter.set_paste_mode("gui")

        if self.mode == "typing":
            print("[HID] 模式切换为: 【通用打字模式 (TYPING)】- OK发消息/长按换行，方向环移动鼠标", flush=True)
        elif self.mode == "terminal":
            print("[HID] 模式切换为: 【终端命令行模式 (TERMINAL)】- OK执行/长按Ctrl+C，方向环翻历史命令", flush=True)
        elif self.mode == "mouse":
            print("[HID] 模式切换为: 【无线飞鼠模式 (MOUSE)】- OK左键/长按双击，返回右键，音量调系统声", flush=True)
        else:
            print("[HID] 模式切换为: 【演讲演示模式 (DIRECTIONAL)】- 方向环翻页，OK下一页(空格)，HOME放映(F5)", flush=True)

        self.notify_mode_change()

    def cycle_mode(self):
        modes = ["typing", "terminal", "mouse", "directional"]
        idx = modes.index(self.mode) if self.mode in modes else 0
        self.switch_to_mode(modes[(idx + 1) % len(modes)])

    def start_loop(self):
        self.running = True
        threading.Thread(target=self.motion_loop, daemon=True).start()

        while self.running:
            dev = self.find_hid_device()
            if not dev:
                time.sleep(2)
                continue

            self.remote_dev = dev
            try:
                dev.grab()
                print(f"[HID] 已独占遥控器按键外设: {dev.name} ({dev.path})", flush=True)
                for event in dev.read_loop():
                    if not self.running:
                        break
                    if event.type != e.EV_KEY:
                        continue
                    code, val = event.code, event.value
                    is_down = (val == 1)

                    if code in DPAD_UP:
                        self.handle_direction('up', val)
                    elif code in DPAD_DOWN:
                        self.handle_direction('down', val)
                    elif code in DPAD_LEFT:
                        self.handle_direction('left', val)
                    elif code in DPAD_RIGHT:
                        self.handle_direction('right', val)
                    else:
                        self.handle_action_key(code, val)
            except Exception as err:
                print(f"[HID] 连接断开或异常: {err}", flush=True)
            finally:
                self.cancel_all_timers()
                try:
                    dev.ungrab()
                except Exception:
                    pass
                time.sleep(2)


class MiAoDaemon:
    """米遥一体化服务守护进程"""
    STATE_DISCONNECTED = "disconnected"
    STATE_READY        = "ready"
    STATE_OPENING      = "opening"
    STATE_STREAMING    = "streaming"

    def __init__(self, model_size="small", enable_mouse=True, mode="typing", paste_mode="gui"):
        self.protocol = ATVVProtocol()
        self.state = self.STATE_DISCONNECTED
        self.stream_id = 0
        self.client: Optional[BleakClient] = None
        self.lock = asyncio.Lock()
        self.enable_mouse = enable_mouse

        self.recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
        os.makedirs(self.recordings_dir, exist_ok=True)

        # 完整虚拟键盘设备 (配置 1~255 完整键盘键位，确保 udev 赋予 ID_INPUT_KEYBOARD=1 属性)
        self.ui_kb = None
        try:
            full_keys = [k for k in range(1, 256)]
            self.ui_kb = UInput({e.EV_KEY: full_keys}, name="Mi-Ao-Virtual-Keyboard", version=0x1)
            print("[系统] 虚拟键盘设备就绪 (完整键位 ID_INPUT_KEYBOARD=1 /dev/uinput)", flush=True)
        except Exception as err:
            print(f"[系统] 虚拟键盘初始化失败: {err}", flush=True)

        # 独立虚拟鼠标设备
        self.ui_mouse = None
        try:
            mouse_caps = {
                e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
                e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE]
            }
            self.ui_mouse = UInput(mouse_caps, name="Mi-Ao-Virtual-Mouse", version=0x1)
            print("[系统] 虚拟鼠标设备就绪 (/dev/uinput -> Mi-Ao-Virtual-Mouse)", flush=True)
        except Exception as err:
            print(f"[系统] 虚拟鼠标初始化失败: {err}", flush=True)

        # 提交器与双阶 Whisper 转写器
        effective_paste_mode = "terminal" if mode == "terminal" else paste_mode
        self.submitter = LinuxSubmitter(
            base_dir=os.path.dirname(__file__),
            uinput_keyboard=self.ui_kb,
            paste_mode=effective_paste_mode
        )
        self.transcriber = LocalWhisperTranscriber(model_size=model_size)

        # 原始音频数据缓存与解码状态隔离
        self.raw_frames = []
        self.sync_predictor = 0
        self.sync_step = 0
        self.stream_typed_text = ""
        self.is_recording = False
        self.streaming_thread = None
        self.stream_lock = threading.Lock()

        # 飞鼠与按键控制器
        self.hid_controller = None
        if self.enable_mouse:
            self.hid_controller = IntegratedHIDController(
                uinput_keyboard=self.ui_kb,
                uinput_mouse=self.ui_mouse,
                submitter=self.submitter,
                mode=mode
            )

    def decode_raw_frames(self, frames: List[bytes], predictor: int, step_index: int) -> List[int]:
        """每次基于独立全新的解码器状态从第 0 帧解码，彻底杜绝跨会话状态累积污染"""
        if not frames:
            return []
        proto = ATVVProtocol()
        if self.protocol.capabilities:
            proto.accept_capabilities(self.protocol.capabilities)
        proto.decoder.reset(predictor, step_index)
        samples = []
        for f in frames:
            res = proto.decode_audio(f)
            if res and res[1]:
                samples.extend(res[1])
        return samples

    async def find_ble_device(self) -> BLEDevice:
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
                print(f"[语音] ATVV v{caps.version} 协商成功 (Codec={self.protocol.codec})，随时就绪！", flush=True)

            elif event_type == "start_search":
                if self.state in (self.STATE_STREAMING, self.STATE_OPENING):
                    await self.finalize_recording(reason="second-press")
                elif self.state == self.STATE_READY:
                    await self.begin_opening()

            elif event_type == "audio_start":
                if self.state in (self.STATE_OPENING, self.STATE_READY):
                    self.stream_id = event.get("stream_id", 0)
                    self.state = self.STATE_STREAMING
                    with self.stream_lock:
                        self.raw_frames = []
                        self.sync_predictor = 0
                        self.sync_step = 0
                        self.stream_typed_text = ""
                        self.is_recording = True
                    print("\n[语音] 按下语音键，正在倾听并实时打字...", flush=True)
                    self.streaming_thread = threading.Thread(target=self._streaming_worker, daemon=True)
                    self.streaming_thread.start()

            elif event_type == "audio_stop":
                if self.state not in (self.STATE_STREAMING, self.STATE_OPENING):
                    return
                print("[语音] 松开语音键，正在进行终态校准...", flush=True)
                await self.finalize_recording(reason="remote-release")

            elif event_type == "audio_sync":
                with self.stream_lock:
                    self.sync_predictor = event.get("predictor", 0)
                    self.sync_step = event.get("step_index", 0)

    def on_audio_notification(self, sender, data: bytearray):
        if self.state in (self.STATE_STREAMING, self.STATE_OPENING):
            with self.stream_lock:
                self.raw_frames.append(bytes(data))

    async def begin_opening(self):
        try:
            with self.stream_lock:
                self.raw_frames = []
                self.sync_predictor = 0
                self.sync_step = 0
                self.stream_typed_text = ""
            self.state = self.STATE_OPENING
            cmd = self.protocol.mic_open_command()
            await self.client.write_gatt_char(ATVVProtocol.TX_UUID, cmd, response=True)
        except Exception:
            self.state = self.STATE_READY

    def _streaming_worker(self):
        """流式后台线程：每隔约 0.3s 将已录入的原始帧快速推断，并差量注入打字框"""
        last_process_time = time.time()
        # 16kHz ADPCM 每帧 120 字节 = 240 samples (15ms)。40 帧约为 0.6s
        min_frames = 40

        while self.is_recording:
            time.sleep(0.06)
            now = time.time()
            if now - last_process_time < 0.28:
                continue

            with self.stream_lock:
                frames_snapshot = list(self.raw_frames)
                pred = self.sync_predictor
                step = self.sync_step

            if len(frames_snapshot) < min_frames:
                continue

            last_process_time = now
            samples = self.decode_raw_frames(frames_snapshot, pred, step)
            partial_text = self.transcriber.transcribe_stream_samples(samples)
            partial_text = partial_text.strip()

            if not self.is_recording:
                break

            if partial_text and partial_text != self.stream_typed_text:
                # 计算公共前缀
                k = 0
                while (k < len(self.stream_typed_text) and 
                       k < len(partial_text) and 
                       self.stream_typed_text[k] == partial_text[k]):
                    k += 1

                backspaces = len(self.stream_typed_text) - k
                delta = partial_text[k:]

                if backspaces > 0 or delta:
                    self.submitter.type_diff(backspaces, delta)
                    self.stream_typed_text = partial_text
                    print(f"  [实时打字] {partial_text}", flush=True)

    async def finalize_recording(self, reason: str):
        self.state = self.STATE_READY
        self.is_recording = False

        try:
            cmd = self.protocol.mic_close_command(self.stream_id)
            await self.client.write_gatt_char(ATVVProtocol.TX_UUID, cmd, response=True)
        except Exception:
            pass

        if self.streaming_thread:
            self.streaming_thread.join(timeout=0.6)

        with self.stream_lock:
            all_frames = list(self.raw_frames)
            pred = self.sync_predictor
            step = self.sync_step
            currently_typed = self.stream_typed_text
            self.raw_frames = []

        all_samples = self.decode_raw_frames(all_frames, pred, step)
        sample_rate = 16000

        if len(all_samples) < sample_rate * 0.25:
            # 误触抖动，清理可能已打出的临时文字
            if currently_typed:
                self.submitter.type_diff(len(currently_typed), "")
            return

        duration = len(all_samples) / sample_rate
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_path = os.path.join(self.recordings_dir, f"speech_{timestamp}.wav")
        AudioPipeline.write_wav(all_samples, sample_rate, wav_path)

        # 终态高精度 Whisper 识别
        final_text = self.transcriber.transcribe_final_samples(all_samples)
        if not final_text:
            if currently_typed:
                self.submitter.type_diff(len(currently_typed), "")
            return

        # 计算并更新终态与流式打字的差量
        k = 0
        while (k < len(currently_typed) and 
               k < len(final_text) and 
               currently_typed[k] == final_text[k]):
            k += 1

        backspaces = len(currently_typed) - k
        delta = final_text[k:]

        if backspaces > 0 or delta:
            self.submitter.type_diff(backspaces, delta)

        # 归档并全量同步剪贴板
        self.submitter.record_submission(final_text, audio_path=wav_path)
        with self.stream_lock:
            self.stream_typed_text = ""

        print("--------------------------------------------------", flush=True)
        print(f"🎉 语音输入完毕: {final_text}", flush=True)
        print(f"   [已实时输入到打字框 | 按遥控器【OK/确认键】即可发送]", flush=True)
        print("--------------------------------------------------", flush=True)

    async def run_ble(self):
        while True:
            try:
                device = await self.find_ble_device()
                print(f"[语音] 正在连接 GATT 语音通道 [{device.address}]...", flush=True)
                async with BleakClient(device, timeout=10.0) as client:
                    self.client = client
                    print(f"[语音] BLE 通道连接成功！", flush=True)

                    await client.start_notify(ATVVProtocol.CONTROL_UUID, self.on_control_notification)
                    await client.start_notify(ATVVProtocol.RX_UUID, self.on_audio_notification)
                    await client.write_gatt_char(ATVVProtocol.TX_UUID, ATVVProtocol.GET_CAPS_COMMAND, response=True)

                    while client.is_connected:
                        await asyncio.sleep(1)
            except Exception as err:
                print(f"[语音] BLE 连接异常或断开: {err}，5秒后重试...", flush=True)
                await asyncio.sleep(5)

    def start(self):
        if self.enable_mouse and self.hid_controller:
            threading.Thread(target=self.hid_controller.start_loop, daemon=True).start()

        if self.hid_controller and self.hid_controller.mode == "typing":
            mode_desc = "通用打字与确认发送模式 (Ctrl+V, OK键 = Enter发送)"
        elif self.hid_controller and self.hid_controller.mode == "terminal":
            mode_desc = "终端打字与确认发送模式 (Ctrl+Shift+V, OK键 = Enter发送)"
        elif self.hid_controller and self.hid_controller.mode == "mouse":
            mode_desc = "飞鼠点击模式 (OK键 = 鼠标左键, 返回键 = 鼠标右键)"
        elif self.hid_controller and self.hid_controller.mode == "directional":
            mode_desc = "键盘方向键模式 (方向环 = 方向键, OK键 = Enter发送)"
        else:
            mode_desc = self.hid_controller.mode if self.hid_controller else "typing"

        print("==================================================", flush=True)
        print(">> 【米遥 (MI-AO) Linux 一体化守护服务启动】", flush=True)
        print(f">> - 模式：{mode_desc}", flush=True)
        print(">> - 语音：按住【语音键】说话，文字实时流式输出到打字框", flush=True)
        print(">> - 发送：松手后按遥控器【OK/确认键】，直接回车发送！", flush=True)
        print(">> - 切换：【短按TV】切 打字/终端 常用模式，【长按TV >0.45s】切 飞鼠/演示 特殊模式", flush=True)
        print("==================================================", flush=True)
        asyncio.run(self.run_ble())


def main():
    import argparse
    parser = argparse.ArgumentParser(description="米遥 Linux 一体化服务")
    parser.add_argument("--model", default="medium", help="终态 Whisper 模型规格 (默认: medium，可选: small, base)")
    parser.add_argument("--mode", default="typing", choices=["typing", "terminal", "mouse", "directional"], help="初始遥控器模式 (默认: typing, 可选: terminal, mouse, directional)")
    parser.add_argument("--paste-mode", default="gui", choices=["gui", "terminal", "universal"], help="粘贴快捷键策略 (默认: gui [Ctrl+V], 可选: terminal [Ctrl+Shift+V], universal [Shift+Insert])")
    parser.add_argument("--no-mouse", action="store_true", help="禁用飞鼠按键控制")
    args = parser.parse_args()

    daemon = MiAoDaemon(
        model_size=args.model,
        enable_mouse=not args.no_mouse,
        mode=args.mode,
        paste_mode=args.paste_mode
    )
    try:
        daemon.start()
    except KeyboardInterrupt:
        print("\n米遥服务安全退出。", flush=True)

if __name__ == "__main__":
    main()
