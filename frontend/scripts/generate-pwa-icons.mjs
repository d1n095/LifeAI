// Generate the PWA icon set from scripts/pwa-icon-source.svg into public/icons/.
// Uses `sharp` (already a frontend dependency via package.json overrides). Run:
//   node scripts/generate-pwa-icons.mjs
// The generated PNGs are committed (static assets served from /icons/*).
import sharp from "sharp";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, "pwa-icon-source.svg");
const outDir = join(here, "..", "public", "icons");

// background matches tailwind `base` so maskable corners are not transparent.
const BASE = "#05050a";

const targets = [
  { file: "icon-192.png", size: 192, flatten: false },
  { file: "icon-512.png", size: 512, flatten: false },
  // maskable: flatten onto the base color so the full square is opaque (no see-through corners).
  { file: "icon-maskable-512.png", size: 512, flatten: true },
  // apple-touch-icon for iOS/macOS Safari "Add to Dock".
  { file: "apple-touch-icon.png", size: 180, flatten: true },
];

await mkdir(outDir, { recursive: true });
for (const t of targets) {
  let img = sharp(src, { density: 384 }).resize(t.size, t.size, { fit: "contain" });
  if (t.flatten) img = img.flatten({ background: BASE });
  await img.png().toFile(join(outDir, t.file));
  console.log(`wrote public/icons/${t.file} (${t.size}x${t.size})`);
}
console.log("done");
