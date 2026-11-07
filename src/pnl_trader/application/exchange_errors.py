"""Application-level exchange errors shared across adapters and workflows."""


class ExchangeOperationError(RuntimeError):
    """An exchange API rejection or exhausted request retry."""


class ExchangeAccessError(RuntimeError):
    """Binance rejected credentials, permissions, or access to the API."""


class ExchangeResponseError(RuntimeError):
    """An exchange response could not be validated or reconciled safely."""


class InstrumentRulesUnavailableError(ExchangeOperationError):
    """The exchange has no usable market-order rules for a symbol."""


class UnsupportedLeverageError(ExchangeOperationError):
    """The exchange rejected a requested leverage as unsupported for a symbol."""

    def __init__(self, symbol: str, leverage: int, detail: str) -> None:
        self.symbol = symbol
        self.leverage = leverage
        self.detail = detail
        super().__init__(
            f"Binance does not support {leverage}x leverage for {symbol}: {detail}"
        )
