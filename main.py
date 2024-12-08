import io
import json
import os
import threading
import time
import uuid
import wave

import dotenv
import nls
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# 加载环境变量
dotenv.load_dotenv()

URL = "wss://nls-gateway-cn-shanghai.aliyuncs.com/ws/v1"
TOKEN = os.getenv("TOKEN")
APPKEY = os.getenv("APPKEY")

# # 创建AcsClient实例
# client = AcsClient(
#     os.getenv("ALIYUN_AK_ID"), os.getenv("ALIYUN_AK_SECRET"), "cn-shanghai"
# )

# # 创建request，并设置参数。
# request = CommonRequest()
# request.set_method("POST")
# request.set_domain("nls-meta.cn-shanghai.aliyuncs.com")
# request.set_version("2019-02-28")
# request.set_action_name("CreateToken")

# try:
#     response = client.do_action_with_exception(request)
#     print(response)

#     jss = json.loads(response)
#     if "Token" in jss and "Id" in jss["Token"]:
#         token = jss["Token"]["Id"]
#         expireTime = jss["Token"]["ExpireTime"]
#         print("token = " + token)
#         print("expireTime = " + str(expireTime))
# except Exception as e:
#     print(e)

app = FastAPI()


# Midwares
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def pcm_to_wav(pcm_data, sample_rate=16000, channels=1, sampwidth=2):
    """
    将 PCM 数据转换为 WAV 格式
    :param pcm_data: PCM 数据
    :param sample_rate: 采样率（默认16000）
    :param channels: 声道数（默认单声道）
    :param sampwidth: 每个采样的字节数（默认 2 字节，即 16 位）
    :return: 包装后的 WAV 数据流
    """
    # 使用 BytesIO 创建一个内存中的字节流
    wav_io = io.BytesIO()

    # 创建一个 WAV 文件并写入 PCM 数据
    with wave.open(wav_io, "wb") as wav_file:
        wav_file.setnchannels(channels)  # 设置声道数，1 单声道，2 双声道
        wav_file.setsampwidth(sampwidth)  # 设置每个采样的字节数，通常为 2（16 位）
        wav_file.setframerate(sample_rate)  # 设置采样率
        wav_file.writeframes(pcm_data)  # 写入 PCM 数据

    wav_io.seek(0)  # 将文件指针移到文件开始
    return wav_io


class TtsSynthesizer:
    def __init__(self, text):
        self.text = text
        self.pcm_data = io.BytesIO()

    def on_metainfo(self, message, *args):
        print("on_metainfo:", message)

    def on_error(self, message, *args):
        print("on_error:", message)

    def on_close(self, *args):
        print("on_close:", args)

    def on_data(self, data, *args):
        # 接收合成的 PCM 数据并保存到内存中
        self.pcm_data.write(data)

    def on_completed(self, message, *args):
        print("on_completed:", message)

    def synthesize(self):
        tts = nls.NlsSpeechSynthesizer(
            url=URL,
            token=TOKEN,
            appkey=APPKEY,
            on_metainfo=self.on_metainfo,
            on_data=self.on_data,
            on_completed=self.on_completed,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        tts.start(self.text, voice="ailun")  # 可以根据需要修改声音参数


class SpeechRecognizer:
    def __init__(self, audio_data):
        self.audio_data = audio_data
        self.recognized_text = ""

    def on_sentence_begin(self, message, *args):
        print("Sentence begins:", message)

    def on_sentence_end(self, message, *args):
        print("Sentence ends:", message)
        self.recognized_text += json.loads(message)["payload"]["result"]

    def on_start(self, message, *args):
        print("Recognition started:", message)

    def on_error(self, message, *args):
        print("Error:", message)
        # 确保你记录或处理错误信息
        if args:
            print("Error details:", args)

    def on_result_changed(self, message, *args):
        print("Result changed:", message)

    def on_completed(self, message, *args):
        print("Recognition completed:", message)

    def on_close(self, *args):
        print("Connection closed:", args)
        # 处理连接关闭的情况
        if args:
            print("Closure details:", args)

    def start_recognition(self):
        transcriber = nls.NlsSpeechTranscriber(
            url=URL,
            token=TOKEN,
            appkey=APPKEY,
            on_sentence_begin=self.on_sentence_begin,
            on_sentence_end=self.on_sentence_end,
            on_start=self.on_start,
            on_result_changed=self.on_result_changed,
            on_completed=self.on_completed,
            on_error=self.on_error,
            on_close=self.on_close,
        )

        print("Session started...")
        transcriber.start(
            aformat="pcm",
            enable_intermediate_result=True,
            enable_punctuation_prediction=True,
        )

        # 分割 PCM 数据并发送到识别服务
        slice_size = 640  # 每次发送的数据块大小
        slices = zip(*(iter(self.audio_data),) * slice_size)
        for slice_data in slices:
            transcriber.send_audio(bytes(slice_data))
            time.sleep(0.01)

        transcriber.stop()


@app.post("/synthesize")
async def synthesize_text(request: Request):
    data = await request.json()
    content = data["content"]

    if not content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    # 创建语音合成实例并启动线程
    tts_synthesizer = TtsSynthesizer(content)
    thread = threading.Thread(target=tts_synthesizer.synthesize)
    thread.start()

    # 等待语音合成完成
    thread.join()

    if tts_synthesizer.pcm_data is None:
        raise HTTPException(status_code=500, detail="Failed to synthesize speech")

    # 返回 PCM 数据作为流
    wav_stream = pcm_to_wav(tts_synthesizer.pcm_data.getvalue())
    return StreamingResponse(wav_stream, media_type="audio/wav")


@app.post("/recognize")
async def recognize_audio(file: UploadFile = File(...)):
    # 读取 PCM 文件数据
    audio_data = await file.read()

    if not audio_data:
        raise HTTPException(status_code=400, detail="Audio data is empty")

    # # 随机生成一个文件名并保存音频数据到 tests 目录
    # file_name = f"{uuid.uuid4().hex}.pcm"  # 使用 UUID 生成唯一文件名
    # file_path = os.path.join("tests", file_name)

    # # 保存音频数据到文件
    # with open(file_path, "wb") as f:
    #     f.write(audio_data)

    # 开始语音识别
    recognizer = SpeechRecognizer(audio_data)
    recognition_thread = threading.Thread(target=recognizer.start_recognition)
    recognition_thread.start()

    # 等待识别完成
    recognition_thread.join()

    print(recognizer.recognized_text)
    # 如果没有识别到内容，返回错误
    if not recognizer.recognized_text:
        raise HTTPException(status_code=500, detail="Failed to recognize speech")

    # 返回识别的文本
    return JSONResponse(content={"content": recognizer.recognized_text})


# 运行 FastAPI 应用
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
