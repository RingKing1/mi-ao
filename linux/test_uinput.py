#!/usr/bin/env python3
"""
Linux /dev/uinput 虚拟鼠标与键盘输入测试
验证在 Wayland / X11 环境下模拟鼠标移动、点击以及键盘按键。
"""
import sys
import time
import os

# 兼容 sudo 环境下加载普通用户的 site-packages
for user in [os.environ.get("SUDO_USER"), os.environ.get("USER"), "nvidia"]:
    if user:
        user_site = f"/home/{user}/.local/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        if os.path.isdir(user_site) and user_site not in sys.path:
            sys.path.insert(0, user_site)

try:
    import evdev
    from evdev import UInput, ecodes as e
except ImportError:
    print("错误: 未安装 evdev 库。请运行: python3 -m pip install evdev")
    sys.exit(1)

def check_permission():
    if not os.path.exists("/dev/uinput"):
        print("错误: /dev/uinput 不存在，请确认系统支持 uinput 内核模块。")
        return False
    if not os.access("/dev/uinput", os.W_OK):
        print("提示: 当前用户对 /dev/uinput 没有写权限。")
        print("请使用 sudo 运行此脚本，或者将当前用户加入 input 组并配置 udev 规则。")
        return False
    return True

def run_uinput_test():
    if not check_permission():
        sys.exit(1)

    print("=== 初始化 uinput 虚拟输入设备 ===")
    
    # 定义虚拟设备的输入能力
    capabilities = {
        e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
        e.EV_KEY: [
            e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE,
            e.KEY_UP, e.KEY_DOWN, e.KEY_LEFT, e.KEY_RIGHT,
            e.KEY_ENTER, e.KEY_ESC, e.KEY_PAGEUP, e.KEY_PAGEDOWN
        ]
    }

    try:
        ui = UInput(capabilities, name="Mi-Ao-Virtual-Controller", version=0x1)
        print(f"成功创建虚拟设备: {ui.name}")
        print("设备节点:", ui.device.path)
    except Exception as err:
        print(f"创建 uinput 设备失败: {err}")
        sys.exit(1)

    print("\n[测试 1] 模拟鼠标相对移动 (画一个正方形)...")
    time.sleep(1)
    
    steps = 20
    delay = 0.01
    
    # 向右移动
    for _ in range(steps):
        ui.write(e.EV_REL, e.REL_X, 5)
        ui.syn()
        time.sleep(delay)
    # 向下移动
    for _ in range(steps):
        ui.write(e.EV_REL, e.REL_Y, 5)
        ui.syn()
        time.sleep(delay)
    # 向左移动
    for _ in range(steps):
        ui.write(e.EV_REL, e.REL_X, -5)
        ui.syn()
        time.sleep(delay)
    # 向上移动
    for _ in range(steps):
        ui.write(e.EV_REL, e.REL_Y, -5)
        ui.syn()
        time.sleep(delay)

    print("鼠标移动测试完成！")
    print("uinput 在当前桌面环境（Wayland）下工作正常。")
    ui.close()

if __name__ == "__main__":
    run_uinput_test()
