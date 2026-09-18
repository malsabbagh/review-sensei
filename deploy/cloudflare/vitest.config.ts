import { readFileSync } from "node:fs";
import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

function yamlTextPlugin() {
  return {
    name: "yaml-text",
    load(id: string) {
      if (id.endsWith(".yml")) {
        const content = readFileSync(id, "utf8");
        return `export default ${JSON.stringify(content)};`;
      }
    },
  };
}

export default defineConfig({
  plugins: [yamlTextPlugin()],
  resolve: {
    alias: {
      "cloudflare:workers": fileURLToPath(
        new URL("./test/shims/cloudflare-workers.ts", import.meta.url),
      ),
    },
  },
  test: {
    include: ["test/**/*.test.ts"],
    environment: "node",
    pool: "forks",
  },
});
