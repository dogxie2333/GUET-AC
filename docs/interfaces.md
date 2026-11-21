# 本轮补齐的内部接口契约（2026-11-21）

本文件是并行开发的唯一接口来源。任何一方需要改契约，先通知 Lead，不得单方面改字段。

## 1. Python CLI 新增/变更

全局参数保持 `--account`、`--config PATH` 在子命令之前。

### `guet-ac config-json`

只读，stdout 输出单行 JSON，不含 Cookie、会话或密码：

```json
{
  "default_location": "",
  "course_locations": {"11": "osm-way-606411550"},
  "auto_number": true,
  "auto_gps": true,
  "auto_qr": false,
  "poll_seconds": 15,
  "accuracy": 35.0,
  "submit_coordinate_system": "gcj02"
}
```

配置缺失字段按默认值补齐：`auto_number=false`、`auto_gps=false`、`auto_qr=false`、`default_location=""`、`course_locations={}`、`poll_seconds=15`、`accuracy=35.0`、`submit_coordinate_system="gcj02"`。

### `guet-ac locations --json`

stdout 输出 `{"places":[{"id","name","latitude","longitude","source","method"?}]}`。不带 `--json` 时保持现有制表符文本格式。

### `guet-ac config-update`

stdin 读取一个 JSON 对象，允许键：`default_location`、`course_locations`、`auto_number`、`auto_gps`、`auto_qr`、`accuracy`、`poll_seconds`、`submit_coordinate_system`。未提供的键保持原值。

- 复用 `guet_ac.config.load` 的校验；未知地点、非法课号、非法布尔值必须拒绝。
- 写入前把原文件备份到同目录 `<config>.before-web-<UTC时间戳>`（例如 `config.toml.before-web-20261121T001500Z`），权限 0600，原子替换。
- 成功：stdout 输出写回后的 config JSON（同 `config-json`），退出码 0。
- 失败：stderr 输出单行 JSON `{"error":"中文原因"}`，退出码 2，不得改写原文件。

### `guet-ac config-check`

只读，配置合法退出 0；非法时 stderr `{"error":"..."}`，退出码 2。

### `guet-ac status --json`

stdout 输出单行 JSON：

```json
{
  "profile": {"id": "123", "name": "姓名"},
  "rollcalls": [
    {"id": "22", "course_id": "11", "course_title": "课程名",
     "status": "absent", "kind": "数字码", "active": true}
  ],
  "at": "2026-11-21T00:15:00+00:00"
}
```

`kind` 取值与 `client.kind` 一致：`GPS`、`数字码`、`二维码`、`其他`。失败时 stderr 单行 JSON `{"error":"..."}`，退出码 2。不得输出 Cookie、会话或原始响应。

### `guet-ac qr-submit`

stdin 读取单个 JSON 对象，二选一：

```json
{"courseId": "11", "rollcallId": "22", "data": "..."}
```

或

```json
{"text": "二维码原始内容"}
```

行为：解析/校验载荷 → 加载会话 → 调用 `Client.submit(record, "二维码", token=data)`（内部会复核活动进行中并回查）。

stdout 单行 JSON：

- 已确认：`{"ok":true,"confirmed":true,"message":"已确认签到。"}`，退出码 0。
- 已提交未确认：`{"ok":true,"confirmed":false,"message":"已提交，请在畅课查看结果。"}`，退出码 3。
- 失败：`{"ok":false,"stage":"parse|session|network|submit|verify","error":"中文原因"}`，退出码 2。

`stage` 规则：载荷缺失/无效=`parse`；会话过期或未登录=`session`；网络中断=`network`；接口拒绝或活动不再有效=`submit`；回查异常=`verify`。错误信息不得包含二维码 data。

### `guet-ac test-event`

参数：`--event-dir DIR`（必填）、`--channel 数字码|GPS|二维码`（默认 `数字码`）、`--stage started|action_required|confirmed|unconfirmed`（默认 `confirmed`）。写入一条 `test: true`、消息以 `【测试】` 开头的事件文件，stdout 输出文件路径，退出码 0。用于端到端验证主动通知，不触碰畅课。

### `watch` 通道与事件

新增配置字段 `auto_number`、`auto_gps`（布尔）。`watch --auto` 表示按配置里的三个自动开关执行；`--auto-number`、`--auto-gps` 仍可单独强制开启；`--dry-run` 只查询。`auto_qr` 仍控制服务端取码。

事件 stage 集合固定为：`started`、`action_required`、`confirmed`、`unconfirmed`。

- `started`：活动开始，每活动一次。
- `action_required`：本应自动提交但缺少配置（如 GPS 未绑定地点）或提交前失败，需要用户处理。
- `confirmed`：畅课回查确认本人已签到。
- `unconfirmed`：提交过但回查未确认，或上次提交结果不明。

事件 JSON：`{"stage":"...","channel":"...","message":"...","test":true?}`，`message` ≤200 字符。事件目录中的状态文件不得命名为 64 位十六进制 `.json`。

### 提交去重/重启恢复

- 事件目录下的 `state.json`（可用 `--state-file` 覆盖）记录活动状态；文件 0600、原子替换。
- 同一活动在任意重启前后最多发起一次写请求：写入请求前先持久化 `submitting`，请求后写 `confirmed`/`unconfirmed`。
- 启动时发现 `submitting` 且活动仍进行中：不再提交，产生一次 `unconfirmed` 事件（消息说明上次结果不明）。超过 14 天的记录可清理，最多保留 500 条。

## 2. 二维码识别脚本契约（`scripts/guet_qr_preview.py`）

`python guet_qr_preview.py [--json] IMAGE`

- 不带 `--json`：保持现有行为（成功打印“已识别一个二维码。”，失败打印“识别失败：...”）。
- 带 `--json`：stdout 只输出单行 JSON。
  - 成功：`{"ok":true,"decoder":"wxscan|zxing|aruco|detail","payload":{"courseId":"11","rollcallId":"22","data":"..."}}`，退出码 0。
  - 失败：`{"ok":false,"stage":"image|decode|parse","error":"<固定枚举>","message":"中文提示"}`，退出码 2。
  - `error` 固定枚举：`unreadable`、`too_large`、`pixels_too_large`、`no_qr`、`multiple_qr`、`invalid_payload`、`timeout`、`internal`。
- 识别顺序：wxscan → zxing → detail pass → aruco 兜底（cv2 可用时）。失败不得输出二维码内容。
- 每个解码器的候选文本必须先通过 `guet_ac.qr.parse` 校验才被采用；前序解码器返回显示转义文本（如 `<DLE>`）时继续尝试后续解码器，避免紧凑码被卡死。
- Aruco 兜底必须限时（总解码仍受现有 deadline 约束），cv2 不可用时直接跳过，不能让整条链路失败。
- QQ 桥接设置 `GUET_AC_ARUCO_SCOPE=1`（同时提供 `XDG_RUNTIME_DIR`/`DBUS_SESSION_BUS_ADDRESS`）时，Aruco 通过 `systemd-run --user --scope` 调用 `scripts/guet_aruco_worker.py`，避免 OpenCV 在桥接 150 MiB cgroup 内触发 OOM；scope 调用失败即返回空，不回退到进程内 import。

## 3. 事件通知模块（`scripts/emb_guet_notify.mjs`）

- `knownStages` 增加 `action_required`。
- 只向唯一已绑定、`platformAllowed && subscribed` 的私聊发送；保留 15 分钟窗口、`.dispatch` 独占文件、结果不明不重试。
- 测试事件（`test:true`）与真实事件走同一路径，不特殊绕过权限。
- 新增 Node 单测：无绑定/权限未开/已订阅发送/重复派发不重发/测试事件 stage 合法。

## 4. QQ 私聊路由（`scripts/emb_guet_private.mjs`）

`createGuetPrivateRoute({download, run})` 返回 `async ({privateChat,text,images,reply}) => boolean`。

流程与回执：

1. 非私聊返回 false。
2. `/畅课状态` 保持现有回执。
3. 图片：一次一张；URL 必须是 https 且主机为 `qpic.cn`/`qq.com`；下载上限 8 MiB。
4. 下载失败回执：“QQ 图片下载失败，请重发原图或稍后再试喵。”；超限：“图片超过 8 MiB，请压缩后再发喵。”；URL 不合法：“这张图片的下载地址无效，请重新发送喵。”
5. 调 `guet_qr_preview.py --json`（超时 10 秒）。失败按 `stage/error` 区分：
   - `image` → “图片无法读取，请发原图喵。”
   - `decode`（no_qr/multiple_qr/timeout/internal）→ “没能在图片里读出一个二维码喵。”
   - `parse`（invalid_payload）→ “读到了二维码，但不是畅课签到码喵。”
6. 识别成功后调 `guet-ac qr-submit`（stdin JSON，超时 30 秒，env 带 `GUET_AC_SESSION_DIR`）：
   - 确认：“已识别并提交，畅课已确认签到喵。”
   - 未确认：“已识别并提交，畅课还没确认，请打开畅课查看喵。”
   - `session`：“二维码已识别，但畅课会话已失效，请更新会话后再发一次喵。”
   - `parse`：“二维码内容不是有效的畅课签到码喵。”
   - `submit`：“二维码有效，但提交被畅课拒绝（可能活动已结束）喵。”
   - `network`：“二维码已识别，提交时网络中断，请在畅课确认是否已签到喵。”
7. 任何阶段只允许一次 `reply`，不记录附件 URL、二维码内容或会话凭据。

## 5. Web 配置 API 与页面

新增文件 `deploy/emb-web-ai/guet-config.mjs`，导出 `createGuetConfig({ bin, config, sessionDir, home, exec })`，返回 `handle(req,res,{pathname,person,isAdmin,sameOrigin})`。

- 仅 `isAdmin` 可访问；非管理员 403 `{"error":"forbidden"}`。
- `GET /api/guet-config`：返回
  ```json
  {"ok":true,"config":{...},"locations":[...],"courses":[...],"coursesError":null,
   "service":{"active":true,"autoNumber":true,"autoGps":true,"autoQr":false},
   "warning":"..."}
  ```
  `courses` 来自 `guet-ac status --json`，失败时为空数组并把中文原因放进 `coursesError`；`locations` 来自 `guet-ac locations --json`。
- `POST /api/guet-config`：same-origin；body `{"config":{...}}`（也接受平铺字段）。只代理 `config-update`，成功返回最新 config；CLI 校验失败返回 400 与中文原因。
- 所有子进程加超时；错误信息不得包含会话/凭据；不直接解析或拼接 TOML。
- 环境默认：`GUET_AC_HOME=~/.local/share/guet-ac`，`GUET_AC_BIN=$HOME/.local/share/guet-ac/.venv/bin/guet-ac`，`GUET_AC_CONFIG=$HOME/.local/share/guet-ac/config.toml`，`GUET_AC_SESSION_DIR=$HOME/.local/share/guet-ac/session`。

挂载脚本 `scripts/stage_web_guet.py`：以 `/home/orangepi/emb-web-ai/server.mjs` 为输入，幂等注入

- `import { createGuetConfig } from "./guet-config.mjs";`
- 初始化 `guetConfigHandler`（使用环境默认路径）
- 在 `qqSettingsHandler` 调用之后、`presetFor` 之前调用 `guetConfigHandler`。

不允许改上游其它路由；注入失败要非零退出并保留原文件。

页面：更新 `dist/guet-course-location-simulation.html`，同一文件同时支持两种模式：

- 由 `https://ai.guet.win/guet-locations.html` 打开（http/https）：调用 `/api/guet-config`，显示服务器状态、课程列表、已有绑定；保存/取消绑定、切换 `auto_gps`/`auto_number` 都走 POST。非管理员或未登录给出明确提示。
- 直接 `file://` 打开：保留“离线模拟”行为，使用内置地点数据和 localStorage。
- 页面不得内联任何会话、密钥或管理口令。

## 6. 部署与验证路径

- 仓库：`C:\Users\DogXie\Documents\Codex\2026-11-19\wh\outputs\GUET-AC`
- Pi 应用：`/home/orangepi/.local/share/guet-ac/`，会话 `session/`，事件 `events/`
- Pi 桥接：`/home/orangepi/emb-maibot-staging/emb-dsh-qqbot-staging/`、`/home/orangepi/emb-qq-official/`
- Pi Web：`/home/orangepi/emb-web-ai/`，静态目录 `public/`
- 本地测试：`.venv\Scripts\python.exe -m pytest -q`；Node 单测 `node --test tests/*.test.mjs`
- 部署由 Lead 执行；teammate 只改仓库并跑本地测试，不碰 Pi、不重启服务、不提交 git。
