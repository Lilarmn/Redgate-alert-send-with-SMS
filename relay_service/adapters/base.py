"""SMS Provider Adapter Interface and Result Data Models."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SendResult:
    """Represents the result of sending an SMS via any provider."""
    success: bool
    status_code: Optional[int] = None
    response_code: Optional[str] = None
    message_id: Optional[str] = None
    error_message: Optional[str] = None
    raw_response: Optional[str] = None
    extra_details: Dict[str, Any] = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """Abstract base class for all SMS providers."""

    @abstractmethod
    def send(self, phone: str, text: str) -> SendResult:
        """Send an SMS message to a single phone number.
        
        Args:
            phone: Destination phone number (e.g. 09121234567).
            text: SMS body text.
            
        Returns:
            SendResult containing success status, ids, and error details.
        """
        pass
