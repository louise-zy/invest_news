import os
import json
import time
import random
import sqlite3
import requests
import hmac
import hashlib
import base64
import urllib.parse
import re
from bs4 import BeautifulSoup
from datetime import datetime

# ================= Configuration =================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CURRENT_DIR, "config.json")

# Load Config
config = {}
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)

DB_FILE = os.path.join(CURRENT_DIR, config.get("db_file", "esdm_news.db"))
TARGET_URL = config.get("target_url", "https://www.esdm.go.id/id/media-center/siaran-pers")
KEYWORDS = [k.lower() for k in config.get("keywords", ["rkab", "nikel", "kobalt"])]

# DingTalk
DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", config.get("dingtalk", {}).get("webhook_url"))
DINGTALK_SECRET = os.environ.get("DINGTALK_SECRET", config.get("dingtalk", {}).get("secret"))

# LLM
LLM_API_KEY = os.environ.get("LLM_API_KEY", config.get("llm", {}).get("api_key"))
LLM_BASE_URL = config.get("llm", {}).get("base_url", "https://api.deepseek.com")
LLM_MODEL = config.get("llm", {}).get("model", "deepseek-chat")

# ================= Database =================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS news
                 (url TEXT PRIMARY KEY, title TEXT, published_date TEXT, processed_at TEXT)''')
    conn.commit()
    conn.close()

def is_processed(url):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM news WHERE url = ?", (url,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_processed(url, title, published_date):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO news (url, title, published_date, processed_at) VALUES (?, ?, ?, ?)",
              (url, title, published_date, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

# ================= Network =================
def fetch_url(url, retries=3):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5"
    }
    
    for i in range(retries):
        try:
            # Random delay
            time.sleep(random.uniform(1, 3))
            response = requests.get(url, headers=headers, timeout=20, verify=False)
            if response.status_code == 200:
                return response.content
            print(f"[-] Request failed {url}: {response.status_code}")
        except Exception as e:
            print(f"[-] Error fetching {url}: {e}")
            time.sleep(2)
    return None

# ================= Translation =================
def translate_title(text):
    if not LLM_API_KEY:
        return text + " (未配置翻译)"
    
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}"
        }
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": "You are a professional translator. Translate the following Indonesian news title to Chinese. Output ONLY the translated text."},
                {"role": "user", "content": text}
            ],
            "temperature": 0.3
        }
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions", json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"[-] Translation failed: {e}")
    return text

# ================= Notification =================
def send_dingtalk(title_cn, title_original, matched_keywords, date, url):
    if not DINGTALK_WEBHOOK:
        print("[-] No DingTalk Webhook configured")
        return

    webhook_url = DINGTALK_WEBHOOK
    
    # Check if secret is a real signature secret (starts with SEC) or just a keyword
    # If it's a keyword, we append it to the content to satisfy DingTalk security settings
    forced_keyword = ""
    
    if DINGTALK_SECRET:
        if DINGTALK_SECRET.startswith("SEC"):
            timestamp = str(round(time.time() * 1000))
            secret_enc = DINGTALK_SECRET.encode('utf-8')
            string_to_sign = '{}\n{}'.format(timestamp, DINGTALK_SECRET)
            string_to_sign_enc = string_to_sign.encode('utf-8')
            hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            webhook_url = f"{DINGTALK_WEBHOOK}&timestamp={timestamp}&sign={sign}"
        else:
            # Assume it's a keyword
            forced_keyword = f"\n\n(Keyword: {DINGTALK_SECRET})"

    # Markdown Content
    content = f"""**【印尼能矿部政策预警】**
- **中文标题**: {title_cn}
- **原文标题**: {title_original}
- **关键词**: {', '.join(matched_keywords)}
- **发布时间**: {date}
- **详情链接**: [点击跳转]({url})
{forced_keyword}
"""

    data = {
        "msgtype": "markdown",
        "markdown": {
            "title": f"🇮🇩 ESDM: {title_cn}",
            "text": content
        }
    }

    print(f"[*] Sending DingTalk notification to {webhook_url[:60]}...")
    try:
        resp = requests.post(webhook_url, json=data)
        print(f"[DEBUG] DingTalk Response: {resp.status_code} - {resp.text}")
        
        if resp.json().get("errcode") == 0:
            print(f"[+] Notification sent for: {title_cn}")
        else:
            print(f"[-] DingTalk Error: {resp.text}")
            
    except Exception as e:
        print(f"[-] Failed to send notification: {e}")

# ================= Main Logic =================
def run_monitor():
    print(f"[*] Starting ESDM Monitor at {datetime.now()}")
    html = fetch_url(TARGET_URL)
    if not html:
        return

    soup = BeautifulSoup(html, "html.parser")
    
    # Based on debug_site.py, links contain "arsip-berita"
    # We need to find the containers. Usually news lists have titles inside <a> or <h3>
    # We will look for all links containing "arsip-berita"
    
    # Deduplicate links found on page
    found_links = set()
    
    # Find all 'a' tags first
    all_links = soup.find_all("a", href=True)
    
    for link in all_links:
        href = link['href']
        title = link.get_text(strip=True)
        
        if "arsip-berita" in href and len(title) > 10:
            # Fix relative URL
            if href.startswith("/"):
                full_url = f"https://www.esdm.go.id{href}"
            else:
                full_url = href
            
            if full_url in found_links:
                continue
            found_links.add(full_url)
            
            if is_processed(full_url):
                print(f"[.] Skipping processed: {title[:20]}...")
                continue
            
            # Check Title Keywords
            title_lower = title.lower()
            matched = [k for k in KEYWORDS if k in title_lower]
            
            # If title doesn't match, maybe check body? 
            # PRD says "Check keyword hit logic on title and body"
            # So if not matched in title, we fetch the page.
            # But fetching every page is aggressive. Let's fetch only if title looks interesting OR just fetch to be safe?
            # PRD says "Real-time monitoring... high frequency".
            # Let's fetch detail for ALL new articles to be safe, as keywords might be in body.
            
            print(f"[*] Checking details for: {title}")
            detail_html = fetch_url(full_url)
            detail_text = ""
            date_str = "Unknown"
            
            if detail_html:
                detail_soup = BeautifulSoup(detail_html, "html.parser")
                # Try to extract text and date
                detail_text_raw = detail_soup.get_text()
                
                # Try to find date (Pattern: Tanggal : 19 Desember 2024)
                # Search in full text
                date_match = re.search(r"Tanggal\s*[:]\s*(.*?)(\n|\r|$)", detail_text_raw, re.IGNORECASE)
                if date_match:
                    date_str = date_match.group(1).strip()
                else:
                    # Fallback: check og:description
                    og_desc = detail_soup.find("meta", property="og:description")
                    if og_desc:
                        desc_text = og_desc.get("content", "")
                        date_match_2 = re.search(r"Tanggal\s*[:]\s*(.*?)(\n|\r|$)", desc_text, re.IGNORECASE)
                        if date_match_2:
                            date_str = date_match_2.group(1).strip()
                
                # Prepare for keyword check
                detail_text = detail_text_raw.lower()
            
            # Re-check keywords in title AND body
            if not matched:
                matched = [k for k in KEYWORDS if k in detail_text]
            
            if matched:
                print(f"[!] Hit found! Keywords: {matched}")
                
                # Translate
                title_cn = translate_title(title)
                
                # Send Notification
                send_dingtalk(title_cn, title, matched, date_str, full_url)
                
                # Mark as processed
                mark_processed(full_url, title, date_str)
            else:
                print(f"[-] No keywords found in: {title}")
                # Still mark as processed so we don't check it again? 
                # Yes, otherwise we keep checking irrelevant news.
                mark_processed(full_url, title, date_str)

if __name__ == "__main__":
    init_db()
    run_monitor()
