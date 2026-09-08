"use strict";

const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const assert = require("node:assert/strict");

const launcher = require("../bin/review-sensei.js");

const glibcReport = { getReport: () => ({ header: { glibcVersionRuntime: "2.36" } }) };

test("maps exactly the supported targets", () => {
  assert.deepEqual(
    launcher.TARGETS.map((target) => target.id),
    ["darwin-arm64", "darwin-x64", "linux-arm64-gnu", "linux-x64-gnu", "win32-x64"],
  );
  assert.equal(launcher.selectTarget("darwin", "arm64").packageName, "@reviewsensei/cli-darwin-arm64");
  assert.equal(launcher.selectTarget("darwin", "x64").packageName, "@reviewsensei/cli-darwin-x64");
  assert.equal(launcher.selectTarget("linux", "arm64", glibcReport).id, "linux-arm64-gnu");
  assert.equal(launcher.selectTarget("linux", "x64", glibcReport).id, "linux-x64-gnu");
  assert.equal(launcher.selectTarget("win32", "x64").id, "win32-x64");
});

test("rejects unknown, musl, and missing-glibc targets before spawning", () => {
  for (const [platform, arch, report] of [
    ["freebsd", "x64", undefined],
    ["linux", "x64", { getReport: () => ({ header: {} }) }],
    ["linux", "arm64", { getReport: () => ({ header: { glibcVersionRuntime: "" } }) }],
  ]) {
    assert.throws(
      () => launcher.selectTarget(platform, arch, report),
      (error) => {
        assert.equal(error.code, "UNSUPPORTED_TARGET");
        assert.match(error.message, new RegExp(`${platform}/${arch}`));
        assert.match(error.message, /supported targets: .*linux-x64-gnu/);
        assert.doesNotMatch(error.message, /TOKEN|secret|password|HOME/);
        return true;
      },
    );
  }
});

function fakeRuntime(platform = "darwin", arch = "arm64") {
  const runtime = new EventEmitter();
  runtime.platform = platform;
  runtime.arch = arch;
  runtime.report = glibcReport;
  runtime.cwd = () => "/safe/current-directory";
  runtime.env = { REVIEW_MARKER: "must-not-be-printed" };
  runtime.stderr = { write: () => {} };
  return runtime;
}

test("forwards argv and inherited process options, and returns child exit code", async () => {
  const runtime = fakeRuntime();
  let call;
  const child = new EventEmitter();
  const code = await launcher.run(["--title", "space & *", "--diff", "$(echo nope)"], {
    process: runtime,
    resolveExecutable: () => "/safe/bin/review-sensei",
    spawn: (executable, argv, options) => {
      call = { executable, argv, options };
      setImmediate(() => child.emit("exit", 17, null));
      return child;
    },
  });
  assert.equal(code, 17);
  assert.deepEqual(call.argv, ["--title", "space & *", "--diff", "$(echo nope)"]);
  assert.equal(call.executable, "/safe/bin/review-sensei");
  assert.deepEqual(call.options, {
    shell: false,
    stdio: "inherit",
    cwd: "/safe/current-directory",
    env: runtime.env,
  });
});

test("forwards supported signals and converts signal exits", async () => {
  const runtime = fakeRuntime();
  const child = new EventEmitter();
  const killed = [];
  child.kill = (signal) => killed.push(signal);
  const result = launcher.run([], {
    process: runtime,
    resolveExecutable: () => "/safe/bin/review-sensei",
    spawn: () => {
      setImmediate(() => {
        runtime.emit("SIGINT");
        child.emit("exit", null, "SIGTERM");
      });
      return child;
    },
  });
  assert.equal(await result, 143);
  assert.deepEqual(killed, ["SIGINT"]);
  assert.equal(runtime.listenerCount("SIGINT"), 0);
  assert.equal(runtime.listenerCount("SIGTERM"), 0);
  assert.equal(runtime.listenerCount("SIGHUP"), 0);
});

test("sanitizes missing-package and spawn errors", async () => {
  const messages = [];
  const runtime = fakeRuntime();
  const missing = await launcher.run([], {
    process: runtime,
    resolveExecutable: () => {
      throw new Error("/home/user/.npm/_cacache/TOKEN=secret");
    },
    writeError: (message) => messages.push(message),
  });
  assert.equal(missing, 1);
  assert.equal(messages.length, 1);
  assert.doesNotMatch(messages[0], /home|TOKEN|secret|cacache/);

  const spawnFailed = await launcher.run([], {
    process: runtime,
    resolveExecutable: () => "/safe/bin/review-sensei",
    spawn: () => {
      throw new Error("spawn /home/user/.ssh/id_rsa failed");
    },
    writeError: (message) => messages.push(message),
  });
  assert.equal(spawnFailed, 1);
  assert.doesNotMatch(messages.at(-1), /home|id_rsa/);
});

test("validates exact installed package metadata and regular payload", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "reviewsensei-launcher-"));
  try {
    const packageRoot = path.join(root, "platform");
    fs.mkdirSync(path.join(packageRoot, "bin"), { recursive: true });
    const executable = path.join(packageRoot, "bin", "review-sensei");
    fs.writeFileSync(executable, "#!/bin/sh\n", { mode: 0o755 });
    const manifest = {
      name: "@reviewsensei/cli-darwin-arm64",
      version: "0.1.0",
    };
    const fakeRequire = (request) => {
      assert.equal(request, "@reviewsensei/cli-darwin-arm64/package.json");
      return manifest;
    };
    fakeRequire.resolve = (request) => {
      assert.equal(request, "@reviewsensei/cli-darwin-arm64/package.json");
      return path.join(packageRoot, "package.json");
    };
    assert.equal(
      launcher.resolvePlatformExecutable(launcher.TARGETS[0], {
        requirePackageJson: fakeRequire,
      }),
      executable,
    );

    assert.throws(
      () => launcher.resolvePlatformExecutable(launcher.TARGETS[0], {
        requirePackageJson: fakeRequire,
        launcherVersion: "9.9.9",
      }),
      /does not match launcher version/,
    );
    manifest.bin = { "review-sensei": "bin/review-sensei" };
    assert.throws(
      () => launcher.resolvePlatformExecutable(launcher.TARGETS[0], {
        requirePackageJson: fakeRequire,
      }),
      /must not expose a command/,
    );
    delete manifest.bin;
    assert.throws(
      () => launcher.resolvePlatformExecutable(launcher.TARGETS[0], {
        requirePackageJson: fakeRequire,
        lstatSync: () => {
          throw new Error("/home/user/.ssh/id_rsa");
        },
      }),
      /not a regular file/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
