"""Application-level exchange errors shared across adapters and workflows."""


class UnsupportedLeverageError(RuntimeError):
    """The exchange rejected a requested leverage as unsupported for a symbol."""

    def __init__(self, symbol: str, leverage: int, detail: str) -> None:
        self.symbol = symbol
        self.leverage = leverage
        self.detail = detail
        super().__init__(
            f"Binance does not support {leverage}x leverage for {symbol}: {detail}"
        )
