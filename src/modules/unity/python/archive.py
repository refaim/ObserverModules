import enum
from dataclasses import dataclass
from pathlib import Path, PurePath

import UnityPy
from UnityPy.enums import ClassIDType
from UnityPy.files import ObjectReader

from extractors import TextureExtractor, GameObjectExtractor, SpriteExtractor, AudioExtractor, MonoBehaviourExtractor


class FileType(enum.Enum):
    AudioClip = enum.auto()
    GameObject = enum.auto()
    MonoBehaviour = enum.auto()
    Sprite = enum.auto()
    Texture2D = enum.auto()


@dataclass(frozen=True)
class File:
    type: FileType
    path: PurePath


ASSET_TYPE_TO_PROPS = {
    ClassIDType.AudioClip: (FileType.AudioClip, AudioExtractor()),
    ClassIDType.GameObject: (FileType.GameObject, GameObjectExtractor()),
    ClassIDType.MonoBehaviour: (FileType.MonoBehaviour, MonoBehaviourExtractor()),
    ClassIDType.Sprite: (FileType.Sprite, SpriteExtractor()),
    ClassIDType.Texture2D: (FileType.Texture2D, TextureExtractor()),
}


class Archive:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._env = None

    def list_files(self) -> list[File]:
        self._load()

        # TODO тут возвращаются дубли по путям, надо разобраться
        files = []
        for item_path_str, item in self._env.container.items():
            item_ref: ObjectReader = item.deref()
            file_type, extractor = ASSET_TYPE_TO_PROPS.get(item_ref.type)
            item_path = PurePath(item_path_str)
            item_path = item_path.parent / extractor.get_file_name(item_path, item)
            files.append(File(file_type, item_path))
        return files

    def _load(self) -> None:
        if self._env is None:
            self._env = UnityPy.load(str(self._path))
