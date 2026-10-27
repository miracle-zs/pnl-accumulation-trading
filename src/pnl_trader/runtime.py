"""Application configuration and Binance USDⓈ-M Futures composition root."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal

import httpx

from .adapters.binance import BinanceFuturesGateway
from .adapters.process_lock import ProcessFileLock
from .adapters.serverchan import ServerChanNotifier
from .adapters.sqlite import SQLiteStateStore
from .application.execution import ExecutionCoordinator
from .application.review_batch import ReconciledReviewBatchSource
from .application.review_executor import SymbolReviewExecutor
from .application.review_run import ReviewRunCoordinator
from .application.scheduler import PeriodicReviewScheduler
from .application.universe import UniverseCatalog
from .application.universe_models import (
    AssetClassification,
    VersionedAssetClassificationDirectory,
)
from .credentials import optional_secret, require_secret


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    data_dir: Path
    classification_file: Path

    @classmethod
    def from_environment(cls) -> RuntimeConfig:
        """Load local runtime values without ever selecting the example list."""
        classification_file = Path(
            _required_environment("PNL_ASSET_CLASSIFICATIONS")
        ).expanduser().resolve()
        data_dir = Path(_required_environment("PNL_DATA_DIR")).expanduser().resolve()

        return cls(
            data_dir=data_dir,
            classification_file=classification_file,
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable {name} is missing")
    return value


def _load_reviewed_classifications(path: Path) -> VersionedAssetClassificationDirectory:
    directory = VersionedAssetClassificationDirectory.from_toml(path)
    if directory.version.lower().startswith("example-"):
        raise ValueError("asset classification version still identifies itself as an example")
    return directory


def validate_runtime_config(config: RuntimeConfig) -> dict[str, object]:
    """Validate settings and classifications without network or database writes."""

    classifications = _load_reviewed_classifications(config.classification_file)
    native_asset_count = sum(
        entry.classification is AssetClassification.NATIVE_CRYPTO
        for entry in classifications.assets
    )
    return {
        "status": "valid",
        "mode": "Binance USDⓈ-M Futures",
        "classification_version": classifications.version,
        "native_asset_count": native_asset_count,
        "data_dir": str(config.data_dir),
    }


async def run_application(config: RuntimeConfig) -> None:
    """Compose adapters and keep the Binance Futures service alive until shutdown."""

    classifications = _load_reviewed_classifications(config.classification_file)
    api_key = require_secret("BINANCE_API_KEY")
    api_secret = require_secret("BINANCE_API_SECRET")
    process_lock = ProcessFileLock(config.data_dir / "trader.lock")
    process_lock.acquire()
    try:
        with SQLiteStateStore(config.data_dir / "state.sqlite3") as store:
            timeout = httpx.Timeout(10.0, connect=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                try:
                    serverchan_sendkey = optional_secret("SERVERCHAN_SENDKEY")
                except ValueError as exc:
                    logger.warning(
                        "Server酱通知已停用（凭据不可用：%s）",
                        type(exc).__name__,
                    )
                    serverchan_sendkey = None

                notifier = None
                if serverchan_sendkey:
                    try:
                        notifier = ServerChanNotifier(
                            client=client,
                            sendkey=serverchan_sendkey,
                        )
                    except ValueError as exc:
                        logger.warning(
                            "Server酱通知已停用（SendKey 无效：%s）",
                            type(exc).__name__,
                        )
                else:
                    logger.warning(
                        "SERVERCHAN_SENDKEY 未设置，整轮失败通知已停用"
                    )

                exchange = BinanceFuturesGateway(
                    client=client,
                    api_key=api_key,
                    api_secret=api_secret,
                )
                universe = UniverseCatalog(
                    exchange_info=exchange,
                    classifications=classifications,
                )
                execution = ExecutionCoordinator(
                    store=store,
                    exchange=exchange,
                )
                symbol_reviews = SymbolReviewExecutor(
                    store=store,
                    exchange=exchange,
                    execution=execution,
                )
                batch_source = ReconciledReviewBatchSource(
                    store=store,
                    positions=exchange,
                    signals=universe,
                    account_equity=exchange,
                )
                review_runs = ReviewRunCoordinator(
                    store=store,
                    execution=execution,
                    symbol_reviews=symbol_reviews,
                    batch_source=batch_source,
                    account_equity=exchange,
                )

                stop_event = asyncio.Event()
                loop = asyncio.get_running_loop()
                for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(shutdown_signal, stop_event.set)
                scheduler = PeriodicReviewScheduler(
                    review_runs=review_runs,
                    notifier=notifier,
                )
                await scheduler.run_until_stopped(stop_event)
    finally:
        process_lock.release()
