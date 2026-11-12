# QQ 待办

把 NapCat 收到的 QQ 群通知转成待办。网页支持查看来源和原话、确认待办、完成和忽略。接收器和状态文件运行在自己的电脑上。iPhone 可以把私人 HTTPS 网页添加到主屏幕。

## Windows 快速开始

需要 Python 3.14、PowerShell 和已登录的 NapCat。在 NapCat 中启用 OneBot 11 正向 WebSocket，绑定本机 127.0.0.1:3001，设置访问令牌。在仓库根目录安装依赖。

```powershell
python -m pip install -r requirements.txt
```

先用合成消息打开演示页面，熟悉待做、待确认和手动状态操作。

```powershell
python -B .\outputs\trial.py --mode mock --state .\work\demo.json
python -B .\outputs\todo_server.py --state .\work\demo.json
```

浏览器打开 http://127.0.0.1:8765/。停止演示服务后再启动真实接收器。状态文件存放在 work 目录。

### 模式一 · 本地模型

先启动兼容 OpenAI Chat Completions 的本地 llama.cpp 服务，例如加载 Qwen 模型并监听 127.0.0.1:9931。将以下群号和班级替换为自己的配置。访问令牌只在 PowerShell 中输入。

```powershell
$env:NAPCAT_ONEBOT_TOKEN = [pscredential]::new('onebot', (Read-Host 'OneBot token' -AsSecureString)).GetNetworkCredential().Password
$env:TODO_EXTRACT_PROVIDER = 'local'
python -B .\outputs\napcat_pilot.py --group 123456789 --class-name '示例班' --state .\work\live.json --model-port 9931
```

本地模式使用本机模型处理消息。若要纳入全部群，可改用 --all-groups，并使用独立状态文件。设置 TODO_MUTED_GROUPS 可排除逗号分隔的群号。自定义班级别名、年级和校区可复制 outputs/personal-profile.example.json 为 outputs/personal-profile.json，班级必须与启动参数一致。

### 模式二 · DeepSeek API

仍在本机接收消息。程序先用规则筛出学校通知，再把必要文本发送给 DeepSeek。云端模式适用于全部群入口，启用前请确认允许把这部分文本交给 API。图片文件和分享卡片原始载荷由本机保留。

```powershell
$env:NAPCAT_ONEBOT_TOKEN = [pscredential]::new('onebot', (Read-Host 'OneBot token' -AsSecureString)).GetNetworkCredential().Password
$env:TODO_EXTRACT_PROVIDER = 'deepseek'
$env:DEEPSEEK_API_KEY = [pscredential]::new('deepseek', (Read-Host 'DeepSeek API Key' -AsSecureString)).GetNetworkCredential().Password
try {
    python -B .\outputs\napcat_pilot.py --all-groups --class-name '示例班' --state .\work\live-cloud.json
} finally {
    Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
}
```

正式运行用量写到 work/deepseek-live-usage.jsonl。停止时按 Ctrl+C。再次启动时使用同一模式、群范围、班级和状态路径。两种模式可以复用同一份已启用全部群的状态，切换前先停止旧接收器。

### 手机访问

电脑页面默认只监听 127.0.0.1:8765。局域网访问可在启动接收器前设置 TODO_LAN_ADDRESS、TODO_LAN_SUBNET 和 TODO_PHONE_PASSWORD，手机访问对应局域网地址的 8877 端口。防火墙仅放行所需的私有网段。

远程访问可先让手机和电脑连接同一个 Tailscale 私人网络。将 TODO_SERVE_HOST 设置成电脑的 Tailscale DNS 名称，并设置至少 16 个 ASCII 字符的 TODO_PHONE_PASSWORD，再启动接收器。在管理员终端使用 tailscale serve --bg http://127.0.0.1:8878，打开它输出的 HTTPS 地址，用户名为 todo。iPhone 用 Safari 的「添加到主屏幕」打开网页 App。Windows 重启后重新启动接收器，保持电脑开机且联网。

## 目录

- outputs/napcat_pilot.py 与 all_groups.py 接收、补查及去重。
- outputs/trial.py 与 live_cloud.py 负责模型调用和卡片校验。
- outputs/todo_server.py 与 todo.html 提供桌面和手机网页。
- outputs/mobile_sync.py 提供独立的整理稿同步服务，可通过 --help 查看用法。
- outputs/verify*.py 和样例 JSON 用于合成数据验证。
- work/ 是运行目录，包含本机状态、记录和用量。Git 会忽略它。

模型可能漏收、误判适用人群或补写时间。使用页面上的 QQ 原话核对重要事项。
