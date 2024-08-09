import asyncio
import os
import time
import csv
from io import StringIO, BytesIO
from typing import List, Dict, Any, Optional, Tuple, Union
from quart import Quart, request, jsonify, render_template, send_from_directory, Response, send_file, ResponseReturnValue
from aiohttp import ClientSession, ClientTimeout, ClientResponseError, ServerDisconnectedError, TooManyRedirects
from cachetools import TTLCache
import logging
from logging.config import dictConfig
from functools import wraps

# Logging configuration
dictConfig({
    'version': 1,
    'formatters': {'default': {
        'format': '[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
    }},
    'handlers': {'wsgi': {
        'class': 'logging.StreamHandler',
        'stream': 'ext://sys.stdout',
        'formatter': 'default'
    }},
    'root': {
        'level': 'INFO',
        'handlers': ['wsgi']
    }
})

logger = logging.getLogger(__name__)

class Config:
    API_BASE_URL: str = "https://api.scryfall.com/cards/named"
    REQUEST_TIMEOUT: float = 30.0
    MAX_RETRIES: int = 3
    RATE_LIMIT_DELAY: float = 0.1
    MAX_QUEUE_SIZE: int = 1000
    BATCH_SIZE: int = 10
    CACHE_SIZE: int = 1000
    CACHE_TTL: int = 86400
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    DEBUG: bool = False
    RATE_LIMIT: int = 10
    RATE_LIMIT_PERIOD: int = 60
    MAX_CONCURRENT_REQUESTS: int = 5

app = Quart(__name__, static_folder='static', template_folder='templates')
app.config.from_object(Config)

card_cache: TTLCache = TTLCache(maxsize=Config.CACHE_SIZE, ttl=Config.CACHE_TTL)

class RateLimiter:
    def __init__(self, limit: int, period: int):
        self.limit: int = limit
        self.period: int = period
        self.requests: List[float] = []

    def is_allowed(self) -> bool:
        now: float = time.time()
        self.requests = [t for t in self.requests if now - t < self.period]
        if len(self.requests) < self.limit:
            self.requests.append(now)
            return True
        return False

rate_limiter: RateLimiter = RateLimiter(Config.RATE_LIMIT, Config.RATE_LIMIT_PERIOD)

def rate_limit(f):
    @wraps(f)
    async def decorated_function(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        if not rate_limiter.is_allowed():
            return jsonify(error="Rate limit exceeded"), 429
        try:
            return await f(*args, **kwargs)
        except Exception as e:
            logger.error(f"Unexpected error in rate-limited function: {e}")
            return jsonify(error="Internal server error"), 500
    return decorated_function

class CardData:
    def __init__(self, name: str, oracle_text: str = '', mana_cost: str = '', type_line: str = '', set_name: str = '', found: bool = True):
        self.name: str = name
        self.oracle_text: str = oracle_text
        self.mana_cost: str = mana_cost
        self.type_line: str = type_line
        self.set_name: str = set_name
        self.found: bool = found

class CardManager:
    def __init__(self):
        self.session: Optional[ClientSession] = None
        self.semaphore: asyncio.Semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_REQUESTS)

    async def __aenter__(self) -> 'CardManager':
        if self.session is None or self.session.closed:
            self.session = ClientSession(timeout=ClientTimeout(total=Config.REQUEST_TIMEOUT))
        return self

    async def __aexit__(self, exc_type: Optional[type], exc_val: Optional[Exception], exc_tb: Optional[Any]) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def fetch_card_info(self, card_name: str) -> CardData:
        if not self.session or self.session.closed:
            raise RuntimeError("CardManager session is not initialized or has been closed")

        params: Dict[str, str] = {"fuzzy": card_name}

        async with self.semaphore:
            for attempt in range(Config.MAX_RETRIES):
                try:
                    await asyncio.sleep(Config.RATE_LIMIT_DELAY)
                    async with self.session.get(Config.API_BASE_URL, params=params) as response:
                        if response.status == 404:
                            logger.info(f"Card '{card_name}' not found in the API")
                            return CardData(name=card_name, found=False)
                        response.raise_for_status()
                        data: Dict[str, Any] = await response.json()
                        logger.info(f"Successfully fetched data for '{card_name}'")
                        return CardData(
                            name=data.get('name', ''),
                            oracle_text=data.get('oracle_text', ''),
                            mana_cost=data.get('mana_cost', ''),
                            type_line=data.get('type_line', ''),
                            set_name=data.get('set_name', ''),
                            found=True
                        )
                except ClientResponseError as e:
                    if e.status == 404:
                        logger.info(f"Card '{card_name}' not found in the API")
                        return CardData(name=card_name, found=False)
                    logger.warning(f"Attempt {attempt + 1} failed for card '{card_name}': {e}")
                    if attempt == Config.MAX_RETRIES - 1:
                        logger.error(f"All attempts failed for card '{card_name}': {e}")
                        return CardData(name=card_name, found=False)
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                except (ServerDisconnectedError, TooManyRedirects, asyncio.TimeoutError) as e:
                    logger.error(f"Network error while fetching card '{card_name}': {e}")
                    if attempt == Config.MAX_RETRIES - 1:
                        return CardData(name=card_name, found=False)
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                except Exception as e:
                    logger.error(f"Unexpected error fetching card '{card_name}': {e}")
                    return CardData(name=card_name, found=False)

        return CardData(name=card_name, found=False)

class QueueWorker:
    def __init__(self):
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=Config.MAX_QUEUE_SIZE)
        self.is_running: bool = False
        self.worker_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if not self.is_running:
            self.is_running = True
            self.worker_task = asyncio.create_task(self.process_queue())
            logger.info("Queue worker started")

    async def stop(self) -> None:
        if self.is_running:
            self.is_running = False
            if self.worker_task:
                try:
                    await asyncio.wait_for(self.worker_task, timeout=60.0)
                except asyncio.TimeoutError:
                    logger.warning("Queue worker did not stop gracefully, forcing stop")
                    self.worker_task.cancel()
                except Exception as e:
                    logger.error(f"Error stopping queue worker: {e}")
            logger.info("Queue worker stopped")

    async def process_queue(self) -> None:
        async with CardManager() as card_manager:
            while self.is_running:
                try:
                    batch: List[str] = []
                    for _ in range(Config.BATCH_SIZE):
                        try:
                            card_name: str = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                            batch.append(card_name)
                        except asyncio.TimeoutError:
                            break
                        except Exception as e:
                            logger.error(f"Error getting item from queue: {e}")
                            continue

                    if not batch:
                        await asyncio.sleep(0.1)
                        continue

                    logger.info(f"Processing batch of {len(batch)} cards")
                    for card_name in batch:
                        try:
                            if card_name not in card_cache:
                                logger.info(f"Fetching data for '{card_name}'")
                                card_info: CardData = await card_manager.fetch_card_info(card_name)
                                if card_info.found:
                                    logger.info(f"Successfully cached data for '{card_name}'")
                                else:
                                    logger.info(f"No data found for '{card_name}', caching as not found")
                                card_cache[card_name] = card_info
                            else:
                                logger.info(f"Data for '{card_name}' found in cache")
                        except Exception as e:
                            logger.error(f"Error processing card '{card_name}': {e}")
                        finally:
                            self.queue.task_done()  # Always mark the task as done, even if there was an error

                except asyncio.CancelledError:
                    logger.info("Queue processing cancelled")
                    break
                except Exception as e:
                    logger.error(f"Unexpected error in process_queue: {e}")
                    await asyncio.sleep(1)

    async def add_to_queue(self, card_name: str) -> bool:
        try:
            await asyncio.wait_for(self.queue.put(card_name), timeout=5.0)
            return True
        except asyncio.QueueFull:
            logger.warning(f"Queue full, unable to add: {card_name}")
            return False
        except asyncio.TimeoutError:
            logger.warning(f"Timeout while adding to queue: {card_name}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error adding '{card_name}' to queue: {e}")
            return False

    def get_queue_size(self) -> int:
        return self.queue.qsize()

queue_worker: QueueWorker = QueueWorker()

# Application lifecycle management
@app.before_serving
async def startup() -> None:
    try:
        await queue_worker.start()
    except Exception as e:
        logger.error(f"Error during startup: {e}")
        raise

@app.after_serving
async def shutdown() -> None:
    try:
        await queue_worker.stop()
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")

# Route handlers
@app.route('/')
async def index() -> ResponseReturnValue:
    try:
        return await render_template('index.html')
    except Exception as e:
        logger.error(f"Error rendering index: {e}")
        return "Internal Server Error", 500

@app.route('/js/<path:path>')
async def send_js(path: str) -> ResponseReturnValue:
    try:
        return await send_from_directory('templates', path)
    except FileNotFoundError:
        return "File not found", 404
    except Exception as e:
        logger.error(f"Error sending JS file: {e}")
        return "Internal Server Error", 500

@app.route('/css/<path:path>')
async def send_css(path: str) -> ResponseReturnValue:
    try:
        return await send_from_directory('static', path)
    except FileNotFoundError:
        return "File not found", 404
    except Exception as e:
        logger.error(f"Error sending CSS file: {e}")
        return "Internal Server Error", 500

@app.route('/fetch', methods=['POST'])
@rate_limit
async def fetch_cards() -> ResponseReturnValue:
    try:
        data: Dict[str, Any] = await request.get_json()
        card_names: List[str] = data.get('card_names', [])

        if not card_names:
            # If no card names provided, return all cached cards
            return jsonify([{
                'name': card.name,
                'oracle_text': card.oracle_text,
                'mana_cost': card.mana_cost,
                'type_line': card.type_line,
                'set_name': card.set_name,
                'status': 'found' if card.found else 'not found'
            } for card in card_cache.values()]), 200

        results: List[Dict[str, Any]] = []
        for name in card_names:
            if name in card_cache:
                card_data: CardData = card_cache[name]
                results.append({
                    'name': card_data.name,
                    'oracle_text': card_data.oracle_text,
                    'mana_cost': card_data.mana_cost,
                    'type_line': card_data.type_line,
                    'set_name': card_data.set_name,
                    'status': 'found' if card_data.found else 'not found'
                })
            else:
                queued: bool = await queue_worker.add_to_queue(name)
                results.append({'name': name, 'status': 'queued' if queued else 'queue full'})

        return jsonify(results), 200
    except Exception as e:
        logger.error(f"Error in fetch_cards: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/export', methods=['POST'])
async def export_cards() -> ResponseReturnValue:
    try:
        data: Dict[str, Any] = await request.get_json()
        card_names: List[str] = data.get('card_names', [])

        if not card_names:
            card_names = list(card_cache.keys())

        output = StringIO()
        writer = csv.writer(output)

        # Write header
        writer.writerow(['Name', 'Oracle Text', 'Mana Cost', 'Type Line', 'Set Name', 'Status'])

        for name in card_names:
            card_data: Optional[CardData] = card_cache.get(name)
            if card_data:
                writer.writerow([
                    card_data.name,
                    card_data.oracle_text,
                    card_data.mana_cost,
                    card_data.type_line,
                    card_data.set_name,
                    'found' if card_data.found else 'not found'
                ])
            else:
                writer.writerow([name, '', '', '', '', 'not found'])

        output.seek(0)
        bytes_io = BytesIO(output.getvalue().encode())
        return await send_file(
            bytes_io,
            mimetype='text/csv',
            as_attachment=True,
            attachment_filename='card_data.csv'
        )
    except Exception as e:
        logger.error(f"Error in export_cards: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/status')
async def status() -> ResponseReturnValue:
    try:
        return jsonify({
            'queue_size': queue_worker.get_queue_size(),
            'cache_size': len(card_cache),
            'is_fetching': queue_worker.is_running
        }), 200
    except Exception as e:
        logger.error(f"Error in status: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/clear', methods=['POST'])
async def clear_cards() -> ResponseReturnValue:
    try:
        card_cache.clear()
        await queue_worker.stop()
        await queue_worker.start()
        return jsonify({'success': True, 'message': 'All cards cleared'}), 200
    except Exception as e:
        logger.error(f"Error in clear_cards: {e}")
        return jsonify({'error': 'Internal server error'}), 500

# Error handlers
@app.errorhandler(404)
async def not_found(error: Any) -> ResponseReturnValue:
    logger.warning(f"Not found error occurred: {error}")
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
async def server_error(error: Any) -> ResponseReturnValue:
    logger.error(f"Internal server error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)