# SHIMEI Video Studio 本地控制台

一个本地 Web UI，用来调用火山方舟 Doubao Seedance 2.0 视频生成接口。界面参考即梦/剪映一类的轻量生成入口：中间输入 Prompt，下面配置模型参数和参考素材，高级 Key 配置默认折叠。

浏览器只访问本机 Python 服务，Ark API Key 和 TOS AK/SK 不会写进前端页面。默认会读取本机 `.seedance_config.json`、环境变量，或你在高级配置里保存的值。

## 启动

首次使用安装依赖：

```powershell
python -m pip install -r requirements.txt
```

启动服务：

```powershell
python app.py
```

打开：

```text
http://127.0.0.1:7860
```

如需换端口：

```powershell
$env:SEEDANCE_UI_PORT=7861
python app.py
```

## 使用流程

1. 启动本地服务并打开 `http://127.0.0.1:7860`。
2. 如果本机已经保存过 Ark/TOS 配置，页面右上角会显示已配置状态，可以直接使用。
3. 在中央输入框填写视频 Prompt。
4. 选择模型、分辨率、比例、时长、Seed 等参数。
5. 可选上传参考素材：图片、参考视频、参考音频会先上传到 TOS，再自动填入公网/预签名 URL。
6. Prompt 里可以写 `图片1`、`图片2`、`参考视频1`、`音频1` 等描述，让模型理解这些素材的用途。
7. 点击 `生成视频`。
8. 页面会自动提交任务、轮询状态，并在成功后下载 MP4 到本机 `outputs/`。
9. 生成成功后可在页面预览、下载，也可以从历史记录重新打开。

## Key 与配置

高级配置默认折叠，只有换 Key、换桶、换区域时需要打开。

支持三种配置方式：

- 在界面高级配置里填写并点击 `保存本机配置`。
- 使用环境变量启动。
- 手动维护本机 `.seedance_config.json`。

仓库内提供两个不含真实密钥的模板：

- `.env.example`
- `.seedance_config.example.json`

Ark Key 环境变量：

```powershell
$env:ARK_API_KEY="ark-..."
python app.py
```

TOS 环境变量：

```powershell
$env:TOS_ACCESSKEY="AKLT..."
$env:TOS_SECRETKEY="..."
$env:TOS_BUCKET="miles"
$env:TOS_REGION="cn-beijing"
$env:TOS_ENDPOINT="tos-cn-beijing.volces.com"
python app.py
```

本项目默认桶名可使用 `miles`，对象前缀默认 `seedance-references`。

也可以在部署机器上通过环境变量生成本机配置文件：

```bash
export ARK_API_KEY="ark-..."
export TOS_ACCESSKEY="AKLT..."
export TOS_SECRETKEY="..."
export TOS_BUCKET="miles"
python scripts/write_local_config.py
```

生成的 `.seedance_config.json` 只保存在当前机器，默认不会提交到 Git。

## TOS 参考素材上传

Ark 需要读取公网可访问的素材 URL。本工具支持把本地素材上传到火山 TOS，再把返回 URL 填入生成请求。

支持上传：

- 图片：JPEG、PNG、WebP，单个不超过 12 MB。
- 视频：MP4、MOV、WebM，单个不超过 300 MB。
- 音频：MP3、WAV、M4A、AAC、OGG，单个不超过 80 MB。

URL 模式：

- `signed`：默认，生成预签名 URL，桶可以保持私有。
- `public`：尝试按公开读对象上传，可配合公开桶、CDN 或自定义域名。

## 支持模型

模型 ID 来自火山方舟 Seedance 2.0 官方 API/SDK 文档整理为固定白名单，不是从实时模型列表接口拉取。

- `doubao-seedance-2-0-260128`：Doubao Seedance 2.0，支持 `480p`、`720p`、`1080p`、`4k`。
- `doubao-seedance-2-0-fast-260128`：Doubao Seedance 2.0 Fast，支持 `480p`、`720p`。

## 参数说明

- `content`：由提示词和参考素材组成。界面当前提供 4 个图片槽、1 个参考视频槽、1 个参考音频槽。
- `duration`：Seedance 2.0 支持 `4-15` 秒，或 `-1`。
- `ratio`：可选 `adaptive`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16`、`21:9`。
- `resolution`：标准版可选 `480p`、`720p`、`1080p`、`4k`；Fast 模型仅支持 `480p`、`720p`。
- `seed`：默认 `-1` 表示随机。
- `generate_audio`：让模型生成同步音频。
- `return_last_frame`：返回尾帧图片，应用会保存为 `.last_frame.png`。
- `watermark`：是否添加水印，默认关闭。
- `tools`：当前界面支持开启 `web_search`。
- `safety_identifier`：可选终端用户标识，最长 64 字符。

## 首帧、尾帧与风格一致性

Ark Seedance 2.0 的接口用 `role: reference_image` 表达图片输入，没有单独的全片设定参数。首帧、尾帧、人物一致性主要靠参考图、上一段尾帧、稳定的人物设定词和分镜 Prompt 来保持。

推荐写法：

```text
首帧为图片1，保持图片1中的同一位亚洲女性医生形象、白大褂、医美诊室暖光风格。参考视频1的镜头运动节奏，音频1作为低音量背景氛围。
```

做多段长视频时，可以开启 `返回尾帧`，把上一段尾帧作为下一段的 `图片1` 上传引用。

## 输出位置

生成文件默认保存到：

```text
outputs/
```

每个视频会保存为 `.mp4`，并配套保存一个同名 `.json` 元数据文件，记录任务 ID、请求体和接口返回信息。如果开启尾帧返回，还会保存 `.last_frame.png`。

## Ubuntu 部署

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ARK_API_KEY="ark-..."
export TOS_ACCESSKEY="AKLT..."
export TOS_SECRETKEY="..."
export TOS_BUCKET="miles"
python app.py
```

默认监听 `127.0.0.1:7860`。如果要公网访问，建议放在 Nginx/Caddy 后面，并给页面加访问控制。

如果只是给局域网其他设备访问同一台后端服务，可以让服务监听所有网卡：

```bash
export SEEDANCE_UI_HOST="0.0.0.0"
export SEEDANCE_UI_PORT="7860"
python app.py
```

然后其他设备访问：

```text
http://运行后端机器的局域网IP:7860
```

注意不要在其他设备打开 `127.0.0.1:7860`，因为 `127.0.0.1` 永远指向那台设备自己。

## 安全说明

- `.seedance_config.json`、`outputs/` 已加入 `.gitignore`。
- 不建议把 Ark API Key 或 TOS AK/SK 写入代码并提交到 Git。
- 即使仓库是 private，也建议在部署机器上用环境变量或本机配置文件管理密钥。
- 如果 Key 曾经暴露在聊天记录、截图或公开仓库中，建议在火山控制台轮换 Key。
