import logging

# Step 1: Set up the logger configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mtg_card_fetcher")  # Create a logger instance