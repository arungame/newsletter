from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional

import yaml

from .models import ChannelVariant
from .utils import clean_text

VALID_CHANNEL_MODES = {"review", "auto", "manual_export"}


@dataclass(frozen=True)
class ChannelCapabilities:
    automatic_publish: bool = False
    manual_handoff: bool = False
    preview: bool = False
    status_polling: bool = False


@dataclass(frozen=True)
class ChannelConfig:
    name: str
    enabled: bool = False
    mode: str = "review"
    account_id: str = "default"
    publish_time: str = "06:00"
    adapter: str = ""

    def __post_init__(self) -> None:
        if self.mode not in VALID_CHANNEL_MODES:
            raise ValueError(f"unsupported channel mode for {self.name}: {self.mode}")

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def load_channel_configs(path: Path) -> Dict[str, ChannelConfig]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    channels = raw.get("channels", raw)
    if not isinstance(channels, Mapping):
        raise ValueError("channels file must contain a mapping named 'channels'")

    configs: Dict[str, ChannelConfig] = {}
    for raw_name, raw_config in channels.items():
        name = clean_text(str(raw_name)).lower().replace("-", "_")
        if not name:
            continue
        values = raw_config if isinstance(raw_config, Mapping) else {}
        configs[name] = ChannelConfig(
            name=name,
            enabled=bool(values.get("enabled", False)),
            mode=clean_text(str(values.get("mode", "review"))).lower() or "review",
            account_id=clean_text(str(values.get("account_id", "default"))) or "default",
            publish_time=clean_text(str(values.get("publish_time", "06:00"))) or "06:00",
            adapter=clean_text(str(values.get("adapter", name))).lower() or name,
        )
    return configs


def update_channel_mode(path: Path, channel: str, mode: str) -> ChannelConfig:
    normalized_channel = clean_text(channel).lower().replace("-", "_")
    normalized_mode = clean_text(mode).lower()
    if not normalized_channel:
        raise ValueError("channel name is required")
    if normalized_mode not in VALID_CHANNEL_MODES:
        raise ValueError(f"unsupported channel mode: {normalized_mode}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("channels file must contain a mapping")
    channels = raw.setdefault("channels", {})
    if not isinstance(channels, dict):
        raise ValueError("channels file must contain a mapping named 'channels'")
    current = channels.get(normalized_channel)
    if not isinstance(current, dict):
        raise ValueError(f"channel is not configured: {normalized_channel}")
    current["mode"] = normalized_mode
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    try:
        temporary.replace(path)
    except OSError:
        # Docker bind-mounted files cannot always be atomically replaced.
        path.write_text(temporary.read_text(encoding="utf-8"), encoding="utf-8")
        temporary.unlink(missing_ok=True)
    return load_channel_configs(path)[normalized_channel]


class PublisherAdapter(ABC):
    name: str
    capabilities: ChannelCapabilities

    @abstractmethod
    def prepare(self, payload: Dict[str, object], config: ChannelConfig) -> ChannelVariant:
        raise NotImplementedError

    @abstractmethod
    def validate(self, variant: ChannelVariant, config: ChannelConfig) -> None:
        raise NotImplementedError

    @abstractmethod
    def preview(self, variant: ChannelVariant, config: ChannelConfig) -> Dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    def publish(self, variant: ChannelVariant, config: ChannelConfig) -> object:
        raise NotImplementedError

    def fetch_status(self, publication: Dict[str, object], config: ChannelConfig) -> object:
        raise ValueError(f"status polling is not supported for channel: {self.name}")


class AdapterRegistry:
    def __init__(self, adapters: Optional[Iterable[PublisherAdapter]] = None):
        self._adapters: Dict[str, PublisherAdapter] = {}
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: PublisherAdapter) -> None:
        name = clean_text(adapter.name).lower().replace("-", "_")
        if not name:
            raise ValueError("publisher adapter name is required")
        self._adapters[name] = adapter

    def get(self, name: str) -> PublisherAdapter:
        normalized = clean_text(name).lower().replace("-", "_")
        try:
            return self._adapters[normalized]
        except KeyError as exc:
            raise ValueError(f"unsupported publish target: {normalized or 'unknown'}") from exc

    def names(self) -> list[str]:
        return sorted(self._adapters)


class CallbackPublisherAdapter(PublisherAdapter):
    """Small concrete adapter used to bind a channel implementation to the common contract."""

    def __init__(
        self,
        name: str,
        *,
        capabilities: ChannelCapabilities,
        publish_callback: Callable[[ChannelVariant, ChannelConfig], object],
        preview_callback: Optional[
            Callable[[ChannelVariant, ChannelConfig], Dict[str, object]]
        ] = None,
        status_callback: Optional[Callable[[Dict[str, object], ChannelConfig], object]] = None,
    ):
        self.name = name
        self.capabilities = capabilities
        self._publish_callback = publish_callback
        self._preview_callback = preview_callback
        self._status_callback = status_callback

    def prepare(self, payload: Dict[str, object], config: ChannelConfig) -> ChannelVariant:
        issue = payload.get("canonical_issue")
        canonical = issue if isinstance(issue, dict) else {}
        digest = payload.get("digest") if isinstance(payload.get("digest"), dict) else {}
        return ChannelVariant(
            channel=config.name,
            account_id=config.account_id,
            content_version=max(1, int((payload.get("editor_snapshot") or {}).get("version") or 1)),
            title=clean_text(str(canonical.get("title") or digest.get("title") or "AI 新闻日报")),
            body=clean_text(str(canonical.get("share_summary") or digest.get("overview") or "")),
            canonical_url=clean_text(str(canonical.get("public_url") or "")),
            payload={"source_payload": payload},
        )

    def validate(self, variant: ChannelVariant, config: ChannelConfig) -> None:
        if not variant.title:
            raise ValueError(f"{config.name} variant title is required")
        if config.mode not in VALID_CHANNEL_MODES:
            raise ValueError(f"unsupported channel mode: {config.mode}")

    def preview(self, variant: ChannelVariant, config: ChannelConfig) -> Dict[str, object]:
        if self._preview_callback is None:
            return variant.to_dict()
        return self._preview_callback(variant, config)

    def publish(self, variant: ChannelVariant, config: ChannelConfig) -> object:
        return self._publish_callback(variant, config)

    def fetch_status(self, publication: Dict[str, object], config: ChannelConfig) -> object:
        if self._status_callback is None:
            return super().fetch_status(publication, config)
        return self._status_callback(publication, config)


class WebPublisherAdapter(CallbackPublisherAdapter):
    def __init__(self, **kwargs):
        super().__init__(
            "static_site",
            capabilities=ChannelCapabilities(automatic_publish=True, preview=True),
            **kwargs,
        )


class WeChatPublisherAdapter(CallbackPublisherAdapter):
    def __init__(self, **kwargs):
        super().__init__(
            "wechat",
            capabilities=ChannelCapabilities(
                automatic_publish=True,
                preview=True,
                status_polling=True,
            ),
            **kwargs,
        )


class ManualExportPublisher(CallbackPublisherAdapter):
    def __init__(self, **kwargs):
        super().__init__(
            "manual_export",
            capabilities=ChannelCapabilities(manual_handoff=True, preview=True),
            **kwargs,
        )
