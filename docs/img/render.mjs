// Renders every docs/img/*.dot to an .svg (white background, so it reads in GitHub light and dark mode).
//   npm i @viz-js/viz@3.11.0   then, from that folder:   node <repo>/docs/img/render.mjs <repo>/docs/img
import {readdirSync, readFileSync, writeFileSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {createRequire} from "node:module";

const here = process.argv[2] || dirname(fileURLToPath(import.meta.url));
const viz_ = await import(pathToFileURL(createRequire(join(process.cwd(), "x.js")).resolve("@viz-js/viz")).href);
const instance = viz_.instance ?? viz_.default.instance;
const viz = await instance();
for (const f of readdirSync(here).filter(f => f.endsWith(".dot"))) {
  writeFileSync(join(here, f.replace(/\.dot$/, ".svg")), viz.renderString(readFileSync(join(here, f), "utf8"), {format: "svg"}));
  console.log("wrote", f.replace(/\.dot$/, ".svg"));
}
