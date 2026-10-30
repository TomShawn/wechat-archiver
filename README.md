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
./wechat-archiver archive --input my-urls.txt --method browser --ocr
~~~

也支持单篇文章或 HTTP 模式：

~~~bash
./wechat-archiver archive --url 'https://mp.weixin.qq.com/s/文章标识' --method browser --no-ocr
./wechat-archiver archive --input my-urls.txt --method http --no-ocr
# 也可以明确指定最终交付目录：
./wechat-archiver archive --input my-urls.txt --method browser --ocr --output-dir "/绝对路径/我的批次"
~~~

- --method browser 显式调用本机 agent-browser；--method http 使用普通 HTTP 请求，可能被微信限制。
- URL 包含 & 时，请使用引号包裹，或直接写入输入文件。
- 验证码、访问受限页面不会被自动绕过。

### 输出目录与运行数据

- **不指定 --output-dir**：批次交付目录会自动创建在 wechat-archiver/ 的上一级目录，名称类似 微信公众号归档-20261028-163000-a1b2c3/，与程序文件夹平级。
- **指定 --output-dir**：最终 Markdown、图片、原始文件和批次报告写入指定目录。相对路径按调用程序时所在的目录解析，推荐使用绝对路径。
- **程序内部状态**：SQLite、归档缓存、日志、搜索结果及诊断报告默认放在项目旁的 wechat-archiver-data/，不会写入源码目录。

默认目录结构如下：

~~~text
GitHub/
├── wechat-archiver/                 # 程序源码
├── wechat-archiver-data/            # SQLite、缓存、日志和内部归档数据
└── 微信公众号归档-20261028-163000-a1b2c3/  # 批次交付文件
    ├── 文章标题.md
    ├── assets/
    ├── originals/
    ├── report.json
    └── reports/index.md
~~~

config.yaml 中的相对路径均相对于配置文件所在目录。需要自定义内部数据位置时，可修改该文件；--output-dir 则只控制本次批次的最终交付目录。

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

- --force 重新处理已归档的文章并保留旧版本，复用已缓存的文章页面，不再请求微信。
- --refetch（archive、retry 可用）重新抓取文章页面后再处理，隐含 --force；抓取结果无效时保留原缓存和原归档。
- 重新处理失败时，已有成功、重复或部分完成交付的记录保持原状态，只记录本次错误。

默认配置文件为 config.yaml，含 SQLite 位置、输出目录、图片大小上限、请求间隔和本地 OCR 设置。文章页面按 request_interval 串行请求；图片按 image_request_interval 限速，并以 image_workers 个线程并发下载。单张图片返回 403 只记为该图片失败，同一图片主机连续 3 次 429 才停止本批次对它的请求。

## 已知边界

- 图片 OCR 可能误读中文小字、代码、流程图及复杂表格；需对照原图检查。
- OCR 先按中文（简体、繁体）识别；不含中文的行（代码、英文）再用英文模式识别并替换，因此含英文的图片会识别两遍。整图置信度取各行中位数，低于阈值或超过 20% 的行低于阈值时提示人工核对。
- 归档目录、批次交付目录与内部检查点中的图片使用硬链接共享同一份数据（跨磁盘时自动改为复制），归档图片不要原地修改。
- 嵌入的视频不会下载，保留原始标记或引用。
- 通过 ChatGPT + WebCodex 在某些文章中做过的 AI 视觉识别和文字描述，是对话协作完成的后处理，**并非本 Python 程序自动执行的功能**。
- 网络文章访问遵守网站限制，程序不自动破解验证码。

## 源码结构

- src/wechat_archiver/cli.py：CLI 入口。
- archive.py、batch.py：归档流程、批次汇总与导出。
- browser.py、fetcher.py：浏览器及 HTTP 抓取。
- parser.py、images.py、ocr.py、markdown.py：正文、图片、OCR、Markdown 转换。
- quality.py、database.py、storage.py：质量检查、SQLite 和文件持久化。

此 Git 仓库保存运行所需的源码、启动脚本、依赖锁文件、默认配置和 tests/ 下的单元测试（执行 uv sync 后运行 .venv/bin/python -m pytest）。历史归档文件、图片、数据库、日志和测试报告没有迁入。运行时生成的内容均被 .gitignore 排除，避免意外提交真实归档资料。
