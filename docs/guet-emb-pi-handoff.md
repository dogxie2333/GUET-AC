# GUET-AC 与 EMB 猫猫交接

状态快照：2026-11-21 01:05（北京时间）。已核对 Pi 的 systemd、畅课只读查询、桥接健康检查、Web 配置 API 和二维码全链路本地探针。本文件不包含密码、Cookie、QQ 身份标识或二维码载荷。

## 1. 现在运行着什么

| 链路 | 当前状态 | 核对证据 |
| --- | --- | --- |
| 桂电网页登录 → Pi 会话 | 已接通 | Pi 上只读 `status --json` 返回本人资料；会话目录 0700、会话文件 0600。 |
| Pi 常驻监控 | 已升级 | `guet-ac.service` 用户单元 active、NRestarts=0，实际参数为 `watch --auto --event-dir …/events`；配置热加载已启用，网页改配置无需重启服务。 |
| 自动数字签到 | 已启用，真实提交待活动 | 数字码读取→提交→回查；跨重启去重，只有畅课回查确认才显示“已确认完成”。本轮 31 项旧测试 + 新增测试全部通过。 |
| GPS 自动签到 | 已启用，真实活动待验收 | `auto_gps=true`，按 `course_locations`/`default_location` 取花江地点并 WGS84→GCJ-02 提交；未绑定地点时只发 `action_required`，不假提交。 |
| 课程地点网页 | **已接 Pi** | <https://ai.guet.win/guet-locations.html>，仅管理员；`GET /api/guet-config` 实测 200，返回 64 个地点、当前配置和服务状态；保存/取消绑定与 auto_* 开关走 `POST`。 |
| QQ 私聊发图 → 畅课扫码提交 | **已接通真实 QQ 链路，待真实活动** | 私聊图片 → 下载 → 识别 → 解析 → `qr-submit` → 回查 → 分阶段回执；用户实测收到“二维码有效，但提交被畅课拒绝（可能活动已结束）”。按用户明确要求，任意私聊均可提交，不再要求 `/bind`；每私聊 10 分钟最多 8 张图。 |
| 投影小码识别 | Aruco 已部署 Pi | `wxscan → zxing → detail → aruco`；Aruco 在 `systemd-run --user --scope` 中运行，避开桥接 150 MiB cgroup；Pi 合成投影图实测 Aruco 定位 1 个并解码成功。 |
| 签到事件主动通知 | **已打通真实推送，用户已确认收到** | 唯一已绑定私聊、15 分钟窗口、`.dispatch` 去重；真实主动消息 API 实测 HTTP 200 且返回消息 ID，测试事件 `.dispatch` 写 `sent`，用户确认两条测试消息都收到。QQ 的 `C2C_MSG_RECEIVE` 生命周期事件未在网关上观察到，因此改用“发送结果”证明平台许可，不再依赖 EMB 网页订阅。 |
| VPS 手机扫码页 | 教程已写好，静态页只显示结果 | [vps-public-qr-scan-guide.md](vps-public-qr-scan-guide.md)；不连接 Pi、不提交签到，提交链路走 QQ 照片或网页配置后的 Pi 监控。 |

## 2. 本轮补齐的内容

### 2.1 自动数字签到

- 发起写请求前先把活动状态持久化为 `submitting`；请求后写 `confirmed`/`unconfirmed`。同一活动在任意重启前后最多一次写请求，避免重复提交。
- 启动时发现仍进行中的 `submitting`：不再提交，产生一次“上次提交结果不明”的 `unconfirmed` 事件。
- 只有 `Client.verify` 回查确认本人在签到名单中才发 `confirmed`；否则发 `unconfirmed`，不伪造完成。
- 数字码读取失败、活动未进行等场景发送 `action_required`，方便主动通知里提示人工处理。
- 事件目录新增 `state.json`（0600、原子替换、14 天/500 条清理），不会被 64 位十六进制事件扫描误读。

### 2.2 GPS 自动签到与课程地点网页

- 新增配置 `auto_number`、`auto_gps`、`auto_qr`；Pi 服务用 `watch --auto` 严格按配置执行。
- GPS 提交复用 `Client.submit(place=…, accuracy=…, coordinate_system=…)`，使用课程绑定地点或默认地点；未绑定地点时 `action_required`，不提交。
- `guet-ac` 新增 `config-json`、`config-check`、`config-update`、`locations --json`、`status --json`、`qr-submit`、`test-event`。
- `config-update` 由网页调用：只允许 8 个配置字段、复用 Python 校验、写前备份 `config.toml.before-web-<UTC>`、0600 原子替换。
- Web 端 `deploy/emb-web-ai/guet-config.mjs` 挂载 `/api/guet-config`：仅管理员、POST 必须同源、子进程超时、状态读取失败降级；页面 `web/guet-locations.html` 部署为 `/guet-locations.html`，同时保留 `file://` 离线模拟模式。
- 页面可浏览课程（来自 `status --json` 去重）、64 个 OSM 地点、已有绑定，并切换 `auto_number`/`auto_gps`；监控服务每轮检测配置 mtime，热加载新配置。

### 2.3 QQ 扫码照片全链路

- 私聊图片处理顺序：附件下载 → `guet_qr_preview.py --json` → 解析畅课载荷 → `guet-ac qr-submit` → 回查 → 单条回执。
- 回执区分：下载失败、超过 8 MiB、图片无法读取、无二维码、多二维码、不是畅课码、会话失效、活动已结束/提交被拒、网络中断、已提交未确认、已确认。
- 按用户明确要求，**任意私聊**发图都会识别并提交，不再要求 `/bind`；未绑定只影响“签到事件推送目标”，不影响照片提交。每私聊 10 分钟最多 8 张图，防止刷解码。
- 风险提示：当前 `c2cMode=open`，能私聊猫猫的人都可以让机器人用号主畅课会话提交。若以后要收紧，可把 QQ c2c 改为 allowlist，或在 `emb_guet_private.mjs` 恢复 bound 判断；本轮按用户选择保持开放提交。
- 附件 URL 只允许 `https` 且主机属于 `qpic.cn`/`qq.com`/`qq.com.cn`；临时图片用后立即删除；日志不记录 URL、二维码内容或会话凭据。
- 解码器修正：zxing-cpp 的 `.text` 会把 `\x10/\x1f/\x1e` 显示转义为 `<DLE>` 等，改用 `item.bytes` 还原；每个解码候选必须先通过 `qr.parse` 才采用，前序解码器的坏文本不会挡住后续兜底。

### 2.4 投影小码 Aruco

- `scripts/guet_qr_preview.py` 新增 Aruco 兜底：`cv2.QRCodeDetectorAruco.detect` → 四点排序 → 透视拉正 → zxing-cpp。cv2 缺失、异常、超时都安全跳过。
- 桥接进程的 `MemoryMax` 为 150 MiB，直接 import OpenCV 会顶到上限；新增 `scripts/guet_aruco_worker.py`，通过 `systemd-run --user --scope` 在 user slice 中运行，重新 import OpenCV 并回传解码文本。`GUET_AC_ARUCO_SCOPE=1` 由私聊路由传入。
- Pi 合成投影图实测（`guet_qr_bench.py`）：clean/far-small/low-contrast 均可解码（decoder=zxing），clean 的 Aruco 单独路径也成功；scoped Aruco 实测 0.45 s 返回 1 个候选。真实课堂原图尚未取得，未把“合成图通过”当成“教室原图已验收”。

### 2.5 主动通知

- `knownStages` 增加 `action_required`；`test-event` 写入带 `【测试】` 前缀和 `test:true` 的事件，走与真实事件相同的投递路径。
- 只向**唯一**已绑定私聊发送；不再要求 EMB 提醒页勾选订阅，也不依赖未到达的 `C2C_MSG_RECEIVE` 生命周期事件。QQ 客户端仍需开启机器人资料卡的“允许主动发送/主动消息”开关，平台会在真正发送时校验。
- 发送成功写 `.dispatch` 的 `sent`；结果不明写 `unconfirmed`，不自动重试；15 分钟窗口和每轮最多 4 条保持不变。通用提醒任务仍走原来的订阅/权限模型，不受此改动影响。
- 实测：直接调用主动消息 API 返回 HTTP 200 和消息 ID；用 `test-event` 生成事件后，桥接约 5 秒内发送，`.dispatch` 内容为 `sent`，健康计数 `sendCount` 增加，用户已确认客户端收到。

## 3. 运行入口与源码

- GitHub：<https://github.com/dogxie2333/GUET-AC>。本机仓库：`C:\Users\DogXie\Documents\Codex\2026-11-19\wh\outputs\GUET-AC`。
- Pi SSH：`orangepi@172.16.8.147`。应用目录 `~/.local/share/guet-ac/`，Python 3.10.22 虚拟环境 `.venv/`，会话 `session/`，事件与去重状态 `events/`。
- 服务：用户单元 `guet-ac.service`，覆盖文件 `guet-ac.service.d/attendance.conf`；实际命令 `watch --auto --event-dir …/events`。
- 核心源码：`guet_ac/cli.py`、`client.py`、`config.py`、`store.py`、`ledger.py`；网页 API `deploy/emb-web-ai/guet-config.mjs`；识别器 `scripts/guet_qr_preview.py`、`scripts/guet_aruco_worker.py`、`scripts/guet_qr_bench.py`、`scripts/guet_wxscan.mjs`。
- QQ 模块：源码 `scripts/emb_guet_private.mjs`、`scripts/emb_guet_notify.mjs`；生产入口为系统单元 `emb-qq-bridge.service` 启动的 `~/emb-maibot-staging/emb-dsh-qqbot-staging/qq-maibot-live.js`。私聊路由在相邻 `emb-qq-official/main.js` 与 `emb-guet-private.mjs`，主动通知挂在 `emb-plugin.js`。
- Web：`~/emb-web-ai/server.mjs` 已注入 `guet-config.mjs`；页面 `~/emb-web-ai/public/guet-locations.html`（仓库源 `web/guet-locations.html`）。入口 <https://ai.guet.win/guet-locations.html>。
- 地点数据：`guet_ac/data/huajiang.json`（64 条，OSM/ODbL）。课程地点网页必须管理员登录；提醒页仍为 <https://ai.guet.win/?plugin=reminders>。
- 版本：`guet-ac 0.3.0` wheel 在 `dist/guet_ac-0.3.0-py3-none-any.whl`（构建产物，仓库 `.gitignore` 掉 `dist/`）。Pi 已安装 0.3.0。

## 4. 验收顺序（含仅剩的用户操作）

1. **QQ 主动通知授权（已由用户完成）。** 手机 QQ 打开猫猫私聊 → 机器人头像/资料卡 → 开启“允许主动发送/主动消息”开关。平台侧已用真实主动消息 API 验证可发送。
2. **主动通知测试（Pi 命令）。** 在 Pi 运行：
   ```bash
   cd ~/.local/share/guet-ac
   .venv/bin/guet-ac test-event --event-dir events --channel 数字码 --stage confirmed
   sleep 8
   ls -la events | tail -5
   ```
   预期出现事件文件和同名 `.dispatch`（内容 `sent`），QQ 收到“【测试】数字码签到已确认完成。”。不需要 EMB 提醒页订阅；未收到时见第 5 节。
3. **QQ 照片链路（已由用户实测）。** 发送 `dist/guet-qr-chain-test.png`，收到“二维码有效，但提交被畅课拒绝（可能活动已结束）喵。”，证明下载→识别→解析→提交→回执整条链路可用；有真实活动时才会出现“已确认签到”。任意私聊均可提交（含未绑定），每私聊 10 分钟最多 8 张图；未绑定只影响主动通知目标，不影响提交。
4. **自动数字/GPS 真实验收。** 等真实数字码或 GPS 活动出现：检查事件目录 `started`、一次提交、`confirmed`；GPS 需先在课程地点网页绑定对应 `course_id`，否则只会收到 `action_required`。没有真实活动时继续用 `pytest`/`once --dry-run`，不伪造真实结果。
5. **投影小码真实原图。** 取得教室投影照片后，在 Pi 运行 `python guet_qr_bench.py 照片路径`，只统计检测/解码与耗时，不输出内容；再决定是否需要调整 Aruco 参数。

## 5. 排查与恢复

以下命令在 Pi 上运行，均不主动发消息或提交签到。

```bash
cd ~/.local/share/guet-ac
systemctl --user show guet-ac -p ActiveState -p NRestarts -p ExecStart
GUET_AC_SESSION_DIR="$PWD/session" .venv/bin/guet-ac --config config.toml status --json
GUET_AC_SESSION_DIR="$PWD/session" .venv/bin/guet-ac --config config.toml once --dry-run
GUET_AC_SESSION_DIR="$PWD/session" .venv/bin/guet-ac --config config.toml config-json
.venv/bin/python guet_qr_preview.py --json guet-offline-qr-probe.png
.venv/bin/python guet_qr_bench.py /path/to/classroom-photo.jpg
systemctl show emb-qq-bridge -p ActiveState -p NRestarts -p MemoryCurrent
curl -sS -o /dev/null -w 'QQ health %{http_code}\n' http://127.0.0.1:8812/health
curl -sS -o /dev/null -w 'Web health %{http_code}\n' http://127.0.0.1:8796/health
journalctl -u emb-qq-bridge --since '15 min ago' --no-pager
```

- **QQ 显示异常或无回复。** 健康检查 200 且 `connected:true`、日志出现 `OFFICIAL_MAIBOT_QQ_READY` 才算连接就绪。不要为检查状态反复重启；桥接认证和网关查询走 `api.bot.qq.com`，Pi 直连部分 QQ DNS 会超时，线上已有地址优选和启动重试。
- **主动通知不发送。** 依次核对：私聊是否唯一绑定；QQ 资料卡“允许主动发送/主动消息”开关是否开启；事件是否在 15 分钟内；`.dispatch` 是否已存在。桥接不依赖 `C2C_MSG_RECEIVE` 生命周期事件，直接看发送结果：`.dispatch` 为 `sent` 表示 QQ 已接受，`unconfirmed` 表示网络/平台结果不明且不会重试。若发送被平台拒绝，检查客户端开关、平台主动消息额度和机器人认证状态。
- **QQ 自动消息生命周期事件未观察到（已知）。** `emb-qq-bridge` 能收到普通 C2C 消息，但 `C2C_MSG_RECEIVE`/`C2C_MSG_REJECT` 未在网关注入；这不影响 GUET 主动通知，因为投递改用真实发送结果验证。若以后要做“用户关闭通知立即停止发送”的实时联动，需要在 SDK 网关层继续排查 intent/事件分发，不能用轮询猜测。
- **发图无回执或回执异常。** 先看桥接健康计数和日志。回执已区分下载/解码/解析/会话/提交/网络阶段；需要定位时取原图在 Pi 上跑 `guet_qr_preview.py --json`，不要记录二维码内容。wxscan 模型目录为 `~/.local/share/guet-ac/wxscan/`。
- **Aruco 不生效。** 确认 `scripts/guet_aruco_worker.py` 存在、`~/.local/bin/systemd-run` 可用、桥接进程能访问 `/run/user/1000/bus`；Aruco 只在其他解码器失败后触发，失败会静默跳过，不影响普通识别。临时禁用可把 worker 改名（scoped 调用会安全返回空）。
- **畅课会话失效。** 在本机仓库运行 `uv run --no-project --python 3.10 --with playwright python scripts/guet_browser_login.py`，通过校方认证后由用户确认通过 SSH 交付仅限畅课域名的会话；导入后在 Pi 先跑 `status --json`。
- **网页配置异常。** `GET /api/guet-config` 仅管理员可用；未登录返回 302/401 是预期。检查 `journalctl` 中 emb-web-ai 日志和 `~/.dsh/emb/webai.log`；页面源 `web/guet-locations.html`，注入脚本 `scripts/stage_web_guet.py` 可重复运行（幂等）。
- **需要关闭 GPS 自动提交。** 在网页关闭 auto_gps，或执行：
  ```bash
  printf '%s' '{"auto_gps":false}' | GUET_AC_SESSION_DIR="$PWD/session" .venv/bin/guet-ac --config config.toml config-update
  ```
  热加载会在下一轮生效；要连数字码一起关闭，把 auto_number 也设为 false，或把服务参数改回 `--dry-run` 后重启用户单元。
- **回滚（按改动范围选）。**
  - 应用/CLI：部署备份在 `~/.local/share/guet-ac/.deploy-backup-<UTC>/`（含旧脚本、旧 `config.toml`、旧 site-packages `guet_ac`），恢复后重装旧 wheel（目录内保留 `guet_ac-0.2.0-py3-none-any.whl`）并重启 `guet-ac`。
  - 桥接：`.bridge-deploy-backup-<UTC>/` 保存旧 `emb-guet-private.mjs` 与 `emb-guet-notify.mjs`；恢复后 `kill` 当前 MainPID，systemd 会自动重启。
  - Web：`~/.web-deploy-backup-<UTC>/` 保存旧 `server.mjs`；恢复后 `kill` 当前 MainPID，systemd 自动重启；`guet-config.mjs` 可直接删除（删除后 `/api/guet-config` 返回 404，页面显示接口不可用）。
  - 配置：`config.toml.before-web-<UTC>` 为每次网页写回的自动备份；恢复后无需重启（热加载）。
  - 服务参数：原覆盖文件为 `watch --auto-number --event-dir …`（GPS 只提醒），恢复后 `systemctl --user daemon-reload && systemctl --user restart guet-ac`。
- **颜色/日志安全。** 日志只记录阶段、错误类别、耗时和图片尺寸；附件 URL、二维码 data、Cookie 不写入日志、文档或提交。

## 6. 验证记录

- 本地测试：`pytest -q` **106 passed**；Node `node --test tests/node/*.test.mjs tests/web/guet-config.test.mjs` **30 passed**；`ruff check` 通过。
- Pi 安装：`guet-ac 0.2.0 → 0.3.0`，`guet-ac` 用户单元 active、NRestarts=0、ExecStart 为 `watch --auto --event-dir …/events`。
- Pi 只读：`status --json` 返回本人资料和空活动列表；`config-json` 显示 `auto_number=true`、`auto_gps=true`；`locations --json` 返回 64 地点。
- Pi 解码：wxscan 读出普通和紧凑码（compact 控制字节解析正确）；Aruco scoped worker 0.45 s 返回候选；合成投影基准 clean/far-small/low-contrast 可解码，inverted 由 zxing try_invert 解出，multiple/no-qr 分类正确。
- Pi 全链路真实 QQ：用户发送 `dist/guet-qr-chain-test.png`，约 1.3 s 内收到“二维码有效，但提交被畅课拒绝（可能活动已结束）喵。”，证明 QQ 附件下载、wxscan 识别、载荷解析、`qr-submit` 会话读取和分阶段回执全部打通；未绑定私聊也走同一提交路径（用户要求），仅受每私聊图片频率限制。
- Web API：管理员 GET 200（64 地点、service auto 三项 true）；POST 绑定/取消绑定 200；未登录 302/401 符合预期。
- 主动通知：直接调用 QQ 主动消息 API 返回 HTTP 200 且带消息 ID；`test-event` 生成事件后桥接约 5 秒内发送，`.dispatch` 内容为 `sent`；用户已确认收到“【测试】GUET 主动通知链路探测”和“【测试】数字码签到已确认完成”两条消息。

## 7. 上游识别方案对照（保留）

使用同一张用户提供的 QQ 聊天截图，仅统计检测数和解码数，未输出二维码内容。

| 上游方案 | 整张截图 | QQ 图片预览 | 投影区域 |
| --- | --- | --- | --- |
| QReader 3.16，官方 small 权重，置信度阈值 0.25 | 检测 0，解码 0 | 检测 1，解码 0 | 检测 1，解码 0 |
| OpenCV contrib 4.12.0，原生 WeChatQRCode + 上游 Caffe 权重 | 解码 0 | 解码 0 | 解码 0 |

OpenCV 用默认缩放和原分辨率各跑一轮，两个设置结果相同。本轮新增 OpenCV `QRCodeDetectorAruco` 定位 + 透视拉正 + zxing-cpp 兜底，并已部署 Pi；真实课堂原图对照仍待补。
