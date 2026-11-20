# GUET-AC 与 EMB 猫猫交接

状态快照：2026-11-20 23:47（北京时间）。已核对 Pi 的 systemd、实际畅课查询、QQ 桥接计数和提醒授权记录。本文件不包含密码、Cookie、QQ 身份标识或二维码载荷。

## 1. 现在运行着什么

| 链路 | 当前状态 | 核对证据 |
| --- | --- | --- |
| 桂电网页登录 → Pi 会话 | 已接通 | 在本机独立 Chrome 完成官方认证，Pi 验证并保存了畅课会话。本次在 Pi 实跑只读查询，退出码为 0。 |
| Pi 常驻监控 | 已启用 | `guet-ac.service` 用户单元 active，重启计数为 0。实际参数是 `watch --auto-number --event-dir …/events`，数字码可提交，GPS 和二维码只记录开始。 |
| 自动数字签到 | 已启用，真实提交未验收 | 畅课向当前登录会话提供数字码时才提交一次，再回查状态。31 项 CLI 测试此前通过，快照时事件目录无 JSON，当前没有可用于验收的活动。 |
| QQ 私聊直接发图 | 已上线 | 不用发送命令或绑定。官方 SDK、附件处理、Pi 解码和被动回复体已用隔离样本串通。当前桥接记录 2 条真实私聊、2 次 QQ 接口接受回复、0 次发送错误。这些累计值不证明两条都为识别成功，也不代表用户客户端已确认显示。 |
| `/畅课状态` | 已上线 | 不用绑定，只回报 Pi 的监控进程状态。已在 Pi 上验证无桌面会话时也可查询 systemd 用户单元。 |
| 签到事件主动通知 | 模块已上线，QQ 权限未开通 | 已实现读取开始和结果事件，并限制为唯一已绑定、已订阅且获 QQ 平台许可的私聊。当前主动消息许可数为 0、订阅数为 0，没有实际推送。 |
| QQ 图片 → 畅课扫码提交 | **未接入** | QQ 图片只识别并回复，不把载荷交给提交逻辑。不能把“识别出二维码”当成“已签到”。 |
| GPS 自动签到与网页配置 | **未接入** | Pi 上 64 条花江地点预设，默认地点和课号绑定为空。网页是浏览器本地模拟，未连接 Pi。线上 GPS 保持开始提醒。 |

## 2. 后续工作与验收顺序

1. **QQ 收图与回执。** 让用户直接发一张普通测试二维码，并保留客户端回执结果。对照同一分钟的网关 C2C、SDK C2C、发送成功和发送错误计数。对真实图片验收时要分别确认附件下载、解码、QQ 接受发送和客户端显示。现有回执会把下载异常、文件超限和解码失败合成同一句，下一轮应先把这几种失败拆开。日志只记录阶段、错误类别、耗时和图片尺寸，避免记录附件 URL、二维码内容或会话凭据。
2. **投影小码识别。** 线上用 wxscan（WeChat 二维码算法的 Rust 移植，CNN 检测和超分辨率模型已加载），失败后走 ZXing 和分区增强。对照测试中 ZBar 没读出，普通 OpenCV 检测器定位后也未解码。OpenCV `QRCodeDetectorAruco` 对放大的 QQ 预览能定位，透视拉正后交给 ZXing 能读出。它未部署到 Pi。取得这张照片的原图及 QQ 实际压缩版本后，用整图对照，再决定是否加入 Aruco 作为兜底。验收覆盖远拍、反色、暗投影、多码、普通无二维码照片以及单次处理超时。
3. **主动通知。** 用户已指定发给本人已绑定的猫猫私聊。还需在 QQ 侧开启接收主动消息，再在 EMB 提醒页订阅对应私聊。授权后，以一条明确标注为测试的事件验证 QQ 与客户端回执，再观察真实活动。通知模块只处理 15 分钟内的事件，使用独占 `.dispatch` 文件记录发送尝试。发送结果不明时不会自动重试，排查时保留这些文件。
4. **数字签到真实验收。** 等实际数字签到活动，检查开始事件、一次提交以及畅课本人签到状态。仅当回查确认时显示“已确认完成”。没有真实活动时继续使用模拟测试，不伪造真实结果。
5. **扫码提交与课程地点。** QQ 与畅课写入仍是独立链路。扫码需要先核对校方支持的网课入口及适用范围，再以学校支持的方式测试。课堂图像识别成功只说明解码成功。课程地点模拟页可验证不同课号独立选点，真实 GPS 监控继续提醒，预录坐标不作实际位置提交。

EMB 主动提醒设置：<https://ai.guet.win/?plugin=reminders>

## 3. 运行入口与源码

- GitHub：<https://github.com/dogxie2333/GUET-AC>。Windows 桌面发行：<https://github.com/dogxie2333/GUET-AC/releases/tag/desktop-v0.2.0>。
- 本机仓库：`C:\Users\DogXie\Documents\Codex\2026-11-19\wh\outputs\GUET-AC`。快照时本地 `main` 为 `af426c1`，比已记录的 `origin/main` 多 15 个提交。Pi 上有另外部署的 EMB 桥接补丁，不能从 Windows 发行包推断线上状态。
- Pi SSH：`orangepi@172.16.8.147`。应用目录：`~/.local/share/guet-ac/`，Python 3.10.22 虚拟环境：`.venv/`。服务单元在 `~/.config/systemd/user/`，参数覆盖文件为 `guet-ac.service.d/attendance.conf`。会话目录 `session/` 由本机用户保护。不要复制、粘贴或写入仓库。
- 核心源码：`guet_ac/cli.py`、`guet_ac/client.py`、`guet_ac/store.py`、`guet_ac/config.py`。本机官方网页登录入口：`scripts/guet_browser_login.py`。
- 识别源码：`scripts/guet_qr_preview.py`、`scripts/guet_wxscan.mjs`。Pi 的模型、上游 worker 和 Wasm 在 `~/.local/share/guet-ac/wxscan/`。上游 scanner 为 wxscan-rs v0.1.1，LiteRT runtime 为 v2.17.1-b2，检测与超分辨率权重来自 [wxscan-weights](https://github.com/wilinz/wxscan-weights)。
- QQ 模块：`scripts/emb_guet_private.mjs`、`scripts/emb_guet_notify.mjs`、`scripts/qq-edge-lookup.mjs`。实际生产入口为系统单元 `emb-qq-bridge.service` 启动的 `~/emb-maibot-staging/emb-dsh-qqbot-staging/qq-maibot-live.js`。私聊路由嵌在相邻 `emb-qq-official/main.js`，主动通知挂在 `emb-plugin.js`，被动回复的连接超时重试在 `sdk-transport.js`。
- 课程地点模拟页：`dist/guet-course-location-simulation.html`。只存在于本机构建目录，通过浏览器存储独立模拟课号的绑定。
- 普通测试二维码：Pi 应用目录下 `guet-offline-qr-probe.png`。它的内容是测试标记，用于识别验收。

## 4. 排查与恢复

以下命令在 Pi 上运行。它们检查状态或只读查询，不主动发消息或提交签到。

```bash
cd ~/.local/share/guet-ac
systemctl --user show guet-ac -p ActiveState -p NRestarts -p ExecStart
GUET_AC_SESSION_DIR="$PWD/session" .venv/bin/guet-ac once --dry-run
.venv/bin/python guet_qr_preview.py guet-offline-qr-probe.png
systemctl show emb-qq-bridge -p ActiveState -p NRestarts -p ExecMainStartTimestamp
curl -sS -o /dev/null -w 'QQ health %{http_code}\n' http://127.0.0.1:8812/health
journalctl --user -u guet-ac --since '15 min ago'
```

- **QQ 显示异常或无回复。** 桥接进程 active 不表示网关已登录。健康检查 `200` 和日志中的 `OFFICIAL_MAIBOT_QQ_READY` 才是连接就绪证据。SDK 的认证和网关查询实际走 `api.bot.qq.com`，网关 URL 返回 `api.sgroup.qq.com`。Pi 直连 QQ 的部分 DNS 地址会超时。现在线上有 QQ 专用地址优选、认证和查询重试，以及启动超时恢复。被动文本和 Markdown 只对明确未建立连接的错误重试一次，保留原回复序号。不要为了检查状态反复重启。
- **识别异常。** 先看网关有没有收件。如果收件和回复都正常但回复“没能识别”，取原图做本地解码对照，再区分附件下载和解码。QQ 图片服务的签名地址勿写入文档。当前分区增强在 Pi 上比旧版能多读出一部分小码，极小码仍可能失败。
- **畅课会话失效。** 在本机仓库用 `uv run --no-project --python 3.10 --with playwright python scripts/guet_browser_login.py`。它打开独立 Chrome，通过校方认证后等待畅课资料接口确认，再由用户确认通过 SSH 交付仅限畅课域名的会话。导入后在 Pi 先跑只读查询。
- **需要暂时关闭自动数字提交。** 把用户单元覆盖里的 `--auto-number` 换成 `--dry-run`，保留 `--event-dir`，随后运行 `systemctl --user daemon-reload` 和 `systemctl --user restart guet-ac`，再查实际 `ExecStart`。
- **回滚。** Pi 上保留了识别器 `guet_qr_preview.py.before-detail-pass`，恢复该文件后下条图片消息即使用旧识别器。QQ 桥接各次变更也保留了相邻备份，如 `emb-plugin.js.before-guet-notify`、`sdk-transport.js.before-markdown-retry` 和 `qq-maibot-live.js.before-boot-retry`。只回滚关联文件，先检查差异和 Node 语法，再重启 `emb-qq-bridge.service` 并确认健康检查为 `200`。

## 5. 上游完整识别方案对照

使用同一张用户提供的 QQ 聊天截图，仅统计检测数和解码数，未输出二维码内容。

| 上游方案 | 整张截图 | QQ 图片预览 | 投影区域 |
| --- | --- | --- | --- |
| QReader 3.16，官方 small 权重，置信度阈值 0.25 | 检测 0，解码 0 | 检测 1，解码 0 | 检测 1，解码 0 |
| OpenCV contrib 4.12.0，原生 WeChatQRCode + 上游 Caffe 权重 | 解码 0 | 解码 0 | 解码 0 |

OpenCV 用默认缩放和原分辨率各跑一轮，两个设置结果相同。两种方案均直接调用上游检测与解码接口，无自定义组合，因此本轮不替换 Pi 上的识别器。下一轮使用课堂原图和 QQ 接收的压缩图做同源对比；单张聊天截图的结果不能代表课堂原图。
