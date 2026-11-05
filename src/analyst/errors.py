class AnalystError(Exception):
    """Base class; message is safe to show to the user."""


class LLMUnavailable(AnalystError):
    """The model API failed after retries and fallback."""


class DataUnavailable(AnalystError):
    """BigQuery cannot be reached or is not configured."""


class QueryTimeout(AnalystError):
    """A query ran longer than QUERY_TIMEOUT_S; the job was cancelled. Fed back to the model as a SQL error."""


class NeedsConfirmation(AnalystError):
    """Raised inside the loop when a destructive tool needs the user's yes and no callback is available."""

    def __init__(self, preview: dict) -> None:
        super().__init__("confirmation required")
        self.preview = preview


class UnsafeSQL(AnalystError):
    """The SQL was rejected by the scope/PII guard. Message is fed back to the model."""
