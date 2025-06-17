import pika
import json
import logging
import requests
import random
import time
from lxml import html
import os
from pymongo import MongoClient, errors

# --- Logging Setup ---
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Console Handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# File Handler
file_handler = logging.FileHandler('crawler.log')
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

# --- Config ---
MAX_RETRIES = 3

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

def connect_to_rabbitmq():
    rabbitmq_host = os.getenv("RABBITMQ_HOST", "rabbitmq")
    rabbitmq_port = int(os.getenv('RABBITMQ_PORT', 5672))
    rabbitmq_user = os.getenv("RABBITMQ_DEFAULT_USER", "guest")
    rabbitmq_password = os.getenv("RABBITMQ_DEFAULT_PASS", "guest")

    credentials = pika.PlainCredentials(rabbitmq_user, rabbitmq_password)
    connection = pika.BlockingConnection(pika.ConnectionParameters(host=rabbitmq_host, port=rabbitmq_port, credentials=credentials))

    channel = connection.channel()
    channel.queue_declare(queue='search_results', durable=True)
    channel.queue_declare(queue='search_results_retry', durable=True)
    channel.queue_declare(queue='raw_links', durable=True)
    
    return connection, channel

def publish_retry(channel, url, attempts):
    if attempts < MAX_RETRIES:
        payload = json.dumps({"url": url, "attempts": attempts})
        channel.basic_publish(
            exchange='',
            routing_key='search_results_retry',
            body=payload
        )
        logger.warning(f"Requeued URL to retry queue (attempt {attempts}): {url}")
    else:
        logger.error(f"Max retries reached. Dropping URL: {url}")

def get_proxy():
    proxy_host = os.getenv('PROXY_HOST', None)
    proxy_port = os.getenv('PROXY_PORT', '8080')
    if proxy_host:
        return {'http': f'http://{proxy_host}:{proxy_port}', 'https': f'http://{proxy_host}:{proxy_port}'}
    return None

# MongoDB connection
def get_mongo_collection():
    mongo_host = os.getenv("MONGO_HOST", "mongo")  # Use Docker service name
    mongo_port = int(os.getenv("MONGO_PORT", 27017))
    mongo_user = os.getenv("MONGO_INITDB_ROOT_USERNAME", None)
    mongo_password = os.getenv("MONGO_INITDB_ROOT_PASSWORD", None)
    db_name = os.getenv("MONGO_DB_NAME", "tintel_db")  # Default database name
    collection_name = os.getenv("MONGO_COLLECTION_NAME", "crawled_links")  # Default collection name

    if mongo_user and mongo_password:
        # Use authentication if username and password are provided
        from urllib.parse import quote_plus
        escaped_user = quote_plus(mongo_user)
        escaped_password = quote_plus(mongo_password)
        client = MongoClient(f"mongodb://{escaped_user}:{escaped_password}@{mongo_host}:{mongo_port}")
    else:
        # Connect without authentication
        client = MongoClient(f"mongodb://{mongo_host}:{mongo_port}")

    db = client[db_name]
    collection = db[collection_name]
    collection.create_index("url", unique=True)
    return collection

def insert_unique_links(collection, links, parent_url=None):
    inserted_count = 0
    for link in links:
        doc = {
            "url": link,
            "timestamp": time.time(),
            "source": "crawler",
            "is_valid": False,
            "dispatched": False,
        }
        if parent_url:
            doc["parent_url"] = parent_url
        try:
            result = collection.update_one(
                {"url": link},
                {"$setOnInsert": doc},
                upsert=True
            )
            if result.upserted_id:
                inserted_count += 1
        except errors.PyMongoError as e:
            logger.error(f"MongoDB insert error: {e}")
    return inserted_count

def extract_urls(url, headers):
    proxies = get_proxy()
    verify_ssl = False if proxies else True

    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, proxies=proxies, verify=verify_ssl, timeout=8)

            if response.status_code == 429:
                backoff = random.uniform(60, 120)
                logger.warning(f"429 received. Backing off for {backoff:.2f} seconds.")
                time.sleep(backoff)
                continue

            response.raise_for_status()
            tree = html.fromstring(response.content)
            links = tree.xpath('//a/@href')
            logger.info(f"Extracted {len(links)} links from {url}")
            return links

        except requests.exceptions.RequestException as e:
            logger.warning(f"Attempt {attempt+1}/3 failed fetching {url}: {e}")
            time.sleep(random.uniform(2, 4))
    return []

def clean_links(links):
    return list({link for link in links if link.startswith('http://') or link.startswith('https://')})

def publish_raw_links(channel, links):
    for link in links:
        payload = json.dumps({"url": link})
        channel.basic_publish(
            exchange='',
            routing_key='raw_links',
            body=payload
        )
        logger.info(f"Pushed to raw_links queue: {link}")

def create_callback(channel):
    def callback(ch, method, properties, body):
        try:
            msg = json.loads(body)
            url = msg["url"]
            attempts = msg.get("attempts", 0)
        except json.JSONDecodeError:
            url = body.decode()
            attempts = 0

        logger.info(f"Received URL to crawl: {url} (attempt {attempts})")

        headers = {
            'User-Agent': random.choice(USER_AGENTS)
        }

        crawled_collection = get_mongo_collection()
        links = extract_urls(url, headers)
        links = clean_links(links)

        if links:
            inserted = insert_unique_links(crawled_collection, links, parent_url=url)
            logger.info(f"Inserted {inserted} unique crawled links from: {url}")
            publish_raw_links(channel, links)
        else:
            logger.warning(f"No valid links found, will retry: {url}")
            publish_retry(channel, url, attempts + 1)

        ch.basic_ack(delivery_tag=method.delivery_tag)

        delay = random.uniform(3, 6)
        logger.info(f"Sleeping {delay:.2f}s before next task...")
        time.sleep(delay)

    return callback

def main():
    connection, channel = connect_to_rabbitmq()
    callback = create_callback(channel)

    channel.basic_consume(queue='search_results', on_message_callback=callback, auto_ack=False)
    channel.basic_consume(queue='search_results_retry', on_message_callback=callback, auto_ack=False)

    logger.info("Crawler started. Waiting for URLs...")
    channel.start_consuming()

if __name__ == "__main__":
    main()
