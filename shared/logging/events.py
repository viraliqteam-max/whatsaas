import logging


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    field_text = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    logger.log(level, "event=%s %s", event, field_text)
