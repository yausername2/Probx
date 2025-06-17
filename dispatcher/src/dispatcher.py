import pika
import json
import re
import time
import logging
import os
import threading
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# --- Logging Setup ---
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Console Handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# File Handler
file_handler = logging.FileHandler('dispatcher.log')
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

# --- Mongo Setup ---
# MongoDB connection
def get_mongo():
    mongo_host = os.getenv("MONGO_HOST", "mongo")  # Use Docker service name
    mongo_port = int(os.getenv("MONGO_PORT", 27017))
    mongo_user = os.getenv("MONGO_INITDB_ROOT_USERNAME", None)
    mongo_password = os.getenv("MONGO_INITDB_ROOT_PASSWORD", None)
    db_name = os.getenv("MONGO_DB_NAME", "tintel_db")  # Default database name
    crawler_collection = os.getenv("MONGO_COLLECTION_NAME", "crawled_links")  # Default collection name

    if mongo_user and mongo_password:
        # Use authentication if username and password are provided
        from urllib.parse import quote_plus
        escaped_user = quote_plus(mongo_user)
        escaped_password = quote_plus(mongo_password)
        client = MongoClient(f"mongodb://{escaped_user}:{escaped_password}@{mongo_host}:{mongo_port}")
    else:
        # Connect without authentication
        client = MongoClient(f"mongodb://{mongo_host}:{mongo_port}")

    return client, db_name, crawler_collection

# --- RabbitMQ Setup ---
def connect_to_rabbitmq():
    rabbitmq_host = os.getenv("RABBITMQ_HOST", "rabbitmq")
    rabbitmq_port = int(os.getenv('RABBITMQ_PORT', 5672))
    rabbitmq_user = os.getenv("RABBITMQ_DEFAULT_USER", "guest")
    rabbitmq_password = os.getenv("RABBITMQ_DEFAULT_PASS", "guest")

    credentials = pika.PlainCredentials(rabbitmq_user, rabbitmq_password)
    connection = pika.BlockingConnection(pika.ConnectionParameters(host=rabbitmq_host, port=rabbitmq_port, credentials=credentials))

    channel = connection.channel()
    channel.queue_declare(queue='raw_links', durable=True)
    
    return connection, channel

# --- Dispatcher Class ---
class Dispatcher:
    def __init__(self, mongo_client, mongo_db, crawler_collection, mode='live', reload_interval=60):
        self.mongo_client = mongo_client
        self.mongo_db = mongo_db
        self.crawler_collection = crawler_collection
        self.mode = mode
        self.handlers = {}
        self.reload_interval = reload_interval

        # Load initial handlers
        self.load_handlers()

        # Start a background thread to reload handlers
        if self.mode == 'live':
            self.reload_thread = threading.Thread(target=self.reload_handlers_periodically)
            self.reload_thread.daemon = True
            self.reload_thread.start()

    def load_handlers(self):
        """ Load service handlers from MongoDB (or JSON file) """
        db = self.mongo_client[self.mongo_db]
        handlers = {}

        try:
            for doc in db["service_handlers"].find():
                name = doc["name"]
                handlers[name] = {
                    "regex": re.compile(doc["regex"]),
                    "queue": doc["queue"],
                    "collection": doc["collection"]
                }
            logger.info(f"Loaded {len(handlers)} service handlers.")
        except PyMongoError as e:
            logger.error(f"Failed to load service handlers: {e}")
        self.handlers = handlers

    def reload_handlers_periodically(self):
        """ Reload handlers periodically based on reload_interval """
        while True:
            time.sleep(self.reload_interval)
            logger.info("Reloading service handlers...")
            self.load_handlers()

    def dispatch_link(self, link, channel, doc=None):
        """ Dispatch link to the appropriate handler """
        for name, handler in self.handlers.items():
            if handler["regex"].match(link):
                if not doc:
                    doc = self.crawler_collection.find_one({"url": link})
                if doc:
                    # Mark as dispatched into db to avoid reprocessing when backfilling
                    try:
                        self.crawler_collection.update_one(
                            {"_id": doc["_id"]},
                            {"$set": {"dispatched": True}}
                        )
                    except PyMongoError as e:
                        logger.warning(f"Failed to mark document for {link} as dispatched: {e}")
                        continue
                else:
                    logger.warning(f"Document not found for link: {link}.")
                try:
                    payload = json.dumps({"url": link})
                    channel.queue_declare(queue=handler["queue"], durable=True)
                    channel.basic_publish(
                        exchange='',
                        routing_key=handler["queue"],
                        body=payload
                    )
                    logger.info(f"Dispatched to '{handler['queue']}' via handler '{name}': {link}")
                    self.save_to_db(handler["collection"], link)
                except Exception as e:
                    logger.error(f"Failed to dispatch link {link} to handler {name}: {e}")
                return
        logger.info(f"No handler matched for: {link}")

    def save_to_db(self, collection_name, url):
        """ Save URL to MongoDB collection """
        db = self.mongo_client[self.mongo_db]
        collection = db[collection_name]
        doc = {
            "url": url,
            "timestamp": time.time(),
            "source": "dispatcher",
            "is_valid": False
        }
        try:
            collection.update_one({"url": url}, {"$setOnInsert": doc}, upsert=True)
        except PyMongoError as e:
            logger.error(f"Mongo insert error for {url}: {e}")

    def process_raw_links(self, channel):
        """ Process links in live mode from raw_links queue """
        def callback(ch, method, properties, body):
            try:
                msg = json.loads(body)
                url = msg["url"]
            except Exception as e:
                logger.warning(f"Invalid message format: {e}")
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return

            self.dispatch_link(url, channel)
            ch.basic_ack(delivery_tag=method.delivery_tag)

        channel.basic_consume(queue='raw_links', on_message_callback=callback, auto_ack=False)

    def backfill_links(self, db, channel):
        """ Process existing links in backfill mode from crawled_links """
        crawled_collection = db[self.crawler_collection]
        logger.info(f"Backfilling links from {self.crawler_collection} collection...")
        logger.info(f"Found {crawled_collection.count_documents({})} documents in the collection.")
        for doc in crawled_collection.find():
            url = doc["url"]
            # Skip already processed URLs (those that have a matching handler)
            if doc.get("dispatched", False):
                continue
            self.dispatch_link(url, channel, doc)
                
    def run_live(self):
        """ Start RabbitMQ consumer in live mode """
        connection, channel = connect_to_rabbitmq()
        logger.info("Running in live mode...")
        self.process_raw_links(channel)
        channel.start_consuming()

    def run_backfill(self):
        """ Trigger backfill manually """
        logger.info("Running in backfill mode...")
        db = self.mongo_client[self.mongo_db]
        connection, channel = connect_to_rabbitmq()
        self.backfill_links(db, channel)
        connection.close()

    def run(self):
        """ Run the dispatcher based on the mode (live or backfill) """
        if self.mode == 'live':
            self.run_live()
        elif self.mode == 'backfill':
            self.run_backfill()

    # --- Exposed Methods for Web UI ---
    def add_handler(self, name, regex, queue, collection):
        """ Add a new handler to the database """
        db = self.mongo_client[self.mongo_db]
        handler = {
            "name": name,
            "regex": regex,
            "queue": queue,
            "collection": collection
        }
        try:
            db["service_handlers"].insert_one(handler)
            self.load_handlers()  # Reload handlers after adding a new one
            return handler
        except PyMongoError as e:
            logger.error(f"Failed to add handler: {e}")
            return None

    def trigger_backfill(self):
        """ Trigger backfill process for the Web UI """
        logger.info("Backfill process started.")
        db = self.mongo_client[self.mongo_db]
        connection, channel = connect_to_rabbitmq()
        self.backfill_links(db, channel)
        connection.close()
        return "Backfill process completed."

    def reload_handlers(self):
        """ Reload service handlers from MongoDB """
        logger.info("Reloading service handlers.")
        self.load_handlers()
        return len(self.handlers)

# --- Main ---
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Dispatcher for service handler routing")
    parser.add_argument('--mode', choices=['live', 'backfill'], default='live', help='Mode to run the dispatcher')
    args = parser.parse_args()

    mongo_client, mongo_db, crawler_collection = get_mongo()
    dispatcher = Dispatcher(mongo_client, mongo_db, crawler_collection, mode=args.mode)
    dispatcher.run()
