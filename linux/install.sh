#!/bin/bash
# 米遥 Linux 一键安装与配置脚本
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"

echo "=== 安装米遥 (MI-AO) Linux 版 ==="

# 1. 建立命令行软链接
mkdir -p "$BIN_DIR"
ln -sf "$DIR/mi-ao" "$BIN_DIR/mi-ao"
echo ">> [1/3] 命令行工具已安装到 $BIN_DIR/mi-ao"

# 2. 安装 systemd 用户服务
mkdir -p "$HOME/.config/systemd/user"
cp "$DIR/mi-ao.service" "$HOME/.config/systemd/user/mi-ao.service"
systemctl --user daemon-reload
echo ">> [2/3] systemd 后台服务单元已注册"

# 3. 检查与提示系统权限
echo ">> [3/3] 检查系统权限..."
NEED_SUDO=0
if [ ! -w "/dev/uinput" ]; then
    NEED_SUDO=1
fi

if [ "$NEED_SUDO" -eq 1 ]; then
    echo "--------------------------------------------------"
    echo "注意：为了让普通用户免 sudo 访问飞鼠与虚拟按键，"
    echo "请执行一次系统规则配置（需输入一次密码）："
    echo "  sudo bash $DIR/setup_permissions.sh"
    echo "--------------------------------------------------"
else
    echo ">> 系统输入权限正常！"
fi

echo ""
echo "🎉 安装完成！你现在可以使用以下命令进行日常管理："
echo "   mi-ao status   - 查看遥控器连接与后台服务状态"
echo "   mi-ao start    - 在后台启动米遥服务"
echo "   mi-ao stop     - 停止后台服务"
echo "   mi-ao logs     - 实时查看语音输入与识别日志"
echo "   mi-ao enable   - 设置开机自动在后台启动"
