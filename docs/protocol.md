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
