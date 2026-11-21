# 接口与参考项目

## 本次直接读取的桂电页面

2026-11-19，在不登录、不提交签到的情况下读取：

1. `https://courses.guet.edu.cn/user/index`
2. 跳转到 `https://identity.guet.edu.cn/auth/realms/guet/...`
3. 继续跳转到 `https://cas.guet.edu.cn/authserver/login?service=...`

CAS 页面包含 `pwdFromId`、`pwdEncryptSalt`、`execution`。页面的 `encrypt.js` 使用 AES-CBC/PKCS7；`login.js` 指明 `checkNeedCaptcha.htl`、`getCaptcha.htl`、`cllt=userNameLogin`，提交前禁用明文 `passwordText`。

项目实现此认证流程，保留 service 参数和逐跳 Cookie；动态 salt、execution 每次重新获取。每次登录创建独立 Session，避免复用过期的 CAS JSESSIONID/route。

## 业务接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/profile` | 核验业务登录会话 |
| GET | `/api/radar/rollcalls` | 查询签到活动 |
| GET | `/api/rollcall/{rid}/student_rollcalls` | 数字码读取 |
| PUT | `/api/rollcall/{rid}/answer_number_rollcall` | 数字码提交，`deviceId`、`numberCode` |
| PUT | `/api/rollcall/{rid}/answer?api_version=1.1.2` | GPS 坐标提交 |
| GET | `/api/course/{cid}/rollcall/{rid}/qr_code` | 请求二维码 data，受会话权限控制 |
| PUT | `/api/rollcall/{rid}/answer_qr_rollcall` | 二维码提交，`deviceId`、`data` |
| GET | `/api/course/{cid}/student/{sid}/rollcalls` | 本人签到记录 |

业务提交实现依据以下公开源码；本次未登录真实学生账号、未执行真实签到。

## 来源与复用评估

- [wilinz/guet_cas](https://github.com/wilinz/guet_cas)：BSD-3-Clause。桂电 CAS 协议、ASCII IV 和过期 CAS Cookie 处理的参考。本项目使用 Python requests/BeautifulSoup/cryptography 实现相同协议，没有复制其 Dart 源码。
- [wilinz/fuck_tronclass_sign](https://github.com/wilinz/fuck_tronclass_sign)：`main.py` 明确对接桂电站点，包含 `answer_qr_rollcall` 和 OAuth 登录链。没有根目录许可证，本项目仅参考协议字段。
- [wilinz/tronclass_plus](https://github.com/wilinz/tronclass_plus)：已发布 Android v1.6.0 APK；支持桂电直登、多账号切换和当前账号扫码。`qr_rollcalls_page.dart` 调用 `ChangkeClient.instance.dio`，未遍历已保存账号。主项目未标注许可证，因此未复制、改名分发其 App 源码。
- [zxyzhk/tronclass-autosigner](https://github.com/zxyzhk/tronclass-autosigner)，检查提交 `18ba0d7966737f02ac5a26b88d70313edc4e06cb`：MIT。存在完整自动取码调度分支；学生会话读不到取码接口时需要有效二维码内容。本项目没有继承其伪造 data 探测或距离反推逻辑。
- [gaojunran/tronclass-signin-app](https://github.com/gaojunran/tronclass-signin-app) 和 [重邮后端](https://github.com/gaojunran/tronclass-signin-api-cqupt)：扫码后批量提交的 Web 前后端。需要有人提供二维码，不属于无人取码。
- [hot-YUser/auto-Tronclass-VPS](https://github.com/hot-YUser/auto-Tronclass-VPS)：教师账号维护二维码活动和 token 服务；不是普通学生账号通用的免扫码实现。
- [yhf216/ZJU-AC](https://github.com/yhf216/ZJU-AC)：数字码/GPS 流程的早期研究参考；未发现二维码提交实现，未标注许可证，未复制代码。

## 二维码解析

支持原始紧凑串、JSON、`/j?p=...`、`/scanner-jumper?_p=...` 和 `_wg_p` 参数。不访问二维码中的 URL，所有提交只发送到 `courses.guet.edu.cn`。

紧凑字段按 base36 索引映射：`0=courseId`、`3=data`、`4=rollcallId`；`\x10` 后是 base36 整数；`\x1e`/`\x1f` 分别转义 `!`/`~`。客户端解码载荷，不生成服务器签名。

自动取码默认关闭，配置 `auto_qr=true` 后使用当前账号请求取码接口。403、缺码或过期时输出原因，不反复探测、不构造伪造令牌。

## CLI 契约（2026-11-21）

全局参数 `--account`、`--config PATH` 必须写在子命令之前。

| 命令 | 说明 | 失败行为 |
| --- | --- | --- |
| `config-json` | 只读，stdout 单行 JSON，字段为 `default_location`、`course_locations`、`auto_number`、`auto_gps`、`auto_qr`、`poll_seconds`、`accuracy`、`submit_coordinate_system`；缺失字段按默认值补齐，不含 Cookie/会话/密码 | stderr `{"error":"..."}`，退出码 2 |
| `config-check` | 只读，配置合法无输出退出 0 | stderr `{"error":"..."}`，退出码 2 |
| `config-update` | stdin 读 JSON 对象，允许上述 8 个字段，未提供的键保持原值；复用 config.load 校验；写入前备份到 `<config>.before-web-<UTC>`（0600），原子替换；成功输出写回后的 config JSON | 退出码 2 且不改原文件 |
| `locations --json` | stdout `{"places":[{id,name,latitude,longitude,source,method?}]}`；不带 `--json` 保持制表符文本 | 退出码 1 |
| `status --json` | stdout `{"profile":{id,name},"rollcalls":[{id,course_id,course_title,status,kind,active}],"at":ISO8601}`，`kind` 为 `GPS`/`数字码`/`二维码`/`其他`，不输出会话或原始响应 | stderr `{"error":"..."}`，退出码 2 |
| `qr-submit` | stdin 读 `{"courseId","rollcallId","data"}` 或 `{"text": 二维码原始内容}`；confirmed 退出 0，未确认退出 3 | `{"ok":false,"stage":"parse\|session\|network\|submit\|verify","error":"..."}`，退出码 2 |
| `test-event` | `--event-dir DIR`（必填）、`--channel 数字码\|GPS\|二维码`、`--stage started\|action_required\|confirmed\|unconfirmed`；写一条 `test:true`、消息以 `【测试】` 开头的事件并输出文件路径 | 退出码 1 |

`watch` / `once` 的新增参数：

- `--auto`：严格按 `auto_number`、`auto_gps`、`auto_qr` 三个配置开关执行；
- `--auto-number` / `--auto-gps`：只强制开启对应通道（保持旧 `--auto-number` 不处理 GPS/二维码的语义）；
- 默认（不带以上开关）：数字码与 GPS 自动处理，二维码由 `auto_qr` 控制，与旧版一致；
- `--dry-run`：只查询，不提交，也不写去重状态；
- `--event-dir DIR`：事件目录；`--state-file PATH`：覆盖去重状态文件路径；
- 配置热加载：`watch`/`once` 每轮检查 `--config` 文件的 mtime/size，变化后重新加载并重算 `auto_*` 通道与 `poll_seconds`，网页 `config-update` 写回后无需重启服务；重载失败时 stderr 打印一行中文警告并继续使用旧配置。

GPS 提交复用 `Client.submit(place=..., accuracy=..., coordinate_system=...)`，数字码使用 `Client.number_code`。只有畅课回查确认本人已签到时才算 confirmed；GPS 未绑定地点时不提交，改为 action_required。

## 事件协议与去重状态

事件目录中每个事件一个 `64 位十六进制.json` 文件，内容：

```json
{"stage": "started|action_required|confirmed|unconfirmed",
 "channel": "GPS|数字码|二维码",
 "message": "不超过 200 字符",
 "test": true}
```

- `started`：活动开始，每活动一次；
- `action_required`：本应自动提交但缺少配置（如 GPS 未绑定地点）或提交前失败，需要用户处理；
- `confirmed`：畅课回查确认；
- `unconfirmed`：提交过但回查未确认，或上次提交结果不明。

去重/重启恢复使用事件目录下的 `state.json`（0600、temp+replace 原子替换，损坏文件按空账本处理，可用 `--state-file` 覆盖）：

- 发起写请求前先持久化 `submitting`，请求后写 `confirmed`/`unconfirmed`；同一活动在任意重启前后最多一次写请求；
- 启动时发现仍进行中的 `submitting`：不再提交，产生一次 unconfirmed 事件说明上次结果不明；
- 记录超过 14 天或超过 500 条时清理。`state.json` 不是 64 位十六进制名，且不含顶层 `stage`，不会被通知模块当作签到事件。
