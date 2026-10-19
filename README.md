# web-scraper

A small Python 3.12+ library for core web scraping logic.

## Install

```bash
python -m pip install -e .
```

## First scrape

```python
from web_scraper import fetch_page

soup = fetch_page("https://example.com")
print(soup.title.get_text(strip=True) if soup.title else "No title")
```
