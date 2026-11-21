# 用现有 VPS 公网域名接入手机扫码页

用途：用手机打开 HTTPS 页面、授权摄像头并识别测试二维码。二维码在浏览器内由开源 jsQR 解码，结果只显示在当前页面。这份教程采用 VPS 静态部署，Pi 无需公网 IP、端口映射或额外服务。

> 本页只负责本地解码和显示，**不提交签到**，也不连接畅课会话。真正的扫码提交链路由 QQ 私聊照片（识别→解析→`qr-submit`→回查）或课程地点网页配置后的 Pi 监控完成，见 [GUET-AC 与 EMB 猫猫交接](guet-emb-pi-handoff.md)。不要为了“顺手提交”给这个公开静态页加提交接口或嵌入会话。

## 1. 确认域名和 HTTPS

选一个已指向 VPS 且证书有效的域名。教程以 `scan.example.com` 代称；也可以沿用现有 HTTPS 站点的 `/qr-scan/` 子路径。已有站点时，优先使用子路径，省掉新增 DNS 和证书的步骤。

在本机 PowerShell 中检查 DNS 和 HTTPS，把示例域名换成你的：

```powershell
Resolve-DnsName scan.example.com
curl.exe -I https://scan.example.com/
```

如果新建子域名，先添加指向 VPS 公网 IPv4 的 A 记录。如配置 AAAA，也要确保该 IPv6 地址的 443 可达。在 VPS 上用当前证书管理工具签发覆盖该域名的证书。新增子域名的 Nginx 虚拟主机按你现有站点的 HTTPS 配置建立，以下步骤假设目标域名已经能正常提供 HTTPS。

## 2. 放置静态文件

在 VPS 的 SSH 终端运行：

```bash
sudo install -d -m 0755 /srv/qr-scan
workdir=$(mktemp -d)
curl -fL --proto '=https' 'https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js' -o "$workdir/jsQR.js"
sudo install -m 0644 "$workdir/jsQR.js" /srv/qr-scan/jsQR.js
```

固定使用 jsQR 1.4.0。浏览器从同一个域名加载脚本，扫码时无需访问 CDN。[jsQR 上游](https://github.com/cozmo/jsQR)

把下列内容保存为 VPS 上的 `/srv/qr-scan/index.html`。页面只负责取帧、调用上游库和显示结果，不包含自行实现的解码算法。

```html
<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>手机扫码</title>
<main>
  <h1>手机扫码</h1>
  <p>对准二维码，识别结果只显示在当前页面。</p>
  <button id="start" type="button">打开摄像头</button>
  <button id="stop" type="button" disabled>停止</button>
  <p role="status" id="status">等待打开摄像头</p>
  <video id="preview" autoplay muted playsinline
         style="display:block;width:100%;max-width:480px"></video>
  <pre id="result" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre>
</main>
<script src="./jsQR.js"></script>
<script>
const video = document.getElementById('preview');
const hint = document.getElementById('status');
const result = document.getElementById('result');
const startButton = document.getElementById('start');
const stopButton = document.getElementById('stop');
const canvas = document.createElement('canvas');
const ctx = canvas.getContext('2d', {willReadFrequently:true});
let stream, frame, generation = 0, last = 0;

function stop() {
  ++generation;
  cancelAnimationFrame(frame);
  stream?.getTracks().forEach(track => track.stop());
  stream = undefined;
  video.srcObject = null;
  startButton.disabled = false;
  stopButton.disabled = true;
}

function scan(now) {
  if (!stream) return;
  if (video.readyState >= 2 && now - last >= 130) {
    last = now;
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    ctx.drawImage(video, 0, 0);
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const code = jsQR(data.data, data.width, data.height);
    if (code) {
      result.textContent = code.data;
      stop();
      hint.textContent = '识别成功';
      return;
    }
  }
  frame = requestAnimationFrame(scan);
}

startButton.onclick = async () => {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    hint.textContent = '请用手机浏览器打开有效的 HTTPS 地址。';
    return;
  }
  if (typeof jsQR !== 'function') {
    hint.textContent = '扫码库未加载，请检查 jsQR.js。';
    return;
  }
  startButton.disabled = true;
  result.textContent = '';
  hint.textContent = '等待摄像头授权';
  const request = ++generation;
  try {
    const camera = await navigator.mediaDevices.getUserMedia({
      video:{facingMode:{ideal:'environment'}}, audio:false
    });
    if (request !== generation) {
      camera.getTracks().forEach(track => track.stop());
      return;
    }
    stream = camera;
    video.srcObject = stream;
    stopButton.disabled = false;
    await video.play();
    if (request !== generation) return;
    hint.textContent = '请对准二维码';
    frame = requestAnimationFrame(scan);
  } catch (error) {
    if (request !== generation) return;
    stop();
    hint.textContent = error.name === 'NotAllowedError'
      ? '摄像头未获授权。请在浏览器站点设置中允许摄像头。'
      : '摄像头未能启动。请检查是否被其他应用占用。';
  }
};
stopButton.onclick = () => {stop(); hint.textContent = '已停止';};
window.addEventListener('pagehide', stop);
</script>
</html>
```

## 3. 接入现有 Nginx 站点

把下列两个 `location` 放进**现有域名对应的 HTTPS server 块**。替换实际配置文件前先备份该文件，保留站点原有证书、其他路由和反代设置。

```nginx
location = /qr-scan {
    return 308 /qr-scan/;
}

location ^~ /qr-scan/ {
    alias /srv/qr-scan/;
    index index.html;

    add_header Permissions-Policy "camera=(self), microphone=()" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'" always;
}
```

如果站点依赖父级的安全响应头，检查当前 Nginx 的 `add_header` 继承规则，并在该路径保留站点要求的响应头。该配置有内联脚本和样式，所以为本路径允许了内联内容；生产站也可以把两者移到同源静态文件后收紧策略。

```bash
sudo nginx -t && sudo systemctl reload nginx
```

这里使用 `reload` 使新配置生效。若 `nginx -t` 报错，先修配置。

## 4. 在真实手机验证

1. 用手机的系统浏览器访问 `https://scan.example.com/qr-scan/`。先用手机流量测一轮，确认公网可达。
2. 点「打开摄像头」并授权。用普通测试二维码验证，结果应显示在页面，摄像头应随即停止。
3. 再用无二维码的画面验证「停止」按钮。重新打开或退出页面时，浏览器应释放摄像头。
4. 在浏览器开发者工具的网络记录里检查这一页只加载本域名的静态文件，扫码时没有额外请求。

也可以先在本机检查响应头：

```powershell
curl.exe -I https://scan.example.com/qr-scan/
curl.exe -I https://scan.example.com/qr-scan/jsQR.js
```

- **网页 404**：核对 HTTPS 虚拟主机、`alias` 末尾斜杠和 Nginx 对 `/srv/qr-scan/` 的读取权限。
- **页面打开但摄像头不弹权限**：检查 HTTPS 证书和站点权限。如果从 QQ 或微信内置浏览器打开，改在系统浏览器测试。
- **脚本未加载**：检查第二条请求是否为 200 和 JavaScript MIME 类型，并看站点的 CSP 是否覆盖了该路径。
- **摄像头工作但读不到**：先用近距离普通测试码验证运行路径，再测试远拍。此页使用 jsQR 上游包；当前 Pi 上的 wxscan 属于另一条独立的图片识别链路。

## 5. 回滚

从该域名的 HTTPS 配置中移除本教程新增的两个 `location`，或恢复改动前的配置文件，然后执行：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

本教程只涉及 VPS 扫码静态页，没有连接 GUET-AC 登录会话、签到提交或 QQ 桥接。完成后应以真实手机测试结果为准。
