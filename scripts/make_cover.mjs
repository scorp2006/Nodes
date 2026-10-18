// The blog cover: a white, rounded, friendly card. "Nodes" in Fredoka, the node mascots (the same faces as the
// dashboard) sitting on a soft graph, talking like the gate. Builds assets/cover.html and renders it to a 2000x840 PNG
// (DEV's 1000x420 cover at 2x) with headless Chrome or Edge.
//   node scripts/make_cover.mjs
import {execFileSync} from "node:child_process";
import {existsSync, readFileSync, writeFileSync} from "node:fs";
import {dirname, join} from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = readFileSync(join(root, "nodes/web/animals.js"), "utf8");
const {animalSVG, colorFor} = await import("data:text/javascript," + encodeURIComponent(src));

// cast: the name picks the animal and colour in order, exactly as the dashboard does
const cast = ["cat", "bunny", "bear", "frog"].map(n => `cover-${n}`);
cast.forEach(n => colorFor(n));
const [cat, bunny, bear, frog] = cast;

const node = (who, x, y, size, bubble, tone) => `
  <div class="node" style="left:${x}px;top:${y}px;--c:${colorFor(who)};width:${size}px;height:${size}px">${animalSVG(who, size - 14)}</div>
  ${bubble ? `<div class="bubble ${tone}" style="left:${x + size / 2}px;top:${y - 46}px">${bubble}</div>` : ""}`;

const html = `<!doctype html><html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@400;500;600;700&display=block" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0}
html,body{width:1000px;height:420px;background:#fff;overflow:hidden}
body{position:relative;font-family:Fredoka,"Arial Rounded MT Bold","Segoe UI",sans-serif;color:#16161a}
.dot{position:absolute;border-radius:50%}
h1{position:absolute;left:62px;top:52px;font-size:150px;font-weight:700;letter-spacing:-3px;line-height:1}
.tag{position:absolute;left:68px;top:232px;font-size:31px;font-weight:600;color:#2b2b31;letter-spacing:-.3px}
.sub{position:absolute;left:68px;top:280px;font-size:18px;font-weight:400;color:#6c6c74;width:440px;line-height:1.35}
.pills{position:absolute;left:66px;top:340px;display:flex;gap:10px}
.pill{font-size:16px;font-weight:600;padding:7px 16px;border-radius:999px;background:var(--bg);color:var(--fg)}
svg.edges{position:absolute;left:0;top:0;width:1000px;height:420px}
.node{position:absolute;border-radius:50%;background:#fff;border:5px solid var(--c);display:grid;place-items:center;
  box-shadow:0 10px 24px rgba(30,30,50,.12)}
.hub{position:absolute;width:30px;height:30px;border-radius:50%;background:#16161a;box-shadow:0 0 0 7px #fff,0 0 0 9px #ececf0}
.bubble{position:absolute;transform:translateX(-50%);font-size:17px;font-weight:600;padding:6px 14px;border-radius:14px;background:#fff;
  border:2px solid #16161a;white-space:nowrap;box-shadow:0 4px 0 #16161a}
.bubble:after{content:"";position:absolute;left:50%;bottom:-9px;margin-left:-7px;width:12px;height:12px;background:#fff;
  border-right:2px solid #16161a;border-bottom:2px solid #16161a;transform:rotate(45deg)}
.go{color:#2a9d6a}.wait{color:#d08a00}.hand{color:#2f7fd1}.ink{color:#16161a}
</style></head><body>
<!-- soft confetti -->
${[[14, 18, 14, "#ffd166"], [948, 380, 18, "#5ab4ff"], [520, 32, 10, "#ff8a7a"], [470, 380, 12, "#4cd4a0"], [975, 40, 9, "#ff8fab"],
   [36, 372, 9, "#5ab4ff"], [560, 230, 8, "#ffd166"], [420, 132, 7, "#ff8fab"]]
  .map(([x, y, s, c]) => `<div class="dot" style="left:${x}px;top:${y}px;width:${s}px;height:${s}px;background:${c}"></div>`).join("")}
<svg class="edges" viewBox="0 0 1000 420">
  <g fill="none" stroke-linecap="round" stroke-width="5">
    <path d="M760 214 C 720 170, 690 150, 668 140" stroke="#e4e4ea"/>
    <path d="M760 214 C 800 160, 830 130, 872 112" stroke="#e4e4ea"/>
    <path d="M760 214 C 720 270, 680 300, 650 318" stroke="#e4e4ea"/>
    <path d="M760 214 C 810 260, 840 290, 870 306" stroke="#e4e4ea"/>
    <path d="M900 150 C 940 210, 930 250, 905 280" stroke="#f2b84b" stroke-dasharray="2 12"/>
  </g>
</svg>
<h1>Nodes</h1>
<div class="tag">Coding agents that don’t collide.</div>
<div class="sub">One shared graph tells every AI agent who is working where, before it edits.</div>
<div class="pills">
  <span class="pill" style="--bg:#ffe6e2;--fg:#d65a4a">FalkorDB</span>
  <span class="pill" style="--bg:#e3f1ff;--fg:#2f7fd1">MCP</span>
  <span class="pill" style="--bg:#ddf6ec;--fg:#23885c">Claude Code</span>
</div>
<div class="hub" style="left:745px;top:199px"></div>
${node(cat, 612, 92, 104, "go!", "go")}
${node(bunny, 836, 62, 104, "wait…", "wait")}
${node(bear, 592, 270, 98, "handoff", "hand")}
${node(frog, 836, 262, 98, "on it", "ink")}
</body></html>`;

const out = join(root, "assets");
writeFileSync(join(out, "cover.html"), html);
const browsers = ["C:/Program Files/Google/Chrome/Application/chrome.exe", "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
                  "/usr/bin/google-chrome", "/usr/bin/chromium"];
const bin = browsers.find(existsSync);
if (!bin) throw new Error("no Chrome or Edge found to render the cover");
execFileSync(bin, ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2", "--window-size=1000,420",
                   "--virtual-time-budget=8000", `--screenshot=${join(out, "nodes-cover.png")}`,
                   pathToFileURL(join(out, "cover.html")).href], {stdio: "ignore"});
console.log(join(out, "nodes-cover.png"));
