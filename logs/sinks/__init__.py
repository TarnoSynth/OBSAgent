"""Sink registry dla RunLoggera."""

from logs.sinks.base import Sink
from logs.sinks.console import ConsoleSink
from logs.sinks.jsonl import JsonlSink

__all__ = ["ConsoleSink", "JsonlSink", "Sink"]
