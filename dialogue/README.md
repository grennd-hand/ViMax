# ViMax 群聊剧本模式

母本坐席内容确定对话目标、必要说明和原始业务信息，原账号无需沿用。重新设置参与者、角色卡、语言和总条数，生成后按原九列导出 Excel / JSON。

本模式移入用户原有 `ai-script-dialogue-generator` 的导入、人物记忆、校验功能；生成器改为 ViMax `Screenwriter` 的故事规划和剧本写作。界面直接集成到 ViMax 原工作台，共用原导航、项目列表、设置与文件视图，原工作台已中文化。独立数据库在 `.vimax/dialogue/`，原应用及其数据库不参与新模式运行。

## 本地运行

在 ViMax 根目录安装 Python 3.12 及依赖：

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv/Scripts/python.exe -r dialogue/backend/requirements.txt
npm ci --prefix web
npm run build --prefix web
```

设置 `MODEL_API_BASE_URL`、`MODEL_API_KEY` 环境变量，或在启动时指定接口地址与 Windows DPAPI 密钥文件：

```powershell
./scripts/start-dialogue.ps1 -BaseUrl https://your-provider.example/v1 -KeyFile C:/private/model-key.dpapi
# 另开一个终端
npm start --prefix web
```

访问 `http://127.0.0.1:4173`，在原工作台选择“群聊剧本”。群聊后端默认端口9014；可通过 `VIMAX_DIALOGUE_URL` 调整 Web 代理目标。只有一套 Web 界面；后台接口由原 Web 服务代理。访问 `/dialogue-api/health` 可检查服务是否已配置模型。

## 生成与校验

ViMax 先依据坐席母本和确认目标生成事件线，再生成结构化群聊草稿；条数、回复编号、身份与母本原值经过校验，随后通过各角色独立会话广播并保存。整段复核检查目标实际完成、人物选择和重复内容，根据问题修订后重新复核。失败保留草稿及已接受消息，可继续或重试，不显示为成功。

新模式保留角色知识库、独立人物记忆和公开原话索引。新参与者无需全部发言；验收以已确认内容目标为准。模型运行和导出均在本地，不操作外部业务网站或聊天平台。
