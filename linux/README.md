# 米遥 (MI-AO) Linux 版使用指南

把小米蓝牙遥控器 2 Pro 变成 Linux 上的 AI 语音遥控仙女棒与无线飞鼠手柄。

---

## 架构特性

- **纯算法解码**：1:1 移植官方 IMA/DVI ADPCM 算法，无平台私有依赖。
- **GATT 语音桥接**：基于 Linux BlueZ 原生 D-Bus，稳定运行 ATVV v1.0 状态机。
- **本地极速 Whisper**：内存直传采样点推理，0.3 秒内完成中英文语音转文字。
- **硬件级按键独占**：基于 Linux `evdev` (`EVIOCGRAB`) 独占设备，彻底屏蔽系统原生按键副作用。
- **Wayland 原生兼容**：基于 Linux 内核 `/dev/uinput` 虚拟设备，完美兼容 GNOME Wayland 桌面。
- **systemd 守护进程**：开机后台静默自启，随时拿起遥控器即用。

---

## 常用管理命令

安装后可直接在任意终端运行：

```bash
mi-ao status    # 查看遥控器连接与服务状态
mi-ao start     # 启动后台守护服务
mi-ao stop      # 停止后台服务
mi-ao restart   # 重启后台服务
mi-ao logs      # 实时监听语音输入日志
mi-ao enable    # 设置开机/登录自动启动
mi-ao disable   # 取消开机自启
mi-ao test      # 在前台测试运行
```

---

## 遥控器操作指南

| 遥控器按键 | 功能说明 |
| --- | --- |
| **语音键** | **按住说话，松手识别**（0.3s 转写文字并自动复制到剪贴板） |
| **方向环** | **移动鼠标指针**（带平滑长按加速曲线） |
| **OK / 确认键** | **鼠标左键点击**（指针模式）/ 原生回车键（方向模式） |
| **返回键** | **鼠标右键点击**（指针模式）/ 原生 ESC 键（方向模式） |
| **音量 +/- 键** | **鼠标滚轮上下滚动** |
| **TV 键** | **模式切换**（在“无线飞鼠模式”与“键盘方向键模式”间切换） |

---

## 结果输出位置

- **剪贴板**：自动将最新识别文字放入系统剪贴板（推荐安装 `sudo apt install -y wl-clipboard`）。
- **最新文本文件**：`linux/recordings/latest_transcript.txt`
- **历史记录**：`linux/recordings/history.jsonl`
- **录音音频备份**：`linux/recordings/speech_YYYYMMDD_HHMMSS.wav`
