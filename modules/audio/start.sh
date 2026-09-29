#!/bin/bash
# 启动脚本 - 用于 Linux

conda activate llm || { echo "Failed to activate conda env"; exit 1; }

# 进入项目目录
cd /home/luo/桌面/api || { echo "Directory not found"; exit 1; }

# 定义清理函数，在脚本退出时杀死所有后台进程
cleanup() {
    echo "Stopping all services..."
    kill $ASR_PID $TTS_PID $LLM_PID 2>/dev/null
    wait $ASR_PID $TTS_PID $LLM_PID 2>/dev/null
    echo "All services stopped."
    exit 0
}

# 捕获退出信号（Ctrl+C、终止信号等）
trap cleanup SIGINT SIGTERM

# 启动 TTS
echo "Starting TTS server..."
python chat.py &
TTS_PID=$!

sleep 2

# 启动 词汇提取
echo "Starting ASR server..."
python asr_server_sensesmall.py &
ASR_PID=$!

sleep 10

# 等待所有后台进程（脚本将常驻，直到用户中断）
echo "All services started. Press Ctrl+C to stop."
wait
