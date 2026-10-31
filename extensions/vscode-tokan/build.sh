#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

VERSION=$(node -p "require('./package.json').version")
mkdir -p out

# Compile TypeScript for real. tsc's per-file --outDir handling differs across
# versions; the project config (tsconfig.json / inline flags) must emit
# out/extension.js — never overwrite a real build with a stub.
if ! npx tsc --noEmit false --outDir ./out --declaration false \
      --esModuleInterop true --target ES2020 --module commonjs \
      --skipLibCheck src/extension.ts; then
  echo "tsc failed" >&2
  exit 1
fi

# Guard: refuse to package a stub or an empty build.
if ! grep -q "gatewayBase" out/extension.js 2>/dev/null; then
  echo "out/extension.js does not look compiled from src/extension.ts" >&2
  exit 1
fi

# Package .vsix (zip-based). VS Code requires every entry under extension/.
rm -rf .build
mkdir -p .build/extension
cp package.json .build/extension/
cp -r out .build/extension/
cp -r src .build/extension/ 2>/dev/null || true
[ -f README.md ] && cp README.md .build/extension/

# zip updates an existing archive in place — always start from scratch so a
# stale layout from an older build can never leak into the package.
rm -f "vscode-tokan-${VERSION}.vsix"
cd .build
zip -qr "../vscode-tokan-${VERSION}.vsix" extension
cd ..
rm -rf .build

echo "Built vscode-tokan-${VERSION}.vsix"
