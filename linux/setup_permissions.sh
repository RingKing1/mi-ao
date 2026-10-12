#!/bin/bash
# 配置 Linux 系统的 udev 规则并添加用户到 input 组
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== 配置米遥 Linux 权限 ==="
if [ "$EUID" -ne 0 ]; then
    echo "请使用 sudo 运行此脚本以应用系统规则:"
    echo "  sudo bash $0"
    exit 1
fi

CURRENT_USER="${SUDO_USER:-$USER}"

echo "1. 复制 udev 规则到 /etc/udev/rules.d/..."
cp "$SCRIPT_DIR/99-miao.rules" /etc/udev/rules.d/99-miao.rules

echo "2. 重新加载 udev 规则..."
udevadm control --reload-rules
udevadm trigger

echo "3. 确保 uinput 内核模块已加载..."
modprobe uinput || true

echo "4. 将用户 '$CURRENT_USER' 加入 'input' 组..."
usermod -aG input "$CURRENT_USER"

# 临时设置当前 /dev/uinput 权限，避免必须注销重新登录
chmod 666 /dev/uinput 2>/dev/null || true

echo "=== 配置完成 ==="
echo "注意: 用户组变更可能需要注销或重新登录后全局生效；"
echo "当前会话也可直接使用 sudo 运行测试脚本。"
