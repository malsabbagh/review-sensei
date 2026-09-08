#!/usr/bin/env node
"use strict";

const { createRequire } = require("node:module");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const childProcess = require("node:child_process");

const launcherPackage = require("../package.json");
const launcherRequire = createRequire(path.join(__dirname, "..", "package.json"));

// Keep this table deliberately boring and explicit. Adding an alias here is a
// public support commitment and must be accompanied by a native build lane.
const TARGETS = Object.freeze([
  Object.freeze({
    id: "darwin-arm64",
    platform: "darwin",
    arch: "arm64",
    packageName: "@reviewsensei/cli-darwin-arm64",
    payload: "bin/review-sensei",
  }),
  Object.freeze({
    id: "darwin-x64",
    platform: "darwin",
    arch: "x64",
    packageName: "@reviewsensei/cli-darwin-x64",
    payload: "bin/review-sensei",
  }),
  Object.freeze({
    id: "linux-arm64-gnu",
    platform: "linux",
    arch: "arm64",
    packageName: "@reviewsensei/cli-linux-arm64-gnu",
    payload: "bin/review-sensei",
  }),
  Object.freeze({
    id: "linux-x64-gnu",
    platform: "linux",
    arch: "x64",
    packageName: "@reviewsensei/cli-linux-x64-gnu",
    payload: "bin/review-sensei",
  }),
  Object.freeze({
    id: "win32-x64",
    platform: "win32",
    arch: "x64",
    packageName: "@reviewsensei/cli-win32-x64",
    payload: "bin/review-sensei.exe",
  }),
]);

const SUPPORTED_TARGETS = TARGETS.map((target) => target.id).join(", ");
const SIGNALS = Object.freeze(["SIGINT", "SIGTERM", "SIGHUP"]);

class LauncherError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "LauncherError";
    this.code = code;
  }
}

function unsupportedTarget(platform, arch, reason) {
  const suffix = reason ? ` (${reason})` : "";
  return new LauncherError(
    "UNSUPPORTED_TARGET",
    `unsupported target ${String(platform)}/${String(arch)}${suffix}; supported targets: ${SUPPORTED_TARGETS}`,
  );
}

function glibcVersion(report) {
  try {
    const value = typeof report === "function" ? report() : report;
    const version = value && value.header && value.header.glibcVersionRuntime;
    return typeof version === "string" ? version.trim() : "";
  } catch (_error) {
    return "";
  }
}

/** Return the one supported target for a Node platform/architecture pair. */
function selectTarget(
  platform = process.platform,
  arch = process.arch,
  report = process.report,
) {
  const target = TARGETS.find(
    (candidate) => candidate.platform === platform && candidate.arch === arch,
  );
  if (!target) {
    throw unsupportedTarget(platform, arch);
  }
  const reportValue =
    report && typeof report.getReport === "function"
      ? report.getReport.bind(report)
      : report;
  if (platform === "linux" && !glibcVersion(reportValue)) {
    throw unsupportedTarget(platform, arch, "glibc runtime was not detected");
  }
  return target;
}

function packageManifestPath(target, requirePackageJson, resolvePackageJson) {
  if (typeof resolvePackageJson === "function") {
    return resolvePackageJson(`${target.packageName}/package.json`);
  }
  return requirePackageJson.resolve(`${target.packageName}/package.json`);
}

/**
 * Resolve the executable from the installed optional package.
 *
 * Only the package's own package.json is loaded. The package and binary are
 * checked before any process is spawned, so a partial npm installation fails
 * closed without a fallback downloader or system-Python invocation.
 */
function resolvePlatformExecutable(target, options = {}) {
  const requirePackageJson = options.requirePackageJson || launcherRequire;
  const expectedVersion = options.launcherVersion || launcherPackage.version;
  let manifestPath;
  let manifest;
  try {
    manifestPath = packageManifestPath(
      target,
      requirePackageJson,
      options.resolvePackageJson,
    );
    manifest = options.loadPackageJson
      ? options.loadPackageJson(`${target.packageName}/package.json`)
      : requirePackageJson(`${target.packageName}/package.json`);
  } catch (_error) {
    throw new LauncherError(
      "PLATFORM_PACKAGE_MISSING",
      `supported platform package ${target.packageName} is not installed`,
    );
  }

  if (!manifest || manifest.name !== target.packageName) {
    throw new LauncherError(
      "PLATFORM_PACKAGE_INVALID",
      `platform package ${target.packageName} has invalid metadata`,
    );
  }
  if (manifest.version !== expectedVersion) {
    throw new LauncherError(
      "PLATFORM_VERSION_MISMATCH",
      `platform package ${target.packageName} does not match launcher version`,
    );
  }
  // Platform packages intentionally have no npm `bin` field. The launcher is
  // the sole owner of the public command, avoiding a transitive optional
  // dependency shadowing this shim in node_modules/.bin.
  if (Object.prototype.hasOwnProperty.call(manifest, "bin")) {
    throw new LauncherError(
      "PLATFORM_PAYLOAD_INVALID",
      `platform package ${target.packageName} must not expose a command`,
    );
  }

  const packageRoot = path.dirname(manifestPath);
  const executable = path.resolve(packageRoot, target.payload);
  const relative = path.relative(packageRoot, executable);
  if (!relative || relative.startsWith(".." + path.sep) || path.isAbsolute(relative)) {
    throw new LauncherError(
      "PLATFORM_PAYLOAD_INVALID",
      `platform package ${target.packageName} executable is outside its package`,
    );
  }
  let stat;
  try {
    stat = options.lstatSync
      ? options.lstatSync(executable)
      : fs.lstatSync(executable);
  } catch (_error) {
    throw new LauncherError(
      "PLATFORM_PAYLOAD_INVALID",
      `platform package ${target.packageName} executable is not a regular file`,
    );
  }
  if (!stat.isFile() || (typeof stat.isSymbolicLink === "function" && stat.isSymbolicLink())) {
    throw new LauncherError(
      "PLATFORM_PAYLOAD_INVALID",
      `platform package ${target.packageName} executable is not a regular file`,
    );
  }
  return executable;
}

function safeErrorMessage(error) {
  if (error instanceof LauncherError) return error.message;
  return "unable to start the ReviewSensei platform executable";
}

function signalExitStatus(signal) {
  const number = os.constants.signals && os.constants.signals[signal];
  return 128 + (typeof number === "number" ? number : 1);
}

/**
 * Spawn the selected native executable while forwarding raw argv and process
 * behavior. The optional seams are intentionally small so this contract can
 * be tested with Node's built-in test runner without a child process.
 */
function run(argv = [], options = {}) {
  const runtime = options.process || process;
  const writeError =
    options.writeError ||
    ((message) => {
      if (runtime.stderr && typeof runtime.stderr.write === "function") {
        runtime.stderr.write(`${message}\n`);
      }
    });
  let target;
  let executable;
  try {
    target =
      options.target ||
      selectTarget(
        options.platform === undefined ? runtime.platform : options.platform,
        options.arch === undefined ? runtime.arch : options.arch,
        options.report === undefined ? runtime.report : options.report,
      );
    executable = (options.resolveExecutable || resolvePlatformExecutable)(target, {
      launcherVersion: options.launcherVersion || launcherPackage.version,
      requirePackageJson: options.requirePackageJson,
      resolvePackageJson: options.resolvePackageJson,
      loadPackageJson: options.loadPackageJson,
      lstatSync: options.lstatSync,
    });
  } catch (error) {
    writeError(`review-sensei: ${safeErrorMessage(error)}`);
    return Promise.resolve(1);
  }

  const spawn = options.spawn || childProcess.spawn;
  const spawnOptions = {
    shell: false,
    stdio: "inherit",
    cwd: runtime.cwd(),
    env: runtime.env,
  };
  let child;
  try {
    child = spawn(executable, [...argv], spawnOptions);
  } catch (_error) {
    writeError("review-sensei: unable to start the ReviewSensei platform executable");
    return Promise.resolve(1);
  }

  return new Promise((resolve) => {
    let settled = false;
    const removers = [];
    const cleanup = () => {
      for (const remove of removers.splice(0)) remove();
    };
    const finish = (code) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(code);
    };
    const onExit = (code, signal) => {
      if (typeof code === "number") finish(code);
      else if (typeof signal === "string" && signal) finish(signalExitStatus(signal));
      else finish(1);
    };
    if (child && typeof child.once === "function") {
      child.once("error", () => {
        writeError("review-sensei: unable to start the ReviewSensei platform executable");
        finish(1);
      });
      child.once("exit", onExit);
    } else {
      finish(1);
      return;
    }
    const add = typeof runtime.on === "function" ? "on" : null;
    const remove =
      typeof runtime.off === "function"
        ? "off"
        : typeof runtime.removeListener === "function"
          ? "removeListener"
          : null;
    if (add && remove && child && typeof child.kill === "function") {
      for (const signal of SIGNALS) {
        const forward = () => child.kill(signal);
        runtime[add](signal, forward);
        removers.push(() => runtime[remove](signal, forward));
      }
    }
  });
}

module.exports = {
  LauncherError,
  SIGNALS,
  SUPPORTED_TARGETS,
  TARGETS,
  resolvePlatformExecutable,
  run,
  selectTarget,
};

if (require.main === module) {
  run(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  });
}
