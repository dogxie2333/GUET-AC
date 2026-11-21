import { test } from "node:test";
import assert from "node:assert/strict";

import { createGuetConfig } from "../../deploy/emb-web-ai/guet-config.mjs";

const CONFIG = {
  default_location: "",
  course_locations: { "11": "osm-way-606411548" },
  auto_number: true,
  auto_gps: true,
  auto_qr: false,
  poll_seconds: 15,
  accuracy: 35.0,
  submit_coordinate_system: "gcj02",
};
const PLACES = {
  places: [
    {
      id: "osm-way-606411548",
      name: "中央食堂",
      latitude: 25.3191451,
      longitude: 110.4121261,
      source: "https://www.openstreetmap.org/way/606411548",
      method: "outline_vertex_mean",
    },
  ],
};
const STATUS = {
  profile: { id: "123", name: "测试用户" },
  rollcalls: [
    { id: "22", course_id: "11", course_title: "高等数学", status: "absent", kind: "数字码", active: true },
    { id: "23", course_id: "11", course_title: "高等数学", status: "present", kind: "GPS", active: false },
    { id: "24", course_id: "12", course_title: "大学英语", status: "absent", kind: "GPS", active: true },
  ],
  at: "2026-11-21T00:15:00+00:00",
};
const SYSTEMCTL_ACTIVE = [
  "ActiveState=active",
  "ExecStart={ path=/home/orangepi/.local/share/guet-ac/.venv/bin/guet-ac ; " +
    "argv[]=/home/orangepi/.local/share/guet-ac/.venv/bin/guet-ac watch --auto-number --auto-gps " +
    "--event-dir /home/orangepi/.local/share/guet-ac/events ; ignore_errors=no }",
  "",
].join("\n");

function fakeRes() {
  return {
    statusCode: 0,
    headers: null,
    body: "",
    headersSent: false,
    writeHead(status, headers) {
      this.statusCode = status;
      this.headers = headers || {};
      this.headersSent = true;
      return this;
    },
    end(data) {
      if (data !== undefined) this.body += String(data);
      return this;
    },
  };
}

function fakeReq({ method = "GET", body, headers = {} } = {}) {
  const payload = body === undefined ? [] : [Buffer.from(body)];
  return {
    method,
    headers,
    socket: {},
    async *[Symbol.asyncIterator]() {
      for (const chunk of payload) yield chunk;
    },
  };
}

function makeExec(routes) {
  const calls = [];
  const exec = async (file, args, options = {}) => {
    calls.push({ file, args, options });
    for (const [predicate, responder] of routes) {
      if (!predicate(file, args, options)) continue;
      const result = typeof responder === "function"
        ? responder({ file, args, options })
        : responder;
      return typeof result === "string" ? { stdout: result, stderr: "" } : result;
    }
    const error = new Error(`unexpected exec: ${file} ${args.join(" ")}`);
    error.code = "ENOENT";
    throw error;
  };
  exec.calls = calls;
  return exec;
}

function cliFailure(code, stderr) {
  return () => {
    const error = new Error(`exit ${code}`);
    error.code = code;
    error.stderr = stderr;
    throw error;
  };
}

function happyRoutes() {
  return [
    [(file, args) => args[2] === "config-json", () => ({ stdout: JSON.stringify(CONFIG) })],
    [(file, args) => args[2] === "locations", () => ({ stdout: JSON.stringify(PLACES) })],
    [(file, args) => args[2] === "status", () => ({ stdout: JSON.stringify(STATUS) })],
    [(file) => file === "systemctl", () => ({ stdout: SYSTEMCTL_ACTIVE })],
  ];
}

function makeHandler({ routes = happyRoutes(), isAdmin = true, sameOrigin = () => true,
  allowedPersons = [] } = {}) {
  const exec = makeExec(routes);
  const instance = createGuetConfig({
    bin: "/opt/guet-ac",
    config: "/opt/config.toml",
    sessionDir: "/opt/session",
    home: "/home/pi",
    exec,
    allowedPersons,
  });
  const handle = (req, res, extras = {}) =>
    instance(req, res, {
      pathname: "/api/guet-config",
      person: "admin",
      isAdmin,
      sameOrigin,
      ...extras,
    });
  return { handle, exec };
}

test("未知路径返回 false 且不写响应", async () => {
  const { handle, exec } = makeHandler();
  const res = fakeRes();
  const handled = await handle(fakeReq(), res, { pathname: "/api/other" });
  assert.equal(handled, false);
  assert.equal(res.headersSent, false);
  assert.equal(exec.calls.length, 0);
});

test("非管理员但列入课程地点授权列表可访问", async () => {
  const { handle, exec } = makeHandler({ isAdmin: false, allowedPersons: ["owner"] });
  const res = fakeRes();
  await handle(fakeReq(), res, { person: "owner" });
  assert.equal(res.statusCode, 200);
  assert.equal(exec.calls.length, 4);
});

test("非管理员已登录但未授权仍 403", async () => {
  const { handle, exec } = makeHandler({ isAdmin: false, allowedPersons: ["owner"] });
  const res = fakeRes();
  await handle(fakeReq(), res, { person: "stranger" });
  assert.equal(res.statusCode, 403);
  assert.equal(JSON.parse(res.body).error, "forbidden");
  assert.equal(exec.calls.length, 0);
});


test("非管理员 GET 返回 403 forbidden", async () => {
  const { handle, exec } = makeHandler({ isAdmin: false });
  const res = fakeRes();
  const handled = await handle(fakeReq(), res);
  assert.equal(handled, true);
  assert.equal(res.statusCode, 403);
  assert.equal(JSON.parse(res.body).error, "forbidden");
  assert.equal(exec.calls.length, 0);
});

test("GET 聚合 config/locations/status 与服务状态", async () => {
  const { handle, exec } = makeHandler();
  const res = fakeRes();
  const handled = await handle(fakeReq(), res);
  assert.equal(handled, true);
  assert.equal(res.statusCode, 200);
  const data = JSON.parse(res.body);
  assert.equal(data.ok, true);
  assert.deepEqual(data.config, CONFIG);
  assert.equal(data.locations.length, 1);
  assert.equal(data.locations[0].name, "中央食堂");
  assert.equal(data.courses.length, 2, "同一课程的多条 rollcall 应去重");
  assert.deepEqual(data.courses.map((course) => course.course_id), ["11", "12"]);
  assert.equal(data.courses[0].course_title, "高等数学");
  assert.equal(data.coursesError, null);
  assert.deepEqual(data.service, {
    active: true,
    autoNumber: true,
    autoGps: true,
    autoQr: false,
  });
  assert.equal(data.warning, "");

  const configCall = exec.calls.find((call) => call.args[2] === "config-json");
  assert.equal(configCall.file, "/opt/guet-ac");
  assert.deepEqual(configCall.args.slice(0, 2), ["--config", "/opt/config.toml"]);
  assert.equal(configCall.options.timeout, 8000);
  assert.ok(configCall.options.maxBuffer >= 1024);
  assert.equal(configCall.options.env.GUET_AC_SESSION_DIR, "/opt/session");
  const statusCall = exec.calls.find((call) => call.args[2] === "status");
  assert.equal(statusCall.options.timeout, 12000);
  const locationsCall = exec.calls.find((call) => call.args[2] === "locations");
  assert.ok(locationsCall.args.includes("--json"));
  const systemctlCall = exec.calls.find((call) => call.file === "systemctl");
  assert.deepEqual(systemctlCall.args, [
    "--user",
    "show",
    "guet-ac",
    "-p",
    "ActiveState",
    "-p",
    "ExecStart",
  ]);
});

test("GET 在 status 失败时降级：courses=[] 且 coursesError 为中文原因", async () => {
  const routes = happyRoutes().filter(([predicate]) => !predicate("x", ["--config", "c", "status"]));
  routes.push([
    (file, args) => args[2] === "status",
    cliFailure(2, '{"error":"畅课会话已失效，请重新登录。"}'),
  ]);
  const { handle } = makeHandler({ routes });
  const res = fakeRes();
  await handle(fakeReq(), res);
  assert.equal(res.statusCode, 200);
  const data = JSON.parse(res.body);
  assert.deepEqual(data.courses, []);
  assert.equal(data.coursesError, "畅课会话已失效，请重新登录。");
  assert.equal(data.config.auto_number, true);
  assert.equal(data.service.active, true);
});

test("GET 读不到服务状态时 active=null 且不报错", async () => {
  const routes = happyRoutes().filter(([predicate]) => !predicate("systemctl", []));
  routes.push([
    (file) => file === "systemctl",
    () => {
      const error = new Error("not found");
      error.code = "ENOENT";
      throw error;
    },
  ]);
  const { handle } = makeHandler({ routes });
  const res = fakeRes();
  await handle(fakeReq(), res);
  assert.equal(res.statusCode, 200);
  const data = JSON.parse(res.body);
  assert.deepEqual(data.service, {
    active: null,
    autoNumber: null,
    autoGps: null,
    autoQr: null,
  });
  assert.equal(data.warning, "");
});

test("GET 服务未运行给出 warning，且 --auto 时按配置解析开关", async () => {
  const autoLine = [
    "ActiveState=inactive",
    "ExecStart={ argv[]=/opt/guet-ac watch --auto --event-dir /home/orangepi/events ; }",
  ].join("\n");
  const routes = happyRoutes().filter(([predicate]) => !predicate("systemctl", []));
  routes.push([(file) => file === "systemctl", () => ({ stdout: autoLine })]);
  const { handle } = makeHandler({ routes });
  const res = fakeRes();
  await handle(fakeReq(), res);
  const data = JSON.parse(res.body);
  assert.equal(data.service.active, false);
  assert.equal(data.service.autoNumber, true);
  assert.equal(data.service.autoGps, true);
  assert.equal(data.service.autoQr, false);
  assert.match(data.warning, /监控服务未运行/);
});

test("GET config-json 失败返回 503 中文原因", async () => {
  const routes = [
    [(file, args) => args[2] === "config-json", cliFailure(2, '{"error":"配置文件损坏。"}')],
    ...happyRoutes().filter(([predicate]) => !predicate("x", ["--config", "c", "config-json"])),
  ];
  const { handle } = makeHandler({ routes });
  const res = fakeRes();
  await handle(fakeReq(), res);
  assert.equal(res.statusCode, 503);
  const data = JSON.parse(res.body);
  assert.equal(data.error, "unavailable");
  assert.match(data.message, /配置文件损坏/);
});

test("GET locations 超时返回 503", async () => {
  const routes = [
    [
      (file, args) => args[2] === "locations",
      () => {
        const error = new Error("timeout");
        error.killed = true;
        error.signal = "SIGTERM";
        throw error;
      },
    ],
    ...happyRoutes().filter(([predicate]) => !predicate("x", ["--config", "c", "locations"])),
  ];
  const { handle } = makeHandler({ routes });
  const res = fakeRes();
  await handle(fakeReq(), res);
  assert.equal(res.statusCode, 503);
  assert.match(JSON.parse(res.body).message, /超时/);
});

test("POST 成功：只代理允许字段并返回最新 config", async () => {
  let stdin = null;
  const routes = [
    [
      (file, args) => args[2] === "config-update",
      ({ options }) => {
        stdin = JSON.parse(options.input);
        const updated = { ...CONFIG, course_locations: { "11": "osm-way-606411548", "12": "osm-way-604" } };
        return { stdout: JSON.stringify(updated) };
      },
    ],
    ...happyRoutes(),
  ];
  let originChecked = 0;
  const { handle } = makeHandler({
    routes,
    sameOrigin: () => {
      originChecked += 1;
      return true;
    },
  });
  const res = fakeRes();
  await handle(
    fakeReq({
      method: "POST",
      body: JSON.stringify({
        config: {
          course_locations: { "11": "osm-way-606411548", "12": "osm-way-604" },
          auto_gps: false,
          account: "should-be-dropped",
        },
      }),
    }),
    res,
  );
  assert.equal(originChecked, 1);
  assert.equal(res.statusCode, 200);
  const data = JSON.parse(res.body);
  assert.equal(data.ok, true);
  assert.deepEqual(Object.keys(stdin).sort(), ["auto_gps", "course_locations"]);
  assert.equal(stdin.account, undefined);
  assert.deepEqual(data.config.course_locations, {
    "11": "osm-way-606411548",
    "12": "osm-way-604",
  });
});

test("POST 接受平铺字段", async () => {
  let stdin = null;
  const routes = [
    [
      (file, args) => args[2] === "config-update",
      ({ options }) => {
        stdin = JSON.parse(options.input);
        return { stdout: JSON.stringify({ ...CONFIG, auto_number: false }) };
      },
    ],
    ...happyRoutes(),
  ];
  const { handle } = makeHandler({ routes });
  const res = fakeRes();
  await handle(fakeReq({ method: "POST", body: JSON.stringify({ auto_number: false }) }), res);
  assert.equal(res.statusCode, 200);
  assert.deepEqual(stdin, { auto_number: false });
  assert.equal(JSON.parse(res.body).config.auto_number, false);
});

test("POST CLI 校验失败（退出码 2）返回 400 中文原因", async () => {
  const routes = [
    [
      (file, args) => args[2] === "config-update",
      cliFailure(2, '{"error":"未知地点：bad，请运行 locations 查看。"}'),
    ],
    ...happyRoutes(),
  ];
  const { handle } = makeHandler({ routes });
  const res = fakeRes();
  await handle(
    fakeReq({ method: "POST", body: JSON.stringify({ course_locations: { "11": "bad" } }) }),
    res,
  );
  assert.equal(res.statusCode, 400);
  const data = JSON.parse(res.body);
  assert.equal(data.error, "invalid");
  assert.match(data.message, /未知地点/);
});

test("POST 跨源返回 403 且不调用子进程", async () => {
  const { handle, exec } = makeHandler({ sameOrigin: () => false });
  const res = fakeRes();
  await handle(fakeReq({ method: "POST", body: JSON.stringify({ auto_gps: true }) }), res);
  assert.equal(res.statusCode, 403);
  assert.equal(JSON.parse(res.body).error, "origin");
  assert.equal(exec.calls.length, 0);
});

test("POST 没有允许字段返回 400", async () => {
  const { handle, exec } = makeHandler();
  const res = fakeRes();
  await handle(
    fakeReq({ method: "POST", body: JSON.stringify({ config: { account: "x" } }) }),
    res,
  );
  assert.equal(res.statusCode, 400);
  assert.match(JSON.parse(res.body).message, /没有可更新/);
  assert.equal(exec.calls.length, 0);
});

test("POST 超时返回 503", async () => {
  const routes = [
    [
      (file, args) => args[2] === "config-update",
      () => {
        const error = new Error("timeout");
        error.killed = true;
        error.signal = "SIGTERM";
        throw error;
      },
    ],
    ...happyRoutes(),
  ];
  const { handle } = makeHandler({ routes });
  const res = fakeRes();
  await handle(fakeReq({ method: "POST", body: JSON.stringify({ accuracy: 40 }) }), res);
  assert.equal(res.statusCode, 503);
  assert.equal(JSON.parse(res.body).error, "timeout");
});

test("POST 成功但输出无法解析时回退读取 config-json", async () => {
  const routes = [
    [(file, args) => args[2] === "config-update", () => ({ stdout: "not-json" })],
    ...happyRoutes(),
  ];
  const { handle } = makeHandler({ routes });
  const res = fakeRes();
  await handle(fakeReq({ method: "POST", body: JSON.stringify({ accuracy: 42 }) }), res);
  assert.equal(res.statusCode, 200);
  assert.equal(JSON.parse(res.body).config.accuracy, 35.0);
});

test("非法 JSON body 返回 400", async () => {
  const { handle } = makeHandler();
  const res = fakeRes();
  await handle(fakeReq({ method: "POST", body: "{bad" }), res);
  assert.equal(res.statusCode, 400);
  assert.equal(JSON.parse(res.body).error, "invalid");
});

test("未知方法返回 405", async () => {
  const { handle, exec } = makeHandler();
  const res = fakeRes();
  await handle(fakeReq({ method: "DELETE" }), res);
  assert.equal(res.statusCode, 405);
  assert.equal(JSON.parse(res.body).error, "method_not_allowed");
  assert.equal(exec.calls.length, 0);
});

test("未注入 exec 时默认路径按接口文档解析", async () => {
  const exec = makeExec(happyRoutes());
  const instance = createGuetConfig({ exec, home: "/tmp/fake-home" });
  const res = fakeRes();
  await instance(fakeReq(), res, {
    pathname: "/api/guet-config",
    person: "admin",
    isAdmin: true,
    sameOrigin: () => true,
  });
  assert.equal(res.statusCode, 200);
  const configCall = exec.calls.find((call) => call.args[2] === "config-json");
  assert.ok(configCall.file.endsWith("guet-ac"));
  assert.equal(configCall.args[0], "--config");
  assert.ok(configCall.args[1].endsWith("config.toml"));
  assert.ok(configCall.options.env.GUET_AC_SESSION_DIR.endsWith("session"));
});
