#!/usr/bin/env python3
"""
小米蓝牙遥控器 2 Pro - Linux evdev 独占捕获测试
通过 ioctl(EVIOCGRAB) 独占设备，彻底隔离对系统的原生副作用。
"""

import sys
import os
import glob
import time

# 兼容 sudo 环境下加载普通用户的 site-packages
for user in [os.environ.get("SUDO_USER"), os.environ.get("USER"), "nvidia"]:
    if user:
        user_site = f"/home/{user}/.local/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        if os.path.isdir(user_site) and user_site not in sys.path:
            sys.path.insert(0, user_site)

try:
    import evdev
    from evdev import InputDevice, ecodes as e
except ImportError:
    print("错误: 未安装 evdev 库。请运行: python3 -m pip install evdev")
    sys.exit(1)

XIAOMI_VENDOR_ID = 0x2717
XIAOMI_PRODUCT_ID = 0x32B8

def find_candidate_devices():
    devices = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            dev = InputDevice(path)
            devices.append(dev)
        except (PermissionError, FileNotFoundError):
            continue
    return devices

def identify_remote(devices):
    # 优先精确匹配 Vendor / Product
    for dev in devices:
        if dev.info.vendor == XIAOMI_VENDOR_ID and dev.info.product == XIAOMI_PRODUCT_ID:
            return dev, "精确匹配硬件 ID (Vendor=0x2717, Product=0x32b8)"
    
    # 次选匹配名称关键字
    keywords = ["小米", "xiaomi", "remote", "遥控器"]
    for dev in devices:
        name_lower = dev.name.lower()
        if any(kw in name_lower for kw in keywords):
            return dev, f"匹配设备名称关键字: '{dev.name}'"
            
    return None, None

def main():
    if os.geteuid() != 0 and not os.access("/dev/input/event0", os.R_OK):
        print("提示: 当前用户无权读取 /dev/input/event* 设备。")
        print("请使用 sudo 运行此脚本，例如:")
        print("  sudo python3 linux/test_remote_grab.py\n")

    devices = find_candidate_devices()
    if not devices:
        print("未发现可读取的输入设备。如果已连接遥控器，请使用 sudo 运行。")
        sys.exit(1)

    print("=== 当前检测到的输入设备列表 ===")
    for idx, d in enumerate(devices):
        vid_pid = f"VID:{d.info.vendor:04x} PID:{d.info.product:04x}"
        print(f" [{idx}] {d.path:<18} {vid_pid:<18} {d.name}")

    target_dev, match_reason = identify_remote(devices)
    
    if target_dev:
        print(f"\n[自动识别成功] 找到目标遥控器: {target_dev.name} ({target_dev.path})")
        print(f"匹配原因: {match_reason}")
    else:
        print("\n未自动匹配到小米遥控器。")
        print("如果遥控器已通过蓝牙连接，请输入对应设备的编号 [0-N]，或按 Ctrl+C 退出:")
        try:
            choice = input("请输入设备编号: ").strip()
            if not choice.isdigit() or int(choice) >= len(devices):
                print("无效编号，退出。")
                sys.exit(1)
            target_dev = devices[int(choice)]
        except KeyboardInterrupt:
            print("\n已取消。")
            sys.exit(0)

    print(f"\n正在接管设备: {target_dev.name} ({target_dev.path})")
    
    try:
        # 核心关键：独占抓取，阻止系统其他进程（包括桌面环境）接收事件
        target_dev.grab()
        print(">> [已开启独占 Grab]：遥控器的按键事件已被独占拦截，系统桌面不会产生任何默认反应！")
        print(">> 请尝试按下遥控器按键（方向键、OK、返回、Home 等）。按 Ctrl+C 退出。\n")
        
        for event in target_dev.read_loop():
            if event.type == e.EV_KEY:
                key_event = evdev.categorize(event)
                state_str = "按下 (DOWN)" if event.value == 1 else ("松开 (UP)" if event.value == 0 else "长按 (HOLD)")
                
                # 尝试获取按键名
                key_name = key_event.keycode if hasattr(key_event, 'keycode') else f"CODE_{event.code}"
                if isinstance(key_name, list):
                    key_name = "/".join(key_name)
                    
                print(f"[HID 事件] 按键: {key_name:<20} 键码: {event.code:<5} 状态: {state_str}")
                
    except KeyboardInterrupt:
        print("\n正在释放设备并退出...")
    except Exception as err:
        print(f"\n发生错误: {err}")
    finally:
        try:
            target_dev.ungrab()
            print(">> [已释放独占 Ungrab]：设备已恢复系统默认状态。")
        except Exception:
            pass

if __name__ == "__main__":
    main()
