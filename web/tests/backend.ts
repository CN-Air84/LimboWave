// Process adapter only. The actual fixture is backend.py and uses real WebServer.
import { spawn, type ChildProcess } from "node:child_process";
import { resolve } from "node:path";
export type Backend = {
  process: ChildProcess;
  ticket: string;
  code: string;
  url: string;
};
export async function startBackend(): Promise<Backend> {
  const root = resolve(import.meta.dirname, "../..");
  const child = spawn(
    process.env.LIMBOWAVE_PYTHON || resolve(root, ".venv/Scripts/python.exe"),
    ["-u", resolve(import.meta.dirname, "backend.py"), "--port", "8877"],
    {
      cwd: root,
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONPATH: [resolve(root, "src"), root].join(
          process.platform === "win32" ? ";" : ":",
        ),
        PYTHONIOENCODING: "utf-8",
      },
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  const config = await new Promise<{
    ticket: string;
    code: string;
    url: string;
  }>((resolveReady, reject) => {
    let output = "",
      errors = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`Backend startup timed out: ${errors}`));
    }, 20000);
    child.stderr?.on("data", (chunk) => {
      errors += chunk.toString();
    });
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`Backend exited ${code}: ${errors}`));
    });
    child.stdout?.on("data", (chunk) => {
      output += chunk.toString();
      const line = output.split("\n").find((s) => s.startsWith("{"));
      if (line) {
        try {
          const data = JSON.parse(line);
          if (data.ticket && data.url) {
            clearTimeout(timer);
            resolveReady(data);
          }
        } catch {
          /* Wait for complete JSON line. */
        }
      }
    });
  });
  return { process: child, ...config };
}
export async function stopBackend(backend: Backend | undefined) {
  if (!backend || backend.process.exitCode !== null) return;
  await new Promise<void>((resolveStopped) => {
    const timer = setTimeout(() => {
      backend.process.kill();
      resolveStopped();
    }, 7000);
    backend.process.once("exit", () => {
      clearTimeout(timer);
      resolveStopped();
    });
    backend.process.stdin?.end();
  });
}
