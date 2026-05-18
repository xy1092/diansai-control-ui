# 电赛小车控制面板

这是 NUEDC 小车的独立 Web 控制面板，用于串口/ESP32 Wi-Fi 透传连接小车，实时查看遥测、调节 PID、导出黑匣子，并可接入 Claude Code / Codex 辅助调参。

## 功能

- 实时显示 `L`、`R`、`LINE`、`ANG` 四个 PID 通道
- 支持 `$SET` 写入 PID，`$DUMP` 读回当前参数
- 支持 USB 串口和 ESP32 Wi-Fi UART 透传
- 支持运行参数 `$CFGSET`
- 支持黑匣子 `$LOGDUMP` 导出 CSV
- 支持 AI 调试：
  - 本地规则调参
  - Claude Code CLI
  - Codex CLI
  - 自动闭环调参：开始后循环采样、分析、写入，直到稳定或手动停止

## 安装

```bash
git clone git@github.com:<your-github-user>/diansai-control-ui.git
cd diansai-control-ui
./scripts/setup.sh
```

## 启动

推荐用模式编号启动：

```bash
./scripts/start.sh 0   # 有线 USB，默认 /dev/ttyACM0
./scripts/start.sh 1   # ESP32 Wi-Fi 透传，只在电脑本机打开
./scripts/start.sh 2   # 手机模式，监听 0.0.0.0:8765
```

有线端口不是默认值时：

```bash
./scripts/start.sh 0 /dev/ttyACM1
```

仍然支持直接传连接地址：

```bash
./scripts/start.sh /dev/ttyACM0
./scripts/start.sh socket://192.168.4.1:3333
```

启动后默认电脑访问：

```text
http://127.0.0.1:8765/
```

手机模式下，手机和电脑都连接 `NUEDC-CAR-UART` 后，用手机浏览器打开电脑在热点里的地址，例如：

```text
http://<电脑在热点中的IP>:8765/
```

如果手机打不开，优先确认手机 Wi-Fi 代理/VPN/私有 DNS 已关闭，并确认电脑防火墙允许 `wlan0` 入站访问 `8765/tcp`。

## AI 调试

面板的 `AI 调试` 页可以选择：

- `本地规则`
- `Claude Code`
- `Codex`

选择 Claude/Codex 时，需要本机已安装并登录对应 CLI：

```bash
claude --help
codex --help
```

AI 调参流程：

1. 连接小车
2. 让小车运行并产生遥测
3. 选择通道和 AI 引擎
4. 点击 `分析建议` 查看建议
5. 点击 `应用建议` 写入
6. 或点击 `开始自动` 让面板循环调参

安全限制：

- 单轮 PID 变化不超过当前值的 20%
- PID 参数不会写成负数
- 模型输出必须是 JSON，格式错误会回退本地规则
- Claude/Codex 超时或不可用时自动回退本地规则

## 常用连接

```text
/dev/ttyACM0
socket://192.168.4.1:3333
```

默认波特率：

```text
115200
```

## 目录

```text
server.py              FastAPI 后端
web/                   前端页面
scripts/setup.sh       创建 Python 环境并安装依赖
scripts/start.sh       启动控制面板
requirements.txt       Python 依赖
```
