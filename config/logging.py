"""Small JSON formatter for operational events; callers choose non-PII fields."""
import json
import logging


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "outcome": getattr(record, "outcome", None),
        }
        if hasattr(record, "deleted"):
            payload["deleted"] = record.deleted
        return json.dumps({key: value for key, value in payload.items() if value is not None}, separators=(",", ":"))
