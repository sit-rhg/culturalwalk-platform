"""把后端 + 构建好的前端 + 依赖打成一个免安装目录（Windows x64）。

产出一个 zip：解压后双击 start.bat 就能跑，不需要 Node、不需要 pip、不需要 Postgres。
CI 里由 .github/workflows/release.yml 调用，并把 zip 挂到 GitHub Release。

用法：

    # 本机试打（不含 Python 运行时，start.bat 会退回 py -3.12 / python）
    python scripts/package_windows.py

    # CI：把 windows-latest 上的 Python 目录一起带上，用户机器什么都不用装
    python scripts/package_windows.py --python-dir "C:/hostedtoolcache/windows/Python/3.12.x/x64" \
        --version 0.1.0
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = REPO_ROOT / "services" / "api"
WEB_ROOT = REPO_ROOT / "apps" / "web"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def _find_pnpm() -> list[str]:
    """返回执行 pnpm 的命令前缀（本机没装 pnpm 时用 node 跑它的 cjs）。"""
    direct = shutil.which("pnpm")
    if direct:
        return [direct]
    node = shutil.which("node")
    candidates = [
        Path.home() / "AppData/Local/pnpm/pnpm.cjs",
        Path.home() / ".dsh/dsh-runtimes/dsh-primary-runtime/dependencies/pnpm/bin/pnpm.cjs",
    ]
    if node:
        for candidate in candidates:
            if candidate.is_file():
                return [node, str(candidate)]
    raise SystemExit(
        "找不到 pnpm：先 pnpm install，或把 pnpm 加进 PATH（CI 用 pnpm/action-setup）"
    )


def _build_web(skip: bool) -> None:
    index = WEB_ROOT / "dist" / "index.html"
    if skip and index.is_file():
        print(f"[web] 复用已有构建产物：{index}")
        return
    command = [*_find_pnpm(), "build"]
    print(f"[web] {' '.join(command)}")
    subprocess.run(command, cwd=WEB_ROOT, check=True)


def _install_deps(python: str, target: Path) -> None:
    """把 pyproject 里的运行依赖装进 target；项目本身由我们手工拷 app/。"""
    target.mkdir(parents=True, exist_ok=True)
    command = [
        python,
        "-m",
        "pip",
        "install",
        "--no-compile",
        "--upgrade",
        "--target",
        str(target),
        ".",
    ]
    print(f"[deps] {' '.join(command)}")
    subprocess.run(command, cwd=API_ROOT, check=True)
    # pip 会把 app 自己也装进 target（setuptools 找到的就是它）；留着会与根目录
    # 的 app/ 争 sys.path 顺序，删掉，统一用仓库里那份源码。
    installed_app = target / "app"
    if installed_app.is_dir():
        shutil.rmtree(installed_app)


def _copy_tree(source: Path, target: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in SKIP_DIRS or Path(name).suffix in SKIP_SUFFIXES}

    shutil.copytree(source, target, ignore=ignore)


def _write_readme(staging: Path, version: str) -> None:
    text = f"""文化行走平台 · 免安装版 {version}
========================================

1. 解压到一个**没有中文和空格也可以**的目录（路径长一点也没关系）。
2. 双击 start.bat。
3. 浏览器打开 http://127.0.0.1:8000 ，用演示账号登录：

   demo@demo.local     / Demo12345!   普通用户（看路线、排行程）
   editor@demo.local   / Demo12345!   投稿与创作
   reviewer@demo.local / Demo12345!   审核与发布

第一次启动会自动建库并灌演示数据（约 10 秒），之后直接起服务。
关掉黑窗口即停止；数据在 data\\ 目录，删掉 data\\demo.db 就是全新环境。

接口文档：http://127.0.0.1:8000/docs
"""
    (staging / "README.txt").write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="打 Windows 免安装包")
    parser.add_argument("--version", default="0.1.0", help="版本号，进文件名")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "release"), help="zip 输出目录")
    parser.add_argument(
        "--python-dir",
        default="",
        help="要一起打包的 Python 目录（含 python.exe）；留空则要求用户机器已装 Python",
    )
    parser.add_argument("--skip-web-build", action="store_true", help="复用 apps/web/dist")
    parser.add_argument("--skip-deps", action="store_true", help="跳过 pip install（本机试打）")
    args = parser.parse_args()

    name = f"CulturalWalk-{args.version}-windows-x64"
    # 中间目录放系统临时目录：留在仓库里会被 ruff/pytest 扫到，也容易被误提交。
    build_root = Path(tempfile.mkdtemp(prefix="culturalwalk-package-"))
    staging = build_root / name
    staging.mkdir(parents=True)

    _build_web(args.skip_web_build)

    if not args.skip_deps:
        _install_deps(sys.executable, staging / "site-packages")
    else:
        (staging / "site-packages").mkdir()

    print("[copy] 后端与迁移")
    _copy_tree(API_ROOT / "app", staging / "app")
    _copy_tree(API_ROOT / "migrations", staging / "migrations")
    shutil.copy2(API_ROOT / "alembic.ini", staging / "alembic.ini")
    (staging / "scripts").mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "seed_demo.py", staging / "scripts" / "seed_demo.py")

    print("[copy] 前端构建产物")
    _copy_tree(WEB_ROOT / "dist", staging / "web")
    # 打包后由后端托管，不该再去拉 vite 的 index（它引用的是开发地址）。
    for junk in ("vite.svg",):
        candidate = staging / "web" / junk
        if candidate.exists():
            candidate.unlink()

    start_bat = Path(__file__).with_name("release") / "start.bat"
    if not start_bat.is_file():
        raise SystemExit(f"缺少启动脚本模板：{start_bat}")
    shutil.copy2(start_bat, staging / "start.bat")
    shutil.copy2(start_bat.with_name("requirements.txt"), staging / "requirements.txt")

    if args.python_dir:
        runtime_dir = Path(args.python_dir)
        if not (runtime_dir / "python.exe").is_file():
            raise SystemExit(f"{runtime_dir} 里没有 python.exe，不是可用的 Python 目录")
        print(f"[copy] Python 运行时：{runtime_dir}")
        _copy_tree(runtime_dir, staging / "runtime")

    _write_readme(staging, args.version)

    args.out_dir_path = Path(args.out_dir)
    args.out_dir_path.mkdir(parents=True, exist_ok=True)
    zip_path = args.out_dir_path / f"{name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                archive.write(path, f"{name}/{path.relative_to(staging).as_posix()}")

    size_mb = zip_path.stat().st_size / 1024 / 1024
    file_count = sum(1 for path in staging.rglob("*") if path.is_file())
    shutil.rmtree(build_root, ignore_errors=True)
    print(f"[done] {zip_path}（{size_mb:.1f} MB，{file_count} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
