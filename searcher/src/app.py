from flask import Flask, render_template, request, redirect, url_for
import yagooglesearch
import pika
import os
import threading
from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
import logging

# === Logging Setup ===
LOG_FILE = 'searcher.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),     # Log to file
        logging.StreamHandler()            # Also log to console (for Docker)
    ]
)

# === Flask App ===
app = Flask(__name__)

# MongoDB connection
def get_mongo_collection():
    mongo_host = os.getenv("MONGO_HOST", "mongo")  # Use Docker service name
    mongo_port = int(os.getenv("MONGO_PORT", 27017))
    mongo_user = os.getenv("MONGO_INITDB_ROOT_USERNAME", None)
    mongo_password = os.getenv("MONGO_INITDB_ROOT_PASSWORD", None)
    db_name = os.getenv("MONGO_DB_NAME", "tintel_db")  # Default database name
    collection_name = os.getenv("MONGO_COLLECTION_NAME", "searcher")  # Default collection name

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
    return collection

# Function to send URLs to RabbitMQ
def send_to_rabbitmq(urls):
    # Get the RabbitMQ host from the environment variable
    rabbitmq_host = os.getenv('RABBITMQ_HOST', 'rabbitmq')  # Default to 'rabbitmq' service name
    rabbitmq_port = int(os.getenv('RABBITMQ_PORT', 5672))
    rabbitmq_user = os.getenv("RABBITMQ_DEFAULT_USER", "guest")
    rabbitmq_password = os.getenv("RABBITMQ_DEFAULT_PASS", "guest")

    credentials = pika.PlainCredentials(rabbitmq_user, rabbitmq_password)
    connection = pika.BlockingConnection(pika.ConnectionParameters(host=rabbitmq_host, port=rabbitmq_port, credentials=credentials))
    channel = connection.channel()

    # Declare the queue (it will be created if it doesn't exist)
    channel.queue_declare(queue='search_results', durable=True)

    for url in urls:
        # Send each URL to the queue
        channel.basic_publish(exchange='',
                              routing_key='search_results',
                              body=url)
        print(f"Sent URL to queue: {url}")

    connection.close()

# Function to get the proxy settings
def get_proxy():
    # Get the proxy host from the environment variable, default to None if not set
    proxy_host = os.getenv('PROXY_HOST', None)
    proxy_port = os.getenv('PROXY_PORT', '8080')
    if proxy_host:
        return f"http://{proxy_host}:{proxy_port}"
    return ""  # No proxy if PROXY_HOST is not set

# Run search + write to MongoDB
def run_search(query):
    logging.info(f"Started search for query: '{query}'")
    proxies = get_proxy()
    collection = get_mongo_collection()
    now = datetime.now(timezone.utc)

    # Create indexes for performance
    try:
        collection.create_index("url", unique=True)
        collection.create_index("retrieved_at")
    except Exception as e:
        logging.warning(f"Index creation warning: {e}")

    one_day_ago = now - timedelta(days=1)
    recent_urls = set(
        doc["url"]
        for doc in collection.find({"retrieved_at": {"$gte": one_day_ago}}, {"url": 1})
    )
    logging.info(f"Loaded {len(recent_urls)} recent URLs from the past 24h.")

    # Perform search
    search_client = yagooglesearch.SearchClient(
        query=query,
        tbs="li:1",
        max_search_result_urls_to_return=400,
        http_429_cool_off_time_in_minutes=60,
        http_429_cool_off_factor=2.0,
        proxy=proxies,
        verify_ssl=False if proxies else True,
        verbosity=5,
        verbose_output=False
    )

    search_client.assign_random_user_agent()
    try:
        found_urls = search_client.search()
    except Exception as e:
        logging.error(f"Search failed: {e}")
        return

    logging.info(f"Found {len(found_urls)} URLs from Google.")

    new_urls = []
    docs_to_insert = []

    for url in found_urls:
        if url not in recent_urls:
            new_urls.append(url)
            docs_to_insert.append({
                "query": query,
                "url": url,
                "retrieved_at": now,
                "source": "searcher"
            })

    if docs_to_insert:
        try:
            result = collection.insert_many(docs_to_insert, ordered=False)
            logging.info(f"Inserted {len(result.inserted_ids)} new URLs into MongoDB.")
        except Exception as e:
            logging.error(f"Error inserting URLs into MongoDB: {e}")
    else:
        logging.info("No new URLs to insert into MongoDB.")

    if new_urls:
        send_to_rabbitmq(new_urls)
        logging.info(f"Sent {len(new_urls)} new URLs to RabbitMQ.")
    else:
        logging.info("No new URLs to send to RabbitMQ.")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/search', methods=['POST'])
def search():
    query = request.form['query']
    # Use threading to run the search in the background, so it doesn't block the web app
    threading.Thread(target=run_search, args=(query,)).start()
    return redirect(url_for('index', status="Search started!"))

if __name__ == "__main__":
    logging.info("Starting Searcher Flask app...")
    app.run(debug=False, host='0.0.0.0', port=5000)
