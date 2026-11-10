# -*- coding: utf-8 -*-
"""打包 jrxy-auto-sign 为可迁移的 zip

只打包源码 + 启动脚本，不含 venv（虚拟环境里有绝对路径，换机器不可用）
和运行数据（账号密码、照片、日志）。

用法：
    python scripts/build_package.py
    python scripts/build_package.py --out D:\\somewhere
    python scripts/build_package.py --with-accounts   # 连账号配置一起打包（含密码，慎用）
"""
import argparse
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_NAME = "jrxy-auto-sign"

# 排除规则
EXCLUDE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".idea", ".vscode",
    "node_modules", "deviceid_capture", ".pytest_cache",
}
EXCLUDE_DIRS_DEFAULT = {
    "accounts",          # 含学号密码，默认不打包
    "data",              # web-server/data 含日志/照片/Key
}
EXCLUDE_FILES_SUFFIX = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".bak", ".tmp"}
EXCLUDE_FILES_EXACT = {
    "config.yml", "device_id.txt", ".device_id", "amap.json",
    "schedule.json", "web_token.txt", "部署记录.md",
}

# 需要保留的空目录（运行时自动创建，但目录结构要留）
KEEP_EMPTY = ["accounts", "web-server/data"]


def should_skip(path: Path, rel: Path, with_accounts: bool) -> bool:
    parts = set(rel.parts[:-1])
    if parts & EXCLUDE_DIRS:
        return True
    if not with_accounts and parts & EXCLUDE_DIRS_DEFAULT:
        return True
    if rel.name in EXCLUDE_FILES_EXACT:
        return True
    if rel.suffix.lower() in EXCLUDE_FILES_SUFFIX:
        return True
    # 签到照片（可能含个人图像）
    if rel.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        # docs 里的截图可以留
        if "docs" not in rel.parts:
            return True
    return False


def collect(with_accounts: bool):
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        d = Path(dirpath)
        # 就地裁剪目录，避免走入大目录
        dirnames[:] = [
            n for n in dirnames
            if n not in EXCLUDE_DIRS
            and (with_accounts or n not in EXCLUDE_DIRS_DEFAULT)
        ]
        for fn in filenames:
            p = d / fn
            try:
                rel = p.relative_to(ROOT)
            except ValueError:
                continue
            if should_skip(p, rel, with_accounts):
                continue
            files.append((p, rel))
    return files


def main():
    ap = argparse.ArgumentParser(description="打包为可迁移 zip")
    ap.add_argument("--out", default=str(ROOT.parent), help="输出目录")
    ap.add_argument("--with-accounts", action="store_true",
                    help="连 accounts/ 一起打包（含学号密码，仅自己用）")
    args = ap.parse_args()

    files = collect(args.with_accounts)
    total = sum(p.stat().st_size for p, _ in files)

    stamp = datetime.now().strftime("%Y%m%d")
    tag = "_含账号" if args.with_accounts else ""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / ("%s%s_%s.zip" % (PKG_NAME, tag, stamp))

    print("打包 %d 个文件，共 %.2f MB" % (len(files), total / 1024 / 1024))
    print("输出: %s" % out_path)
    print()

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        # 统一放在 PKG_NAME/ 下，解压后是一个完整文件夹
        for p, rel in sorted(files, key=lambda x: str(x[1])):
            arc = str(Path(PKG_NAME) / rel).replace("\\", "/")
            z.write(p, arc)
        # 保持空目录结构
        for d in KEEP_EMPTY:
            if not any(rel.parts[:len(Path(d).parts)] == Path(d).parts for _, rel in files):
                z.writestr(str(Path(PKG_NAME) / d).replace("\\", "/") + "/.keep", "")

    size = out_path.stat().st_size
    print("完成！zip 大小 %.2f MB" % (size / 1024 / 1024))
    print()
    print("=" * 60)
    print("另一台电脑上使用步骤：")
    print("=" * 60)
    print("  1. 解压 zip")
    print("  2. 安装 Python 3.10（勾选 Add to PATH）")
    print("     https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe")
    print("  3. 双击 一键安装.bat")
    print("  4. 双击 启动.bat")
    print()
    if not args.with_accounts:
        print("注意：本次未包含 accounts/（账号数据）。")
        print("      新机器上需要在网页里重新添加账号。")
        print("      想连账号一起带走，用 --with-accounts 重新打包。")


if __name__ == "__main__":
    main()
