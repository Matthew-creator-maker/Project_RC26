#!/usr/bin/env bash
# CPU 基线安装；新建项目内虚拟环境，不改系统 Python/现有 ROS 或 CUDA 环境。
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
source /etc/os-release
if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 22.04 ]]; then
  echo "此脚本针对 Ubuntu 22.04。其他系统请按 README 手动安装。" >&2
  exit 1
fi
arch="$(uname -m)"
if [[ "$arch" != x86_64 && "$arch" != aarch64 ]]; then
  echo "未验证的架构: $arch，需要 64 位 x86_64 或 aarch64。" >&2
  exit 1
fi
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip libgl1 libglib2.0-0 libusb-1.0-0 fonts-noto-cjk
python3 -c 'import sys; assert sys.version_info[:2] == (3, 10), "请使用 Ubuntu 22.04 的 Python 3.10"'
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
if [[ "$arch" == x86_64 ]]; then
  .venv/bin/python -m pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cpu
else
  # ARM CPU 版本；如需 Jetson CUDA，另按 JetPack 对应版本配置，勿混装。
  .venv/bin/python -m pip install torch==2.6.0 torchvision==0.21.0
fi
.venv/bin/python -m pip install -r requirements-ubuntu22.txt
.venv/bin/python -m pip check
echo "依赖安装完成。请先填写收拢位/工作空间，再运行："
echo "source .venv/bin/activate"
echo "python tools/check_environment.py --hardware"
echo "RealSense 的系统驱动及 USB udev 权限另见 README；此脚本不更改内核。"
