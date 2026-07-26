from app.infrastructure.logging.logger import logger
import logging

def test_logger():
    assert logger.name == "creator_os"
    assert logger.level == logging.INFO
    assert len(logger.handlers) >= 1
