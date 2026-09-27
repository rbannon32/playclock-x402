"""Error types, mapped to what an agent should *do* about them.

Each error carries a ``hint`` because the consumer is a language model deciding
whether to retry, ask the user for money, or give up. "402" is not actionable;
"your wallet is not opted into USDC" is.
"""

from __future__ import annotations

__all__ = [
    "ApiError",
    "ApiUnreachable",
    "DataNotReady",
    "PaymentInFlight",
    "PaymentRefused",
    "PlayClockError",
    "PriceTooHigh",
    "WalletError",
]


class PlayClockError(RuntimeError):
    """Base class. ``hint`` is written for a model, not a stack trace reader."""

    hint: str = ""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        if hint:
            self.hint = hint

    def as_text(self) -> str:
        """Render for a tool result: what happened, then what to do."""
        return f"{self}\n\n{self.hint}" if self.hint else str(self)


class ApiUnreachable(PlayClockError):
    """The host did not answer at all. Nothing was signed, nothing was spent."""

    hint = (
        "The service may be down or the base URL wrong. Nothing was charged. "
        "Retrying later is safe."
    )


class ApiError(PlayClockError):
    """A non-2xx that is not a payment problem."""

    hint = "This is a server-side or request-shape problem, not a payment one. Nothing was charged."


class PriceTooHigh(PlayClockError):
    """The quote exceeded the configured ceiling. Refused before signing."""

    hint = (
        "Nothing was signed and nothing was spent. Ask the user whether to raise "
        "PLAYCLOCK_MAX_PRICE_USDC before retrying — do not retry on your own."
    )


class PaymentRefused(PlayClockError):
    """The paid retry was rejected, or a payment could not be built."""

    hint = (
        "The payment did not settle, so no USDC left the wallet. Common causes: the "
        "wallet is not opted into the USDC ASA, holds no USDC, or is on the wrong "
        "network. Do not retry blindly."
    )


class PaymentInFlight(PlayClockError):
    """The payment was signed and sent, and no response came back.

    Deliberately *not* an :class:`ApiUnreachable`. That error tells the agent
    nothing was charged and retrying is safe, which is true right up until the
    payment header goes out — after that the money may already have moved, and
    "retry" means signing a second payment for an answer already bought.
    """

    hint = (
        "The payment was already sent and may have settled, so this was NOT a "
        "free failure. Do NOT retry this tool — that would sign a second payment. "
        "Call playclock_recover_payments instead: it replays the identical "
        "request and returns the answer already paid for, at no extra cost."
    )


class DataNotReady(PlayClockError):
    """503 — the API refused to sell because its data is not ingested yet."""

    hint = (
        "The service deliberately refuses to sell an empty answer, so you were NOT "
        "charged. This resolves on its own once the next ingest runs; retry later."
    )


class WalletError(PlayClockError):
    """No usable payment method is configured."""

    hint = (
        "Set ALGORAND_MNEMONIC to a funded 25-word Algorand mnemonic, or set "
        "PLAYCLOCK_MOCK=1 to explore against a server running X402_MODE=mock."
    )
