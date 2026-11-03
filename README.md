# 一级练习室

面向计算机一级 B 选择题训练的轻量 Web 应用。Flask + SQLite + 原生 JavaScript，单个 Docker 容器，无需 Node 构建、Redis 或独立数据库。

## 已实现

- 管理员、班主任、学生三种角色；班主任仅能管理本人班级，管理员可查看所有班级。
- 新建班级时创建专属班主任账号；单独添加学生或 Excel 批量导入。
- 随机初始密码、首次登录强制改密、教师重置密码、改密后旧会话失效、CSRF 防护、登录失败限流、操作记录。
- 10 个分类：从原始 Word 识别 520 条记录，完全相同记录去重后实际可用 484 题。保留原始答案与解析，不自动纠正内容。
- 分类随机训练、全题库随机训练，题量可设 1–100。提交整组题目后显示解析。
- 班级每日练习：随机固定组题或手选题，设置开放时间和达标率（默认100%）。可重复做，必须单次达到设定正确率；一旦达标，后续低分不会撤销完成状态。
- 自动错题归档，再次答对标记已掌握，再次出错重新纳入复习。
- 教师进度页每10秒更新，包含未开始、作答中、待达标、已达标，支持 CSV 导出。
- 模拟考试：服务器计时、单次作答、答题自动保存、超时以已保存答案交卷，统一结束前不公开解析或学生排名；按正确数、用时排名。
- 手机与桌面自适应界面、在线一致性备份脚本。

## Docker 部署

服务器安装 Docker Engine 与 Compose 插件后：

```bash
git clone https://github.com/warmingkkk/ncre-practice.git
cd ncre-practice
cp .env.example .env
```

编辑 `.env`：把 `ADMIN_PASSWORD` 设置为自己的强密码（至少10位）。默认监听 `127.0.0.1:8080`，适合在前面放已有 Nginx / Caddy。然后：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 web
```

登录账号为 `admin`，初始密码为 `.env` 中的 `ADMIN_PASSWORD`。首次登录必须更改。环境变量只在首次创建管理员时生效，后续修改环境变量不会重置密码。

内网临时访问可把 `BIND_ADDRESS` 改成 `0.0.0.0`，用 `http://服务器IP:8080` 访问。正式给学生使用时配置域名和 HTTPS，把 `COOKIE_SECURE=1` 后重建服务：

```bash
docker compose up -d
```

已有 Caddy 可采用下面的站点配置（替换域名）：

```caddyfile
practice.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

容器默认非 root、应用内存上限512MB，数据保存在 Docker 命名卷 `practice-data`，日志轮转。镜像升级不会清空数据。不要执行 `docker compose down -v`，该命令会删除数据卷。

## 第一次使用

1. 管理员新建班级，填写班主任姓名和登录账号；保存页面上一次性显示的初始密码。
2. 管理员或班主任进入班级，上传学生名单。**兼容当前目录文件的两列“学号、姓名”**，班级由上传位置确定。也支持“班级、学号、姓名”三列，班级名需完全匹配当前班级。多班级名单请按班拆分。
3. 学生账号为学号，初始密码随机生成，导入完成页可打印/保存 PDF。已经存在的同班学号跳过，不覆盖密码。任何一条发生冲突则整批回滚。
4. 在班级发布任务，可从分类随机抽题，也可展开手动选题；全班使用相同的一组题目。
5. 学生登录，首次改密后进入任务。每日练习可反复做；教师打开完成情况页即可持续查看。

Excel 学号建议存为文本，避免 Excel 在保存前就丢失前导零。名单、密码、生产数据库均在 `.gitignore` / `.dockerignore` 中排除。当前目录42人真实名单已验证能解析，没有上传 GitHub，也没有写入演示数据库。

## 多人使用与资源

8 个 Waitress 工作线程 + SQLite WAL + 索引和短事务。每题选择在700毫秒后合并保存，无 WebSocket 长连接、无学生端轮询。教师查看的班级页面每10秒查询一次。

本地 Windows 隔离 HTTP 压测：300 名虚拟学生同时进入，完成读题、保存、交卷与概览查询，共1,800个请求，300份满分答卷全部入库，零错误。第一轮约9.71秒完成，P95约1.92秒。最新结果见 `load-test-result.json`。此测试使用预签名会话，**不包含同时登录时的密码哈希、TLS和公网延迟，也不是2GB服务器实测**。2GB/1核服务器上线后应在目标机器复跑压测；512MB是配置上限，不是测得的内存峰值。

SQLite WAL 支持读写并行，但写事务仍串行，数据库卷需要本机磁盘，不要放在网络文件系统。见 [SQLite 官方说明](https://www.sqlite.org/wal.html)。应用通过生产 WSGI 服务运行，见 [Flask 部署说明](https://flask.palletsprojects.com/en/stable/deploying/)。

本应用适合几百人班级训练。若未来持续高频并发写入、扩展多台应用实例，可迁移 PostgreSQL，当前无需先部署额外数据库。

## 备份与恢复

在线备份会把 WAL 中已经提交的数据一并写入一致性快照：

```bash
docker compose exec web python scripts/backup.py
docker compose cp web:/app/data/backup-实际时间.sqlite3 ./backup.sqlite3
```

备份文件包含学生和成绩，请另存到私有位置。建议服务器每天执行一次，并将副本复制到另一台设备。

恢复前先停机，并保留当前数据卷备份。把要恢复的文件复制进已停止的容器，移除旧 WAL / SHM，再启动：

```bash
docker compose stop web
docker compose cp ./backup.sqlite3 web:/app/data/restore.sqlite3
docker compose run --rm --user root web python -c "from pathlib import Path; import os; p=Path('/app/data'); (p/'restore.sqlite3').replace(p/'practice.db'); [(p/n).unlink(missing_ok=True) for n in ['practice.db-wal','practice.db-shm']]; os.chown(p/'practice.db',10001,10001)"
docker compose up -d
```

`secret` 文件也保存在数据卷中，恢复整个卷时一起保存。仅恢复数据库时现有 secret 可继续使用。升级代码前先备份，然后 `git pull`、`docker compose up -d --build`。

## 开发与验证

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install pytest
python -m pytest -q
python scripts/load_test.py --users 300
python app.py
```

本地直接运行会自动生成管理员初始密码并写到 `data/initial-admin-password.txt`。Docker 部署使用 `.env` 中的密码。

纯虚拟数据演示：`python scripts/preview.py`，访问 `http://127.0.0.1:8081`，账号 `admin` / `teacher` / `student`，演示密码均为 `DemoPractice2026`。此脚本只监听本机并使用临时数据库，不能用作正式服务器启动方式。

重新解析原题：`python scripts/extract_bank.py /path/to/Word题库目录`，结果写入 `seed/questions.json` 与检查报告。已有数据库不会自动覆盖，正式站新增分类使用管理员的 Word 导入功能。支持每题“题干、A./B./C./D.、答案：A、解析：…”的结构；有无法解析题目时导入整批失败，不静默跳过。

## 边界说明

- 当前交付是选择题训练，未包含 Office 操作题、官方考试对接、短信、家长端或严格监考。
- 题目来自提供的资料，其中包含较早期技术表述，发布前可由教师在题库中搜索预览核对；本版不提供在线改题，以免影响正在作答的试卷。
- 超时由服务器强制判断；浏览器打开时自动提交，关闭页面时由下次访问试卷或教师查看进度触发结算，用时记到截止时刻。截止后到达的新答案不再接收。断网期间未成功保存的答案无法恢复。
- 进度采用10秒轮询；并非毫秒级推送。练习记录保留在数据库，学生主页提供汇总与错题，未提供完整历史记录筛选页。
- 本机无 Docker Engine，因此本地没有执行镜像构建。仓库的 CI 配置在 GitHub 上运行功能测试、构建镜像和容器健康检查；需以实际 Actions 结果为准。
