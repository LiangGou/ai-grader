# AI 判卷（AI Grader）

给老师用的 AI 判卷 / 判作业 Web App（MVP）。

**核心流程**：手机拍学生试卷/作业照片 → 老师用**语音**说出判分规则（不用打字）→
语音自动整理成**结构化 rubric**（可手动修改）→ 调用 **OpenAI API（GPT-4o 看图）**
按 rubric 逐题判分 → 自动统计总分 → 全班成绩统计 + 导出 CSV。

- UI 中文，移动端优先（老师用手机拍照上传）。
- 技术栈：Python + Flask + SQLite（标准库），vanilla HTML/CSS/JS（无构建步骤）。

---

## 功能

1. **班级 / 任务管理**：创建班级 → 在班级下创建判卷任务（作业/试卷名称）。
2. **Rubric 录入**（任务设置页）：
   - 🎤 语音录入：Web Speech API（`zh-CN`）实时转写进"原始记录"文本框（需 Chrome/Edge）；
   - ✨ 生成结构化 Rubric：后端调 OpenAI 把口语记录整理成逐题「题号 / 满分 / 得分规则」JSON；
   - 结构化表格可直接编辑：增删行、改题号/满分/规则；
   - 也支持纯文本模式：跳过语音，直接打字写 rubric；
   - 可选上传参考答案/标准答案照片，判分时一并作为上下文。
3. **拍照判卷**：输入学生姓名（支持语音）→ 多张答卷照片上传（移动端直接调相机）
   → 后端把 rubric + 参考答案图 + 答卷图发给 GPT-4o，要求输出严格 JSON
   （每题：识别到的答案简述、得分、满分、给分理由；总分；整体评语）→
   老师可手动修改每题分数后保存入库。
4. **成绩统计**：全班成绩表、平均分/最高/最低、分数段分布直方图（纯 CSS/JS）、导出 CSV。
5. **设置页**：填写 OpenAI API Key（写入本机 `.env`），模型名可配置（默认 `gpt-4o`）。

## 安装与运行

```bash
cd ai-grader
./run.sh
# 或手动：
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --host=0.0.0.0 --port=5000
```

浏览器打开 http://localhost:5000。

## 配置 OpenAI Key

1. 打开 https://platform.openai.com 登录，进入 **API keys** 页面；
2. 点 **Create new secret key**，复制生成的 key（以 `sk-` 开头，只显示一次）；
3. 在本应用「设置」页粘贴保存（或复制 `.env.example` 为 `.env` 后填入）。

建议在 OpenAI 后台给 key 设置用量限额（Limits），避免意外超支。
模型通过环境变量 `OPENAI_MODEL` 配置，默认 `gpt-4o`（看图判分质量优先）。

> 🔒 安全：Key 只保存在服务器本机 `.env` 文件中；`.env` 已在 `.gitignore` 中，
> 不会提交到 git；请求只发往 `api.openai.com`。

## Replit 部署备注

1. 把本目录代码上传到 Replit（可直接拖 zip / 从 GitHub import）；
2. 在 Replit 的 **Secrets**（环境变量）里添加 `OPENAI_API_KEY`（和可选的 `OPENAI_MODEL`），
   不要把 key 写进代码；
3. Replit 会自动识别 `requirements.txt` 安装依赖，Run 命令用 `python app.py`
   或 `flask --app app run --host=0.0.0.0 --port=5000`；
4. 数据存在 `ai_grader.db`（SQLite 单文件），答卷照片存在 `uploads/`，
   Replit 重启/休眠可能丢失文件，正式使用建议定期导出 CSV 备份。

## 目录结构

```
ai-grader/
  app.py            Flask 路由（页面 + API）
  grader.py         调 OpenAI API：rubric 结构化、看图判分
  db.py             SQLite 建表 + CRUD（标准库 sqlite3）
  templates/        base / index / assignment / grade / results / settings
  static/           style.css（移动端优先）/ app.js（语音、拍照、判分交互）
  uploads/          上传的答卷/参考答案照片（git 忽略）
  requirements.txt  flask / openai / python-dotenv
  .env.example      环境变量模板（占位符）
  run.sh            一键启动
```

## 已知限制（MVP）

- 语音转写依赖浏览器 Web Speech API（Chrome/Edge 最稳，Safari 支持有限）；
- 超大照片可能超出模型单图大小限制，拍照时尽量拍清晰但不过度放大；
- SQLite 单文件，多老师并发量大时建议换 Postgres。
