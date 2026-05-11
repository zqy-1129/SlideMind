# SlideMind

滑坡智能问答系统首版工程骨架，采用 Python 后端、MongoDB、Neo4j、Milvus 和 Vue 前端。

## 功能

- 上传 `csv`、`xlsx`、`txt`、`docx`、`pdf` 数据文件
- 表格和文本数据入库到 MongoDB
- 在页面中按数据集查看已入库的表格记录、文本资料和文本切片
- 数据管理页支持删除数据集、删除当前类别数据、删除单个导入任务及其关联数据
- 智能分析页集中提供知识图谱生成和问答
- 文本切片和简易向量写入 Milvus
- 从结构化数据构建 Neo4j 基础知识图谱
- 提供数据管理、图谱查询、智能问答 API
- Vue 前端提供上传、数据集、图谱和问答界面

## 本地启动

复制环境变量：

```bash
cp .env.example .env
```

使用 Docker Compose 启动：

```bash
docker compose up --build
```

如果构建时报 `failed to fetch oauth token` 或连接 `auth.docker.io` 超时，说明 Docker 无法访问 Docker Hub。可以在 `.env` 中把 `PYTHON_IMAGE`、`NODE_IMAGE`、`MONGO_IMAGE` 等变量改成你当前网络可访问的镜像源或内网仓库地址，然后再次执行：

```bash
docker compose pull
docker compose up --build
```

在国内网络环境下，可以先尝试预置镜像配置：

```powershell
.\scripts\use-cn-docker-mirror.ps1
docker compose pull
docker compose up --build
```

确认是否生效：

```powershell
docker compose config | Select-String "daocloud|docker.io"
```

如果输出里仍然是 `docker.io/library/python` 或 `docker.io/library/node`，说明 `.env` 没有被当前命令读取，请确认命令是在项目根目录执行。

也可以在 Docker Desktop 中配置公司代理或 registry mirror。若命令同时提示无法读取 `C:\Users\zqy\.docker\config.json`，请用当前登录用户打开 Docker Desktop，或修复该文件权限后再运行。

服务地址：

- 前端：http://localhost:5173
- 后端 API：http://localhost:8000
- API 文档：http://localhost:8000/docs
- Neo4j Browser：http://localhost:7474
- Milvus：localhost:19530
- MongoDB：localhost:27017

## 后端开发

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## 前端开发

```bash
cd frontend
npm install
npm run dev
```

在 Windows PowerShell 执行策略限制下，可使用 `npm.cmd install` 和 `npm.cmd run dev`。

## 数据流

1. 前端上传文件到 `/api/imports`
2. 后端保存上传元数据到 MongoDB
3. Celery worker 解析文件并写入 MongoDB
4. 文本片段向量化后写入 Milvus
5. 图谱构建任务从 MongoDB 读取数据并写入 Neo4j
6. 问答服务按问题类型联合查询 MongoDB、Neo4j、Milvus
