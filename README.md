# GUET-AC · 桂电畅课助手

桂林电子科技大学畅课的 Python 命令行客户端。支持桂电统一身份认证直登、数字码/GPS 自动签到、花江校区建筑地点预设和按课程绑定地点，并提供二维码内容提交与可选的自动取码。

## 功能

- 桂电 CAS 直登：动态密码 salt、ASCII IV 的 AES-CBC、图形验证码交互、Cookie 完整跳转，登录后通过畅课业务 API 确认。
- 登录会话保存到系统凭据库，不保存学校密码；可用不同账号别名分别登录。
- 默认每 15 秒查询活动。数字码从接口读取；GPS 使用课程绑定地点或默认地点。
- 花江校区地点来自 OpenStreetMap，每条保留来源；预设 WGS84，默认转换 GCJ-02 后提交。
- 二维码可粘贴原始内容提交；当前会话有取码权限时，启用 `auto_qr` 可自动取码并提交。
- 每次提交前重查活动，每个活动在同一进程中最多提交一次，提交后回查本人状态。

本版是命令行项目。Android 客户端参考及已有 APK 见 [协议研究记录](docs/protocol.md)。

## 安装

需要 Python 3.11+ 和系统凭据库。Windows 默认使用凭据管理器。

```powershell
uv sync --extra dev
Copy-Item config.example.toml config.toml
uv run guet-ac login
uv run guet-ac locations
uv run guet-ac status
```

未安装 uv 时：`python -m pip install -e .`，随后直接运行 `guet-ac`。

## 配置签到地点

`config.toml` 可保存默认地点及各课程绑定，例如：

```toml
default_location = "osm-way-606411550" # 第一教学楼
poll_seconds = 15
accuracy = 35.0
submit_coordinate_system = "gcj02"
auto_qr = false

[course_locations]
"12345" = "osm-way-606478146" # 第二教学楼
"67890" = "osm-way-606478153" # 第三教学楼
```

课号使用 `status` 输出的畅课 `course_id`。以上两条课号为配置示例。课程绑定优先于默认地点；两者都为空时，只提示该 GPS 活动。

```powershell
uv run guet-ac once --dry-run # 只查询
uv run guet-ac once           # 查询并处理一次
uv run guet-ac watch          # 持续处理
uv run guet-ac number 12345 67890 # 输入数字码，参数为课程和活动编号
uv run guet-ac qr             # 粘贴二维码内容，为当前账号提交
```

二维码图片可先用扫码工具解出原始内容；本版不含相机或图片识别界面。`auto_qr=true` 通过当前学生会话请求服务器生成的有效 data，读不到时提示人工扫码。

## 多账号

```powershell
uv run guet-ac --account study login
uv run guet-ac --account study status
uv run guet-ac --account study watch
uv run guet-ac --account study logout
```

不同别名保存独立会话。每个命令处理所选账号；本版没有一次扫码批量处理多个账号的入口。

## 检查与来源

```powershell
uv run pytest
uv run ruff check .
```

实现测试使用模拟认证和业务响应。开发期间读取了桂电实际登录跳转与认证前端脚本；没有提供学生凭据、没有执行真实账号签到。

代码采用 [MIT](LICENSE)，地点数据库采用 [ODbL 1.0](guet_ac/data/README.md)。登录和二维码协议来源、已有手机 App 的完成度见 [docs/protocol.md](docs/protocol.md)。

## Windows exe

在 [Releases](https://github.com/dogxie2333/GUET-AC/releases/latest) 下载 Windows x64 压缩包并解压，在该目录打开 PowerShell，无需安装 Python：

```powershell
Copy-Item config.example.toml config.toml
.\GUET-AC.exe login
.\GUET-AC.exe locations
.\GUET-AC.exe status
.\GUET-AC.exe watch
```

这是控制台程序，命令参数与上文 `guet-ac` 相同。GitHub Actions 在 Windows 上构建，执行协议测试及 exe 启动、内置地点读取检查，并在版本标签推送后上传发行文件。
