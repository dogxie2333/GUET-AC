// GUET-AC 课程地点配置 API（/api/guet-config）。
// 登录、会话与管理员身份由 server.mjs 判定；本模块只做管理员/同源校验并代理
// guet-ac 子命令，不读取会话、Cookie 或二维码内容，也不直接解析/拼接 TOML。
import { execFile } from "node:child_process";
import { homedir, userInfo } from "node:os";
import { join } from "node:path";

const API_PATH = "/api/guet-config";
const MAX_BODY_BYTES = 32 * 1024;
const MAX_BUFFER = 1024 * 1024;
const TIMEOUT_MS = 8000;
const STATUS_TIMEOUT_MS = 12000;
// 只有这些键允许经网页写入；其余键（包括未知键）一律丢弃。
const CONFIG_FIELDS = [
  "default_location",
  "course_locations",
  "auto_number",
  "auto_gps",
  "auto_qr",
  "accuracy",
  "poll_seconds",
  "submit_coordinate_system",
];
const CONFIG_DEFAULTS = {
  default_location: "",
  course_locations: {},
  auto_number: false,
  auto_gps: false,
  auto_qr: false,
  poll_seconds: 15,
  accuracy: 35.0,
  submit_coordinate_system: "gcj02",
};
const ACTIVE_STATES = new Set(["active", "reloading", "activating"]);
const INACTIVE_STATES = new Set(["inactive", "failed", "deactivating", "maintenance", "unknown"]);

function send(res, status, data) {
  const body = Buffer.from(JSON.stringify(data));
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": body.length,
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  res.end(body);
}

// execFile 的 Promise 包装；与 promisify(execFile) 行为一致，另支持把字符串写进 stdin。
function execFileWithInput(file, args, options = {}) {
  const { input, ...execOptions } = options;
  return new Promise((resolve, reject) => {
    const child = execFile(file, args, execOptions, (error, stdout, stderr) => {
      if (error) {
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
        return;
      }
      resolve({ stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
    });
    if (!child.stdin) return;
    child.stdin.on("error", () => { /* 子进程提前退出时忽略 EPIPE */ });
    child.stdin.end(input === undefined || input === null ? undefined : String(input));
  });
}

async function settle(promise) {
  try {
    return { ok: true, value: await promise };
  } catch (error) {
    return { ok: false, error };
  }
}

// 子进程约定输出单行 JSON；容错读取最后一条可解析的 JSON 行。
function parseJsonLine(text) {
  const raw = String(text ?? "").trim();
  if (!raw) throw new Error("empty_output");
  try {
    return JSON.parse(raw);
  } catch { /* 继续尝试逐行解析 */ }
  const lines = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      return JSON.parse(lines[index]);
    } catch { /* 尝试上一行 */ }
  }
  throw new Error("invalid_json");
}

function stderrReason(error, fallback) {
  const raw = typeof error?.stderr === "string"
    ? error.stderr
    : Buffer.isBuffer(error?.stderr) ? error.stderr.toString("utf8") : "";
  for (const line of raw.split(/\r?\n/)) {
    const text = line.trim();
    if (!text) continue;
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed.error === "string" && parsed.error.trim()) {
        return parsed.error.trim().slice(0, 200);
      }
    } catch { /* 不是 JSON，继续 */ }
  }
  return fallback;
}

function isTimeout(error) {
  if (!error) return false;
  return error.killed === true || error.signal === "SIGTERM" ||
    error.code === "ETIMEDOUT" || error.name === "TimeoutError";
}

function failureReason(error, fallback) {
  return isTimeout(error) ? "响应超时，请稍后重试。" : stderrReason(error, fallback);
}

// 只保留接口文档允许的配置字段，并补齐缺失默认值。
function sanitizeConfig(raw) {
  const input = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const courseLocations = input.course_locations && typeof input.course_locations === "object" &&
    !Array.isArray(input.course_locations) ? { ...input.course_locations } : {};
  const pollSeconds = Number.isInteger(input.poll_seconds) && input.poll_seconds >= 5
    ? input.poll_seconds : CONFIG_DEFAULTS.poll_seconds;
  const accuracy = typeof input.accuracy === "number" && Number.isFinite(input.accuracy) &&
    input.accuracy > 0 ? input.accuracy : CONFIG_DEFAULTS.accuracy;
  return {
    default_location: typeof input.default_location === "string"
      ? input.default_location : CONFIG_DEFAULTS.default_location,
    course_locations: courseLocations,
    auto_number: input.auto_number === true,
    auto_gps: input.auto_gps === true,
    auto_qr: input.auto_qr === true,
    poll_seconds: pollSeconds,
    accuracy,
    submit_coordinate_system: input.submit_coordinate_system === "wgs84" ? "wgs84" : "gcj02",
  };
}

// 同一课程可能有多条 rollcall；课程下拉去重，保留首次出现的课程信息。
function dedupeCourses(rollcalls) {
  const seen = new Set();
  const courses = [];
  for (const item of Array.isArray(rollcalls) ? rollcalls : []) {
    const courseId = String(item?.course_id ?? "").trim();
    if (!courseId || seen.has(courseId)) continue;
    seen.add(courseId);
    courses.push({
      course_id: courseId,
      course_title: String(item?.course_title ?? "").trim(),
      active: item?.active === true,
      kind: String(item?.kind ?? ""),
      status: String(item?.status ?? ""),
      rollcall_id: String(item?.id ?? ""),
    });
  }
  return courses;
}

function readJsonBody(req, limit = MAX_BODY_BYTES) {
  return (async () => {
    const chunks = [];
    let size = 0;
    for await (const chunk of req) {
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      size += buffer.length;
      if (size > limit) {
        const error = new Error("body_limit");
        error.code = "body_limit";
        throw error;
      }
      chunks.push(buffer);
    }
    let parsed;
    try {
      parsed = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    } catch {
      const error = new Error("invalid_json");
      error.code = "invalid_json";
      throw error;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      const error = new Error("invalid_body");
      error.code = "invalid_body";
      throw error;
    }
    return parsed;
  })();
}

function serviceFrom(raw, configValues) {
  if (!raw) return { active: null, autoNumber: null, autoGps: null, autoQr: null };
  const state = String(raw.activeState ?? "");
  let active = null;
  if (ACTIVE_STATES.has(state)) active = true;
  else if (INACTIVE_STATES.has(state)) active = false;
  const execStart = String(raw.execStart ?? "");
  if (!execStart) return { active, autoNumber: null, autoGps: null, autoQr: null };
  // `watch --auto` 表示按配置里的三个开关执行；否则看显式参数。
  const followsConfig = /(?:^|[\s;])--auto(?=[\s;]|$)/.test(execStart);
  const flag = (name, configKey) => {
    if (followsConfig) return Boolean(configValues?.[configKey]);
    return new RegExp(`(?:^|[\\s;])--${name}(?=[\\s;]|$)`).test(execStart);
  };
  return {
    active,
    autoNumber: flag("auto-number", "auto_number"),
    autoGps: flag("auto-gps", "auto_gps"),
    autoQr: flag("auto-qr", "auto_qr"),
  };
}

export function createGuetConfig(options = {}) {
  const home = options.home || homedir();
  const guetHome = process.env.GUET_AC_HOME || join(home, ".local", "share", "guet-ac");
  const bin = options.bin || process.env.GUET_AC_BIN || join(guetHome, ".venv", "bin", "guet-ac");
  const configPath = options.config || process.env.GUET_AC_CONFIG || join(guetHome, "config.toml");
  const sessionDir = options.sessionDir || process.env.GUET_AC_SESSION_DIR || join(guetHome, "session");
  const exec = options.exec || execFileWithInput;

  // Pi 的用户 systemd 需要 XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS；
  // 从服务进程启动时通常没有，这里按 uid 补默认值（已有环境变量则保持）。
  const uid = (() => {
    try {
      return userInfo().uid;
    } catch {
      return -1;
    }
  })();
  const defaultXdg = process.platform === "linux" && Number.isInteger(uid) && uid >= 0
    ? `/run/user/${uid}` : "";
  const xdg = process.env.XDG_RUNTIME_DIR || defaultXdg;
  const dbus = process.env.DBUS_SESSION_BUS_ADDRESS ||
    (xdg ? `unix:path=${join(xdg, "bus")}` : "");
  const childEnv = {
    ...process.env,
    GUET_AC_SESSION_DIR: sessionDir,
    PYTHONIOENCODING: "utf-8",
  };
  if (xdg) childEnv.XDG_RUNTIME_DIR = xdg;
  if (dbus) childEnv.DBUS_SESSION_BUS_ADDRESS = dbus;

  function runGuet(subcommand, rest = [], timeoutMs = TIMEOUT_MS, input) {
    const args = ["--config", configPath, subcommand, ...rest];
    const execOptions = {
      timeout: timeoutMs,
      maxBuffer: MAX_BUFFER,
      env: childEnv,
      ...(input === undefined ? {} : { input }),
    };
    return exec(bin, args, execOptions).then(({ stdout, stderr }) => ({
      stdout: String(stdout ?? ""),
      stderr: String(stderr ?? ""),
    }));
  }

  async function readServiceRaw() {
    try {
      const { stdout } = await exec(
        "systemctl",
        ["--user", "show", "guet-ac", "-p", "ActiveState", "-p", "ExecStart"],
        { timeout: TIMEOUT_MS, maxBuffer: MAX_BUFFER, env: childEnv },
      );
      const text = String(stdout ?? "");
      return {
        activeState: (text.match(/^ActiveState=(.*)$/m) || [])[1]?.trim() || "",
        execStart: (text.match(/^ExecStart=(.*)$/m) || [])[1] || "",
      };
    } catch {
      // 读不到服务状态不算错误：active=null，由页面显示“未知”。
      return null;
    }
  }

  async function handleGet(_req, res) {
    const [configResult, locationsResult, statusResult, serviceRaw] = await Promise.all([
      settle(runGuet("config-json")),
      settle(runGuet("locations", ["--json"])),
      settle(runGuet("status", ["--json"], STATUS_TIMEOUT_MS)),
      readServiceRaw(),
    ]);

    if (!configResult.ok) {
      return send(res, 503, {
        error: "unavailable",
        message: "读取配置失败：" + failureReason(configResult.error, "配置服务暂时不可用。"),
      });
    }
    let config;
    try {
      config = sanitizeConfig(parseJsonLine(configResult.value.stdout));
    } catch {
      return send(res, 503, { error: "unavailable", message: "配置读取结果无法解析，请稍后重试。" });
    }

    if (!locationsResult.ok) {
      return send(res, 503, {
        error: "unavailable",
        message: "读取地点列表失败：" + failureReason(locationsResult.error, "地点服务暂时不可用。"),
      });
    }
    let locations;
    try {
      const parsed = parseJsonLine(locationsResult.value.stdout);
      locations = (Array.isArray(parsed?.places) ? parsed.places : [])
        .filter((place) => place && typeof place.id === "string" && place.id)
        .map((place) => ({
          id: place.id,
          name: typeof place.name === "string" && place.name ? place.name : place.id,
          latitude: Number(place.latitude),
          longitude: Number(place.longitude),
          source: typeof place.source === "string" ? place.source : "",
          ...(typeof place.method === "string" && place.method ? { method: place.method } : {}),
        }));
    } catch {
      return send(res, 503, { error: "unavailable", message: "地点列表无法解析，请稍后重试。" });
    }

    let courses = [];
    let coursesError = null;
    if (!statusResult.ok) {
      coursesError = failureReason(statusResult.error, "暂时无法读取畅课课程列表。");
    } else {
      try {
        courses = dedupeCourses(parseJsonLine(statusResult.value.stdout)?.rollcalls);
      } catch {
        coursesError = "畅课课程列表无法解析。";
      }
    }

    const service = serviceFrom(serviceRaw, config);
    const warning = service.active === false ? "监控服务未运行，自动签到不会执行。" : "";
    return send(res, 200, { ok: true, config, locations, courses, coursesError, service, warning });
  }

  async function handlePost(req, res) {
    let body;
    try {
      body = await readJsonBody(req);
    } catch (error) {
      const message = error?.code === "body_limit" ? "请求内容过大。" : "请求内容不是有效的 JSON 对象。";
      return send(res, 400, { error: "invalid", message });
    }
    // 兼容 {"config":{...}} 与平铺字段；未知字段静默丢弃，交给 CLI 校验类型。
    const nested = body.config && typeof body.config === "object" && !Array.isArray(body.config)
      ? body.config : body;
    const patch = {};
    for (const key of CONFIG_FIELDS) {
      if (Object.hasOwn(nested, key)) patch[key] = nested[key];
    }
    if (!Object.keys(patch).length) {
      return send(res, 400, { error: "invalid", message: "没有可更新的配置字段。" });
    }

    let result;
    try {
      result = await runGuet("config-update", [], TIMEOUT_MS, JSON.stringify(patch));
    } catch (error) {
      if (isTimeout(error)) {
        return send(res, 503, { error: "timeout", message: "配置服务响应超时，请稍后重试。" });
      }
      if (typeof error?.code === "number" && error.code === 2) {
        return send(res, 400, {
          error: "invalid",
          message: stderrReason(error, "配置校验失败，请检查输入。"),
        });
      }
      return send(res, 503, { error: "unavailable", message: "配置服务暂时不可用，请稍后重试。" });
    }

    let latest;
    try {
      latest = sanitizeConfig(parseJsonLine(result.stdout));
    } catch {
      // CLI 已成功写回但 stdout 异常时，再读一次 config-json 兜底。
      try {
        latest = sanitizeConfig(parseJsonLine((await runGuet("config-json")).stdout));
      } catch {
        return send(res, 500, {
          error: "internal",
          message: "配置已保存，但读取最新配置失败，请刷新页面确认。",
        });
      }
    }
    return send(res, 200, { ok: true, config: latest });
  }

  return async function handle(req, res, context = {}) {
    const { pathname, isAdmin, sameOrigin } = context;
    if (pathname !== API_PATH) return false;
    if (!isAdmin) {
      send(res, 403, { error: "forbidden", message: "需要管理员权限。" });
      return true;
    }
    try {
      if (req.method === "GET") {
        await handleGet(req, res);
        return true;
      }
      if (req.method === "POST") {
        const allowed = typeof sameOrigin === "function" ? Boolean(sameOrigin(req)) : false;
        if (!allowed) {
          send(res, 403, { error: "origin", message: "请从 EMB 网站提交。" });
          return true;
        }
        await handlePost(req, res);
        return true;
      }
      send(res, 405, { error: "method_not_allowed", message: "只支持 GET 与 POST。" });
      return true;
    } catch {
      if (!res.headersSent) send(res, 500, { error: "internal", message: "服务器内部错误。" });
      else {
        try {
          res.end();
        } catch { /* 响应已结束 */ }
      }
      return true;
    }
  };
}
