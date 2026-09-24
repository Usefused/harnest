import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const repo = path.resolve(root, "../..");
const env = {
  ...process.env,
  PYTHONPATH: [path.join(repo, "src"), process.env.PYTHONPATH]
    .filter(Boolean)
    .join(path.delimiter),
};
const python = process.env.HARNEST_PYTHON || "python3";
const children = [];
let stopping = false;

function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of children) child.kill("SIGTERM");
  process.exitCode = code;
}

function start(command, args) {
  const child = spawn(command, args, { cwd: root, env, stdio: "inherit" });
  children.push(child);
  child.on("error", (error) => {
    console.error(error.message);
    stop(1);
  });
  child.on("exit", (code) => {
    if (!stopping) stop(code || 1);
  });
}

for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => stop());
start(python, ["serve.py", "--framework", "adk", "--port", "1910"]);
start(python, ["serve.py", "--framework", "langgraph", "--port", "1911"]);
start(process.execPath, ["node_modules/vite/bin/vite.js"]);
