import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import google.generativeai as genai
import json
import time
from pathlib import Path

# --- 設定 ---
LAST_PROCESSED_DATES_FILE = "last_processed_dates.json"
GEMINI_USAGE_FILE = "gemini_usage.json"
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_DAILY_REQUEST_LIMIT = int(os.getenv("GEMINI_DAILY_REQUEST_LIMIT", "200"))
MAX_ARTICLES_PER_RUN = int(os.getenv("MAX_ARTICLES_PER_RUN", "200"))
TARGET_DAYS_AGO = int(os.getenv("TARGET_DAYS_AGO", "1"))
GEMINI_MIN_INTERVAL_SECONDS = float(os.getenv("GEMINI_MIN_INTERVAL_SECONDS", "5"))
GEMINI_USAGE_HISTORY_DAYS = int(os.getenv("GEMINI_USAGE_HISTORY_DAYS", "120"))
BLOG_SOURCES = [source.strip() for source in os.getenv("BLOG_SOURCES", "hinatazaka,nogizaka").split(",") if source.strip()]

SOURCE_CONFIGS = {
    "hinatazaka": {
        "site_name": "日向坂46",
        "list_url": "https://www.hinatazaka46.com/s/official/diary/member/list",
        "base_url": "https://www.hinatazaka46.com",
        "list_item_selector": "div.p-blog-group .p-blog-article",
        "title_selector": ".c-blog-article__title",
        "member_selector": ".c-blog-article__name",
        "date_selector": ".c-blog-article__date",
        "detail_link_selector": ".c-button-blog-detail",
        "content_selector": ".c-blog-article__text",
        "date_format": "%Y.%m.%d %H:%M",
        "kind": "flat",
    },
    "nogizaka": {
        "site_name": "乃木坂46",
        "member_directory_url": "https://www.nogizaka46.com/s/n46/diary/MEMBER",
        "base_url": "https://www.nogizaka46.com",
        "member_link_selector": "a[href*='/diary/MEMBER/list?'][href*='&ct=']",
        "member_list_item_selector": "a.bl--card",
        "title_selector": ".bl--card__ttl",
        "date_selector": ".bl--card__date",
        "detail_link_selector": None,
        "content_selector": ".bd--ctt .bd--edit",
        "date_format": "%Y.%m.%d %H:%M",
        "kind": "member_directory",
    },
}

# --- Gemini初期化 ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")


def load_last_processed_dates() -> dict[str, str]:
    data = load_json_file(LAST_PROCESSED_DATES_FILE, {})
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}


def save_last_processed_dates(payload: dict[str, str]):
    save_json_file(LAST_PROCESSED_DATES_FILE, payload)


def load_json_file(path: str, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json_file(path: str, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def prune_usage_history(data: dict) -> dict:
    # Date key is ISO format so lexical sort is chronological.
    keys = sorted(k for k in data.keys() if isinstance(k, str))
    keep_keys = set(keys[-GEMINI_USAGE_HISTORY_DAYS:])
    return {k: int(v) for k, v in data.items() if k in keep_keys}


def get_today_usage() -> int:
    today = datetime.now().date().isoformat()
    data = load_json_file(GEMINI_USAGE_FILE, {})
    if not isinstance(data, dict):
        return 0
    data = prune_usage_history(data)
    return int(data.get(today, 0))


def increment_today_usage() -> int:
    today = datetime.now().date().isoformat()
    data = load_json_file(GEMINI_USAGE_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data[today] = int(data.get(today, 0)) + 1
    data = prune_usage_history(data)
    save_json_file(GEMINI_USAGE_FILE, data)
    return int(data[today])


def compute_target_date(now: datetime) -> datetime.date:
    return (now - timedelta(days=TARGET_DAYS_AGO)).date()


def normalize_url(base_url: str, href: str) -> str:
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return base_url.rstrip("/") + href


def fetch_html(url: str) -> BeautifulSoup:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def collect_hinata_articles(config: dict, target_date: datetime.date) -> list[dict]:
    soup = fetch_html(config["list_url"])
    articles = soup.select(config["list_item_selector"])
    results = []
    for article in articles:
        title_el = article.select_one(config["title_selector"])
        name_el = article.select_one(config["member_selector"])
        date_el = article.select_one(config["date_selector"])
        link_el = article.select_one(config["detail_link_selector"])
        if not all([title_el, name_el, date_el, link_el]):
            continue
        date_str = date_el.get_text(strip=True)
        try:
            pub_date = datetime.strptime(date_str, config["date_format"])
        except ValueError:
            continue
        if pub_date.date() != target_date:
            continue
        href = link_el.get("href", "")
        full_url = normalize_url(config["base_url"], href)
        results.append({
            "source_key": "hinatazaka",
            "site": config["site_name"],
            "title": title_el.get_text(strip=True),
            "member": name_el.get_text(strip=True),
            "date": pub_date,
            "url": full_url,
            "detail_url": full_url,
        })
    return results


def extract_nogizaka_member_entries(config: dict) -> list[dict]:
    soup = fetch_html(config["member_directory_url"])
    member_links = soup.select(config["member_link_selector"])
    results = []
    seen_hrefs = set()
    now = datetime.now()
    for link in member_links:
        href = link.get("href", "")
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        text = link.get_text(" ", strip=True)
        # 形式: "愛宕 心響 04.15 20:36 更新" または "愛宕 心響(04.15 20:36 更新)"
        match = re.search(r"(.+?)\s*[\s(](\d{2}\.\d{2})\s+(\d{2}:\d{2})\s+更新", text)
        if not match:
            continue
        name = match.group(1).strip()
        date_part = match.group(2)
        time_part = match.group(3)
        try:
            update_date = datetime.strptime(f"{now.year}.{date_part} {time_part}", "%Y.%m.%d %H:%M")
            # If this date appears far in the future (e.g., Jan run with Dec entries), treat it as previous year.
            if update_date - now > timedelta(days=32):
                update_date = update_date.replace(year=now.year - 1)
        except ValueError:
            continue
        results.append({
            "source_key": "nogizaka",
            "member": name,
            "href": href,
            "update_date": update_date,
        })
    return results



def collect_nogizaka_articles(config: dict, target_date: datetime.date) -> list[dict]:
    results = []
    member_entries = extract_nogizaka_member_entries(config)
    for entry in member_entries:
        if entry["update_date"].date() != target_date:
            continue
        member_url = normalize_url(config["base_url"], entry["href"])
        soup = fetch_html(member_url)
        cards = soup.select(config["member_list_item_selector"])
        for card in cards:
            title_el = card.select_one(config["title_selector"])
            date_el = card.select_one(config["date_selector"])
            if not all([title_el, date_el]):
                continue
            date_str = date_el.get_text(strip=True)
            try:
                pub_date = datetime.strptime(date_str, config["date_format"])
            except ValueError:
                continue
            if pub_date.date() != target_date:
                continue
            href = card.get("href", "")
            if not href:
                continue
            results.append({
                "source_key": "nogizaka",
                "site": config["site_name"],
                "title": title_el.get_text(strip=True),
                "member": entry["member"],
                "date": pub_date,
                "url": normalize_url(config["base_url"], href),
                "detail_url": normalize_url(config["base_url"], href),
            })
    return results


def fetch_blog_content(url: str, content_selector: str) -> str:
    soup = fetch_html(url)
    content_el = soup.select_one(content_selector)
    if content_el:
        return content_el.get_text(separator="\n", strip=True)
    return ""


def summarize(site_name: str, member: str, title: str, content: str) -> str:
    prompt = f"""以下は{site_name}のメンバー「{member}」のブログです。
タイトル: {title}

本文:
{content[:3000]}

---
このブログを日本語で3〜5行に要約してください。メンバーの気持ちや出来事を中心にまとめてください。"""
    response = model.generate_content(prompt)
    return response.text.strip()


def post_to_slack(site_name: str, member: str, title: str, url: str, summary: str, pub_date: datetime):
    date_str = pub_date.strftime("%Y/%m/%d %H:%M")
    message = {
        "text": f"*📝 {site_name} / {member}* がブログを更新しました（{date_str}）\n*{title}*\n{url}",
        "attachments": [
            {
                "color": "#f5a623",
                "text": summary,
            }
        ],
    }
    resp = requests.post(SLACK_WEBHOOK_URL, json=message, timeout=10)
    resp.raise_for_status()


def main():
    now = datetime.now()
    target_date = compute_target_date(now)
    target_date_iso = target_date.isoformat()
    last_processed_dates = load_last_processed_dates()

    today_usage = get_today_usage()
    print(f"取得対象日: {target_date_iso}")
    all_articles = []
    completed_sources: set[str] = set()
    for source_key in BLOG_SOURCES:
        config = SOURCE_CONFIGS.get(source_key)
        if not config:
            print(f"  不明なソースをスキップ: {source_key}")
            continue
        if last_processed_dates.get(source_key) == target_date_iso:
            print(f"  {config['site_name']} は {target_date_iso} 処理済み")
            continue
        try:
            if config["kind"] == "flat":
                articles = collect_hinata_articles(config, target_date)
            else:
                articles = collect_nogizaka_articles(config, target_date)
            print(f"  {config['site_name']}: {len(articles)}件")
            all_articles.extend(articles)
            completed_sources.add(source_key)
        except Exception as e:
            print(f"  {config['site_name']} の取得に失敗: {e}")

    new_articles = sorted(all_articles, key=lambda a: (a["date"], a["site"], a["member"], a["title"]))
    if MAX_ARTICLES_PER_RUN > 0:
        new_articles = new_articles[:MAX_ARTICLES_PER_RUN]
    print(f"新着: {len(new_articles)}件")
    print(f"Gemini使用量: {today_usage}/{GEMINI_DAILY_REQUEST_LIMIT} (本日)")
    last_gemini_call_ts: float | None = None
    for article in new_articles:
        print(f"処理中: {article['member']} - {article['title']}")
        try:
            config = SOURCE_CONFIGS[article["source_key"]]
            content = fetch_blog_content(article["detail_url"], config["content_selector"])
            if not content:
                print("  本文取得失敗、スキップ")
                continue
            if today_usage < GEMINI_DAILY_REQUEST_LIMIT:
                if last_gemini_call_ts is not None:
                    elapsed = time.time() - last_gemini_call_ts
                    wait_seconds = GEMINI_MIN_INTERVAL_SECONDS - elapsed
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                summary = summarize(article["site"], article["member"], article["title"], content)
                last_gemini_call_ts = time.time()
                today_usage = increment_today_usage()
            else:
                summary = "本日のGemini無料枠上限に到達したため、要約はスキップしました。"
            post_to_slack(
                article["site"],
                article["member"],
                article["title"],
                article["url"],
                summary,
                article["date"],
            )
            print("  Slack送信完了")
        except Exception as e:
            print(f"  エラー: {e}")
            continue
    for source_key in completed_sources:
        if source_key in SOURCE_CONFIGS:
            last_processed_dates[source_key] = target_date_iso
    save_last_processed_dates(last_processed_dates)
    print("完了")


if __name__ == "__main__":
    main()
