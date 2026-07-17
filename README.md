# Steel Pipe Distributor Outreach MVP

这是一个本地优先的 B2B 线索抓取、评分、邮件草稿和受控发信系统，适合和 n8n 搭配使用。

默认流程是：

1. 导入或通过搜索 API 发现公司官网。
2. 抓取官网公开页面，如首页、contact、about、products、locations。
3. 提取官网公开业务邮箱，如 `sales@`、`info@`、`quotes@`、`contact@`。
4. 用规则先评分。
5. 只对高分线索调用本地模型。
6. 生成邮件草稿。
7. 人工审核后再小批量发送。

系统默认不会直接群发。只有 `approved=1` 的邮件才会进入发送流程。

## 快速开始

新设备第一次运行，建议先看 [MIGRATION.md](MIGRATION.md)。项目提供了自动检测和准备脚本，会在启动前检查 Python、VS Code、虚拟环境和依赖：

```bash
./scripts/bootstrap_macos.sh
```

Windows PowerShell：

```powershell
.\scripts\bootstrap_windows.ps1
```

```bash
python3 -m trade_outreach.cli init-db
python3 -m trade_outreach.cli import-leads-csv data/google_maps_export.csv --source google-maps
python3 -m trade_outreach.cli stats
python3 -m trade_outreach.cli scrape --limit 200
python3 -m trade_outreach.cli score
python3 -m trade_outreach.cli draft --limit 100
python3 -m trade_outreach.cli export-csv data/drafts.csv
```

## 本地 Dashboard

启动本地可视化面板：

```bash
python3 -m trade_outreach.dashboard
```

然后打开：

```text
http://127.0.0.1:8765
```

根页面是任务管理页。每个 task 都有独立的 dashboard、SQLite 数据库、queries、countries 和公司资料。可以在任务页创建新任务，例如 `Valve distributors`，也可以 rename/delete 任务。删除任务只会从任务列表移除，数据文件会留在本地以防误删。

Dashboard 可以查看 stats、leads、抓到的邮箱、drafts、prompt、邮件模板，并可执行 discovery preview/import、crawl emails、score、draft、approve、send dry-run、test email 等动作。`Crawl Emails` 是唯一的邮件抓取按钮，会并发处理所有还没有邮箱的 lead，包括新导入、之前没抓到邮箱、以及抓取失败的网站。有邮箱后可以直接点 `Generate Drafts`，不需要先运行 score。`Generate Drafts` 会优先为还没有 draft 的有邮箱 lead 生成草稿，不会被已有草稿的老 lead 占用 limit。`Score / Rescore` 只用于排序和参考。`Approve Batch` 会批准所有 pending 草稿，包括低分和个人邮箱草稿。单封邮件需要重写时，在 Drafts 表对应行点击 `Regenerate`。

Leads 表会显示 `Country`。新通过 Google Places 导入的 lead 会自动记录本次搜索对应的国家；旧数据如果没有 country，Dashboard 会尽量从 `source` 搜索词里识别国家。

Dashboard 下方 `Queries` 和 `Countries` 面板可以直接添加或删除搜索词和目标国家。Discover 按钮会把 `Queries` 中的每个关键词和 `Countries` 中的每个国家组合后提交给 Google Places。

Dashboard 左侧 `Your Company` 可以填写/保存当前任务自己的公司名、目标市场、邮寄地址、推广业务概述，并可添加/删除可选信息条目，例如 Facebook、官网、WhatsApp、证书链接等。保存后只影响当前 task，新生成的邮件草稿和 Prompt 显示会使用这些信息，不影响其他 task。

如果想向同一批客户推广不同产品，可以在原 task 点击 `Export Leads CSV`，然后进入另一个 task，在左侧输入导出的 CSV 路径并点击 `Import Leads CSV`。这样客户群体可以复用，但邮件草稿、发送状态、公司资料仍然独立。

真实发送使用 `Send All Approved`，会发送已批准且未发送的 draft，并按 `.env` 中的 `MAIL_BCC` 给自己抄送。系统默认每天最多真实发送 100 封，每封间隔 800 秒；单封 `Send` 也会受每日上限限制。先用 `Send Dry Run` 确认收件人和 BCC，再真实发送。

如果已经有一个邮箱但不需要爬网站，可以在 Dashboard 左侧输入邮箱和可选公司名，点击 `Create Draft`，系统会把它保存为本地 lead 并直接生成 draft。

如果公司已经在 Leads 表里，可以直接在该行 Emails 列输入新邮箱并点击 `Add`。系统会把邮箱合并进该公司的本地 `leads.emails` 字段，不覆盖已有邮箱。如果这个邮箱还没有对应 draft，系统会同时为该邮箱创建一封新的 pending draft。

邮箱抓取使用自适应联系页发现：先抓首页和常见路径，再分析页面链接，优先访问 `contact`、`kontakt`、`ansprechpartner`、`team`、`standorte`、`unternehmen`、`about` 等更可能包含邮箱的页面，并跳过 `career`、`privacy`、`news`、`blog` 等低价值链接。若官网链接到关联品牌/子站的联系或公司页面，也会少量跟随。提取器也会处理 footer 中被标签或空格拆开的邮箱，例如 `info <span>@</span> example . com`。

## 批量发现目标公司

系统不会凭空猜邮箱。邮箱来自目标公司官网公开页面，流程是先批量发现官网，再抓取公开邮箱。

### 自动获取目标公司

推荐用 Google Places API 自动获取公司官网。`.env` 中配置：

```bash
GOOGLE_PLACES_API_KEY=你的 Google Places API Key
GOOGLE_PLACES_REGION_CODE=GB
GOOGLE_PLACES_LANGUAGE_CODE=en
```

搜索范围主要由两个文件决定：

- `examples/discovery_queries.txt`: 行业、产品、客户类型关键词，例如 distributor、stockholder、supplier
- `examples/countries.txt`: 目标国家列表

程序实际提交给 Google Places 的搜索词是 `关键词 + 国家`，例如 `steel pipe distributor Germany`。所以调整关键词会直接影响搜索范围和返回公司类型；调整国家文件会影响目标市场国家。`.env` 里的 `GOOGLE_PLACES_REGION_CODE` 和 `GOOGLE_PLACES_LANGUAGE_CODE` 主要影响 Google Places 的地区/语言偏好，不等同于只搜索某一个国家。

先小批量预览，不写数据库：

```bash
python3 -m trade_outreach.cli discover-batch \
  --limit-queries 2 \
  --limit-countries 2 \
  --count 5
```

确认结果质量后导入：

```bash
python3 -m trade_outreach.cli discover-batch \
  --limit-queries 2 \
  --limit-countries 2 \
  --count 5 \
  --execute
```

导入后抓官网公开邮箱：

```bash
python3 -m trade_outreach.cli scrape --limit 100
python3 -m trade_outreach.cli score
python3 -m trade_outreach.cli draft --limit 100
python3 -m trade_outreach.cli export-csv data/drafts.csv
python3 -m trade_outreach.cli stats
```

如果要重试所有还没有邮箱的网站：

```bash
python3 -m trade_outreach.cli scrape --limit 999 --retry-no-email --max-seconds 900 --fetch-attempts 28 --fetch-timeout 4 --workers 6
```

低成本方式是不使用 Search API，先从公开目录或展会名单复制官网 URL，再导入：

```bash
python3 -m trade_outreach.cli import-urls examples/manual_urls.txt --source europages
python3 -m trade_outreach.cli scrape --limit 100
python3 -m trade_outreach.cli score
python3 -m trade_outreach.cli draft --limit 100
```

如果你使用 Apollo、Lusha、Apify Google Maps Scraper、Google Maps scraper 或其他工具导出了 CSV，可以直接导入常见字段：

```bash
python3 -m trade_outreach.cli import-leads-csv data/google_maps_export.csv --source apify-google-maps
```

CSV 中这些列名会被自动识别：

- 公司名：`company_name`, `company`, `title`, `name`, `business name`, `place name`
- 官网：`website`, `website_url`, `websiteurl`, `site`, `url`, `company website`, `business website`
- 邮箱：`email`, `emails`, `company email`, `contact email`
- 电话：`phone`, `phones`, `phone number`, `telephone`

导入后继续：

```bash
python3 -m trade_outreach.cli scrape --limit 200
python3 -m trade_outreach.cli score
python3 -m trade_outreach.cli draft --limit 100
```

单独调试某个官网的邮箱抓取：

```bash
python3 -m trade_outreach.cli debug-scrape-url https://example.com/
```

这个命令只预览抓取结果，不会写入 dashboard。要写入数据库，使用：

```bash
python3 -m trade_outreach.cli rescrape-url https://example.com/
```

把某个官网加入数据库并重抓：

```bash
python3 -m trade_outreach.cli rescrape-url https://example.com/
```

适合欧洲 steel pipe 的公开来源包括：

- Europages / WLW / Kompass 等企业目录
- Tube Düsseldorf 等展会 exhibitor list
- 欧洲各国 steel stockholders、pipe distributors、industrial suppliers 协会目录
- 目标国家 Google Maps 手动搜索结果，复制公司官网 URL

导入后继续：

```bash
python3 -m trade_outreach.cli scrape --limit 100
python3 -m trade_outreach.cli score
python3 -m trade_outreach.cli draft --limit 100
python3 -m trade_outreach.cli approve-batch --limit 20 --min-score 8
python3 -m trade_outreach.cli approve-batch --limit 20 --min-score 8 --execute
python3 -m trade_outreach.cli send --limit 100 --delay-seconds 800 --dry-run
python3 -m trade_outreach.cli send --limit 100 --delay-seconds 800
```

目标市场可以在 Dashboard 左侧 `Your Company` 里保存，也可以在 `.env` 中设置：

```bash
TARGET_REGION=your target market
GOOGLE_PLACES_REGION_CODE=GB
GOOGLE_PLACES_LANGUAGE_CODE=en
```

## Prompt 和模板位置

- DeepSeek/Ollama 个性化首句 prompt: [trade_outreach/prompts.py](trade_outreach/prompts.py)
- 邮件标题和正文模板: [trade_outreach/templates.py](trade_outreach/templates.py)
- 关键词评分规则: [trade_outreach/scoring.py](trade_outreach/scoring.py)
- 搜索发现逻辑和排除域名: [trade_outreach/discovery.py](trade_outreach/discovery.py)

查看草稿：

```bash
python3 -m trade_outreach.cli list-drafts
```

人工批准某条草稿：

```bash
python3 -m trade_outreach.cli approve-draft 1
```

取消批准某条未发送草稿：

```bash
python3 -m trade_outreach.cli unapprove-draft 1
```

dry-run 发送：

```bash
python3 -m trade_outreach.cli send --limit 5 --dry-run
```

真实发送需要先配置 SMTP 环境变量，再去掉 `--dry-run`。

批量发送仍然只会发送 `approved=1` 且未发送过的草稿：

```bash
python3 -m trade_outreach.cli send --limit 100 --delay-seconds 800 --dry-run
python3 -m trade_outreach.cli send --limit 100 --delay-seconds 800
```

## 自动循环流程

`auto-pipeline` 会检测当前草稿发送状态：

1. 如果还有已批准但未发送的 draft，先发送这一批。
2. 如果所有 draft 都已发送，自动执行下一轮 `discover-batch -> scrape -> score -> draft`。
3. 新 draft 默认自动批准。
4. 默认通过 SMTP 真实发送，仍受 `SEND_DAILY_LIMIT`、`--send-limit` 和发送间隔限制。

全自动连续运行：

```bash
python3 -m trade_outreach.cli auto-pipeline \
  --cycles 0 \
  --sleep-seconds 0 \
  --send-limit 5 \
  --delay-seconds 800
```

Auto Pipeline 会给内部发送命令启用 `--continue-after-sleep`。如果电脑睡眠导致等待时间超过 `--delay-seconds`，醒来后会继续发送后续邮件，而不是停止这一批。

安全预览，不真实发送：

```bash
python3 -m trade_outreach.cli auto-pipeline \
  --cycles 1 \
  --dry-run \
  --send-limit 5 \
  --delay-seconds 800
```

如果临时需要保留人工审核，可以关闭自动批准：

```bash
python3 -m trade_outreach.cli auto-pipeline \
  --cycles 1 \
  --dry-run \
  --no-auto-approve
```

真实发送仍然只会发送 `approved=1` 且 `sent_at` 为空的 draft，并受 `SEND_DAILY_LIMIT` 限制。

## 配置

复制示例配置：

```bash
cp .env.example .env
```

常用环境变量：

- `OUTREACH_DB`: SQLite 文件路径，默认 `data/outreach.sqlite3`
- `LLM_PROVIDER`: `deepseek` 或 `ollama`
- `DEEPSEEK_API_KEY`: DeepSeek API Key
- `DEEPSEEK_BASE_URL`: 默认 `https://api.deepseek.com`
- `DEEPSEEK_MODEL`: 默认 `deepseek-v4-flash`
- `OLLAMA_URL`: Ollama 地址，默认 `http://localhost:11434`
- `OLLAMA_MODEL`: 本地模型名，默认 `llama3.1:8b`
- `USE_LLM`: 是否使用本地模型，默认 `0`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`
- `SMTP_SECURITY`: `starttls`, `ssl`, 或 `none`
- `MAIL_FROM`, `MAIL_REPLY_TO`, `MAIL_CC`, `MAIL_BCC`
- `SEND_DELAY_SECONDS`: 批量发送时每封之间的默认间隔秒数，默认 `800`
- `SEND_DAILY_LIMIT`: 每天最多真实发送封数，默认 `100`
- `COMPANY_NAME`, `COMPANY_ADDRESS`, `COMPANY_FACEBOOK_URL`

如果想每封客户邮件都抄送自己：

```bash
MAIL_CC=test@example.com
```

如果不想让客户看到抄送地址，建议用密送：

```bash
MAIL_BCC=test@example.com
```

## 使用 DeepSeek

复制 `.env.example` 为 `.env`，然后填入：

```bash
USE_LLM=1
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的 DeepSeek API Key
DEEPSEEK_MODEL=deepseek-v4-flash
```

DeepSeek 官方文档显示它支持 OpenAI 兼容格式，`deepseek-v4-flash` 适合低成本批量分类和短文案生成。`deepseek-chat` 和 `deepseek-reasoner` 将在 2026-07-24 23:59 北京时间弃用，所以这里默认不用旧模型名。

## 成本效率策略

- LLM 默认关闭，只用规则评分也能跑完整流程。
- `USE_LLM=1` 后，模型只处理高分线索，并且只生成一句个性化开头。
- `score` 主要用于排序和判断相关性，不再默认阻止生成草稿。
- 默认只给通用业务邮箱生成草稿，如 `sales@`, `info@`, `export@`, `contact@`, `vertrieb@`。
- 如果你确认要给个人邮箱也生成草稿，可以运行 `python3 -m trade_outreach.cli draft --include-personal`。
- 网页抓取只抓首页和常见联系页，避免把大量无关页面塞给模型。
- 邮件发送强制检查退订黑名单和 `approved` 状态。

## n8n

可以导入 [n8n/steel_pipe_outreach_workflow.json](n8n/steel_pipe_outreach_workflow.json)，里面使用 Execute Command 节点调用本地 CLI。

## 合规提醒

面向美国商业邮件时，请遵守 CAN-SPAM：真实发件人信息、非误导标题、实体邮寄地址、清晰退订方式，并及时处理退订。建议新域名从每天 20-50 封开始，逐步升量。
