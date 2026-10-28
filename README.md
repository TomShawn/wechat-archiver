# wechat-archiver

本地微信公众号文章归档工具（Python 命令行程序）。根据用户提供的微信公众号文章 URL、本地 HTML 或 Markdown，保存正文、图片、原始页面、质量报告及 SQLite 索引，支持去重、失败恢复和批次导出。

**本程序默认使用 macOS Apple Vision 做本地图片 OCR；不依赖大模型 API。**

## 安装

环境要求：macOS、Python 3.11+、uv。使用浏览器抓取时，需另行安装 agent-browser 及其浏览器运行时，并确保 agent-browser 命令在 PATH 中。

~~~bash
git clone https://github.com/TomShawn/wechat-archiver.git
cd wechat-archiver
uv sync --locked --no-dev --extra ocr --system-certs
./wechat-archiver --help
./wechat-archiver doctor
~~~

不需要 OCR 时可去掉安装命令中的 --extra ocr，归档时使用 --no-ocr。启动脚本 ./wechat-archiver 使用本项目中的 .venv，无须手工激活环境。

## 归档公众号文章

将待归档的公众号文章 URL 按行放入 my-urls.txt，执行：

~~~bash
./wechat-archiver archive --input my-urls.txt --method browser --ocr --output-dir batches/我的批次
~~~

也支持单篇文章或 HTTP 模式：

~~~bash
./wechat-archiver archive --url 'https://mp.weixin.qq.com/s/文章标识' --method browser --no-ocr
./wechat-archiver archive --input my-urls.txt --method http --no-ocr
~~~

- --method browser 显式调用本机 agent-browser；--method http 使用普通 HTTP 请求，可能被微信限制。
- URL 包含 & 时，请使用引号包裹，或直接写入输入文件。
- 验证码、访问受限页面不会被自动绕过。

输出目录示例：

~~~text
batches/我的批次/
├── 文章标题.md
├── assets/              # 下载图片
├── originals/           # 原始 HTML 与质量记录
├── report.json          # 批次结果
└── reports/index.md     # 批次索引
~~~

不指定 --output-dir 时程序会自行分配批次目录。历史文章索引、SQLite 和断点续传数据保存在本地 data/，缓存归档保存在 output/。

## 导入本地 HTML 或 Markdown

~~~bash
./wechat-archiver import --html '/绝对路径/文章.html' --offline --no-ocr
./wechat-archiver import --markdown '/绝对路径/已有文章.md' --offline --no-ocr
~~~

--offline 禁止获取远程图片，仅使用已有缓存或本地文件。需要时可去掉该参数。

## 状态和恢复

~~~bash
./wechat-archiver status
./wechat-archiver retry --failed --ocr
./wechat-archiver doctor
~~~

默认配置文件为 config.yaml，含 SQLite 位置、输出目录、图片大小上限、请求间隔和本地 OCR 设置。

## 已知边界

- 图片 OCR 可能误读中文小字、代码、流程图及复杂表格；需对照原图检查。
- 嵌入的视频不会下载，保留原始标记或引用。
- 通过 ChatGPT + WebCodex 在某些文章中做过的 AI 视觉识别和文字描述，是对话协作完成的后处理，**并非本 Python 程序自动执行的功能**。
- 网络文章访问遵守网站限制，程序不自动破解验证码。

## 源码结构

- src/wechat_archiver/cli.py：CLI 入口。
- archive.py、batch.py：归档流程、批次汇总与导出。
- browser.py、fetcher.py：浏览器及 HTTP 抓取。
- parser.py、images.py、ocr.py、markdown.py：正文、图片、OCR、Markdown 转换。
- quality.py、database.py、storage.py：质量检查、SQLite 和文件持久化。

此 Git 仓库仅保存运行所需的源码、启动脚本、依赖锁文件和默认配置。历史归档文件、图片、数据库、日志、测试报告、测试样本及测试脚本没有迁入。运行时生成的内容均被 .gitignore 排除，避免意外提交真实归档资料。
