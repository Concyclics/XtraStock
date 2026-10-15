BEGIN;

ALTER TABLE news.articles
  ADD COLUMN IF NOT EXISTS page_name text,
  ADD COLUMN IF NOT EXISTS page_url text,
  ADD COLUMN IF NOT EXISTS page_pdf_url text,
  ADD COLUMN IF NOT EXISTS page_image_url text;

COMMENT ON COLUMN news.articles.page_name IS 'Newspaper page title/section name, e.g. 要闻, 评论, 理论.';
COMMENT ON COLUMN news.articles.page_url IS 'Original newspaper page URL.';
COMMENT ON COLUMN news.articles.page_pdf_url IS 'Original newspaper page PDF URL.';
COMMENT ON COLUMN news.articles.page_image_url IS 'Original newspaper page image URL.';

CREATE INDEX IF NOT EXISTS idx_news_articles_page_name
  ON news.articles(source_name, page_name);

COMMIT;
