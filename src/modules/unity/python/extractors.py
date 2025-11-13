from pathlib import PurePath
from typing import Protocol

from UnityPy.classes.PPtr import PPtr


class AssetExtractorProtocol(Protocol):
    def get_file_name(self, path: PurePath, pointer: PPtr) -> str:
        ...


class AudioExtractor(AssetExtractorProtocol):
    def get_file_name(self, path: PurePath, pointer: PPtr) -> str:
        return path.name


class GameObjectExtractor(AssetExtractorProtocol):
    def get_file_name(self, path: PurePath, pointer: PPtr) -> str:
        return path.name


class MonoBehaviourExtractor(AssetExtractorProtocol):
    def get_file_name(self, path: PurePath, pointer: PPtr) -> str:
        return path.name


class SpriteExtractor(AssetExtractorProtocol):
    def get_file_name(self, path: PurePath, pointer: PPtr) -> str:
        return path.name


class TextureExtractor(AssetExtractorProtocol):
    def get_file_name(self, path: PurePath, pointer: PPtr) -> str:
        return f"{path.stem}.png"
