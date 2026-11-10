// What the PDF viewer relies on in the PDF.js build it bundles, checked in the installed files, so
// an upgrade that changes any of it fails here instead of in the browser.
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(path, "utf8");

interface Manifest {
  dependencies: Record<string, string>;
}
interface Lock {
  packages: Record<string, { version?: string; dependencies?: Record<string, string> }>;
}

const manifest = JSON.parse(read("package.json")) as Manifest;
const lock = JSON.parse(read("package-lock.json")) as Lock;

// react-pdf imports "pdfjs-dist" (its `main`, build/pdf.mjs); the viewer imports the worker.
const PDFJS_MAIN = "node_modules/pdfjs-dist/build/pdf.mjs";
const PDFJS_WORKER = "node_modules/pdfjs-dist/build/pdf.worker.min.mjs";

function filesUnder(folder: string): string[] {
  return readdirSync(folder, { withFileTypes: true, recursive: true })
    .filter((entry) => entry.isFile() && /\.(m?js|tsx?)$/.test(entry.name))
    .map((entry) => join(entry.parentPath, entry.name));
}

describe("the bundled PDF.js", () => {
  it("is pinned to exact versions, and react-pdf uses the same PDF.js as the worker", () => {
    const pdfjs = manifest.dependencies["pdfjs-dist"];
    const reactPdf = manifest.dependencies["react-pdf"];
    expect(pdfjs).toMatch(/^\d+\.\d+\.\d+$/);
    expect(reactPdf).toMatch(/^\d+\.\d+\.\d+$/);
    expect(lock.packages["node_modules/pdfjs-dist"]?.version).toBe(pdfjs);
    expect(lock.packages["node_modules/react-pdf"]?.version).toBe(reactPdf);
    // PDF.js refuses a worker of another version, so there must be one copy, the one pinned here.
    expect(lock.packages["node_modules/react-pdf"]?.dependencies?.["pdfjs-dist"]).toBe(pdfjs);
    const nested = Object.keys(lock.packages).filter((path) =>
      /node_modules\/.+\/node_modules\/pdfjs-dist$/.test(path),
    );
    expect(nested).toEqual([]);
  });

  it("is the build the viewer imports", () => {
    expect(read("src/pages/PdfViewer.tsx")).toContain(
      'from "pdfjs-dist/build/pdf.worker.min.mjs?worker&url"',
    );
    const pdfjsManifest = JSON.parse(read("node_modules/pdfjs-dist/package.json")) as {
      main: string;
    };
    expect(`node_modules/pdfjs-dist/${pdfjsManifest.main}`).toBe(PDFJS_MAIN);
  });

  it.each([PDFJS_MAIN, PDFJS_WORKER])("has no eval path in %s", (path) => {
    // The policy's script-src 'self' has no 'unsafe-eval', and PDF.js 6 has no option to need it.
    const code = read(path);
    expect(code).not.toMatch(/\beval\s*\(/);
    expect(code).not.toMatch(/\bnew\s+Function\s*\(/);
    expect(code).not.toMatch(/\bFunction\s*\(\s*["'`]/);
    expect(code).not.toContain("isEvalSupported");
  });

  it("is never given PDF.js's scripting sandbox or an address on another host", () => {
    // The sandbox is what runs a PDF's JavaScript; neither react-pdf nor the app loads it.
    for (const file of filesUnder("node_modules/react-pdf/dist")) {
      expect(read(file), file).not.toMatch(/sandbox|enableScripting/);
    }
    for (const file of filesUnder("src").filter((name) => !/\.test\.tsx?$/.test(name))) {
      const code = read(file);
      expect(code, file).not.toMatch(/(from\s+|import\s*\(\s*)["'][^"']*pdf\.sandbox/);
      expect(code, file).not.toMatch(/enableScripting|unpkg\.com|jsdelivr|cdnjs/);
    }
  });
});
