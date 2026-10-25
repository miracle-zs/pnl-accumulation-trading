"""Application configuration and Binance USDⓈ-M Futures composition root."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import logging
import os
from pathlib import Path
import signal
import time

import httpx

from .adapters.binance import (
    BINANCE_USDM_FUTURES_URL,
    BinanceFuturesGateway,
)
from .adapters.process_lock import ProcessFileLock
from .adapters.sqlite import SQLiteStateStore
from .application.execution import ExecutionCoordinator
from .application.review_batch import ReconciledReviewBatchSource
from .application.review_executor import SymbolReviewExecutor
from .application.review_run import ReviewRunCoordinator
from .application.risk import AccountRiskGate
from .application.risk_models import AccountRiskPolicy
from .application.scheduler import PeriodicReviewScheduler
from .application.universe import UniverseCatalog
from .application.universe_models import (
    AssetClassification,
    VersionedAssetClassificationDirectory,
)
from .credentials import require_secret


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    account_id: str
    strategy_version: str
    data_dir: Path
    classification_file: Path
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    reserve_buffer: Decimal
    slippage_buffer_rate: Decimal
    max_maintenance_margin_ratio: Decimal
    taker_fee_rate: Decimal = Decimal("0.0005")

    @classmethod
    def from_environment(cls) -> RuntimeConfig:
        """Load required runtime values without ever selecting the example list."""

        account_id = _required_environment("PNL_ACCOUNT_ID")
        api_key = require_secret("BINANCE_API_KEY")
        api_secret = require_secret("BINANCE_API_SECRET")
        classification_file = Path(
            _required_environment("PNL_ASSET_CLASSIFICATIONS")
        ).expanduser().resolve()
        data_dir = Path(os.environ.get("PNL_DATA_DIR", "./var")).expanduser().resolve()
        strategy_version = os.environ.get("PNL_STRATEGY_VERSION", "periodic-review-v1").strip()
        if not strategy_version:
            raise ValueError("PNL_STRATEGY_VERSION must not be empty")

        if not classification_file.is_file():
            raise FileNotFoundError(
                f"asset classification file does not exist: {classification_file}"
            )
        if classification_file.name.lower().endswith(".example.toml"):
            raise ValueError(
                "the example classification file cannot be used by the runtime; "
                "create and review a separate full directory"
            )

        return cls(
            account_id=account_id,
            strategy_version=strategy_version,
            data_dir=data_dir,
            classification_file=classification_file,
            api_key=api_key,
            api_secret=api_secret,
            reserve_buffer=_required_decimal_environment("PNL_RESERVE_BUFFER_USDT"),
            slippage_buffer_rate=_required_decimal_environment(
                "PNL_SLIPPAGE_BUFFER_RATE"
            ),
            max_maintenance_margin_ratio=_required_decimal_environment(
                "PNL_MAX_MAINTENANCE_MARGIN_RATIO"
            ),
            taker_fee_rate=_decimal_environment("PNL_TAKER_FEE_RATE", "0.0005"),
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable {name} is missing")
    return value


def _required_decimal_environment(name: str) -> Decimal:
    return _parse_decimal(name, _required_environment(name))


def _decimal_environment(name: str, default: str) -> Decimal:
    return _parse_decimal(name, os.environ.get(name, default))


def _parse_decimal(name: str, value: str) -> Decimal:
    try:
        result = Decimal(value.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _load_reviewed_classifications(path: Path) -> VersionedAssetClassificationDirectory:
    directory = VersionedAssetClassificationDirectory.from_toml(path)
    if directory.version.lower().startswith("example-"):
        raise ValueError("asset classification version still identifies itself as an example")
    if not any(
        entry.classification is AssetClassification.NATIVE_CRYPTO
        for entry in directory.assets
    ):
        raise ValueError("the reviewed asset directory has no native-crypto entries")
    if any("example" in entry.source.lower() for entry in directory.assets):
        raise ValueError("example classification records remain in the runtime directory")
    return directory


def validate_runtime_config(config: RuntimeConfig) -> dict[str, object]:
    """Validate settings and classifications without network or database writes."""

    classifications = _load_reviewed_classifications(config.classification_file)
    AccountRiskPolicy(
        max_maintenance_margin_ratio=config.max_maintenance_margin_ratio,
        reserve_buffer=config.reserve_buffer,
        taker_fee_rate=config.taker_fee_rate,
        slippage_buffer_rate=config.slippage_buffer_rate,
    )
    native_asset_count = sum(
        entry.classification is AssetClassification.NATIVE_CRYPTO
        for entry in classifications.assets
    )
    return {
        "status": "valid",
        "mode": "Binance USDⓈ-M Futures",
        "account_id": config.account_id,
        "strategy_version": config.strategy_version,
        "classification_version": classifications.version,
        "native_asset_count": native_asset_count,
        "data_dir": str(config.data_dir),
    }


async def run_application(config: RuntimeConfig) -> None:
    """Compose adapters and keep the Binance Futures service alive until shutdown."""

    classifications = _load_reviewed_classifications(config.classification_file)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    process_lock = ProcessFileLock(config.data_dir / "trader.lock")
    process_lock.acquire()
    try:
        with SQLiteStateStore(config.data_dir / "state.sqlite3") as store:
            cached_universe = store.load_latest_universe_snapshot()
            fallback_rules = (
                {}
                if cached_universe is None
                else {
                    entry.symbol: entry.instrument.rules
                    for entry in cached_universe.instruments
                    if entry.instrument.rules is not None
                }
            )
            timeout = httpx.Timeout(10.0, connect=5.0)
            limits = httpx.Limits(max_connections=16, max_keepalive_connections=8)
            async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
                exchange = BinanceFuturesGateway(
                    client=client,
                    api_key=config.api_key,
                    api_secret=config.api_secret,
                    base_url=BINANCE_USDM_FUTURES_URL,
                    rules_cache_ttl_ms=86_400_000,
                    fallback_rules=fallback_rules,
                )
                universe = UniverseCatalog(
                    exchange_info=exchange,
                    mark_prices=exchange,
                    store=store,
                    classifications=classifications,
                    initial_snapshot=cached_universe,
                )
                execution = ExecutionCoordinator(
                    store=store,
                    exchange=exchange,
                )
                policy = AccountRiskPolicy(
                    max_maintenance_margin_ratio=config.max_maintenance_margin_ratio,
                    reserve_buffer=config.reserve_buffer,
                    taker_fee_rate=config.taker_fee_rate,
                    slippage_buffer_rate=config.slippage_buffer_rate,
                )
                risk_gate = AccountRiskGate(
                    snapshots=exchange,
                    brackets=exchange,
                    policy=policy,
                )
                symbol_reviews = SymbolReviewExecutor(
                    store=store,
                    exchange=exchange,
                    execution=execution,
                    risk_gate=risk_gate,
                )
                batch_source = ReconciledReviewBatchSource(
                    store=store,
                    positions=exchange,
                    open_orders=exchange,
                    signals=universe,
                )
                review_runs = ReviewRunCoordinator(
                    store=store,
                    execution=execution,
                    symbol_reviews=symbol_reviews,
                    batch_source=batch_source,
                    process_lock=process_lock,
                )

                recovery = await review_runs.recover_pending_on_startup(config.account_id)
                logger.info("startup pending-order recovery completed: count=%s", len(recovery))

                snapshot = await universe.refresh(force=True)
                logger.info(
                    "initial universe snapshot loaded: version=%s eligible=%s pending_review=%s",
                    snapshot.directory_version,
                    len(snapshot.eligible_symbols),
                    sum(item.eligibility.value == "PENDING_REVIEW" for item in snapshot.instruments),
                )

                startup_now_ms = time.time_ns() // 1_000_000
                startup_slot_ms = startup_now_ms - startup_now_ms % 900_000
                startup_batch = await batch_source.load_review_batch(
                    config.account_id,
                    config.strategy_version,
                    startup_slot_ms,
                )
                logger.info(
                    "startup state reconciliation completed: candidates=%s",
                    len(startup_batch.candidates),
                )

                stop_event = asyncio.Event()
                loop = asyncio.get_running_loop()
                registered_signals: list[signal.Signals] = []
                for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
                    try:
                        loop.add_signal_handler(shutdown_signal, stop_event.set)
                        registered_signals.append(shutdown_signal)
                    except (NotImplementedError, RuntimeError):
                        logger.warning("could not register handler for %s", shutdown_signal.name)
                try:
                    scheduler = PeriodicReviewScheduler(
                        account_id=config.account_id,
                        strategy_version=config.strategy_version,
                        review_runs=review_runs,
                    )
                    await scheduler.run_until_stopped(stop_event)
                finally:
                    for shutdown_signal in registered_signals:
                        loop.remove_signal_handler(shutdown_signal)
    finally:
        process_lock.release()
