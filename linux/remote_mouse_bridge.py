#!/usr/bin/env python3
"""
小米蓝牙遥控器 2 Pro - Linux 最小运行桥接 (HID -> uinput)
复现原版 Swift 的按键到鼠标/键盘模拟逻辑：
- 方向环：移动鼠标指针（按住平滑加速）
- OK/Center 键：鼠标左键
- Back 键：鼠标右键
- 音量 +/-：鼠标滚轮上下滚动
- TV 键：切换模式（Pointer 鼠标模式 / Directional 原生键盘模式）
- 独占设备 (EVIOCGRAB)，系统完全不受遥控器默认按键干扰
"""

import sys
import os
import glob
import time
import threading

# 兼容 sudo 环境下加载普通用户的 site-packages
for user in [os.environ.get("SUDO_USER"), os.environ.get("USER"), "nvidia"]:
    if user:
        user_site = f"/home/{user}/.local/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        if os.path.isdir(user_site) and user_site not in sys.path:
            sys.path.insert(0, user_site)

try:
    import evdev
    from evdev import InputDevice, UInput, ecodes as e
except ImportError:
    print("错误: 未安装 evdev 库。请运行: python3 -m pip install evdev")
    sys.exit(1)

XIAOMI_VENDOR_ID = 0x2717
XIAOMI_PRODUCT_ID = 0x32B8

# 常见键码映射（适配不同的蓝牙协议解析）
DPAD_UP = [e.KEY_UP]
DPAD_DOWN = [e.KEY_DOWN]
DPAD_LEFT = [e.KEY_LEFT]
DPAD_RIGHT = [e.KEY_RIGHT]
CENTER_KEYS = [e.KEY_ENTER, e.KEY_OK, 28, 352]
BACK_KEYS = [e.KEY_BACK, e.KEY_ESC, 158, 1]
HOME_KEYS = [e.KEY_HOMEPAGE, e.KEY_HOME, 172, 102]
VOL_UP_KEYS = [e.KEY_VOLUMEUP, 115]
VOL_DOWN_KEYS = [e.KEY_VOLUMEDOWN, 114]
TV_KEYS = [e.KEY_TV, e.KEY_PROG1, 377, 148]

class RemoteMouseBridge:
    def __init__(self, remote_dev: InputDevice):
        self.remote_dev = remote_dev
        self.running = False
        
        # 创建 uinput 虚拟鼠标与键盘
        capabilities = {
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
            e.EV_KEY: [
                e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE,
                e.KEY_UP, e.KEY_DOWN, e.KEY_LEFT, e.KEY_RIGHT,
                e.KEY_ENTER, e.KEY_ESC, e.KEY_PAGEUP, e.KEY_PAGEDOWN
            ]
        }
        self.ui = UInput(capabilities, name="Mi-Ao-Virtual-Mouse", version=0x1)
        
        # 状态控制
        self.mode = "pointer"  # "pointer" 或 "directional"
        self.active_direction = None  # 当前按下的方向: 'up', 'down', 'left', 'right'
        self.direction_pressed_at = 0.0
        self.lock = threading.Lock()
        
    def pointer_speed(self, held_seconds: float) -> float:
        """平滑加速曲线，与原版 Swift 实现对齐"""
        if held_seconds < 0.2:
            return 120.0
        elif held_seconds < 1.0:
            progress = (held_seconds - 0.2) / 0.8
            return 120.0 + progress * (520.0 - 120.0)
        else:
            return min(1100.0, 520.0 + (held_seconds - 1.0) * 420.0)

    def motion_loop(self):
        """高频运动循环 (约 60Hz) 驱动鼠标平滑移动"""
        tick_interval = 1.0 / 60.0
        last_tick = time.time()
        
        while self.running:
            time.sleep(tick_interval)
            now = time.time()
            delta = min(0.05, now - last_tick)
            last_tick = now
            
            with self.lock:
                if self.mode != "pointer" or not self.active_direction:
                    continue
                held = now - self.direction_pressed_at
                distance = self.pointer_speed(held) * delta
                
                dx, dy = 0, 0
                if self.active_direction == 'up':
                    dy = -int(distance)
                elif self.active_direction == 'down':
                    dy = int(distance)
                elif self.active_direction == 'left':
                    dx = -int(distance)
                elif self.active_direction == 'right':
                    dx = int(distance)
                    
                if dx != 0 or dy != 0:
                    if dx != 0:
                        self.ui.write(e.EV_REL, e.REL_X, dx)
                    if dy != 0:
                        self.ui.write(e.EV_REL, e.REL_Y, dy)
                    self.ui.syn()

    def handle_direction(self, direction: str, is_down: bool):
        with self.lock:
            if is_down:
                if self.mode == "pointer":
                    self.active_direction = direction
                    self.direction_pressed_at = time.time()
                else:
                    # Directional 键盘原生模式
                    key_map = {'up': e.KEY_UP, 'down': e.KEY_DOWN, 'left': e.KEY_LEFT, 'right': e.KEY_RIGHT}
                    self.ui.write(e.EV_KEY, key_map[direction], 1)
                    self.ui.syn()
            else:
                if self.mode == "pointer":
                    if self.active_direction == direction:
                        self.active_direction = None
                else:
                    key_map = {'up': e.KEY_UP, 'down': e.KEY_DOWN, 'left': e.KEY_LEFT, 'right': e.KEY_RIGHT}
                    self.ui.write(e.EV_KEY, key_map[direction], 0)
                    self.ui.syn()

    def run(self):
        self.running = True
        self.remote_dev.grab()
        print(f">> [已独占接管设备] {self.remote_dev.name}")
        print(">> 当前模式: [POINTER 鼠标模式]")
        print(">> 操作指南:")
        print("   - 方向环: 移动鼠标 (长按加速)")
        print("   - OK 键 : 鼠标左键点击")
        print("   - 返回键: 鼠标右键点击")
        print("   - 音量键: 滚轮上下滚动")
        print("   - TV 键 : 切换 鼠标模式 <-> 键盘方向键模式")
        print("   - 按 Ctrl+C 退出并释放设备\n")

        motion_thread = threading.Thread(target=self.motion_loop, daemon=True)
        motion_thread.start()

        try:
            for event in self.remote_dev.read_loop():
                if event.type != e.EV_KEY:
                    continue
                code, val = event.code, event.value
                is_down = (val == 1)
                is_up = (val == 0)

                # 方向键处理
                if code in DPAD_UP:
                    if is_down: self.handle_direction('up', True)
                    elif is_up: self.handle_direction('up', False)
                elif code in DPAD_DOWN:
                    if is_down: self.handle_direction('down', True)
                    elif is_up: self.handle_direction('down', False)
                elif code in DPAD_LEFT:
                    if is_down: self.handle_direction('left', True)
                    elif is_up: self.handle_direction('left', False)
                elif code in DPAD_RIGHT:
                    if is_down: self.handle_direction('right', True)
                    elif is_up: self.handle_direction('right', False)
                
                # OK 键 -> 鼠标左键
                elif code in CENTER_KEYS:
                    if self.mode == "pointer":
                        self.ui.write(e.EV_KEY, e.BTN_LEFT, 1 if is_down else 0)
                        self.ui.syn()
                    else:
                        self.ui.write(e.EV_KEY, e.KEY_ENTER, 1 if is_down else 0)
                        self.ui.syn()
                        
                # 返回键 -> 鼠标右键
                elif code in BACK_KEYS:
                    if self.mode == "pointer":
                        self.ui.write(e.EV_KEY, e.BTN_RIGHT, 1 if is_down else 0)
                        self.ui.syn()
                    else:
                        self.ui.write(e.EV_KEY, e.KEY_ESC, 1 if is_down else 0)
                        self.ui.syn()

                # 音量滚轮
                elif code in VOL_UP_KEYS and is_down:
                    self.ui.write(e.EV_REL, e.REL_WHEEL, 1)
                    self.ui.syn()
                elif code in VOL_DOWN_KEYS and is_down:
                    self.ui.write(e.EV_REL, e.REL_WHEEL, -1)
                    self.ui.syn()

                # TV 键切换模式
                elif code in TV_KEYS and is_down:
                    with self.lock:
                        self.mode = "directional" if self.mode == "pointer" else "pointer"
                        self.active_direction = None
                    print(f">> [模式切换] 当前模式为: {self.mode.upper()}")

        finally:
            self.running = False
            try:
                self.remote_dev.ungrab()
                print(">> 设备独占已释放")
            except Exception:
                pass
            self.ui.close()

def main():
    devices = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            devices.append(InputDevice(path))
        except Exception:
            pass

    target = None
    for d in devices:
        if d.info.vendor == XIAOMI_VENDOR_ID and d.info.product == XIAOMI_PRODUCT_ID:
            target = d
            break
        if any(kw in d.name.lower() for kw in ["小米", "xiaomi", "remote"]):
            target = d
            break

    if not target:
        if not devices:
            print("错误: 无法访问输入设备。请使用 sudo 运行此程序。")
            sys.exit(1)
        print("未自动检测到小米遥控器。请选择设备编号:")
        for idx, d in enumerate(devices):
            print(f" [{idx}] {d.path} - {d.name}")
        try:
            idx = int(input("输入设备编号: ").strip())
            target = devices[idx]
        except Exception:
            print("选择无效，退出。")
            sys.exit(1)

    bridge = RemoteMouseBridge(target)
    bridge.run()

if __name__ == "__main__":
    main()
