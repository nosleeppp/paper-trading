#!/bin/bash
# 同步 _version.py 和 pyproject.toml 的版本号
# 用法: bash scripts/sync_version.sh 0.14.5

set -e
VERSION=${1:?请指定版本号, 如: bash scripts/sync_version.sh 0.14.5}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

# _version.py
sed -i '' "s/__version__ = '.*'/__version__ = '$VERSION'/" "$ROOT/src/paper_trading/_version.py"

# pyproject.toml
sed -i '' "s/version = \".*\"/version = \"$VERSION\"/" "$ROOT/pyproject.toml"

echo "版本已同步为 $VERSION"
grep "__version__\|^version" "$ROOT/src/paper_trading/_version.py" "$ROOT/pyproject.toml"
