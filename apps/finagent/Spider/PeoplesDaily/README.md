# People's Daily Spider

这个项目用于抓取《人民日报》电子版文章，保存为单篇 `txt`，按年份增量维护 `CSV`，并保留期次、版面、文章三层 SQLite 数据。

## 文件说明

- `peoples_daily_spider.py`：统一版爬虫。自动兼容 2024 年底前后的两套人民日报页面结构。
- `data/`：文章原文目录，每篇文章一个 `txt`，文件名格式为 `YYYYMMDD-版号-序号.txt`。
- `data/raw/`：原始 HTML 存档，按日期分为 `issue/`、`pages/`、`articles/`。
- `clean_data/`：年份 CSV 目录，文件名格式为 `RenMin_Daily_YYYY.csv`。
- `peoples_daily.sqlite3`：SQLite 元数据和正文库，包含 `issues`、`pages`、`articles` 三张表。
- `RenMinDaily.py`、`RenMinDailyv2.py`、`transpd.py`：旧脚本，保留作备份。

## 安装依赖

```bash
pip install requests beautifulsoup4
```

项目不再依赖 `pandas` 来刷新 CSV。

## 直接续爬

```bash
python peoples_daily_spider.py
```

默认行为：

1. 自动扫描 `data/*.txt`，读取现有文件名中的最大日期。
2. 从最大日期的下一天开始爬取。
3. 结束日期默认为当天。
4. 每篇文章保存为独立 `txt`。
5. 版面数据、文章正文和原始 HTML 路径写入 SQLite。
6. 只把新增或 CSV 中缺失的记录追加到对应年份 CSV，不重写整份大文件。

例如当前 `data/` 中最后一天是 `20260518`，运行脚本时当天是 `20260623`，则会自动更新 `20260519` 到 `20260623`。

## 指定日期范围

```bash
python peoples_daily_spider.py --start 20260519 --end 20260623
```

如果 `data/` 目录为空，必须手动指定 `--start`：

```bash
python peoples_daily_spider.py --start 20210101
```

## 只同步 CSV

如果已经有 txt 文件，只想把缺失记录追加到 CSV：

```bash
python peoples_daily_spider.py --sync-csv
```

只同步某一年：

```bash
python peoples_daily_spider.py --sync-csv --year 2026
```

## 常用参数

- `--data-dir data`：指定 txt 目录。
- `--clean-dir clean_data`：指定 CSV 目录。
- `--db-file peoples_daily.sqlite3`：指定 SQLite 输出文件；传空字符串可关闭 SQLite 入库。
- `--sleep 0.5`：每下载一篇文章后暂停 0.5 秒，降低请求频率。

## 输出格式

TXT 文件名：

```text
YYYYMMDD-版号-序号.txt
```

CSV 字段：

```text
id,date,title,content
```

其中：

- `id`：去掉 `.txt` 后的文件名。
- `date`：文章日期，格式 `YYYYMMDD`。
- `title`：按旧逻辑取 txt 前三行拼接。
- `content`：从第四行开始的正文。

SQLite 表：

```text
issues(date, source, issue_url, site_version, page_count, fetched_at)
pages(page_id, date, page_no, page_name, page_url, pdf_url, image_url, raw_html_path, fetched_at)
articles(article_id, date, page_no, article_no, sort_order, title, page_name, article_url, content_text, txt_path, raw_html_path, fetched_at)
```

## 接口兼容

脚本以 `2024-12-04` 为分界：

- `2024-12-03` 及以前使用旧版页面：`/rmrb/html/YYYY-MM/DD/...`
- `2024-12-04` 及以后使用新版页面：`/rmrb/pc/layout/YYYYMM/DD/...`
