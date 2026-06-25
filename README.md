# 即梦视频生成本地控制台

本项目提供一个本地 Web UI，用于调用火山引擎即梦视频生成接口。密钥只在本机后端用于签名请求，不会放进浏览器直连第三方。

## 启动

```powershell
python app.py
```

打开：

```text
http://127.0.0.1:7860
```

## Key 文件导入格式

支持 `.txt`、`.env`、`.json`，常见写法都可以：

```text
AccessKeyID: AKLT...
SecretAccessKey: ...
```

```text
VOLC_ACCESSKEY=AKLT...
VOLC_SECRETKEY=...
```

```json
{
  "access_key": "AKLT...",
  "secret_key": "..."
}
```

导入后会先填入页面输入框。勾选“保存到本机配置”并点击“保存配置”后，会写入本机 `.jimeng_config.json`。

## 支持模型

模型列表根据火山引擎即梦视频生成接口文档整理为固定 `req_key` 清单，不是从实时模型列表接口获取。

- 3.0 Pro 1080P：文生视频 / 首帧图生视频
- 3.0 1080P / 720P 文生视频
- 3.0 1080P / 720P 首帧图生视频
- 3.0 1080P / 720P 首尾帧图生视频
- 3.0 720P 运镜图生视频
