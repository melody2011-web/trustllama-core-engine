import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const PUBLIC_COPY_ROOTS = [
  {
    directory: "artifacts/llama-website/src",
    extensions: new Set([".js", ".jsx", ".ts", ".tsx"]),
  },
  {
    directory: "artifacts/llama-website/public",
    extensions: new Set([".html", ".json", ".md", ".txt"]),
  },
];
const ALWAYS_CHECK = ["artifacts/llama-website/index.html"];
const PUBLIC_IMAGE_DIRECTORY = "artifacts/llama-website/public/memes";
const APPROVED_PUBLIC_IMAGES = new Map([
  ["2026-08-30-tlama-community-v01.png", "bc92be0b8790b5121749602093a4fe231e8fc2375b60d24b4cbd961d14af6612"],
  ["2026-08-30-tlama-sheriff-v01.png", "9f0d9a27fac3bc3a179c8745a9c74807d2a4285e15e87f9f7f795f7973a100e1"],
  ["2026-08-30-tlama-space-v01.png", "7e1d588267760e27c51f347191f16ce0abaa4d3e5b8297415b2a781206df9add"],
  ["2026-08-30-tlama-vault-v01.png", "7d00e7550e0dbe4e85d36b187a32a35ee654876cd6ee8840bb7313a6fcc4545e"],
  ["2026-08-30-tlama-verify-v01.png", "a28758a9da30032950b9c04e38ea7d9dd80e64c33b02776d5f7883bd0181e86e"],
  ["2026-09-06-tlama-trust-llama-v01.png", "11ba40785c83188260c864d0f2189b511b2f947c4f3f9dc0d8d849b53fecd1a4"],
  ["2026-09-11-tlama-arcade-shield-v01.png", "1088d34c3eca3d058a88fd57948f717261f1a7cfb5f3991b13c5e736038c12d0"],
  ["2026-09-11-tlama-built-in-public-v01.png", "0b74b0ab47a83cb1bb1a310fa9e261cc60aa338ff16f5d44d83026d09bdfe6d4"],
  ["2026-09-11-tlama-evidence-trail-v01.png", "426b116a50105a39023ee05ca582cac596154b858bc1d02202fbf24bf23111f2"],
  ["2026-09-11-tlama-same-rules-v01.png", "66dcb87ae93631393b966eae6abb2a737ec3961957c2bf22f686457f838f2233"],
  ["2026-09-11-tlama-trust-the-proof-v01.png", "d156bde81f00f5c8f1269ea76498b651c8ee73a77d002fbf0b6c65c37045da61"],
  ["2026-09-16-tlama-security-matrix-v01.png", "9ac1f45f50333646426a9cca97010ad316ba3c37dd3d5ec12c632e0617d81f64"],
]);

const PROHIBITED_COPY = [
  {
    category: "finance-oriented product positioning",
    pattern: /\bGameFi\b/gi,
  },
  {
    category: "token pricing or valuation",
    pattern:
      /(?:\$[A-Z]{2,10}\b|\b(?:TLAMA|token|coin)(?:'s)?\s+(?:current\s+)?price\b|\bprice\s+(?:of|for)\s+(?:the\s+)?(?:TLAMA|token|coin)\b|\b(?:TLAMA|token|coin)\s+(?:is\s+)?priced\s+at\b|\b(?:TLAMA|token|coin)\s+(?:is\s+)?(?:worth|valued\s+at)\s+[$€£]?\d|\bmarket\s+cap\b|\bfully\s+diluted\s+valuation\b|\bFDV\b)/gi,
  },
  {
    category: "token sale or launch timeline",
    pattern:
      /\b(?:presale|pre-sale|public\s+sale|private\s+sale|token\s+sale|ICO|IDO|IEO|TGE|token\s+generation\s+event|fair\s+launch|launchpad|(?:TLAMA|token|coin)\s+launch(?:es|ing|ed)?|(?:TLAMA|token|coin)\s+(?:launches|goes\s+live)\s+(?:in\s+)?(?:Q[1-4]|\d{4})|launch\s+(?:date|timeline|quarter|Q[1-4]))\b/gi,
  },
  {
    category: "investment or returns language",
    pattern:
      /\b(?:invest(?:ment|ing|or|ors)?|guaranteed\s+returns?|passive\s+income|profit(?:able|s)?|ROI|APY|APR|yield\s+farming|price\s+appreciation|upside\s+potential|early\s+(?:buyer|investor)s?|earn\s+\d+(?:\.\d+)?%\s+(?:returns?|yield|profit)|\d+(?:\.\d+)?x\s+(?:returns?|gains?|profit|potential)|stake\s+(?:\$?TLAMA|the\s+token|tokens?|coins?)\s+(?:to|for)\s+(?:earn\s+)?(?:yield|returns?|rewards?|APY|APR))\b/gi,
  },
  {
    category: "liquidity promotion",
    pattern:
      /\b(?:liquidity\s+(?:pool|provider|mining|incentive|rewards?|locked|lock)|provide\s+liquidity|LP\s+(?:token|pool|rewards?)|DEX\s+liquidity)\b/gi,
  },
  {
    category: "token trading promotion",
    pattern:
      /\b(?:(?:buy|purchase|sell|trade|swap|acquire)\s+(?:the\s+|some\s+)?(?:\$?TLAMA|tokens?|coins?)\b|(?:\$?TLAMA|tokens?|coins?)\s+(?:is\s+|are\s+)?(?:now\s+)?(?:listed|listing|trading|available)\s+(?:on|at)\b|(?:token|coin)\s+(?:trading|listing|listed)|exchange\s+listing|trade\s+now|buy\s+now)\b/gi,
  },
  {
    category: "transaction tax or burn promotion",
    pattern:
      /\b(?:(?:buy|sell)\s+tax|tax\s+on\s+(?:buys?|sells?|trades?)|burn(?:ing|s|ed)?\s+(?:on\s+)?(?:every\s+)?(?:buy|sell|trade|transaction)|(?:buy|sell|trade)\s+and\s+burn)\b/gi,
  },
  {
    category: "token purchase or transaction onboarding",
    pattern:
      /\b(?:swap\s+(?:SOL|USDC|USDT|BNB|ETH)\s+for\s+\$?TLAMA|earn\s+\$?TLAMA|purchase\s+(?:with|using)\s+\$?TLAMA|(?:join|enter)\s+(?:the\s+)?(?:public\s+|token\s+)?sale|get\s+\$?TLAMA|wallet\s+balances?|request\s+(?:a\s+)?(?:live\s+)?quote|(?:Raydium|DEX)\s+(?:checkout|quote)|transaction\s+approval|approve\s+(?:the\s+)?(?:final\s+)?swap)\b/gi,
  },
];

export function findFinancialPromotionCopy(source, file = "<source>") {
  const findings = [];

  for (const { category, pattern } of PROHIBITED_COPY) {
    pattern.lastIndex = 0;
    for (const match of source.matchAll(pattern)) {
      const line = source.slice(0, match.index).split("\n").length;
      findings.push({ file, line, category, text: match[0] });
    }
  }

  return findings;
}

function extensionOf(file) {
  const dot = file.lastIndexOf(".");
  return dot === -1 ? "" : file.slice(dot);
}

async function listMatchingFiles(rootUrl, directory, extensions) {
  const files = [];
  const entries = await readdir(new URL(`${directory}/`, rootUrl), {
    withFileTypes: true,
  });

  for (const entry of entries) {
    const relativePath = `${directory}/${entry.name}`;
    if (entry.isDirectory()) {
      files.push(
        ...(await listMatchingFiles(rootUrl, relativePath, extensions)),
      );
    } else if (entry.isFile() && extensions.has(extensionOf(entry.name))) {
      files.push(relativePath);
    }
  }

  return files;
}

export async function listPublicGameCopyFiles(
  rootUrl = new URL("../", import.meta.url),
) {
  const files = [...ALWAYS_CHECK];

  for (const { directory, extensions } of PUBLIC_COPY_ROOTS) {
    files.push(...(await listMatchingFiles(rootUrl, directory, extensions)));
  }

  return files.sort();
}

export async function checkPublicGameCopy(rootUrl = new URL("../", import.meta.url)) {
  const findings = [];

  for (const relativePath of await listPublicGameCopyFiles(rootUrl)) {
    const source = await readFile(new URL(relativePath, rootUrl), "utf8");
    findings.push(...findFinancialPromotionCopy(source, relativePath));
  }

  return findings;
}

export async function checkPublicGameImages(
  rootUrl = new URL("../", import.meta.url),
) {
  const entries = await readdir(
    new URL(`${PUBLIC_IMAGE_DIRECTORY}/`, rootUrl),
    { withFileTypes: true },
  );
  const findings = [];

  for (const entry of entries) {
    if (!entry.isFile() || !/\.(?:png|jpe?g|webp|gif)$/i.test(entry.name)) {
      continue;
    }
    const expectedHash = APPROVED_PUBLIC_IMAGES.get(entry.name);
    if (!expectedHash) {
      findings.push({
        file: `${PUBLIC_IMAGE_DIRECTORY}/${entry.name}`,
        category: "unreviewed public artwork",
        text: entry.name,
      });
      continue;
    }
    const bytes = await readFile(
      new URL(`${PUBLIC_IMAGE_DIRECTORY}/${entry.name}`, rootUrl),
    );
    const actualHash = createHash("sha256").update(bytes).digest("hex");
    if (actualHash !== expectedHash) {
      findings.push({
        file: `${PUBLIC_IMAGE_DIRECTORY}/${entry.name}`,
        category: "changed public artwork",
        text: entry.name,
      });
    }
  }

  for (const approvedName of APPROVED_PUBLIC_IMAGES.keys()) {
    if (!entries.some((entry) => entry.isFile() && entry.name === approvedName)) {
      findings.push({
        file: `${PUBLIC_IMAGE_DIRECTORY}/${approvedName}`,
        category: "missing approved artwork",
        text: approvedName,
      });
    }
  }

  return findings;
}

async function main() {
  const findings = [
    ...(await checkPublicGameCopy()),
    ...(await checkPublicGameImages()),
  ];

  if (findings.length > 0) {
    console.error("Public game copy contains prohibited financial promotion:");
    for (const finding of findings) {
      console.error(
        `- ${finding.file}:${finding.line} [${finding.category}] ${JSON.stringify(finding.text)}`,
      );
    }
    process.exitCode = 1;
    return;
  }

  console.log("Public game copy compliance check passed.");
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  await main();
}