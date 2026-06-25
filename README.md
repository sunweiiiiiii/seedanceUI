# Seedance 2.0 本地控制台

本项目提供一个本地 Web UI，用于调用火山方舟 Ark 的 Doubao Seedance 2.0 视频生成接口。浏览器只和本机 Python 后端通信，Ark API Key 不会写进前端代码。

## 启动

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

## 完整使用流程

1. 启动本地服务并打开 `http://127.0.0.1:7860`。
2. 展开左侧 `Key 配置`。
3. 手动填写 Ark API Key，或导入 Key 文件。
4. 如需长期使用，勾选 `保存到本机配置` 后点击 `保存配置`。未勾选时 Key 只保存在本次后端进程中。
5. 选择 `Doubao Seedance 2.0` 或 `Doubao Seedance 2.0 Fast`。
6. 设置时长、分辨率、比例和高级参数。
7. 在中间区域选择 `纯文本`、`参考图` 或 `多模态`。
8. 填写提示词；参考图、参考视频、参考音频需要填写公网可访问 URL。
9. 点击 `生成视频`。
10. 页面会自动提交任务、轮询状态，并在成功后下载 MP4 到本机。
11. 生成成功后可在右侧预览、下载，也可以从历史记录重新打开。

## 接口调用流程

应用内部使用火山方舟异步视频生成流程：

1. 使用 Ark API Key 作为 Bearer Token。
2. 创建视频生成任务：

```text
POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks
Authorization: Bearer <ARK_API_KEY>
```

3. 获取返回的 `id`。
4. 查询任务状态：

```text
GET https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{id}
Authorization: Bearer <ARK_API_KEY>
```

5. 当 `status` 为 `succeeded` 且返回 `content.video_url` 时，应用会立即下载为本地 `.mp4`。

生成结果 URL 通常有时效限制，因此应用会在任务完成后自动转存到 `outputs/`。

## Key 文件导入格式

支持 `.txt`、`.env`、`.json`，常见写法都可以：

```text
ARK_API_KEY=ark-...
```

```text
Authorization: Bearer ark-...
```

```json
{
  "api_key": "ark-..."
}
```

导入后会先填入页面输入框。勾选 `保存到本机配置` 并点击 `保存配置` 后，会写入本机 `.seedance_config.json`。

也可以通过环境变量启动：

```powershell
$env:ARK_API_KEY="ark-..."
python app.py
```

## 支持模型

模型 ID 来自火山方舟 Seedance 2.0 官方 API/SDK 文档整理为固定白名单，不是从实时模型列表接口拉取。

- `doubao-seedance-2-0-260128`：Doubao Seedance 2.0，支持 `480p`、`720p`、`1080p`。
- `doubao-seedance-2-0-fast-260128`：Doubao Seedance 2.0 Fast，支持 `480p`、`720p`，不支持 `1080p`。

## 参数说明

- `content`：由提示词和参考素材组成，最多 5 项。包含提示词时，最多再放 4 个参考素材。
- `duration`：Seedance 2.0 支持 `4-15` 秒，或 `-1`。
- `ratio`：可选 `adaptive`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16`、`21:9`。
- `resolution`：可选 `480p`、`720p`、`1080p`；Fast 模型不支持 `1080p`。
- `seed`：默认 `-1` 表示随机。
- `generate_audio`：让模型生成同步音频。
- `return_last_frame`：返回尾帧图片，应用会保存为 `.last_frame.png`。
- `watermark`：是否添加水印，默认关闭。
- `tools`：当前界面支持开启 `web_search`。
- `safety_identifier`：可选终端用户标识，最长 64 字符。

## 参考图与首帧

Ark Seedance 2.0 的接口文档使用 `role: reference_image` 表达图片输入，没有单独的 `first_frame` 或 `last_frame` 参数。界面里的 `参考图 / 首帧 / 首尾帧` 是组织提示词和输入的模式切换，实际请求仍会把图片 URL 放入 `content` 的 `reference_image`。

如果需要首尾帧效果，建议在提示词中明确写：

```text
首帧为图片1，尾帧定格为图片2。
```

本地上传文件不能直接交给 Ark 读取；参考素材必须是公网可访问 URL。可以先上传到 TOS、OSS、COS 或其他静态文件服务，再把 URL 填入界面。

## 输出位置

生成文件默认保存到：

```text
outputs/
```

每个视频会保存为 `.mp4`，并配套保存一个同名 `.json` 元数据文件，记录任务 ID、请求体和接口返回信息。如果开启尾帧返回，还会保存 `.last_frame.png`。

## 安全说明

- 不建议把 Ark API Key 写入代码或提交到 Git。
- `.seedance_config.json`、`outputs/`、临时文档目录已加入 `.gitignore`。
- 如果 Key 曾经暴露在聊天记录、截图或公开仓库中，建议在火山方舟控制台轮换 Key。
