创建conda虚拟环境:conda create -n llm python=3.10 -y
然后启动环境：conda activate llm

sudo apt update
sudo apt install portaudio19-dev python3-pyaudio   # pyaudio 依赖
sudo apt install build-essential                    # webrtcvad 编译可能需要
sudo apt install libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev libsdl2-ttf-dev   # pygame 依赖
sudo apt install git wget curl
安装依赖：pip install fastapi uvicorn funasr torch torchaudio python-multipart edge_tts pyaudio webrtcvad pygame noisereduce opencv-python  onnxruntime 
cd 到你存放llm的路径下。

doorbell.py是门铃检测系统。依赖要求：pip install tensorflow tensorflow-hub pyaudio numpy
记得去修改一下三个py文件里面的路径

启动edge_tts服务端：python ./chat.py
再开一个进程，重复上述流程，然后启动asr服务端：python ./asr_server.py
运行llama.cpp:python ./run_llama_cpp
启动主程序：python ./fake_stream_asr.py

webrtcvad的代码中要用到旧版本的setuptools，不可以下载最新版本的81.0.2,要下载65.5.0的版本。

